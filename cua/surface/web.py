"""Surface implementation for web applications, driven by Playwright."""

import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

from cua.schema import (
    Anchor, LabelledValueLocator, NthOfRoleLocator, RoleInScopeLocator, RoleNameLocator,
    TableCellLocator, Target, Timing,
)
from cua.surface.base import (
    NavigationBlocked, Observation, Resolution, Surface, TargetNotFound,
)

POLL_INTERVAL_MS = 100
MONEY_CELL = re.compile(r"^\$[\d,]+\.\d{2}$")
RequestGuard = Callable[[str, bool], bool]  # (url, is_navigation) -> allowed?

# Installed in every page while an operator may hold control. Reports WHAT was touched
# (role and name) and, for typing, HOW MANY characters, never the text itself.
OPERATOR_CAPTURE_JS = """
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const send = (event) => {
    try { window.__cuaOperatorEvent({ ...event, at: new Date().toISOString() }); } catch (e) {}
  };
  const describe = (el) => {
    const tag = el.tagName.toLowerCase();
    const isButtonInput = tag === 'input' && ['submit', 'button'].includes(el.type);
    const role = el.getAttribute('role')
      || (tag === 'a' ? 'link'
        : (tag === 'button' || isButtonInput) ? 'button'
        : tag === 'select' ? 'combobox'
        : (tag === 'input' || tag === 'textarea') ? 'textbox'
        : tag === 'td' || tag === 'th' ? 'cell' : tag);
    let name = el.getAttribute('aria-label') || (isButtonInput ? el.value : '');
    if (!name && (role === 'textbox' || role === 'combobox')) {
      const row = el.closest('tr');
      if (row && row.cells.length) name = 'field in row "' + row.cells[0].innerText.trim() + '"';
    }
    if (!name) name = (el.innerText || '').trim();
    return { role, name: name.slice(0, 60) };
  };
  document.addEventListener('click', (e) => {
    const el = e.target.closest('a, button, input, select, textarea, td, th') || e.target;
    send({ type: 'click', ...describe(el) });
  }, true);
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') return;
    const chars = el.type === 'password' ? null : (el.value || '').length;
    send({ type: 'typed', ...describe(el), chars });
  }, true);
  document.addEventListener('change', (e) => {
    if (e.target.tagName === 'SELECT') send({ type: 'selected', ...describe(e.target) });
  }, true);
})();
"""


class WebSurface(Surface):
    def __init__(self, page: Page, base_url: str, timing: Timing):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.timing = timing
        self.blocked: list[str] = []  # URLs the request guard refused, in order
        self._navigations_in_flight: set = set()
        self._operator_sink: Callable[[dict], None] | None = None
        self._operator_events: list[dict] = []
        self._capture_installed = False
        page.set_default_timeout(timing.action_timeout_ms)
        page.set_default_navigation_timeout(timing.navigation_timeout_ms)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)

    # --- Looking -------------------------------------------------------------------

    def goto(self, route: str) -> None:
        before = len(self.blocked)
        try:
            self.page.goto(self.base_url + route, wait_until="load")
        except PlaywrightError:
            self._raise_if_blocked(before)
            raise
        self._raise_if_blocked(before)

    def current_route(self) -> str:
        return urlparse(self.page.url).path

    def observe(self) -> Observation:
        return Observation(
            route=self.current_route(),
            title=self.page.title(),
            accessibility_tree=self.page.locator("body").aria_snapshot(),
        )

    def text_visible(self, text: str) -> bool:
        return self.page.get_by_text(text).filter(visible=True).count() > 0

    def control_visible(self, anchor: Anchor) -> bool:
        matches = self.page.get_by_role(anchor.role, name=anchor.name, exact=True)
        return matches.filter(visible=True).count() > 0

    # --- Acting --------------------------------------------------------------------

    def fill(self, target: Target, value: str) -> Resolution:
        locator, resolution = self._resolve(target)
        locator.fill(value)
        return resolution

    def select(self, target: Target, value: str) -> Resolution:
        locator, resolution = self._resolve(target)
        locator.select_option(label=value)
        return resolution

    def click(self, target: Target) -> Resolution:
        locator, resolution = self._resolve(target)
        before = len(self.blocked)
        try:
            locator.click()
            self.page.wait_for_load_state("load")
        except PlaywrightError:
            self._raise_if_blocked(before)
            raise
        self._raise_if_blocked(before)
        return resolution

    def read_text(self, target: Target) -> tuple[str, Resolution]:
        locator, resolution = self._resolve(target)
        return locator.inner_text().strip(), resolution

    def screenshot(self, path: Path, sensitive_labels: Sequence[str] = ()) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        masks = [
            # the value cell immediately to the right of each sensitive label
            self.page.get_by_role("cell", name=label, exact=True).locator("xpath=following-sibling::*[1]")
            for label in sensitive_labels
        ]
        masks.append(self.page.get_by_role("cell", name=MONEY_CELL))  # every money amount
        self.page.screenshot(path=str(path), full_page=True, mask=masks)

    def _raise_if_blocked(self, count_before: int) -> None:
        """If the guard refused a request during the last action, turn that into a clear error."""
        if len(self.blocked) > count_before:
            raise NavigationBlocked(self.blocked[-1])

    # --- Page-load tracking ----------------------------------------------------------

    def navigation_pending(self) -> bool:
        return bool(self._navigations_in_flight)

    def _on_request(self, request) -> None:
        if request.is_navigation_request():
            self._navigations_in_flight.add(request)

    def _on_request_done(self, request) -> None:
        self._navigations_in_flight.discard(request)

    def position_among_role(self, target: Target, role: str) -> int | None:
        locator, _ = self._resolve(target)
        element = locator.element_handle()
        for index, candidate in enumerate(self.page.get_by_role(role).all()):
            if candidate.evaluate("(node, other) => node === other", element):
                return index
        return None

    # --- Capturing what an operator does --------------------------------------------

    def start_operator_capture(self, on_event: Callable[[dict], None]) -> None:
        if not self._capture_installed:
            self.page.expose_binding("__cuaOperatorEvent", self._on_operator_event)
            self.page.add_init_script(OPERATOR_CAPTURE_JS)  # every page loaded from now on
            self.page.evaluate(OPERATOR_CAPTURE_JS)         # ...and the page already open
            self.page.on("framenavigated", self._on_navigated)
            self._capture_installed = True
        self._operator_events = []
        self._operator_sink = on_event

    def stop_operator_capture(self) -> None:
        self.page.evaluate("0")  # any browser call delivers events still queued
        self._operator_sink = None

    def _on_operator_event(self, _source, event: dict) -> None:
        if self._operator_sink is None:
            return  # only report while a person holds control
        last = self._operator_events[-1] if self._operator_events else None
        if (event.get("type") == "typed" and last and last.get("type") == "typed"
                and last.get("name") == event.get("name")):
            last.update(event)  # one event per field, not one per keystroke
            return
        self._operator_events.append(event)
        self._operator_sink(event)

    def _on_navigated(self, frame) -> None:
        if self._operator_sink is not None and frame == self.page.main_frame:
            self._operator_sink({"type": "navigated", "route": urlparse(frame.url).path})

    # --- Finding controls ----------------------------------------------------------

    def _resolve(self, target: Target) -> tuple[Locator, Resolution]:
        """Return the first locator, in rank order, that matches EXACTLY one control.

        Re-checks every 100 ms until the action timeout, because a page may still be
        rendering. Zero matches means 'not here (yet)'; two or more means ambiguous,
        and we never guess between candidates.
        """
        deadline = time.monotonic() + self.timing.action_timeout_ms / 1000
        while True:
            attempts: list[tuple[str, int]] = []
            for index, spec in enumerate(target.locators):
                candidate = self._build(spec)
                count = candidate.count() if candidate is not None else 0
                attempts.append((spec.strategy, count))
                if count == 1:
                    return candidate, Resolution(index, spec.strategy)
            if time.monotonic() >= deadline:
                raise TargetNotFound(target.description, attempts)
            self.page.wait_for_timeout(POLL_INTERVAL_MS)

    def _build(self, spec) -> Locator | None:
        """Translate one schema locator into a Playwright locator."""
        page = self.page
        match spec:
            case RoleNameLocator(role=role, name=name):
                return page.get_by_role(role, name=name, exact=True)
            case RoleInScopeLocator(role=role, scope=scope):
                # exact=False: a container's computed name can include values typed into
                # its fields ("Member ID: 12345"), so we match containers CONTAINING the label.
                named = page.get_by_role(scope.role, name=scope.name, exact=False)
                # Layout tables wrap whole panels, so an outer row's name also contains the
                # label. Keep only the innermost match: one with no matching container inside.
                innermost = named.filter(has_not=named)
                return innermost.get_by_role(role)
            case NthOfRoleLocator(role=role, index=index):
                matches = page.get_by_role(role)
                return matches.nth(index) if matches.count() > index else None
            case TableCellLocator(row_where=row_where, column=column):
                return self._table_cell(row_where, column)
            case LabelledValueLocator(label=label):
                labels = page.get_by_role("cell", name=label, exact=True)
                if labels.count() != 1:
                    return None
                return labels.locator("xpath=following-sibling::*[1]")
        raise TypeError(f"unsupported locator: {spec!r}")

    def _table_cell(self, row_where: str, column: str) -> Locator | None:
        """The cell in the row containing `row_where`, under the column headed `column`."""
        header = self.page.get_by_role("cell", name=column, exact=True)
        row_label = self.page.get_by_role("cell", name=row_where, exact=True)
        if header.count() != 1 or row_label.count() != 1:
            return None
        column_index = header.evaluate("cell => cell.cellIndex")  # position within its row
        return row_label.locator(f"xpath=../*[{column_index + 1}]")


@contextmanager
def open_web_surface(base_url: str, timing: Timing, *, headless: bool = True,
                     video_dir: Path | None = None,
                     request_guard: RequestGuard | None = None) -> Iterator[WebSurface]:
    """Start a browser, yield a surface on a fresh page, and always clean up afterwards.

    If request_guard is given, EVERY request the browser makes is checked against it,
    including ones caused by clicks and redirects we didn't predict.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(video_dir) if video_dir else None,
        )
        page = context.new_page()
        surface = WebSurface(page, base_url, timing)

        if request_guard is not None:
            def guard(route, request):
                if request_guard(request.url, request.is_navigation_request()):
                    route.continue_()
                else:
                    surface.blocked.append(request.url)
                    route.abort("blockedbyclient")
            context.route("**/*", guard)

        try:
            yield surface
        finally:
            context.close()
            browser.close()