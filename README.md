# Computer-Use Automation System

A system that lets an AI agent operate a legacy back-office application that has no API.
An LLM-driven planner works out how to accomplish a goal **once**; the successful run is
recorded as a typed, versioned, reviewable **capability**; that capability is then
**replayed deterministically**, with no model in the loop, whenever an agent invokes it,
with explicit handling of business outcomes, recoverable conditions and hard failures,
live **human handoff** when a step needs a person, and **safety guardrails** throughout.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is
> how the AI agent invokes it in production.

The target is **Harbor Point Credit Union**, a deliberately legacy-style fake back office
included in this repo (server-rendered tables, no IDs or test IDs, unlabelled fields), with
switchable faults (popups, session expiry, slow pages, outages). Design and trade-offs are
in [REPORT.md](REPORT.md); recorded evidence is in [evidence/](evidence/README.md).

## What's here

| Piece | Where |
|---|---|
| Artifact schema (capabilities, app profiles), typed and validated | `cua/schema.py`, `schemas/*.schema.json` |
| Surface layer: seeing and acting on a screen (Playwright, accessibility tree) | `cua/surface/` |
| Deterministic replay engine and its result contract | `cua/replay/` |
| Discovery: planner interface, scripted mock, Claude planner, recorder | `cua/agent/` |
| Human handoff: control lease, operator interface, console operator | `cua/handoff/` |
| Safety policy (default-deny allowlist, risk rules) | `policy.yaml`, `cua/policy.py` |
| Redaction and redacted evidence logs | `cua/redaction.py`, `cua/evidence.py` |
| The target app (fake credit union) | `bank_app/` |
| Saved capabilities and the app profile | `artifacts/harbor-member-console/` |
| Evidence generator and recorded evidence | `cua/demo.py`, `evidence/` |

## Setup

Requires **Python 3.11+** (developed on 3.12) on macOS or Linux.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

`.env` holds local configuration. The values in `.env.example` work as-is for the demo:

| Variable | Used by | Notes |
|---|---|---|
| `BANK_USER`, `BANK_PASSWORD` | the bank app | Demo operator login for the fake app |
| `CUA_SECRET_OPERATOR_USER`, `CUA_SECRET_OPERATOR_PASSWORD` | the automation | Credentials the automation signs on with. Artifacts only ever *name* secrets; values come from here (the seam for a real vault) |
| `ANTHROPIC_API_KEY` | `--planner claude` only | **Optional.** Not needed for anything below |
| `CUA_MODEL` | `--planner claude` only | Optional model override |

## Running without live services

Everything runs locally with **no API key and no network access**: the bank app runs on
your machine, and discovery uses a **scripted mock planner** (`planner_scripts/*.json`)
behind the same interface as the Claude planner. The mock reads the screen before every
move and gives up if what it expects isn't there, so it exercises the same stuck-detection
and escalation paths a real model would.

The Claude-backed planner (`--planner claude`) is implemented and tested offline against a
fake client, but the recorded evidence uses the mock (see REPORT.md, *Cuts*).

## Demo path

Use two terminals, both with the venv active.

**Terminal 1: start the bank app**

```bash
flask --app bank_app.app run --port 5050
```

(Optional: open http://localhost:5050 and sign in with the demo login from `.env`.)

**Terminal 2: discover a capability from a goal**

```bash
python -m cua discover --app artifacts/harbor-member-console \
  --capability-id member.savings_balance_demo \
  --goal "Look up member {{inputs.member_id}} and read their savings balance" \
  --input member_id=12345 \
  --planner mock --script planner_scripts/lookup_balance.json --headed
```

This signs on, lets the planner reach the goal, records the run as a draft capability,
**replays it to prove it** (same inputs, no planner), and only then saves
`artifacts/harbor-member-console/member.savings_balance_demo@1.0.0.json`.
(`discover` never overwrites an existing artifact: pick a new id to run it again.)

**Replay the new capability for a different member:**

```bash
python -m cua replay artifacts/harbor-member-console/member.savings_balance_demo@1.0.0.json \
  --arg member_id=23456 --allow-draft
```

`--allow-draft` is needed because discovered artifacts are saved as `draft` until reviewed;
production replay refuses drafts.

**See error and outcome handling:**

```bash
# A legitimate business outcome (exit code 3)
python -m cua replay artifacts/harbor-member-console/member.lookup_balance@1.0.0.json \
  --arg member_id=99999 --allow-draft

# Invalid input, rejected before the browser opens (exit code 1)
python -m cua replay artifacts/harbor-member-console/member.lookup_balance@1.0.0.json \
  --arg member_id=abc --allow-draft
```

For runtime faults, stop the app in Terminal 1 (Ctrl+C), start it again with a fault switched
on, then replay again. Afterwards, restart it **without** `BANK_FAULTS` before doing anything else:

**Human handoff (you are the operator):**

```bash
python -m cua replay artifacts/harbor-member-console/account.open_sub_account@1.0.0.json \
  --arg member_id=23456 --arg deposit=100.00 --allow-draft --operator console
```

A visible browser fills in the form and pauses before the irreversible **Confirm**. The page
is shielded while you decide. In the terminal, type `a` (approve: automation confirms),
`t` (take control: click Confirm yourself in the browser, then type `done`), or `r` (reject).
After you hand back, automation re-checks the screen and never repeats the step.

**Regenerate all evidence from scratch** (each scenario starts its own fresh app):

```bash
python -m cua.demo
```

### Output and exit codes

Both commands print a JSON result on **stdout** (for a calling program) and a one-line
summary on **stderr** (for a person). Run logs are redacted and written to `runs/<run_id>/`.

| Exit code | Meaning |
|---|---|
| 0 | `success` (replay) or `saved` (discover) |
| 1 | `failed` |
| 2 | usage error (bad arguments, missing files, no API key for `--planner claude`) |
| 3 | `business_outcome` |
| 4 | `needs_human` |

### Bank app faults

Switched on with `BANK_FAULTS` (comma-separated) when starting the app. Each fault is
deterministic, so demos and tests are reproducible.

| Fault | Behavior | Replay's handling |
|---|---|---|
| `notice` | A system-notice overlay blocks the search page | recoverable: dismissed |
| `timeout` | The session expires the first time a member page opens | recoverable: re-authenticates and resumes |
| `slow` | Member pages take `BANK_SLOW_SECONDS` (default 4) to load | waits within budget; `timeout` beyond it |
| `outage` | "Core system unavailable" | hard failure: `app_error`, retryable |

## Tests

```bash
pytest -v                 # everything (about a minute)
pytest -m "not live" -v   # offline only: no browser, no app
```

Before a full run, restart the bank app in Terminal 1 **with no faults** (plain
`flask --app bank_app.app run --port 5050`), so its data is clean: several live tests read
from that shared app. Tests that need faults or create accounts start their own fresh
copies automatically.

## Using the real Claude planner

```bash
python -m cua discover ... --planner claude
```

Requires `ANTHROPIC_API_KEY` in `.env`. The planner is given one tool per action in a
restricted vocabulary, sees only redacted screens and input placeholders, and must state
the risk of every click.

## Known limitations

- The console operator's responsive wait uses `select` on the terminal: macOS and Linux only.
- The live Claude planner path has not been run against the real API in this submission.

See REPORT.md for design decisions, trade-offs, and what I'd build next.