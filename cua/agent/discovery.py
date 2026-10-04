"""Discovery: an AI planner accomplishes a goal on the live app, and the successful run
is recorded as a DRAFT capability.

  clear interruptions -> observe (redacted) -> planner decides ONE action
      -> policy check -> act -> verify -> record -> feedback to the planner

Safety is the same as replay:
  * the system signs on BEFORE the planner starts; the planner never sees credentials;
  * the planner only sees REDACTED screens and input PLACEHOLDERS, never real values;
  * every action passes the policy and the in-browser request guard;
  * known interruptions (popups, expired sessions) are handled from the app profile, not
    by the planner, so they never get recorded as steps of the capability.
"""

import time
from enum import StrEnum
from pathlib import Path
from cua.agent.recorder import Recorder, slugify

from playwright.sync_api import Error as PlaywrightError

from cua.agent.actions import (
    Choose, ClickOn, FieldInRow, Finish, GiveUp, NamedControl, NavigateTo, ReadValue,
    TypeInto, to_target,
)
from cua.agent.planner import Feedback, Planner, PlannerError, PlannerRequest, Turn
from cua.agent.recorder import Recorder
from cua.evidence import RUNS_DIR, RunLog
from cua.policy import Policy, Verdict
from cua.redaction import Redactor
from cua.replay.engine import parse_money
from cua.schema import (
    TEMPLATE_RE, Anchor, AppProfile, Capability, ClickAction, Detector, DismissRecovery,
    ExtractAction, FillAction, InputParam, NavigateAction, ReauthenticateRecovery, Risk,
    SelectAction, Sensitivity, Step, StrictModel,
)
from cua.secrets import MissingSecret, SecretStore
from cua.surface import NavigationBlocked, SurfaceError, TargetNotFound, open_web_surface
from cua.templating import TemplateError, render, render_model

POLL_SECONDS = 0.1


class DiscoveryStatus(StrEnum):
    RECORDED = "recorded"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"


class InterventionRequest(StrictModel):
    """Everything a human operator needs to pick up where automation stopped."""
    goal: str
    reason: str
    turn: int
    route: str
    last_action: str | None = None
    screenshot: str | None = None


class DiscoveryResult(StrictModel):
    run_id: str
    status: DiscoveryStatus
    turns: int
    capability: Capability | None = None
    intervention: InterventionRequest | None = None
    failure: str | None = None


class _Halt(Exception):
    """Raised anywhere during discovery to end it with a specific result."""

    def __init__(self, status: DiscoveryStatus, *, capability: Capability | None = None,
                 intervention: InterventionRequest | None = None, failure: str | None = None):
        super().__init__(status.value)
        self.status = status
        self.capability = capability
        self.intervention = intervention
        self.failure = failure


class _ExtractError(Exception):
    pass


class Discoverer:
    """Configured once per app and tenant; run() can then be called for any goal."""

    def __init__(self, profile: AppProfile, policy: Policy, base_url: str, planner: Planner, *,
                 secrets: SecretStore | None = None, headless: bool = True,
                 runs_dir: Path = RUNS_DIR, max_turns: int = 15,
                 max_consecutive_failures: int = 3):
        self.profile = profile
        self.policy = policy
        self.base_url = base_url
        self.planner = planner
        self.secrets = secrets or SecretStore()
        self.headless = headless
        self.runs_dir = Path(runs_dir)
        self.max_turns = max_turns
        self.max_consecutive_failures = max_consecutive_failures

    def run(self, *, goal: str, capability_id: str, inputs: list[InputParam],
            input_values: dict[str, str]) -> DiscoveryResult:
        return _Session(self, goal, capability_id, inputs, input_values).run()


class _Session:
    """Everything belonging to ONE discovery run."""

    def __init__(self, config: Discoverer, goal: str, capability_id: str,
                 inputs: list[InputParam], input_values: dict[str, str]):
        self.config = config
        self.profile = config.profile
        self.policy = config.policy
        self.goal = goal
        self.capability_id = capability_id
        self.inputs = inputs
        self.input_values = input_values
        self.redactor = Redactor(self.profile.sensitive_labels)
        self.history: list[Turn] = []
        self.turn = 0
        self.surface = None
        self.log: RunLog | None = None
        self.recorder: Recorder | None = None
        self.secret_values: dict[str, str] = {}

    # --- The run as a whole ------------------------------------------------------

    def run(self) -> DiscoveryResult:
        for param in self.inputs:  # BEFORE the log exists, so nothing is written unredacted
            self.redactor.register(param.name, self.input_values.get(param.name), param.sensitivity)
        with RunLog("discover", self.redactor, root=self.config.runs_dir) as log:
            self.log = log
            log.event("run_started", f"discovering '{self.capability_id}'", goal=self.goal,
                      planner=self.config.planner.name, inputs=self.input_values)
            try:
                self._preflight()
                with open_web_surface(self.config.base_url, self.profile.timing,
                                      headless=self.config.headless,
                                      request_guard=self.policy.allows_request) as surface:
                    self.surface = surface
                    self._sign_on()
                    self.recorder = Recorder(
                        capability_id=self.capability_id, app_profile=self.profile.id,
                        goal=self.goal, inputs=self.inputs, input_values=self.input_values,
                        planner_name=self.config.planner.name, run_id=log.run_id)
                    self._loop()  # always ends by raising _Halt
            except _Halt as halt:
                result = DiscoveryResult(run_id=log.run_id, status=halt.status, turns=self.turn,
                                         capability=halt.capability,
                                         intervention=halt.intervention, failure=halt.failure)
            log.event("run_finished", f"status: {result.status.value}", turns=self.turn)
            log.write_summary(result.model_dump(mode="json"))
        return result

    def _preflight(self) -> None:
        origin = self.policy.check_origin(self.config.base_url)
        if not origin.allowed:
            raise _Halt(DiscoveryStatus.FAILED, failure=origin.reason)
        declared = {p.name for p in self.inputs}
        unknown = sorted({name for ns, name in TEMPLATE_RE.findall(self.goal)
                          if ns != "inputs" or name not in declared})
        if unknown:
            raise _Halt(DiscoveryStatus.FAILED, failure=f"goal uses undeclared placeholders {unknown}")
        missing = [p.name for p in self.inputs if p.required and not self.input_values.get(p.name)]
        if missing:
            raise _Halt(DiscoveryStatus.FAILED, failure=f"missing values for inputs {missing}")
        try:
            self.secret_values = self.config.secrets.resolve(self.profile.secrets)
        except MissingSecret as exc:
            raise _Halt(DiscoveryStatus.FAILED, failure=str(exc)) from None
        for name, value in self.secret_values.items():
            self.redactor.register(name, value, Sensitivity.SECRET)

    # --- Before the planner looks: sign-on and interruptions ------------------------

    def _sign_on(self) -> None:
        sign_on = self.profile.sign_on
        if self.surface.current_route() != sign_on.route:
            self.surface.goto(sign_on.route)
        for step in sign_on.steps:
            rendered = render_model(step, {"secrets": self.secret_values})
            decision = self.policy.check_step(rendered, self.surface.current_route())
            if not decision.allowed:
                raise self._halt_failed(f"sign-on step '{step.id}' refused: {decision.reason}")
            try:
                if isinstance(rendered.action, FillAction):
                    self.surface.fill(rendered.target, rendered.action.value)
                elif isinstance(rendered.action, ClickAction):
                    self.surface.click(rendered.target)
            except (SurfaceError, PlaywrightError) as exc:
                raise self._halt_failed(f"sign-on failed at '{step.id}': {_first_line(exc)}") from None
        if not self._wait_visible(sign_on.success.visible):
            raise self._halt_failed("sign-on did not reach a signed-in page")
        self.log.event("signed_on", "signed on (the planner never sees this)")

    def _clear_interruptions(self) -> None:
        """Handle known popups and expired sessions from the app profile, NOT the planner."""
        for _ in range(self.profile.timing.max_recoveries_per_step + 1):
            found = next(((name, item) for name, item in self.profile.interstitials.items()
                          if self._detected(item.detect)), None)
            if found is None:
                return
            name, interstitial = found
            if interstitial.kind == "hard_failure":
                raise self._halt_failed(f"{name}: {interstitial.description}")
            self.log.event("recovering", f"'{name}' handled by the app profile", turn=self.turn)
            try:
                match interstitial.recovery:
                    case DismissRecovery(target=target):
                        self.surface.click(target)
                    case ReauthenticateRecovery():
                        self._sign_on()
            except (SurfaceError, PlaywrightError) as exc:
                raise self._halt_failed(f"could not recover from '{name}': {_first_line(exc)}") from None
        raise self._halt_failed("interruptions kept recurring")

    # --- The planner loop -----------------------------------------------------------

    def _loop(self) -> None:
        for turn in range(1, self.config.max_turns + 1):
            self.turn = turn
            self._clear_interruptions()
            observation = self.surface.observe()
            screen = self.redactor.redact(observation.accessibility_tree)
            route = self.redactor.redact(observation.route)
            self.log.event("observation", "screen shown to the planner", turn=turn,
                           route=route, screen=screen)

            request = PlannerRequest(goal=self.goal, input_names=[p.name for p in self.inputs],
                                     screen=screen, route=route, history=list(self.history),
                                     steps_remaining=self.config.max_turns - turn + 1)
            try:
                action = self.config.planner.decide(request)
            except PlannerError as exc:
                raise self._halt_failed(f"planner error: {exc}") from None
            self.log.event("decision", action.intent, turn=turn,
                           action=action.model_dump(mode="json"))

            match action:
                case Finish():
                    self._finish(action)
                case GiveUp():
                    raise self._halt_human(f"the planner gave up: {action.intent}", action)

            feedback = self._attempt(action)
            self.history.append(Turn(action, feedback))
            self.log.event("feedback", feedback.message, turn=turn, ok=feedback.ok)
            self._check_not_stuck()

        raise self._halt_human(
            f"used all {self.config.max_turns} actions without finishing", None)

    def _finish(self, action: Finish) -> None:
        try:
            capability = self.recorder.build(action.intent)
        except ValueError as exc:  # RecordingError, or the schema rejecting the result
            raise self._halt_human(f"the planner finished, but the recording is invalid: {exc}",
                                   action) from None
        self.log.event("recorded", f"recorded {len(capability.steps)} steps",
                       step_ids=[step.id for step in capability.steps])
        raise _Halt(DiscoveryStatus.RECORDED, capability=capability)

    def _check_not_stuck(self) -> None:
        limit = self.config.max_consecutive_failures
        recent = self.history[-limit:]
        if len(recent) == limit and not any(turn.feedback.ok for turn in recent):
            raise self._halt_human(
                f"stuck: {limit} failed attempts in a row; last error: {recent[-1].feedback.message}",
                recent[-1].action)

    # --- Trying one action -------------------------------------------------------------

    def _attempt(self, action) -> Feedback:
        try:
            step = self._provisional_step(action)
        except (TemplateError, ValueError) as exc:
            return self._feedback(False, f"invalid action: {exc}")

        route_before = self.surface.current_route()
        decision = self.policy.check_step(step, route_before)
        self.log.event("policy", decision.reason, turn=self.turn)
        if decision.verdict == Verdict.DENY:
            return self._feedback(False, f"not allowed: {decision.reason}")
        if decision.verdict == Verdict.REQUIRE_APPROVAL:
            raise self._halt_human(f"approval required: {decision.reason}", action)

        fallback_index = None
        try:
            target = getattr(action, "target", None)
            if isinstance(target, (NamedControl, FieldInRow)):
                # Measure BEFORE acting: a click may make the control disappear.
                fallback_index = self.surface.position_among_role(step.target, target.role)
            self._perform(action, step)
        except TargetNotFound as exc:
            return self._feedback(False, f"{exc}; describe the control more specifically")
        except NavigationBlocked as exc:
            return self._feedback(False, f"blocked by policy: {exc}")
        except _ExtractError as exc:
            return self._feedback(False, str(exc))
        except (SurfaceError, PlaywrightError) as exc:
            return self._feedback(False, f"action failed: {_first_line(exc)}")

        expected = getattr(action, "expect", None)
        landmark_seen = expected is not None and self._wait_visible([expected])
        route_after = self.surface.current_route()
        recorded = self.recorder.record(
            action, risk=self.policy.effective_risk(step), route_before=route_before,
            route_after=route_after, landmark_seen=landmark_seen, fallback_index=fallback_index)

        message = f"done (recorded as step '{recorded.id}'); now on {route_after}"
        if expected is not None and not landmark_seen:
            message += f"; note: expected {expected.role} '{expected.name}' was NOT seen"
        return self._feedback(True, message)

    def _provisional_step(self, action) -> Step:
        """A filled-in Step for the policy check and for acting. Never recorded or logged."""
        values = {"inputs": self.input_values}
        step_id = slugify(action.intent)  # meaningful in policy reasons and intervention requests
        match action:
            case NavigateTo():
                return Step(id=step_id, intent=action.intent, risk=Risk.READ_ONLY,
                            action=NavigateAction(route=render(action.route, values)))
            case TypeInto():
                return Step(id=step_id, intent=action.intent, risk=Risk.REVERSIBLE,
                            action=FillAction(value=render(action.value, values)),
                            target=to_target(action.target))
            case Choose():
                return Step(id=step_id, intent=action.intent, risk=Risk.REVERSIBLE,
                            action=SelectAction(value=render(action.value, values)),
                            target=to_target(action.target))
            case ClickOn():
                return Step(id=step_id, intent=action.intent, risk=action.risk,
                            action=ClickAction(), target=to_target(action.target))
            case ReadValue():
                return Step(id=step_id, intent=action.intent, risk=Risk.READ_ONLY,
                            action=ExtractAction(into=action.output, parse=action.parse),
                            target=to_target(action.target))
        raise ValueError(f"'{action.action}' cannot be performed")

    def _perform(self, action, step: Step) -> None:
        match step.action:
            case NavigateAction(route=route):
                self.surface.goto(route)
            case FillAction(value=value):
                self.surface.fill(step.target, value)
            case SelectAction(value=value):
                self.surface.select(step.target, value)
            case ClickAction():
                self.surface.click(step.target)
            case ExtractAction(into=into, parse=parse):
                text, _ = self.surface.read_text(step.target)
                self.redactor.register(into, text, action.sensitivity)  # before anything logs it
                value = parse_money(text) if parse == "money" else text
                if value is None:
                    raise _ExtractError(f"could not read '{into}' as money")
                self.redactor.register(into, value, action.sensitivity)

    def _feedback(self, ok: bool, message: str) -> Feedback:
        return Feedback(ok, self.redactor.redact(message))  # the planner only sees redacted text

    # --- Ending the run, with evidence ------------------------------------------------

    def _halt_human(self, reason: str, action) -> _Halt:
        screenshot = self._capture(f"intervention-turn{self.turn}")
        intervention = InterventionRequest(
            goal=self.goal, reason=reason, turn=self.turn,
            route=self.surface.current_route() if self.surface else "",
            last_action=action.intent if action is not None else None, screenshot=screenshot)
        self.log.event("intervention_requested", reason, turn=self.turn)
        return _Halt(DiscoveryStatus.NEEDS_HUMAN, intervention=intervention)

    def _halt_failed(self, message: str) -> _Halt:
        self._capture(f"failure-turn{self.turn}")
        self.log.event("failure", message, turn=self.turn)
        return _Halt(DiscoveryStatus.FAILED, failure=message)

    def _capture(self, label: str) -> str | None:
        if self.surface is None:
            return None
        path = self.log.screenshot_path(label)
        try:
            self.surface.screenshot(path, self.profile.sensitive_labels)
        except PlaywrightError:
            return None
        return str(path)

    # --- Small helpers ------------------------------------------------------------------

    def _detected(self, detector: Detector) -> bool:
        if detector.url is not None and self.surface.current_route() != detector.url:
            return False
        if detector.text is not None and not self.surface.text_visible(detector.text):
            return False
        return True

    def _wait_visible(self, anchors: list[Anchor]) -> bool:
        deadline = time.monotonic() + self.profile.timing.navigation_timeout_ms / 1000
        while time.monotonic() < deadline:
            if all(self.surface.control_visible(anchor) for anchor in anchors):
                return True
            time.sleep(POLL_SECONDS)
        return all(self.surface.control_visible(anchor) for anchor in anchors)


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__