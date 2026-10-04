"""Happy-path replay against the running bank app: no LLM anywhere."""

import pytest

from cua.policy import Policy
from cua.replay import Replayer, ReplayStatus
from cua.schema import load_capability
from tests.test_live_surface import BASE_URL, PROFILE, app_is_running

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not app_is_running(), reason=f"bank app not running at {BASE_URL}"),
]

CAPABILITY = load_capability("artifacts/harbor-member-console/member.lookup_balance@1.0.0.json")


def make(tmp_path):
    return Replayer(PROFILE, Policy.load("policy.yaml"), BASE_URL,
                    allow_draft=True, runs_dir=tmp_path)


@pytest.mark.parametrize("args, expected", [
    ({"member_id": "12345"}, "2450.75"),
    ({"member_id": "23456"}, "18920.00"),           # Checking is listed first for this member
    ({"member_id": "12345", "account_type": "Checking"}, "812.30"),
])
def test_replay_returns_the_right_balance(tmp_path, args, expected):
    result = make(tmp_path).run(CAPABILITY, args)
    assert result.status == ReplayStatus.SUCCESS, result.failure
    assert result.outputs == {"balance": expected}
    assert result.warnings == []


def test_evidence_contains_no_sensitive_values(tmp_path):
    result = make(tmp_path).run(CAPABILITY, {"member_id": "12345"})
    run_dir = tmp_path / result.run_id
    written = (run_dir / "events.jsonl").read_text() + (run_dir / "summary.json").read_text()
    for raw in ["2450.75", "2,450.75", "12345", "harbor-demo-2026"]:
        assert raw not in written, f"{raw!r} leaked into the evidence"