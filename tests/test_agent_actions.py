"""The planner vocabulary turns sensible requests into robust locators and rejects the rest."""

import pytest
from pydantic import ValidationError

from cua.agent.actions import action_json_schema, parse_action, to_target
from cua.schema import RoleInScopeLocator, RoleNameLocator, TableCellLocator


def test_named_click_becomes_role_name_locator():
    action = parse_action({"action": "click", "intent": "Run the search", "risk": "read_only",
                           "target": {"kind": "named", "role": "button", "name": "Search"}})
    target = to_target(action.target)
    assert isinstance(target.locators[0], RoleNameLocator)
    assert target.description == "button 'Search'"


def test_field_in_row_becomes_scoped_locator():
    action = parse_action({"action": "fill", "intent": "Type the member ID",
                           "value": "{{inputs.member_id}}",
                           "target": {"kind": "in_row", "role": "textbox", "row_label": "Member ID:"}})
    locator = to_target(action.target).locators[0]
    assert isinstance(locator, RoleInScopeLocator)
    assert locator.scope.name == "Member ID:"


def test_table_cell_becomes_table_locator():
    action = parse_action({"action": "extract", "intent": "Read the balance", "output": "balance",
                           "parse": "money", "sensitivity": "financial",
                           "target": {"kind": "table_cell", "row": "Share Savings", "column": "Balance"}})
    assert isinstance(to_target(action.target).locators[0], TableCellLocator)


def test_position_based_targets_are_not_in_the_vocabulary():
    with pytest.raises(ValidationError):
        parse_action({"action": "click", "intent": "Click", "risk": "read_only",
                      "target": {"kind": "nth", "role": "button", "index": 0}})


def test_planner_cannot_type_a_secret():
    with pytest.raises(ValidationError, match="only inputs"):
        parse_action({"action": "fill", "intent": "Type password",
                      "value": "{{secrets.operator_password}}",
                      "target": {"kind": "in_row", "role": "textbox", "row_label": "Password:"}})


def test_click_must_state_its_risk():
    with pytest.raises(ValidationError, match="risk"):
        parse_action({"action": "click", "intent": "Click",
                      "target": {"kind": "named", "role": "button", "name": "Search"}})


def test_outputs_can_never_be_secrets():
    with pytest.raises(ValidationError, match="never be secrets"):
        parse_action({"action": "extract", "intent": "Read", "output": "token",
                      "sensitivity": "secret",
                      "target": {"kind": "table_cell", "row": "Token", "column": "Value"}})


def test_unknown_actions_are_rejected():
    with pytest.raises(ValidationError):
        parse_action({"action": "run_shell", "intent": "rm -rf /"})


def test_json_schema_lists_every_action_for_the_llm():
    schema_text = str(action_json_schema())
    for name in ["navigate", "fill", "select", "click", "extract", "finish", "give_up"]:
        assert f"'{name}'" in schema_text