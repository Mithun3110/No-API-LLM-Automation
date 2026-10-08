"""The browser layer: the ONLY module that imports Playwright.

This is the surface seam. Everything above it (agent, replay, handoff) talks in terms of
locator strategies, text and URLs, never Playwright objects. A desktop version would
implement the same methods with an OS accessibility API (UI Automation, AX API).

Playwright exceptions are converted to BrowserError here, so no caller needs to know
which automation library is underneath.
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

from src.models.recipe import (
    CssStrategy, LabelStrategy, NearTextStrategy, RoleStrategy, Strategy, TextStrategy, WaitCondition,
)

# Short action timeout on purpose: a click blocked by a popup should fail fast so the
# replay engine can classify it, instead of Playwright silently retrying for 30 seconds.
ACTION_TIMEOUT_MS = 5_000
POLL_INTERVAL_MS = 200

# YAML quotes a line in '...' when the name contains a colon, so both forms are matched.
CONTAINER_NAME = re.compile(r"""^(\s*- )'?(table|rowgroup|row|cell|generic) ".*"'?:$""")


class BrowserError(Exception):
    """Any failure from the browser: element not actionable, navigation failed, etc."""


@dataclass
class Element:
    """An element found by a strategy. Opaque to callers: pass it back to click/type/read."""
    _locator: Locator
    description: str


@dataclass
class FindResult:
    """Outcome of trying a target's strategies in order."""
    element: Element | None
    strategy_index: int | None = None          # which strategy matched (0 = preferred)
    attempts: list[str] = field(default_factory=list)  # e.g. 'role=button "Search": 1 match'

    @property
    def found(self) -> bool:
        return self.element is not None

    @property
    def drifted(self) -> bool:
        """A backup strategy was needed: the page has changed since recording."""
        return self.strategy_index is not None and self.strategy_index > 0


def describe(strategy: Strategy) -> str:
    """Readable one-line form of a strategy, for logs and failure messages."""
    match strategy:
        case RoleStrategy(role=role, name=name, exact=exact):
            return f'role={role} "{name}"' + ("" if exact else " (partial)")
        case LabelStrategy(text=text):
            return f'label "{text}"'
        case TextStrategy(text=text):
            return f'text "{text}"'
        case NearTextStrategy(anchor=anchor, relation=relation):
            return f'{relation.replace("_", " ")} "{anchor}"'
        case CssStrategy(value=value):
            return f"css {value}"
    raise ValueError(f"unknown strategy {strategy!r}")


class Browser:
    def __init__(self, headless: bool = False, slow_mo_ms: int = 0):
        self._playwright = sync_playwright().start()
        # Visible by default: the human operator must be able to take over this same window.
        self._browser = self._playwright.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.page: Page = self._context.new_page()
        self._tracing = False

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        try:
            self._context.close()
            self._browser.close()
        finally:
            self._playwright.stop()

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------ navigation and state
    def goto(self, url: str, timeout_s: float = 30) -> None:
        try:
            self.page.goto(url, timeout=timeout_s * 1000)
        except PlaywrightError as e:
            raise BrowserError(f"navigation to {url} failed: {first_line(e)}") from e

    def current_url(self) -> str:
        return self.page.url

    def snapshot(self) -> str:
        """Full accessibility tree as YAML (role, name, nesting)."""
        return self.page.locator("body").aria_snapshot()

    def compact_snapshot(self) -> str:
        """Accessibility tree for the LLM, with legacy-layout noise removed.

        Nested layout tables give every outer row and cell a name made of ALL the text
        inside it, so the same text repeats at each nesting level. Containers that have
        children lose their name (the children carry the text); leaf elements keep theirs.
        """
        lines = []
        for line in self.snapshot().splitlines():
            # '- row "long text":' or "- 'row \"text: with colon\"':"  ->  '- row:'
            # Only containers end with ':'; leaf cells like '- cell "$16,057.78"' are kept.
            line = CONTAINER_NAME.sub(r"\1\2:", line)
            lines.append(line)
        return "\n".join(lines)

    def text_visible(self, text: str) -> bool:
        """True if this text is visible anywhere on the page (substring match)."""
        return self.page.get_by_text(text).filter(visible=True).count() > 0

    def dialog_open(self, name: str) -> bool:
        return self.page.get_by_role("dialog", name=name).filter(visible=True).count() > 0

    def wait_for_any(self, conditions: list[WaitCondition], timeout_s: float) -> WaitCondition | None:
        """Wait until any condition holds. Returns the one that matched, or None on timeout.

        Polls with page.wait_for_timeout rather than time.sleep, so Playwright keeps
        processing browser events (needed later for recording the human's actions).
        """
        deadline = time.monotonic() + timeout_s
        while True:
            for c in conditions:
                if c.text is not None and self.text_visible(c.text):
                    return c
                if c.url_contains is not None and c.url_contains in self.page.url:
                    return c
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(POLL_INTERVAL_MS)

    # ------------------------------------------------------------ finding elements
    def find(self, strategies: list[Strategy]) -> FindResult:
        """Try strategies in order; the first that matches exactly ONE visible element wins.

        Zero matches: the element is gone or renamed. Several: the strategy is ambiguous,
        and clicking one at random could hit the wrong button. Both mean "try the next".
        """
        result = FindResult(element=None)
        for index, strategy in enumerate(strategies):
            try:
                locator = self._locate(strategy).filter(visible=True)
                count = locator.count()
            except PlaywrightError as e:  # e.g. invalid CSS in a recipe
                result.attempts.append(f"{describe(strategy)}: error {first_line(e)}")
                continue
            result.attempts.append(f"{describe(strategy)}: {count} match{'es' if count != 1 else ''}")
            if count == 1:
                result.element = Element(locator, describe(strategy))
                result.strategy_index = index
                return result
        return result

    def _locate(self, strategy: Strategy) -> Locator:
        page = self.page
        match strategy:
            case RoleStrategy(role=role, name=name, exact=exact):
                return page.get_by_role(role, name=name, exact=exact)
            case LabelStrategy(text=text):
                return page.get_by_label(text, exact=True)
            case TextStrategy(text=text):
                return page.get_by_text(text, exact=True)
            case NearTextStrategy(anchor=anchor, relation=relation):
                return self._near_text(anchor, relation)
            case CssStrategy(value=value):
                return page.locator(value)
        raise ValueError(f"unknown strategy {strategy!r}")

    def _near_text(self, anchor: str, relation: str) -> Locator:
        # Find the table cell whose text is exactly the anchor (e.g. "Savings Balance:"),
        # then step to its neighbour. Legacy apps lay out label/value pairs as table cells.
        # The full-text regex matters with nested tables: outer layout cells also CONTAIN the
        # anchor text, but only the label cell's whole text IS the anchor.
        anchor_cell = self.page.locator("td, th").filter(has_text=re.compile(rf"^\s*{re.escape(anchor)}\s*$"))
        if relation == "right_of":
            return anchor_cell.locator("xpath=following-sibling::*[self::td or self::th][1]")
        # below: same column in the next row
        if anchor_cell.count() != 1:
            return anchor_cell  # let find() report 0 or many matches
        column = anchor_cell.evaluate("cell => cell.cellIndex") + 1
        return anchor_cell.locator(f"xpath=parent::tr/following-sibling::tr[1]/*[self::td or self::th][{column}]")

    # ------------------------------------------------------------ actions
    def click(self, element: Element) -> None:
        self._act(element, "click", lambda loc: loc.click())

    def type(self, element: Element, value: str) -> None:
        # fill() replaces existing text; typing on top of a pre-filled field would append.
        self._act(element, "type into", lambda loc: loc.fill(value))

    def select(self, element: Element, value: str) -> None:
        # Playwright matches the option's value or its visible label.
        self._act(element, "select in", lambda loc: loc.select_option(value))

    def read_text(self, element: Element) -> str:
        try:
            return element._locator.inner_text().strip()
        except PlaywrightError as e:
            raise BrowserError(f"could not read {element.description}: {first_line(e)}") from e

    def _act(self, element: Element, verb: str, action) -> None:
        try:
            action(element._locator)
        except PlaywrightError as e:
            raise BrowserError(f"could not {verb} {element.description}: {blocked_reason(e)}") from e

    # ------------------------------------------------------------ evidence
    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def start_trace(self) -> None:
        if not self._tracing:
            self._context.tracing.start(screenshots=True, snapshots=True)
            self._tracing = True

    def stop_trace(self, path: Path | None) -> Path | None:
        """Stop tracing; save the trace only if a path is given (we keep traces on failure)."""
        if not self._tracing:
            return None
        self._tracing = False
        if path is None:
            self._context.tracing.stop()
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.tracing.stop(path=str(path))
        return path


def first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0]


def blocked_reason(error: Exception) -> str:
    """Turn Playwright's long call log into one useful sentence."""
    for line in str(error).splitlines():
        if "intercepts pointer events" in line:
            # e.g. '<div class="overlay" id="notice-overlay">…</div> intercepts pointer events'
            return "blocked by " + line.strip(" -")
    return first_line(error)
