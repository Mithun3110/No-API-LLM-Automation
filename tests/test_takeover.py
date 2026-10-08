"""Human takeover on the same live session: request, control states, recording, resume, abort."""

import json
import urllib.request
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from src.agent import MockLLM, run_discovery
from src.handoff import ScriptedOperator, open_session
from src.logs import read_log
from src.models import InterventionRequest, Recipe, RunStatus
from src.models.settings import load_settings
from src.recorder import record
from src.replay import replay
from src.safety import load_policy

FIXTURE = Path(__file__).parent / "fixtures" / "member.lookup_savings_balance@1.0.0.json"


@pytest.fixture
def session_for(bank_url, tmp_path):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})

    def _open(mode, operator):
        return open_session(mode, settings=settings, policy=policy, runs_dir=tmp_path / "runs", echo=False,
                            username="demo", password="demo123", approver=operator.approve)
    return _open


def recipe(**changes) -> Recipe:
    data = json.loads(FIXTURE.read_text())
    for step_no, field, value in changes.get("step_changes", []):
        data["steps"][step_no - 1][field] = value
    return Recipe.model_validate(data)


def server_error_at_step_3(session, bank_url):
    original = session.perform

    def perform(action):
        if action.step == 3 and action.kind == "click" and not getattr(perform, "done", False):
            perform.done = True
            urllib.request.urlopen(f"{bank_url}/_admin/inject?error=server_error")
        return original(action)
    session.perform = perform


def human_searches(member_id):
    """The person in control: go back to search, type the ID, click Search (real DOM events)."""
    def act(browser):
        browser.page.click("text=Member Search")
        browser.settle(5)
        browser.page.fill("#f1", member_id)
        browser.page.click("input[value=Search]")
        browser.settle(5)
    return act


def entries(session):
    return read_log(session.logger.folder.log_path)


# ---------------------------------------------------------------- replay
def test_replay_hard_failure_human_fixes_it_and_replay_resumes(session_for, bank_url):
    op = ScriptedOperator(["resume"], human=human_searches("12345"))
    with session_for("replay", op) as s:
        server_error_at_step_3(s, bank_url)
        result = replay(recipe(), {"member_id": "12345"}, s, op)
        log = entries(s)
    r = result.run_result
    assert r.status == RunStatus.SUCCESS and r.outputs == {"savings_balance": Decimal("16057.78")}

    # the request carried full context, and was saved in the run folder
    req = op.requests[0]
    assert (req.recipe_id, req.step, req.reason) == ("member.lookup_savings_balance", 3, "hard_failure")
    assert "server_error" in req.detail and Path(req.screenshot_path).exists()
    saved = InterventionRequest.model_validate_json(
        (s.logger.folder.path / "intervention_1.json").read_text())
    assert saved.step == 3

    # who was in control, in order
    changes = [e.data["to"] for e in log if e.action == "control_change"]
    assert changes == ["PAUSED", "HUMAN", "AUTOMATION"]

    # what the human did was recorded, masked, and replay resumed at the matching page
    human = [e for e in log if e.mode == "human" and e.outcome == "recorded"]
    assert [e.action for e in human] == ["click", "type", "click"]
    assert "***45" in human[1].target and "12345" not in json.dumps([e.model_dump(mode="json") for e in log])
    assert any(e.outcome == "resumed" and e.step == 4 for e in log)
    assert r.human_interventions and "-> resume" in r.human_interventions[0]


def test_resume_goes_back_to_the_first_step_on_that_page(session_for):
    # Extract fails (label renamed). The human goes to Member Search. Steps 2 (type) and 3 (click)
    # were both recorded on that page, so replay resumes at 2, not at 3 with an empty box.
    bad = recipe(step_changes=[(4, "target", {"strategies": [{"by": "near_text", "anchor": "Savings Bal:"}],
                                              "why": "renamed"})])
    op = ScriptedOperator(["resume", "abort"],
                          human=lambda b: (b.page.click("text=Member Search"), b.settle(5)))
    with session_for("replay", op) as s:
        result = replay(bad, {"member_id": "12345"}, s, op)
        log = entries(s)
    assert [e.step for e in log if e.outcome == "resumed"] == [2]
    assert result.run_result.status == RunStatus.ABORTED_BY_OPERATOR


def test_abort(session_for, bank_url):
    op = ScriptedOperator(["abort"])
    with session_for("replay", op) as s:
        server_error_at_step_3(s, bank_url)
        result = replay(recipe(), {"member_id": "12345"}, s, op)
        assert s.control.value == "PAUSED"  # automation never takes control back after an abort
    assert result.run_result.status == RunStatus.ABORTED_BY_OPERATOR


def test_no_matching_page_after_resume_escalates(session_for, bank_url):
    # The human resumes while still on the error page, three times: no step matches that page.
    op = ScriptedOperator(["resume", "resume", "resume"])
    with session_for("replay", op) as s:
        server_error_at_step_3(s, bank_url)
        result = replay(recipe(), {"member_id": "12345"}, s, op)
    r = result.run_result
    assert r.status == RunStatus.ESCALATED and r.failure.error_type == "no_resume_point"
    assert len(op.requests) == 3  # bounded by settings.max_takeovers


def test_without_operator_hard_failure_is_returned(session_for, bank_url):
    op = ScriptedOperator([])
    with session_for("replay", op) as s:
        server_error_at_step_3(s, bank_url)
        result = replay(recipe(), {"member_id": "12345"}, s)  # no operator passed
    assert result.run_result.status == RunStatus.FAILED and not op.requests


# ---------------------------------------------------------------- discovery
TASK = {"tool": "define_task", "args": {
    "recipe_id": "member.lookup_savings_balance", "name": "Lookup", "description": "Read savings balance.",
    "inputs": [{"name": "member_id", "type": "string", "value": "12345", "pattern": "^\\d{5}$"}],
    "outputs": [{"name": "savings_balance", "type": "currency"}]}}
EXTRACT = {"tool": "extract", "args": {"label": "Savings Balance:", "output": "savings_balance", "parse": "currency",
                                       "reason": "read"}}
DONE = {"tool": "done", "args": {"success": True, "summary": "ok"}}


def test_discovery_stuck_human_helps_and_their_steps_enter_the_recipe(session_for, tmp_path):
    goal = "Look up member 12345 and read their savings balance"
    llm = MockLLM([TASK, {"tool": "ask_human", "args": {"reason": "I cannot find the search box"}}, EXTRACT, DONE])
    op = ScriptedOperator(["resume"], human=human_searches("12345"))
    with session_for("discover", op) as s:
        result = run_discovery(s, llm, goal, op)
        saved = record(result, s.logger.run_id, s.settings, s.guard, s.logger.masker, recipes_dir=tmp_path / "rec")
    assert result.outcome == "success" and op.requests[0].reason == "agent_asked"
    r = saved.recipe
    assert r.needs_review  # human steps were not chosen or verified by the agent
    sources = [(st.action, st.source) for st in r.steps]
    assert ("type", "human") in sources and ("extract", "agent") in sources
    typed = next(st for st in r.steps if st.action == "type")
    assert typed.value == "{{member_id}}" and typed.expect_page.heading == "Member Search"
    assert "12345" not in saved.path.read_text()


def test_discovery_abort(session_for):
    llm = MockLLM([TASK, {"tool": "ask_human", "args": {"reason": "unsure"}}])
    op = ScriptedOperator(["abort"])
    with session_for("discover", op) as s:
        result = run_discovery(s, llm, "Look up member 12345 and read their savings balance", op)
    assert result.run_result.status == RunStatus.ABORTED_BY_OPERATOR


# ---------------------------------------------------------------- recording details
def test_recorder_never_sends_passwords(browser, bank_url):
    got = []
    browser.goto(bank_url + "/login")
    browser.start_human_recording(got.append)
    browser.page.fill("#f2", "demo123")
    browser.page.click("input[type=submit]")
    browser.wait(0.5)
    assert any(g["kind"] == "type" and g["value"] == "***" for g in got)
    assert "demo123" not in json.dumps(got)


def test_actions_are_ignored_unless_a_human_is_in_control(browser, bank_url):
    got = []
    browser.goto(bank_url + "/login")
    browser.start_human_recording(got.append)
    browser.stop_human_recording()
    browser.page.fill("#f1", "demo")
    browser.wait(0.5)
    assert got == []
