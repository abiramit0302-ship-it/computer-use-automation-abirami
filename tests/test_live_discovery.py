"""The discovery loop, driven by a fixed list of planner actions against the live app."""

from pathlib import Path
from cua.agent.workflow import discover_and_save
from cua.schema import load_capability

import pytest

from cua.agent.actions import parse_action
from cua.agent.discovery import Discoverer, DiscoveryStatus
from cua.agent.planner import Planner
from cua.policy import Policy
from cua.replay import Replayer, ReplayStatus
from cua.schema import InputParam, Sensitivity, Timing, ValueType
from tests.test_live_faults import bank_app
from tests.test_live_surface import BASE_URL, PROFILE, app_is_running

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not app_is_running(), reason=f"bank app not running at {BASE_URL}"),
]

POLICY = Policy.load("policy.yaml")
GOAL = "Look up member {{inputs.member_id}} and read their savings balance"
INPUTS = [InputParam(name="member_id", type=ValueType.STRING,
                     description="5-digit member number", sensitivity=Sensitivity.IDENTIFIER)]

FLOW_A = [
    {"action": "navigate", "intent": "Open the member inquiry screen", "route": "/search",
     "expect": {"role": "cell", "name": "Member Inquiry"}},
    {"action": "fill", "intent": "Type the member ID", "value": "{{inputs.member_id}}",
     "target": {"kind": "in_row", "role": "textbox", "row_label": "Member ID:"}},
    {"action": "click", "intent": "Run the search", "risk": "read_only",
     "target": {"kind": "named", "role": "button", "name": "Search"},
     "expect": {"role": "cell", "name": "Member Profile"}},
    {"action": "extract", "intent": "Read the savings balance", "output": "balance",
     "parse": "money", "sensitivity": "financial",
     "target": {"kind": "table_cell", "row": "Share Savings", "column": "Balance"}},
    {"action": "finish", "intent": "Read the member's savings balance"},
]


class FakePlanner(Planner):
    """Returns a fixed list of actions, then gives up. Records what it was shown."""
    name = "fake-test-planner"

    def __init__(self, actions: list[dict]):
        self.actions = [parse_action(a) for a in actions]
        self.requests = []

    def decide(self, request):
        self.requests.append(request)
        if self.actions:
            return self.actions.pop(0)
        return parse_action({"action": "give_up", "intent": "script exhausted"})


def discover(planner, tmp_path, *, base_url=BASE_URL, policy=POLICY, profile=PROFILE):
    return Discoverer(profile, policy, base_url, planner, runs_dir=tmp_path).run(
        goal=GOAL, capability_id="member.lookup_savings_balance",
        inputs=INPUTS, input_values={"member_id": "12345"})


def test_discovered_capability_replays_with_a_different_member(tmp_path):
    planner = FakePlanner(FLOW_A)
    result = discover(planner, tmp_path)
    assert result.status == DiscoveryStatus.RECORDED, result.intervention or result.failure
    capability = result.capability
    assert len(capability.steps) == 4
    assert "12345" not in capability.model_dump_json()

    # The planner never saw the real member ID anywhere.
    for request in planner.requests:
        seen = request.screen + request.route + " ".join(t.feedback.message for t in request.history)
        assert "12345" not in seen

    # Recorded with 12345, replayed with 23456: the artifact really is parameterized.
    replay = Replayer(PROFILE, POLICY, BASE_URL, allow_draft=True, runs_dir=tmp_path)
    result = replay.run(capability, {"member_id": "23456"})
    assert result.status == ReplayStatus.SUCCESS, result.failure
    assert result.outputs == {"balance": "18920.00"}


def test_system_notice_is_handled_and_never_recorded(tmp_path):
    with bank_app("notice") as url:
        policy = Policy(POLICY.config.model_copy(update={"allowed_origins": [url]}))
        result = discover(FakePlanner(FLOW_A), tmp_path, base_url=url, policy=policy)
    assert result.status == DiscoveryStatus.RECORDED, result.intervention or result.failure
    assert len(result.capability.steps) == 4
    assert "Acknowledge" not in result.capability.model_dump_json()


def test_giving_up_requests_a_human_with_evidence(tmp_path):
    result = discover(FakePlanner([{"action": "give_up", "intent": "I can't find the search"}]),
                      tmp_path)
    assert result.status == DiscoveryStatus.NEEDS_HUMAN
    assert "gave up" in result.intervention.reason
    assert Path(result.intervention.screenshot).exists()


def test_repeated_failures_mean_the_planner_is_stuck(tmp_path):
    quick = PROFILE.model_copy(update={"timing": Timing(action_timeout_ms=1000)})
    # A harmless-sounding control that doesn't exist: the policy allows the click,
    # the click itself fails, and three failures in a row mean "stuck".
    ghost_click = {"action": "click", "intent": "Print the statement", "risk": "read_only",
                   "target": {"kind": "named", "role": "button", "name": "Print Statement"}}
    result = discover(FakePlanner([ghost_click] * 3), tmp_path, profile=quick)
    assert result.status == DiscoveryStatus.NEEDS_HUMAN
    assert "stuck" in result.intervention.reason

def test_a_dangerous_sounding_click_is_stopped_before_it_is_even_tried(tmp_path):
    delete_click = {"action": "click", "intent": "Delete the member", "risk": "reversible",
                    "target": {"kind": "named", "role": "button", "name": "Delete Member"}}
    result = discover(FakePlanner([delete_click]), tmp_path)
    assert result.status == DiscoveryStatus.NEEDS_HUMAN
    assert "approval required" in result.intervention.reason
    assert "delete_the_member" in result.intervention.reason  # a meaningful step id, not 'pending'


def test_irreversible_click_stops_for_approval_even_if_mislabelled(tmp_path):
    flow_b = [
        {"action": "navigate", "intent": "Open the sub-account form",
         "route": "/member/{{inputs.member_id}}/open-account"},
        {"action": "select", "intent": "Choose the account type", "value": "Share Savings",
         "target": {"kind": "in_row", "role": "combobox", "row_label": "Account Type:"}},
        {"action": "fill", "intent": "Enter the opening deposit", "value": "100.00",
         "target": {"kind": "in_row", "role": "textbox", "row_label": "Initial Deposit ($):"}},
        {"action": "click", "intent": "Continue to review", "risk": "reversible",
         "target": {"kind": "named", "role": "button", "name": "Continue"},
         "expect": {"role": "cell", "name": "Initial Deposit:"}},
        {"action": "click", "intent": "Confirm the new account", "risk": "reversible",
         "target": {"kind": "named", "role": "button", "name": "Confirm"}},
    ]
    result = discover(FakePlanner(flow_b), tmp_path)
    assert result.status == DiscoveryStatus.NEEDS_HUMAN
    assert "approval required" in result.intervention.reason
    assert result.intervention.last_action == "Confirm the new account"

def test_scripted_mock_discovers_flow_a_from_its_script_file(tmp_path):
    from cua.agent.scripted import ScriptedPlanner
    planner = ScriptedPlanner.from_file("planner_scripts/lookup_balance.json")
    result = discover(planner, tmp_path)
    assert result.status == DiscoveryStatus.RECORDED, result.intervention or result.failure
    assert result.capability.provenance.discovered_by == "scripted-mock:lookup-balance"
    assert len(result.capability.steps) == 4

def test_discover_verify_save_writes_only_a_proven_artifact(tmp_path):
    from cua.agent.scripted import ScriptedPlanner
    planner = ScriptedPlanner.from_file("planner_scripts/lookup_balance.json")
    report = discover_and_save(
        discoverer=Discoverer(PROFILE, POLICY, BASE_URL, planner, runs_dir=tmp_path),
        replayer=Replayer(PROFILE, POLICY, BASE_URL, allow_draft=True, runs_dir=tmp_path),
        goal=GOAL, capability_id="member.savings_balance", inputs=INPUTS,
        input_values={"member_id": "12345"}, out_dir=tmp_path)
    assert report.status == "saved", report.message
    assert report.verification.outputs == report.discovery.observed_outputs == {"balance": "2450.75"}
    saved = load_capability(report.artifact)
    assert saved.meta.status.value == "draft"


def test_existing_artifact_is_never_overwritten(tmp_path):
    (tmp_path / "member.savings_balance@1.0.0.json").write_text("{}")
    report = discover_and_save(
        discoverer=Discoverer(PROFILE, POLICY, BASE_URL, FakePlanner([]), runs_dir=tmp_path),
        replayer=Replayer(PROFILE, POLICY, BASE_URL, allow_draft=True, runs_dir=tmp_path),
        goal=GOAL, capability_id="member.savings_balance", inputs=INPUTS,
        input_values={"member_id": "12345"}, out_dir=tmp_path)
    assert report.status == "failed"
    assert "already exists" in report.message
    assert report.discovery is None  # refused before discovery even started