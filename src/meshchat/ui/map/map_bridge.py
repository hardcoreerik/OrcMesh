"""MeshChat – MapBridge: QWebChannel bridge between Python and Leaflet JS."""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import QObject, Signal, Slot

log = logging.getLogger(__name__)


class MapBridge(QObject):
    """
    Exposed to JavaScript as `bridge`.

    Python → JS:  call JS functions via self._page.runJavaScript(...)
    JS → Python:  JS calls bridge.slotName(args)
    """

    node_clicked   = Signal(int)    # node_num from JS
    map_ready      = Signal()       # JS map finished loading
    theme_changed  = Signal(str)    # "light" or "dark", from the in-page toggle button

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = None
        # index.html/map.js load asynchronously in QWebEngineView; any
        # runJavaScript call issued before mapReady() fires targets a page
        # that hasn't defined updateNode() yet and is silently dropped.
        # Buffer calls and flush them once the JS side confirms it's ready.
        self._ready = False
        self._pending_calls: list[str] = []

    def set_page(self, page) -> None:
        self._page = page

    def _run(self, js: str) -> None:
        if not self._page:
            return
        if self._ready:
            self._page.runJavaScript(js)
        else:
            self._pending_calls.append(js)

    # ── Python → JS ────────────────────────────────────────────────────

    def update_node(self, node_num: int, lat: float, lon: float,
                    name: str, role: str, is_local: bool = False,
                    hops_used: int | None = None,
                    last_heard_s: int | None = None) -> None:
        payload = json.dumps({
            "node_num": node_num,
            "lat": lat,
            "lon": lon,
            "name": name,
            "role": role,
            "is_local": is_local,
            "hops_used": hops_used,
            "last_heard_s": last_heard_s,
        })
        self._run(f"updateNode({payload})")

    def clear_nodes(self) -> None:
        self._run("clearNodes()")

    def fit_bounds(self) -> None:
        self._run("fitAll()")

    def focus_node(self, node_num: int) -> None:
        self._run(f"focusNode({int(node_num)})")

    def refresh_size(self, refit: bool = True) -> None:
        self._run(f"refreshSize({'true' if refit else 'false'})")

    def set_theme(self, theme: str) -> None:
        self._run(f"setMapTheme({json.dumps(theme)})")

    # ── JS → Python ────────────────────────────────────────────────────

    @Slot(int)
    def nodeClicked(self, node_num: int) -> None:  # noqa: N802 (Qt naming)
        log.debug("Map: node clicked %d", node_num)
        self.node_clicked.emit(node_num)

    @Slot(str)
    def themeChanged(self, theme: str) -> None:  # noqa: N802
        log.debug("Map: theme changed to %s", theme)
        self.theme_changed.emit(theme)

    @Slot()
    def mapReady(self) -> None:  # noqa: N802
        log.info("Map: JavaScript bridge ready (%d buffered call(s) to flush)", len(self._pending_calls))
        self._ready = True
        pending, self._pending_calls = self._pending_calls, []
        for js in pending:
            self._page.runJavaScript(js)
        self.map_ready.emit()
