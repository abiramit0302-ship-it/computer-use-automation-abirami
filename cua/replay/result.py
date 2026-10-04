"""The replay result contract: what a calling agent gets back.

Every replay returns exactly ONE of four statuses:
  success           the goal was reached; `outputs` holds the declared outputs
  business_outcome  a legitimate non-success answer (e.g. MEMBER_NOT_FOUND)
  failed            a hard failure, with enough detail to debug without re-running
  needs_human       automation paused; a person must decide or act
The validator enforces that each status carries exactly the details that belong to it.
"""

from enum import StrEnum

from pydantic import Field, model_validator

from cua.schema import StrictModel


class ReplayStatus(StrEnum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"


class FailureCategory(StrEnum):
    INVALID_INPUT = "invalid_input"                  # caller broke an input rule; caught before the app
    ARTIFACT_NOT_APPROVED = "artifact_not_approved"  # draft capability, drafts not allowed
    CONFIGURATION_ERROR = "configuration_error"      # e.g. capability built for a different app profile
    POLICY_DENIED = "policy_denied"                  # a step would leave the allowlist
    SIGN_ON_FAILED = "sign_on_failed"
    TARGET_NOT_FOUND = "target_not_found"            # a needed control is missing, no known reason why
    CHECKPOINT_FAILED = "checkpoint_failed"          # screen isn't what it should be, no known reason why
    APP_ERROR = "app_error"                          # the app showed a known hard failure (outage)
    TIMEOUT = "timeout"                              # a page didn't load within the time budget
    RECOVERY_EXHAUSTED = "recovery_exhausted"        # a known problem kept coming back


# Failures where trying the whole capability again later could plausibly succeed.
RETRYABLE = {
    FailureCategory.SIGN_ON_FAILED,
    FailureCategory.APP_ERROR,
    FailureCategory.TIMEOUT,
    FailureCategory.RECOVERY_EXHAUSTED,
}


class OutcomeInfo(StrictModel):
    id: str            # key in the capability's outcomes, e.g. "member_not_found"
    code: str          # stable code for the caller, e.g. "MEMBER_NOT_FOUND"
    description: str
    step_id: str       # where it was detected


class FailureInfo(StrictModel):
    category: FailureCategory
    message: str
    retryable: bool
    step_id: str | None = None      # None when the failure happened before any step ran
    intent: str | None = None       # the step's purpose, in plain words
    expected: str | None = None     # what should have been on screen
    observed: str | None = None     # what actually was
    screenshot: str | None = None   # path to a masked screenshot

    @classmethod
    def build(cls, category: FailureCategory, message: str, **details) -> "FailureInfo":
        """Create a failure whose `retryable` flag always follows from its category."""
        return cls(category=category, message=message, retryable=category in RETRYABLE, **details)


class EscalationInfo(StrictModel):
    step_id: str
    intent: str
    reason: str
    screenshot: str | None = None


class ReplayResult(StrictModel):
    run_id: str
    capability: str                                  # e.g. "member.lookup_balance@1.0.0"
    status: ReplayStatus
    outputs: dict[str, str] = Field(default_factory=dict)
    outcome: OutcomeInfo | None = None
    failure: FailureInfo | None = None
    escalation: EscalationInfo | None = None
    warnings: list[str] = Field(default_factory=list)    # fallback locators used, slow responses
    recoveries: list[str] = Field(default_factory=list)  # e.g. "dismissed system_notice before ..."
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _details_match_status(self):
        present = {
            "outcome": self.outcome is not None,
            "failure": self.failure is not None,
            "escalation": self.escalation is not None,
        }
        required = {
            ReplayStatus.SUCCESS: None,
            ReplayStatus.BUSINESS_OUTCOME: "outcome",
            ReplayStatus.FAILED: "failure",
            ReplayStatus.NEEDS_HUMAN: "escalation",
        }[self.status]
        for name, is_present in present.items():
            if is_present != (name == required):
                raise ValueError(f"a '{self.status.value}' result "
                                 f"{'needs' if name == required else 'must not have'} '{name}'")
        if self.outputs and self.status != ReplayStatus.SUCCESS:
            raise ValueError("only a 'success' result may carry outputs")
        return self