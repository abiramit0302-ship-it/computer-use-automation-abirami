"""The Claude-backed planner.

Claude is given one tool per action in the planner vocabulary and must call exactly one
per turn. Tool input schemas are GENERATED from the same Pydantic classes that validate
the answers, so the instructions and the check can never drift apart. Each turn is a
fresh, self-contained message: goal (with placeholders), input names, route, a summary
of earlier actions and their feedback, and the REDACTED screen. An invalid answer gets
one corrective retry; a second invalid answer raises PlannerError.

Note: in this submission this path is exercised offline with a fake client only; the
evidence runs use the scripted mock (see README).
"""

import os

import anthropic
from pydantic import ValidationError

from cua.agent.actions import (
    Choose, ClickOn, Finish, GiveUp, NavigateTo, ReadValue, TypeInto, parse_action,
)
from cua.agent.planner import Planner, PlannerError, PlannerRequest

ACTION_MODELS = [NavigateTo, TypeInto, Choose, ClickOn, ReadValue, Finish, GiveUp]

TOOL_DESCRIPTIONS = {
    "navigate": "Open a route of the application (a path starting with '/').",
    "fill": "Type a value into a text field. Use {{inputs.NAME}} placeholders for inputs.",
    "select": "Choose an option in a dropdown by its visible label.",
    "click": "Click a control. State honestly whether the click is read_only, reversible or "
             "irreversible (anything that commits a change is irreversible).",
    "extract": "Read a value from the screen into a named output.",
    "finish": "The goal is complete. Summarize what was achieved.",
    "give_up": "You cannot proceed safely or confidently. Explain why. Prefer this to guessing.",
}

SYSTEM_PROMPT = """You operate a bank's back-office web application on behalf of an AI agent.
Each turn you see the current screen as an accessibility tree and choose EXACTLY ONE action
by calling one tool.

Rules:
- Point at controls the way a person would: by role and visible name ("named"), by the
  label of the table row a field sits in ("in_row"), or by table row and column header
  ("table_cell"). Never guess at positions.
- Inputs are given as placeholders such as {{inputs.member_id}}. Type the placeholder
  itself; you will never see the real value. Never invent values.
- Some screen values are redacted (e.g. [MONEY], [PII]). That is expected; work with the
  structure of the screen.
- After a click or navigation, name a landmark you expect to see next (expect), if any.
- If an action fails, read the feedback and adjust. If you cannot proceed safely, call
  give_up with the reason. Never take risky actions to "see what happens".
- Call finish only when the goal is fully achieved."""


def tool_definitions() -> list[dict]:
    """One tool per action, with input schemas generated from the vocabulary's classes."""
    tools = []
    for model in ACTION_MODELS:
        name = model.model_fields["action"].default
        schema = model.model_json_schema()
        schema.pop("title", None)
        schema["properties"].pop("action", None)  # implied by which tool is called
        schema["required"] = [field for field in schema.get("required", []) if field != "action"]
        tools.append({"name": name, "description": TOOL_DESCRIPTIONS[name], "input_schema": schema})
    return tools


def render_request(request: PlannerRequest) -> str:
    """The self-contained message sent each turn. Contains only redacted, placeholder data."""
    placeholders = ", ".join("{{inputs." + name + "}}" for name in request.input_names) or "(none)"
    if request.history:
        history = "\n".join(
            f"{i}. {turn.action.action}: {turn.action.intent} -> "
            f"{'OK' if turn.feedback.ok else 'FAILED'}: {turn.feedback.message}"
            for i, turn in enumerate(request.history, start=1))
    else:
        history = "(nothing yet)"
    return (f"GOAL: {request.goal}\n"
            f"INPUT PLACEHOLDERS: {placeholders}\n"
            f"CURRENT ROUTE: {request.route}\n"
            f"ACTIONS LEFT: {request.steps_remaining}\n\n"
            f"WHAT HAS HAPPENED SO FAR:\n{history}\n\n"
            f"CURRENT SCREEN (accessibility tree; sensitive values redacted):\n{request.screen}\n\n"
            "Choose exactly one next action by calling one tool.")


class ClaudePlanner(Planner):
    def __init__(self, *, model: str | None = None, client=None):
        self.model = model or os.environ.get("CUA_MODEL", "claude-sonnet-5-5")
        self.name = f"claude:{self.model}"
        if client is None:
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not key or key.startswith("your-"):
                raise PlannerError("ANTHROPIC_API_KEY is not set; use the scripted mock planner instead")
            client = anthropic.Anthropic()
        self.client = client
        self.tools = tool_definitions()

    def decide(self, request: PlannerRequest):
        messages = [{"role": "user", "content": render_request(request)}]
        for _attempt in range(2):
            try:
                response = self.client.messages.create(
                    model=self.model, max_tokens=1024, system=SYSTEM_PROMPT,
                    tools=self.tools, tool_choice={"type": "any"}, messages=messages)
            except anthropic.APIError as exc:
                raise PlannerError(f"model API error: {exc}") from exc

            call = next((block for block in response.content if block.type == "tool_use"), None)
            if call is None:
                raise PlannerError("the model did not choose an action")
            try:
                return parse_action({"action": call.name, **call.input})
            except ValidationError as exc:
                # Tell the model exactly what was wrong and let it try once more.
                messages += [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": call.id, "is_error": True,
                        "content": f"That action was invalid: {exc}. Choose again."}]},
                ]
        raise PlannerError("the model produced two invalid actions in a row")