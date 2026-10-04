"""A minimal but real operator UI: a person at the terminal, working in the visible browser.

The handoff request is printed to the terminal; the person answers approve / take control /
reject, and if they take control, works directly in the SAME browser window the automation
was using, then types 'done' to hand back (or 'abort').

Everything goes to STDERR so stdout stays clean JSON for a calling program. If no answer
can be read (no terminal attached), the SAFE choice wins: review -> reject, control -> abort.
A production operator console (web UI, work queue, streamed browser) would be another
implementation of the same Operator interface.
"""

import sys
from collections.abc import Callable
import select

from cua.handoff.operator import HandBack, HandoffRequest, Operator, Review
from cua.surface import Surface


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _ask(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:  # end of input: nobody is there to answer
        raise EOFError
    return line

def _ask_while_idle(prompt: str, surface: Surface) -> str:
    """Wait for the operator's answer WITHOUT freezing the browser: check the keyboard
    every 0.1s, and in between let the session process its pending work (request-guard
    decisions, recorded clicks, page loads). Works on macOS and Linux terminals."""
    print(prompt, end="", file=sys.stderr, flush=True)
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            line = sys.stdin.readline()
            if not line:
                raise EOFError
            return line
        surface.idle(0.1)


REVIEW_ANSWERS = {
    "a": Review.APPROVE, "approve": Review.APPROVE,
    "t": Review.TAKE_CONTROL, "take": Review.TAKE_CONTROL,
    "r": Review.REJECT, "reject": Review.REJECT,
}
HANDBACK_ANSWERS = {"done": HandBack.RESUME, "abort": HandBack.ABORT}


class ConsoleOperator(Operator):
    def __init__(self, operator_id: str, *, ask: Callable[[str], str] | None = None,
                 say: Callable[[str], None] = _say):
        self.id = operator_id
        self._say = say
        if ask is None:  # a real person at a real terminal
            self._ask = _ask
            self._ask_in_control = _ask_while_idle
        else:            # tests supply answers directly
            self._ask = ask
            self._ask_in_control = lambda prompt, surface: ask(prompt)

    def review(self, request: HandoffRequest, surface: Surface) -> Review:
        self._say("")
        self._say("=" * 64)
        self._say("  HUMAN INPUT NEEDED: automation has paused")
        self._say("=" * 64)
        self._say(f"  Capability : {request.capability}")
        self._say(f"  Step       : {request.step_id}")
        self._say(f"  Intent     : {request.intent}")
        self._say(f"  Why paused : {request.reason}")
        self._say(f"  Screen     : {request.route}  (also visible in the browser window)")
        if request.screenshot:
            self._say(f"  Screenshot : {request.screenshot}")
        self._say("")
        self._say("  Nobody controls the browser right now: it is shielded and will not load pages.")
        self._say("  a = approve      automation performs this step")
        self._say("  t = take control you act in the browser window yourself")
        self._say("  r = reject       stop the run; nothing is committed")
        while True:
            try:
                answer = self._ask_in_control("  Your decision [a/t/r]: ", surface).strip().lower()
            except EOFError:
                self._say("  (no answer available: rejecting, the safe choice)")
                return Review.REJECT
            if answer in REVIEW_ANSWERS:
                return REVIEW_ANSWERS[answer]
            self._say("  Please type a, t or r.")

    def operate(self, request: HandoffRequest, surface: Surface) -> HandBack:
        self._say("")
        self._say("  You now control the browser window. Automation is paused and will not act.")
        self._say("  Your clicks and page changes are recorded; values you type are NOT.")
        self._say(f"  Complete '{request.intent}' yourself, then come back here.")
        while True:
            try:
                answer = self._ask_in_control(
                    "  Type 'done' to hand control back, or 'abort' to stop: ", surface)
            except EOFError:
                self._say("  (no answer available: aborting, the safe choice)")
                return HandBack.ABORT
            choice = answer.strip().lower()
            if choice in HANDBACK_ANSWERS:
                self._say("  Control handed back. Automation will re-check the screen "
                          "before doing anything." if choice == "done" else "  Run stopped.")
                return HANDBACK_ANSWERS[choice]
            self._say("  Please type 'done' or 'abort'.")