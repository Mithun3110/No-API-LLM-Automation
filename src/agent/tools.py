"""The tools the LLM may call during discovery, as typed models.

The LLM returns ONE tool call per turn. Its arguments are validated here; a malformed
call becomes feedback for the next turn, never a crash and never a guessed action.
Each action tool maps to exactly one session Action, which then passes the safety gate.
"""

from typing import Literal

from pydantic import Field

from src.handoff import Action
from src.models.common import StrictModel, ValueType
from src.models.recipe import NAME, NearTextStrategy, RoleStrategy

ROLE = Literal["button", "link", "textbox", "combobox", "checkbox", "radio", "cell"]


# ---------------------------------------------------------------- task definition (first call)
# Models often omit "obvious" fields, so description and sensitive have defaults.
# sensitive defaults to True: masked unless the model explicitly says it is not sensitive.
class InputDef(StrictModel):
    name: str = Field(pattern=NAME, description="snake_case name, e.g. member_id")
    description: str = ""
    type: ValueType
    value: str = Field(description="the value for THIS run, copied exactly from the goal")
    pattern: str | None = Field(default=None, description="regex the value must match, e.g. ^\\d{5}$")
    sensitive: bool = Field(default=True, description="true for member IDs, names, phones, amounts, addresses")


class OutputDef(StrictModel):
    name: str = Field(pattern=NAME, description="snake_case name, e.g. savings_balance")
    description: str = ""
    type: ValueType
    sensitive: bool = True


class DefineTask(StrictModel):
    """Before acting: say what this capability is, its inputs (with values from the goal) and outputs."""
    recipe_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
                           description="namespaced id, e.g. member.lookup_savings_balance")
    name: str
    description: str
    inputs: list[InputDef]
    outputs: list[OutputDef] = Field(min_length=1)


# ---------------------------------------------------------------- actions
class Navigate(StrictModel):
    """Go to a page of the bank by path, e.g. /search."""
    path: str = Field(pattern=r"^/", description="path starting with /")
    reason: str


class Click(StrictModel):
    """Click an element, identified by role and name exactly as shown in the page tree."""
    role: ROLE
    name: str
    reason: str


class TypeText(StrictModel):
    """Type text into a textbox, identified by its name in the page tree. Replaces existing text."""
    name: str
    text: str
    reason: str


class SelectOption(StrictModel):
    """Choose an option in a dropdown (combobox), identified by its name in the page tree."""
    name: str
    option: str = Field(description="the option's visible text")
    reason: str


class Extract(StrictModel):
    """Read a value shown next to a label, e.g. label 'Savings Balance:' -> '$1,234.56'."""
    label: str = Field(description="the label text exactly as on the page, e.g. Savings Balance:")
    output: str = Field(pattern=NAME, description="which declared output this value is")
    parse: Literal["currency", "text", "number"]
    reason: str


class Wait(StrictModel):
    """Wait for a slow page."""
    seconds: float = Field(ge=1, le=10)
    reason: str


class Done(StrictModel):
    """Finish. success=true only when every declared output has been extracted."""
    success: bool
    summary: str


class AskHuman(StrictModel):
    """Stop and ask a human operator for help, e.g. when blocked or unsure."""
    reason: str


TASK_TOOLS = {"define_task": DefineTask}
ACTION_TOOLS = {
    "navigate": Navigate, "click": Click, "type_text": TypeText, "select_option": SelectOption,
    "extract": Extract, "wait": Wait, "done": Done, "ask_human": AskHuman,
}


def tool_specs(tools: dict[str, type[StrictModel]]) -> list[dict]:
    """Provider-neutral tool list: name, description, JSON schema of the arguments."""
    return [{"name": name, "description": (model.__doc__ or "").strip(), "parameters": model.model_json_schema()}
            for name, model in tools.items()]


def to_action(args: StrictModel, step: int) -> Action | None:
    """Map an action tool call to a session Action. done/ask_human/extract-parse are handled by the loop."""
    reason = getattr(args, "reason", None)
    match args:
        case Navigate(path=path):
            return Action("navigate", url=path, step=step, reason=reason, mode="discover")
        case Click(role=role, name=name):
            return Action("click", (RoleStrategy(by="role", role=role, name=name),), step=step, reason=reason,
                          mode="discover")
        case TypeText(name=name, text=text):
            return Action("type", (RoleStrategy(by="role", role="textbox", name=name),), value=text, step=step,
                          reason=reason, mode="discover")
        case SelectOption(name=name, option=option):
            return Action("select", (RoleStrategy(by="role", role="combobox", name=name),), value=option,
                          step=step, reason=reason, mode="discover")
        case Extract(label=label):
            return Action("extract", (NearTextStrategy(by="near_text", anchor=label),), step=step, reason=reason,
                          mode="discover")
        case Wait(seconds=seconds):
            return Action("wait", seconds=seconds, step=step, reason=reason, mode="discover")
    return None
