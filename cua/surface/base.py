"""The seam between WHAT to do and HOW to do it on a particular kind of screen.

Replay, the agent loop and the guardrails only ever talk to a Surface. WebSurface
drives a browser through Playwright. A desktop surface would implement the same
operations through the operating system's accessibility API (UI Automation on
Windows, the AX API on macOS), and nothing above this layer would change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from collections.abc import Sequence
from pathlib import Path

from cua.schema import Anchor, Target


@dataclass(frozen=True)
class Resolution:
    """Which of a target's ranked locators found the control."""
    locator_index: int
    strategy: str

    @property
    def used_fallback(self) -> bool:
        """True when the preferred locator failed: an early warning that the UI has drifted."""
        return self.locator_index > 0


@dataclass(frozen=True)
class Observation:
    """What the screen shows right now, in the text form the LLM reads."""
    route: str
    title: str
    accessibility_tree: str


class SurfaceError(Exception):
    """Base class for problems acting on a surface."""


class TargetNotFound(SurfaceError):
    """No locator matched exactly one control before the timeout."""

    def __init__(self, description: str, attempts: list[tuple[str, int]]):
        self.description = description
        self.attempts = attempts  # (strategy, number of matches) from the final check
        tried = ", ".join(f"{strategy}: {count} match(es)" for strategy, count in attempts)
        super().__init__(f"could not find exactly one '{description}' ({tried})")

class NavigationBlocked(SurfaceError):
    """The browser refused to load a page because the policy doesn't allow it."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(f"navigation to {url} was blocked by policy")


class Surface(ABC):
    """Everything the rest of the system is allowed to do to an application."""

    @abstractmethod
    def goto(self, route: str) -> None:
        """Open a route, relative to the tenant's base URL or entry point."""

    @abstractmethod
    def current_route(self) -> str:
        """The route currently shown, e.g. '/member/12345'."""

    @abstractmethod
    def observe(self) -> Observation:
        """Describe the current screen (used by the LLM during discovery)."""

    @abstractmethod
    def text_visible(self, text: str) -> bool:
        """Is this text visible anywhere on screen right now? (detects outcomes and popups)"""

    @abstractmethod
    def control_visible(self, anchor: Anchor) -> bool:
        """Is a control with this role and name visible right now? (checks checkpoints)"""

    @abstractmethod
    def fill(self, target: Target, value: str) -> Resolution:
        """Type a value into a field."""

    @abstractmethod
    def select(self, target: Target, value: str) -> Resolution:
        """Choose an option from a dropdown by its visible label."""

    @abstractmethod
    def click(self, target: Target) -> Resolution:
        """Click a control and wait for any resulting page load."""

    @abstractmethod
    def read_text(self, target: Target) -> tuple[str, Resolution]:
        """Read the visible text of a control."""

    @abstractmethod
    def screenshot(self, path: Path, sensitive_labels: Sequence[str] = ()) -> None:
        """Save an image of the current screen for evidence, masking sensitive values."""

    def navigation_pending(self) -> bool:
        """Is a page load still in progress? Surfaces that can't tell simply return False."""
        return False
    
    def position_among_role(self, target: Target, role: str) -> int | None:
        """Where this control sits among all controls with the same role (for a fallback
        locator). Surfaces that can't tell return None, and no fallback is recorded."""
        return None