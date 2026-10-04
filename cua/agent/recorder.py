"""The recorder: turns the planner's SUCCESSFUL actions into a capability artifact.

It is a pure transformation of facts the discovery loop has already verified on the
live screen: the action that worked, the route before and after, whether the expected
landmark really appeared, and the control's position (for a fallback locator).
No browser, no LLM, no I/O, so it can be tested exhaustively offline.

Every artifact it produces is a DRAFT with no business outcomes: a happy-path run never
sees "No member found", so a reviewer adds outcomes before approving the capability.
"""

import re
from datetime import datetime, timezone

from cua.agent.actions import (
    Choose, ClickOn, FieldInRow, NamedControl, NavigateTo, ReadValue, TableCell, TypeInto,
)
from cua.schema import (
    Anchor, Capability, CapabilityMeta, ClickAction, Expectation, ExtractAction, FillAction,
    InputParam, NavigateAction, NthOfRoleLocator, OutputParam, Provenance, Risk,
    SelectAction, Status, Step, SuccessCondition, Target, ValueType,
)

RATIONALE = {
    "named": "Control has its own accessible name: the most robust rung.",
    "in_row": "Control has no accessible name; anchored to the innermost row carrying its label.",
    "table_cell": "Found by meaning (row label x column header), never by position.",
}
FALLBACK_NOTE = (" Position fallback captured at discovery time: fragile, used only if the "
                 "primary fails, and logged as drift when it is.")


class RecordingError(ValueError):
    """The recorded actions can't form a valid capability."""


def slugify(text: str, max_length: int = 40) -> str:
    """'Type the member ID' -> 'type_the_member_id' (a valid step id)."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"step_{slug}".strip("_")
    if len(slug) > max_length:
        slug = slug[:max_length].rsplit("_", 1)[0] or slug[:max_length]
    return slug


class Recorder:
    def __init__(self, *, capability_id: str, app_profile: str, goal: str,
                 inputs: list[InputParam], input_values: dict[str, str],
                 planner_name: str, run_id: str):
        self.capability_id = capability_id
        self.app_profile = app_profile
        self.goal = goal
        self.inputs = inputs
        self.input_values = input_values
        self.planner_name = planner_name
        self.run_id = run_id
        self._steps: list[Step] = []
        self._outputs: list[OutputParam] = []

    @property
    def steps(self) -> list[Step]:
        return list(self._steps)

    # --- Recording one successful action ---------------------------------------------

    def record(self, action, *, risk: Risk, route_before: str, route_after: str,
               landmark_seen: bool, fallback_index: int | None = None) -> Step:
        """Turn one verified, successful action into a step."""
        target = None
        expect = None
        match action:
            case NavigateTo():
                step_action = NavigateAction(route=self._parameterize_route(action.route))
                expect = self._expectation(
                    url=route_after, anchor=action.expect if landmark_seen else None)
            case TypeInto():
                step_action = FillAction(value=self._parameterize_value(action.value))
                target = self._target(action.target, fallback_index)
            case Choose():
                step_action = SelectAction(value=self._parameterize_value(action.value))
                target = self._target(action.target, fallback_index)
            case ClickOn():
                step_action = ClickAction()
                target = self._target(action.target, fallback_index)
                expect = self._expectation(
                    url=route_after if route_after != route_before else None,
                    anchor=action.expect if landmark_seen else None)
            case ReadValue():
                step_action = ExtractAction(into=action.output, parse=action.parse)
                target = self._target(action.target, fallback_index)
                self._declare_output(action)
            case _:
                raise RecordingError(f"'{action.action}' is not a recordable action")

        step = Step(id=self._unique_id(action.intent), intent=action.intent,
                    action=step_action, target=target, risk=risk, expect=expect)
        self._steps.append(step)
        return step

    # --- Assembling the capability ----------------------------------------------------

    def build(self, summary: str) -> Capability:
        if not self._steps:
            raise RecordingError("nothing was recorded")
        if self._outputs:
            success = SuccessCondition(outputs_present=[o.name for o in self._outputs])
        else:
            # No data to return (e.g. "reach the confirmation screen"): success is the last
            # landmark we SAW on the real screen.
            last = next((s.expect for s in reversed(self._steps)
                         if s.expect is not None and s.expect.visible), None)
            if last is None:
                raise RecordingError("no outputs and no verified landmark to define success")
            success = SuccessCondition(visible=list(last.visible))

        return Capability(
            schema_version="1.0",
            meta=CapabilityMeta(id=self.capability_id, version="1.0.0", status=Status.DRAFT,
                                summary=summary, app_profile=self.app_profile),
            inputs=self.inputs,
            outputs=self._outputs,
            steps=self._steps,
            outcomes={},
            success=success,
            provenance=Provenance(
                discovered_by=self.planner_name,
                discovery_run_id=self.run_id,
                recorded_at=datetime.now(timezone.utc).replace(microsecond=0),
                goal=self.goal),
        )

    # --- Helpers ------------------------------------------------------------------------

    def _target(self, spec: NamedControl | FieldInRow | TableCell,
                fallback_index: int | None) -> Target:
        locators = [spec.locator()]
        rationale = RATIONALE[spec.kind]
        if fallback_index is not None and not isinstance(spec, TableCell):
            locators.append(NthOfRoleLocator(role=spec.role, index=fallback_index))
            rationale += FALLBACK_NOTE
        return Target(description=spec.describe(), locators=locators, rationale=rationale)

    def _expectation(self, *, url: str | None, anchor: Anchor | None) -> Expectation | None:
        if url is None and anchor is None:
            return None
        return Expectation(url=self._parameterize_route(url) if url else None,
                           visible=[anchor] if anchor else [])

    def _placeholder_for(self, value: str) -> str | None:
        for name, known in self.input_values.items():
            if known and value == known:
                return "{{inputs." + name + "}}"
        return None

    def _parameterize_value(self, value: str) -> str:
        """Safety net: if the planner typed an input's literal value, store the placeholder."""
        return self._placeholder_for(value) or value

    def _parameterize_route(self, route: str) -> str:
        """'/member/12345' -> '/member/{{inputs.member_id}}' (whole path segments only)."""
        segments = route.split("/")
        return "/".join(self._placeholder_for(segment) or segment for segment in segments)

    def _declare_output(self, action: ReadValue) -> None:
        if any(output.name == action.output for output in self._outputs):
            raise RecordingError(f"output '{action.output}' was read twice")
        self._outputs.append(OutputParam(
            name=action.output,
            type=ValueType.MONEY if action.parse == "money" else ValueType.STRING,
            description=action.intent,
            sensitivity=action.sensitivity))

    def _unique_id(self, intent: str) -> str:
        base = slugify(intent)
        existing = {step.id for step in self._steps}
        candidate, n = base, 2
        while candidate in existing:
            candidate, n = f"{base}_{n}", n + 1
        return candidate