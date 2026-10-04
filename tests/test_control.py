"""The control lease enforces a single holder and only legal handovers."""

import pytest

from cua.handoff.control import ControlError, ControlLease, ControlState
from cua.handoff.operator import HandBack, HandoffRequest, Review, ScriptedOperator

REQUEST = HandoffRequest(run_id="replay-test", capability="account.open_sub_account@1.0.0",
                         step_id="confirm", intent="Confirm the new account",
                         reason="irreversible step requires approval", route="/review")


def test_automation_holds_control_at_the_start():
    lease = ControlLease()
    assert lease.holder == "automation"
    lease.require_automation()  # does not raise


def test_approve_path_returns_control_to_automation():
    lease = ControlLease()
    lease.request_operator("approval needed")
    lease.return_to_automation(by="operator:abi", reason="approved")
    assert lease.state == ControlState.AUTOMATION
    assert [c.to_state for c in lease.history] == [
        ControlState.AWAITING_OPERATOR, ControlState.AUTOMATION]


def test_automation_cannot_act_while_a_person_holds_the_session():
    lease = ControlLease()
    lease.request_operator("approval needed")
    with pytest.raises(ControlError, match="nobody"):
        lease.require_automation()          # waiting: nobody may act
    lease.grant_to("abi")
    assert lease.holder == "operator:abi"
    with pytest.raises(ControlError, match="operator:abi"):
        lease.require_automation()          # the person is working
    lease.return_to_automation(by="operator:abi", reason="handed back")
    lease.require_automation()              # automation may act again


def test_nothing_can_happen_after_the_run_ends():
    lease = ControlLease()
    lease.request_operator("approval needed")
    lease.end(by="operator:abi", reason="rejected")
    with pytest.raises(ControlError):
        lease.require_automation()
    with pytest.raises(ControlError, match="illegal handover"):
        lease.grant_to("abi")


def test_operator_cannot_take_control_unless_automation_asked():
    with pytest.raises(ControlError, match="illegal handover"):
        ControlLease().grant_to("abi")


def test_every_change_is_reported_with_who_and_why():
    changes = []
    lease = ControlLease(on_change=changes.append)
    lease.request_operator("approval needed")
    lease.grant_to("abi")
    assert [(c.by, c.reason) for c in changes] == [
        ("automation", "approval needed"), ("operator:abi", "operator took control")]


def test_scripted_operator_decides_and_acts():
    acted = []
    operator = ScriptedOperator(Review.TAKE_CONTROL, actions=lambda surface: acted.append(surface))
    assert operator.review(REQUEST) == Review.TAKE_CONTROL
    assert operator.operate(REQUEST, surface="fake-surface") == HandBack.RESUME
    assert acted == ["fake-surface"] and operator.requests == [REQUEST]