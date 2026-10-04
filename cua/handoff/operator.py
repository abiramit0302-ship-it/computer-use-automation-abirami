"""The operator boundary: how a person is brought into a paused run.

When replay cannot or must not continue on its own (an irreversible step needs approval,
or a step fails in a way automation can't fix), it pauses and hands a HandoffRequest to
an Operator. The operator:

  review()   decides: APPROVE (automation performs the step), TAKE_CONTROL (the person
             acts in the SAME live session), or REJECT (end the run, nothing committed).
             NOBODY holds control during review: the page is shielded and page loads are
             blocked, so nothing done in the browser at this point can commit anything;
  operate()  only after TAKE_CONTROL: blocks while the person works, then hands back
             (RESUME) or gives up (ABORT).

Implementations: ConsoleOperator (a person at the terminal, using the visible browser
window) and ScriptedOperator (acts programmatically, for tests and recorded evidence).
A production web console with a work queue would be a third implementation.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum

from cua.schema import StrictModel
from cua.surface import Surface


class Review(StrEnum):
    APPROVE = "approve"
    TAKE_CONTROL = "take_control"
    REJECT = "reject"


class HandBack(StrEnum):
    RESUME = "resume"
    ABORT = "abort"


class HandoffRequest(StrictModel):
    """Everything an operator needs to decide, without re-running anything."""
    run_id: str
    capability: str
    step_id: str
    intent: str
    reason: str
    route: str
    screenshot: str | None = None


class Operator(ABC):
    id: str = "operator"

    @abstractmethod
    def review(self, request: HandoffRequest, surface: Surface) -> Review:
        """Decide what should happen at the paused step. The surface is shielded meanwhile."""

    @abstractmethod
    def operate(self, request: HandoffRequest, surface: Surface) -> HandBack:
        """Called only after TAKE_CONTROL. Blocks while the person works in the live session."""


class ScriptedOperator(Operator):
    """Acts like a person would, programmatically. For tests and recorded evidence."""

    def __init__(self, review: Review, *, actions: Callable[[Surface], None] | None = None,
                 review_actions: Callable[[Surface], None] | None = None,
                 hand_back: HandBack = HandBack.RESUME, operator_id: str = "scripted-operator"):
        self.id = operator_id
        self._review = review
        self._actions = actions
        self._review_actions = review_actions  # e.g. a person clicking BEFORE taking control
        self._hand_back = hand_back
        self.requests: list[HandoffRequest] = []

    def review(self, request: HandoffRequest, surface: Surface) -> Review:
        self.requests.append(request)
        if self._review_actions is not None:
            self._review_actions(surface)
        return self._review

    def operate(self, request: HandoffRequest, surface: Surface) -> HandBack:
        if self._actions is not None:
            self._actions(surface)  # what the "person" does in the live session
        return self._hand_back