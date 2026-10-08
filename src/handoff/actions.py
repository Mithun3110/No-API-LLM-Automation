"""What callers ask the session to do, and what comes back.

These types are the vocabulary shared by discovery, replay and recovery. None of them
mention Playwright: the session translates them into browser calls.
"""

from dataclasses import dataclass, field
from typing import Callable, Literal

from src.models.common import LogMode, PolicyAction
from src.models.recipe import RoleStrategy, Strategy

Status = Literal["done", "blocked", "rejected", "not_found", "failed"]


@dataclass(frozen=True)
class Action:
    kind: PolicyAction                         # navigate, click, type, select, extract, wait
    strategies: tuple[Strategy, ...] = ()      # how to find the element (click/type/select/extract)
    url: str | None = None                     # navigate: absolute, or relative to the bank's base URL
    value: str | None = None                   # type/select
    seconds: float | None = None               # wait
    step: int | None = None                    # recipe step number, for the log
    step_risk: str = "safe"                    # safe | irreversible (from the recipe step)
    reason: str | None = None                  # why: step description or the LLM's reasoning
    mode: LogMode = "replay"                   # who is asking, for the log

    @property
    def declared_role_and_name(self) -> tuple[str | None, str | None]:
        """The element's identity as recorded (first role strategy), used for the risk check.

        Taken from the recipe, not from whichever backup strategy matched, so falling back to
        CSS can never hide that this is the "Confirm" button.
        """
        for s in self.strategies:
            if isinstance(s, RoleStrategy):
                return s.role, s.name
        return None, None


@dataclass
class ActionOutcome:
    status: Status
    message: str = ""                          # readable detail for failures and blocks
    text: str | None = None                    # extract: the raw text read from the page
    strategy_used: str | None = None           # e.g. 'label "Member ID"'
    drifted: bool = False                      # a backup strategy was needed
    attempts: list[str] = field(default_factory=list)
    rule: str | None = None                    # safety rule that decided, if any
    # Did the action reach the page? True: yes. False: certainly not (not found, blocked by
    # policy or an overlay, rejected), so retrying is safe. None: unknown, never retry.
    performed: bool | None = None
    settled: bool = True                       # False: a page load was still in flight afterwards

    @property
    def ok(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True)
class ApprovalRequest:
    """Shown to the human before a risky click. Everything in it is already masked."""
    step: int | None
    action_description: str                    # e.g. 'click button "Confirm" on /member/***45/open-account'
    reason: str                                # why approval is needed
    page_summary: str                          # what is about to be confirmed, masked


# Called for risky actions. Returns True to approve. The terminal version arrives in step 9.
Approver = Callable[[ApprovalRequest], bool]


def reject_all(_request: ApprovalRequest) -> bool:
    """Default approver: with nobody to ask, the conservative answer is no."""
    return False
