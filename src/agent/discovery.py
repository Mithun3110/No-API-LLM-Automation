"""The discovery agent: the LLM observes the page, decides one action, the session acts.

    define task -> loop: observe (tree) -> decide (one tool call) -> act (safety gate) -> log

Stops on: done (goal met or not achievable), operator rejected a risky action, stuck
(same action failed twice, a click changed nothing, the LLM asked for a human, step limit,
timeout), or an LLM error. Being stuck leads to human takeover in step 9; for now it stops
with a clear reason and evidence.

Successful steps are kept in order for the recorder (step 7). Failed attempts are logged
but never become part of a recipe.
"""

import hashlib
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from src.handoff import Action, Session
from src.models import FailureInfo, RunResult, RunStatus
from src.models.intervention import StopReason
from src.models.recipe import Strategy
from src.models.values import ParseError, parse_value

from . import prompts
from .llm import LLMClient, LLMError
from .tools import ACTION_TOOLS, TASK_TOOLS, AskHuman, DefineTask, Done, Extract, tool_specs, to_action

HISTORY_SHOWN = 12  # most recent actions included in each prompt

Outcome = Literal["success", "not_achievable", "rejected", "stuck", "llm_error"]


@dataclass
class AgentStep:
    """One action the agent took, successful or not."""
    number: int
    tool: str
    args: dict
    status: str              # done, blocked, rejected, not_found, failed, invalid, unchanged
    message: str = ""
    url_after: str = ""
    heading_before: str = ""
    heading_after: str = ""
    extracted: str | None = None   # extract: raw page text
    # Captured BEFORE the action, while the element is still on the page (for the recorder):
    locators: list[Strategy] = field(default_factory=list)  # verified, in preference order
    in_dialog: str | None = None   # name of the dialog the element was in, e.g. "Notice"
    value: str | None = None       # what was typed/selected, or the navigate path

    def history_line(self) -> str:
        args = {k: v for k, v in self.args.items() if k != "reason"}
        line = f"{self.number}. {self.tool} {args} -> {self.status}"
        if self.message:
            line += f": {self.message}"
        if self.status == "done" and self.heading_after:
            line += f' (now on "{self.heading_after}")'
        return line


@dataclass
class DiscoveryResult:
    outcome: Outcome
    goal: str
    task: DefineTask | None
    steps: list[AgentStep] = field(default_factory=list)
    outputs: dict[str, str | Decimal] = field(default_factory=dict)
    stop_reason: StopReason | None = None
    detail: str = ""
    run_result: RunResult | None = None

    @property
    def successful_steps(self) -> list[AgentStep]:
        """What the recorder turns into recipe steps: only actions that worked."""
        return [s for s in self.steps if s.status == "done" and s.tool not in ("done", "wait")]


class Discovery:
    def __init__(self, session: Session, llm: LLMClient, goal: str):
        self.session = session
        self.llm = llm
        self.goal = goal
        self.settings = session.settings
        self.task: DefineTask | None = None
        self.steps: list[AgentStep] = []
        self.outputs: dict[str, str | Decimal] = {}
        self._failures: dict[str, int] = {}  # action signature -> times it failed

    # ------------------------------------------------------------ main loop
    def run(self) -> DiscoveryResult:
        log = self.session.logger
        log.log("discover", "started", self.session.control, reason=f"goal: {self.goal}",
                data={"model": self.llm.model})
        try:
            self.task = self._define_task()
        except (LLMError, ValueError) as e:
            return self._finish("llm_error", detail=f"could not define the task: {e}")

        deadline = time.monotonic() + self.settings.discovery_timeout_s
        for number in range(1, self.settings.step_limit + 1):
            if time.monotonic() > deadline:
                return self._finish("stuck", "timeout", f"no result after {self.settings.discovery_timeout_s:.0f}s")
            try:
                call = self.llm.decide(prompts.SYSTEM, self._step_prompt(), tool_specs(ACTION_TOOLS))
            except LLMError as e:
                return self._finish("llm_error", detail=str(e))

            finished = self._handle(number, call.name, call.arguments)
            if finished:
                return finished
        return self._finish("stuck", "step_limit", f"goal not reached in {self.settings.step_limit} steps")

    def _define_task(self) -> DefineTask:
        call = self.llm.decide(prompts.SYSTEM, prompts.TASK_PROMPT.format(goal=self.goal), tool_specs(TASK_TOOLS))
        if call.name != "define_task":
            raise ValueError(f"expected define_task, got {call.name}")
        task = DefineTask.model_validate(call.arguments)
        # Inputs must come from the goal. A value the LLM made up would become a wrong default.
        for i in task.inputs:
            if i.value.lower() not in self.goal.lower():
                raise ValueError(f"input '{i.name}' value is not in the goal")
            if i.sensitive:
                self.session.logger.masker.add_value(i.value, field=i.name)
        self.session.logger.log(
            "discover", "task_defined", self.session.control, action="define_task", reason=task.description,
            data={"recipe_id": task.recipe_id, "inputs": {i.name: i.value for i in task.inputs},
                  "outputs": [o.name for o in task.outputs]})
        return task

    # ------------------------------------------------------------ one turn
    def _handle(self, number: int, tool: str, arguments: dict) -> DiscoveryResult | None:
        model = ACTION_TOOLS.get(tool)
        if model is None:
            return self._record_failure(number, tool, arguments, "invalid", f"unknown tool '{tool}'")
        try:
            args = model.model_validate(arguments)
        except ValidationError as e:
            return self._record_failure(number, tool, arguments, "invalid", _short(e))

        if isinstance(args, AskHuman):
            self._add(AgentStep(number, tool, arguments, "done"))
            return self._finish("stuck", "agent_asked", args.reason)
        if isinstance(args, Done):
            return self._handle_done(number, tool, arguments, args)
        if isinstance(args, Extract) and args.output not in self._output_names():
            return self._record_failure(number, tool, arguments, "invalid",
                                        f"'{args.output}' is not a declared output")

        action = to_action(args, step=number)
        browser = self.session.browser
        before = self._fingerprint()
        heading_before = browser.heading()
        locators, in_dialog = self._capture_locators(action)
        outcome = self.session.perform(action)
        step = AgentStep(number, tool, arguments, outcome.status, outcome.message,
                         url_after=browser.current_url(), heading_before=heading_before,
                         heading_after=browser.heading(), locators=locators, in_dialog=in_dialog,
                         value=action.value if action.value is not None else action.url)

        if outcome.ok and not outcome.settled:
            step.message = "the next page is still loading"  # the model sees this and can wait
        if outcome.status == "rejected":
            self._add(step)
            return self._finish("rejected", detail=outcome.message)
        if not outcome.ok:
            return self._record_failure(number, tool, arguments, outcome.status, outcome.message, step)

        if isinstance(args, Extract):
            try:
                value = parse_value(outcome.text or "", args.parse)
            except ParseError as e:
                return self._record_failure(number, tool, arguments, "failed", str(e), step)
            step.extracted = outcome.text
            self.outputs[args.output] = value
            if self._output(args.output).sensitive:
                self.session.logger.masker.add_value(outcome.text, field=args.output)

        # A click that changes nothing means the agent is not making progress.
        if tool == "click" and self._fingerprint() == before:
            step.status, step.message = "unchanged", "the page did not change"
            self._add(step)
            return self._finish("stuck", "page_unchanged", f'clicking "{args.name}" changed nothing')

        self._add(step)
        return None

    def _handle_done(self, number: int, tool: str, arguments: dict, args: Done) -> DiscoveryResult | None:
        if not args.success:
            self._add(AgentStep(number, tool, arguments, "done", args.summary))
            return self._finish("not_achievable", detail=args.summary)
        missing = self._missing_outputs()
        if missing:
            return self._record_failure(number, tool, arguments, "invalid",
                                        f"outputs not extracted yet: {', '.join(missing)}")
        self._add(AgentStep(number, tool, arguments, "done", args.summary))
        return self._finish("success", detail=args.summary)

    def _record_failure(self, number, tool, arguments, status, message, step: AgentStep | None = None):
        """Log a failed attempt; the same action failing twice means the agent is stuck."""
        step = step or AgentStep(number, tool, arguments, status, message)
        step.status, step.message = status, message
        self._add(step)
        signature = f"{tool}:{sorted((k, str(v)) for k, v in arguments.items() if k != 'reason')}"
        self._failures[signature] = self._failures.get(signature, 0) + 1
        if self._failures[signature] >= 2:
            return self._finish("stuck", "repeated_failure", f"{tool} failed twice: {message}")
        return None

    # ------------------------------------------------------------ helpers
    def _capture_locators(self, action: Action) -> tuple[list[Strategy], str | None]:
        """Read-only look at the target before acting: backup locators and dialog membership."""
        if not action.strategies:
            return [], None
        b = self.session.browser
        found = b.find(list(action.strategies))
        if not found.found:
            return [], None  # perform() will report not_found
        return b.locators_for(found.element, action.strategies[0]), b.element_facts(found.element)["dialog"]

    def _add(self, step: AgentStep) -> None:
        self.steps.append(step)
        if step.status in ("invalid", "unchanged"):
            # perform() already logged real actions; these never reached the browser
            self.session.logger.log("discover", step.status, self.session.control, step=step.number,
                                    action=step.tool, reason=step.message)

    def _step_prompt(self) -> str:
        b = self.session.browser
        tree = b.compact_snapshot()
        if len(tree) > self.settings.max_tree_chars:
            tree = tree[: self.settings.max_tree_chars] + "\n... (page tree truncated)"
        history = "\n".join(s.history_line() for s in self.steps[-HISTORY_SHOWN:]) or "(nothing yet)"
        task_line = (f"{self.task.recipe_id}: inputs "
                     + (", ".join(f"{i.name}={i.value}" for i in self.task.inputs) or "none"))
        return prompts.STEP_PROMPT.format(goal=self.goal, task_line=task_line,
                                          missing=", ".join(self._missing_outputs()) or "none (call done)",
                                          history=history, path=b.current_url(), tree=tree)

    def _fingerprint(self) -> str:
        b = self.session.browser
        return hashlib.sha256((b.current_url() + b.compact_snapshot()).encode()).hexdigest()

    def _output_names(self) -> list[str]:
        return [o.name for o in self.task.outputs]

    def _output(self, name: str):
        return next(o for o in self.task.outputs if o.name == name)

    def _missing_outputs(self) -> list[str]:
        return [n for n in self._output_names() if n not in self.outputs]

    def _finish(self, outcome: Outcome, stop_reason: StopReason | None = None, detail: str = "") -> DiscoveryResult:
        s, log = self.session, self.session.logger
        result = DiscoveryResult(outcome, self.goal, self.task, self.steps, self.outputs, stop_reason, detail)
        evidence = []
        if outcome != "success":
            evidence.append(str(s.browser.screenshot(log.folder.screenshot_path(outcome))))
            s.keep_trace = True  # anything but success keeps the trace for debugging
        result.run_result = self._run_result(result, evidence)
        log.log("discover", outcome, s.control, reason=detail,
                data={"stop_reason": stop_reason, "steps": len(self.steps)})
        log.write_result(result.run_result)
        return result

    def _run_result(self, r: DiscoveryResult, evidence: list[str]) -> RunResult:
        common = dict(run_id=self.session.logger.run_id, mode="discover",
                      recipe_id=r.task.recipe_id if r.task else None,
                      log_file=str(self.session.logger.folder.log_path))
        if r.outcome == "success":
            return RunResult(status=RunStatus.SUCCESS, outputs=r.outputs, message=r.detail, **common)
        if r.outcome == "rejected":
            return RunResult(status=RunStatus.REJECTED_BY_OPERATOR, message=r.detail, **common)
        last = self.steps[-1].number if self.steps else None
        return RunResult(status=RunStatus.FAILED, message=r.detail, **common, failure=FailureInfo(
            step=last, expected="the goal to be reached", observed=r.detail,
            error_type=r.stop_reason or r.outcome, evidence=evidence))


def run_discovery(session: Session, llm: LLMClient, goal: str) -> DiscoveryResult:
    """Open the entry page and let the agent work towards the goal."""
    session.perform(Action("navigate", url=session.settings.entry_path, mode="discover", reason="open entry page"))
    return Discovery(session, llm, goal).run()


def _short(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(p) for p in first["loc"]) or "arguments"
    return f"invalid {where}: {first['msg']}"
