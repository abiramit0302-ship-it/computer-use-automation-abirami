"""The recorder turns verified actions into a valid, parameterized, reviewable draft."""

import pytest

from cua.agent.actions import parse_action
from cua.agent.recorder import Recorder, RecordingError, slugify
from cua.schema import (
    InputParam, NthOfRoleLocator, Risk, Sensitivity, Status, ValueType, load_capability, save,
)

INPUTS = [InputParam(name="member_id", type=ValueType.STRING,
                     description="5-digit member number", sensitivity=Sensitivity.IDENTIFIER)]


def make_recorder() -> Recorder:
    return Recorder(capability_id="member.lookup_savings_balance",
                    app_profile="harbor-member-console",
                    goal="Look up member {{inputs.member_id}} and read their savings balance",
                    inputs=INPUTS, input_values={"member_id": "12345"},
                    planner_name="scripted-mock", run_id="discover-test")


def record_flow_a(recorder: Recorder):
    """The facts the discovery loop would hand over for a successful Flow A run."""
    recorder.record(parse_action({
        "action": "navigate", "intent": "Open the member inquiry screen", "route": "/search",
        "expect": {"role": "cell", "name": "Member Inquiry"}}),
        risk=Risk.READ_ONLY, route_before="/search", route_after="/search", landmark_seen=True)
    recorder.record(parse_action({
        "action": "fill", "intent": "Type the member ID", "value": "{{inputs.member_id}}",
        "target": {"kind": "in_row", "role": "textbox", "row_label": "Member ID:"}}),
        risk=Risk.REVERSIBLE, route_before="/search", route_after="/search",
        landmark_seen=False, fallback_index=0)
    recorder.record(parse_action({
        "action": "click", "intent": "Run the search", "risk": "read_only",
        "target": {"kind": "named", "role": "button", "name": "Search"},
        "expect": {"role": "cell", "name": "Member Profile"}}),
        risk=Risk.READ_ONLY, route_before="/search", route_after="/member/12345",
        landmark_seen=True, fallback_index=0)
    recorder.record(parse_action({
        "action": "extract", "intent": "Read the savings balance", "output": "balance",
        "parse": "money", "sensitivity": "financial",
        "target": {"kind": "table_cell", "row": "Share Savings", "column": "Balance"}}),
        risk=Risk.READ_ONLY, route_before="/member/12345", route_after="/member/12345",
        landmark_seen=False)
    return recorder.build("Read a member's savings balance")


def test_flow_a_becomes_a_valid_draft_without_outcomes():
    capability = record_flow_a(make_recorder())
    assert capability.meta.status == Status.DRAFT
    assert capability.outcomes == {}
    assert [s.id for s in capability.steps] == [
        "open_the_member_inquiry_screen", "type_the_member_id",
        "run_the_search", "read_the_savings_balance"]
    assert capability.success.outputs_present == ["balance"]
    assert capability.outputs[0].sensitivity == Sensitivity.FINANCIAL


def test_member_id_is_parameterized_and_never_stored():
    capability = record_flow_a(make_recorder())
    fill, click = capability.steps[1], capability.steps[2]
    assert fill.action.value == "{{inputs.member_id}}"
    assert click.expect.url == "/member/{{inputs.member_id}}"
    assert "12345" not in capability.model_dump_json()


def test_fallbacks_are_added_except_for_table_cells():
    capability = record_flow_a(make_recorder())
    fill_locators = capability.steps[1].target.locators
    assert len(fill_locators) == 2 and isinstance(fill_locators[1], NthOfRoleLocator)
    assert len(capability.steps[3].target.locators) == 1  # never read a balance by position


def test_unverified_landmark_is_not_recorded():
    recorder = make_recorder()
    step = recorder.record(parse_action({
        "action": "click", "intent": "Run the search", "risk": "read_only",
        "target": {"kind": "named", "role": "button", "name": "Search"},
        "expect": {"role": "cell", "name": "Member Profile"}}),
        risk=Risk.READ_ONLY, route_before="/search", route_after="/search", landmark_seen=False)
    assert step.expect is None  # neither the route changed nor the landmark was seen


def test_literal_input_value_is_turned_into_a_placeholder():
    recorder = make_recorder()
    step = recorder.record(parse_action({
        "action": "fill", "intent": "Type the member ID", "value": "12345",
        "target": {"kind": "in_row", "role": "textbox", "row_label": "Member ID:"}}),
        risk=Risk.REVERSIBLE, route_before="/search", route_after="/search", landmark_seen=False)
    assert step.action.value == "{{inputs.member_id}}"


def test_repeated_intents_get_unique_step_ids():
    recorder = make_recorder()
    for _ in range(2):
        recorder.record(parse_action({
            "action": "click", "intent": "Continue", "risk": "reversible",
            "target": {"kind": "named", "role": "button", "name": "Continue"}}),
            risk=Risk.REVERSIBLE, route_before="/a", route_after="/a", landmark_seen=False)
    assert [s.id for s in recorder.steps] == ["continue", "continue_2"]


def test_success_without_outputs_uses_the_last_seen_landmark():
    recorder = make_recorder()
    recorder.record(parse_action({
        "action": "click", "intent": "Continue to review", "risk": "reversible",
        "target": {"kind": "named", "role": "button", "name": "Continue"},
        "expect": {"role": "cell", "name": "Review Account Request"}}),
        risk=Risk.REVERSIBLE, route_before="/a", route_after="/b", landmark_seen=True)
    capability = recorder.build("Reach the review screen")
    assert capability.success.visible[0].name == "Review Account Request"


def test_finish_is_not_recordable_and_empty_runs_cannot_build():
    recorder = make_recorder()
    with pytest.raises(RecordingError, match="not a recordable action"):
        recorder.record(parse_action({"action": "finish", "intent": "Done"}),
                        risk=Risk.READ_ONLY, route_before="/", route_after="/", landmark_seen=False)
    with pytest.raises(RecordingError, match="nothing was recorded"):
        recorder.build("Nothing")


def test_saved_artifact_loads_back_identically(tmp_path):
    capability = record_flow_a(make_recorder())
    path = tmp_path / "member.lookup_savings_balance@1.0.0.json"
    save(capability, path)
    assert load_capability(path) == capability


def test_slugify():
    assert slugify("Type the member ID") == "type_the_member_id"
    assert slugify("123 go!") == "step_123_go"