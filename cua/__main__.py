"""Command-line entry point:  python -m cua <command> ...

  python -m cua replay <capability.json> --arg member_id=12345 [--allow-draft] [--headed]

Prints the ReplayResult as JSON on stdout (the answer, for a calling program) and a
one-line summary on stderr (for a person). Exit codes:
  0 success   1 failed   2 usage error   3 business_outcome   4 needs_human
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from cua.policy import Policy
from cua.replay import Replayer, ReplayStatus
from cua.schema import load_app_profile, load_capability

EXIT_CODES = {
    ReplayStatus.SUCCESS: 0,
    ReplayStatus.FAILED: 1,
    ReplayStatus.BUSINESS_OUTCOME: 3,
    ReplayStatus.NEEDS_HUMAN: 4,
}
USAGE_ERROR = 2


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cua",
        description="Computer-use automation: discover, record and replay capabilities.")
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser("replay", help="replay a saved capability deterministically (no LLM)")
    replay.add_argument("capability",
                        help="path to a capability artifact, e.g. "
                             "artifacts/harbor-member-console/member.lookup_balance@1.0.0.json")
    replay.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE",
                        help="an input for the capability; repeat for several")
    replay.add_argument("--base-url", default=os.environ.get("BANK_BASE_URL", "http://localhost:5050"),
                        help="the tenant's application URL (default: $BANK_BASE_URL or localhost:5050)")
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
    except (ValueError, OSError) as exc:  # bad --arg, missing or invalid files
        print(f"error: {exc}", file=sys.stderr)
        return USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())