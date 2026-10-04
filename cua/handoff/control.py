"""Who controls the live session: exactly one holder at any moment.

    AUTOMATION -> AWAITING_OPERATOR -> OPERATOR -> AUTOMATION   (operator took control, handed back)
    AUTOMATION -> AWAITING_OPERATOR -> AUTOMATION                (operator approved; automation acts)
    any live state -> ENDED                                       (operator rejected or aborted)

Automation must call require_automation() before EVERY action, so it can never act while a
person holds the session (the single-writer rule). Illegal moves raise instead of silently
corrupting the state, and every change is recorded: when, by whom, and why.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class ControlState(StrEnum):
    AUTOMATION = "automation"
    AWAITING_OPERATOR = "awaiting_operator"
    OPERATOR = "operator"
    ENDED = "ended"


ALLOWED = {
    (ControlState.AUTOMATION, ControlState.AWAITING_OPERATOR),
    (ControlState.AWAITING_OPERATOR, ControlState.OPERATOR),
    (ControlState.AWAITING_OPERATOR, ControlState.AUTOMATION),
    (ControlState.OPERATOR, ControlState.AUTOMATION),
    (ControlState.AUTOMATION, ControlState.ENDED),
    (ControlState.AWAITING_OPERATOR, ControlState.ENDED),
    (ControlState.OPERATOR, ControlState.ENDED),
}


class ControlError(Exception):
    """Someone tried to act without holding control, or an illegal handover was attempted."""


@dataclass(frozen=True)
class ControlChange:
    at: str
    from_state: ControlState
    to_state: ControlState
    by: str      # "automation" or "operator:<id>"
    reason: str


class ControlLease:
    def __init__(self, on_change: Callable[[ControlChange], None] | None = None):
        self.state = ControlState.AUTOMATION
        self.operator_id: str | None = None
        self.history: list[ControlChange] = []
        self._on_change = on_change

    @property
    def holder(self) -> str:
        if self.state == ControlState.AUTOMATION:
            return "automation"
        if self.state == ControlState.OPERATOR:
            return f"operator:{self.operator_id}"
        return "nobody"  # waiting for an operator, or ended

    def require_automation(self) -> None:
        """Call before EVERY automated action."""
        if self.state != ControlState.AUTOMATION:
            raise ControlError(f"automation may not act: session control is '{self.state.value}' "
                               f"(holder: {self.holder})")

    def request_operator(self, reason: str) -> None:
        self._move(ControlState.AWAITING_OPERATOR, by="automation", reason=reason)

    def grant_to(self, operator_id: str, reason: str = "operator took control") -> None:
        self._move(ControlState.OPERATOR, by=f"operator:{operator_id}", reason=reason)
        self.operator_id = operator_id

    def return_to_automation(self, by: str, reason: str) -> None:
        self._move(ControlState.AUTOMATION, by=by, reason=reason)
        self.operator_id = None

    def end(self, by: str, reason: str) -> None:
        self._move(ControlState.ENDED, by=by, reason=reason)

    def _move(self, to_state: ControlState, *, by: str, reason: str) -> None:
        if (self.state, to_state) not in ALLOWED:
            raise ControlError(f"illegal handover: {self.state.value} -> {to_state.value}")
        change = ControlChange(at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                               from_state=self.state, to_state=to_state, by=by, reason=reason)
        self.state = to_state
        self.history.append(change)
        if self._on_change is not None:
            self._on_change(change)