"""The recipe: a recorded, reusable, typed capability.

Two parts:
- the CONTRACT (inputs, outputs, outcomes, success_check): what a calling agent needs
  to know to invoke it and interpret the result.
- the EXECUTION (steps, error_handlers): how the replay engine performs it with no LLM.

Validation here catches broken recipes when they are loaded, not halfway through a
replay on a live bank screen.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .common import OUTCOME_CODE_PATTERN, StepAction, StrictModel, ValueType

PLACEHOLDER = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
SEMVER = r"^\d+\.\d+\.\d+$"
NAME = r"^[a-z_][a-z0-9_]*$"  # input/output names, also used inside {{...}}


# ---------------------------------------------------------------- contract part
class AppInfo(StrictModel):
    name: str
    version: str  # the app version the recipe was recorded against; used to detect drift
    surface: Literal["web", "desktop"] = "web"
    entry_url: str


class Provenance(StrictModel):
    recorded_from_run: str
    recorded_at: datetime


class InputSpec(StrictModel):
    description: str
    type: ValueType
    pattern: str | None = None  # regex the value must fully match, checked before replay starts
    required: bool = True
    sensitive: bool = False     # sensitive values are masked in every log

    @field_validator("pattern")
    @classmethod
    def pattern_compiles(cls, v: str | None) -> str | None:
        # Fail when the recipe loads, not at replay time. re.error is not a ValueError,
        # so convert it, or Pydantic would crash instead of reporting a validation error.
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"invalid regex pattern: {e}") from e
        return v


class OutputSpec(StrictModel):
    description: str
    type: ValueType
    sensitive: bool = False


class SuccessCheck(StrictModel):
    """Final checkpoint: how we know the whole recipe worked."""
    description: str
    heading: str | None = None        # the final page's heading (preferred: identifies the page)
    text_visible: str | None = None
    outputs_present: list[str] = []


# ---------------------------------------------------------------- locators
# Strategies are tried in order. A strategy counts only if it matches exactly one element.
class RoleStrategy(StrictModel):
    by: Literal["role"]
    role: str           # accessibility role: button, textbox, link, combobox, cell, ...
    name: str           # accessible name as shown in the accessibility tree
    exact: bool = True  # exact name match; avoids "Search" also matching "Member Search"


class LabelStrategy(StrictModel):
    by: Literal["label"]
    text: str


class TextStrategy(StrictModel):
    by: Literal["text"]
    text: str


class NearTextStrategy(StrictModel):
    """The element next to a fixed label, e.g. the value cell right of 'Savings Balance:'.

    Needed for reading values: the value itself changes per member, so it cannot be
    the locator. The label next to it is stable.
    """
    by: Literal["near_text"]
    anchor: str
    relation: Literal["right_of", "below"] = "right_of"


class CssStrategy(StrictModel):
    by: Literal["css"]
    value: str


Strategy = Annotated[
    RoleStrategy | LabelStrategy | TextStrategy | NearTextStrategy | CssStrategy,
    Field(discriminator="by"),
]


class Target(StrictModel):
    strategies: list[Strategy] = Field(min_length=1)
    why: str  # reasoning about robustness, for human reviewers

    @model_validator(mode="after")
    def css_only_last(self) -> "Target":
        # CSS depends on page structure, which is the first thing to change. Last resort only.
        kinds = [s.by for s in self.strategies]
        if "css" in kinds[:-1]:
            raise ValueError("a css strategy may only be the last (fallback) strategy")
        return self


# ---------------------------------------------------------------- page conditions
class PageCondition(StrictModel):
    """Which page we are on. Exactly one field is set.

    heading is preferred: text_visible matches text ANYWHERE, and legacy apps repeat page names
    in navigation ("Member Search" is a link on every page), so it cannot identify a page.
    """
    heading: str | None = None        # the page's main heading equals this
    text_visible: str | None = None   # this text appears somewhere on the page

    @model_validator(mode="after")
    def exactly_one(self) -> "PageCondition":
        if (self.heading is None) == (self.text_visible is None):
            raise ValueError("page condition needs exactly one of: heading, text_visible")
        return self

    def describe(self) -> str:
        return f'heading "{self.heading}"' if self.heading else f'text "{self.text_visible}"'


class WaitCondition(StrictModel):
    """One thing that proves an action took effect. Exactly one field is set."""
    heading: str | None = None
    text: str | None = None
    url_contains: str | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> "WaitCondition":
        if sum(v is not None for v in (self.heading, self.text, self.url_contains)) != 1:
            raise ValueError("wait condition needs exactly one of: heading, text, url_contains")
        return self

    def describe(self) -> str:
        return f'heading "{self.heading}"' if self.heading else \
            f'text "{self.text}"' if self.text else f'url containing "{self.url_contains}"'


class WaitFor(StrictModel):
    any_of: list[WaitCondition] = Field(min_length=1)


# ---------------------------------------------------------------- execution part
class Step(StrictModel):
    step: int = Field(ge=1)
    description: str
    source: Literal["agent", "human"] = "agent"  # human = performed by the operator during discovery
    expect_page: PageCondition | None = None     # checked before the step; also picks the resume point
    action: StepAction
    url: str | None = None                       # navigate only; relative to app.entry_url's origin
    target: Target | None = None                 # click/type/select/extract
    value: str | None = None                     # type/select; usually a {{placeholder}}
    save_as: str | None = None                   # extract only: the output name
    parse: Literal["currency", "text", "number"] | None = None  # extract only
    only_if: PageCondition | None = None         # skip the step when this is not on the page
    risk: Literal["safe", "irreversible"] = "safe"
    wait_for: WaitFor | None = None

    @model_validator(mode="after")
    def fields_match_action(self) -> "Step":
        a = self.action
        problems = []
        if a == "navigate" and not self.url:
            problems.append("navigate needs url")
        if a != "navigate" and self.url:
            problems.append("only navigate may have url")
        if a in {"click", "type", "select", "extract"} and self.target is None:
            problems.append(f"{a} needs target")
        if a in {"type", "select"} and self.value is None:
            problems.append(f"{a} needs value")
        if a not in {"type", "select"} and self.value is not None:
            problems.append("only type/select may have value")
        if a == "extract" and (self.save_as is None or self.parse is None):
            problems.append("extract needs save_as and parse")
        if a != "extract" and (self.save_as is not None or self.parse is not None):
            problems.append("only extract may have save_as/parse")
        if problems:
            raise ValueError(f"step {self.step}: " + "; ".join(problems))
        return self

    @property
    def is_repeatable(self) -> bool:
        """Safe to fully re-run on retry: cannot have changed data (see retry rule)."""
        return self.action in {"navigate", "type", "extract"}


# ---------------------------------------------------------------- error handlers
class When(StrictModel):
    """What triggers a handler. Exactly one field is set."""
    text: str | None = None            # this text is on the page
    dialog: str | None = None          # a dialog with this name is open
    wait_timed_out: bool | None = None  # wait_for did not appear in time

    @model_validator(mode="after")
    def exactly_one(self) -> "When":
        if sum(v is not None for v in (self.text, self.dialog, self.wait_timed_out)) != 1:
            raise ValueError("when needs exactly one of: text, dialog, wait_timed_out")
        return self


class ClickFix(StrictModel):
    action: Literal["click"]
    target: Target


class WaitFix(StrictModel):
    action: Literal["wait"]
    seconds: float = Field(gt=0, le=30)


Fix = Annotated[ClickFix | WaitFix, Field(discriminator="action")]

SCOPE_PATTERN = r"^(any_step|after_step:[1-9]\d*)$"


class _HandlerBase(StrictModel):
    id: str = Field(pattern=NAME)
    description: str
    scope: str = Field(default="any_step", pattern=SCOPE_PATTERN)
    when: When


class BusinessOutcomeHandler(_HandlerBase):
    """A valid business answer (e.g. member not found). Stop and return the code; not a crash."""
    type: Literal["business_outcome"]
    result: str = Field(pattern=OUTCOME_CODE_PATTERN)


class RecoverableHandler(_HandlerBase):
    """A known, harmless interruption. Apply the fix and continue, a bounded number of times."""
    type: Literal["recoverable"]
    fix: Fix
    max_attempts: int = Field(default=3, ge=1, le=5)


class HardFailureHandler(_HandlerBase):
    """Known bad state. Stop with evidence and escalate."""
    type: Literal["hard_failure"]
    then: Literal["escalate"] = "escalate"


ErrorHandler = Annotated[
    BusinessOutcomeHandler | RecoverableHandler | HardFailureHandler,
    Field(discriminator="type"),
]


class DefaultOnUnknown(StrictModel):
    # Fixed on purpose: anything the recipe does not recognise is a hard failure. Never guess.
    type: Literal["hard_failure"] = "hard_failure"
    then: Literal["escalate"] = "escalate"


# ---------------------------------------------------------------- the recipe
class Recipe(StrictModel):
    schema_version: Literal["1.0"]
    recipe_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")  # e.g. member.lookup_savings_balance
    name: str
    description: str
    version: str = Field(pattern=SEMVER)
    status: Literal["draft", "approved"]
    needs_review: bool = False
    app: AppInfo
    provenance: Provenance

    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    outcomes: dict[str, str]

    steps: list[Step] = Field(min_length=1)
    success_check: SuccessCheck
    error_handlers: list[ErrorHandler] = []
    default_on_unknown: DefaultOnUnknown = DefaultOnUnknown()

    @field_validator("inputs", "outputs")
    @classmethod
    def names_are_identifiers(cls, v: dict) -> dict:
        for name in v:
            if not re.fullmatch(NAME, name):
                raise ValueError(f"'{name}' must be lowercase letters, digits and _")
        return v

    @field_validator("outcomes")
    @classmethod
    def outcomes_valid(cls, v: dict[str, str]) -> dict[str, str]:
        if "SUCCESS" not in v:
            raise ValueError("outcomes must include SUCCESS")
        for code in v:
            if not re.fullmatch(OUTCOME_CODE_PATTERN, code):
                raise ValueError(f"outcome code '{code}' must be UPPER_SNAKE_CASE")
        return v

    @model_validator(mode="after")
    def contract_matches_execution(self) -> "Recipe":
        """Cross-checks between the contract and the steps, so the two cannot drift apart."""
        errors = []

        numbers = [s.step for s in self.steps]
        if numbers != list(range(1, len(self.steps) + 1)):
            errors.append(f"steps must be numbered 1..{len(self.steps)} in order, got {numbers}")

        # Every {{placeholder}} must be a declared input; every input must be used.
        used = set()
        for s in self.steps:
            for text in (s.value, s.url):
                for name in PLACEHOLDER.findall(text or ""):
                    used.add(name)
                    if name not in self.inputs:
                        errors.append(f"step {s.step} uses undeclared input {{{{{name}}}}}")
        for name in self.inputs.keys() - used:
            errors.append(f"input '{name}' is declared but never used")

        # Every output is produced by exactly one extract step, and nothing else is extracted.
        produced = [s.save_as for s in self.steps if s.save_as]
        for name in produced:
            if name not in self.outputs:
                errors.append(f"extract saves undeclared output '{name}'")
        for name in self.outputs:
            if produced.count(name) != 1:
                errors.append(f"output '{name}' must be produced by exactly one extract step")
        for name in self.success_check.outputs_present:
            if name not in self.outputs:
                errors.append(f"success_check refers to undeclared output '{name}'")

        for h in self.error_handlers:
            if h.scope.startswith("after_step:") and int(h.scope.split(":")[1]) > len(self.steps):
                errors.append(f"handler '{h.id}' scope {h.scope} points past the last step")
            if isinstance(h, BusinessOutcomeHandler) and h.result not in self.outcomes:
                errors.append(f"handler '{h.id}' returns '{h.result}', which is not in outcomes")
        ids = [h.id for h in self.error_handlers]
        if len(ids) != len(set(ids)):
            errors.append("error handler ids must be unique")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def file_name(self) -> str:
        """recipes/<recipe_id>@<version>.json; old versions are kept side by side."""
        return f"{self.recipe_id}@{self.version}.json"
