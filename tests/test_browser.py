"""Browser layer against the real fake bank: locating, uniqueness, drift, near_text, popups."""

import pytest

from src.browser import BrowserError
from src.models.recipe import (
    CssStrategy, LabelStrategy, NearTextStrategy, RoleStrategy, TextStrategy, WaitCondition,
)

SEARCH_BOX = [RoleStrategy(by="role", role="textbox", name="Member ID"),
              LabelStrategy(by="label", text="Member ID"),
              CssStrategy(by="css", value="input[name=f1]")]
SEARCH_BUTTON = [RoleStrategy(by="role", role="button", name="Search")]


def open_member(b, bank_url, member_id="12345"):
    b.goto(bank_url + "/search")
    b.type(b.find(SEARCH_BOX).element, member_id)
    b.click(b.find(SEARCH_BUTTON).element)
    b.settle(10)  # click returns before the next page arrives


def test_find_by_role_and_name_uses_first_strategy(logged_in):
    result = logged_in.find(SEARCH_BOX)
    assert result.found and result.strategy_index == 0 and not result.drifted


def test_exact_name_does_not_match_member_search_link(logged_in):
    # "Search" must not also match the "Member Search" nav link
    assert logged_in.find(SEARCH_BUTTON).found


def test_backup_strategy_is_reported_as_drift(logged_in):
    renamed = [RoleStrategy(by="role", role="textbox", name="Member Number")] + SEARCH_BOX[1:]
    result = logged_in.find(renamed)
    assert result.found and result.strategy_index == 1 and result.drifted
    assert result.attempts[0] == 'role=textbox "Member Number": 0 matches'


def test_ambiguous_strategy_is_skipped(logged_in, bank_url):
    open_member(logged_in, bank_url)
    # The balance text appears twice (summary + accounts table): never pick one at random.
    result = logged_in.find([TextStrategy(by="text", text="$16,057.78")])
    assert not result.found
    assert result.attempts == ['text "$16,057.78": 2 matches']


def test_near_text_reads_value_next_to_label(logged_in, bank_url):
    open_member(logged_in, bank_url)
    result = logged_in.find([NearTextStrategy(by="near_text", anchor="Savings Balance:")])
    assert result.found
    assert logged_in.read_text(result.element) == "$16,057.78"


def test_near_text_below_reads_same_column(logged_in, bank_url):
    open_member(logged_in, bank_url)
    result = logged_in.find([NearTextStrategy(by="near_text", anchor="Status", relation="below")])
    assert result.found and logged_in.read_text(result.element) == "Open"


def test_css_fallback_for_balance(logged_in, bank_url):
    open_member(logged_in, bank_url)
    css = CssStrategy(by="css", value="td:has-text('Savings Balance:') + td")
    result = logged_in.find([css])
    assert result.found and logged_in.read_text(result.element) == "$16,057.78"


def test_weak_label_falls_back_to_css(logged_in, bank_url):
    logged_in.goto(bank_url + "/member/12345/update")
    # "Mailing Addr." is plain text, not a <label>, so label lookup finds nothing
    result = logged_in.find([LabelStrategy(by="label", text="Mailing Addr."),
                             CssStrategy(by="css", value="textarea[name=f2]")])
    assert result.drifted and result.strategy_index == 1


def test_invalid_css_is_reported_not_raised(logged_in):
    result = logged_in.find([CssStrategy(by="css", value="input[[[")])
    assert not result.found and "error" in result.attempts[0]


def test_wait_for_any_and_timeout(logged_in, bank_url):
    open_member(logged_in, bank_url)
    hit = logged_in.wait_for_any([WaitCondition(text="Nope"), WaitCondition(text="Member Detail")], timeout_s=2)
    assert hit is not None and hit.text == "Member Detail"
    assert logged_in.wait_for_any([WaitCondition(url_contains="/nowhere")], timeout_s=0.5) is None


def test_popup_blocks_click_and_can_be_dismissed(logged_in, bank_url, inject):
    inject("popup")
    logged_in.goto(bank_url + "/search")
    assert logged_in.dialog_open("Notice")
    with pytest.raises(BrowserError, match="blocked by"):
        logged_in.click(logged_in.find(SEARCH_BUTTON).element)
    logged_in.click(logged_in.find([RoleStrategy(by="role", role="button", name="OK")]).element)
    assert not logged_in.dialog_open("Notice")


def test_select_by_visible_label(logged_in, bank_url):
    logged_in.goto(bank_url + "/member/12345/open-account")
    box = logged_in.find([RoleStrategy(by="role", role="combobox", name="Account Type")]).element
    logged_in.select(box, "Money Market")
    assert box._locator.input_value() == "Money Market"


def test_compact_snapshot_removes_repeated_layout_text(logged_in, bank_url):
    open_member(logged_in, bank_url)
    full, compact = logged_in.snapshot(), logged_in.compact_snapshot()
    assert len(compact) < len(full) * 0.7  # ~40% smaller on this page
    assert 'heading "Member Detail"' in compact and 'cell "$16,057.78"' in compact
    assert compact.count("Alice Fernwood") == 1  # once, in its own cell


def test_screenshot_and_trace_files(logged_in, tmp_path):
    logged_in.start_trace()
    logged_in.screenshot(tmp_path / "shot.png")
    trace = logged_in.stop_trace(tmp_path / "trace.zip")
    assert (tmp_path / "shot.png").stat().st_size > 0 and trace.stat().st_size > 0
