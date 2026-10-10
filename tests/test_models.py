"""The recipe schema is the core contract, so every validation rule has a test."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.models import InterventionRequest, Policy, Recipe, RunResult, RunStatus

FIXTURE = Path(__file__).parent / "fixtures" / "member.lookup_savings_balance@1.0.0.json"


@pytest.fixture
def recipe_dict() -> dict:
    return json.loads(FIXTURE.read_text())


def assert_rejected(data: dict, message: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Recipe.model_validate(data)
    assert message in str(exc.value)


# ---------------------------------------------------------------- valid recipe
def test_example_recipe_is_valid(recipe_dict):
    recipe = Recipe.model_validate(recipe_dict)
    assert recipe.file_name == "member.lookup_savings_balance@1.0.0.json"
    assert recipe.inputs["member_id"].sensitive
    assert recipe.steps[3].target.strategies[0].by == "near_text"


def test_recipe_round_trips_through_json(recipe_dict):
    recipe = Recipe.model_validate(recipe_dict)
    again = Recipe.model_validate_json(recipe.model_dump_json())
    assert again == recipe


def test_retry_rule_only_repeats_safe_actions(recipe_dict):
    recipe = Recipe.model_validate(recipe_dict)
    # navigate, type, extract may be re-run; click may have changed data
    assert [s.is_repeatable for s in recipe.steps] == [True, True, False, True]


# ---------------------------------------------------------------- structure
def test_unknown_field_is_rejected(recipe_dict):
    recipe_dict["steps"][0]["wiat_for"] = {}
    assert_rejected(recipe_dict, "Extra inputs are not permitted")


def test_steps_must_be_numbered_in_order(recipe_dict):
    recipe_dict["steps"][1]["step"] = 5
    assert_rejected(recipe_dict, "steps must be numbered 1..4")


def test_bad_version_is_rejected(recipe_dict):
    recipe_dict["version"] = "1.0"
    assert_rejected(recipe_dict, "version")


def test_input_pattern_must_be_valid_regex(recipe_dict):
    recipe_dict["inputs"]["member_id"]["pattern"] = "^(\\d{5}$"
    assert_rejected(recipe_dict, "pattern")


# ---------------------------------------------------------------- contract vs steps
def test_undeclared_placeholder_is_rejected(recipe_dict):
    recipe_dict["steps"][1]["value"] = "{{memberid}}"
    assert_rejected(recipe_dict, "undeclared input {{memberid}}")


def test_unused_input_is_rejected(recipe_dict):
    recipe_dict["inputs"]["branch"] = {"description": "x", "type": "string"}
    assert_rejected(recipe_dict, "input 'branch' is declared but never used")


def test_output_without_extract_step_is_rejected(recipe_dict):
    recipe_dict["outputs"]["checking_balance"] = {"description": "x", "type": "currency"}
    assert_rejected(recipe_dict, "output 'checking_balance' must be produced by exactly one extract step")


def test_business_outcome_must_be_declared(recipe_dict):
    del recipe_dict["outcomes"]["MEMBER_NOT_FOUND"]
    assert_rejected(recipe_dict, "returns 'MEMBER_NOT_FOUND', which is not in outcomes")


def test_outcomes_must_include_success(recipe_dict):
    del recipe_dict["outcomes"]["SUCCESS"]
    assert_rejected(recipe_dict, "outcomes must include SUCCESS")


def test_handler_scope_must_point_to_real_step(recipe_dict):
    recipe_dict["error_handlers"][0]["scope"] = "after_step:9"
    assert_rejected(recipe_dict, "points past the last step")


def test_handler_ids_must_be_unique(recipe_dict):
    recipe_dict["error_handlers"][1]["id"] = "member_not_found"
    assert_rejected(recipe_dict, "error handler ids must be unique")


# ---------------------------------------------------------------- steps and locators
@pytest.mark.parametrize("change, message", [
    (lambda s: s.pop("url"), "navigate needs url"),
    (lambda s: s.update(value="x"), "only type/select may have value"),
])
def test_navigate_step_fields(recipe_dict, change, message):
    change(recipe_dict["steps"][0])
    assert_rejected(recipe_dict, message)


def test_type_step_needs_value(recipe_dict):
    del recipe_dict["steps"][1]["value"]
    assert_rejected(recipe_dict, "type needs value")


def test_extract_step_needs_save_as_and_parse(recipe_dict):
    del recipe_dict["steps"][3]["parse"]
    assert_rejected(recipe_dict, "extract needs save_as and parse")


def test_css_must_be_last_strategy(recipe_dict):
    strategies = recipe_dict["steps"][1]["target"]["strategies"]
    strategies.reverse()  # css first
    assert_rejected(recipe_dict, "css strategy may only be the last")


def test_unknown_strategy_kind_is_rejected(recipe_dict):
    recipe_dict["steps"][1]["target"]["strategies"][0] = {"by": "xy", "x": 10, "y": 20}
    assert_rejected(recipe_dict, "strategies")  # no coordinates, ever


def test_when_needs_exactly_one_trigger(recipe_dict):
    recipe_dict["error_handlers"][0]["when"] = {"text": "a", "dialog": "b"}
    assert_rejected(recipe_dict, "when needs exactly one of")


def test_default_on_unknown_cannot_be_weakened(recipe_dict):
    recipe_dict["default_on_unknown"] = {"type": "recoverable"}
    assert_rejected(recipe_dict, "default_on_unknown")


# ---------------------------------------------------------------- run result
def test_success_result():
    r = RunResult(status=RunStatus.SUCCESS, run_id="r1", mode="replay", outputs={"savings_balance": "16057.78"})
    assert r.outputs["savings_balance"] == "16057.78"


@pytest.mark.parametrize("fields, message", [
    ({"status": "BUSINESS_OUTCOME"}, "BUSINESS_OUTCOME needs outcome_code"),
    ({"status": "FAILED"}, "FAILED needs failure details"),
    ({"status": "SUCCESS", "outcome_code": "MEMBER_NOT_FOUND"}, "SUCCESS cannot have"),
    ({"status": "BUSINESS_OUTCOME", "outcome_code": "MEMBER_NOT_FOUND", "outputs": {"x": "1"}}, "only SUCCESS may return outputs"),
])
def test_inconsistent_results_are_rejected(fields, message):
    with pytest.raises(ValidationError, match=message):
        RunResult(run_id="r1", mode="replay", **fields)


# ---------------------------------------------------------------- intervention and policy
def test_intervention_needs_goal_or_recipe():
    with pytest.raises(ValidationError, match="needs a goal or a recipe_id"):
        InterventionRequest(run_id="r1", mode="replay", reason="hard_failure", detail="x", current_url="http://localhost:5050/")


def test_policy_rejects_unknown_action():
    with pytest.raises(ValidationError):
        Policy(allowed_domains=["localhost:5050"], allowed_paths=["/search"], allowed_actions=["delete_everything"])
