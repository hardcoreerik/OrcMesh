"""Tests for the OrcMaps bridge — tile math, pack discovery, and tile serving.

The rules under test are OrcMaps' contract, not OrcMesh preferences: a pack is
a stem-paired manifest+archive triplet, an archive without a manifest is
invisible, oversized manifests are refused, and attribution comes from the
manifest. The render/serve tests run against the real OrcMaps tools and packs
when they are present (this dev machine), and skip cleanly when they are not.
"""
from __future__ import annotations

import json
import struct
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.services.orcmaps import (
    STYLE_DARK,
    OrcMapsError,
    OrcMapsTools,
    TileServer,
    _ppm_to_png,
    attribution_html,
    default_pack_directories,
    discover_packs,
    find_tools,
    load_pack,
    render_tile,
    tile_bounds_deg,
    tile_center_deg,
    tile_index_deg,
)

# Real OrcMaps install, if this machine has one (see docs/orcmaps-integration.md).
_TOOLS = find_tools()
_PACKS = discover_packs(default_pack_directories(_TOOLS)) if _TOOLS else []


def _manifest(tmp_path: Path, name: str = "test", **overrides) -> Path:
    """Write a manifest+archive pair and return the manifest path."""
    payload = {
        "manifest_version": 1,
        "pack_id": "test-pack",
        "pack_version": "2026.09.1",
        "display_name": "Test Region",
        "region_id": "test",
        "region_name": "Test Region, Testland",
        "bounds": {"min_lon": -124.0, "min_lat": 42.0, "max_lon": -122.0, "max_lat": 45.0},
        "min_zoom": 1,
        "max_zoom": 13,
        "content_profile": "standard",
        "schema_version": "openmaptiles-3.16",
        "pack_class": "open",
        "required_attribution": ["© OpenStreetMap contributors"],
        "attribution_links": ["https://www.openstreetmap.org/copyright"],
        "priority": 10,
        "size_bytes": 1234,
        "output_sha256": "a" * 64,
    }
    payload.update(overrides)
    manifest = tmp_path / f"{name}.manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / f"{name}.pmtiles").write_bytes(b"")
    return manifest


# ── Tile math ────────────────────────────────────────────────────────────────

class TestTileMath:
    def test_root_tile_centre_is_the_origin(self):
        assert tile_center_deg(0, 0, 0) == (0.0, 0.0)

    def test_index_and_centre_round_trip(self):
        # A coordinate's tile always contains that coordinate.
        lat, lon, z = 44.05, -123.09, 10
        x, y = tile_index_deg(lat, lon, z)
        centre_lat, centre_lon = tile_center_deg(z, x, y)
        west, south, east, north = tile_bounds_deg(z, x, y)
        assert west <= lon <= east
        assert south <= lat <= north
        assert abs(centre_lat - lat) < 1.0 and abs(centre_lon - lon) < 1.0

    def test_bounds_are_ordered_and_bounded(self):
        west, south, east, north = tile_bounds_deg(1, 0, 0)
        assert west < east
        assert south < north
        assert -180.0 <= west and east <= 180.0
        # The Mercator projection clamps latitude well inside the poles.
        assert north <= 85.06 and south >= -85.06

    def test_index_is_clamped_to_the_world(self):
        assert tile_index_deg(89.9, -400.0, 2) == (0, 0)


# ── Manifest + discovery rules ───────────────────────────────────────────────

class TestPackDiscovery:
    def test_manifest_and_archive_are_paired_by_stem(self, tmp_path):
        _manifest(tmp_path, "oregon")
        packs = discover_packs([tmp_path])

        assert [p.stem for p in packs] == ["oregon"]
        pack = packs[0]
        assert pack.pmtiles.name == "oregon.pmtiles"
        assert pack.display_name == "Test Region"
        assert pack.attribution == ("© OpenStreetMap contributors",)
        assert pack.attribution_text == "© OpenStreetMap contributors"
        assert pack.zoom_label == "z1-13"
        assert pack.bounds == (-124.0, 42.0, -122.0, 45.0)

    def test_archive_without_a_manifest_is_invisible(self, tmp_path):
        # OrcMaps: "An archive alone is invisible: metadata is what makes a
        # pack usable." Rendering from a bare .pmtiles would bypass provenance.
        (tmp_path / "lone.pmtiles").write_bytes(b"")
        assert discover_packs([tmp_path]) == []

    def test_manifest_without_its_archive_is_skipped(self, tmp_path):
        manifest = _manifest(tmp_path, "orphan")
        (tmp_path / "orphan.pmtiles").unlink()

        assert discover_packs([tmp_path]) == []
        assert load_pack(manifest) is None

    def test_oversized_manifest_is_refused(self, tmp_path):
        manifest = _manifest(tmp_path, "huge", display_name="x" * 70_000)
        assert manifest.stat().st_size > 64 * 1024

        assert load_pack(manifest) is None

    def test_unreadable_manifest_does_not_hide_the_good_ones(self, tmp_path):
        _manifest(tmp_path, "good")
        bad = tmp_path / "bad.manifest.json"
        bad.write_text("{not json", encoding="utf-8")
        (tmp_path / "bad.pmtiles").write_bytes(b"")

        assert [p.stem for p in discover_packs([tmp_path])] == ["good"]

    def test_unusable_bounds_are_tolerated(self, tmp_path):
        _manifest(tmp_path, "nobounds", bounds={"min_lon": "nope"})
        pack = load_pack(tmp_path / "nobounds.manifest.json")

        assert pack is not None and pack.bounds is None

    def test_packs_are_ordered_by_priority_then_zoom(self, tmp_path):
        _manifest(tmp_path, "low", priority=1, max_zoom=8)
        _manifest(tmp_path, "high", priority=50, max_zoom=13)

        assert [p.stem for p in discover_packs([tmp_path])] == ["high", "low"]

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert discover_packs([tmp_path / "nope"]) == []


class TestCoverage:
    def test_zoom_range_is_respected(self, tmp_path):
        _manifest(tmp_path, "p", min_zoom=5, max_zoom=10)
        pack = load_pack(tmp_path / "p.manifest.json")
        assert pack is not None
        # A tile inside the bounds, at a zoom below/above the pack's range.
        x, y = tile_index_deg(44.0, -123.0, 4)
        assert pack.covers(4, x, y) is False
        x, y = tile_index_deg(44.0, -123.0, 10)
        assert pack.covers(10, x, y) is True

    def test_bounds_are_respected(self, tmp_path):
        _manifest(tmp_path, "p")
        pack = load_pack(tmp_path / "p.manifest.json")
        assert pack is not None
        inside = tile_index_deg(44.0, -123.0, 10)
        outside = tile_index_deg(48.85, 2.35, 10)  # Paris
        assert pack.covers(10, *inside) is True
        assert pack.covers(10, *outside) is False

    def test_pack_without_bounds_covers_its_zoom_range(self, tmp_path):
        _manifest(tmp_path, "p", bounds={})
        pack = load_pack(tmp_path / "p.manifest.json")
        assert pack is not None
        assert pack.covers(10, *tile_index_deg(48.85, 2.35, 10)) is True


# ── Attribution (manifest → Leaflet control) ─────────────────────────────────

class TestAttributionHtml:
    """A pack's required attribution must be shown wherever its tiles are, so
    the manifest contract survives the trip into the web view."""

    def test_text_and_http_links_are_rendered(self, tmp_path):
        _manifest(tmp_path, "p")
        pack = load_pack(tmp_path / "p.manifest.json")
        assert pack is not None

        html = attribution_html(pack)

        assert "© OpenStreetMap contributors" in html
        assert '<a href="https://www.openstreetmap.org/copyright"' in html
        # The visible label is the link's host, so the control stays readable.
        assert ">www.openstreetmap.org</a>" in html

    def test_manifest_text_is_escaped(self, tmp_path):
        _manifest(tmp_path, "p", required_attribution=["<script>alert(1)</script>"])
        pack = load_pack(tmp_path / "p.manifest.json")
        assert pack is not None

        html = attribution_html(pack)

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_non_http_links_are_shown_as_text_not_links(self, tmp_path):
        # A manifest is data even when its source is trusted.
        _manifest(tmp_path, "p", attribution_links=["javascript:alert(1)"])
        pack = load_pack(tmp_path / "p.manifest.json")
        assert pack is not None

        html = attribution_html(pack)

        assert "<a href" not in html
        assert "javascript:alert(1)" in html

    def test_pack_without_attribution_renders_empty(self, tmp_path):
        _manifest(tmp_path, "p", required_attribution=[], attribution_links=[])
        pack = load_pack(tmp_path / "p.manifest.json")
        assert pack is not None

        assert attribution_html(pack) == ""


# ── Tile server ──────────────────────────────────────────────────────────────

class TestTileServer:
    @staticmethod
    def _server(tmp_path: Path) -> TileServer:
        _manifest(tmp_path, "p")
        packs = discover_packs([tmp_path])
        tools = _TOOLS or OrcMapsTools(
            home=tmp_path, inspect=tmp_path / "missing.exe",
        )
        return TileServer(tools, packs, style=STYLE_DARK)

    def test_url_template_is_leaflet_shaped(self, tmp_path):
        server = self._server(tmp_path)
        server.start()
        try:
            assert server.url_template.startswith("http://127.0.0.1:")
            assert server.url_template.endswith("/tiles/{z}/{x}/{y}.png")
        finally:
            server.stop()

    def test_tile_outside_every_pack_is_a_404(self, tmp_path):
        # No CLI needed: coverage is decided from the manifest before rendering.
        server = self._server(tmp_path)
        server.start()
        try:
            x, y = tile_index_deg(48.85, 2.35, 10)  # Paris; pack is Oregon-ish
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{server.url_template.format(z=10, x=x, y=y)}", timeout=5)
            assert exc.value.code == 404
        finally:
            server.stop()

    def test_status_reports_packs_and_attribution(self, tmp_path):
        server = self._server(tmp_path)
        server.start()
        try:
            with urllib.request.urlopen(f"{server.base_url}/status", timeout=5) as response:
                status = json.loads(response.read())
            assert status["packs"][0]["display_name"] == "Test Region"
            assert status["packs"][0]["attribution"] == ["© OpenStreetMap contributors"]
            assert status["style"] == STYLE_DARK
            assert status["packs"][0]["zoom"] == [1, 13]
        finally:
            server.stop()

    def test_unknown_path_is_a_404(self, tmp_path):
        server = self._server(tmp_path)
        server.start()
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{server.base_url}/nope", timeout=5)
            assert exc.value.code == 404
        finally:
            server.stop()

    def test_a_hostile_tile_path_cannot_escape_the_root(self, tmp_path):
        server = self._server(tmp_path)
        server.start()
        try:
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{server._base}/tiles/../../etc/passwd", timeout=5)
        finally:
            server.stop()
    def test_a_cache_buster_query_still_resolves_to_a_tile(self, tmp_path):
        """Leaflet appends ?v=n when the render style changes.

        Regression: the handler split the raw path, so "185.png?v=1" failed its
        .png check and every offline tile 404'd — the map stayed blank while the
        server itself worked perfectly for a query-less URL. A covered tile with
        a query must therefore fail as a *render* error (500, the fake tool path
        can't run), never as "not found" (404).
        """
        server = self._server(tmp_path)
        server.start()
        try:
            x, y = tile_index_deg(44.0, -123.0, 10)  # inside the test pack
            url = f"{server.url_template.format(z=10, x=x, y=y)}?v=2"
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(url, timeout=30)
            assert exc.value.code == 500, "query string broke tile path parsing"
        finally:
            server.stop()

# ── Real OrcMaps integration (skipped when the tools aren't built) ───────────

@pytest.mark.skipif(not _TOOLS, reason="OrcMaps host tools not built on this machine")
class TestRealOrcMaps:
    def test_pack_directories_produce_packs(self):
        assert _PACKS, "tools found but no packs discovered under data/local"

    def test_render_tile_returns_real_png_bytes(self):
        pack = next(
            (p for p in _PACKS if p.min_zoom <= 10 <= p.max_zoom and p.bounds), None,
        )
        if pack is None:
            pytest.skip("no pack covers zoom 10 on this machine")
        assert pack.bounds is not None
        min_lon, min_lat, max_lon, max_lat = pack.bounds
        x, y = tile_index_deg((min_lat + max_lat) / 2, (min_lon + max_lon) / 2, 10)

        png = render_tile(_TOOLS, pack, 10, x, y, style=STYLE_DARK)

        assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert len(png) > 1000, "suspiciously small tile"

    def test_server_serves_a_rendered_tile_and_caches_it(self):
        pack = next(
            (p for p in _PACKS if p.min_zoom <= 10 <= p.max_zoom and p.bounds), None,
        )
        if pack is None:
            pytest.skip("no pack covers zoom 10 on this machine")
        assert pack.bounds is not None
        min_lon, min_lat, max_lon, max_lat = pack.bounds
        x, y = tile_index_deg((min_lat + max_lat) / 2, (min_lon + max_lon) / 2, 10)

        server = TileServer(_TOOLS, [pack], style=STYLE_DARK)
        server.start()
        try:
            # With the cache-buster the app actually sends (see MapWidget).
            url = server.url_template.format(z=10, x=x, y=y) + "?v=1"
            with urllib.request.urlopen(url, timeout=30) as response:
                assert response.headers["Content-Type"] == "image/png"
                first = response.read()
            with urllib.request.urlopen(url, timeout=30) as response:
                second = response.read()

            assert first[:8] == b"\x89PNG\r\n\x1a\n"
            assert first == second
            # Second request must be a cache hit, not a second render.
            assert server.cache.stats()["hits"] >= 1
        finally:
            server.stop()

    def test_rendering_a_tile_outside_the_pack_raises(self):
        pack = next((p for p in _PACKS if p.bounds), None)
        if pack is None:
            pytest.skip("no pack with bounds on this machine")
        x, y = tile_index_deg(48.85, 2.35, 10)  # Paris
        if pack.covers(10, x, y):
            pytest.skip("pack covers Paris; cannot test out-of-coverage")
        with pytest.raises(OrcMapsError):
            render_tile(_TOOLS, pack, 10, x, y)

# ── PPM → PNG encoding (pure stdlib: no Qt, no subprocess, no OrcMaps) ───────

def _png_chunks(png: bytes) -> dict[bytes, bytes]:
    """Walk a PNG's chunks, validating each length field as it goes."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "missing PNG signature"
    chunks: dict[bytes, bytes] = {}
    pos = 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        tag = png[pos + 4:pos + 8]
        payload = png[pos + 8:pos + 8 + length]
        assert len(payload) == length, f"chunk {tag!r} is truncated"
        chunks[tag] = payload
        pos += 12 + length
    assert pos == len(png), "trailing bytes after IEND"
    return chunks


class TestPpmEncoding:
    """OrcMaps' CLI writes a P6 PPM; the web view needs PNG.

    This was a QImage round trip first, which failed at runtime on the tile
    server's worker thread ("fromData called with wrong argument values") and
    then dropped the connection. Stdlib-only now, so it is pinned down here
    with no Qt, no subprocess, and no OrcMaps install.
    """

    @staticmethod
    def _ppm(width: int, height: int, pixels: bytes) -> bytes:
        return f"P6\n{width} {height}\n255\n".encode("ascii") + pixels

    def test_p6_becomes_a_structurally_valid_png(self):
        ppm = self._ppm(2, 1, bytes([255, 0, 0, 0, 255, 0]))

        chunks = _png_chunks(_ppm_to_png(ppm))

        assert struct.unpack(">II", chunks[b"IHDR"][:8]) == (2, 1)
        assert chunks[b"IHDR"][8] == 8       # bit depth
        assert chunks[b"IHDR"][9] == 2       # colour type: truecolour RGB
        assert chunks[b"IEND"] == b""

    def test_pixels_survive_the_round_trip(self):
        ppm = self._ppm(2, 1, bytes([255, 0, 0, 0, 255, 0]))

        chunks = _png_chunks(_ppm_to_png(ppm))

        assert zlib.decompress(chunks[b"IDAT"]) == b"\x00" + bytes([255, 0, 0, 0, 255, 0])

    def test_every_scanline_gets_a_filter_byte(self):
        ppm = self._ppm(1, 2, bytes([9, 9, 9, 8, 8, 8]))

        chunks = _png_chunks(_ppm_to_png(ppm))

        assert zlib.decompress(chunks[b"IDAT"]) == (
            b"\x00" + bytes([9, 9, 9]) + b"\x00" + bytes([8, 8, 8])
        )

    def test_header_comments_are_skipped(self):
        ppm = b"P6\n# rendered by OrcMaps\n1 1\n255\n" + bytes([1, 2, 3])

        assert struct.unpack(">II", _png_chunks(_ppm_to_png(ppm))[b"IHDR"][:8]) == (1, 1)

    def test_truncated_pixel_data_is_rejected(self):
        with pytest.raises(OrcMapsError):
            _ppm_to_png(self._ppm(4, 4, bytes(3)))

    def test_non_p6_input_is_rejected(self):
        with pytest.raises(OrcMapsError):
            _ppm_to_png(b"P3\n1 1\n255\n0 0 0\n")



def test_find_tools_on_missing_home_returns_none(tmp_path, monkeypatch):
    # Only the bogus home is probed: otherwise this would find whatever real
    # OrcMaps checkout happens to sit next to this one and prove nothing.
    monkeypatch.delenv("ORCMESH_ORCMAP_INSPECT", raising=False)
    monkeypatch.setattr(
        "meshchat.services.orcmaps._candidate_homes", lambda explicit: [tmp_path],
    )
    assert find_tools(home=tmp_path) is None
