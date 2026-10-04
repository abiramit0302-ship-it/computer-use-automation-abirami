"""The schema must accept a well-formed capability and reject broken ones loudly."""

import copy

import pytest
from pydantic import ValidationError

from cua.schema import Capability

VALID = {
    "schema_version": "1.0",
    "meta": {
        "id": "member.lookup_balance",
        "version": "1.0.0",
        "status": "draft",
        "summary": "Look up a member and return the balance of one of their accounts.",
        "app_profile": "harbor-member-console",
    },
    "inputs": [
        {"name": "member_id", "type": "string", "description": "5-digit member number",
         "sensitivity": "identifier", "pattern": r"^\d{5}$"},
        {"name": "account_type", "type": "string", "description": "Account type to read",
         "sensitivity": "public", "required": False, "default": "Share Savings"},
    ],
    "outputs": [
        {"name": "balance", "type": "money", "description": "Current balance",
         "sensitivity": "financial"},
    ],
    "steps": [
        {"id": "open_search", "intent": "Open the member inquiry screen",
         "action": {"type": "navigate", "route": "/search"}, "risk": "read_only",
         "expect": {"visible": [{"role": "cell", "name": "Member Inquiry"}]}},
        {"id": "enter_member_id", "intent": "Type the member ID",
         "action": {"type": "fill", "value": "{{inputs.member_id}}"}, "risk": "reversible",
         "target": {"description": "Member ID text field",
                    "locators": [
                        {"strategy": "role_in_scope", "role": "textbox",
                         "scope": {"role": "row", "name": "Member ID:"}},
                        {"strategy": "nth_of_role", "role": "textbox", "index": 0},
                    ],
                    "rationale": "Field has no accessible name; anchored to its label's row."}},
        {"id": "submit_search", "intent": "Run the search",
         "action": {"type": "click"}, "risk": "read_only",
         "target": {"description": "Search button",
                    "locators": [{"strategy": "role_name", "role": "button", "name": "Search"}]},
         "expect": {"url": "/member/{{inputs.member_id}}",
                    "visible": [{"role": "cell", "name": "Member Profile"}]},
         "outcomes": ["member_not_found", "invalid_member_id", "member_restricted"]},
        {"id": "read_balance", "intent": "Read the balance for the requested account type",
         "action": {"type": "extract", "into": "balance", "parse": "money"}, "risk": "read_only",
         "target": {"description": "Balance cell for the requested account type",
                    "locators": [{"strategy": "table_cell",
                                  "row_where": "{{inputs.account_type}}", "column": "Balance"}]}},
    ],
    "outcomes": {
        "member_not_found": {"code": "MEMBER_NOT_FOUND", "description": "No member has this ID",
                             "detect": {"text": "No member found with ID"}},
        "invalid_member_id": {"code": "INVALID_MEMBER_ID", "description": "The ID was rejected",
                              "detect": {"text": "Invalid Member ID"}},
        "member_restricted": {"code": "MEMBER_RESTRICTED", "description": "Needs supervisor access",
                              "detect": {"text": "supervisor access required"}},
    },
    "success": {"outputs_present": ["balance"]},
    "provenance": {"discovered_by": "hand-written test fixture", "discovery_run_id": "test",
                   "recorded_at": "2026-10-03T12:00:00Z",
                   "goal": "Look up member {{inputs.member_id}} and read their savings balance"},
}


def broken(change):
    """Return a deep copy of VALID with one deliberate mistake applied."""
    data = copy.deepcopy(VALID)
    change(data)
    return data


def test_valid_capability_loads():
    capability = Capability.model_validate(VALID)
    assert capability.meta.id == "member.lookup_balance"
    assert [s.id for s in capability.steps] == [
        "open_search", "enter_member_id", "submit_search", "read_balance"]


def test_round_trip_through_json_is_lossless():
    capability = Capability.model_validate(VALID)
    assert Capability.model_validate_json(capability.model_dump_json()) == capability


def test_rejects_unknown_field():
    data = broken(lambda d: d["steps"][1]["target"].update(locaters=[]))
    with pytest.raises(ValidationError, match="locaters"):
        Capability.model_validate(data)


def test_rejects_undeclared_input():
    data = broken(lambda d: d["steps"][1]["action"].update(value="{{inputs.memberid}}"))
    with pytest.raises(ValidationError, match="undeclared input 'memberid'"):
        Capability.model_validate(data)


def test_rejects_secret_placeholder_in_capability():
    data = broken(lambda d: d["steps"][1]["action"].update(value="{{secrets.operator_password}}"))
    with pytest.raises(ValidationError, match="only use inputs"):
        Capability.model_validate(data)


def test_rejects_secret_as_caller_input():
    data = broken(lambda d: d["inputs"][0].update(sensitivity="secret"))
    with pytest.raises(ValidationError, match="never caller inputs"):
        Capability.model_validate(data)


def test_rejects_click_without_target():
    data = broken(lambda d: d["steps"][2].pop("target"))
    with pytest.raises(ValidationError, match="needs a target"):
        Capability.model_validate(data)


def test_rejects_step_without_risk():
    data = broken(lambda d: d["steps"][0].pop("risk"))
    with pytest.raises(ValidationError, match="risk"):
        Capability.model_validate(data)


def test_rejects_output_never_extracted():
    data = broken(lambda d: d["steps"].pop())
    with pytest.raises(ValidationError, match="no step extracts it"):
        Capability.model_validate(data)


def test_rejects_undeclared_outcome():
    data = broken(lambda d: d["steps"][2]["outcomes"].append("member_on_holiday"))
    with pytest.raises(ValidationError, match="undeclared outcome"):
        Capability.model_validate(data)