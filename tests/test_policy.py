"""The policy allows what's listed, denies everything else, and only ever raises risk."""

import pytest
import yaml
from pydantic import ValidationError

from cua.policy import Policy, PolicyConfig, Verdict, route_matches
from cua.schema import Step

POLICY = Policy.load("policy.yaml")


def click_step(button_name: str, risk: str) -> Step:
    return Step.model_validate({
        "id": "press_button", "intent": f"Press {button_name}",
        "action": {"type": "click"}, "risk": risk,
        "target": {"description": f"{button_name} button",
                   "locators": [{"strategy": "role_name", "role": "button", "name": button_name}]},
    })


def test_star_matches_exactly_one_segment():
    assert route_matches("/member/*", "/member/12345")
    assert not route_matches("/member/*", "/member/12345/open-account")


def test_parent_directory_trick_is_rejected():
    assert not route_matches("/member/*/open-account", "/member/../open-account")


def test_logout_is_not_allowlisted():
    assert POLICY.check_route("/logout").verdict == Verdict.DENY


def test_unknown_origin_is_denied():
    assert POLICY.check_origin("https://evil.example/login").verdict == Verdict.DENY


def test_read_only_click_on_allowed_page_is_allowed():
    assert POLICY.check_step(click_step("Search", "read_only"), "/search").allowed


def test_irreversible_step_requires_approval():
    decision = POLICY.check_step(click_step("Confirm", "irreversible"),
                                 "/member/12345/open-account/review")
    assert decision.verdict == Verdict.REQUIRE_APPROVAL


def test_mislabelled_confirm_is_raised_to_irreversible():
    decision = POLICY.check_step(click_step("Confirm", "read_only"),
                                 "/member/12345/open-account/review")
    assert decision.verdict == Verdict.REQUIRE_APPROVAL
    assert "raised from read_only" in decision.reason


def test_action_on_page_outside_allowlist_is_denied():
    decision = POLICY.check_step(click_step("Search", "read_only"), "/admin/users")
    assert decision.verdict == Verdict.DENY


def test_requests_are_checked_by_origin_and_route():
    assert POLICY.allows_request("http://localhost:5050/search", True)
    assert not POLICY.allows_request("http://localhost:5050/logout", True)
    assert POLICY.allows_request("http://localhost:5050/favicon.ico", False)  # not a page
    assert not POLICY.allows_request("https://evil.example/tracker.js", False)


def test_policy_must_cover_every_risk_level():
    data = yaml.safe_load(open("policy.yaml"))
    del data["risk_rules"]["irreversible"]
    with pytest.raises(ValidationError, match="missing"):
        PolicyConfig.model_validate(data)