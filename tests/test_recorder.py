"""Recorder: discovery run -> draft recipe. Placeholders, verified locators, checkpoints, safety."""

import json
import urllib.request
from urllib.parse import urlsplit

import pytest

from src.agent import MockLLM, load_mock, run_discovery
from src.handoff import open_session
from src.models import Recipe
from src.models.recipe import CssStrategy, LabelStrategy, NearTextStrategy, RoleStrategy
from src.models.settings import load_settings
from src.recorder import RecorderError, next_version, record
from src.safety import load_policy

LOOKUP = "Look up member 12345 and read their savings balance"


@pytest.fixture
def discover_and_record(bank_url, tmp_path):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url, "headless": True})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})
    recipes = tmp_path / "recipes"

    def _run(goal, llm=None, approve=False):
        with open_session("discover", settings=settings, policy=policy, runs_dir=tmp_path / "runs", echo=False,
                          username="demo", password="demo123", approver=lambda _req: approve) as s:
            result = run_discovery(s, llm or load_mock(goal), goal)
            return result, record(result, s.logger.run_id, s.settings, s.guard, s.logger.masker,
                                  recipes_dir=recipes)
    return _run


def test_lookup_recipe_matches_the_contract(discover_and_record):
    _, saved = discover_and_record(LOOKUP)
    r = saved.recipe
    assert saved.path.name == "member.lookup_savings_balance@1.0.0.json"
    assert (r.status, r.version, r.needs_review) == ("draft", "1.0.0", False)
    assert list(r.inputs) == ["member_id"] and r.inputs["member_id"].sensitive
    assert [s.action for s in r.steps] == ["navigate", "type", "click", "extract"]
    nav, typ, click, ext = r.steps
    assert nav.url == "/search" and nav.wait_for.any_of[0].text == "Member Search"
    assert typ.value == "{{member_id}}" and typ.expect_page.text_visible == "Member Search"
    assert [type(x) for x in typ.target.strategies] == [RoleStrategy, LabelStrategy, CssStrategy]
    assert click.wait_for.any_of[0].text == "Member Detail" and click.risk == "safe"
    assert [type(x) for x in ext.target.strategies] == [NearTextStrategy, CssStrategy]
    assert ext.save_as == "savings_balance" and ext.parse == "currency"
    assert r.success_check.text_visible == "Member Detail"
    assert {h.id for h in r.error_handlers} >= {"member_not_found", "notice_popup", "server_error"}
    assert "verified to match exactly this element" in typ.target.why


def test_saved_file_has_no_real_values(discover_and_record):
    _, saved = discover_and_record(LOOKUP)
    text = saved.path.read_text()
    assert "12345" not in text and "16,057" not in text and "16057" not in text
    Recipe.model_validate_json(text)  # the file on disk is itself a valid recipe


def test_rerecording_bumps_minor_version_and_keeps_old(discover_and_record):
    discover_and_record(LOOKUP)
    _, second = discover_and_record("Look up member 12346 and read their savings balance")
    assert second.recipe.version == "1.1.0"
    assert sorted(p.name for p in second.path.parent.iterdir()) == [
        "member.lookup_savings_balance@1.0.0.json", "member.lookup_savings_balance@1.1.0.json"]


def test_popup_dismissal_is_left_out(discover_and_record, bank_url):
    calls = load_mock(LOOKUP).calls
    ok = {"tool": "click", "args": {"role": "button", "name": "OK", "reason": "close the notice"}}

    class PopupAfterSearch(MockLLM):
        def decide(self, system, prompt, tools):
            if self._next == 2:  # just before the Search click, so the popup shows on the member page
                urllib.request.urlopen(bank_url + "/_admin/inject?error=popup")
            return super().decide(system, prompt, tools)

    llm = PopupAfterSearch(calls[:3] + [ok] + calls[3:])
    result, saved = discover_and_record(LOOKUP, llm=llm)
    assert [s.tool for s in result.successful_steps] == ["type_text", "click", "click", "extract"]
    assert [s.action for s in saved.recipe.steps] == ["navigate", "type", "click", "extract"]
    assert "Notice" in saved.skipped[0]


def test_open_account_recipe_marks_confirm_irreversible(discover_and_record):
    goal = "Open a Money Market account for member 23457 with an initial deposit of $250.00"
    _, saved = discover_and_record(goal, approve=True)
    r = saved.recipe
    assert set(r.inputs) == {"member_id", "account_type", "initial_deposit"}
    confirm = next(s for s in r.steps if s.target and s.target.strategies[0].name == "Confirm")
    assert confirm.risk == "irreversible"
    assert [s.risk for s in r.steps].count("irreversible") == 1  # Continue and Search stay safe
    values = {s.value for s in r.steps if s.value}
    assert values == {"{{member_id}}", "{{account_type}}", "{{initial_deposit}}"}
    assert "250" not in saved.path.read_text()


def test_update_phone_recipe(discover_and_record):
    _, saved = discover_and_record("Update the phone number of member 45679 to 555-222-0199", approve=True)
    text = saved.path.read_text()
    assert "555-222-0199" not in text and "{{new_phone}}" in text
    assert saved.recipe.outputs["confirmation_number"].type == "string"


def test_failed_run_is_never_recorded(discover_and_record):
    with pytest.raises(RecorderError, match="only successful runs"):
        discover_and_record(LOOKUP, llm=MockLLM(load_mock(LOOKUP).calls[:1] + [
            {"tool": "ask_human", "args": {"reason": "stuck"}}]))


def test_sensitive_constant_is_refused(discover_and_record):
    calls = json.loads(json.dumps(load_mock(LOOKUP).calls))
    # The model types a member ID it did not declare as an input: it would be stored as a constant.
    calls[0]["args"]["inputs"] = []
    with pytest.raises(RecorderError, match="would store a real value"):
        discover_and_record("Look up member 12345 and read their savings balance", llm=MockLLM(calls))


def test_next_version(tmp_path):
    assert next_version("a.b", tmp_path) == "1.0.0"
    (tmp_path / "a.b@1.0.0.json").write_text("{}")
    (tmp_path / "a.b@1.4.0.json").write_text("{}")
    assert next_version("a.b", tmp_path) == "1.5.0"
