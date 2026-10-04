"""The new labelled_value locator, and capturing what an operator does without their data."""

import pytest

from cua.schema import Anchor, LabelledValueLocator, RoleInScopeLocator, RoleNameLocator, Target
from cua.surface import open_web_surface
from tests.test_live_surface import BASE_URL, PROFILE, app_is_running, sign_on

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not app_is_running(), reason=f"bank app not running at {BASE_URL}"),
]

MEMBER_ID_FIELD = Target(description="Member ID field", locators=[
    RoleInScopeLocator(role="textbox", scope=Anchor(role="row", name="Member ID:"))])
SEARCH = Target(description="Search button", locators=[RoleNameLocator(role="button", name="Search")])


def test_labelled_value_reads_the_cell_beside_its_label():
    with open_web_surface(BASE_URL, PROFILE.timing) as surface:
        sign_on(surface)
        surface.goto("/member/12345")
        name_cell = Target(description="Member name",
                           locators=[LabelledValueLocator(label="Name:")])
        text, _ = surface.read_text(name_cell)
        assert text == "Jordan Ellis"


def test_operator_actions_are_captured_without_the_values_typed():
    events = []
    with open_web_surface(BASE_URL, PROFILE.timing) as surface:
        sign_on(surface)
        surface.goto("/search")
        surface.start_operator_capture(events.append)
        surface.fill(MEMBER_ID_FIELD, "12345")  # standing in for a person's typing and clicking
        surface.click(SEARCH)
        surface.stop_operator_capture()

    typed = [e for e in events if e["type"] == "typed"]
    assert typed and typed[-1]["chars"] == 5 and "value" not in typed[-1]
    assert any(e["type"] == "click" and e["name"] == "Search" for e in events)
    assert any(e["type"] == "navigated" and e["route"] == "/member/12345" for e in events)
    assert all("at" in e for e in events if e["type"] != "navigated")