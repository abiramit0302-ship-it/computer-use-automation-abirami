"""Regenerate /evidence from a clean start:  python -m cua.demo

Every scenario starts its OWN fresh bank app (so data and faults are exactly as described),
runs through the real code paths, and leaves its redacted run logs in evidence/<scenario>/.
A final self-check scans everything written for raw sensitive values and fails loudly if
any are found. The evidence index is written to evidence/README.md.

Folders not created by this script (e.g. a recorded human console session) are left alone.
"""

import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from bank_app.launcher import running_bank_app
from cua.agent.discovery import Discoverer
from cua.agent.scripted import ScriptedPlanner
from cua.agent.workflow import discover_and_save
from cua.handoff.operator import Review, ScriptedOperator
from cua.policy import Policy
from cua.replay import ReplayResult, Replayer
from cua.schema import InputParam, Sensitivity, ValueType, load_app_profile, load_capability

EVIDENCE = Path("evidence")
APP = Path("artifacts/harbor-member-console")
GOAL = "Look up member {{inputs.member_id}} and read their savings balance"
INPUTS = [InputParam(name="member_id", type=ValueType.STRING,
                     description="5-digit member number", sensitivity=Sensitivity.IDENTIFIER)]

# Raw values that must NEVER appear in any evidence file.
NEVER_IN_EVIDENCE = ["12345", "23456", "99999", "harbor-demo-2026", "op-4471",
                     "2450.75", "2,450.75", "18920", "Jordan Ellis", "Morgan Reyes"]


def policy_for(url: str) -> Policy:
    """The real policy, plus this fresh app's port. policy.yaml itself is untouched."""
    base = Policy.load("policy.yaml")
    return Policy(base.config.model_copy(update={"allowed_origins": [url]}))


def describe(result: ReplayResult) -> str:
    """A one-line, non-sensitive summary for the index (no outputs, no values)."""
    parts = [result.status.value]
    if result.outcome:
        parts.append(f"outcome {result.outcome.code}")
    if result.failure:
        parts.append(f"{result.failure.category.value}, retryable={result.failure.retryable}")
    if result.escalation:
        parts.append("escalated to a human")
    parts += result.recoveries + result.handoffs
    return "; ".join(parts)


def replay(out: Path, capability_file: str, args: dict, *, faults: str = "", operator=None) -> str:
    capability = load_capability(APP / capability_file)
    profile = load_app_profile(APP / "app_profile.json")
    with running_bank_app(faults) as url:
        replayer = Replayer(profile, policy_for(url), url, allow_draft=True,
                            runs_dir=out, operator=operator)
        return describe(replayer.run(capability, args))


def discovery(out: Path) -> str:
    profile = load_app_profile(APP / "app_profile.json")
    with running_bank_app() as url:
        policy = policy_for(url)
        report = discover_and_save(
            discoverer=Discoverer(profile, policy, url,
                                  ScriptedPlanner.from_file("planner_scripts/lookup_balance.json"),
                                  runs_dir=out),
            replayer=Replayer(profile, policy, url, allow_draft=True, runs_dir=out),
            goal=GOAL, capability_id="member.savings_balance",
            inputs=INPUTS, input_values={"member_id": "12345"}, out_dir=out)
    return f"{report.status}: {report.message}"


def press_confirm_bypassing_the_shield(surface) -> None:
    surface.page.get_by_role("button", name="Confirm").dispatch_event("click")


def confirm_as_operator(surface) -> None:
    surface.page.get_by_role("button", name="Confirm").click()


FLOW_A = "member.lookup_balance@1.0.0.json"
FLOW_B = "account.open_sub_account@1.0.0.json"
FLOW_B_ARGS = {"member_id": "23456", "deposit": "100.00"}

SCENARIOS = [
    ("01-discovery",
     "Mock planner discovers Flow A from a goal; the recording is verified by replay, then saved",
     discovery),
    ("02-replay-success",
    "Discovered artifact replayed for a DIFFERENT member than it was recorded with",
     lambda out: replay(out, "member.savings_balance@1.0.0.json", {"member_id": "23456"})),
    ("03-replay-business-outcome",
     "Unknown member: a legitimate answer, not a crash",
     lambda out: replay(out, FLOW_A, {"member_id": "99999"})),
    ("04-replay-invalid-input",
     "Badly formatted member ID: rejected before the browser opens, value never echoed",
     lambda out: replay(out, FLOW_A, {"member_id": "abc"})),
    ("05-replay-session-timeout",
     "Session expires mid-run: re-authenticates and resumes WITHOUT repeating the search",
     lambda out: replay(out, FLOW_A, {"member_id": "12345"}, faults="timeout")),
    ("06-replay-core-outage",
     "Core system down: hard failure, retryable, with a masked screenshot",
     lambda out: replay(out, FLOW_A, {"member_id": "12345"}, faults="outage")),
    ("07-handoff-take-control",
     "Flow B: operator takes control of the live session, confirms, hands back; not repeated",
     lambda out: replay(out, FLOW_B, FLOW_B_ARGS, operator=ScriptedOperator(
         Review.TAKE_CONTROL, operator_id="demo-operator", actions=confirm_as_operator))),
    ("08-handoff-early-click-blocked",
     "Flow B: a click during review (nobody in control) is blocked; operator rejects; nothing committed",
     lambda out: replay(out, FLOW_B, FLOW_B_ARGS, operator=ScriptedOperator(
         Review.REJECT, operator_id="demo-operator",
         review_actions=press_confirm_bypassing_the_shield))),
]


def leaks_in(root: Path) -> list[str]:
    """Every raw sensitive value found in any text file under root."""
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md", ".txt"}:
            text = path.read_text(errors="ignore")
            found += [f"{path}: {value!r}" for value in NEVER_IN_EVIDENCE if value in text]
    return found


def write_index(rows: list[tuple[str, str, str]], leaks: list[str]) -> None:
    lines = [
        "# Evidence",
        "",
        "Generated by `python -m cua.demo`. Every scenario ran against its own fresh copy of",
        "the bank app; each folder holds the redacted run log(s) (`events.jsonl`), the result",
        "(`summary.json`) and any masked screenshots.",
        "",
        "| Folder | What it shows | Result |",
        "|---|---|---|",
        *[f"| `{folder}` | {what} | {result} |" for folder, what, result in rows],
        "",
        "## Redaction self-check",
        "",
        ("Every text file in this folder was scanned for raw member IDs, names, balances and "
         "credentials: **none found.**") if not leaks else
        f"**Raw values were found in {len(leaks)} place(s); see the terminal output of "
        "`python -m cua.demo`. The values are deliberately not repeated here.**",
        "",
    ]
    (EVIDENCE / "README.md").write_text("\n".join(lines))


def main() -> int:
    load_dotenv()
    EVIDENCE.mkdir(exist_ok=True)
    rows = []
    for folder, what, run in SCENARIOS:
        out = EVIDENCE / folder
        shutil.rmtree(out, ignore_errors=True)  # only this script's own folders are replaced
        out.mkdir(parents=True)
        print(f"running {folder} ...", file=sys.stderr)
        rows.append((folder, what, run(out)))
        print(f"  -> {rows[-1][2]}", file=sys.stderr)

    scenario_folders = {folder for folder, _, _ in SCENARIOS}
    for extra in sorted(p.name for p in EVIDENCE.iterdir() if p.is_dir()):
        if extra not in scenario_folders:
            rows.append((extra, "Recorded separately by hand (not regenerated by this script): "
                                "a real person using `--operator console`", "kept as recorded"))
            
    write_index(rows, [])          # write first, so the scan below covers the index itself
    leaks = leaks_in(EVIDENCE)
    write_index(rows, leaks)       # then record the scan's result in the index
    if leaks:
        print("REDACTION CHECK FAILED:\n" + "\n".join(leaks), file=sys.stderr)
        return 1
    print("redaction check passed; index written to evidence/README.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())