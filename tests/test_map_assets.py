"""Guards the map's web assets and the JS-bridge readiness watchdog.

index.html loads vendor/qwebchannel.js to construct the Python↔JS bridge, but
nothing used to produce that file — fetch_vendors.py only downloaded
Leaflet/MarkerCluster, and build.ps1 only re-ran it when leaflet.js was
missing, so a partially populated vendor/ stayed broken indefinitely. With
QWebChannel undefined the page's JS never called mapReady(), and MapBridge
buffered every JS call for the rest of the session: the map drew a basemap
with no node pins and no way to click one, and the only trace was a JS console
line. These tests pin both halves of the fix — the asset is produced from Qt's
own resource, and a bridge that never connects says so out loud.
"""
from __future__ import annotations

import logging
import sys
import unittest.mock as mock

from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.ui.map.map_bridge import MapBridge
from meshchat.ui.map.map_widget import _WEB_DIR, ensure_qwebchannel_asset


class TestQwebchannelAsset:
    def test_missing_asset_is_extracted_from_the_qt_resource(self, tmp_path):
        asset = ensure_qwebchannel_asset(tmp_path)

        assert asset is not None, "Qt resource :/qtwebchannel/qwebchannel.js was unavailable"
        assert asset == tmp_path / "vendor" / "qwebchannel.js"
        assert asset.stat().st_size > 0
        assert "QWebChannel" in asset.read_text(encoding="utf-8")

    def test_existing_asset_is_never_rewritten(self, tmp_path):
        target = tmp_path / "vendor" / "qwebchannel.js"
        target.parent.mkdir(parents=True)
        target.write_text("sentinel", encoding="utf-8")

        assert ensure_qwebchannel_asset(tmp_path) == target
        assert target.read_text(encoding="utf-8") == "sentinel"

    def test_the_shipped_web_dir_ends_up_with_the_asset(self):
        # vendor/ is generated (gitignored), so this asserts the runtime
        # contract MapWidget relies on rather than checking in the file: the
        # helper must be able to produce it on demand, offline.
        asset = ensure_qwebchannel_asset(_WEB_DIR)

        assert asset is not None and asset.is_file()
        assert asset.stat().st_size > 0


class TestBridgeReadyWatchdog:
    def test_buffered_calls_are_flushed_once_the_page_reports_ready(self):
        bridge = MapBridge()
        page = mock.Mock()
        bridge.set_page(page)
        bridge.update_node(1, 45.0, -122.0, "Node", "CLIENT")
        assert bridge.pending_call_count == 1

        bridge.mapReady()

        assert bridge.is_ready is True
        assert bridge.pending_call_count == 0
        page.runJavaScript.assert_called_once()

    def test_a_bridge_that_never_connects_warns_with_the_queued_call_count(self, caplog):
        bridge = MapBridge()
        bridge.set_page(mock.Mock())
        bridge.update_node(1, 45.0, -122.0, "Node", "CLIENT")

        with caplog.at_level(logging.WARNING):
            bridge._on_ready_timeout()

        assert "never reported" in caplog.text
        assert "1 queued call" in caplog.text

    def test_no_warning_once_the_bridge_is_ready(self, caplog):
        bridge = MapBridge()
        bridge.set_page(mock.Mock())
        bridge.mapReady()

        with caplog.at_level(logging.WARNING):
            bridge._on_ready_timeout()

        assert caplog.text == ""
