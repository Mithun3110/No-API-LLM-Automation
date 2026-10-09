"""The seam between deterministic replay and bounded LLM recovery.

Replay only knows this interface. The LLM implementation lives in src/recovery/ and is passed
in by `ask`; strict `replay` passes nothing. So the replay package never imports an LLM client.
"""

from dataclasses import dataclass
from typing import Callable

from src.models import Step

# Failures worth one bounded LLM attempt: the page is not as recorded, but nothing is known to
# be wrong. Known bad states (server error, session expired, policy block) go to a human.
RECOVERABLE_BY_LLM = {"element_not_found", "action_failed", "wrong_page", "wait_timeout", "parse_error"}


@dataclass(frozen=True)
class RecoveryRequest:
    step: Step
    value: str | None          # the step's value with inputs filled in (type/select)
    allowed_values: tuple[str, ...]  # the run's input values: the only text recovery may type
    expected: str
    observed: str
    error_type: str


@dataclass
class RecoveryOutcome:
    success: bool
    detail: str
    actions: int = 0           # how many browser actions recovery used
    extracted: str | None = None  # extract steps: the raw text it read


Recoverer = Callable[[RecoveryRequest], RecoveryOutcome]
