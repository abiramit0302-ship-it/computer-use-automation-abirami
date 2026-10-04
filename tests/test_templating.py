"""Placeholders are filled safely, and secrets come only from the configured store."""

import pytest

from cua.schema import Step
from cua.secrets import MissingSecret, SecretStore
from cua.templating import TemplateError, render, render_model

FILL_STEP = {
    "id": "enter_member_id",
    "intent": "Type the member ID",
    "action": {"type": "fill", "value": "{{inputs.member_id}}"},
    "risk": "reversible",
    "target": {"description": "Member ID field",
               "locators": [{"strategy": "role_name", "role": "textbox", "name": "Member ID"}]},
}


def test_render_fills_placeholders():
    result = render("/member/{{inputs.member_id}}", {"inputs": {"member_id": "12345"}})
    assert result == "/member/12345"


def test_render_fails_loudly_on_missing_value():
    with pytest.raises(TemplateError, match="inputs.member_id"):
        render("{{inputs.member_id}}", {"inputs": {}})


def test_render_model_escapes_tricky_values_and_keeps_original():
    step = Step.model_validate(FILL_STEP)
    tricky = 'quote " and backslash \\'
    filled = render_model(step, {"inputs": {"member_id": tricky}})
    assert filled.action.value == tricky                      # value arrived intact
    assert step.action.value == "{{inputs.member_id}}"        # original still has the placeholder


def test_secret_store_reads_prefixed_variable():
    store = SecretStore({"CUA_SECRET_OPERATOR_PASSWORD": "pw"})
    assert store.get("operator_password") == "pw"


def test_missing_secret_names_the_variable():
    with pytest.raises(MissingSecret, match="CUA_SECRET_OPERATOR_USER"):
        SecretStore({}).get("operator_user")