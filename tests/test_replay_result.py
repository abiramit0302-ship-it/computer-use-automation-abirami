"""The result contract makes inconsistent results impossible."""

import pytest
from pydantic import ValidationError

from cua.replay.result import (
    EscalationInfo, FailureCategory, FailureInfo, OutcomeInfo, ReplayResult, ReplayStatus,
)

BASE = {"run_id": "replay-test", "capability": "member.lookup_balance@1.0.0", "duration_ms": 1200}


def test_success_with_outputs():
    result = ReplayResult(**BASE, status=ReplayStatus.SUCCESS, outputs={"balance": "2450.75"})
    assert result.outputs["balance"] == "2450.75"


def test_business_outcome_needs_outcome_details():
    with pytest.raises(ValidationError, match="needs 'outcome'"):
        ReplayResult(**BASE, status=ReplayStatus.BUSINESS_OUTCOME)


def test_success_must_not_carry_a_failure():
    failure = FailureInfo.build(FailureCategory.TIMEOUT, "page too slow")
    with pytest.raises(ValidationError, match="must not have 'failure'"):
        ReplayResult(**BASE, status=ReplayStatus.SUCCESS, failure=failure)


def test_only_success_carries_outputs():
    outcome = OutcomeInfo(id="member_not_found", code="MEMBER_NOT_FOUND",
                          description="No member", step_id="submit_search")
    with pytest.raises(ValidationError, match="only a 'success' result"):
        ReplayResult(**BASE, status=ReplayStatus.BUSINESS_OUTCOME,
                     outcome=outcome, outputs={"balance": "0.00"})


def test_retryable_follows_category():
    assert FailureInfo.build(FailureCategory.APP_ERROR, "outage").retryable is True
    assert FailureInfo.build(FailureCategory.INVALID_INPUT, "bad id").retryable is False
    assert FailureInfo.build(FailureCategory.TARGET_NOT_FOUND, "no button").retryable is False


def test_needs_human_round_trips_through_json():
    result = ReplayResult(**BASE, status=ReplayStatus.NEEDS_HUMAN,
                          escalation=EscalationInfo(step_id="confirm", intent="Confirm the account",
                                                    reason="irreversible step requires approval"))
    assert ReplayResult.model_validate_json(result.model_dump_json()) == result