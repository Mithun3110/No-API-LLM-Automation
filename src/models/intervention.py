"""What a human operator receives when automation hands over control."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from .common import RunMode, StrictModel

# Why automation stopped. Fixed list, so an operator queue could route on it later.
StopReason = Literal[
    "repeated_failure",     # the same action failed twice
    "page_unchanged",       # an action had no visible effect
    "agent_asked",          # the LLM called ask_human
    "step_limit",           # discovery hit the step limit
    "timeout",              # discovery ran out of time
    "hard_failure",         # replay hit a hard failure or unknown state
    "recovery_failed",      # bounded LLM recovery could not fix the step
    "no_resume_point",      # after a takeover, no step's expect_page matched the page
]


class InterventionRequest(StrictModel):
    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mode: RunMode
    goal: str | None = None            # discovery: the goal
    recipe_id: str | None = None       # replay: the capability being run
    recipe_version: str | None = None
    step: int | None = None
    step_description: str | None = None
    reason: StopReason
    detail: str                        # one readable sentence: what went wrong (masked)
    current_url: str
    screenshot_path: str | None = None

    @model_validator(mode="after")
    def has_context(self) -> "InterventionRequest":
        # An operator must know WHAT was being attempted, not just that something broke.
        if not self.goal and not self.recipe_id:
            raise ValueError("intervention request needs a goal or a recipe_id")
        return self
