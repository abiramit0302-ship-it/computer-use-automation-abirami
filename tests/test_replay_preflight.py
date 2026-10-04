"""Preflight problems are caught before the application is ever touched."""

from cua.policy import Policy
from cua.replay import FailureCategory, Replayer, ReplayStatus, parse_money
from cua.schema import load_app_profile, load_capability
from cua.secrets import SecretStore

PROFILE = load_app_profile("artifacts/harbor-member-console/app_profile.json")
CAPABILITY = load_capability("artifacts/harbor-member-console/member.lookup_balance@1.0.0.json")
POLICY = Policy.load("policy.yaml")
GOOD_SECRETS = SecretStore({"CUA_SECRET_OPERATOR_USER": "u", "CUA_SECRET_OPERATOR_PASSWORD": "p"})


def make(tmp_path, base_url="http://localhost:5050", **options):
    options.setdefault("allow_draft", True)
    options.setdefault("secrets", GOOD_SECRETS)
    return Replayer(PROFILE, POLICY, base_url, runs_dir=tmp_path, **options)


def failure_of(result):
    assert result.status == ReplayStatus.FAILED
    return result.failure


def test_badly_formatted_input_is_rejected_without_echoing_it(tmp_path):
    failure = failure_of(make(tmp_path).run(CAPABILITY, {"member_id": "abc"}))
    assert failure.category == FailureCategory.INVALID_INPUT
    assert failure.retryable is False
    assert "abc" not in failure.message


def test_unknown_input_is_rejected(tmp_path):
    failure = failure_of(make(tmp_path).run(CAPABILITY, {"member_id": "12345", "colour": "blue"}))
    assert failure.category == FailureCategory.INVALID_INPUT


def test_missing_required_input_is_rejected(tmp_path):
    failure = failure_of(make(tmp_path).run(CAPABILITY, {}))
    assert "missing required input 'member_id'" in failure.message


def test_draft_is_refused_unless_allowed(tmp_path):
    failure = failure_of(make(tmp_path, allow_draft=False).run(CAPABILITY, {"member_id": "12345"}))
    assert failure.category == FailureCategory.ARTIFACT_NOT_APPROVED


def test_origin_outside_allowlist_is_refused(tmp_path):
    failure = failure_of(make(tmp_path, base_url="https://evil.example").run(
        CAPABILITY, {"member_id": "12345"}))
    assert failure.category == FailureCategory.POLICY_DENIED


def test_missing_secret_is_a_configuration_error(tmp_path):
    failure = failure_of(make(tmp_path, secrets=SecretStore({})).run(
        CAPABILITY, {"member_id": "12345"}))
    assert failure.category == FailureCategory.CONFIGURATION_ERROR
    assert "CUA_SECRET_" in failure.message


def test_parse_money():
    assert parse_money("$2,450.75") == "2450.75"
    assert parse_money("$18,920.00") == "18920.00"
    assert parse_money("n/a") is None