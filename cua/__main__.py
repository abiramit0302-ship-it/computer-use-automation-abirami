"""Command-line entry point:  python -m cua <command> ...

  python -m cua discover --app artifacts/harbor-member-console --capability-id member.savings_balance \\
      --goal "Look up member {{inputs.member_id}} and read their savings balance" \\
      --input member_id=12345 --planner mock --script planner_scripts/lookup_balance.json

  python -m cua replay <capability.json> --arg member_id=12345 [--allow-draft] [--headed]

Each prints a JSON result on stdout (for a calling program) and a one-line summary on
stderr (for a person). Exit codes:
  0 success / saved   1 failed   2 usage error   3 business_outcome   4 needs_human
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from cua.agent.discovery import Discoverer
from cua.agent.planner import PlannerError
from cua.agent.workflow import discover_and_save
from cua.policy import Policy
from cua.replay import Replayer, ReplayStatus
from cua.schema import InputParam, Sensitivity, ValueType, load_app_profile, load_capability

EXIT_CODES = {
    ReplayStatus.SUCCESS: 0,
    ReplayStatus.FAILED: 1,
    ReplayStatus.BUSINESS_OUTCOME: 3,
    ReplayStatus.NEEDS_HUMAN: 4,
}
DISCOVER_EXIT_CODES = {"saved": 0, "failed": 1, "needs_human": 4}
USAGE_ERROR = 2
DEFAULT_BASE_URL = "http://localhost:5050"


def parse_pairs(pairs: list[str]) -> dict[str, str]:
    """['member_id=12345'] -> {'member_id': '12345'}"""
    args: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            # Don't echo the argument: it may contain a sensitive value.
            raise ValueError("--arg expects NAME=VALUE (an argument was missing '=')")
        args[name] = value
    return args


def parse_inputs(specs: list[str]) -> tuple[list[InputParam], dict[str, str]]:
    """['member_id=12345', 'account_type:public=Checking'] -> declarations and values.

    Format: NAME[:SENSITIVITY]=VALUE. Sensitivity defaults to 'identifier'.
    """
    params: list[InputParam] = []
    values: dict[str, str] = {}
    for spec in specs:
        head, separator, value = spec.partition("=")
        if not separator or not head:
            raise ValueError("--input expects NAME[:SENSITIVITY]=VALUE")
        name, _, sensitivity = head.partition(":")
        params.append(InputParam(
            name=name, type=ValueType.STRING,
            description=f"'{name}', supplied when the capability was discovered",
            sensitivity=Sensitivity(sensitivity or "identifier")))
        values[name] = value
    return params, values


def cmd_replay(options: argparse.Namespace) -> int:
    capability_path = Path(options.capability)
    capability = load_capability(capability_path)
    profile = load_app_profile(capability_path.parent / "app_profile.json")
    replayer = Replayer(profile, Policy.load(options.policy), options.base_url,
                        allow_draft=options.allow_draft, headless=not options.headed)

    result = replayer.run(capability, parse_pairs(options.arg))

    print(result.model_dump_json(indent=2))  # stdout: the answer
    print(f"[{result.status.value}] {result.capability} - evidence in runs/{result.run_id}/",
          file=sys.stderr)                    # stderr: for the person watching
    return EXIT_CODES[result.status]


def cmd_discover(options: argparse.Namespace) -> int:
    app_dir = Path(options.app)
    profile = load_app_profile(app_dir / "app_profile.json")
    policy = Policy.load(options.policy)
    inputs, values = parse_inputs(options.input)

    if options.planner == "mock":
        if not options.script:
            raise ValueError("--planner mock needs --script (e.g. planner_scripts/lookup_balance.json)")
        from cua.agent.scripted import ScriptedPlanner
        planner = ScriptedPlanner.from_file(options.script)
    else:
        from cua.agent.claude import ClaudePlanner
        planner = ClaudePlanner()

    report = discover_and_save(
        discoverer=Discoverer(profile, policy, options.base_url, planner,
                              headless=not options.headed),
        replayer=Replayer(profile, policy, options.base_url, allow_draft=True),
        goal=options.goal, capability_id=options.capability_id,
        inputs=inputs, input_values=values, out_dir=app_dir)

    # The full capability is on disk; keep stdout readable by leaving it out here.
    print(report.model_dump_json(indent=2, exclude={"discovery": {"capability"}}))
    runs = [r.run_id for r in (report.discovery, report.verification) if r is not None]
    print(f"[{report.status}] {report.message} - evidence in "
          + ", ".join(f"runs/{run}/" for run in runs), file=sys.stderr)
    return DISCOVER_EXIT_CODES[report.status]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cua",
        description="Computer-use automation: discover, record and replay capabilities.")
    commands = parser.add_subparsers(dest="command", required=True)
    base_url = os.environ.get("BANK_BASE_URL", DEFAULT_BASE_URL)

    discover = commands.add_parser(
        "discover", help="let a planner accomplish a goal, then record, verify and save it")
    discover.add_argument("--app", required=True,
                          help="app folder holding app_profile.json, e.g. artifacts/harbor-member-console")
    discover.add_argument("--capability-id", required=True, help="e.g. member.savings_balance")
    discover.add_argument("--goal", required=True,
                          help='goal with placeholders, e.g. "Look up member {{inputs.member_id}} ..."')
    discover.add_argument("--input", action="append", default=[], metavar="NAME[:SENSITIVITY]=VALUE",
                          help="an input used during discovery; sensitivity defaults to identifier")
    discover.add_argument("--planner", choices=["mock", "claude"], default="mock",
                          help="mock = scripted stand-in (no API key); claude = real model")
    discover.add_argument("--script", help="script file for --planner mock")
    discover.add_argument("--base-url", default=base_url, help="the tenant's application URL")
    discover.add_argument("--policy", default="policy.yaml", help="safety policy file")
    discover.add_argument("--headed", action="store_true", help="show the browser window")
    discover.set_defaults(handler=cmd_discover)

    replay = commands.add_parser("replay", help="replay a saved capability deterministically (no LLM)")
    replay.add_argument("capability",
                        help="path to a capability artifact, e.g. "
                             "artifacts/harbor-member-console/member.lookup_balance@1.0.0.json")
    replay.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE",
                        help="an input for the capability; repeat for several")
    replay.add_argument("--base-url", default=base_url, help="the tenant's application URL")
    replay.add_argument("--policy", default="policy.yaml", help="safety policy file")
    replay.add_argument("--allow-draft", action="store_true",
                        help="allow running a draft capability (development only)")
    replay.add_argument("--headed", action="store_true", help="show the browser window")
    replay.set_defaults(handler=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # before building the parser, so defaults can come from .env
    options = build_parser().parse_args(argv)
    try:
        return options.handler(options)
    except (ValueError, OSError, PlannerError) as exc:  # bad arguments/files, no API key
        print(f"error: {exc}", file=sys.stderr)
        return USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())