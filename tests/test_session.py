"""The session gate: control, login, safety enforcement, approval, logging. Against the real bank."""

from urllib.parse import urlsplit

import pytest

from src.handoff import Action, ControlError, SessionError, open_session
from src.logs import read_log
from src.models import ControlState
from src.models.recipe import CssStrategy, LabelStrategy, NearTextStrategy, RoleStrategy
from src.models.settings import load_settings
from src.safety import load_policy

SEARCH_BOX = (RoleStrategy(by="role", role="textbox", name="Member ID"), CssStrategy(by="css", value="input[name=f1]"))
SEARCH = (RoleStrategy(by="role", role="button", name="Search"),)
CONFIRM = (RoleStrategy(by="role", role="button", name="Confirm"),)


@pytest.fixture
def make_session(bank_url, tmp_path):
    """open_session pointed at the private test bank, with a policy that allows its port."""
    settings = load_settings().model_copy(update={"bank_base_url": bank_url})
    base = load_policy()
    policy = base.model_copy(update={"allowed_domains": [urlsplit(bank_url).netloc]})

    def _make(**kw):
        kw.setdefault("username", "demo")
        kw.setdefault("password", "demo123")
        return open_session("replay", settings=settings, policy=policy, runs_dir=tmp_path, headless=True,
                            echo=False, **kw)
    return _make


def log_of(session):
    return read_log(session.logger.folder.log_path)


def search(session, member_id="12345"):
    session.perform(Action("navigate", url="/search"))
    session.perform(Action("type", SEARCH_BOX, value=member_id))
    return session.perform(Action("click", SEARCH))


# ---------------------------------------------------------------- login
def test_login_and_password_never_logged(make_session):
    with make_session() as s:
        assert s.browser.text_visible("Member Search")
        raw = s.logger.folder.log_path.read_text()
    assert "demo123" not in raw and '"action":"login"' in raw and '"outcome":"ok"' in raw


def test_wrong_password_raises_and_keeps_trace(make_session, tmp_path):
    with pytest.raises(SessionError, match="rejected the credentials"):
        with make_session(password="wrong-password"):
            pass
    trace = next(tmp_path.glob("*/trace.zip"))  # failed runs keep their trace
    assert trace.stat().st_size > 0


def test_successful_run_does_not_keep_trace(make_session, tmp_path):
    with make_session():
        pass
    assert not list(tmp_path.glob("*/trace.zip"))


# ---------------------------------------------------------------- actions through the gate
def test_search_and_extract(make_session):
    with make_session() as s:
        assert search(s).ok
        out = s.perform(Action("extract", (NearTextStrategy(by="near_text", anchor="Savings Balance:"),)))
        assert out.ok and out.text == "$16,057.78"
        entries = log_of(s)
    typed = next(e for e in entries if e.action == "type" and e.mode == "replay")
    assert typed.data["value"] == "***45"  # member id masked in the log


def test_not_found_is_reported_with_attempts(make_session):
    with make_session() as s:
        out = s.perform(Action("click", (RoleStrategy(by="role", role="button", name="Delete Everything"),)))
    assert out.status == "not_found" and out.attempts == ['role=button "Delete Everything": 0 matches']


def test_drift_warning_logged(make_session):
    with make_session() as s:
        renamed = (RoleStrategy(by="role", role="textbox", name="Member Number"), LabelStrategy(by="label", text="Member ID"))
        out = s.perform(Action("type", renamed, value="12345"))
        assert out.ok and out.drifted
        last = log_of(s)[-1]
    assert last.warnings == ['drift: backup locator label "Member ID" used']


def test_navigate_outside_allowlist_is_blocked(make_session):
    with make_session() as s:
        before = s.browser.current_url()
        out = s.perform(Action("navigate", url="/_admin/inject?error=server_error"))
        assert out.status == "blocked" and out.rule == "url_not_allowed"
        assert s.browser.current_url() == before  # nothing happened


def test_click_blocked_by_popup_fails_cleanly(make_session, inject):
    with make_session() as s:
        inject("popup")
        s.perform(Action("navigate", url="/search"))
        out = s.perform(Action("click", SEARCH))
    assert out.status == "failed" and "blocked by" in out.message


# ---------------------------------------------------------------- risky actions
def open_account_form(s, member_id="12347"):
    s.perform(Action("navigate", url=f"/member/{member_id}/open-account"))
    s.perform(Action("select", (RoleStrategy(by="role", role="combobox", name="Account Type"),), value="Money Market"))
    s.perform(Action("type", (LabelStrategy(by="label", text="Initial Deposit"),), value="250.00"))
    s.perform(Action("click", (RoleStrategy(by="role", role="button", name="Continue"),)))


def test_risky_click_rejected_by_default_and_nothing_changes(make_session):
    with make_session() as s:
        open_account_form(s)
        out = s.perform(Action("click", CONFIRM, step=7))
        assert out.status == "rejected"
        assert s.browser.text_visible("Confirm New Account")  # still on the review page
        entries = log_of(s)
    assert [e.data.get("to") for e in entries if e.action == "control_change"] == ["PAUSED", "AUTOMATION"]
    approval = next(e for e in entries if e.action == "approval")
    assert approval.outcome == "rejected" and approval.controller == ControlState.AUTOMATION


def test_risky_click_runs_when_approved_and_request_is_masked(make_session):
    seen = []
    with make_session(approver=lambda req: seen.append(req) or True) as s:
        open_account_form(s, member_id="12348")
        out = s.perform(Action("click", CONFIRM, step=7))
        assert out.ok and s.browser.text_visible("account opened successfully")
    req = seen[0]
    assert req.step == 7 and 'button "Confirm"' in req.action_description and "***48" in req.action_description
    assert "Money Market" in req.page_summary and "250.00" not in req.page_summary  # amount masked


def test_backup_locator_cannot_hide_a_risky_button(make_session):
    with make_session() as s:
        open_account_form(s, member_id="12399")
        # The role strategy is renamed (0 matches) so CSS matches, but the declared name still says Confirm.
        sneaky = (RoleStrategy(by="role", role="button", name="Confirm Now"), CssStrategy(by="css", value="input[value=Confirm]"))
        out = s.perform(Action("click", sneaky))
    assert out.status == "rejected"


# ---------------------------------------------------------------- control
def test_automation_cannot_act_while_human_has_control(make_session):
    with make_session() as s:
        s.set_control(ControlState.HUMAN, "operator took over")
        with pytest.raises(ControlError, match="control is HUMAN"):
            s.perform(Action("navigate", url="/search"))
        s.set_control(ControlState.AUTOMATION, "operator resumed")
        assert s.perform(Action("navigate", url="/search")).ok
        changes = [e for e in log_of(s) if e.action == "control_change"]
    assert [(c.data["from"], c.data["to"]) for c in changes] == [("AUTOMATION", "HUMAN"), ("HUMAN", "AUTOMATION")]
