"""Secrets and regulated data never survive into anything we write down."""

import json
from decimal import Decimal

from cua.evidence import RunLog
from cua.redaction import Redactor
from cua.schema import Sensitivity

LABELS = ["Name:", "Date of Birth:", "SSN (last 4):"]

# What the member page's accessibility tree looks like (shortened)
MEMBER_PAGE = '''- table:
  - rowgroup:
    - row "Member ID: 12345 Date of Birth: 1984-03-14":
      - cell "Member ID:"
      - cell "12345"
      - cell "Date of Birth:"
      - cell "1984-03-14"
    - row "Name: Jordan Ellis SSN (last 4): ***-**-4821":
      - cell "Name:"
      - cell "Jordan Ellis"
      - cell "SSN (last 4):"
      - cell "***-**-4821"
    - row "Share Savings 12345-S00 $2,450.75":
      - cell "Share Savings"
      - cell "12345-S00"
      - cell "$2,450.75"'''


def make_redactor() -> Redactor:
    redactor = Redactor(LABELS)
    redactor.register("member_id", "12345", Sensitivity.IDENTIFIER)
    redactor.register("operator_password", "harbor-demo-2026", Sensitivity.SECRET)
    return redactor


def test_secret_value_never_survives():
    out = make_redactor().redact("typed harbor-demo-2026 into the password field")
    assert "harbor-demo-2026" not in out
    assert "[SECRET:operator_password]" in out


def test_identifier_is_masked_but_distinguishable():
    assert make_redactor().redact("/member/12345") == "/member/[member_id:***45]"


def test_public_values_are_kept():
    redactor = make_redactor()
    redactor.register("account_type", "Share Savings", Sensitivity.PUBLIC)
    assert "Share Savings" in redactor.redact("reading the Share Savings row")


def test_patterns_catch_unregistered_sensitive_shapes():
    text = ("ssn 123-45-6789, card 4111 1111 1111 1111, dob 1984-03-14, "
            "balance $2,450.75, email jordan.ellis@example.com")
    out = Redactor().redact(text)
    for leaked in ["123-45-6789", "4111", "1984-03-14", "2,450.75", "jordan.ellis"]:
        assert leaked not in out


def test_timestamps_and_run_ids_survive():
    redactor = Redactor()
    for text in ["2026-10-04T13:35:48.123+00:00", "replay-20261004T133548Z-a1b2c3"]:
        assert redactor.redact(text) == text


def test_member_page_snapshot_keeps_structure_but_not_data():
    out = make_redactor().redact(MEMBER_PAGE)
    for leaked in ["Jordan Ellis", "1984-03-14", "4821", "2,450.75", "12345"]:
        assert leaked not in out, f"{leaked!r} leaked"
    # The structure the LLM needs to navigate is still there.
    assert 'cell "Share Savings"' in out
    assert 'cell "Name:"' in out


def test_nested_data_and_non_strings_are_redacted():
    redactor = make_redactor()
    redactor.register("balance", "2450.75", Sensitivity.FINANCIAL)
    data = {"outputs": {"balance": Decimal("2450.75")},
            "attempts": [{"value": "harbor-demo-2026"}],
            "count": 3}
    out = redactor.redact_data(data)
    assert out["outputs"]["balance"] == "[FINANCIAL:balance]"
    assert out["attempts"][0]["value"] == "[SECRET:operator_password]"
    assert out["count"] == 3


def test_run_log_only_ever_writes_redacted_data(tmp_path):
    with RunLog("test", make_redactor(), root=tmp_path) as log:
        log.event("step", "filled the password", step_id="enter_password",
                  value="harbor-demo-2026")
        log.write_summary({"status": "success", "member": "12345"})
        run_dir = log.dir

    written = (run_dir / "events.jsonl").read_text() + (run_dir / "summary.json").read_text()
    assert "harbor-demo-2026" not in written
    assert "12345" not in written

    first = json.loads((run_dir / "events.jsonl").read_text().splitlines()[0])
    assert first["seq"] == 1
    assert first["step_id"] == "enter_password"