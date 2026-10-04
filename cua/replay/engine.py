"""Deterministic replay: run a saved capability with NO LLM in the decision loop.

This is the production path. An AI agent calls run() with a capability and its
inputs, and gets back a ReplayResult: success, business_outcome, failed or
needs_human. Every run leaves a redacted evidence trail in runs/<run_id>/.
"""

import re
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from cua.evidence import RUNS_DIR, RunLog
from cua.policy import Policy, Verdict
from cua.redaction import Redactor
from cua.replay.result import (
    EscalationInfo, FailureCategory, FailureInfo, OutcomeInfo, ReplayResult, ReplayStatus,
)
from cua.schema import (
    AppProfile, Capability, ClickAction, Expectation, ExtractAction, FillAction,
    NavigateAction, SelectAction, Sensitivity, Status, Step,
)
from cua.secrets import MissingSecret, SecretStore
from cua.surface import NavigationBlocked, Resolution, Surface, TargetNotFound, open_web_surface
from cua.templating import render_model

SLOW_STEP_SECONDS = 2.0  # steps slower than this are flagged in warnings
POLL_SECONDS = 0.1


def parse_money(text: str) -> str | None:
    """'$2,450.75' -> '2450.75'. Returns None if the text isn't an amount."""
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return str(amount.quantize(Decimal("0.01")))


class _Stop(Exception):
    """Raised anywhere during a run to end it immediately with a specific result."""

    def __init__(self, status: ReplayStatus, *, outcome: OutcomeInfo | None = None,
                 failure: FailureInfo | None = None, escalation: EscalationInfo | None = None):
        super().__init__(status.value)
        self.status = status
        self.outcome = outcome
        self.failure = failure
        self.escalation = escalation


class Replayer:
    """Configured once per app and tenant; run() can then be called for any capability."""

    def __init__(self, profile: AppProfile, policy: Policy, base_url: str, *,
                 secrets: SecretStore | None = None, allow_draft: bool = False,
                 headless: bool = True, runs_dir: Path = RUNS_DIR):
        self.profile = profile
        self.policy = policy
        self.base_url = base_url
        self.secrets = secrets or SecretStore()
        self.allow_draft = allow_draft
        self.headless = headless
        self.runs_dir = Path(runs_dir)

    def run(self, capability: Capability, args: Mapping[str, str]) -> ReplayResult:
        return _Execution(self, capability, dict(args)).run()


class _Execution:
    """Everything belonging to ONE run: its inputs, outputs, surface and evidence log."""

    def __init__(self, config: Replayer, capability: Capability, args: dict[str, str]):
        self.config = config
        self.profile = config.profile
        self.policy = config.policy
        self.capability = capability
        self.args = args
        self.label = f"{capability.meta.id}@{capability.meta.version}"
        self.redactor = Redactor(self.profile.sensitive_labels)
        self.surface: Surface | None = None
        self.log: RunLog | None = None
        self.inputs: dict[str, str] = {}
        self.secret_values: dict[str, str] = {}
        self.outputs: dict[str, str] = {}
        self.warnings: list[str] = []
        self.recoveries: list[str] = []
        self.started = time.monotonic()

    # --- The run as a whole ------------------------------------------------------

    def run(self) -> ReplayResult:
        self._register_args()  # BEFORE the log exists, so nothing is ever written unredacted
        with RunLog("replay", self.redactor, root=self.config.runs_dir) as log:
            self.log = log
            log.event("run_started", f"replaying {self.label}", inputs=self.args)
            try:
                self._preflight()
                with open_web_surface(self.config.base_url, self.profile.timing,
                                      headless=self.config.headless,
                                      request_guard=self.policy.allows_request) as surface:
                    self.surface = surface
                    self._sign_on()
                    for step in self.capability.steps:
                        self._run_step(step)
                    self._check_success()
                result = self._result(ReplayStatus.SUCCESS)
            except _Stop as stop:
                result = self._result(stop.status, outcome=stop.outcome,
                                      failure=stop.failure, escalation=stop.escalation)
            log.event("run_finished", f"status: {result.status.value}",
                      result=result.model_dump(mode="json"))
            log.write_summary(result.model_dump(mode="json"))
        return result

    def _result(self, status: ReplayStatus, **details) -> ReplayResult:
        return ReplayResult(
            run_id=self.log.run_id,
            capability=self.label,
            status=status,
            outputs=dict(self.outputs) if status == ReplayStatus.SUCCESS else {},
            warnings=self.warnings,
            recoveries=self.recoveries,
            duration_ms=int((time.monotonic() - self.started) * 1000),
            **details,
        )

    # --- Before touching the application -----------------------------------------

    def _register_args(self) -> None:
        declared = {p.name: p for p in self.capability.inputs}
        for name, value in self.args.items():
            # Undeclared arguments are unknown, so treat them as personal data to be safe.
            sensitivity = declared[name].sensitivity if name in declared else Sensitivity.PII
            self.redactor.register(name, value, sensitivity)

    def _preflight(self) -> None:
        meta = self.capability.meta
        if meta.app_profile != self.profile.id:
            raise self._failure(FailureCategory.CONFIGURATION_ERROR,
                                f"capability is for app profile '{meta.app_profile}', "
                                f"not '{self.profile.id}'")
        if meta.status == Status.DEPRECATED or (
                meta.status == Status.DRAFT and not self.config.allow_draft):
            raise self._failure(FailureCategory.ARTIFACT_NOT_APPROVED,
                                f"capability status is '{meta.status.value}'; "
                                "only approved capabilities run unattended")
        origin = self.policy.check_origin(self.config.base_url)
        if not origin.allowed:
            raise self._failure(FailureCategory.POLICY_DENIED, origin.reason)

        self.inputs = self._validated_inputs()

        try:
            self.secret_values = self.config.secrets.resolve(self.profile.secrets)
        except MissingSecret as exc:
            raise self._failure(FailureCategory.CONFIGURATION_ERROR, str(exc)) from None
        for name, value in self.secret_values.items():
            self.redactor.register(name, value, Sensitivity.SECRET)
        self.log.event("preflight_passed", "inputs, approval, policy and secrets OK")

    def _validated_inputs(self) -> dict[str, str]:
        problems: list[str] = []
        declared = {p.name for p in self.capability.inputs}
        unknown = sorted(set(self.args) - declared)
        if unknown:
            problems.append(f"unknown inputs: {unknown}")
        values: dict[str, str] = {}
        for param in self.capability.inputs:
            value = self.args.get(param.name, param.default)
            if value is None or value == "":
                if param.required:
                    problems.append(f"missing required input '{param.name}'")
                continue
            if param.pattern and not re.fullmatch(param.pattern, value):
                # Never echo the value itself: it may be sensitive.
                problems.append(f"input '{param.name}' does not match its required format")
            values[param.name] = value
        if problems:
            raise self._failure(FailureCategory.INVALID_INPUT, "; ".join(problems))
        return values

    # --- Signing on ---------------------------------------------------------------

    def _sign_on(self) -> None:
        sign_on = self.profile.sign_on
        self.log.event("sign_on", "signing on")
        if self.surface.current_route() != sign_on.route:
            self.surface.goto(sign_on.route)
        for step in sign_on.steps:
            self._run_step(step, values={"secrets": self.secret_values}, sign_on=True)
        if not self._wait_for(sign_on.success):
            raise self._failure(FailureCategory.SIGN_ON_FAILED, "sign-on did not reach a signed-in page",
                                expected=self._describe(sign_on.success),
                                observed=self._describe_screen())
        self.log.event("signed_on", "signed on")

    # --- Running one step ---------------------------------------------------------

    def _run_step(self, step: Step, values: dict | None = None, *, sign_on: bool = False) -> None:
        values = values if values is not None else {"inputs": self.inputs}
        self._handle_interruptions(step)  # filled in during part 3b

        rendered = render_model(step, values)  # filled-in copy: used for acting, NEVER logged
        decision = self.policy.check_step(rendered, self.surface.current_route())
        self.log.event("step_started", step.intent, step_id=step.id,
                       action=step.action.type, risk=step.risk.value, policy=decision.reason)
        if decision.verdict == Verdict.DENY:
            raise self._failure(FailureCategory.POLICY_DENIED, decision.reason, step=step)
        if decision.verdict == Verdict.REQUIRE_APPROVAL:
            raise self._escalation(step, decision.reason)

        not_found = FailureCategory.SIGN_ON_FAILED if sign_on else FailureCategory.TARGET_NOT_FOUND
        timed_out = FailureCategory.SIGN_ON_FAILED if sign_on else FailureCategory.TIMEOUT
        started = time.monotonic()
        try:
            self._act(rendered)
        except TargetNotFound as exc:
            raise self._failure(not_found, str(exc), step=step,
                                expected=f"exactly one '{step.target.description}'",
                                observed=self._describe_screen()) from None
        except NavigationBlocked as exc:
            raise self._failure(FailureCategory.POLICY_DENIED, str(exc), step=step) from None
        except PlaywrightTimeout as exc:
            raise self._failure(timed_out, _first_line(exc), step=step,
                                observed=self._describe_screen()) from None
        except PlaywrightError as exc:
            raise self._failure(FailureCategory.APP_ERROR, _first_line(exc), step=step) from None

        if rendered.expect is not None:
            self._verify(step, rendered.expect, sign_on=sign_on)

        elapsed = time.monotonic() - started
        if elapsed > SLOW_STEP_SECONDS:
            self.warnings.append(f"step '{step.id}' took {elapsed:.1f}s (slow response)")
        self.log.event("step_done", "step complete", step_id=step.id, seconds=round(elapsed, 2))

    def _act(self, step: Step) -> None:
        resolution: Resolution | None = None
        match step.action:
            case NavigateAction(route=route):
                self.surface.goto(route)
            case FillAction(value=value):
                resolution = self.surface.fill(step.target, value)
            case SelectAction(value=value):
                resolution = self.surface.select(step.target, value)
            case ClickAction():
                resolution = self.surface.click(step.target)
            case ExtractAction(into=into, parse=parse):
                text, resolution = self.surface.read_text(step.target)
                self._store_output(step, into, parse, text)
        if resolution is not None and resolution.used_fallback:
            warning = (f"step '{step.id}' used fallback locator #{resolution.locator_index + 1} "
                       f"({resolution.strategy}): possible UI drift")
            self.warnings.append(warning)
            self.log.event("drift_warning", warning, step_id=step.id)

    def _store_output(self, step: Step, into: str, parse: str, text: str) -> None:
        output = next(o for o in self.capability.outputs if o.name == into)
        self.redactor.register(into, text, output.sensitivity)  # the on-screen form, e.g. "$2,450.75"
        value = parse_money(text) if parse == "money" else text
        if value is None:
            raise self._failure(FailureCategory.CHECKPOINT_FAILED, f"could not read '{into}' as money",
                                step=step, expected="an amount like $1,234.56",
                                observed="unparseable text")
        self.redactor.register(into, value, output.sensitivity)  # the parsed form, e.g. "2450.75"
        self.outputs[into] = value
        self.log.event("extracted", f"read output '{into}'", step_id=step.id, value=value)

    # --- Checkpoints --------------------------------------------------------------

    def _verify(self, step: Step, expect: Expectation, *, sign_on: bool = False) -> None:
        """Wait until the step's expectation holds. Never assume an action worked."""
        deadline = time.monotonic() + self.profile.timing.navigation_timeout_ms / 1000
        while True:
            if self._expectation_met(expect):
                self.log.event("checkpoint_passed", self._describe(expect), step_id=step.id)
                return
            if self._scan_known_states(step):  # filled in during part 3b
                continue
            if time.monotonic() >= deadline:
                category = (FailureCategory.SIGN_ON_FAILED if sign_on
                            else FailureCategory.CHECKPOINT_FAILED)
                raise self._failure(category, "the screen did not reach the expected state",
                                    step=step, expected=self._describe(expect),
                                    observed=self._describe_screen())
            time.sleep(POLL_SECONDS)

    def _wait_for(self, expect: Expectation) -> bool:
        deadline = time.monotonic() + self.profile.timing.navigation_timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._expectation_met(expect):
                return True
            time.sleep(POLL_SECONDS)
        return self._expectation_met(expect)

    def _expectation_met(self, expect: Expectation) -> bool:
        if expect.url is not None and self.surface.current_route() != expect.url:
            return False
        return all(self.surface.control_visible(anchor) for anchor in expect.visible)

    def _check_success(self) -> None:
        success = self.capability.success
        missing = [name for name in success.outputs_present if name not in self.outputs]
        not_visible = [a for a in success.visible if not self.surface.control_visible(a)]
        if missing or not_visible:
            raise self._failure(FailureCategory.CHECKPOINT_FAILED, "success condition not met",
                                expected=f"outputs {success.outputs_present}",
                                observed=f"missing outputs {missing}, "
                                         f"missing controls {[a.name for a in not_visible]}")
        self.log.event("success_condition_met", "all declared outputs present")

    # --- Extension points, filled in during part 3b ------------------------------

    def _handle_interruptions(self, step: Step) -> None:
        """Before acting: detect and recover from known popups or expired sessions."""
        return None

    def _scan_known_states(self, step: Step) -> bool:
        """While a checkpoint isn't met: does a KNOWN state explain the screen?

        Raises _Stop for a business outcome or hard failure; returns True if it recovered
        from something (so the checkpoint should be re-checked); False if nothing matched.
        """
        return False

    # --- Ending a run, with evidence ----------------------------------------------

    def _failure(self, category: FailureCategory, message: str, *, step: Step | None = None,
                 expected: str | None = None, observed: str | None = None) -> _Stop:
        screenshot = self._capture_evidence(f"failure-{step.id if step else 'preflight'}")
        self.log.event("failure", message, step_id=step.id if step else None,
                       category=category.value, expected=expected, observed=observed)
        failure = FailureInfo.build(category, message,
                                    step_id=step.id if step else None,
                                    intent=step.intent if step else None,
                                    expected=expected, observed=observed, screenshot=screenshot)
        return _Stop(ReplayStatus.FAILED, failure=failure)

    def _escalation(self, step: Step, reason: str) -> _Stop:
        screenshot = self._capture_evidence(f"escalation-{step.id}")
        self.log.event("escalation", reason, step_id=step.id)
        return _Stop(ReplayStatus.NEEDS_HUMAN, escalation=EscalationInfo(
            step_id=step.id, intent=step.intent, reason=reason, screenshot=screenshot))

    def _capture_evidence(self, label: str) -> str | None:
        """Masked screenshot plus a redacted text snapshot of the screen."""
        if self.surface is None:
            return None  # failed before the browser opened: nothing to capture
        path = self.log.screenshot_path(label)
        try:
            self.surface.screenshot(path, self.profile.sensitive_labels)
            observation = self.surface.observe()
        except PlaywrightError:
            return None
        self.log.event("screen", "captured evidence", route=observation.route,
                       accessibility_tree=observation.accessibility_tree, screenshot=str(path))
        return str(path)

    def _describe(self, expect: Expectation) -> str:
        parts = []
        if expect.url:
            parts.append(f"route {expect.url}")
        parts += [f"{a.role} '{a.name}' visible" for a in expect.visible]
        return "; ".join(parts)

    def _describe_screen(self) -> str:
        return f"route {self.surface.current_route()}" if self.surface else "no screen"


def _first_line(exc: Exception) -> str:
    """Playwright errors include a long call log; the first line says what happened."""
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__