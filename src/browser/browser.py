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

# Injected into every page. Reports what a HUMAN does (clicks, typing, dropdowns) back to Python
# through an exposed binding. Passwords are never sent. It runs on every page, but Python only
# keeps the reports while a human is in control.
HUMAN_RECORDER_JS = r"""
(() => {
  if (window.__humanRecorderInstalled) return;
  window.__humanRecorderInstalled = true;
  const roleOf = el => {
    const t = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || 'text').toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button' || (t === 'input' && ['submit', 'button'].includes(ty))) return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea' || t === 'input') return 'textbox';
    return null;
  };
  const label = el => (el.labels && el.labels.length) ? el.labels[0].innerText.trim() : null;
  const nameOf = el => label(el) || el.getAttribute('aria-label')
      || (el.tagName === 'INPUT' && ['submit', 'button'].includes(el.type) ? el.value : null)
      || (el.innerText || '').trim();
  const heading = () => { const h = document.querySelector('h1, h2'); return h ? h.innerText.trim() : ''; };
  const dialogOf = el => {
    const d = el.closest('[role=dialog]');
    if (!d) return null;
    const id = d.getAttribute('aria-labelledby');
    return id && document.getElementById(id) ? document.getElementById(id).innerText.trim() : 'dialog';
  };
  const info = el => ({ role: roleOf(el), name: (nameOf(el) || '').slice(0, 80), label: label(el),
                        tag: el.tagName.toLowerCase(), field: el.getAttribute('name'),
                        type: el.getAttribute('type'), value_attr: el.getAttribute('value'),
                        dialog: dialogOf(el), heading: heading(), path: location.pathname });
  const report = data => { try { window.__humanAction(data); } catch (e) {} };
  document.addEventListener('click', e => {
    const el = e.target.closest('a, button, input[type=submit], input[type=button]');
    if (el) report({ kind: 'click', ...info(el) });
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    if (!el.matches('input, select, textarea') || ['submit', 'button'].includes(el.type)) return;
    const value = el.type === 'password' ? '***'
        : el.tagName === 'SELECT' ? el.options[el.selectedIndex].text : el.value;
    report({ kind: el.tagName === 'SELECT' ? 'select' : 'type', value, ...info(el) });
  }, true);
  document.addEventListener('submit', e => {
    if (e.submitter) return;  // a click on the button was already reported
    const b = e.target.querySelector('input[type=submit], button[type=submit], button');
    if (b) report({ kind: 'click', ...info(b), pressed_enter: true });
  }, true);
})();
"""

# YAML quotes a line in '...' when the name contains a colon, so both forms are matched.
CONTAINER_NAME = re.compile(r"""^(\s*- )'?(table|rowgroup|row|cell|generic) ".*"'?:$""")


class BrowserError(Exception):
    """Any failure from the browser: element not actionable, navigation failed, etc."""


class ElementBlocked(BrowserError):
    """Another element (e.g. a modal overlay) covers the target. The action did NOT happen."""


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
        if headless:
            self._browser = self._playwright.chromium.launch(headless=True, slow_mo=slow_mo_ms)
            self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        else:
            # Visible window: use the REAL window size, not an emulated viewport. With emulation,
            # native dropdown menus (<select>) are drawn by macOS at the wrong place on screen,
            # away from their field, which confuses the human operator.
            self._browser = self._playwright.chromium.launch(
                headless=False, slow_mo=slow_mo_ms, args=["--window-size=1280,900"])
            self._context = self._browser.new_context(no_viewport=True)
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.page: Page = self._context.new_page()
        self._tracing = False
        # Page loads in flight. A click returns before the next page arrives, and the old page
        # stays visible meanwhile, so "what is on screen" alone cannot tell us we are done.
        self._pending_navigations: set = set()
        self.page.on("request", self._on_request)
        self.page.on("requestfinished", self._on_request_done)
        self.page.on("requestfailed", self._on_request_done)
        self._human_listener = None
        self._recorder_installed = False

    def _on_request(self, request) -> None:
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            self._pending_navigations.add(request)

    def _on_request_done(self, request) -> None:
        self._pending_navigations.discard(request)

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

    def has_heading(self, text: str) -> bool:
        """True if a visible h1/h2 has exactly this text: identifies the page, unlike text_visible."""
        return self.page.locator("h1, h2").filter(visible=True).filter(
            has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")).count() > 0

    def page_matches(self, condition) -> bool:
        """PageCondition or WaitCondition: heading, text, text_visible or url_contains."""
        if getattr(condition, "heading", None) is not None:
            return self.has_heading(condition.heading)
        text = getattr(condition, "text", None) or getattr(condition, "text_visible", None)
        if text is not None:
            return self.text_visible(text)
        return condition.url_contains in self.page.url

    def heading(self) -> str:
        """The page's main heading (first visible h1/h2), e.g. "Member Detail". Empty if none."""
        h = self.page.locator("h1, h2").filter(visible=True)
        return h.first.inner_text().strip() if h.count() else ""

    def visible_text(self, max_chars: int = 2000) -> str:
        """The page's visible text, whitespace collapsed. Used for approval summaries."""
        text = " ".join(self.page.locator("body").inner_text().split())
        return text[:max_chars] + ("..." if len(text) > max_chars else "")

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
                if self.page_matches(c):
                    return c
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(POLL_INTERVAL_MS)

    def settle(self, timeout_s: float) -> bool:
        """Wait until no page load is in flight and the current page has loaded.

        Returns False if a load is still in flight after timeout_s (e.g. a slow page). The
        caller decides what that means; nothing is retried here.
        """
        self.page.wait_for_timeout(100)  # give a just-dispatched click time to start its request
        deadline = time.monotonic() + timeout_s
        while self._pending_navigations:
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(POLL_INTERVAL_MS)
        try:
            remaining = max(deadline - time.monotonic(), 0.1)
            self.page.wait_for_load_state("load", timeout=remaining * 1000)
        except PlaywrightError:
            return False
        return True

    def wait(self, seconds: float) -> None:
        """Pause without blocking Playwright's event processing (unlike time.sleep)."""
        self.page.wait_for_timeout(seconds * 1000)

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

    # ------------------------------------------------------------ human recording
    def start_human_recording(self, on_action) -> None:
        """Report the human's actions on this page and every page after it to on_action(dict)."""
        if not self._recorder_installed:
            self.page.expose_binding("__humanAction", lambda _source, data: self._on_human_action(data))
            self._context.add_init_script(HUMAN_RECORDER_JS)  # every future page
            self._recorder_installed = True
        self.page.evaluate(HUMAN_RECORDER_JS)                  # the page already open
        self._human_listener = on_action

    def stop_human_recording(self) -> None:
        self._human_listener = None

    def _on_human_action(self, data: dict) -> None:
        if self._human_listener is not None:  # ignored unless a human is in control
            self._human_listener(data)

    # ------------------------------------------------------------ recording
    def element_facts(self, element: Element) -> dict:
        """Plain facts about an element, for building backup locators and for the recorder."""
        return element._locator.evaluate("""el => {
            const dialog = el.closest('[role=dialog]');
            const labelled = dialog && dialog.getAttribute('aria-labelledby');
            const label = (el.labels && el.labels.length) ? el.labels[0].innerText : el.getAttribute('aria-label');
            return {
                tag: el.tagName.toLowerCase(), type: el.getAttribute('type'), name: el.getAttribute('name'),
                id: el.id || null, value: el.getAttribute('value'), label: label ? label.trim() : null,
                text: (el.innerText || '').trim().slice(0, 80),
                dialog: dialog ? (labelled ? document.getElementById(labelled).innerText.trim() : 'dialog') : null,
            };
        }""")

    def locators_for(self, element: Element, primary: Strategy) -> list[Strategy]:
        """All verified ways to find this element, in preference order: role/near_text, label, text, css.

        CSS is the web-specific last resort, so it is built here in the surface layer.
        Only one CSS fallback is kept: the first candidate that proves unique.
        """
        f = self.element_facts(element)
        candidates: list[Strategy] = []
        if f["label"] and f["tag"] in ("input", "select", "textarea"):
            candidates.append(LabelStrategy(by="label", text=f["label"]))
        if f["tag"] == "a" and f["text"]:
            candidates.append(TextStrategy(by="text", text=f["text"]))
        css: list[str] = []
        if isinstance(primary, NearTextStrategy) and primary.relation == "right_of":
            css.append(f"td:has-text('{primary.anchor}') + td")
        if f["tag"] == "input" and f["type"] == "submit" and f["value"]:
            css.append(f'input[type="submit"][value="{f["value"]}"]')
        if f["name"] and f["tag"] in ("input", "select", "textarea"):
            css.append(f'{f["tag"]}[name="{f["name"]}"]')
        if f["tag"] == "button" and f["text"]:
            css.append(f'button:has-text("{f["text"]}")')
        verified_css = self.backup_strategies(element, [CssStrategy(by="css", value=c) for c in css])
        return [primary] + self.backup_strategies(element, candidates) + verified_css[:1]

    def backup_strategies(self, element: Element, candidates: list[Strategy]) -> list[Strategy]:
        """Keep only candidates that match exactly one visible element, and it is THIS element.

        Checked now, on the live page, so every locator in a recipe was proven to work once.
        """
        target = element._locator.element_handle()
        kept = []
        for strategy in candidates:
            try:
                loc = self._locate(strategy).filter(visible=True)
                if loc.count() != 1:
                    continue
                if self.page.evaluate("([a, b]) => a === b", [target, loc.element_handle()]):
                    kept.append(strategy)
            except PlaywrightError:
                continue  # e.g. a candidate CSS selector the page cannot parse
        return kept

    # ------------------------------------------------------------ actions
    def click(self, element: Element) -> None:
        # no_wait_after: return as soon as the click is dispatched. Waiting for the next page
        # is the caller's job (wait_for). Without it, a slow page makes click() time out even
        # though the click HAPPENED, and a retry would submit twice.
        self._act(element, "click", lambda loc: loc.click(no_wait_after=True))

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
            message = f"could not {verb} {element.description}: {blocked_reason(e)}"
            if "intercepts pointer events" in str(e):
                raise ElementBlocked(message) from e  # certain: the action never reached the element
            raise BrowserError(message) from e

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
