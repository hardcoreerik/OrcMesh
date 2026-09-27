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


def _write_pack(directory, stem: str, **overrides):
    """A real manifest+archive pair on disk, mirroring OrcMaps' triplet rule."""
    payload = {
        "manifest_version": 1,
        "pack_id": f"{stem}-pack",
        "display_name": "Oregon regional",
        "region_name": "Oregon, USA",
        "bounds": {"min_lon": -124.0, "min_lat": 42.0, "max_lon": -122.0, "max_lat": 45.0},
        "min_zoom": 3,
        "max_zoom": 12,
        "pack_class": "open",
        "required_attribution": ["© OpenStreetMap contributors"],
        "attribution_links": ["https://www.openstreetmap.org/copyright"],
    }
    payload.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    (directory / f"{stem}.pmtiles").write_bytes(b"")


def _pack(tmp_path):
    """One discovered regional pack, the way the app discovers it."""
    _write_pack(tmp_path, "oregon")

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


class TestPackSetComposition:
    """A chosen pack is a *preference*, not the whole basemap.

    OrcMaps resolves a pack per view, so the tile server has to hold the whole
    installed set: a world overview only covers the low zooms a regional pack
    cannot contain, and it can only do that if it is being served. These tests
    pin that, plus the two things that must widen with the set — the credit
    line and the zoom clamp.
    """

    @staticmethod
    def _two_packs(tmp_path):
        """A world overview (z0-8, no required credit) and a regional pack."""
        world_dir = tmp_path / "world"
        world_dir.mkdir()
        _write_pack(
            world_dir, "world-overview", display_name="World Overview",
            bounds={"min_lon": -180.0, "min_lat": -85.05, "max_lon": 180.0, "max_lat": 85.05},
            min_zoom=0, max_zoom=8, pack_class="clean",
            required_attribution=[], attribution_links=["https://www.naturalearthdata.com/"],
        )
        regional = _pack(tmp_path)  # z3-12, OSM credit
        world = discover_packs([world_dir])[0]
        return world, regional

    def test_the_chosen_pack_is_served_first(self, tmp_path):
        widget = _widget()
        world, regional = self._two_packs(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), regional, [world, regional])

            served = widget.offline_packs
            assert served[0] is regional, "the user's choice takes precedence on ties"
            assert world in served, "the overview must be served to cover low zooms"
        finally:
            widget.shutdown()

    def test_a_pack_is_not_served_twice(self, tmp_path):
        widget = _widget()
        world, regional = self._two_packs(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), regional, [regional, world])

            stems = [p.stem for p in widget.offline_packs]
            assert stems.count(regional.stem) == 1
        finally:
            widget.shutdown()

    def test_the_zoom_clamp_widens_to_the_whole_set(self, tmp_path):
        """Clamping to the chosen pack would make the world unreachable."""
        widget = _widget()
        world, regional = self._two_packs(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), regional, [world, regional])

            spec = widget._bridge.set_basemap.call_args.args[0]
            assert (spec["min_zoom"], spec["max_zoom"]) == (0, 12)
        finally:
            widget.shutdown()

    def test_every_served_packs_credit_is_shown(self, tmp_path):
        """A tile may come from any served pack, so all credits must appear."""
        widget = _widget()
        world, regional = self._two_packs(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), regional, [world, regional])

            attribution = widget._bridge.set_basemap.call_args.args[0]["attribution"]
            assert "OpenStreetMap" in attribution, "the regional pack's credit"
            assert "naturalearthdata.com" in attribution, "the overview's credit"
        finally:
            widget.shutdown()

    def test_switching_online_clears_the_served_set(self, tmp_path):
        widget = _widget()
        world, regional = self._two_packs(tmp_path)
        try:
            widget.show_offline_pack(_tools(tmp_path), regional, [world, regional])

            widget.show_online_basemap()

            assert widget.offline_packs == []
        finally:
            widget.shutdown()
