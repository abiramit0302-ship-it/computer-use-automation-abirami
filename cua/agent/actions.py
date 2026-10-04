"""The planner's action vocabulary: the ONLY things a planner (LLM or mock) can ask for.

Deliberately narrower than the artifact schema:
  * Targets are described the way a person would (a named control, a field in a labelled
    row, a table cell), never by position or coordinates, so whatever the planner picks
    is already a robust locator. Fallbacks are added later by the recorder, never chosen
    by the planner.
  * Values may contain {{inputs.*}} placeholders but never {{secrets.*}}.
  * Every action states its intent in plain words; it becomes the recorded step's intent.
  * Finish and GiveUp let the planner end a run honestly instead of guessing.
"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from cua.schema import (
    IDENTIFIER, TEMPLATE_RE, Anchor, RoleInScopeLocator, RoleNameLocator, Risk, Route,
    Sensitivity, StrictModel, TableCellLocator, Target,
)

# --- How the planner points at things ---------------------------------------------


class NamedControl(StrictModel):
    """A control with its own visible name, e.g. button 'Search'."""
    kind: Literal["named"] = "named"
    role: str
    name: str

    def locator(self) -> RoleNameLocator:
        return RoleNameLocator(role=self.role, name=self.name)

    def describe(self) -> str:
        return f"{self.role} '{self.name}'"


class FieldInRow(StrictModel):
    """An unnamed control identified by the label of the row it sits in."""
    kind: Literal["in_row"] = "in_row"
    role: str
    row_label: str

    def locator(self) -> RoleInScopeLocator:
        return RoleInScopeLocator(role=self.role, scope=Anchor(role="row", name=self.row_label))

    def describe(self) -> str:
        return f"{self.role} in the row labelled '{self.row_label}'"


class TableCell(StrictModel):
    """A value found by meaning: the row containing `row`, in the column headed `column`."""
    kind: Literal["table_cell"] = "table_cell"
    row: str
    column: str

    def locator(self) -> TableCellLocator:
        return TableCellLocator(row_where=self.row, column=self.column)

    def describe(self) -> str:
        return f"cell in row '{self.row}', column '{self.column}'"


TargetSpec = Annotated[NamedControl | FieldInRow | TableCell, Field(discriminator="kind")]


def to_target(spec: NamedControl | FieldInRow | TableCell) -> Target:
    """Turn what the planner pointed at into a schema Target with one (primary) locator."""
    return Target(description=spec.describe(), locators=[spec.locator()])


# --- What the planner can ask for ----------------------------------------------------


def _only_input_placeholders(text: str) -> None:
    for namespace, name in TEMPLATE_RE.findall(text):
        if namespace != "inputs":
            raise ValueError(f"only inputs.* placeholders are allowed, found {namespace}.{name}")


class _PlannerAction(StrictModel):
    intent: str = Field(min_length=1, description="What this action is for, in plain words")


class NavigateTo(_PlannerAction):
    action: Literal["navigate"] = "navigate"
    route: Route
    expect: Anchor | None = None  # what should be visible afterwards

    @model_validator(mode="after")
    def _placeholders(self):
        _only_input_placeholders(self.route)
        return self


class TypeInto(_PlannerAction):
    action: Literal["fill"] = "fill"
    target: TargetSpec
    value: str  # may be a placeholder such as {{inputs.member_id}}

    @model_validator(mode="after")
    def _placeholders(self):
        _only_input_placeholders(self.value)
        return self


class Choose(_PlannerAction):
    action: Literal["select"] = "select"
    target: TargetSpec
    value: str  # the option's visible label

    @model_validator(mode="after")
    def _placeholders(self):
        _only_input_placeholders(self.value)
        return self


class ClickOn(_PlannerAction):
    action: Literal["click"] = "click"
    target: TargetSpec
    risk: Risk                     # no default: the planner must say how risky the click is
    expect: Anchor | None = None   # what should be visible afterwards


class ReadValue(_PlannerAction):
    action: Literal["extract"] = "extract"
    target: TargetSpec
    output: str = Field(pattern=IDENTIFIER)  # name of the output to produce, e.g. "balance"
    parse: Literal["text", "money"] = "text"
    sensitivity: Sensitivity

    @model_validator(mode="after")
    def _not_secret(self):
        if self.sensitivity == Sensitivity.SECRET:
            raise ValueError("outputs can never be secrets")
        return self


class Finish(_PlannerAction):
    """The goal is complete. `intent` summarizes what was achieved."""
    action: Literal["finish"] = "finish"


class GiveUp(_PlannerAction):
    """The planner can't proceed safely. `intent` explains why. Better than guessing."""
    action: Literal["give_up"] = "give_up"


PlannerAction = Annotated[
    NavigateTo | TypeInto | Choose | ClickOn | ReadValue | Finish | GiveUp,
    Field(discriminator="action"),
]

_ADAPTER = TypeAdapter(PlannerAction)


def parse_action(data: dict) -> NavigateTo | TypeInto | Choose | ClickOn | ReadValue | Finish | GiveUp:
    """Validate raw planner output (e.g. an LLM tool call) into exactly one allowed action."""
    return _ADAPTER.validate_python(data)


def action_json_schema() -> dict:
    """JSON Schema for the action vocabulary, used to tell an LLM exactly what it may output."""
    return _ADAPTER.json_schema()