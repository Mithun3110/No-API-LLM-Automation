"""Shared building blocks for all models."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    # extra="forbid": a typo like "wiat_for" is an error, not a silently ignored field.
    # Recipes are reviewed by humans and read by agents, so the file must mean exactly what it says.
    model_config = ConfigDict(extra="forbid")


class ControlState(str, Enum):
    """Who may act on the live browser session. Exactly one at a time."""
    AUTOMATION = "AUTOMATION"
    HUMAN = "HUMAN"
    PAUSED = "PAUSED"


# Actions a recipe step can perform.
StepAction = Literal["navigate", "click", "type", "select", "extract"]

# Actions the policy can allow (steps plus waiting, used by recoverable fixes and the agent).
PolicyAction = Literal["navigate", "click", "type", "select", "extract", "wait"]

# Which part of the system is acting or reporting.
RunMode = Literal["ask", "replay", "discover"]
LogMode = Literal["discover", "replay", "recovery", "human", "catalog", "session"]

# Value types for recipe inputs and outputs.
ValueType = Literal["string", "number", "currency"]

# Result-code format, e.g. MEMBER_NOT_FOUND.
OUTCOME_CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"
