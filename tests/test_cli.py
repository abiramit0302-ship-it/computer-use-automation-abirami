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

def test_discover_mock_without_script_is_a_usage_error(capsys):
    code = main(["discover", "--app", "artifacts/harbor-member-console",
                 "--capability-id", "member.x", "--goal", "Look up member {{inputs.member_id}}",
                 "--input", "member_id=12345", "--planner", "mock"])
    assert code == 2
    assert "--script" in capsys.readouterr().err


def test_discover_rejects_secret_inputs(capsys):
    code = main(["discover", "--app", "artifacts/harbor-member-console",
                 "--capability-id", "member.x", "--goal", "x",
                 "--input", "password:secret=hunter2", "--planner", "mock",
                 "--script", "planner_scripts/lookup_balance.json"])
    assert code == 2
    assert "hunter2" not in capsys.readouterr().err