"""Artifact schema: the typed, versioned contract for recorded capabilities.

Two kinds of document, both stored as JSON:
  * AppProfile - one per vendor product: sign-on, app-wide interstitials, timing.
  * Capability - one per task: inputs, outputs, steps, business outcomes, success.

Loading a file through these models validates it, so a malformed or inconsistent
artifact is rejected before replay ever touches the application.

Run `python -m cua.schema` to export JSON Schema files into schemas/.
"""

import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"

IDENTIFIER = r"^[a-z][a-z0-9_]*$"
Route = Annotated[str, Field(pattern=r"^/")]  # path relative to a tenant's base URL
TEMPLATE_RE = re.compile(r"\{\{\s*(\w+)\.(\w+)\s*\}\}")


def template_refs(model: BaseModel) -> list[tuple[str, str]]:
    """Every {{namespace.name}} placeholder anywhere inside a model, e.g. ('inputs', 'member_id')."""
    return TEMPLATE_RE.findall(model.model_dump_json())


class StrictModel(BaseModel):
    """Base for every schema model: unknown fields are errors, and objects are immutable."""
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Vocabularies ---------------------------------------------------------------

class Sensitivity(StrEnum):
    """Drives redaction: logging code decides what to hide from this label."""
    PUBLIC = "public"          # safe to log as-is
    IDENTIFIER = "identifier"  # e.g. a member ID: masked in logs
    PII = "pii"                # personal data: never logged
    FINANCIAL = "financial"    # balances and amounts: never logged
    SECRET = "secret"          # credentials: never logged, stored, or passed in by callers


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    MONEY = "money"


class Risk(StrEnum):
    READ_ONLY = "read_only"        # changes nothing (reading, navigating, searching)
    REVERSIBLE = "reversible"      # changes something that can be undone (typing into a form)
    IRREVERSIBLE = "irreversible"  # commits a real change (confirming a new account)


class Status(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


# --- How controls are found -------------------------------------------------------

class Anchor(StrictModel):
    """A control described the way a person would: its role and visible name."""
    role: str
    name: str


class RoleNameLocator(StrictModel):
    """Best rung: the control has its own accessible name, e.g. button 'Search'."""
    strategy: Literal["role_name"] = "role_name"
    role: str
    name: str


class RoleInScopeLocator(StrictModel):
    """An unnamed control inside a named container, e.g. the textbox in row 'Member ID:'."""
    strategy: Literal["role_in_scope"] = "role_in_scope"
    role: str
    scope: Anchor


class NthOfRoleLocator(StrictModel):
    """Last-resort fallback by position. Fragile: replay records a warning when it's used."""
    strategy: Literal["nth_of_role"] = "nth_of_role"
    role: str
    index: int = Field(ge=0)


class TableCellLocator(StrictModel):
    """A cell found by meaning: the row containing `row_where`, in the column headed `column`."""
    strategy: Literal["table_cell"] = "table_cell"
    row_where: str
    column: str

class LabelledValueLocator(StrictModel):
    """The value shown beside a label, e.g. the cell right after 'Confirmation No.:'."""
    strategy: Literal["labelled_value"] = "labelled_value"
    label: str

Locator = Annotated[
    RoleNameLocator | RoleInScopeLocator | NthOfRoleLocator | TableCellLocator | LabelledValueLocator,
    Field(discriminator="strategy"),
]


class Target(StrictModel):
    """A control to act on, with locators ranked from most to least robust."""
    description: str
    locators: list[Locator] = Field(min_length=1)
    rationale: str | None = None


# --- What a step does ---------------------------------------------------------------

class NavigateAction(StrictModel):
    type: Literal["navigate"] = "navigate"
    route: Route


class FillAction(StrictModel):
    type: Literal["fill"] = "fill"
    value: str  # usually a template such as {{inputs.member_id}}


class SelectAction(StrictModel):
    type: Literal["select"] = "select"
    value: str


class ClickAction(StrictModel):
    type: Literal["click"] = "click"


class ExtractAction(StrictModel):
    type: Literal["extract"] = "extract"
    into: str = Field(pattern=IDENTIFIER)  # name of a declared output
    parse: Literal["text", "money"] = "text"


Action = Annotated[
    NavigateAction | FillAction | SelectAction | ClickAction | ExtractAction,
    Field(discriminator="type"),
]


class Expectation(StrictModel):
    """A checkpoint: what must be true after a step, so we never assume a click worked."""
    url: Route | None = None
    visible: list[Anchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks_something(self):
        if self.url is None and not self.visible:
            raise ValueError("an expectation must check a url or at least one visible element")
        return self


class Step(StrictModel):
    id: str = Field(pattern=IDENTIFIER)
    intent: str = Field(min_length=1)  # human-readable purpose, for reviewers and operators
    action: Action
    target: Target | None = None
    risk: Risk  # deliberately no default: someone must decide how risky every step is
    expect: Expectation | None = None
    outcomes: list[str] = Field(default_factory=list)  # business outcomes possible here

    @model_validator(mode="after")
    def _target_matches_action(self):
        needs_target = self.action.type != "navigate"
        if needs_target and self.target is None:
            raise ValueError(f"step '{self.id}': a {self.action.type} action needs a target")
        if not needs_target and self.target is not None:
            raise ValueError(f"step '{self.id}': a navigate action must not have a target")
        return self


# --- Recognizing states ---------------------------------------------------------------

class Detector(StrictModel):
    """Recognizes a state by what's on screen, as a human would, not by HTTP status codes."""
    text: str | None = None
    url: Route | None = None

    @model_validator(mode="after")
    def _detects_something(self):
        if self.text is None and self.url is None:
            raise ValueError("a detector needs text or a url")
        return self


class BusinessOutcome(StrictModel):
    """A legitimate, non-success answer the caller needs, e.g. 'no such member'."""
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str
    detect: Detector


class DismissRecovery(StrictModel):
    type: Literal["dismiss"] = "dismiss"
    target: Target


class ReauthenticateRecovery(StrictModel):
    type: Literal["reauthenticate"] = "reauthenticate"


Recovery = Annotated[DismissRecovery | ReauthenticateRecovery, Field(discriminator="type")]


class Interstitial(StrictModel):
    """An app-wide state that can interrupt any task: a popup, an expired session, an outage."""
    description: str
    detect: Detector
    kind: Literal["recoverable", "hard_failure"]
    recovery: Recovery | None = None

    @model_validator(mode="after")
    def _recovery_matches_kind(self):
        if self.kind == "recoverable" and self.recovery is None:
            raise ValueError("a recoverable interstitial needs a recovery")
        if self.kind == "hard_failure" and self.recovery is not None:
            raise ValueError("a hard failure must not declare a recovery")
        return self


# --- The capability contract ------------------------------------------------------------

class InputParam(StrictModel):
    name: str = Field(pattern=IDENTIFIER)
    type: ValueType
    description: str
    sensitivity: Sensitivity
    required: bool = True
    default: str | None = None
    pattern: str | None = None  # regex the value must fully match

    @model_validator(mode="after")
    def _sane(self):
        if self.sensitivity == Sensitivity.SECRET:
            raise ValueError(f"input '{self.name}': secrets are never caller inputs; "
                             "reference them from the app profile instead")
        if self.required and self.default is not None:
            raise ValueError(f"input '{self.name}': a parameter with a default must set required=false")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"input '{self.name}': invalid pattern: {exc}") from exc
        return self


class OutputParam(StrictModel):
    name: str = Field(pattern=IDENTIFIER)
    type: ValueType
    description: str
    sensitivity: Sensitivity


class SuccessCondition(StrictModel):
    outputs_present: list[str] = Field(default_factory=list)
    visible: list[Anchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks_something(self):
        if not self.outputs_present and not self.visible:
            raise ValueError("success must require outputs or visible elements")
        return self


class CapabilityMeta(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")  # e.g. member.lookup_balance
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")                    # version of this recipe
    status: Status = Status.DRAFT
    summary: str
    app_profile: str  # id of the AppProfile this capability runs on


class Provenance(StrictModel):
    """Where the recipe came from. Links to the discovery run; never embeds its transcript."""
    discovered_by: str
    discovery_run_id: str
    recorded_at: datetime
    goal: str


class Capability(StrictModel):
    schema_version: Literal["1.0"]  # version of the FORMAT, separate from meta.version
    meta: CapabilityMeta
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputParam] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    outcomes: dict[str, BusinessOutcome] = Field(default_factory=dict)
    success: SuccessCondition
    provenance: Provenance

    @model_validator(mode="after")
    def _references_are_consistent(self):
        problems: list[str] = []
        input_names = [p.name for p in self.inputs]
        output_names = [o.name for o in self.outputs]
        step_ids = [s.id for s in self.steps]

        for label, names in (("input", input_names), ("output", output_names), ("step", step_ids)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            if duplicates:
                problems.append(f"duplicate {label} names: {duplicates}")

        extracted: set[str] = set()
        for step in self.steps:
            for namespace, name in template_refs(step):
                if namespace != "inputs":
                    problems.append(f"step '{step.id}': capabilities may only use inputs.* "
                                    f"placeholders, found {namespace}.{name}")
                elif name not in input_names:
                    problems.append(f"step '{step.id}': uses undeclared input '{name}'")
            for outcome in step.outcomes:
                if outcome not in self.outcomes:
                    problems.append(f"step '{step.id}': refers to undeclared outcome '{outcome}'")
            if isinstance(step.action, ExtractAction):
                if step.action.into not in output_names:
                    problems.append(f"step '{step.id}': extracts into undeclared output "
                                    f"'{step.action.into}'")
                extracted.add(step.action.into)

        for name in output_names:
            if name not in extracted:
                problems.append(f"output '{name}' is declared but no step extracts it")
        for name in self.success.outputs_present:
            if name not in output_names:
                problems.append(f"success requires undeclared output '{name}'")

        if problems:
            raise ValueError("; ".join(problems))
        return self


# --- The app profile -------------------------------------------------------------------

class Timing(StrictModel):
    action_timeout_ms: int = Field(default=5000, gt=0)
    navigation_timeout_ms: int = Field(default=10000, gt=0)
    max_recoveries_per_step: int = Field(default=2, ge=0)


class SignOn(StrictModel):
    route: Route  # where the sign-on screen lives
    steps: list[Step] = Field(min_length=1)
    success: Expectation


class AppProfile(StrictModel):
    schema_version: Literal["1.0"]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    product_version: str
    description: str
    secrets: list[str] = Field(default_factory=list)  # names the runtime must supply, never values
    sensitive_labels: list[str] = Field(default_factory=list)  # screen labels whose value is personal data
    sign_on: SignOn
    interstitials: dict[str, Interstitial] = Field(default_factory=dict)
    timing: Timing = Field(default_factory=Timing)

    @model_validator(mode="after")
    def _secret_refs_declared(self):
        problems: list[str] = []
        for step in self.sign_on.steps:
            for namespace, name in template_refs(step):
                if namespace != "secrets":
                    problems.append(f"sign-on step '{step.id}': only secrets.* placeholders "
                                    f"are allowed, found {namespace}.{name}")
                elif name not in self.secrets:
                    problems.append(f"sign-on step '{step.id}': uses undeclared secret '{name}'")
        if problems:
            raise ValueError("; ".join(problems))
        return self


# --- Reading and writing ------------------------------------------------------------------

def load_capability(path: str | Path) -> Capability:
    return Capability.model_validate_json(Path(path).read_text())


def load_app_profile(path: str | Path) -> AppProfile:
    return AppProfile.model_validate_json(Path(path).read_text())


def save(model: BaseModel, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(model.model_dump_json(indent=2, exclude_none=True) + "\n")


if __name__ == "__main__":
    out_dir = Path("schemas")
    out_dir.mkdir(exist_ok=True)
    for model, name in ((Capability, "capability"), (AppProfile, "app_profile")):
        schema = model.model_json_schema()
        (out_dir / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
        print(f"wrote schemas/{name}.schema.json")