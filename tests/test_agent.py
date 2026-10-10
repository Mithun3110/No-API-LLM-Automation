"""Discovery loop with scripted LLMs against the real bank: success, every stop condition, feedback."""

import json
from decimal import Decimal
from urllib.parse import urlsplit

import pytest

from src.agent import MockLLM, load_mock, run_discovery
from src.agent.llm import LLMError, make_llm
from src.handoff import open_session
from src.logs import read_log
from src.models import RunStatus
from src.models.settings import load_settings
from src.models.values import ParseError, parse_value
from src.safety import load_policy

GOAL = "Look up member 12345 and read their savings balance"
TASK = {"tool": "define_task", "args": {
    "recipe_id": "member.lookup_savings_balance", "name": "Lookup", "description": "Read savings balance.",
    "inputs": [{"name": "member_id", "type": "string", "value": "12345", "pattern": "^\\d{5}$"}],
    "outputs": [{"name": "savings_balance", "type": "currency"}]}}
TYPE_ID = {"tool": "type_text", "args": {"name": "Member ID", "text": "12345", "reason": "enter id"}}
SEARCH = {"tool": "click", "args": {"role": "button", "name": "Search", "reason": "search"}}
EXTRACT = {"tool": "extract", "args": {"label": "Savings Balance:", "output": "savings_balance", "parse": "currency",
                                       "reason": "read"}}
DONE = {"tool": "done", "args": {"success": True, "summary": "ok"}}


@pytest.fixture
def discover(bank_url, tmp_path):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True, "step_limit": 6})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})

    def _run(calls, goal=GOAL, **kw):
        """calls: a list of scripted tool calls, or a ready-made (mock) LLM."""
        llm = calls if hasattr(calls, "decide") else MockLLM(calls)
        with open_session("discover", settings=settings, policy=policy, runs_dir=tmp_path, echo=False,
                          username="demo", password="demo123", **kw) as s:
            return run_discovery(s, llm, goal), s
    return _run


# ---------------------------------------------------------------- success
def test_scripted_discovery_succeeds(discover):
    result, s = discover([TASK, TYPE_ID, SEARCH, EXTRACT, DONE])
    assert result.outcome == "success" and result.outputs == {"savings_balance": Decimal("16057.78")}
    assert [st.tool for st in result.successful_steps] == ["type_text", "click", "extract"]
    assert result.successful_steps[1].heading_after == "Member Detail"  # what appeared, for the recorder
    saved = json.loads(s.logger.folder.result_path.read_text())
    assert saved["status"] == "SUCCESS" and saved["outputs"] == {"savings_balance": "***"}
    raw_log = s.logger.folder.log_path.read_text()
    assert "12345" not in raw_log and "16,057.78" not in raw_log  # input and output masked


def test_mock_script_file_works_for_another_member(discover):
    goal = "Look up member 12346 and read their savings balance"
    result, _ = discover(load_mock(goal).calls, goal=goal)
    assert result.outcome == "success" and result.outputs["savings_balance"] == Decimal("23938.88")


# ---------------------------------------------------------------- stop conditions
def test_ask_human_stops_as_stuck(discover):
    result, _ = discover([TASK, {"tool": "ask_human", "args": {"reason": "not sure"}}])
    assert result.outcome == "stuck" and result.stop_reason == "agent_asked"
    assert result.run_result.status == RunStatus.FAILED and result.run_result.failure.evidence  # screenshot


def test_same_failed_action_twice_is_stuck(discover):
    missing = {"tool": "click", "args": {"role": "button", "name": "Find Member", "reason": "x"}}
    result, _ = discover([TASK, missing, missing])
    assert result.stop_reason == "repeated_failure" and [st.status for st in result.steps] == ["not_found"] * 2


def test_click_that_changes_nothing_is_stuck(discover):
    # The "Teller: Demo Teller" area has no effect... use a link to the page we are already on
    same_page = {"tool": "click", "args": {"role": "link", "name": "Member Search", "reason": "x"}}
    result, _ = discover([TASK, same_page])
    assert result.stop_reason == "page_unchanged"


def test_step_limit(discover):
    wait = {"tool": "wait", "args": {"seconds": 1, "reason": "x"}}
    result, _ = discover([TASK] + [wait] * 10)
    assert result.stop_reason == "step_limit" and len(result.steps) == 6


def test_not_achievable_goal(discover):
    calls = [{**TASK, "args": {**TASK["args"], "inputs": [{**TASK["args"]["inputs"][0], "value": "99999"}]}},
             {**TYPE_ID, "args": {**TYPE_ID["args"], "text": "99999"}}, SEARCH,
             {"tool": "done", "args": {"success": False, "summary": "No member found"}}]
    result, _ = discover(calls, goal="Look up member 99999 and read their savings balance")
    assert result.outcome == "not_achievable" and not result.outputs


# ---------------------------------------------------------------- feedback instead of crashes
def test_invalid_arguments_become_feedback(discover):
    bad = {"tool": "click", "args": {"role": "spaceship", "name": "Search", "reason": "x"}}
    result, _ = discover([TASK, bad, SEARCH, TYPE_ID, SEARCH, EXTRACT, DONE])
    assert result.steps[0].status == "invalid" and "role" in result.steps[0].message
    assert result.outcome == "success"  # recovered on the next turn


def test_done_before_outputs_is_refused(discover):
    result, _ = discover([TASK, DONE, TYPE_ID, SEARCH, EXTRACT, DONE])
    assert result.steps[0].status == "invalid" and "savings_balance" in result.steps[0].message
    assert result.outcome == "success"


def test_blocked_navigation_is_feedback(discover):
    admin = {"tool": "navigate", "args": {"path": "/_admin/inject?error=popup", "reason": "x"}}
    result, _ = discover([TASK, admin, TYPE_ID, SEARCH, EXTRACT, DONE])
    assert result.steps[0].status == "blocked" and result.outcome == "success"


def test_risky_click_rejected_stops_discovery(discover):
    task = {"tool": "define_task", "args": {
        "recipe_id": "member.open_sub_account", "name": "Open", "description": "Open account.",
        "inputs": [{"name": "member_id", "type": "string", "value": "12347"}],
        "outputs": [{"name": "confirmation_number", "type": "string", "sensitive": False}]}}
    calls = [task,
             {"tool": "navigate", "args": {"path": "/member/12347/open-account", "reason": "x"}},
             {"tool": "select_option", "args": {"name": "Account Type", "option": "Money Market", "reason": "x"}},
             {"tool": "type_text", "args": {"name": "Initial Deposit", "text": "100", "reason": "x"}},
             {"tool": "click", "args": {"role": "button", "name": "Continue", "reason": "x"}},
             {"tool": "click", "args": {"role": "button", "name": "Confirm", "reason": "x"}}]
    result, _ = discover(calls, goal="Open a Money Market account for member 12347 with 100")
    assert result.outcome == "rejected" and result.run_result.status == RunStatus.REJECTED_BY_OPERATOR


def test_task_input_must_come_from_goal(discover):
    made_up = {**TASK, "args": {**TASK["args"], "inputs": [{**TASK["args"]["inputs"][0], "value": "54321"}]}}
    result, s = discover([made_up] * 3)
    assert result.outcome == "llm_error" and "not in the goal" in result.detail
    assert [e.outcome for e in read_log(s.logger.folder.log_path)].count("task_rejected") == 2


def test_rejected_task_is_retried_with_the_reason(discover):
    """Seen with Groq: an input value sent as a number. The model is told why, and fixes it."""
    bad = {**TASK, "args": {**TASK["args"], "inputs": [{"name": "member_id", "type": "string"}]}}  # no value

    class Recording(MockLLM):
        prompts: list[str] = []

        def decide(self, system, prompt, tools):
            Recording.prompts.append(prompt)
            return super().decide(system, prompt, tools)

    result, _ = discover(Recording([bad, TASK, TYPE_ID, SEARCH, EXTRACT, DONE]))
    assert result.outcome == "success"
    assert "was rejected" in Recording.prompts[1] and "value" in Recording.prompts[1]


# ---------------------------------------------------------------- units
def test_parse_value():
    assert parse_value("$16,057.78", "currency") == Decimal("16057.78")
    assert parse_value("-$12.00", "currency") == Decimal("-12.00")
    assert parse_value(" CNF-100001 ", "text") == "CNF-100001"
    with pytest.raises(ParseError):
        parse_value("N/A", "currency")


def test_make_llm_needs_a_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        make_llm(load_settings())


def test_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("LLM_PROVIDER", "carrier-pigeon")
    with pytest.raises(LLMError, match="unknown LLM_PROVIDER"):
        make_llm(load_settings())
