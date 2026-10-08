"""Deterministic replay: run a recipe with given inputs. No LLM is ever imported or called here.

For each step:
  1. only_if    - skip the step if its condition is not on the page
  2. checks     - error handlers (business outcome / recoverable / hard failure)
  3. expect_page- are we on the page this step was recorded on?
  4. act        - through Session.perform (locators in order, safety gate, approval)
  5. wait_for   - wait for the proof that the step worked, watching error handlers meanwhile
  6. extract    - parse and keep declared outputs
Then the success check.

Three kinds of result, kept apart on purpose:
- business outcome: a valid answer (e.g. MEMBER_NOT_FOUND). Stop and return the code.
- recoverable: a known interruption (popup, slow page). Fix it, bounded by max_attempts.
- hard failure: anything else, including anything unknown. Stop with step, expected,
  observed and evidence (screenshot, accessibility snapshot, trace).

Retry rule: never redo an action that may have changed data.
- slow page: keep waiting for wait_for; do not redo the action
- popup before an action (the click was blocked, so it never happened): dismiss, then act
- popup after an action: dismiss, then keep waiting; do not redo the action
- only navigate, type and extract (repeatable) may be fully re-run
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal

from src.handoff import Action, ActionOutcome, Session
from src.models import FailureInfo, Recipe, RunResult, RunStatus, Step
from src.models.recipe import (
    BusinessOutcomeHandler, ClickFix, HardFailureHandler, RecoverableHandler, WaitCondition, WaitFix,
)
from src.models.values import ParseError, parse_value

from .inputs import fill, validate_inputs

EXPECT_PAGE_GRACE_S = 2  # short tolerance for a page that is just finishing rendering
POLL_S = 0.5             # how often errors and popups are checked while waiting


class StopReplay(Exception):
    """Internal: the run ends here with this result (business outcome, failure, rejection)."""

    def __init__(self, result: "StepStop"):
        self.result = result


@dataclass
class StepStop:
    status: RunStatus
    step: int | None
    outcome_code: str | None = None
    expected: str = ""
    observed: str = ""
    error_type: str = ""
    message: str = ""


@dataclass
class ReplayResult:
    run_result: RunResult
    failed_step: int | None = None   # where a hard failure happened (for escalation and resume)
    outputs: dict[str, str | Decimal] = field(default_factory=dict)

    @property
    def status(self) -> RunStatus:
        return self.run_result.status


class Replayer:
    def __init__(self, recipe: Recipe, session: Session, inputs: dict[str, str]):
        self.recipe = recipe
        self.session = session
        self.browser = session.browser
        self.log = session.logger
        self.inputs = inputs
        self.timeout = session.settings.wait_timeout_s
        self.outputs: dict[str, str | Decimal] = {}
        self.recoveries: list[str] = []
        self.warnings: list[str] = []
        self._attempts: dict[tuple[int, str], int] = {}  # (step, handler id) -> fixes applied

    # ------------------------------------------------------------ run
    def run(self, start_at: int = 1) -> ReplayResult:
        r = self.recipe
        self.log.log("replay", "started", self.session.control,
                     reason=f"{r.recipe_id}@{r.version} from step {start_at}",
                     data={"inputs": dict(self.inputs)})
        try:
            for step in r.steps[start_at - 1:]:
                self._run_step(step)
            self._success_check()
        except StopReplay as stop:
            return self._finish(stop.result)
        return self._finish(StepStop(RunStatus.SUCCESS, None))

    def _run_step(self, step: Step) -> None:
        n = step.step
        if step.only_if and not self.browser.text_visible(step.only_if.text_visible):
            self.log.log("replay", "skipped", self.session.control, step=n, action=step.action,
                         reason=f'only_if "{step.only_if.text_visible}" not on the page')
            return

        self._check_handlers(n, phase="before")
        if step.expect_page and not self._visible(step.expect_page.text_visible, EXPECT_PAGE_GRACE_S):
            self._check_handlers(n, phase="before")  # an error page may explain it
            raise self._hard(n, f'page "{step.expect_page.text_visible}" before this step',
                             f'on "{self.browser.heading() or self.browser.current_url()}"', "wrong_page")

        started = time.monotonic()  # the wait budget counts from the action, not after it
        outcome = self._act(step)
        if outcome.drifted:
            self.warnings.append(f"step {n}: backup locator used ({outcome.strategy_used})")

        if step.action == "extract":
            self._keep_output(step, outcome.text or "")
        if step.wait_for:
            self._wait_for(step, started)
        else:
            self._check_handlers(n, phase="after")

    # ------------------------------------------------------------ acting, with the retry rule
    def _act(self, step: Step) -> ActionOutcome:
        n = step.step
        while True:
            outcome = self.session.perform(self._action(step))
            if outcome.ok:
                return outcome
            if outcome.status == "rejected":
                raise StopReplay(StepStop(RunStatus.REJECTED_BY_OPERATOR, n, message=outcome.message,
                                          error_type="rejected"))
            if outcome.status == "blocked":
                raise self._hard(n, f"{step.action} allowed by policy", outcome.message, "policy_blocked")
            # Not found or failed: a known interruption (e.g. a popup) may be the cause.
            fixed = self._check_handlers(n, phase="before")
            safe_to_redo = outcome.performed is False or step.is_repeatable
            if fixed and safe_to_redo:
                continue  # e.g. popup blocked the click, so the click never happened: do it now
            expected = f"{step.action}: {step.description}"
            observed = outcome.message + (f" [{'; '.join(outcome.attempts)}]" if outcome.attempts else "")
            raise self._hard(n, expected, observed, "element_not_found" if outcome.status == "not_found"
                             else "action_failed")

    def _action(self, step: Step) -> Action:
        strategies = tuple(step.target.strategies) if step.target else ()
        return Action(
            step.action, strategies,
            url=fill(step.url, self.inputs) if step.url else None,
            value=fill(step.value, self.inputs) if step.value is not None else None,
            step=step.step, step_risk=step.risk, reason=step.description, mode="replay",
        )

    # ------------------------------------------------------------ waiting
    def _wait_for(self, step: Step, started: float) -> None:
        """Wait for proof the step worked, while watching for errors. Never redoes the action."""
        n = step.step
        deadline = started + self.timeout
        while True:
            if self.browser.wait_for_any(step.wait_for.any_of, POLL_S) is not None:
                return
            if self._check_handlers(n, phase="after"):
                continue  # popup AFTER the action: dismissed, keep waiting; the action is not redone
            if time.monotonic() < deadline:
                continue
            # Timed out: a recoverable "wait_timed_out" handler (slow page) may allow more time.
            if not self._apply_timeout_handler(n):
                expected = " or ".join(f'"{c.text or c.url_contains}"' for c in step.wait_for.any_of)
                raise self._hard(n, f"{expected} after {step.action}",
                                 f'still on "{self.browser.heading() or self.browser.current_url()}"',
                                 "wait_timeout")
            deadline = time.monotonic()  # after each extra wait, check once, then the next fix

    def _apply_timeout_handler(self, n: int) -> bool:
        for h in self._active(n, "after"):
            if isinstance(h, RecoverableHandler) and h.when.wait_timed_out:
                return self._apply_fix(n, h)
        return False

    # ------------------------------------------------------------ error handlers
    def _check_handlers(self, n: int, phase: str) -> bool:
        """Check the page against error handlers. Returns True if a recoverable fix was applied.

        Order: business outcomes first (they are the answer), then hard failures, then
        recoverable conditions. Raises StopReplay for the first two.
        """
        handlers = self._active(n, phase)
        for h in handlers:
            if isinstance(h, BusinessOutcomeHandler) and self._triggered(h):
                self.log.log("replay", "business_outcome", self.session.control, step=n, reason=h.description,
                             data={"handler": h.id, "result": h.result})
                raise StopReplay(StepStop(RunStatus.BUSINESS_OUTCOME, n, outcome_code=h.result,
                                          message=self.recipe.outcomes.get(h.result, h.description)))
        for h in handlers:
            if isinstance(h, HardFailureHandler) and self._triggered(h):
                raise self._hard(n, "no error page", f'"{h.when.text or h.when.dialog}" is shown', h.id)
        for h in handlers:
            if isinstance(h, RecoverableHandler) and not h.when.wait_timed_out and self._triggered(h):
                return self._apply_fix(n, h)
        return False

    def _active(self, n: int, phase: str) -> list:
        """any_step handlers always; after_step:N while finishing step N or about to start N+1.

        Returns a list, not a generator: callers loop over it more than once.
        """
        active = []
        for h in self.recipe.error_handlers:
            if h.scope == "any_step":
                active.append(h)
            else:
                k = int(h.scope.split(":")[1])
                if (phase == "after" and n == k) or (phase == "before" and n == k + 1):
                    active.append(h)
        return active

    def _triggered(self, h) -> bool:
        if h.when.text is not None:
            return self.browser.text_visible(h.when.text)
        if h.when.dialog is not None:
            return self.browser.dialog_open(h.when.dialog)
        return False  # wait_timed_out is handled by _wait_for

    def _apply_fix(self, n: int, h: RecoverableHandler) -> bool:
        key = (n, h.id)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        if self._attempts[key] > h.max_attempts:
            raise self._hard(n, f"'{h.id}' fixed within {h.max_attempts} attempts",
                             f"still happening after {h.max_attempts} fixes", h.id)
        if isinstance(h.fix, ClickFix):
            outcome = self.session.perform(Action("click", tuple(h.fix.target.strategies), step=n,
                                                  reason=f"recover: {h.description}", mode="replay"))
            if not outcome.ok:
                raise self._hard(n, f"fix for '{h.id}' to work", outcome.message, h.id)
        elif isinstance(h.fix, WaitFix):
            self.session.perform(Action("wait", seconds=h.fix.seconds, step=n,
                                        reason=f"recover: {h.description}", mode="replay"))
        note = f"step {n}: {h.id} (attempt {self._attempts[key]})"
        self.recoveries.append(note)
        self.log.log("replay", "recovered", self.session.control, step=n, action="fix", reason=h.description,
                     data={"handler": h.id, "attempt": self._attempts[key]})
        return True

    # ------------------------------------------------------------ outputs and success
    def _keep_output(self, step: Step, text: str) -> None:
        try:
            value = parse_value(text, step.parse)
        except ParseError as e:
            raise self._hard(step.step, f"a {step.parse} value for {step.save_as}", str(e), "parse_error")
        self.outputs[step.save_as] = value
        if self.recipe.outputs[step.save_as].sensitive:
            self.log.masker.add_value(text, field=step.save_as)

    def _success_check(self) -> None:
        check = self.recipe.success_check
        last = self.recipe.steps[-1].step
        self._check_handlers(last, phase="after")
        missing = [o for o in check.outputs_present if o not in self.outputs]
        if missing:
            raise self._hard(None, f"outputs {', '.join(check.outputs_present)}",
                             f"missing {', '.join(missing)}", "success_check")
        if check.text_visible and not self._visible(check.text_visible, EXPECT_PAGE_GRACE_S):
            raise self._hard(None, f'"{check.text_visible}" on the final page',
                             f'on "{self.browser.heading()}"', "success_check")

    def _visible(self, text: str, timeout_s: float) -> bool:
        return self.browser.wait_for_any([WaitCondition(text=text)], timeout_s) is not None

    # ------------------------------------------------------------ results
    def _hard(self, step: int | None, expected: str, observed: str, error_type: str) -> StopReplay:
        return StopReplay(StepStop(RunStatus.FAILED, step, expected=expected, observed=observed,
                                   error_type=error_type, message=f"hard failure: {error_type}"))

    def _finish(self, stop: StepStop) -> ReplayResult:
        folder = self.log.folder
        common = dict(run_id=self.log.run_id, mode="replay", recipe_id=self.recipe.recipe_id,
                      recipe_version=self.recipe.version, recoveries=self.recoveries, warnings=self.warnings,
                      approvals=list(self.session.approvals), log_file=str(folder.log_path))
        failure = None
        if stop.status == RunStatus.FAILED:
            # Richer evidence for debugging: what the page looked like and a full trace.
            shot = self.browser.screenshot(folder.screenshot_path(f"step{stop.step}_failure"))
            tree = folder.snapshot_path(f"step{stop.step}_failure")
            tree.write_text(self.log.masker.mask_text(self.browser.snapshot()))
            self.session.keep_trace = True
            failure = FailureInfo(step=stop.step, expected=stop.expected, observed=stop.observed,
                                  error_type=stop.error_type, evidence=[str(shot), str(tree), str(folder.trace_path())])
        result = RunResult(
            status=stop.status, outcome_code=stop.outcome_code, failure=failure,
            outputs=self.outputs if stop.status == RunStatus.SUCCESS else {},
            message=stop.message or ("all steps done and success check passed"
                                     if stop.status == RunStatus.SUCCESS else None),
            **common,
        )
        self.log.log("replay", stop.status.value, self.session.control, step=stop.step,
                     reason=stop.observed or stop.message,
                     warnings=self.warnings, data={"outcome_code": stop.outcome_code, "recoveries": self.recoveries})
        self.log.write_result(result)
        return ReplayResult(result, failed_step=stop.step if failure else None, outputs=dict(result.outputs))


def replay(recipe: Recipe, raw_inputs: dict[str, str], session: Session, start_at: int = 1) -> ReplayResult:
    """Validate inputs, then run the recipe on the live session. INVALID_INPUT never touches the page."""
    values, errors = validate_inputs(recipe, raw_inputs)
    if errors:
        result = RunResult(status=RunStatus.INVALID_INPUT, run_id=session.logger.run_id, mode="replay",
                           recipe_id=recipe.recipe_id, recipe_version=recipe.version, message="; ".join(errors),
                           log_file=str(session.logger.folder.log_path))
        session.logger.log("replay", "INVALID_INPUT", session.control, reason=result.message)
        session.logger.write_result(result)
        return ReplayResult(result)
    for name, spec in recipe.inputs.items():
        if spec.sensitive and name in values:
            session.logger.masker.add_value(values[name], field=name)
    return Replayer(recipe, session, values).run(start_at)
