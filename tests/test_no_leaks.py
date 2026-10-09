"""After real runs (discovery, replay, failure, takeover), the run folders must not contain the
member's real data. Screenshots and the failure accessibility tree are a documented limit: they
are kept local and the data is fake (see REPORT, Safety)."""

import json
import sqlite3
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import run
from src.handoff import ScriptedOperator
from src.models.settings import load_settings
from src.safety import load_policy

MEMBER = "12345"
SCANNED = ("log.jsonl", "result.json", "intervention_*.json")


def sensitive_values(db_path: Path) -> list[str]:
    """The real values the bank holds for this member, in the forms the UI shows them."""
    db = sqlite3.connect(db_path)
    name, address, phone = db.execute("SELECT name, address, phone FROM members WHERE member_id = ?",
                                      (MEMBER,)).fetchone()
    values = [MEMBER, name, address, phone]
    for number, cents in db.execute("SELECT account_number, balance_cents FROM accounts WHERE member_id = ?",
                                    (MEMBER,)):
        values += [number, f"{cents / 100:,.2f}", f"{cents / 100:.2f}"]
    return values


@pytest.fixture
def ctx(bank_url, tmp_path):
    settings = load_settings().model_copy(update={"bank_base_url": bank_url})
    policy = load_policy().model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})
    return run.Context(settings=settings, policy=policy, recipes_dir=tmp_path / "recipes", runs_dir=tmp_path / "runs",
                       interactive=False, headless=True)


def test_run_folders_contain_no_real_member_data(ctx, bank_url, tmp_path_factory):
    goal = f"Look up member {MEMBER} and read their savings balance"
    run.main(["discover", goal, "--mock"], ctx)                                     # LLM discovery
    run.main(["approve", "member.lookup_savings_balance", "--yes"], ctx)
    run.main(["ask", f"What's the savings balance for member {MEMBER}?", "--mock"], ctx)  # recipe replay

    # a hard failure (with evidence) handed to a human who searches again and resumes
    def human(browser):
        browser.page.click("text=Member Search")
        browser.settle(5)
        browser.page.fill("#f1", MEMBER)
        browser.page.click("input[value=Search]")
        browser.settle(5)
    ctx.operator = ScriptedOperator(["resume"], human=human)
    urllib.request.urlopen(f"{bank_url}/_admin/inject?error=none")
    run.main(["replay", "--recipe", "member.lookup_savings_balance", "--input", f"member_id={MEMBER}",
              "--inject", "server_error", "--at-step", "3"], ctx)

    results = [json.loads(p.read_text()) for p in ctx.runs_dir.glob("*/result.json")]
    assert {r["status"] for r in results} == {"SUCCESS"} and len(results) == 3
    assert any(r["human_interventions"] for r in results)  # the takeover really happened

    db_path = next(Path(tmp_path_factory.getbasetemp()).glob("bank*/bank.db"))
    secrets = sensitive_values(db_path)
    assert len(secrets) >= 8 and "Alice Fernwood" in secrets  # the scan really looks for something
    leaks = []
    for pattern in SCANNED:
        for path in ctx.runs_dir.glob(f"*/{pattern}"):
            text = path.read_text()
            leaks += [f"{path.parent.name}/{path.name}: {v!r}" for v in secrets if v in text]
    assert leaks == []

    recipe_text = next(ctx.recipes_dir.glob("*.json")).read_text()
    assert not [v for v in secrets if v in recipe_text]
