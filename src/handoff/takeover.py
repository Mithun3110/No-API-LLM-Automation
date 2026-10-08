"""Human takeover on the SAME live session.

    AUTOMATION --stuck--> PAUSED (request raised, evidence saved) --> HUMAN (actions recorded)
        --resume--> AUTOMATION (re-read the page and continue)
        --abort---> run ends as ABORTED_BY_OPERATOR

Only one party acts at a time: Session.perform() refuses unless control is AUTOMATION.
"""

import json
from dataclasses import dataclass, field

from src.models import ControlState, InterventionRequest
from src.models.common import LogMode, RunMode
from src.models.intervention import StopReason

from .operator import Decision, Operator
from .session import Session


@dataclass
class HumanAction:
    """One thing the human did, as reported by the in-page recorder. Values are raw here;
    they are masked whenever they are logged or shown."""
    kind: str                 # click, type, select
    role: str | None
    name: str
    label: str | None
    tag: str
    field: str | None         # the element's name attribute, for a CSS fallback
    type: str | None
    value_attr: str | None
    value: str | None         # typed text or selected option
    dialog: str | None
    heading: str              # page heading when it happened
    path: str

    @classmethod
    def from_report(cls, d: dict) -> "HumanAction":
        return cls(kind=d.get("kind", "click"), role=d.get("role"), name=d.get("name") or "", label=d.get("label"),
                   tag=d.get("tag", ""), field=d.get("field"), type=d.get("type"), value_attr=d.get("value_attr"),
                   value=d.get("value"), dialog=d.get("dialog"), heading=d.get("heading", ""), path=d.get("path", ""))

    def describe(self) -> str:
        what = f'{self.kind} {self.role or self.tag} "{self.name}"'
        return what + (f" = {self.value!r}" if self.value is not None else "") + f" on {self.path}"


@dataclass
class TakeoverResult:
    decision: Decision
    actions: list[HumanAction] = field(default_factory=list)
    heading_after: str = ""   # page heading when control came back
    request: InterventionRequest | None = None


def take_over(session: Session, operator: Operator, mode: RunMode, reason: StopReason, detail: str,
              goal: str | None = None, recipe_id: str | None = None, recipe_version: str | None = None,
              step: int | None = None, step_description: str | None = None) -> TakeoverResult:
    log, b, masker = session.logger, session.browser, session.logger.masker
    log_mode: LogMode = "discover" if mode == "discover" else "replay"

    # 1. Pause and raise the request, with evidence, before anyone touches the page.
    session.set_control(ControlState.PAUSED, f"stuck: {reason}", log_mode)
    session.takeovers += 1
    shot = b.screenshot(log.folder.screenshot_path(f"takeover{session.takeovers}"))
    request = InterventionRequest(
        run_id=log.run_id, mode=mode, goal=masker.mask_text(goal) if goal else None, recipe_id=recipe_id,
        recipe_version=recipe_version, step=step,
        step_description=masker.mask_text(step_description) if step_description else None,
        reason=reason, detail=masker.mask_text(detail), current_url=masker.mask_text(b.current_url()),
        screenshot_path=str(shot),
    )
    saved_to = log.folder.intervention_path(session.takeovers)
    saved_to.write_text(json.dumps(request.model_dump(mode="json"), indent=2))
    log.log(log_mode, "intervention_requested", session.control, step=step, reason=request.detail,
            data={"stop_reason": reason, "request": str(saved_to)})
    operator.notify(request, saved_to)

    # 2. Hand the same live browser to the human and record what they do.
    actions: list[HumanAction] = []

    def on_action(report: dict) -> None:
        action = HumanAction.from_report(report)
        actions.append(action)
        log.log("human", "recorded", ControlState.HUMAN, action=action.kind, target=masker.mask_text(action.describe()))

    session.set_control(ControlState.HUMAN, "operator has control of the browser", log_mode)
    b.start_human_recording(on_action)
    try:
        decision = operator.wait_for_decision(b)
    finally:
        b.stop_human_recording()

    # 3. Take control back. On abort the run ends; on resume the caller re-reads the page.
    b.settle(session.settings.wait_timeout_s)
    session.set_control(ControlState.AUTOMATION if decision == "resume" else ControlState.PAUSED,
                        f"operator chose {decision}", log_mode)
    did = "; ".join(a.describe() for a in actions) or "no actions"
    session.human_interventions.append(masker.mask_text(
        f"takeover {session.takeovers} ({reason}): {did} -> {decision}"))
    log.log("human", decision, session.control, reason=f"{len(actions)} recorded action(s)",
            data={"actions": [masker.mask_text(a.describe()) for a in actions]})
    return TakeoverResult(decision, actions, b.heading(), request)
