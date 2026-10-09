"""The config files must match the real bank, or rules silently never fire."""

import json
import re
from pathlib import Path

import pytest

from src.models import Recipe
from src.safety import ProposedAction, SafetyGuard, load_policy

ROOT = Path(__file__).parent.parent
BANK = ROOT / "bank_app"
RULES = json.loads((ROOT / "config" / "app_rules.json").read_text())


def bank_text() -> str:
    """Every message the bank can show: templates plus messages written in app.py."""
    return "\n".join(p.read_text() for p in [BANK / "app.py", *(BANK / "templates").glob("*.html")])


@pytest.mark.parametrize("handler", [h for h in RULES["error_handlers"] if h["when"].get("text")],
                         ids=lambda h: h["id"])
def test_every_text_rule_matches_a_real_bank_message(handler):
    assert handler["when"]["text"] in bank_text(), f"'{handler['when']['text']}' never appears in the bank"


def test_dialog_rule_matches_the_real_dialog():
    dialogs = [h["when"]["dialog"] for h in RULES["error_handlers"] if h["when"].get("dialog")]
    base = (BANK / "templates" / "base.html").read_text()
    for name in dialogs:
        assert 'role="dialog"' in base and f">{name}<" in base


def test_rules_cover_every_injectable_error():
    kinds = {h["id"] for h in RULES["error_handlers"]}
    assert {"notice_popup", "slow_page", "session_expired", "server_error"} <= kinds
    assert {"member_not_found", "permission_denied", "account_already_exists", "validation_error"} <= kinds


def test_rules_are_valid_inside_a_recipe():
    data = json.loads((ROOT / "tests" / "fixtures" / "member.lookup_savings_balance@1.0.0.json").read_text())
    data["error_handlers"] = RULES["error_handlers"]
    data["outcomes"] = {"SUCCESS": "ok", **RULES["outcomes"]}
    Recipe.model_validate(data)


def test_allowlist_covers_every_bank_page_except_logout_and_admin():
    guard = SafetyGuard(load_policy())
    routes = re.findall(r'@app\.(?:route|get|post)\("([^"]+)"', (BANK / "app.py").read_text())
    for route in routes:
        url = "http://localhost:5050" + re.sub(r"<(?:int:)?\w+>", "1", route)
        allowed = guard.url_problem(url) is None
        should_be = not route.startswith(("/_admin", "/logout"))
        assert allowed == should_be, f"{route}: allowed={allowed}"


def test_every_data_changing_button_in_the_bank_is_risky():
    guard = SafetyGuard(load_policy())
    buttons = set(re.findall(r'type="submit" value="([^"]+)"', bank_text()))
    changes_data = {"Confirm", "Transfer", "Close Account"}  # the buttons that commit a change
    for name in buttons:
        risky = guard.is_risky(ProposedAction("click", "", target_role="button", target_name=name))
        assert risky == (name in changes_data), f"button '{name}': risky={risky}"


def test_mock_scripts_are_valid():
    from src.agent.tools import DefineTask
    for path in (ROOT / "config" / "mock_scripts").glob("*.json"):
        script = json.loads(path.read_text())
        re.compile(script["goal_pattern"])
        assert script["calls"][0]["tool"] == "define_task" and script["calls"][-1]["tool"] == "done"
        names = set(re.compile(script["goal_pattern"]).groupindex)
        task = json.loads(re.sub(r"\{\{(\w+)\}\}", "x", json.dumps(script["calls"][0]["args"])))
        DefineTask.model_validate(task)
        assert names == {i["name"] for i in task["inputs"]}, f"{path.name}: pattern groups must be the inputs"
