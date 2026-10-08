"""One live browser session per run, shared by automation and the human operator.

Session.perform() is the ONLY way any mode acts on the page. In order, it:
1. refuses unless automation holds control,
2. finds the element (strategies in order, exactly one match),
3. asks the safety guard (block / needs approval / allow),
4. performs the action through the browser layer,
5. logs what happened, masked.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from dotenv import load_dotenv

from src.browser import Browser, BrowserError, ElementBlocked
from src.logs import RunFolder, RunLogger
from src.logs.run_folder import DEFAULT_RUNS_DIR
from src.models import ControlState, Policy
from src.models.common import LogMode
from src.models.recipe import LabelStrategy, RoleStrategy, WaitCondition
from src.models.settings import Settings, load_settings
from src.safety import Masker, ProposedAction, SafetyGuard, load_policy

from .actions import Action, ActionOutcome, ApprovalRequest, Approver, reject_all


class SessionError(Exception):
    """The session could not be set up (missing credentials, login failed)."""


class ControlError(Exception):
    """Automation tried to act while it did not hold control. A bug, never a page state."""


TARGETED_ACTIONS = ("click", "type", "select", "extract")


class Session:
    def __init__(self, browser: Browser, guard: SafetyGuard, logger: RunLogger, settings: Settings,
                 approver: Approver = reject_all):
        self.browser = browser
        self.guard = guard
        self.logger = logger
        self.settings = settings
        self.approver = approver
        self._control = ControlState.AUTOMATION
        self.keep_trace = False  # set True to keep the trace even when the run did not raise
        self.approvals: list[str] = []  # every approval decision this run, masked, for the result

    # ------------------------------------------------------------ control
    @property
    def control(self) -> ControlState:
        return self._control

    def set_control(self, new: ControlState, reason: str, mode: LogMode = "session") -> None:
        old = self._control
        if new == old:
            return
        self._control = new
        self.logger.log(mode, new.value, new, action="control_change", reason=reason,
                        data={"from": old.value, "to": new.value})

    # ------------------------------------------------------------ the gate
    def perform(self, action: Action) -> ActionOutcome:
        if self._control != ControlState.AUTOMATION:
            # Never silently skipped: acting while the human has control is a programming error.
            raise ControlError(f"automation tried to {action.kind} while control is {self._control.value}")

        # Find first (read-only): the risk check needs to know which element this is.
        found = None
        if action.kind in TARGETED_ACTIONS:
            found = self.browser.find(list(action.strategies))
            if not found.found:
                return self._log(action, ActionOutcome("not_found", "no strategy matched exactly one element",
                                                       attempts=found.attempts, performed=False))

        decision = self.guard.check(self._proposed(action))
        if decision.verdict == "block":
            outcome = ActionOutcome("blocked", decision.reason, performed=False)
        elif decision.verdict == "needs_approval" and not self._ask_approval(action, decision.reason):
            outcome = ActionOutcome("rejected", "operator rejected: " + decision.reason, performed=False)
        else:
            outcome = self._execute(action, found.element if found else None)
        outcome.rule = decision.rule
        if found:
            outcome.strategy_used = found.element.description
            outcome.drifted = found.drifted
            outcome.attempts = found.attempts
        return self._log(action, outcome)

    def _proposed(self, action: Action) -> ProposedAction:
        role, name = action.declared_role_and_name
        return ProposedAction(
            action=action.kind,
            page_url=self.browser.current_url(),
            target_url=self.absolute(action.url) if action.url else None,
            target_role=role, target_name=name, step_risk=action.step_risk,
        )

    def _execute(self, action: Action, element) -> ActionOutcome:
        b = self.browser
        try:
            match action.kind:
                case "navigate":
                    b.goto(self.absolute(action.url))
                case "click":
                    b.click(element)
                    # Clicks return before the next page arrives. Settle so callers never act on
                    # (or read) the old page. A slow page is reported, not retried.
                    if not b.settle(self.settings.wait_timeout_s):
                        return ActionOutcome("done", "the next page is still loading", performed=True,
                                             settled=False)
                case "type":
                    b.type(element, action.value or "")
                case "select":
                    b.select(element, action.value or "")
                case "extract":
                    return ActionOutcome("done", text=b.read_text(element), performed=True)
                case "wait":
                    b.wait(action.seconds or 1)
        except ElementBlocked as e:
            return ActionOutcome("failed", str(e), performed=False)
        except BrowserError as e:
            return ActionOutcome("failed", str(e), performed=None)  # may or may not have happened
        return ActionOutcome("done", performed=True)

    def _ask_approval(self, action: Action, reason: str) -> bool:
        m = self.logger.masker
        role, name = action.declared_role_and_name
        request = ApprovalRequest(
            step=action.step,
            action_description=m.mask_text(f'{action.kind} {role or "element"} "{name or "?"}" '
                                           f"on {urlsplit(self.browser.current_url()).path}"),
            reason=reason,
            page_summary=m.mask_text(self.browser.visible_text(max_chars=600)),
        )
        previous = self._control
        self.set_control(ControlState.PAUSED, "waiting for operator approval", action.mode)
        try:
            approved = bool(self.approver(request))
        finally:
            self.set_control(previous, "approval answered", action.mode)
        self.logger.log(action.mode, "approved" if approved else "rejected", self._control, step=action.step,
                        action="approval", target=request.action_description, reason=reason)
        self.approvals.append(f"{'approved' if approved else 'rejected'}: {request.action_description}")
        return approved

    def _log(self, action: Action, outcome: ActionOutcome) -> ActionOutcome:
        warnings = [f"drift: backup locator {outcome.strategy_used} used"] if outcome.drifted else []
        data: dict = {}
        if action.kind in ("type", "select"):
            data["value"] = action.value  # masked by the logger (patterns + known values)
        if action.kind == "navigate":
            data["url"] = action.url
        if outcome.attempts and (outcome.drifted or not outcome.ok):
            data["attempts"] = outcome.attempts
        if outcome.rule and outcome.rule != "allowed":
            data["rule"] = outcome.rule
        reason = outcome.message if not outcome.ok and outcome.message else action.reason
        target = action.url if action.kind == "navigate" else outcome.strategy_used
        self.logger.log(action.mode, outcome.status, self._control, step=action.step, action=action.kind,
                        target=target, reason=reason, warnings=warnings, data=data)
        return outcome

    # ------------------------------------------------------------ helpers
    def absolute(self, url: str) -> str:
        return urljoin(self.settings.bank_base_url + "/", url)

    def login(self, username: str, password: str) -> None:
        """Sign in through the same gate as every other action. Values are never logged."""
        self.logger.masker.add_value(password)  # mask it anywhere it might appear
        steps = [
            Action("navigate", url="/login", mode="session", reason="open sign-on page"),
            Action("type", (RoleStrategy(by="role", role="textbox", name="User ID"),
                            LabelStrategy(by="label", text="User ID")), value=username, mode="session",
                   reason="enter user ID"),
            Action("type", (LabelStrategy(by="label", text="Password"),), value=password, mode="session",
                   reason="enter password"),
            Action("click", (RoleStrategy(by="role", role="button", name="Sign On"),), mode="session",
                   reason="sign on"),
        ]
        for action in steps:
            if not self.perform(action).ok:
                raise SessionError(f"login failed at '{action.reason}'")
        hit = self.browser.wait_for_any([WaitCondition(text="Member Search"),
                                         WaitCondition(text="Invalid user ID or password.")],
                                        self.settings.wait_timeout_s)
        if hit is None or hit.text != "Member Search":
            self.logger.log("session", "failed", self._control, action="login", reason="sign-on was rejected")
            raise SessionError("login failed: the bank rejected the credentials")
        self.logger.log("session", "ok", self._control, action="login", reason="signed on")


@contextmanager
def open_session(
    mode: LogMode,
    settings: Settings | None = None,
    policy: Policy | None = None,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    approver: Approver = reject_all,
    username: str | None = None,
    password: str | None = None,
    headless: bool | None = None,
    echo: bool = True,
    slow_mo_ms: int = 0,
) -> Iterator[Session]:
    """Open browser, start tracing, sign in, and hand over a ready session. Cleans up on exit.

    slow_mo_ms delays every browser action, so a person can follow along (demos only).

    The trace is kept only if the run failed (an exception left the block, or the caller
    set session.keep_trace = True), because traces are large and most runs succeed.
    """
    load_dotenv()
    settings = settings or load_settings()
    policy = policy or load_policy()
    username = username or os.environ.get("BANK_USERNAME")
    password = password or os.environ.get("BANK_PASSWORD")
    if not username or not password:
        raise SessionError("BANK_USERNAME and BANK_PASSWORD must be set in .env")

    folder = RunFolder(runs_dir=runs_dir)
    masker = Masker(policy.mask_fields, secrets=[username, password])
    logger = RunLogger(folder, masker, echo=echo)
    browser = Browser(headless=settings.headless if headless is None else headless, slow_mo_ms=slow_mo_ms)
    browser.start_trace()
    session = Session(browser, SafetyGuard(policy), logger, settings, approver)
    failed = False
    try:
        logger.log("session", "started", session.control, action="open", reason=f"{mode} run")
        session.login(username, password)
        yield session
    except BaseException:
        failed = True
        raise
    finally:
        try:
            browser.stop_trace(folder.trace_path() if failed or session.keep_trace else None)
        finally:
            browser.close()
