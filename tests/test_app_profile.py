"""The real app profile file must load, and the profile rules must hold."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.schema import AppProfile, load_app_profile

PROFILE_PATH = Path("artifacts/harbor-member-console/app_profile.json")


def raw_profile():
    return json.loads(PROFILE_PATH.read_text())


def test_profile_file_is_valid():
    profile = load_app_profile(PROFILE_PATH)
    assert profile.id == "harbor-member-console"
    assert set(profile.interstitials) == {"system_notice", "session_expired", "core_unavailable"}


def test_rejects_undeclared_secret():
    data = raw_profile()
    data["secrets"].remove("operator_password")
    with pytest.raises(ValidationError, match="undeclared secret 'operator_password'"):
        AppProfile.model_validate(data)


def test_rejects_input_placeholder_in_sign_on():
    data = raw_profile()
    data["sign_on"]["steps"][0]["action"]["value"] = "{{inputs.member_id}}"
    with pytest.raises(ValidationError, match="only secrets"):
        AppProfile.model_validate(data)


def test_rejects_hard_failure_with_recovery():
    data = raw_profile()
    data["interstitials"]["core_unavailable"]["recovery"] = {"type": "reauthenticate"}
    with pytest.raises(ValidationError, match="must not declare a recovery"):
        AppProfile.model_validate(data)


def test_rejects_recoverable_without_recovery():
    data = copy.deepcopy(raw_profile())
    del data["interstitials"]["system_notice"]["recovery"]
    with pytest.raises(ValidationError, match="needs a recovery"):
        AppProfile.model_validate(data)