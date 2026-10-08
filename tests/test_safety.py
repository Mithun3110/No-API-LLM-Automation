"""Safety rules are enforced in code, so each one is tested directly."""

import pytest

from src.safety import Masker, ProposedAction, SafetyGuard, load_policy

BANK = "http://localhost:5050"


@pytest.fixture(scope="module")
def guard() -> SafetyGuard:
    return SafetyGuard(load_policy())  # the real config/policy.json


def act(action="click", page=BANK + "/search", **kw) -> ProposedAction:
    return ProposedAction(action=action, page_url=page, **kw)


# ---------------------------------------------------------------- allowlist
@pytest.mark.parametrize("url", [
    BANK + "/search", BANK + "/members?q=123", BANK + "/member/12345", BANK + "/member/12345/transactions",
    "http://127.0.0.1:5050/login",
])
def test_bank_pages_allowed(guard, url):
    assert guard.check(act("navigate", target_url=url)).allowed


@pytest.mark.parametrize("url, rule_text", [
    ("https://evil.example.com/search", "domain"),
    ("http://localhost:5051/search", "domain"),           # same host, different port
    (BANK + "/_admin/inject?error=popup", "path"),         # test switch is not part of the bank
    (BANK + "/logout", "path"),
    (BANK + "/member/../_admin/inject", "path"),           # path traversal
    ("javascript:alert(1)", "scheme"),
    ("file:///etc/passwd", "scheme"),
])
def test_outside_urls_blocked(guard, url, rule_text):
    d = guard.check(act("navigate", target_url=url))
    assert d.verdict == "block" and d.rule == "url_not_allowed" and rule_text in d.reason


def test_acting_on_a_page_outside_allowlist_is_blocked(guard):
    d = guard.check(act("click", page="https://evil.example.com/", target_role="button", target_name="Search"))
    assert d.verdict == "block" and d.rule == "page_not_allowed"


def test_unknown_action_blocked(guard):
    assert guard.check(act("download")).rule == "action_not_allowed"


def test_reading_and_typing_are_free(guard):
    for a in ("type", "select", "extract", "wait"):
        assert guard.check(act(a, target_role="textbox", target_name="Member ID")).allowed


# ---------------------------------------------------------------- risky actions
@pytest.mark.parametrize("name", ["Confirm", "Transfer", "Close Account", "confirm", "Confirm Transfer"])
def test_data_changing_buttons_need_approval(guard, name):
    d = guard.check(act(target_role="button", target_name=name))
    assert d.verdict == "needs_approval" and d.rule == "risky_button"


@pytest.mark.parametrize("role, name", [
    ("button", "Search"), ("button", "Continue"), ("button", "OK"), ("button", "Sign On"),
    ("link", "Update Member Info"),     # a link that only opens a form
    ("button", "Unconfirmed items"),    # whole-word match only
])
def test_safe_clicks_allowed(guard, role, name):
    assert guard.check(act(target_role=role, target_name=name)).allowed


def test_irreversible_step_needs_approval_whatever_its_name(guard):
    d = guard.check(act(target_role="button", target_name="Go", step_risk="irreversible"))
    assert d.verdict == "needs_approval"


def test_unknown_role_treated_conservatively(guard):
    assert guard.check(act(target_role=None, target_name="Confirm")).verdict == "needs_approval"


def test_recovery_may_never_do_risky_clicks(guard):
    d = guard.check(act(target_role="button", target_name="Confirm"), allow_risky=False)
    assert d.verdict == "block" and d.rule == "risky_in_recovery"


# ---------------------------------------------------------------- masking
@pytest.fixture
def masker() -> Masker:
    return Masker(load_policy().mask_fields, secrets=["demo123"])


def test_mask_by_field_name(masker):
    data = {"member_id": "12345", "savings_balance": "16057.78", "new_phone": "555-222-0199",
            "name": "Alice Fernwood", "address": "2625 Willow Way", "password": "x",
            "account_number": "1234501381", "initial_deposit": "500", "step": 3, "recipe_id": "member.lookup"}
    assert masker.mask(data) == {
        "member_id": "***45", "savings_balance": "***", "new_phone": "***", "name": "***", "address": "***",
        "password": "***", "account_number": "***", "initial_deposit": "***", "step": 3, "recipe_id": "member.lookup"}


def test_mask_patterns_in_free_text(masker):
    text = "Member 12345 has $16,057.78; phone 555-359-0127; account 1234501381"
    assert masker.mask_text(text) == "Member ***45 has $***; phone ***; account ***"


def test_mask_known_values_in_free_text(masker):
    masker.add_value("Alice Fernwood")
    masker.add_value("12399", field="member_id")
    assert masker.mask_text("Typed demo123 for Alice Fernwood (member 12399)") == "Typed *** for *** (member ***99)"


def test_mask_nested_data(masker):
    assert masker.mask({"outputs": {"savings_balance": 1.5}, "list": ["555-111-2222"]}) == \
        {"outputs": {"savings_balance": "***"}, "list": ["***"]}


def test_masking_leaves_structure_alone(masker):
    assert masker.mask_text('step 3: click role=button "Search"') == 'step 3: click role=button "Search"'
