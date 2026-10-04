"""The planner boundary: the ONLY place an AI model sits in the whole system.

The discovery loop calls decide() once per turn. A planner sees the goal (with input
PLACEHOLDERS, never values), the names of the inputs, the REDACTED screen, and what has
happened so far, and answers with exactly one action from the vocabulary in actions.py.

Two implementations share this interface: a scripted mock (no network, deterministic)
and a Claude-backed planner. The discovery loop can't tell them apart.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from cua.agent.actions import PlannerAction


@dataclass(frozen=True)
class Feedback:
    """What happened when an action was tried, told back to the planner."""
    ok: bool
    message: str  # e.g. "done" or "ambiguous: 2 controls match; be more specific"


@dataclass(frozen=True)
class Turn:
    action: PlannerAction
    feedback: Feedback


@dataclass(frozen=True)
class PlannerRequest:
    goal: str              # e.g. "Look up member {{inputs.member_id}} and read their savings balance"
    input_names: list[str]
    screen: str            # REDACTED accessibility tree of the current screen
    route: str
    history: list[Turn]
    steps_remaining: int


class Planner(ABC):
    name: str = "planner"  # recorded in the artifact's provenance

    @abstractmethod
    def decide(self, request: PlannerRequest) -> PlannerAction:
        """Choose exactly one next action."""

class PlannerError(Exception):
    """The planner couldn't produce a decision (e.g. the model API was unreachable)."""