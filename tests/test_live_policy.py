"""Layer 2 in action: the browser itself refuses pages outside the allowlist."""

import pytest

from cua.policy import Policy
from cua.schema import RoleNameLocator, Target
from cua.surface import NavigationBlocked, open_web_surface
from tests.test_live_surface import BASE_URL, PROFILE, app_is_running, sign_on

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not app_is_running(), reason=f"bank app not running at {BASE_URL}"),
]

POLICY = Policy.load("policy.yaml")
SIGN_OFF_LINK = Target(description="Sign off link",
                       locators=[RoleNameLocator(role="link", name="Sign off")])


@pytest.fixture
def guarded_surface():
    with open_web_surface(BASE_URL, PROFILE.timing, request_guard=POLICY.allows_request) as s:
        yield s


def test_allowed_pages_still_work(guarded_surface):
    sign_on(guarded_surface)
    assert guarded_surface.current_route() == "/search"


def test_typing_a_forbidden_route_is_blocked(guarded_surface):
    sign_on(guarded_surface)
    with pytest.raises(NavigationBlocked, match="/logout"):
        guarded_surface.goto("/logout")


def test_clicking_a_link_to_a_forbidden_route_is_blocked(guarded_surface):
    sign_on(guarded_surface)
    with pytest.raises(NavigationBlocked, match="/logout"):
        guarded_surface.click(SIGN_OFF_LINK)