"""Shared test fixtures: a private copy of the fake bank and a headless browser."""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TEST_PORT = 5051  # not 5050, so tests never touch the bank you run by hand


@pytest.fixture(scope="session")
def bank_url(tmp_path_factory) -> str:
    """Start the bank on a throwaway database, so every test session starts from the seed data.

    One bank serves the whole test session. Tests that CHANGE data (an approved Confirm) must
    use a member no other test relies on: 12348 (session), 23457 and 45679 (recorder),
    23499 and 45678 (replay), 56789 and 67890 (recovery).
    """
    db_path = tmp_path_factory.mktemp("bank") / "bank.db"
    env = {**os.environ, "BANK_DB_PATH": str(db_path), "BANK_PORT": str(TEST_PORT)}
    proc = subprocess.Popen([sys.executable, "bank_app/app.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{TEST_PORT}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url + "/login", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("test bank did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def inject(bank_url):
    """Trigger one of the bank's injected errors on the next page load."""
    def _inject(error: str) -> None:
        json.load(urllib.request.urlopen(f"{bank_url}/_admin/inject?error={error}"))
    yield _inject
    _inject("none")  # never leak a pending error into the next test


@pytest.fixture
def browser():
    from src.browser import Browser
    b = Browser(headless=True)
    yield b
    b.close()


@pytest.fixture
def logged_in(browser, bank_url):
    """A browser signed in as the teller. (Real login logic arrives in step 5.)"""
    browser.goto(bank_url + "/login")
    browser.page.fill("#f1", "demo")
    browser.page.fill("#f2", "demo123")
    browser.page.click("input[type=submit]")
    browser.page.wait_for_url("**/search")
    return browser


def pytest_collection_modifyitems(items):
    """Mark every test that needs the bank or a browser, so `pytest -m "not browser"` is fast."""
    for item in items:
        if {"bank_url", "browser"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.browser)
