"""The commands end to end (run.py) against the test bank, with the mock LLM and temp folders."""

import builtins
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import run
from src.agent.llm import ToolCall
from src.catalog import Catalog
from src.catalog.matcher import match_with_llm, recipe_tool
from src.models.settings import load_settings
from src.safety import load_policy

ROOT = Path(__file__).parent.parent
LOOKUP_GOAL = "Look up member 12345 and read their savings balance"
RECIPE_ID = "member.lookup_savings_balance"


@pytest.fixture
def ctx(bank_url, tmp_path):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})
    return run.Context(settings=settings, policy=policy, recipes_dir=tmp_path / "recipes",
                       runs_dir=tmp_path / "runs", interactive=False, headless=True)


def cli(ctx, *argv) -> int:
    return run.main(list(argv), ctx)


def last_result(ctx) -> dict:
    folder = max((ctx.runs_dir).iterdir(), key=lambda p: p.stat().st_mtime)
    return json.loads((folder / "result.json").read_text())


def answer(monkeypatch, *answers):
    """Simulate a person typing at the yes/no prompts."""
    queue = list(answers)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": queue.pop(0))


@pytest.fixture
def discovered(ctx):
    assert cli(ctx, "discover", LOOKUP_GOAL, "--mock") == 0
    return ctx


# ---------------------------------------------------------------- list, discover, approve
def test_list_empty_then_discover_creates_a_draft(ctx, capsys):
    cli(ctx, "list")
    assert "No recipes yet" in capsys.readouterr().out
    assert cli(ctx, "discover", LOOKUP_GOAL, "--mock") == 0
    cli(ctx, "list")
    out = capsys.readouterr().out
    assert f"{RECIPE_ID}  v1.0.0  (draft)" in out and "member_id (string)" in out


def test_approve_records_who_when_and_why(discovered, capsys):
    assert cli(discovered, "approve", RECIPE_ID, "--yes") == 0
    recipe = Catalog.load(discovered.recipes_dir).active(RECIPE_ID).recipe
    assert recipe.status == "approved" and recipe.approval.basis == "manual_review"
    assert "steps:" in capsys.readouterr().out  # the reviewer was shown the summary


def test_approve_needs_a_yes(discovered, monkeypatch):
    discovered.interactive = True
    answer(monkeypatch, "no")
    assert cli(discovered, "approve", RECIPE_ID) == 1
    assert Catalog.load(discovered.recipes_dir).active(RECIPE_ID).recipe.status == "draft"


# ---------------------------------------------------------------- strict replay
def test_strict_replay_refuses_a_draft(discovered):
    assert cli(discovered, "replay", "--recipe", RECIPE_ID, "--input", "member_id=12346") == 1
    r = last_result(discovered)
    assert r["status"] == "DRAFT_NOT_ALLOWED" and "approve it first or pass --allow-draft" in r["message"]


def test_strict_replay_with_allow_draft(discovered):
    assert cli(discovered, "replay", "--recipe", RECIPE_ID, "--input", "member_id=12346", "--allow-draft") == 0
    assert last_result(discovered)["status"] == "SUCCESS"


def test_strict_replay_by_request_after_approval(discovered, capsys):
    cli(discovered, "approve", RECIPE_ID, "--yes")
    assert cli(discovered, "replay", "What's the savings balance for member 12346?", "--mock") == 0
    assert "savings_balance = 23938.88" in capsys.readouterr().out


def test_strict_replay_no_match_never_discovers(discovered):
    assert cli(discovered, "replay", "Transfer $5 for member 12345", "--mock") == 1
    r = last_result(discovered)
    assert r["status"] == "NO_MATCHING_RECIPE" and r["mode"] == "replay"
    assert len(list(discovered.recipes_dir.iterdir())) == 1  # nothing new was discovered


def test_invalid_input_is_refused_before_any_browser(discovered):
    assert cli(discovered, "replay", "--recipe", RECIPE_ID, "--input", "member_id=12a45", "--allow-draft") == 1
    r = last_result(discovered)
    assert r["status"] == "INVALID_INPUT"
    folder = Path(r["log_file"]).parent
    assert not list(folder.glob("*.png")) and "navigate" not in Path(r["log_file"]).read_text()


def test_recipe_path_never_loads_an_llm(discovered):
    script = (f"import sys, run; from pathlib import Path; from urllib.parse import urlsplit\n"
              f"from src.models.settings import load_settings; from src.safety import load_policy\n"
              f"s = load_settings().model_copy(update={{'bank_base_url': '{discovered.settings.bank_base_url}'}})\n"
              f"p = load_policy().model_copy(update={{'allowed_domains': ['{urlsplit(discovered.settings.bank_base_url).netloc}']}})\n"
              f"ctx = run.Context(settings=s, policy=p, recipes_dir=Path(r'{discovered.recipes_dir}'), "
              f"runs_dir=Path(r'{discovered.runs_dir}'), interactive=False, headless=True)\n"
              f"code = run.main(['replay', '--recipe', '{RECIPE_ID}', '--input', 'member_id=12345', '--allow-draft'], ctx)\n"
              f"print('EXIT', code, 'LLM_LOADED', any(m.startswith(('src.agent', 'openai', 'anthropic')) for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True).stdout
    assert "EXIT 0 LLM_LOADED False" in out


# ---------------------------------------------------------------- ask
def test_ask_uses_an_approved_recipe(discovered, capsys):
    cli(discovered, "approve", RECIPE_ID, "--yes")
    assert cli(discovered, "ask", "What's the savings balance for member 23456?", "--mock") == 0
    r = last_result(discovered)
    assert r["status"] == "SUCCESS" and r["mode"] == "replay" and r["recipe_id"] == RECIPE_ID
    assert len(list(discovered.recipes_dir.iterdir())) == 1  # no discovery happened


def test_ask_business_outcome_is_a_valid_answer(discovered):
    cli(discovered, "approve", RECIPE_ID, "--yes")
    assert cli(discovered, "ask", "What's the savings balance for member 99999?", "--mock") == 0
    assert last_result(discovered)["outcome_code"] == "MEMBER_NOT_FOUND"


def test_ask_without_a_recipe_discovers_one(ctx):
    assert cli(ctx, "ask", LOOKUP_GOAL, "--mock") == 0
    assert Catalog.load(ctx.recipes_dir).active(RECIPE_ID).recipe.status == "draft"


def test_ask_draft_needs_confirmation_and_success_offers_approval(discovered, monkeypatch):
    discovered.interactive = True
    discovered.operator = None
    monkeypatch.setattr(run, "wait_before_closing", lambda *a: None)
    answer(monkeypatch, "yes", "yes")  # run the draft? approve it now?
    assert cli(discovered, "ask", "What's the savings balance for member 12346?", "--mock", "--no-human") == 0
    recipe = Catalog.load(discovered.recipes_dir).active(RECIPE_ID).recipe
    assert recipe.status == "approved" and recipe.approval.basis.startswith("successful_replay:")


def test_ask_draft_refused_without_a_person(discovered):
    assert cli(discovered, "ask", "What's the savings balance for member 12346?", "--mock") == 1
    assert last_result(discovered)["status"] == "DRAFT_NOT_ALLOWED"


def test_needs_review_draft_is_never_quick_approved(discovered, monkeypatch, capsys):
    path = next(discovered.recipes_dir.iterdir())
    data = json.loads(path.read_text())
    data["needs_review"] = True
    path.write_text(json.dumps(data))
    discovered.interactive = True
    monkeypatch.setattr(run, "wait_before_closing", lambda *a: None)
    answer(monkeypatch, "yes")  # only the "run the draft?" question may be asked
    cli(discovered, "ask", "What's the savings balance for member 12346?", "--mock", "--no-human")
    assert "review it with `python run.py approve" in capsys.readouterr().out
    assert Catalog.load(discovered.recipes_dir).active(RECIPE_ID).recipe.status == "draft"


# ---------------------------------------------------------------- catalog details
def test_active_version_prefers_latest_approved(discovered):
    cli(discovered, "approve", RECIPE_ID, "--yes")                   # 1.0.0 approved
    cli(discovered, "discover", "Look up member 12346 and read their savings balance", "--mock")  # 1.1.0 draft
    catalog = Catalog.load(discovered.recipes_dir)
    assert [s.recipe.version for s in catalog.versions[RECIPE_ID]] == ["1.0.0", "1.1.0"]
    assert catalog.active(RECIPE_ID).recipe.version == "1.0.0"     # new draft is not active yet


def test_broken_recipe_file_is_reported_not_fatal(discovered, capsys):
    (discovered.recipes_dir / "member.broken@1.0.0.json").write_text("{}")
    cli(discovered, "list")
    out = capsys.readouterr().out
    assert RECIPE_ID in out and "skipped member.broken@1.0.0.json: invalid recipe" in out


def test_llm_matcher_offers_recipes_as_tools(discovered):
    catalog = Catalog.load(discovered.recipes_dir)

    class FakeLLM:
        model = "fake"
        seen = None

        def decide(self, system, prompt, tools):
            FakeLLM.seen = tools
            return ToolCall("member__lookup_savings_balance", {"member_id": "12346"})

    m = match_with_llm("balance of 12346", catalog, FakeLLM())
    assert m.found and m.inputs == {"member_id": "12346"}
    names = [t["name"] for t in FakeLLM.seen]
    assert names == ["member__lookup_savings_balance", "no_match"]
    assert "^\\d{5}$" in recipe_tool(catalog.active(RECIPE_ID))["parameters"]["properties"]["member_id"]["description"]


def test_ask_missing_value_does_not_start_discovery(discovered, monkeypatch):
    import src.catalog.matcher as matcher
    monkeypatch.setattr(matcher, "match_with_mock", lambda request, catalog: matcher.Match(
        None, reason="Missing required value: member_id", missing_value=True))
    assert cli(discovered, "ask", "What's the savings balance?", "--mock") == 1
    r = last_result(discovered)
    assert r["status"] == "INVALID_INPUT" and "member_id" in r["message"]
    assert len(list(discovered.recipes_dir.iterdir())) == 1  # no discovery, no new recipe


def test_result_says_whether_a_recipe_or_the_llm_answered(ctx):
    cli(ctx, "ask", LOOKUP_GOAL, "--mock")
    discovered = last_result(ctx)
    assert discovered["answered_by"] == "llm_discovery" and "decisions" in discovered["llm_used_for"][0]
    cli(ctx, "approve", RECIPE_ID, "--yes")
    cli(ctx, "ask", "What's the savings balance for member 23456?", "--mock")
    matched = last_result(ctx)
    assert matched["answered_by"] == "recipe" and matched["llm_used_for"][0].startswith("matching the request")
    cli(ctx, "replay", "--recipe", RECIPE_ID, "--input", "member_id=12346")
    pure = last_result(ctx)
    assert pure["answered_by"] == "recipe" and pure["llm_used_for"] == []


def test_only_ask_gets_llm_recovery(ctx):
    from types import SimpleNamespace
    from src.recovery import GiveUpRecoverer
    args = SimpleNamespace(mock=True)
    assert run.make_recoverer(args, ctx, None, "replay") is None          # strict: straight to a human
    assert isinstance(run.make_recoverer(args, ctx, None, "ask"), GiveUpRecoverer)


# ---------------------------------------------------------------- --target
def test_discover_without_target_uses_settings(ctx):
    assert cli(ctx, "discover", LOOKUP_GOAL, "--mock") == 0
    recipe = Catalog.load(ctx.recipes_dir).active(RECIPE_ID).recipe
    assert recipe.app.entry_url == ctx.settings.bank_base_url + ctx.settings.entry_path


def test_target_is_used_for_login_and_first_step_and_recorded(ctx, bank_url):
    target = bank_url + "/"  # start on a different entry page than the settings' /search
    assert cli(ctx, "discover", LOOKUP_GOAL, "--mock", "--target", target) == 0
    recipe = Catalog.load(ctx.recipes_dir).active(RECIPE_ID).recipe
    assert recipe.app.entry_url == target                      # where it was recorded
    assert recipe.steps[0].action == "navigate" and recipe.steps[0].url == "/"  # its start page
    folder = Path(last_result(ctx)["log_file"]).parent
    log = [json.loads(line) for line in (folder / "log.jsonl").read_text().splitlines()]
    navigations = [e["target"] for e in log if e["action"] == "navigate"]
    assert navigations[:2] == ["/login", "/"]  # login on the target's app, then its start page


@pytest.mark.parametrize("target, reason", [
    ("https://some-other-site.com/search", "domain"),  # domain outside the allowlist
    ("{bank}/_admin/inject", "path"),                  # the bank's own host, but a forbidden path
])
def test_target_outside_the_allowlist_is_refused(ctx, capsys, bank_url, target, reason):
    assert cli(ctx, "discover", LOOKUP_GOAL, "--mock", "--target", target.format(bank=bank_url)) == 1
    out = capsys.readouterr().out
    assert "outside the allowlist" in out and reason in out
    assert not ctx.runs_dir.exists() or not list(ctx.runs_dir.iterdir())  # nothing was started


def test_replay_has_no_target_option(ctx):
    with pytest.raises(SystemExit):
        cli(ctx, "replay", "--recipe", RECIPE_ID, "--input", "member_id=12345", "--target", "http://x/")


def test_ask_with_target_only_uses_recipes_of_that_app(discovered, bank_url, capsys):
    cli(discovered, "approve", RECIPE_ID, "--yes")
    # same app (the recipe was recorded on it): the recipe is replayed
    assert cli(discovered, "ask", "What's the savings balance for member 23456?", "--mock",
               "--target", bank_url + "/search") == 0
    assert last_result(discovered)["answered_by"] == "recipe"
    # another app (same bank server under a different host name, standing in for another
    # institution's instance): that recipe is NOT used; discovery runs for this app instead
    other = bank_url.replace("127.0.0.1", "localhost")
    discovered.policy = discovered.policy.model_copy(update={
        "allowed_domains": discovered.policy.allowed_domains + [urlsplit(other).netloc]})
    assert cli(discovered, "ask", "What's the savings balance for member 23456?", "--mock",
               "--target", other + "/search") == 0
    assert last_result(discovered)["answered_by"] == "llm_discovery"
