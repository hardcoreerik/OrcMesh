"""MeshChat – MapWidget: QWebEngineView with Leaflet map."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, QUrl, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_THEME_SETTINGS_KEY = "MeshChat/Map/theme"


def _map_available() -> bool:
    """Return True when QtWebEngine is importable."""
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
        from PySide6.QtWebChannel import QWebChannel            # noqa: F401
        return True
    except ImportError:
        return False


class MapWidget(QWidget):
    """
    Leaflet map embedded in a QWebEngineView.
    Falls back to a placeholder when QtWebEngine is unavailable.
    """

    node_clicked = Signal(int)   # node_num

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # _build_webengine() sets self._bridge when QtWebEngine is available;
        # default to None here first so _build_placeholder()'s no-webengine
        # path still has a defined self._bridge to guard against.
        self._bridge = None
        self._did_initial_show = False

        if _map_available():
            self._build_webengine(layout)
        else:
            self._build_placeholder(layout)

    def _build_webengine(self, layout) -> None:
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebChannel import QWebChannel
        from meshchat.ui.map.map_bridge import MapBridge
        from meshchat.ui.map.logging_web_page import LoggingWebEnginePage

        self._view = QWebEngineView()
        self._view.setPage(LoggingWebEnginePage(self._view))

        # QtWebEngine treats file:// pages as fully sandboxed by default —
        # LocalContentCanAccessRemoteUrls is False out of the box, which
        # silently blocks every remote tile image request (no exception,
        # no console error beyond a generic failed-load event). This is why
        # the basemap never rendered even though the bridge/JS pipeline
        # itself was working correctly.
        from PySide6.QtWebEngineCore import QWebEngineSettings
        settings = self._view.page().settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

        self._channel = QWebChannel(self._view.page())
        self._bridge = MapBridge(self)
        self._bridge.set_page(self._view.page())
        self._bridge.node_clicked.connect(self.node_clicked)
        self._bridge.theme_changed.connect(self._on_theme_changed)
        self._bridge.map_ready.connect(self._push_initial_theme)

        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        self._view.loadFinished.connect(
            lambda ok: log.info("Map page load finished: %s", "ok" if ok else "FAILED")
        )

        index_path = _WEB_DIR / "index.html"
        if index_path.exists():
            self._view.load(QUrl.fromLocalFile(str(index_path)))
        else:
            log.warning("Map web assets not found at %s — using fallback", index_path)
            self._view.setHtml(_PLACEHOLDER_HTML, QUrl("about:blank"))

        layout.addWidget(self._view)
        self._webengine_available = True

    def _build_placeholder(self, layout) -> None:
        from PySide6.QtWidgets import QLabel
        from PySide6.QtCore import Qt
        lbl = QLabel(
            "Map not available.\n"
            "Install PySide6-WebEngine to enable the map.\n\n"
            "Node data and monitoring continue normally."
        )
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #5A6690; font-size: 13px; padding: 20px;")
        layout.addWidget(lbl)
        self._webengine_available = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_node(self, node_num: int, lat: float, lon: float,
                    name: str, role: str = "", is_local: bool = False,
                    hops_used: int | None = None,
                    last_heard_s: int | None = None) -> None:
        if self._bridge:
            self._bridge.update_node(node_num, lat, lon, name, role, is_local,
                                     hops_used, last_heard_s)

    def fit_bounds(self) -> None:
        if self._bridge:
            self._bridge.fit_bounds()

    def clear_nodes(self) -> None:
        if self._bridge:
            self._bridge.clear_nodes()

    def focus_node(self, node_num: int) -> None:
        if self._bridge:
            self._bridge.focus_node(node_num)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        # The map tab is hidden at startup, so Leaflet's cached container size
        # is stale (zero) until it's first shown. Re-measure and re-frame once
        # it becomes visible, otherwise the initial auto-fit is meaningless.
        if self._bridge and not self._did_initial_show:
            self._did_initial_show = True
            QTimer.singleShot(200, lambda: self._bridge.refresh_size(True))

    # ------------------------------------------------------------------
    # Theme (light/dark basemap)
    # ------------------------------------------------------------------

    def _push_initial_theme(self) -> None:
        theme = QSettings().value(_THEME_SETTINGS_KEY, "dark")
        if self._bridge:
            self._bridge.set_theme(theme)

    def _on_theme_changed(self, theme: str) -> None:
        QSettings().setValue(_THEME_SETTINGS_KEY, theme)


_PLACEHOLDER_HTML = """
<!DOCTYPE html><html><body style="background:#070D1F;color:#5A6690;
font-family:Segoe UI;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;font-size:14px;text-align:center;">
<div>Map requires PySide6-WebEngine.<br>
Node data and analytics continue normally.</div></body></html>
"""
