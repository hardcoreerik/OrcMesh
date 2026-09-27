"""MeshChat – MapWidget: QWebEngineView with Leaflet map."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSettings, QTimer, QUrl, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

if TYPE_CHECKING:
    from meshchat.services.orcmaps import OrcMapsPack, OrcMapsTools

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


_QWEBCHANNEL_RESOURCE = ":/qtwebchannel/qwebchannel.js"

#: How long the page gets to report its JS bridge as ready before we log a
#: warning. Generous, because it only has to cover page load: a false positive
#: would be noise, while a missed one is invisible to everyone.
_BRIDGE_READY_TIMEOUT_MS = 10_000


def ensure_qwebchannel_asset(web_dir: Path = _WEB_DIR) -> Path | None:
    """Return the page's qwebchannel.js, extracting it from Qt when absent.

    index.html loads ``vendor/qwebchannel.js`` to construct the Python↔JS
    bridge, but nothing in the build ever produced that file:
    ``scripts/fetch_vendors.py`` only downloaded Leaflet/MarkerCluster, and
    ``build.ps1`` only re-ran it when ``vendor/leaflet/leaflet.js`` was missing
    — so a partially populated ``vendor/`` stayed broken forever. With
    ``QWebChannel`` undefined the page's JS never calls ``mapReady()``, and
    ``MapBridge`` then buffered every JS call for the rest of the session: the
    map rendered a basemap with no node pins and no way to click one, and
    nothing was logged beyond a console line.

    Qt ships the matching script as a resource, so this needs no network access
    and cannot drift from the installed Qt version. Returns the asset path, or
    None when it could not be produced (read-only install, unregistered
    resource) — index.html falls back to the ``qrc:///`` URL in that case.

    Idempotent: an existing non-empty asset is never rewritten.
    """
    target = web_dir / "vendor" / "qwebchannel.js"
    try:
        if target.is_file() and target.stat().st_size > 0:
            return target

        from PySide6.QtCore import QFile, QIODevice
        # Importing this is what registers the module's qrc resources, making
        # _QWEBCHANNEL_RESOURCE resolvable at all.
        from PySide6.QtWebChannel import QWebChannel  # noqa: F401

        source = QFile(_QWEBCHANNEL_RESOURCE)
        if not source.exists():
            log.warning(
                "Qt resource %s is not registered; the map page will use its "
                "qrc:/// fallback", _QWEBCHANNEL_RESOURCE,
            )
            return None
        if not source.open(QIODevice.OpenModeFlag.ReadOnly):
            log.warning("Could not open Qt resource %s", _QWEBCHANNEL_RESOURCE)
            return None
        try:
            data = bytes(source.readAll().data())
        finally:
            source.close()
        if not data:
            log.warning("Qt resource %s was empty", _QWEBCHANNEL_RESOURCE)
            return None

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        log.info(
            "Wrote %s (%d bytes) from the Qt resource for the map bridge",
            target, len(data),
        )
        return target
    except Exception as exc:
        log.warning("Could not prepare the map's qwebchannel.js asset: %s", exc)
        return None


class MapWidget(QWidget):
    """
    Leaflet map embedded in a QWebEngineView.
    Falls back to a placeholder when QtWebEngine is unavailable.
    """

    # object, not int: see MapBridge.node_clicked — Qt int signals are
    # 32-bit signed and truncate a real (unsigned, can exceed 0x7FFFFFFF)
    # Meshtastic node_num. Must match MapBridge's signal type since this
    # forwards it directly (signal-to-signal connect below).
    node_clicked = Signal(object)   # node_num

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # _build_webengine() sets self._bridge when QtWebEngine is available;
        # default to None here first so _build_placeholder()'s no-webengine
        # path still has a defined self._bridge to guard against.
        self._bridge = None
        self._did_initial_show = False
        # Offline (OrcMaps) basemap state. The tile server is owned here
        # because its lifetime is exactly "this map view is displaying that
        # pack"; the *choice* of pack lives with the menu that offers it.
        self._tile_server = None
        self._offline_pack: OrcMapsPack | None = None
        self._offline_packs: list[OrcMapsPack] = []
        self._tile_revision = 0

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
        # Buffering JS calls until the page reports ready is deliberate (its
        # updateNode() doesn't exist yet), but "buffered forever" is
        # indistinguishable from a working map that simply has no nodes —
        # which is exactly what a missing bridge script silently produced.
        self._bridge.start_ready_watchdog(_BRIDGE_READY_TIMEOUT_MS)

        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        self._view.loadFinished.connect(
            lambda ok: log.info("Map page load finished: %s", "ok" if ok else "FAILED")
        )

        # Produces the bridge script index.html needs, from Qt's own resource.
        ensure_qwebchannel_asset()

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

    def set_selected_node(self, node_num: int | None) -> None:
        if self._bridge:
            self._bridge.set_selected_node(node_num)

    # ------------------------------------------------------------------
    # Basemap source — online tiles or an offline OrcMaps pack
    # ------------------------------------------------------------------

    def show_online_basemap(self) -> None:
        """Go back to the built-in online basemap (CARTO/OSM)."""
        self._stop_tile_server()
        self._offline_pack = None
        self._offline_packs = []
        if self._bridge:
            self._bridge.set_basemap({"kind": "online"})

    def show_offline_pack(
        self,
        tools: OrcMapsTools,
        pack: OrcMapsPack,
        packs: list[OrcMapsPack] | None = None,
    ) -> None:
        """Serve the basemap from the local OrcMaps packs.

        ``pack`` is the source the user chose and is tried first when more than
        one pack can serve a tile. ``packs`` is everything installed, and it
        matters: OrcMaps resolves a pack *per view*, not per install, so a
        world overview has to be in the set for the zoom levels a regional or
        city pack cannot cover completely. Serving only the chosen pack is what
        made zooming out show near-empty tiles.

        Raises OrcMapsError if the tile server can't start, so the caller can
        report it and fall back to the online basemap instead of leaving the
        user staring at an empty map.
        """
        from meshchat.services.orcmaps import TileServer

        self._stop_tile_server()
        served = [pack]
        for other in packs or []:
            if other.stem != pack.stem:
                served.append(other)
        server = TileServer(tools, served, style=self._style_for_theme())
        server.start()
        self._tile_server = server
        self._offline_pack = pack
        self._offline_packs = served
        self._push_offline_basemap()

    @property
    def offline_pack(self) -> OrcMapsPack | None:
        """The pack the user chose, or None when online."""
        return self._offline_pack

    @property
    def offline_packs(self) -> list[OrcMapsPack]:
        """Every pack the tile server will draw from (empty when online)."""
        return list(self._offline_packs)

    def _style_for_theme(self) -> str:
        from meshchat.services.orcmaps import STYLE_DARK, STYLE_LIGHT
        theme = QSettings().value(_THEME_SETTINGS_KEY, "dark")
        return STYLE_LIGHT if theme == "light" else STYLE_DARK

    def _push_offline_basemap(self) -> None:
        """Push (or re-push) the offline layer to the page.

        Each push bumps a cache-busting revision: a style change renders
        different pixels for the same tile URL, and Leaflet would otherwise
        keep showing the previous style's cached images.
        """
        server, pack = self._tile_server, self._offline_pack
        if server is None or pack is None or self._bridge is None:
            return
        from meshchat.services.orcmaps import attribution_html_for

        served = self._offline_packs or [pack]
        self._tile_revision += 1
        self._bridge.set_basemap({
            "kind": "offline",
            "url": f"{server.url_template}?v={self._tile_revision}",
            # Every served pack may render a tile, so every required credit is
            # shown; the zoom range is the union, else the map clamps to the
            # chosen pack and the world overview can never be reached.
            "attribution": attribution_html_for(served),
            "min_zoom": min(p.min_zoom for p in served),
            "max_zoom": max(p.max_zoom for p in served),
            "label": pack.display_name,
        })
        log.info(
            "Map: offline basemap from pack '%s' (%s, %s) via %s, %d pack(s) served",
            pack.display_name, pack.pack_class, pack.zoom_label,
            server.base_url, len(served),
        )

    def _stop_tile_server(self) -> None:
        server, self._tile_server = self._tile_server, None
        if server is not None:
            server.stop()

    def shutdown(self) -> None:
        """Release the tile server; the map view is going away."""
        self._stop_tile_server()

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
        # A pack restored at startup may have been pushed while the page was
        # still loading (buffered, then flushed by mapReady). Re-push now that
        # the page is definitely ready, so the style always matches the theme.
        if self._tile_server is not None:
            self._push_offline_basemap()

    def _on_theme_changed(self, theme: str) -> None:
        QSettings().setValue(_THEME_SETTINGS_KEY, theme)
        if self._tile_server is not None:
            # Offline tiles are rendered per style, so a theme change means
            # re-rendering: swap the OrcMaps style and push a new revision.
            from meshchat.services.orcmaps import STYLE_DARK, STYLE_LIGHT
            self._tile_server.set_style(STYLE_LIGHT if theme == "light" else STYLE_DARK)
            self._push_offline_basemap()


_PLACEHOLDER_HTML = """
<!DOCTYPE html><html><body style="background:#070D1F;color:#5A6690;
font-family:Segoe UI;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;font-size:14px;text-align:center;">
<div>Map requires PySide6-WebEngine.<br>
Node data and analytics continue normally.</div></body></html>
"""
