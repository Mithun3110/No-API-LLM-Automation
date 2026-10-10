"""Deterministic replay: run a recipe with given inputs. No LLM is ever imported or called here.

For each step:
  1. only_if    - skip the step if its condition is not on the page
  2. checks     - error handlers (business outcome / recoverable / hard failure)
  3. expect_page- are we on the page this step was recorded on?
  4. act        - through Session.perform (locators in order, safety gate, approval)
  5. wait_for   - wait for the proof that the step worked, watching error handlers meanwhile
  6. extract    - parse and keep declared outputs
Then the success check.

On a hard failure: first, if a recoverer was given (only `ask` does), ONE bounded LLM attempt
to complete just that step, then re-check the step's own success criteria. If that is not
possible or fails, and an operator is available, a human takes over the same live session;
on resume, replay continues from the step that matches the page the human left it on.

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

from src.handoff import Action, ActionOutcome, Operator, Session, take_over
from src.models import FailureInfo, Recipe, RunResult, RunStatus, Step
from src.models.recipe import (
    BusinessOutcomeHandler, ClickFix, HardFailureHandler, PageCondition, RecoverableHandler, WaitFix,
)
from src.models.values import ParseError, parse_value

from .inputs import fill, validate_inputs
from .recovery_api import RECOVERABLE_BY_LLM, RecoveryRequest, Recoverer

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
    def __init__(self, recipe: Recipe, session: Session, inputs: dict[str, str], llm_used_for: list[str] | None = None,
                 recoverer: Recoverer | None = None):
        self.recipe = recipe
        self.session = session
        self.browser = session.browser
        self.log = session.logger
        self.inputs = inputs
        self.timeout = session.settings.wait_timeout_s
        self.recoverer = recoverer
        self.outputs: dict[str, str | Decimal] = {}
        self.recoveries: list[str] = []
        self.warnings: list[str] = []
        self.llm_used_for = list(llm_used_for or [])  # set by the caller (e.g. matching); recovery may add to it
        self.llm_recovery_used = False
        self._recovery_tried: set[int] = set()            # once per step: a second attempt is a human's job
        self._attempts: dict[tuple[int, str], int] = {}  # (step, handler id) -> fixes applied
        self._evidence: list[str] = []                    # from the latest hard failure

    # ------------------------------------------------------------ run
    def run(self, operator: Operator | None = None) -> ReplayResult:
        stop = self._execute(1)
        while stop.status == RunStatus.FAILED:
            # 1. One bounded LLM attempt at the failed step (ask only), then carry on deterministically.
            if self._try_recovery(stop):
                stop = self._execute(stop.step + 1) if stop.step < len(self.recipe.steps) else self._finish_steps()
                continue
            # 2. Otherwise hand the same live session to a human, then resume where the page is.
            if operator is None or self.session.takeovers >= self.session.settings.max_takeovers:
                break
            step = self.recipe.steps[stop.step - 1] if stop.step else None
            takeover = take_over(self.session, operator, "replay", "hard_failure",
                                 f"{stop.error_type}: expected {stop.expected}; observed {stop.observed}",
                                 recipe_id=self.recipe.recipe_id, recipe_version=self.recipe.version,
                                 step=stop.step, step_description=step.description if step else None)
            if takeover.decision == "abort":
                stop = StepStop(RunStatus.ABORTED_BY_OPERATOR, stop.step, message="operator aborted the run")
                break
            resume_at = self._resume_point()
            if resume_at is None:
                stop = self._hard_stop(None, "a page that matches a recipe step",
                                       f'on "{self.browser.heading() or self.browser.current_url()}"', "no_resume_point")
                continue
            self.log.log("replay", "resumed", self.session.control, step=resume_at,
                         reason=f'continuing at step {resume_at} (page "{self.browser.heading()}")')
            stop = self._execute(resume_at)
        if stop.status == RunStatus.FAILED and self.session.takeovers:
            stop.status = RunStatus.ESCALATED  # a human had it and it still did not finish
        return self._finish(stop)

    def _finish_steps(self) -> StepStop:
        """After recovering the LAST step: only the success check is left."""
        try:
            self._success_check()
        except StopReplay as stop:
            if stop.result.status == RunStatus.FAILED:
                self._capture_evidence(stop.result)
            return stop.result
        return StepStop(RunStatus.SUCCESS, None)

    # ------------------------------------------------------------ bounded LLM recovery
    def _try_recovery(self, stop: StepStop) -> bool:
        if self.recoverer is None or stop.step is None or stop.step in self._recovery_tried:
            return False
        step = self.recipe.steps[stop.step - 1]
        if stop.error_type not in RECOVERABLE_BY_LLM or step.risk == "irreversible":
            # Known bad states need a human; a risky step is never handed to an LLM.
            return False
        self._recovery_tried.add(step.step)
        self.log.log("recovery", "started", self.session.control, step=step.step, action=step.action,
                     reason=f"{stop.error_type}: {stop.observed}")
        outcome = self.recoverer(RecoveryRequest(
            step=step, value=fill(step.value, self.inputs) if step.value is not None else None,
            allowed_values=tuple(self.inputs.values()), expected=stop.expected, observed=stop.observed,
            error_type=stop.error_type))
        self.llm_recovery_used = True
        verified, why = (self._verify_recovered(step, outcome.extracted) if outcome.success
                         else (False, outcome.detail))
        note = f"step {step.step}: LLM recovery {'succeeded' if verified else 'failed'} ({outcome.actions} action(s))"
        self.llm_used_for.append(f"recovering step {step.step} ({outcome.actions} action(s), "
                                 f"{'succeeded' if verified else 'failed'})")
        self.recoveries.append(note)
        self.log.log("recovery", "succeeded" if verified else "failed", self.session.control, step=step.step,
                     reason=why)
        if verified:
            # The recipe file is never changed automatically: flag it for a reviewer instead.
            self.warnings.append(f"step {step.step} needed LLM recovery: the recipe needs review")
        return verified

    def _verify_recovered(self, step: Step, extracted: str | None) -> tuple[bool, str]:
        """The LLM saying "done" is not proof. The step's own success criteria must hold."""
        if step.action == "extract":
            try:
                self._keep_output(step, extracted or "")
            except StopReplay:
                return False, f"could not read a {step.parse} value for {step.save_as}"
        if step.wait_for and self.browser.wait_for_any(step.wait_for.any_of, self.timeout) is None:
            return False, "the step's wait_for did not appear"
        return True, "the step's own checks passed"

    def _execute(self, start_at: int) -> StepStop:
        r = self.recipe
        self.log.log("replay", "started", self.session.control,
                     reason=f"{r.recipe_id}@{r.version} from step {start_at}",
                     data={"inputs": dict(self.inputs)})
        try:
            for step in r.steps[start_at - 1:]:
                self._run_step(step)
            self._success_check()
        except StopReplay as stop:
            if stop.result.status == RunStatus.FAILED:
                self._capture_evidence(stop.result)
            return stop.result
        return StepStop(RunStatus.SUCCESS, None)

    def _resume_point(self) -> int | None:
        """The step to continue at after a takeover, judged from the page the human left.

        The latest step whose expect_page matches, moved back to the first of the steps
        recorded on that same page: on a form page that means re-filling the form (type and
        select are repeatable) rather than clicking Continue on an empty form.
        """
        steps = self.recipe.steps
        for i in range(len(steps) - 1, -1, -1):
            page = steps[i].expect_page
            if page and self.browser.page_matches(page):
                while i > 0 and steps[i - 1].expect_page == page:
                    i -= 1
                return steps[i].step
        return None

    def _run_step(self, step: Step) -> None:
        n = step.step
        if step.only_if and not self.browser.page_matches(step.only_if):
            self.log.log("replay", "skipped", self.session.control, step=n, action=step.action,
                         reason=f"only_if {step.only_if.describe()} not on the page")
            return

        self._check_handlers(n, phase="before")
        if step.expect_page and not self._page(step.expect_page, EXPECT_PAGE_GRACE_S):
            self._check_handlers(n, phase="before")  # an error page may explain it
            raise self._hard(n, f"page with {step.expect_page.describe()} before this step",
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
                expected = " or ".join(c.describe() for c in step.wait_for.any_of)
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

    def _triggered(self, h: BusinessOutcomeHandler | HardFailureHandler | RecoverableHandler) -> bool:
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
        for cond in (PageCondition(heading=check.heading) if check.heading else None,
                     PageCondition(text_visible=check.text_visible) if check.text_visible else None):
            if cond and not self._page(cond, EXPECT_PAGE_GRACE_S):
                raise self._hard(None, f"{cond.describe()} on the final page",
                                 f'on "{self.browser.heading()}"', "success_check")

    def _page(self, condition: PageCondition, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while not self.browser.page_matches(condition):
            if time.monotonic() >= deadline:
                return False
            self.browser.wait(0.2)
        return True

    # ------------------------------------------------------------ results
    def _hard(self, step: int | None, expected: str, observed: str, error_type: str) -> StopReplay:
        return StopReplay(self._hard_stop(step, expected, observed, error_type))

    def _hard_stop(self, step: int | None, expected: str, observed: str, error_type: str) -> StepStop:
        return StepStop(RunStatus.FAILED, step, expected=expected, observed=observed,
                        error_type=error_type, message=f"hard failure: {error_type}")

    def _capture_evidence(self, stop: StepStop) -> None:
        """Richer evidence for debugging: what the page looked like, and the full trace."""
        folder = self.log.folder
        shot = self.browser.screenshot(folder.screenshot_path(f"step{stop.step}_failure"))
        tree = folder.snapshot_path(f"step{stop.step}_failure")
        tree.write_text(self.log.masker.mask_text(self.browser.snapshot()))
        self.session.keep_trace = True
        self._evidence = [str(shot), str(tree), str(folder.trace_path())]

    def _finish(self, stop: StepStop) -> ReplayResult:
        folder = self.log.folder
        common = dict(run_id=self.log.run_id, mode="replay", recipe_id=self.recipe.recipe_id,
                      recipe_version=self.recipe.version, recoveries=self.recoveries, warnings=self.warnings,
                      approvals=list(self.session.approvals),
                      human_interventions=list(self.session.human_interventions), log_file=str(folder.log_path),
                      answered_by="recipe", llm_used_for=self.llm_used_for,
                      llm_recovery_used=self.llm_recovery_used)
        failure = None
        if stop.status in (RunStatus.FAILED, RunStatus.ESCALATED):
            if not self._evidence:
                self._capture_evidence(stop)
            failure = FailureInfo(step=stop.step, expected=stop.expected, observed=stop.observed,
                                  error_type=stop.error_type, evidence=self._evidence)
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


def replay(recipe: Recipe, raw_inputs: dict[str, str], session: Session,
           operator: Operator | None = None, llm_used_for: list[str] | None = None,
           recoverer: Recoverer | None = None) -> ReplayResult:
    """Validate inputs, then run the recipe on the live session. INVALID_INPUT never touches the page.

    recoverer (only `ask` passes one): an unexpected step failure first gets one bounded LLM attempt.
    operator: a hard failure is then handed to a human on the same session; without one, it is
    returned as FAILED.
    """
    values, errors = validate_inputs(recipe, raw_inputs)
    if errors:
        result = RunResult(status=RunStatus.INVALID_INPUT, run_id=session.logger.run_id, mode="replay",
                           recipe_id=recipe.recipe_id, recipe_version=recipe.version, message="; ".join(errors),
                           log_file=str(session.logger.folder.log_path), answered_by="recipe",
                           llm_used_for=list(llm_used_for or []))
        session.logger.log("replay", "INVALID_INPUT", session.control, reason=result.message)
        session.logger.write_result(result)
        return ReplayResult(result)
    for name, spec in recipe.inputs.items():
        if spec.sensitive and name in values:
            session.logger.masker.add_value(values[name], field=name)
    return Replayer(recipe, session, values, llm_used_for, recoverer).run(operator)
