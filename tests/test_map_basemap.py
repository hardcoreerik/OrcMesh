"""Basemap source wiring: online tiles ↔ an offline OrcMaps pack.

MapWidget owns the tile server's lifetime and pushes a description of the
basemap to the page; the page (map.js) only swaps layers. These tests pin that
payload contract — URL, attribution, zoom clamp, and the cache-busting
revision a theme change needs — without a browser and without rendering a tile.
"""
from __future__ import annotations

import json
import sys
import unittest.mock as mock

from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.services.orcmaps import (
    STYLE_DARK,
    STYLE_LIGHT,
    OrcMapsTools,
    discover_packs,
)
from meshchat.ui.map.map_widget import MapWidget


def _pack(tmp_path):
    """A real manifest+archive pair, discovered the way the app does it."""
    (tmp_path / "oregon.manifest.json").write_text(json.dumps({
        "manifest_version": 1,
        "pack_id": "test-pack",
        "display_name": "Oregon regional",
        "region_name": "Oregon, USA",
        "bounds": {"min_lon": -124.0, "min_lat": 42.0, "max_lon": -122.0, "max_lat": 45.0},
        "min_zoom": 3,
        "max_zoom": 12,
        "pack_class": "open",
        "required_attribution": ["© OpenStreetMap contributors"],
        "attribution_links": ["https://www.openstreetmap.org/copyright"],
    }), encoding="utf-8")
    (tmp_path / "oregon.pmtiles").write_bytes(b"")

    packs = discover_packs([tmp_path])
    assert len(packs) == 1
    return packs[0]


def _tools(tmp_path) -> OrcMapsTools:
    # Only used to construct the server; no tile is ever rendered here.
    return OrcMapsTools(home=tmp_path, inspect=tmp_path / "orcmap_pack_inspect.exe")


def _widget() -> MapWidget:
    """A MapWidget with its bridge replaced, so payloads can be asserted."""
    widget = MapWidget()
    widget._bridge = mock.Mock()
    return widget


class TestOfflineBasemapWiring:
    def test_offline_pack_payload_describes_the_pack(self, tmp_path):
        widget = _widget()
        pack = _pack(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), pack)

            spec = widget._bridge.set_basemap.call_args.args[0]
            assert spec["kind"] == "offline"
            assert spec["url"].startswith("http://127.0.0.1:")
            assert spec["label"] == "Oregon regional"
            # The page clamps the viewport to this, so it must be the pack's.
            assert (spec["min_zoom"], spec["max_zoom"]) == (3, 12)
            assert "OpenStreetMap" in spec["attribution"]
            assert "openstreetmap.org" in spec["attribution"]
            assert widget.offline_pack is pack
        finally:
            widget.shutdown()

    def test_theme_change_refetches_tiles_in_the_new_style(self, tmp_path):
        widget = _widget()
        pack = _pack(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), pack)
            first = widget._bridge.set_basemap.call_args.args[0]
            assert widget._tile_server is not None
            assert widget._tile_server.style == STYLE_DARK

            widget._on_theme_changed("light")

            second = widget._bridge.set_basemap.call_args.args[0]
            assert widget._tile_server.style == STYLE_LIGHT
            assert second["url"] != first["url"], (
                "a style change renders different pixels for the same tile URL, "
                "so Leaflet has to be told to refetch"
            )
            assert second["kind"] == "offline"
        finally:
            widget.shutdown()

    def test_switching_back_online_stops_the_server(self, tmp_path):
        widget = _widget()
        pack = _pack(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), pack)
            assert widget._tile_server is not None

            widget.show_online_basemap()

            assert widget.offline_pack is None
            assert widget._tile_server is None
            assert widget._bridge.set_basemap.call_args.args[0] == {"kind": "online"}
        finally:
            widget.shutdown()

    def test_shutdown_is_safe_when_no_server_was_started(self):
        widget = _widget()
        widget.shutdown()  # must not raise
        assert widget.offline_pack is None
