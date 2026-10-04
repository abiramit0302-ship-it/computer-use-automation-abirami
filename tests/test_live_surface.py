"""Live checks: do our locators really match the running bank app?

Requires the app running WITHOUT faults:
    flask --app bank_app.app run --port 5050
These tests skip themselves automatically when the app isn't reachable.
"""

import os
import urllib.request

import pytest
from dotenv import load_dotenv

from cua.schema import Anchor, RoleInScopeLocator, RoleNameLocator, TableCellLocator, Target, load_app_profile
from cua.secrets import SecretStore
from cua.surface import TargetNotFound, open_web_surface
from cua.templating import render_model

load_dotenv()
BASE_URL = os.environ.get("BANK_BASE_URL", "http://localhost:5050")
PROFILE = load_app_profile("artifacts/harbor-member-console/app_profile.json")


def app_is_running() -> bool:
    try:
        urllib.request.urlopen(BASE_URL + "/login", timeout=2)
        return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not app_is_running(), reason=f"bank app not running at {BASE_URL}"),
]

MEMBER_ID_FIELD = Target(
    description="Member ID text field",
    locators=[RoleInScopeLocator(role="textbox", scope=Anchor(role="row", name="Member ID:"))],
)
SEARCH_BUTTON = Target(
    description="Search button",
    locators=[RoleNameLocator(role="button", name="Search")],
)


def balance_cell(account_type: str) -> Target:
    return Target(
        description=f"{account_type} balance",
        locators=[TableCellLocator(row_where=account_type, column="Balance")],
    )


@pytest.fixture
def surface():
    with open_web_surface(BASE_URL, PROFILE.timing) as s:
        yield s


def sign_on(surface):
    """Run the app profile's sign-on steps, returning how each control was found."""
    secrets = SecretStore().resolve(PROFILE.secrets)
    surface.goto(PROFILE.sign_on.route)
    resolutions = []
    for step in PROFILE.sign_on.steps:
        filled = render_model(step, {"secrets": secrets})
        if filled.action.type == "fill":
            resolutions.append(surface.fill(filled.target, filled.action.value))
        elif filled.action.type == "click":
            resolutions.append(surface.click(filled.target))
    return resolutions


def search(surface, member_id: str):
    surface.goto("/search")
    surface.fill(MEMBER_ID_FIELD, member_id)
    surface.click(SEARCH_BUTTON)


def test_sign_on_works_with_primary_locators(surface):
    resolutions = sign_on(surface)
    assert [r.used_fallback for r in resolutions] == [False, False, False]
    assert surface.control_visible(PROFILE.sign_on.success.visible[0])


def test_reads_balance_by_meaning_not_position(surface):
    sign_on(surface)
    search(surface, "12345")
    text, resolution = surface.read_text(balance_cell("Share Savings"))
    assert text == "$2,450.75"
    assert not resolution.used_fallback

    search(surface, "23456")  # Checking is listed FIRST for this member
    text, _ = surface.read_text(balance_cell("Share Savings"))
    assert text == "$18,920.00"


def test_observe_describes_the_screen(surface):
    sign_on(surface)
    surface.goto("/search")
    observation = surface.observe()
    assert observation.route == "/search"
    assert 'button "Search"' in observation.accessibility_tree


def test_missing_control_fails_clearly(surface):
    sign_on(surface)
    surface.goto("/search")
    ghost = Target(description="Delete Member button",
                   locators=[RoleNameLocator(role="button", name="Delete Member")])
    with pytest.raises(TargetNotFound, match="Delete Member"):
        surface.click(ghost)