"""Live human handoff on Flow B: approve, take control, and reject.

Each test runs against a FRESH bank app, because these tests really open accounts and we
count exactly how many were created, which is how "never double-submit" is proven.
"""

import re

import pytest

from cua.handoff.operator import HandBack, Review, ScriptedOperator
from cua.policy import Policy
from cua.replay import FailureCategory, Replayer, ReplayStatus
from cua.schema import RoleNameLocator, Target, load_capability
from cua.surface import open_web_surface
from tests.test_live_faults import bank_app
from tests.test_live_surface import PROFILE, sign_on

pytestmark = pytest.mark.live

FLOW_B = load_capability("artifacts/harbor-member-console/account.open_sub_account@1.0.0.json")
BASE_POLICY = Policy.load("policy.yaml")
CONFIRM = Target(description="Confirm button",
                 locators=[RoleNameLocator(role="button", name="Confirm")])
ARGS = {"member_id": "23456", "deposit": "100.00"}  # 23456 starts with ONE Share Savings account


def replay(base_url, tmp_path, operator):
    policy = Policy(BASE_POLICY.config.model_copy(update={"allowed_origins": [base_url]}))
    replayer = Replayer(PROFILE, policy, base_url, allow_draft=True, runs_dir=tmp_path,
                        operator=operator)
    return replayer.run(FLOW_B, ARGS)


def share_savings_accounts(base_url) -> int:
    with open_web_surface(base_url, PROFILE.timing) as surface:
        sign_on(surface)
        surface.goto("/member/23456")
        return surface.page.get_by_role("cell", name="Share Savings", exact=True).count()


def events(tmp_path, result) -> str:
    return (tmp_path / result.run_id / "events.jsonl").read_text()


def test_approve_lets_automation_perform_the_step(tmp_path):
    with bank_app() as url:
        result = replay(url, tmp_path, ScriptedOperator(Review.APPROVE, operator_id="abi"))
        accounts = share_savings_accounts(url)
    assert result.status == ReplayStatus.SUCCESS, result.failure or result.escalation
    assert re.fullmatch(r"HP\d{6}", result.outputs["confirmation_number"])
    assert result.handoffs == [
        "step 'confirm_new_account' approved by operator:abi; performed by automation"]
    assert accounts == 2  # the original one plus exactly one new one
    assert "awaiting_operator -> automation" in events(tmp_path, result)


def test_take_control_person_confirms_and_automation_does_not_repeat_it(tmp_path):
    operator = ScriptedOperator(Review.TAKE_CONTROL, operator_id="abi",
                                actions=lambda surface: surface.click(CONFIRM))
    with bank_app() as url:
        result = replay(url, tmp_path, operator)
        accounts = share_savings_accounts(url)
    assert result.status == ReplayStatus.SUCCESS, result.failure or result.escalation
    assert re.fullmatch(r"HP\d{6}", result.outputs["confirmation_number"])
    assert "completed by operator:abi" in result.handoffs[0]
    assert accounts == 2  # NOT 3: the click was not repeated after handback
    log = events(tmp_path, result)
    assert "operator_action" in log and "click button Confirm" in log
    assert "awaiting_operator -> operator" in log and "operator -> automation" in log


def test_reject_ends_the_run_and_commits_nothing(tmp_path):
    with bank_app() as url:
        result = replay(url, tmp_path, ScriptedOperator(Review.REJECT, operator_id="abi"))
        accounts = share_savings_accounts(url)
    assert result.status == ReplayStatus.FAILED
    assert result.failure.category == FailureCategory.OPERATOR_DECLINED
    assert result.failure.retryable is False
    assert accounts == 1  # unchanged


def test_abort_while_in_control_commits_nothing(tmp_path):
    operator = ScriptedOperator(Review.TAKE_CONTROL, operator_id="abi", hand_back=HandBack.ABORT)
    with bank_app() as url:
        result = replay(url, tmp_path, operator)
        accounts = share_savings_accounts(url)
    assert result.status == ReplayStatus.FAILED
    assert result.failure.category == FailureCategory.OPERATOR_DECLINED
    assert accounts == 1