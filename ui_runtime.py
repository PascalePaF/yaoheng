"""Page lifecycle and snapshot presentation for the Tk desktop shell.

The data service owns the newest rates; a page only owns its rendered view.
Hidden pages never need to rebuild a table in the same Tk callback that
receives a network result.  Their latest update is applied when shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Mapping, TypeVar


Page = TypeVar("Page")

RATE_PAGES = (
    "exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market"
)
_RATE_DEPENDENCIES = {
    "all": RATE_PAGES,
    # Both seven-way exchanges and the mixed crypto converter use fiat rates.
    "fiat": RATE_PAGES,
    "crypto": ("exchange", "market_exchange", "crypto", "market"),
}


def affected_rate_pages(section: str) -> tuple[str, ...]:
    return _RATE_DEPENDENCIES.get(section, RATE_PAGES)


@dataclass(frozen=True)
class SnapshotUpdate:
    snapshot: object
    from_cache: bool
    section: str


class PageHost(Generic[Page]):
    """Construct persistent pages on demand and coalesce hidden updates."""

    def __init__(
        self,
        factories: Mapping[str, Callable[[], Page]],
        mount: Callable[[Page], None],
        present: Callable[[str, Page, SnapshotUpdate, bool], None],
    ) -> None:
        self.factories = dict(factories)
        self.pages: dict[str, Page] = {}
        self._mount = mount
        self._present = present
        self._pending: dict[str, SnapshotUpdate] = {}

    def ensure(self, name: str) -> Page:
        if name not in self.factories:
            raise KeyError(name)
        page = self.pages.get(name)
        if page is None:
            page = self.factories[name]()
            self.pages[name] = page
            self._mount(page)
        return page

    def publish(
        self, snapshot: object, from_cache: bool, section: str, visible: str
    ) -> None:
        update = SnapshotUpdate(snapshot, bool(from_cache), section)
        for name in affected_rate_pages(section):
            if name in self.factories:
                self._pending[name] = update
        # Present only the view the user can see.  Repeated network results
        # collapse to one update for each hidden page.
        self.present_pending(visible, visible=True)

    def present_pending(self, name: str, *, visible: bool = False) -> bool:
        update = self._pending.get(name)
        page = self.pages.get(name)
        if update is None or page is None:
            return False
        self._present(name, page, update, visible)
        self._pending.pop(name, None)
        return True

    def pending_names(self) -> frozenset[str]:
        return frozenset(self._pending)
