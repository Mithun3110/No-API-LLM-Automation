"""The discovery agent: the LLM observes the page, decides one action, the session acts.

    define task -> loop: observe (tree) -> decide (one tool call) -> act (safety gate) -> log

Stops on: done (goal met or not achievable), operator rejected a risky action, stuck
(same action failed twice, a click changed nothing, the LLM asked for a human, step limit,
timeout), or an LLM error. When stuck and an operator is available, a human takes over the
same session; otherwise the run stops with the reason and evidence.

Successful steps are kept in order for the recorder. Failed attempts are logged but never
become part of a recipe.
"""

import hashlib
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from src.handoff import Action, HumanAction, Operator, Session, TakeoverResult, take_over
from src.models import FailureInfo, RunResult, RunStatus
from src.models.intervention import StopReason
from src.models.recipe import CssStrategy, LabelStrategy, RoleStrategy, Strategy
from src.models.values import ParseError, parse_value

from . import prompts
from .llm import LLMClient, LLMError
from .tools import (
    ACTION_TOOLS, TASK_TOOLS, AskHuman, DefineTask, Done, Extract, InputDef, OutputDef, tool_specs, to_action,
)

HISTORY_SHOWN = 12  # most recent actions included in each prompt
TASK_ATTEMPTS = 3   # define_task attempts, each told why the previous one was rejected
HUMAN_TOOLS = {"click": "click", "type": "type_text", "select": "select_option"}  # recorder kind -> tool

Outcome = Literal["success", "not_achievable", "rejected", "stuck", "aborted", "llm_error"]


@dataclass
class Stop:
    """Why the loop stopped. run() decides: hand over to a human (stuck) or finish."""
    outcome: Outcome
    stop_reason: StopReason | None = None
    detail: str = ""


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
    source: str = "agent"          # agent, or human (done by the operator during a takeover)

    def history_line(self) -> str:
        args = {k: v for k, v in self.args.items() if k != "reason"}
        who = "HUMAN OPERATOR: " if self.source == "human" else ""
        line = f"{self.number}. {who}{self.tool} {args} -> {self.status}"
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
    auto_inputs: list[str] = field(default_factory=list)  # declared by discovery, not the model

    @property
    def successful_steps(self) -> list[AgentStep]:
        """What the recorder turns into recipe steps: only actions that worked."""
        return [s for s in self.steps if s.status == "done" and s.tool not in ("done", "wait", "ask_human")]


class Discovery:
    def __init__(self, session: Session, llm: LLMClient, goal: str, operator: Operator | None = None):
        self.session = session
        self.operator = operator
        self.llm = llm
        self.goal = goal
        self.settings = session.settings
        self.task: DefineTask | None = None
        self.steps: list[AgentStep] = []
        self.outputs: dict[str, str | Decimal] = {}
        self._failures: dict[str, int] = {}  # action signature -> times it failed
        self.auto_inputs: list[str] = []      # inputs the model forgot and discovery declared

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
        number, budget = 0, self.settings.step_limit
        while True:
            stop = None
            if budget == 0:
                stop = Stop("stuck", "step_limit", f"goal not reached in {self.settings.step_limit} steps")
            elif time.monotonic() > deadline:
                stop = Stop("stuck", "timeout", f"no result after {self.settings.discovery_timeout_s:.0f}s")
            else:
                number, budget = number + 1, budget - 1
                try:
                    call = self.llm.decide(prompts.SYSTEM, self._step_prompt(), tool_specs(ACTION_TOOLS))
                except LLMError as e:
                    return self._finish("llm_error", detail=str(e))
                stop = self._handle(number, call.name, call.arguments)
            if stop is None:
                continue
            if stop.outcome == "stuck" and self._can_hand_over():
                if self._hand_over(stop) == "abort":
                    return self._finish("aborted", stop.stop_reason, "operator aborted the run")
                number = self.steps[-1].number if self.steps else number
                budget = self.settings.step_limit  # the agent gets a fresh budget after a human helped
                deadline = time.monotonic() + self.settings.discovery_timeout_s
                continue
            return self._finish(stop.outcome, stop.stop_reason, stop.detail)

    # ------------------------------------------------------------ human takeover
    def _can_hand_over(self) -> bool:
        return self.operator is not None and self.session.takeovers < self.settings.max_takeovers

    def _hand_over(self, stop: Stop) -> str:
        last = self.steps[-1] if self.steps else None
        result = take_over(self.session, self.operator, "discover", stop.stop_reason, stop.detail,
                           goal=self.goal, step=last.number if last else None,
                           step_description=last.args.get("reason") if last else None)
        if result.decision == "resume":
            self._add_human_steps(result)
            self._failures.clear()  # the human changed the situation: old failures no longer count
        return result.decision

    def _add_human_steps(self, result: TakeoverResult) -> None:
        """What the human did becomes part of the run (and so of the recipe), marked source=human."""
        actions = result.actions
        for i, a in enumerate(actions):
            tool = HUMAN_TOOLS.get(a.kind, "click")
            if tool == "click":
                args = {"role": a.role, "name": a.name}
            elif tool == "type_text":
                args = {"name": a.name, "text": a.value}
            else:
                args = {"name": a.name, "option": a.value}
            args["reason"] = "done by the human operator"
            heading_after = actions[i + 1].heading if i + 1 < len(actions) else result.heading_after
            number = (self.steps[-1].number if self.steps else 0) + 1
            self._add(AgentStep(number, tool, args, "done", heading_before=a.heading,
                                heading_after=heading_after, locators=human_locators(a), in_dialog=a.dialog,
                                value=a.value, source="human"))

    def _define_task(self) -> DefineTask:
        """Ask for the task definition. A rejected definition is sent back WITH the reason, so the
        model can correct it: resending the same prompt tends to repeat the same mistake."""
        prompt = prompts.TASK_PROMPT.format(goal=self.goal)
        for attempt in range(1, TASK_ATTEMPTS):
            try:
                return self._validated_task(self.llm.decide(prompts.SYSTEM, prompt, tool_specs(TASK_TOOLS)))
            except (LLMError, ValueError) as e:  # pydantic's ValidationError is a ValueError
                self.session.logger.log("discover", "task_rejected", self.session.control, action="define_task",
                                        reason=str(e)[:300], data={"attempt": attempt})
                prompt = (prompts.TASK_PROMPT.format(goal=self.goal)
                          + f"\n\nYour previous define_task call was rejected: {str(e)[:300]}\nFix it and call "
                            "define_task again. Every input needs name, type and value.")
        # Last attempt: an error now goes to run(), which ends the run as llm_error.
        return self._validated_task(self.llm.decide(prompts.SYSTEM, prompt, tool_specs(TASK_TOOLS)))

    def _validated_task(self, call) -> DefineTask:
        if call.name != "define_task":
            raise ValueError(f"expected define_task, got {call.name}")
        task = DefineTask.model_validate(call.arguments)
        # Inputs must come from the goal. A value the LLM made up would become a wrong default.
        missing = [i for i in task.inputs if i.value.lower() not in self.goal.lower()]
        if missing:
            raise ValueError(f"input '{missing[0].name}' value '{missing[0].value}' is not in the goal")
        for i in task.inputs:
            if i.sensitive:
                self.session.logger.masker.add_value(i.value, field=i.name)
        self.session.logger.log(
            "discover", "task_defined", self.session.control, action="define_task", reason=task.description,
            data={"recipe_id": task.recipe_id, "inputs": {i.name: i.value for i in task.inputs},
                  "outputs": [o.name for o in task.outputs]})
        return task

    # ------------------------------------------------------------ one turn
    def _handle(self, number: int, tool: str, arguments: dict) -> Stop | None:
        model = ACTION_TOOLS.get(tool)
        if model is None:
            return self._record_failure(number, tool, arguments, "invalid", f"unknown tool '{tool}'")
        try:
            args = model.model_validate(arguments)
        except ValidationError as e:
            return self._record_failure(number, tool, arguments, "invalid", _short(e))

        if isinstance(args, AskHuman):
            self._add(AgentStep(number, tool, arguments, "done"))
            return Stop("stuck", "agent_asked", args.reason)
        if isinstance(args, Done):
            return self._handle_done(number, tool, arguments, args)
        if isinstance(args, Extract) and args.output not in self._output_names():
            return self._record_failure(number, tool, arguments, "invalid",
                                        f"'{args.output}' is not a declared output")

        action = to_action(args, step=number)
        if action.value is not None:
            self._declare_if_from_goal(action.value, field_name=getattr(args, "name", "value"))
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
            return Stop("rejected", detail=outcome.message)
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
            return Stop("stuck", "page_unchanged", f'clicking "{args.name}" changed nothing')

        self._add(step)
        return None

    def _handle_done(self, number: int, tool: str, arguments: dict, args: Done) -> Stop | None:
        if not args.success:
            self._add(AgentStep(number, tool, arguments, "done", args.summary))
            return Stop("not_achievable", detail=args.summary)
        missing = self._missing_outputs()
        if missing:
            return self._record_failure(number, tool, arguments, "invalid",
                                        f"outputs not extracted yet: {', '.join(missing)}")
        self._add(AgentStep(number, tool, arguments, "done", args.summary))
        return Stop("success", detail=args.summary)

    def _record_failure(self, number: int, tool: str, arguments: dict, status: str, message: str,
                        step: AgentStep | None = None) -> Stop | None:
        """Log a failed attempt; the same action failing twice means the agent is stuck."""
        step = step or AgentStep(number, tool, arguments, status, message)
        step.status, step.message = status, message
        self._add(step)
        signature = f"{tool}:{sorted((k, str(v)) for k, v in arguments.items() if k != 'reason')}"
        self._failures[signature] = self._failures.get(signature, 0) + 1
        if self._failures[signature] >= 2:
            return Stop("stuck", "repeated_failure", f"{tool} failed twice: {message}")
        return None

    # ------------------------------------------------------------ helpers
    def _declare_if_from_goal(self, value: str, field_name: str) -> None:
        """A typed/selected value that comes from the goal is a parameter, even if the model
        forgot to declare it. Declare it (sensitive, so it is masked from now on) before the
        action is logged, and remember it so the recipe is marked for review."""
        if value in {i.value for i in self.task.inputs} or value.lower() not in self.goal.lower():
            return
        for i in self.task.inputs:
            if _same_amount(i.value, value):
                # Declared as "$50.00", typed as "50.00": the same input. Keep what was typed, so the
                # recorder can turn it into {{placeholder}}.
                i.value = value
                self.session.logger.masker.add_value(value, field=i.name)
                return
        name = re.sub(r"[^a-z0-9]+", "_", field_name.lower()).strip("_") or "value"
        while name in {i.name for i in self.task.inputs}:
            name += "_2"
        kind = "currency" if re.fullmatch(r"\$?\d[\d,]*\.\d{2}", value) else "string"
        self.task.inputs.append(InputDef(name=name, description=f"{field_name} (declared automatically: "
                                         "the value came from the goal)", type=kind, value=value, sensitive=True))
        self.session.logger.masker.add_value(value, field=name)
        self.auto_inputs.append(name)
        self.session.logger.log("discover", "input_auto_declared", self.session.control, action="define_task",
                                reason=f"'{name}' came from the goal but was not declared", data={"input": name})

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

    def _output(self, name: str) -> OutputDef:
        return next(o for o in self.task.outputs if o.name == name)

    def _missing_outputs(self) -> list[str]:
        return [n for n in self._output_names() if n not in self.outputs]

    def _finish(self, outcome: Outcome, stop_reason: StopReason | None = None, detail: str = "") -> DiscoveryResult:
        s, log = self.session, self.session.logger
        result = DiscoveryResult(outcome, self.goal, self.task, self.steps, self.outputs, stop_reason, detail,
                                 auto_inputs=list(self.auto_inputs))
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
        decisions = 1 + sum(1 for st in self.steps if st.source == "agent")  # define_task + one per turn
        common = dict(run_id=self.session.logger.run_id, mode="discover", answered_by="llm_discovery",
                      llm_used_for=[f"driving the UI live ({self.llm.model}, {decisions} decisions)"],
                      recipe_id=r.task.recipe_id if r.task else None,
                      approvals=list(self.session.approvals),
                      human_interventions=list(self.session.human_interventions),
                      log_file=str(self.session.logger.folder.log_path))
        if r.outcome == "success":
            return RunResult(status=RunStatus.SUCCESS, outputs=r.outputs, message=r.detail, **common)
        if r.outcome == "rejected":
            return RunResult(status=RunStatus.REJECTED_BY_OPERATOR, message=r.detail, **common)
        if r.outcome == "aborted":
            return RunResult(status=RunStatus.ABORTED_BY_OPERATOR, message=r.detail, **common)
        last = self.steps[-1].number if self.steps else None
        # ESCALATED: a human already had it and it still did not finish.
        status = RunStatus.ESCALATED if self.session.takeovers else RunStatus.FAILED
        return RunResult(status=status, message=r.detail, **common, failure=FailureInfo(
            step=last, expected="the goal to be reached", observed=r.detail,
            error_type=r.stop_reason or r.outcome, evidence=evidence))


def run_discovery(session: Session, llm: LLMClient, goal: str, operator: Operator | None = None) -> DiscoveryResult:
    """Open the entry page and let the agent work towards the goal. With an operator, being
    stuck leads to human takeover on this same session instead of ending the run."""
    session.perform(Action("navigate", url=session.settings.entry_path, mode="discover", reason="open entry page"))
    return Discovery(session, llm, goal, operator).run()


def human_locators(a: HumanAction) -> list[Strategy]:
    """Locators for an element the human used. Built from what the in-page recorder saw; they
    could not be re-verified (the page has moved on), which is one reason human steps make the
    recipe needs_review."""
    out: list[Strategy] = []
    if a.role and a.name:
        out.append(RoleStrategy(by="role", role=a.role, name=a.name))
    if a.label and a.tag in ("input", "select", "textarea"):
        out.append(LabelStrategy(by="label", text=a.label))
    if a.tag == "input" and a.type == "submit" and a.value_attr:
        out.append(CssStrategy(by="css", value=f'input[type="submit"][value="{a.value_attr}"]'))
    elif a.field and a.tag in ("input", "select", "textarea"):
        out.append(CssStrategy(by="css", value=f'{a.tag}[name="{a.field}"]'))
    return out


def _same_amount(a: str, b: str) -> bool:
    """'$1,250.00' and '1250.00' are the same amount."""
    norm = [re.sub(r"[$,\s]", "", x) for x in (a, b)]
    return all(re.fullmatch(r"-?\d+(\.\d+)?", n) for n in norm) and float(norm[0]) == float(norm[1])


def _short(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(p) for p in first["loc"]) or "arguments"
    return f"invalid {where}: {first['msg']}"
