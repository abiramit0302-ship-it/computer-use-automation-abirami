"""Every saved artifact must load, match its file name, and point at an existing app profile."""

from pathlib import Path

import pytest

from cua.schema import load_app_profile, load_capability

ARTIFACTS = Path("artifacts")
CAPABILITY_FILES = sorted(p for p in ARTIFACTS.glob("*/*.json") if p.name != "app_profile.json")


def test_there_is_at_least_one_capability():
    assert CAPABILITY_FILES, "no capability artifacts found"


@pytest.mark.parametrize("path", CAPABILITY_FILES, ids=lambda p: p.name)
def test_capability_is_valid_and_consistent(path):
    capability = load_capability(path)
    assert path.name == f"{capability.meta.id}@{capability.meta.version}.json", \
        "file name must be <id>@<version>.json"
    profile = load_app_profile(path.parent / "app_profile.json")
    assert capability.meta.app_profile == profile.id