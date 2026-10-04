"""The CLI prints a JSON result on stdout and signals the status through its exit code."""

import json

from cua.__main__ import main

ARTIFACT = "artifacts/harbor-member-console/member.lookup_balance@1.0.0.json"


def test_invalid_input_prints_json_failure_and_exits_1(capsys):
    code = main(["replay", ARTIFACT, "--allow-draft", "--arg", "member_id=abc"])
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["status"] == "failed"
    assert result["failure"]["category"] == "invalid_input"


def test_malformed_arg_is_a_usage_error(capsys):
    code = main(["replay", ARTIFACT, "--allow-draft", "--arg", "member_id"])
    assert code == 2
    assert "NAME=VALUE" in capsys.readouterr().err