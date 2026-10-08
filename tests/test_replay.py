"""Replay against the real bank: every result kind, the retry rule, evidence, and no LLM."""

import ast
import json
import subprocess
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from src.agent import load_mock, run_discovery
from src.handoff import open_session
from src.logs import read_log
from src.models import Recipe, RunStatus
from src.models.settings import load_settings
from src.recorder import record
from src.replay import replay
from src.safety import load_policy

ROOT = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "member.lookup_savings_balance@1.0.0.json"


@pytest.fixture
def lookup() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def run(bank_url, tmp_path):
    """Replay a recipe (dict or Recipe) on a fresh session against the test bank."""
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})

    def _run(recipe, inputs, approve=False, before=None):
        recipe = recipe if isinstance(recipe, Recipe) else Recipe.model_validate(recipe)
        with open_session("replay", settings=settings, policy=policy, runs_dir=tmp_path / "runs", echo=False,
                          username="demo", password="demo123", approver=lambda _r: approve) as s:
            if before:
                before(s)
            result = replay(recipe, inputs, s)
        return result, s
    return _run


def inject(bank_url, error):
    urllib.request.urlopen(f"{bank_url}/_admin/inject?error={error}")


def inject_before_step(session, bank_url, step, error):
    """Trigger an injected error right before a given step's action (simulates mid-run trouble)."""
    original = session.perform

    def perform(action):
        if action.step == step and action.kind != "wait" and not getattr(perform, "done", False):
            perform.done = True
            inject(bank_url, error)
        return original(action)
    session.perform = perform


def actions(session, kind):
    return [e for e in read_log(session.logger.folder.log_path) if e.mode == "replay" and e.action == kind]


# ---------------------------------------------------------------- happy path
def test_success_returns_outputs(run, lookup):
    result, s = run(lookup, {"member_id": "12345"})
    r = result.run_result
    assert r.status == RunStatus.SUCCESS and r.outputs == {"savings_balance": Decimal("16057.78")}
    assert not r.warnings and not r.recoveries and r.failure is None
    saved = json.loads(s.logger.folder.result_path.read_text())
    assert saved["outputs"] == {"savings_balance": "***"}            # masked on disk
    assert "12345" not in s.logger.folder.log_path.read_text()       # input masked in the log


def test_replays_a_recorded_recipe_for_another_member(run, bank_url, tmp_path):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})
    goal = "Look up member 12345 and read their savings balance"
    with open_session("discover", settings=settings, policy=policy, runs_dir=tmp_path / "d", echo=False,
                      username="demo", password="demo123") as s:
        saved = record(run_discovery(s, load_mock(goal), goal), s.logger.run_id, s.settings, s.guard,
                       s.logger.masker, recipes_dir=tmp_path / "recipes")
    result, _ = run(saved.recipe, {"member_id": "23456"})
    assert result.run_result.outputs == {"savings_balance": Decimal("23684.35")}


# ---------------------------------------------------------------- business outcomes
@pytest.mark.parametrize("member, code", [("99999", "MEMBER_NOT_FOUND"), ("77777", "PERMISSION_DENIED")])
def test_business_outcomes(run, lookup, member, code):
    result, _ = run(lookup, {"member_id": member})
    r = result.run_result
    assert r.status == RunStatus.BUSINESS_OUTCOME and r.outcome_code == code
    assert r.failure is None and not r.outputs  # a valid answer, not a crash


# ---------------------------------------------------------------- invalid input
@pytest.mark.parametrize("inputs, message", [
    ({"member_id": "12a45"}, "does not match"),
    ({}, "member_id is required"),
    ({"member_id": "12345", "pin": "1"}, "unknown input"),
])
def test_invalid_input_never_touches_the_page(run, lookup, inputs, message):
    result, s = run(lookup, inputs)
    assert result.run_result.status == RunStatus.INVALID_INPUT and message in result.run_result.message
    assert not actions(s, "navigate")  # login happened, but no recipe step ran
    assert "12a45" not in (result.run_result.message or "")  # sensitive value not echoed


# ---------------------------------------------------------------- recoverable
def test_popup_before_first_step_is_dismissed(run, lookup, bank_url):
    result, _ = run(lookup, {"member_id": "12345"}, before=lambda s: inject(bank_url, "popup"))
    r = result.run_result
    assert r.status == RunStatus.SUCCESS and any("notice_popup" in x for x in r.recoveries)


def test_popup_after_click_is_dismissed_and_click_not_repeated(run, lookup, bank_url):
    result, s = run(lookup, {"member_id": "12345"},
                    before=lambda s: inject_before_step(s, bank_url, 3, "popup"))
    assert result.run_result.status == RunStatus.SUCCESS
    assert any("notice_popup" in x for x in result.run_result.recoveries)
    search_clicks = [e for e in actions(s, "click") if e.step == 3 and e.target == 'role=button "Search"']
    assert len(search_clicks) == 1


def test_slow_page_waits_and_never_clicks_twice(run, lookup, bank_url):
    result, s = run(lookup, {"member_id": "12345"},
                    before=lambda s: inject_before_step(s, bank_url, 3, "slow_page"))
    r = result.run_result
    assert r.status == RunStatus.SUCCESS and r.outputs["savings_balance"] == Decimal("16057.78")
    assert any("slow_page" in x for x in r.recoveries)
    assert len([e for e in actions(s, "click") if e.step == 3]) == 1  # the retry rule


# ---------------------------------------------------------------- hard failures
def test_server_error_is_hard_failure_with_evidence(run, lookup, bank_url):
    result, _ = run(lookup, {"member_id": "12345"},
                    before=lambda s: inject_before_step(s, bank_url, 3, "server_error"))
    r = result.run_result
    assert r.status == RunStatus.FAILED and result.failed_step == 3
    assert r.failure.error_type == "server_error" and "Something went wrong" in r.failure.observed
    shot, tree, trace = (Path(p) for p in r.failure.evidence)
    assert shot.suffix == ".png" and shot.exists() and tree.exists() and trace.exists()  # trace saved on close
    assert "12345" not in tree.read_text()  # the saved accessibility tree is masked


def test_session_expired_is_hard_failure(run, lookup, bank_url):
    result, _ = run(lookup, {"member_id": "12345"},
                    before=lambda s: inject_before_step(s, bank_url, 3, "session_expired"))
    assert result.run_result.failure.error_type == "session_expired"


def test_backup_locator_logs_drift_and_still_works(run, lookup):
    lookup["steps"][1]["target"]["strategies"][0]["name"] = "Member Number"  # the app renamed it
    result, _ = run(lookup, {"member_id": "12345"})
    r = result.run_result
    assert r.status == RunStatus.SUCCESS and r.warnings == ['step 2: backup locator used (label "Member ID")']


def test_no_locator_matches_is_hard_failure(run, lookup):
    lookup["steps"][2]["target"]["strategies"] = [{"by": "role", "role": "button", "name": "Find"}]
    result, _ = run(lookup, {"member_id": "12345"})
    f = result.run_result.failure
    assert f.step == 3 and f.error_type == "element_not_found" and 'role=button "Find": 0 matches' in f.observed


def test_wrong_page_is_caught_before_acting(run, lookup):
    lookup["steps"][3]["expect_page"] = {"text_visible": "Account Summary"}
    result, _ = run(lookup, {"member_id": "12345"})
    f = result.run_result.failure
    assert f.step == 4 and f.error_type == "wrong_page" and "Member Detail" in f.observed


def test_unknown_state_after_wait_is_hard_failure(run, lookup):
    lookup["steps"][2]["wait_for"] = {"any_of": [{"text": "Member Overview"}]}  # never appears
    lookup["error_handlers"] = [h for h in lookup["error_handlers"] if h["id"] != "slow_page"]
    result, _ = run(lookup, {"member_id": "12345"})
    assert result.run_result.failure.error_type == "wait_timeout"


def test_only_if_skips_a_step(run, lookup):
    ok = {"step": 2, "description": "Close a leftover notice if there is one.", "action": "click",
          "only_if": {"text_visible": "System maintenance"},
          "target": {"strategies": [{"by": "role", "role": "button", "name": "OK"}], "why": "dialog button"}}
    lookup["steps"].insert(1, ok)
    for i, st in enumerate(lookup["steps"], 1):
        st["step"] = i
    result, s = run(lookup, {"member_id": "12345"})
    assert result.run_result.status == RunStatus.SUCCESS
    assert [e.step for e in read_log(s.logger.folder.log_path) if e.outcome == "skipped"] == [2]


# ---------------------------------------------------------------- risky steps
@pytest.fixture(scope="module")
def open_account_recipe(bank_url, tmp_path_factory):
    # Recorded once: discovery really opens the account, so a second run would (correctly) fail.
    tmp_path = tmp_path_factory.mktemp("open_account")
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})
    goal = "Open a Money Market account for member 23499 with an initial deposit of $40.00"
    with open_session("discover", settings=settings, policy=policy, runs_dir=tmp_path / "d", echo=False,
                      username="demo", password="demo123", approver=lambda _r: True) as s:
        return record(run_discovery(s, load_mock(goal), goal), s.logger.run_id, s.settings, s.guard,
                      s.logger.masker, recipes_dir=tmp_path / "recipes").recipe


def test_risky_step_rejected(run, open_account_recipe):
    inputs = {"member_id": "12399", "account_type": "Money Market", "initial_deposit": "10.00"}
    result, _ = run(open_account_recipe, inputs, approve=False)
    r = result.run_result
    assert r.status == RunStatus.REJECTED_BY_OPERATOR and r.approvals[0].startswith("rejected")


def test_risky_step_approved_returns_confirmation(run, open_account_recipe):
    inputs = {"member_id": "45678", "account_type": "Money Market", "initial_deposit": "10.00"}
    result, _ = run(open_account_recipe, inputs, approve=True)
    r = result.run_result
    assert r.status == RunStatus.SUCCESS and str(r.outputs["confirmation_number"]).startswith("CNF-")


def test_existing_account_is_a_business_outcome(run, open_account_recipe):
    inputs = {"member_id": "45678", "account_type": "Savings", "initial_deposit": "10.00"}
    result, _ = run(open_account_recipe, inputs, approve=True)
    assert result.run_result.outcome_code == "ACCOUNT_ALREADY_EXISTS"


# ---------------------------------------------------------------- no LLM in replay
def test_replay_package_never_imports_the_llm():
    for path in (ROOT / "src" / "replay").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                assert not name.startswith(("src.agent", "openai", "anthropic")), f"{path.name} imports {name}"
    loaded = subprocess.run([sys.executable, "-c", "import sys, src.replay; "
                             "print(any(m.startswith(('src.agent','openai','anthropic')) for m in sys.modules))"],
                            cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert loaded == "False"
