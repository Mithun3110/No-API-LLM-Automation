"""The human operator: who receives intervention requests, approves risky actions, and says
resume or abort. The operator UI is deliberately minimal (the terminal); the control model
behind it is real. A production version would be an operator queue with remote co-browsing.
"""

import select
import sys
from pathlib import Path
from typing import Callable, Literal, Protocol

from src.models import InterventionRequest

from .actions import ApprovalRequest

Decision = Literal["resume", "abort"]


class Operator(Protocol):
    def notify(self, request: InterventionRequest, saved_to: Path) -> None: ...
    def wait_for_decision(self, browser) -> Decision: ...
    def approve(self, request: ApprovalRequest) -> bool: ...


class TerminalOperator:
    """A person at this terminal, using the same browser window the automation used."""

    def notify(self, request: InterventionRequest, saved_to: Path) -> None:
        what = f"goal: {request.goal}" if request.goal else f"recipe: {request.recipe_id}@{request.recipe_version}"
        step = f"{request.step} ({request.step_description})" if request.step_description else str(request.step)
        print("\n" + "=" * 70)
        print("  HUMAN TAKEOVER NEEDED")
        print("=" * 70)
        print(f"  run       : {request.run_id}")
        print(f"  {what}")
        print(f"  step      : {step}")
        print(f"  reason    : {request.reason} - {request.detail}")
        print(f"  page      : {request.current_url}")
        print(f"  screenshot: {request.screenshot_path}")
        print(f"  request   : {saved_to}")
        print("-" * 70)
        print("  You now control the browser window. Do what is needed there, then type:")
        print("    resume  - hand control back to the automation")
        print("    abort   - stop the run")
        print("=" * 70)

    def wait_for_decision(self, browser) -> Decision:
        # Not input(): that would block Python, and the browser could not deliver the human's
        # recorded clicks meanwhile. Instead, let the browser run in short slices and check
        # the terminal between them.
        while True:
            browser.wait(0.2)
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                continue
            line = sys.stdin.readline().strip().lower()
            if line in ("resume", "abort"):
                return line  # type: ignore[return-value]
            if line:
                print("  type 'resume' or 'abort'")

    def approve(self, request: ApprovalRequest) -> bool:
        print("\n" + "=" * 70)
        print(f"  APPROVAL NEEDED (step {request.step})")
        print("=" * 70)
        print(f"  action : {request.action_description}")
        print(f"  why    : {request.reason}")
        print(f"  page   : {request.page_summary}")
        print("=" * 70)
        while True:
            answer = input("  Approve this action? [yes/no]: ").strip().lower()
            if answer in ("yes", "no"):
                return answer == "yes"


class ScriptedOperator:
    """For tests: the 'human' acts through real browser events, so recording is tested for real."""

    def __init__(self, decisions: list[Decision], human: Callable | None = None, approvals: list[bool] | None = None):
        self.decisions = list(decisions)
        self.human = human            # human(browser): what the person does while in control
        self.approvals = list(approvals or [])
        self.requests: list[InterventionRequest] = []
        self.approval_requests: list[ApprovalRequest] = []

    def notify(self, request: InterventionRequest, saved_to: Path) -> None:
        self.requests.append(request)

    def wait_for_decision(self, browser) -> Decision:
        if self.human:
            self.human(browser)
        browser.wait(0.5)  # let the recorded actions arrive
        return self.decisions.pop(0) if self.decisions else "abort"

    def approve(self, request: ApprovalRequest) -> bool:
        self.approval_requests.append(request)
        return self.approvals.pop(0) if self.approvals else False
