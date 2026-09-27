"""MeshChat – MapBridge: QWebChannel bridge between Python and Leaflet JS."""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import QObject, QTimer, Signal, Slot

log = logging.getLogger(__name__)


class MapBridge(QObject):
    """
    Exposed to JavaScript as `bridge`.

    Python → JS:  call JS functions via self._page.runJavaScript(...)
    JS → Python:  JS calls bridge.slotName(args)
    """

    # object, not int: Meshtastic node_num is a 32-bit *unsigned* value and
    # can exceed 0x7FFFFFFF (real hardware IDs do) — a Qt-typed int signal
    # is C++ int32 and silently wraps to negative, breaking the node_num ==
    # dict-key match downstream. object carries the Python int through
    # untouched.
    node_clicked   = Signal(object)  # node_num from JS
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
        self._ready_watchdog: QTimer | None = None

    def set_page(self, page) -> None:
        self._page = page

    # ── Readiness ──────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def pending_call_count(self) -> int:
        return len(self._pending_calls)

    def start_ready_watchdog(self, timeout_ms: int) -> None:
        """Warn if the page never confirms its bridge is up.

        Buffered-forever is the failure mode that looks most like success: the
        map draws its basemap, every queued update is quietly discarded, and no
        node pin ever appears. That is what a failed
        ``vendor/qwebchannel.js`` load used to do — silently, for the whole
        session (see map_widget.ensure_qwebchannel_asset). This turns it into a
        startup warning that names the likely cause.
        """
        if self._ready:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_ready_timeout)
        timer.start(timeout_ms)
        self._ready_watchdog = timer

    def _on_ready_timeout(self) -> None:
        if self._ready:
            return
        log.warning(
            "Map: the page never reported its JavaScript bridge ready — %d queued "
            "call(s) will never run and no node pins will appear. A failed "
            "vendor/qwebchannel.js load is the usual cause; check the "
            "[map JS ...] lines above.",
            len(self._pending_calls),
        )

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

    def set_selected_node(self, node_num: int | None) -> None:
        arg = "null" if node_num is None else str(int(node_num))
        self._run(f"setSelectedNode({arg})")

    def refresh_size(self, refit: bool = True) -> None:
        self._run(f"refreshSize({'true' if refit else 'false'})")

    def set_theme(self, theme: str) -> None:
        self._run(f"setMapTheme({json.dumps(theme)})")

    def set_basemap(self, spec: dict) -> None:
        """Swap the basemap layer (online tiles ↔ an OrcMaps pack).

        Python owns the policy — which source, and which OrcMaps style matches
        the current theme — so the page only has to swap layers.
        """
        self._run(f"setBasemap({json.dumps(spec)})")

    # ── JS → Python ────────────────────────────────────────────────────

    @Slot("qlonglong")
    def nodeClicked(self, node_num: int) -> None:  # noqa: N802 (Qt naming)
        # "qlonglong" (64-bit signed), not "int" (32-bit signed) — see the
        # node_clicked Signal(object) comment above; JS delivers node_num as
        # a plain number, and a 32-bit slot type would truncate/wrap it
        # before this method ever sees it.
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
        if self._ready_watchdog is not None:
            self._ready_watchdog.stop()
            self._ready_watchdog = None
        pending, self._pending_calls = self._pending_calls, []
        for js in pending:
            self._page.runJavaScript(js)
        self.map_ready.emit()
