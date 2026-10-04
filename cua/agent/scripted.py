"""The scripted mock planner: a stand-in for an LLM that needs no network or API key.

It follows a script, but it READS THE SCREEN first: each move names a landmark that must
be visible before the move is made. If the landmark is missing, if its last action failed,
or if the script runs out, it gives up, the way a careful model should, so the mock
exercises the same escalation paths as a real model. Scripts are JSON files in
planner_scripts/, so a reviewer can read exactly what the stand-in was going to do.
"""

from pathlib import Path
from typing import Any

from pydantic import Field

from cua.agent.actions import GiveUp, parse_action
from cua.agent.planner import Planner, PlannerRequest
from cua.schema import StrictModel


class ScriptedMove(StrictModel):
    when_visible: str | None = None  # text that must be on screen before this move
    action: dict[str, Any]           # one action in the planner vocabulary


class PlannerScript(StrictModel):
    name: str
    description: str
    moves: list[ScriptedMove] = Field(min_length=1)


class ScriptedPlanner(Planner):
    def __init__(self, script: PlannerScript):
        self.name = f"scripted-mock:{script.name}"
        # Validate every scripted action up front: a broken script fails at load time,
        # not halfway through a run.
        self._moves = [(move.when_visible, parse_action(move.action)) for move in script.moves]
        self._next = 0

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedPlanner":
        return cls(PlannerScript.model_validate_json(Path(path).read_text()))

    def decide(self, request: PlannerRequest):
        if request.history and not request.history[-1].feedback.ok:
            return GiveUp(intent=f"my last action failed ({request.history[-1].feedback.message}); "
                                 "this scripted plan has no recovery for that")
        if self._next >= len(self._moves):
            return GiveUp(intent="the script ended before the goal was finished")
        when_visible, action = self._moves[self._next]
        if when_visible is not None and when_visible not in request.screen:
            return GiveUp(intent=f"expected to see '{when_visible}' before '{action.intent}', "
                                 "but it is not on screen")
        self._next += 1
        return action