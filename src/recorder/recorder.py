"""The recorder: a successful discovery run becomes a typed, versioned recipe (status draft).

What it does to the raw run:
- keeps only actions that worked, in order, and drops actions that dealt with a known
  recoverable interruption (e.g. clicking OK on the Notice popup: an error handler covers it)
- turns this run's input values into {{placeholders}} (12345 -> {{member_id}})
- records several verified locators per element, with the reasoning, and the page
  checkpoints around each step (expect_page before, wait_for after)
- marks data-changing clicks as irreversible
- adds the app's shared error rules
- refuses to save anything that would store a real value

It never runs on a failed or partial discovery: a recipe is a proven path, not a guess.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.agent import AgentStep, DiscoveryResult
from src.catalog.store import write_recipe
from src.models import Recipe
from src.models.recipe import (
    CssStrategy, LabelStrategy, NearTextStrategy, RoleStrategy, Strategy, TextStrategy,
)
from src.models.settings import Settings
from src.safety import Masker, ProposedAction, SafetyGuard

ROOT = Path(__file__).parent.parent.parent
RECIPES_DIR = ROOT / "recipes"
APP_RULES_PATH = ROOT / "config" / "app_rules.json"

TOOL_TO_ACTION = {"navigate": "navigate", "click": "click", "type_text": "type", "select_option": "select",
                  "extract": "extract"}


class RecorderError(Exception):
    """The run cannot be turned into a safe, valid recipe."""


@dataclass
class Recorded:
    recipe: Recipe
    path: Path
    skipped: list[str] = field(default_factory=list)  # steps left out, and why


def record(result: DiscoveryResult, run_id: str, settings: Settings, guard: SafetyGuard, masker: Masker,
           recipes_dir: Path = RECIPES_DIR, app_rules_path: Path = APP_RULES_PATH) -> Recorded:
    if result.outcome != "success" or result.task is None:
        raise RecorderError(f"only successful runs are recorded (this one: {result.outcome})")
    rules = json.loads(app_rules_path.read_text())
    return _Builder(result, run_id, settings, guard, masker, rules).save(recipes_dir)


class _Builder:
    def __init__(self, result, run_id, settings, guard, masker, rules):
        self.result, self.task, self.run_id = result, result.task, run_id
        self.settings, self.guard, self.masker, self.rules = settings, guard, masker, rules
        # Longest values first, so "123456" is replaced before "1234" could match inside it.
        self.values = sorted(((i.value, i.name) for i in self.task.inputs), key=lambda v: -len(v[0]))
        self.skipped: list[str] = []
        self.used_inputs: set[str] = set()

    # ------------------------------------------------------------ assemble
    def save(self, recipes_dir: Path) -> Recorded:
        agent_steps = self.result.successful_steps
        first_heading = agent_steps[0].heading_before if agent_steps else ""
        steps = [self._entry_step(first_heading)]
        for s in agent_steps:
            if self._handled_by_rules(s):
                self.skipped.append(f"step {s.number} ({s.tool} in '{s.in_dialog}' dialog): "
                                    "covered by a recoverable error handler")
                continue
            steps.append(self._step(len(steps) + 1, s))

        last_heading = next((s.heading_after for s in reversed(agent_steps) if s.heading_after), "")
        recipe_id = self.task.recipe_id
        data = {
            "schema_version": "1.0",
            "recipe_id": recipe_id,
            "name": self._clean(self.task.name),
            "description": self._clean(self.task.description),
            "version": next_version(recipe_id, recipes_dir),
            "status": "draft",  # never runs unreviewed in strict mode until approved
            # Human steps (not re-verified) and inputs the system had to declare itself: review them.
            "needs_review": any(s.source == "human" for s in agent_steps) or bool(self.result.auto_inputs),
            "app": {**self.rules["app"], "entry_url": self.settings.bank_base_url + self.settings.entry_path},
            "provenance": {"recorded_from_run": self.run_id, "recorded_at": datetime.now(timezone.utc).isoformat()},
            "inputs": {i.name: {"description": self.masker.mask_text(i.description) or i.name, "type": i.type,
                                "pattern": i.pattern, "required": True, "sensitive": i.sensitive}
                       for i in self.task.inputs if i.name in self.used_inputs},
            "outputs": {o.name: {"description": self.masker.mask_text(o.description) or o.name, "type": o.type,
                                 "sensitive": o.sensitive} for o in self.task.outputs},
            "outcomes": {"SUCCESS": "The goal was completed and outputs were read.", **self.rules["outcomes"]},
            "steps": steps,
            "success_check": {"description": "On the final page with every output read.",
                              "heading": last_heading or None,
                              "outputs_present": [o.name for o in self.task.outputs]},
            "error_handlers": self.rules["error_handlers"],
            "default_on_unknown": {"type": "hard_failure", "then": "escalate"},
        }
        recipe = Recipe.model_validate(data)  # every schema cross-check runs before anything is written
        self._check_no_real_values(json.dumps(recipe.model_dump(mode="json", exclude_none=True)))
        path = recipes_dir / recipe.file_name
        write_recipe(recipe, path)
        return Recorded(recipe, path, self.skipped)

    def _entry_step(self, heading: str) -> dict:
        step = {"step": 1, "description": f"Open the {heading or 'entry'} page.", "source": "agent",
                "action": "navigate", "url": self.settings.entry_path, "risk": "safe"}
        if heading:
            step["wait_for"] = {"any_of": [{"heading": heading}]}
        return step

    def _step(self, number: int, s: AgentStep) -> dict:
        action = TOOL_TO_ACTION[s.tool]
        step: dict = {"step": number, "description": self._clean(s.args.get("reason")) or s.tool,
                      "source": s.source, "action": action, "risk": "safe"}
        if s.heading_before:
            step["expect_page"] = {"heading": s.heading_before}
        if action == "navigate":
            step["url"] = self._placeholder(s.value or "", s.number)
        else:
            strategies = self._generic_strategies(s)
            step["target"] = {"strategies": [x.model_dump(exclude_defaults=False) for x in strategies],
                              "why": explain(strategies)}
        if action in ("type", "select"):
            step["value"] = self._placeholder(s.value or "", s.number)
        if action == "extract":
            step["save_as"], step["parse"] = s.args["output"], s.args["parse"]
        if action == "click" and self._risky(strategies):
            step["risk"] = "irreversible"
        if s.heading_after and s.heading_after != s.heading_before:
            step["wait_for"] = {"any_of": [{"heading": s.heading_after}]}
        return step

    # ------------------------------------------------------------ rules
    def _handled_by_rules(self, s: AgentStep) -> bool:
        dialogs = {h["when"].get("dialog") for h in self.rules["error_handlers"] if h["type"] == "recoverable"}
        return s.in_dialog is not None and s.in_dialog in dialogs

    def _placeholder(self, text: str, step_number: int) -> str:
        """Replace this run's input values with {{name}}. Refuse sensitive constants."""
        for value, name in self.values:
            if value in text:
                text = text.replace(value, "{{" + name + "}}")
                self.used_inputs.add(name)
        bare = re.sub(r"\{\{\w+\}\}", "", text)
        if self.masker.mask_text(bare) != bare:
            raise RecorderError(f"step {step_number} would store a real value; declare it as an input instead")
        if bare.strip() and bare.strip().lower() in self.result.goal.lower():
            # A value copied from the goal is a parameter by definition, never a fixed value.
            raise RecorderError(f"step {step_number} would store '{bare.strip()}' from the goal as a fixed value")
        return text

    def _generic_strategies(self, s: AgentStep) -> list[Strategy]:
        """Drop locators that only work for THIS record (contain an input value or sensitive data)."""
        kept = []
        for strategy in s.locators:
            text = json.dumps(strategy.model_dump())
            if any(v in text for v, _ in self.values) or self.masker.mask_text(text) != text:
                continue
            kept.append(strategy)
        if not kept:
            raise RecorderError(f"step {s.number}: every locator depends on this record's data")
        return kept

    def _risky(self, strategies: list[Strategy]) -> bool:
        role = next((x for x in strategies if isinstance(x, RoleStrategy)), None)
        return self.guard.is_risky(ProposedAction(action="click", page_url="",
                                                  target_role=role.role if role else None,
                                                  target_name=role.name if role else None))

    def _clean(self, text: str | None) -> str:
        """Descriptions come from the LLM and may quote real values: placeholder, then mask."""
        if not text:
            return ""
        for value, name in self.values:
            text = text.replace(value, "{" + name + "}")  # single braces: readable, not a placeholder
        return self.masker.mask_text(text)

    def _check_no_real_values(self, text: str) -> None:
        """Last line of defence: no SENSITIVE input value or output may appear in the file.

        Non-sensitive values (e.g. account_type "Money Market") can legitimately appear in the
        recipe, for example in that input's own pattern.
        """
        sensitive_inputs = [i.value for i in self.task.inputs if i.sensitive]
        sensitive_outputs = {o.name for o in self.task.outputs if o.sensitive}
        extracted = [s.extracted for s in self.result.steps
                     if s.extracted and s.args.get("output") in sensitive_outputs]
        leaked = [v for v in sensitive_inputs + extracted if v and v in text]
        if leaked:
            raise RecorderError(f"refusing to save: the recipe would contain {len(leaked)} real value(s)")


def explain(strategies: list[Strategy]) -> str:
    """Human-readable reasoning about why these locators are robust, for reviewers."""
    parts = []
    for s in strategies:
        match s:
            case RoleStrategy(role=role, name=name):
                parts.append(f'role {role} "{name}": semantic, survives layout and markup changes')
            case LabelStrategy(text=text):
                parts.append(f'label "{text}": a real <label> is linked to the field')
            case TextStrategy(text=text):
                parts.append(f'visible text "{text}"')
            case NearTextStrategy(anchor=anchor):
                parts.append(f'next to the stable label "{anchor}" (the value itself changes per record)')
            case CssStrategy(value=value):
                parts.append(f"css {value}: depends on markup, last resort")
    if len(strategies) == 1:
        parts.append("no verified backup was found")
    return "; ".join(parts) + ". Each was verified to match exactly this element when recorded."


def next_version(recipe_id: str, recipes_dir: Path) -> str:
    """1.0.0 for a new recipe; otherwise bump the minor of the latest (1.0.0 -> 1.1.0)."""
    versions = []
    for path in recipes_dir.glob(f"{recipe_id}@*.json"):
        m = re.fullmatch(rf"{re.escape(recipe_id)}@(\d+)\.(\d+)\.(\d+)\.json", path.name)
        if m:
            versions.append(tuple(int(x) for x in m.groups()))
    if not versions:
        return "1.0.0"
    major, minor, _ = max(versions)
    return f"{major}.{minor + 1}.0"
