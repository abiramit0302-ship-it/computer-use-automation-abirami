"""The console operator: plain-word prompts on stderr, and safe defaults with nobody there."""

import io
from types import SimpleNamespace

from cua.handoff.console import ConsoleOperator
from cua.handoff.operator import HandBack, HandoffRequest, Review

REQUEST = HandoffRequest(run_id="replay-test", capability="account.open_sub_account@1.0.0",
                         step_id="confirm_new_account", intent="Confirm the new account",
                         reason="irreversible step requires approval",
                         route="/member/[member_id:***56]/open-account/review",
                         screenshot="runs/replay-test/screenshots/005-handoff.png")


def operator_with(answers):
    said = []
    remaining = iter(answers)

    def ask(prompt):
        said.append(prompt)
        try:
            return next(remaining) + "\n"
        except StopIteration:
            raise EOFError from None

    return ConsoleOperator("abi", ask=ask, say=said.append), said


def test_review_shows_the_context_and_reads_the_decision():
    operator, said = operator_with(["t"])
    assert operator.review(REQUEST, surface=None) == Review.TAKE_CONTROL
    shown = "\n".join(said)
    for detail in ["confirm_new_account", "Confirm the new account", "requires approval",
                   "005-handoff.png"]:
        assert detail in shown


def test_unclear_answers_are_asked_again():
    operator, said = operator_with(["maybe", "A"])
    assert operator.review(REQUEST, surface=None) == Review.APPROVE
    assert any("Please type a, t or r" in line for line in said)


def test_nobody_at_the_terminal_means_reject():
    operator, _ = operator_with([])
    assert operator.review(REQUEST, surface=None) == Review.REJECT


def test_hand_back_and_abort():
    assert operator_with(["done"])[0].operate(REQUEST, surface=None) == HandBack.RESUME
    assert operator_with(["abort"])[0].operate(REQUEST, surface=None) == HandBack.ABORT


def test_nobody_at_the_terminal_while_in_control_means_abort():
    assert operator_with([])[0].operate(REQUEST, surface=None) == HandBack.ABORT


def test_default_output_never_touches_stdout(capsys):
    operator = ConsoleOperator("abi", ask=lambda prompt: "r\n")  # default say -> stderr
    operator.review(REQUEST, surface=None)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HUMAN INPUT NEEDED" in captured.err


def test_waiting_for_the_operator_keeps_the_browser_responsive(monkeypatch):
    """Regression: the browser froze while the console waited for 'done' (found by hand)."""
    import cua.handoff.console as console
    monkeypatch.setattr(console.sys, "stdin", io.StringIO("done\n"))
    keyboard_ready = iter([False, False, True])  # the person types on the third check
    monkeypatch.setattr(console.select, "select",
                        lambda r, w, x, timeout: (r if next(keyboard_ready) else [], [], []))
    idled = []
    surface = SimpleNamespace(idle=idled.append)

    operator = ConsoleOperator("abi", say=lambda message: None)  # real waiting logic
    assert operator.operate(REQUEST, surface) == HandBack.RESUME
    assert idled == [0.1, 0.1]  # the session was serviced while waiting, not frozen