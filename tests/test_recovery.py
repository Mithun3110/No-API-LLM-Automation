"""Bounded LLM recovery: one step, few actions, never risky, verified by the step's own checks."""

import json
import urllib.request
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from src.agent import MockLLM, load_mock, run_discovery
from src.handoff import open_session
from src.models import Recipe, RunStatus
from src.models.settings import load_settings
from src.recorder import record
from src.recovery import LLMRecoverer
from src.replay import replay
from src.safety import load_policy

FIXTURE = Path(__file__).parent / "fixtures" / "member.lookup_savings_balance@1.0.0.json"
TYPE_ID = {"tool": "type_text", "args": {"name": "Member ID", "text": "12345", "reason": "the box is still here"}}


@pytest.fixture
def run_with(bank_url, tmp_path):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})

    def _run(recipe_data, calls, inputs=None, approve=False, before=None):
        llm = MockLLM(calls)
        with open_session("replay", settings=settings, policy=policy, runs_dir=tmp_path / "runs", echo=False,
                          username="demo", password="demo123", approver=lambda _r: approve) as s:
            if before:
                before(s)
            recipe = recipe_data if isinstance(recipe_data, Recipe) else Recipe.model_validate(recipe_data)
            result = replay(recipe, inputs or {"member_id": "12345"}, s, recoverer=LLMRecoverer(s, llm)).run_result
        return result, llm, s
    return _run


def broken(step_index: int, strategies: list) -> dict:
    data = json.loads(FIXTURE.read_text())
    data["steps"][step_index]["target"]["strategies"] = strategies
    return data


BROKEN_BOX = broken(1, [{"by": "role", "role": "textbox", "name": "Member Number"}])


def test_recovers_a_broken_locator_and_flags_the_recipe(run_with):
    result, llm, _ = run_with(BROKEN_BOX, [TYPE_ID])
    assert result.status == RunStatus.SUCCESS and result.outputs == {"savings_balance": Decimal("16057.78")}
    assert result.llm_recovery_used and result.llm_used_for == ["recovering step 2 (1 action(s), succeeded)"]
    assert result.warnings == ["step 2 needed LLM recovery: the recipe needs review"]
    assert llm._next == 1  # ended as soon as the step's own action worked


def test_recipe_file_is_never_changed(run_with, tmp_path):
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(BROKEN_BOX))
    before = path.read_text()
    run_with(Recipe.model_validate_json(before), [TYPE_ID])
    assert path.read_text() == before


def test_recovery_may_only_type_this_runs_inputs(run_with):
    other_member = {"tool": "type_text", "args": {"name": "Member ID", "text": "99999", "reason": "x"}}
    result, _, _ = run_with(BROKEN_BOX, [other_member, {"tool": "give_up", "args": {"reason": "stuck"}}])
    assert result.status == RunStatus.FAILED and result.llm_used_for == ["recovering step 2 (0 action(s), failed)"]


def test_action_budget_is_enforced(run_with):
    useless = {"tool": "click", "args": {"role": "link", "name": "Member Search", "reason": "x"}}
    result, llm, _ = run_with(BROKEN_BOX, [useless] * 6)
    assert result.status == RunStatus.FAILED and "3 action(s)" in result.llm_used_for[0]


def test_claimed_success_is_verified_by_the_steps_own_checks(run_with):
    # The model "recovers" the balance by reading the wrong label (Name:). Its action worked, but the
    # value does not parse as currency, so replay's own check rejects the recovery.
    data = broken(3, [{"by": "near_text", "anchor": "Savings Bal:"}])
    wrong = {"tool": "extract", "args": {"label": "Name:", "output": "savings_balance", "parse": "currency",
                                         "reason": "x"}}
    result, _, _ = run_with(data, [wrong])
    assert result.status == RunStatus.FAILED and "failed" in result.llm_used_for[0]


def test_recovery_reads_a_renamed_value(run_with):
    data = broken(3, [{"by": "near_text", "anchor": "Savings Bal:"}])
    fix = {"tool": "extract", "args": {"label": "Savings Balance:", "output": "savings_balance", "parse": "currency",
                                       "reason": "the label is Savings Balance:"}}
    result, _, _ = run_with(data, [fix])
    assert result.status == RunStatus.SUCCESS and result.outputs["savings_balance"] == Decimal("16057.78")


def test_known_bad_state_goes_to_a_human_not_the_llm(run_with, bank_url):
    def server_error_at_3(s):
        original = s.perform

        def perform(action):
            if action.step == 3 and action.kind == "click" and not getattr(perform, "done", False):
                perform.done = True
                urllib.request.urlopen(f"{bank_url}/_admin/inject?error=server_error")
            return original(action)
        s.perform = perform

    result, llm, _ = run_with(json.loads(FIXTURE.read_text()), [TYPE_ID], before=server_error_at_3)
    assert result.failure.error_type == "server_error" and not result.llm_recovery_used and llm._next == 0


@pytest.fixture(scope="module")
def open_account(bank_url, tmp_path_factory):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})
    goal = "Open a Money Market account for member 56789 with an initial deposit of $20.00"
    d = tmp_path_factory.mktemp("recovery_open")
    with open_session("discover", settings=settings, policy=policy, runs_dir=d, echo=False, username="demo",
                      password="demo123", approver=lambda _r: True) as s:
        return record(run_discovery(s, load_mock(goal), goal), s.logger.run_id, s.settings, s.guard,
                      s.logger.masker, recipes_dir=d / "recipes").recipe


def test_irreversible_step_is_never_given_to_the_llm(run_with, open_account):
    data = open_account.model_dump(mode="json")
    confirm = next(st for st in data["steps"] if st["risk"] == "irreversible")
    confirm["target"]["strategies"] = [{"by": "role", "role": "button", "name": "Confirm Opening"}]
    inputs = {"member_id": "67890", "account_type": "Money Market", "initial_deposit": "5.00"}
    result, llm, _ = run_with(data, [{"tool": "click", "args": {"role": "button", "name": "Confirm", "reason": "x"}}],
                              inputs=inputs, approve=True)
    assert result.status == RunStatus.FAILED and not result.llm_recovery_used and llm._next == 0


def test_risky_click_is_blocked_in_recovery_mode(run_with, open_account):
    # A safe step fails; the "recovery" tries to click a data-changing button on the way.
    data = open_account.model_dump(mode="json")
    extract = next(st for st in data["steps"] if st["action"] == "extract")
    extract["target"]["strategies"] = [{"by": "near_text", "anchor": "Confirmation No:"}]
    inputs = {"member_id": "67890", "account_type": "Money Market", "initial_deposit": "5.00"}

    def stop_before_confirm(s):
        original = s.perform
        def perform(action):
            if action.mode == "recovery":
                # pretend we are back on the review page: try the risky button through the real gate
                s.browser.page.go_back()
                s.browser.settle(5)
            return original(action)
        s.perform = perform

    risky = {"tool": "click", "args": {"role": "button", "name": "Confirm", "reason": "submit again"}}
    result, _, s = run_with(data, [risky, {"tool": "give_up", "args": {"reason": "no"}}], inputs=inputs,
                            approve=True, before=stop_before_confirm)
    log = [json.loads(line) for line in s.logger.folder.log_path.read_text().splitlines()]
    blocked = [e for e in log if e["mode"] == "recovery" and e["outcome"] == "blocked"]
    assert blocked and blocked[0]["data"]["rule"] == "risky_in_recovery"
    assert result.status == RunStatus.FAILED
