"""Safety policy: what the automation is allowed to do.

Default-deny: anything not explicitly allowed in policy.yaml is refused. Enforced in
two independent layers:
  1. Before every step, check_step() decides allow / require_approval / deny.
  2. Inside the browser, allows_request() is wired into Playwright's network layer, so
     even an unexpected link or redirect can't reach a page outside the allowlist.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import Field, model_validator

from cua.schema import NavigateAction, RoleNameLocator, Risk, Step, StrictModel, Target

ActionType = Literal["navigate", "fill", "select", "click", "extract"]


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyConfig(StrictModel):
    """The validated contents of policy.yaml."""
    version: Literal[1]
    allowed_origins: list[str] = Field(min_length=1)
    allowed_routes: list[str] = Field(min_length=1)
    allowed_actions: list[ActionType] = Field(min_length=1)
    risk_rules: dict[Risk, Verdict]
    irreversible_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self):
        missing = sorted(r.value for r in set(Risk) - set(self.risk_rules))
        if missing:
            raise ValueError(f"risk_rules must cover every risk level; missing {missing}")
        bad_routes = [r for r in self.allowed_routes if not r.startswith("/")]
        if bad_routes:
            raise ValueError(f"allowed_routes must start with '/': {bad_routes}")
        return self


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str  # always explained, so every decision can be logged and audited

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.ALLOW


def origin_of(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def route_matches(pattern: str, route: str) -> bool:
    """Segment-aware glob: '*' matches exactly ONE path segment and never crosses a '/'.

    '/member/*' matches '/member/12345' but not '/member/12345/open-account'.
    """
    pattern_parts = pattern.strip("/").split("/")
    route_parts = route.strip("/").split("/")
    if ".." in route_parts or len(pattern_parts) != len(route_parts):
        return False
    return all(fnmatchcase(part, pat) for pat, part in zip(pattern_parts, route_parts))


class Policy:
    def __init__(self, config: PolicyConfig):
        self.config = config
        self._irreversible = [re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
                              for word in config.irreversible_keywords]

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        data = yaml.safe_load(Path(path).read_text())
        return cls(PolicyConfig.model_validate(data))

    # --- Layer 1: before each step ------------------------------------------------

    def check_origin(self, url: str) -> Decision:
        origin = origin_of(url)
        if origin in self.config.allowed_origins:
            return Decision(Verdict.ALLOW, f"origin {origin} is allowlisted")
        return Decision(Verdict.DENY, f"origin {origin} is not in the allowlist")

    def check_route(self, route: str) -> Decision:
        for pattern in self.config.allowed_routes:
            if route_matches(pattern, route):
                return Decision(Verdict.ALLOW, f"route {route} matches {pattern}")
        return Decision(Verdict.DENY, f"route {route} is not in the allowlist")

    def effective_risk(self, step: Step) -> Risk:
        """The step's declared risk, RAISED to irreversible if what it clicks sounds irreversible.

        Risk can only go up here, never down: an artifact can't talk its way out of approval.
        """
        if step.action.type == "click" and step.target and self._sounds_irreversible(step.target):
            return Risk.IRREVERSIBLE
        return step.risk

    def check_step(self, step: Step, current_route: str) -> Decision:
        """May this (already rendered) step run, given the screen we're currently on?"""
        if step.action.type not in self.config.allowed_actions:
            return Decision(Verdict.DENY, f"action '{step.action.type}' is not allowed")

        route = step.action.route if isinstance(step.action, NavigateAction) else current_route
        route_decision = self.check_route(route)
        if not route_decision.allowed:
            return route_decision

        risk = self.effective_risk(step)
        verdict = self.config.risk_rules[risk]
        raised = "" if risk == step.risk else f" (raised from {step.risk.value}: target sounds irreversible)"
        return Decision(verdict, f"step '{step.id}' has risk {risk.value}{raised} -> {verdict.value}")

    # --- Layer 2: inside the browser ----------------------------------------------

    def allows_request(self, url: str, is_navigation: bool) -> bool:
        """Every request must stay on an allowed origin; page navigations must also hit an allowed route."""
        if not self.check_origin(url).allowed:
            return False
        return not is_navigation or self.check_route(urlparse(url).path).allowed

    # --- Helpers ------------------------------------------------------------------

    def _sounds_irreversible(self, target: Target) -> bool:
        # Only VISIBLE on-screen names count. Descriptions are free text, possibly written
        # by the LLM, and shouldn't decide risk either way.
        names = [loc.name for loc in target.locators if isinstance(loc, RoleNameLocator)]
        return any(pattern.search(name) for pattern in self._irreversible for name in names)