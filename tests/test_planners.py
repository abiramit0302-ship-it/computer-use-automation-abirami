"""Both planners, tested offline: the scripted mock's screen checks, and the Claude
planner's prompt, parsing and retry, using a fake client instead of the real API."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cua.agent.actions import ClickOn, GiveUp, NavigateTo, parse_action
from cua.agent.claude import ClaudePlanner, render_request, tool_definitions
from cua.agent.planner import Feedback, PlannerError, PlannerRequest, Turn
from cua.agent.scripted import ScriptedPlanner

SCRIPT = Path("planner_scripts/lookup_balance.json")
SEARCH_CLICK = parse_action({"action": "click", "intent": "Run the search", "risk": "read_only",
                             "target": {"kind": "named", "role": "button", "name": "Search"}})


def request(screen: str, history=None) -> PlannerRequest:
    return PlannerRequest(goal="Look up member {{inputs.member_id}}", input_names=["member_id"],
                          screen=screen, route="/search", history=history or [], steps_remaining=10)


# --- The scripted mock -------------------------------------------------------------

def test_mock_follows_its_script_when_landmarks_are_visible():
    planner = ScriptedPlanner.from_file(SCRIPT)
    assert isinstance(planner.decide(request('- link "Sign off"')), NavigateTo)


def test_mock_gives_up_when_its_landmark_is_missing():
    planner = ScriptedPlanner.from_file(SCRIPT)
    action = planner.decide(request('- cell "Something unexpected"'))
    assert isinstance(action, GiveUp)
    assert "Sign off" in action.intent


def test_mock_gives_up_after_a_failed_action():
    planner = ScriptedPlanner.from_file(SCRIPT)
    failed = [Turn(SEARCH_CLICK, Feedback(False, "2 controls match"))]
    action = planner.decide(request('- link "Sign off"', history=failed))
    assert isinstance(action, GiveUp)
    assert "no recovery" in action.intent


@pytest.mark.parametrize("path", sorted(Path("planner_scripts").glob("*.json")), ids=lambda p: p.name)
def test_every_script_file_is_valid(path):
    ScriptedPlanner.from_file(path)  # every scripted action passes the vocabulary's validation


# --- The Claude planner, with a fake client ------------------------------------------

class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.responses.pop(0))


def tool_call(name, arguments, call_id="call_1"):
    return [SimpleNamespace(type="tool_use", id=call_id, name=name, input=arguments)]


def claude_with(responses):
    messages = FakeMessages(responses)
    return ClaudePlanner(model="test-model", client=SimpleNamespace(messages=messages)), messages


def test_one_tool_per_action_and_action_field_is_implied():
    tools = tool_definitions()
    assert [t["name"] for t in tools] == [
        "navigate", "fill", "select", "click", "extract", "finish", "give_up"]
    for tool in tools:
        assert "action" not in tool["input_schema"]["properties"]


def test_tool_call_becomes_a_validated_action():
    planner, messages = claude_with([tool_call("click", {
        "intent": "Run the search", "risk": "read_only",
        "target": {"kind": "named", "role": "button", "name": "Search"}})])
    action = planner.decide(request('- button "Search"'))
    assert isinstance(action, ClickOn)
    assert messages.calls[0]["tool_choice"] == {"type": "any"}


def test_invalid_answer_gets_one_corrective_retry():
    planner, messages = claude_with([
        tool_call("click", {"intent": "Run the search",
                            "target": {"kind": "named", "role": "button", "name": "Search"}}),  # no risk
        tool_call("click", {"intent": "Run the search", "risk": "read_only",
                            "target": {"kind": "named", "role": "button", "name": "Search"}},
                  call_id="call_2"),
    ])
    assert isinstance(planner.decide(request('- button "Search"')), ClickOn)
    retry = messages.calls[1]["messages"][-1]["content"][0]
    assert retry["is_error"] is True and "risk" in retry["content"]


def test_two_invalid_answers_stop_the_run():
    bad = {"intent": "x", "target": {"kind": "named", "role": "button", "name": "Search"}}
    planner, _ = claude_with([tool_call("click", bad), tool_call("click", bad, call_id="call_2")])
    with pytest.raises(PlannerError, match="two invalid"):
        planner.decide(request('- button "Search"'))


def test_answer_without_a_tool_call_is_an_error():
    planner, _ = claude_with([[SimpleNamespace(type="text", text="I think you should click Search")]])
    with pytest.raises(PlannerError, match="did not choose"):
        planner.decide(request('- button "Search"'))


def test_prompt_contains_placeholders_and_screen_but_no_values():
    prompt = render_request(request('- cell "[MONEY]"'))
    assert "{{inputs.member_id}}" in prompt
    assert '- cell "[MONEY]"' in prompt


def test_missing_api_key_fails_fast_with_a_helpful_message(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(PlannerError, match="scripted mock"):
        ClaudePlanner()