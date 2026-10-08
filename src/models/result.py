"""The structured result every run returns, whatever happened."""

from decimal import Decimal
from enum import Enum

from pydantic import Field, model_validator

from .common import OUTCOME_CODE_PATTERN, RunMode, StrictModel


class RunStatus(str, Enum):
    SUCCESS = "SUCCESS"                            # outputs returned, success check passed
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"          # valid answer that is not success, e.g. MEMBER_NOT_FOUND
    FAILED = "FAILED"                              # hard failure, with step / expected / observed
    ESCALATED = "ESCALATED"                        # handed to a human who did not finish it
    ABORTED_BY_OPERATOR = "ABORTED_BY_OPERATOR"    # human typed abort
    REJECTED_BY_OPERATOR = "REJECTED_BY_OPERATOR"  # human said no to a risky action
    NO_MATCHING_RECIPE = "NO_MATCHING_RECIPE"      # catalog found nothing (strict replay)
    INVALID_INPUT = "INVALID_INPUT"                # inputs failed the recipe's types/patterns
    DRAFT_NOT_ALLOWED = "DRAFT_NOT_ALLOWED"        # strict replay of a draft without --allow-draft


class FailureInfo(StrictModel):
    step: int | None = None  # None when the failure is before any step (e.g. login)
    expected: str
    observed: str
    error_type: str          # handler id, or "unknown" for default_on_unknown
    evidence: list[str] = []  # screenshot, accessibility snapshot, trace paths


# Currency is Decimal, never float: money must not pick up rounding errors.
OutputValue = str | int | Decimal


class RunResult(StrictModel):
    status: RunStatus
    run_id: str
    mode: RunMode
    recipe_id: str | None = None
    recipe_version: str | None = None
    outputs: dict[str, OutputValue] = {}
    outcome_code: str | None = Field(default=None, pattern=OUTCOME_CODE_PATTERN)
    failure: FailureInfo | None = None
    message: str | None = None  # one human-readable line, e.g. why the input was invalid
    recoveries: list[str] = []
    llm_recovery_used: bool = False
    human_interventions: list[str] = []
    approvals: list[str] = []
    warnings: list[str] = []
    log_file: str | None = None

    @model_validator(mode="after")
    def status_fields_consistent(self) -> "RunResult":
        # A caller should be able to trust the status alone; these rules keep the rest consistent.
        s = self.status
        if s == RunStatus.SUCCESS and (self.failure or self.outcome_code):
            raise ValueError("SUCCESS cannot have a failure or outcome_code")
        if s == RunStatus.BUSINESS_OUTCOME and not self.outcome_code:
            raise ValueError("BUSINESS_OUTCOME needs outcome_code")
        if s in {RunStatus.FAILED, RunStatus.ESCALATED} and not self.failure:
            raise ValueError(f"{s.value} needs failure details")
        if s != RunStatus.SUCCESS and self.outputs:
            raise ValueError("only SUCCESS may return outputs")
        return self
