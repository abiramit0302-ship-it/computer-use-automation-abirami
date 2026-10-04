"""Surface implementation for web applications, driven by Playwright."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, sync_playwright

from cua.schema import (
    Anchor, NthOfRoleLocator, RoleInScopeLocator, RoleNameLocator,
    TableCellLocator, Target, Timing,
)
from cua.surface.base import Observation, Resolution, Surface, TargetNotFound

POLL_INTERVAL_MS = 100


class WebSurface(Surface):
    def __init__(self, page: Page, base_url: str, timing: Timing):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.timing = timing
        page.set_default_timeout(timing.action_timeout_ms)
        page.set_default_navigation_timeout(timing.navigation_timeout_ms)

    # --- Looking -------------------------------------------------------------------

    def goto(self, route: str) -> None:
        self.page.goto(self.base_url + route, wait_until="load")

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
        locator.click()
        self.page.wait_for_load_state("load")
        return resolution

    def read_text(self, target: Target) -> tuple[str, Resolution]:
        locator, resolution = self._resolve(target)
        return locator.inner_text().strip(), resolution

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)

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
                     video_dir: Path | None = None) -> Iterator[WebSurface]:
    """Start a browser, yield a surface on a fresh page, and always clean up afterwards."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(video_dir) if video_dir else None,
        )
        page = context.new_page()
        try:
            yield WebSurface(page, base_url, timing)
        finally:
            context.close()
            browser.close()