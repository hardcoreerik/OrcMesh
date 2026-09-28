"""Tests for setup capability detection.

The rule under test is honesty: the wizard must be able to say what this
machine can and cannot do *before* the user commits to anything, and it must
never raise or guess on a machine with nothing installed.
"""
from __future__ import annotations

from pathlib import Path

from meshchat.services.orcmaps import OrcMapsPack, OrcMapsTools
from meshchat.services.provisioning.capability import (
    MODE_BUILD,
    MODE_DOWNLOAD,
    MODE_UNAVAILABLE,
    capability_summary,
    detect_capability,
    source_packs_for,
)


def _pack(stem: str, *, max_zoom: int, size_bytes: int, display_name: str = "") -> OrcMapsPack:
    return OrcMapsPack(
        stem=stem,
        pmtiles=Path(f"{stem}.pmtiles"),
        manifest=Path(f"{stem}.manifest.json"),
        sha256=None,
        display_name=display_name or stem,
        region_name="Test",
        min_zoom=0,
        max_zoom=max_zoom,
        pack_class="open",
        pack_version="1",
        schema_version="openmaptiles-3.16",
        content_profile="standard",
        attribution=("© OpenStreetMap contributors",),
        attribution_links=("https://www.openstreetmap.org/copyright",),
        bounds=(0.0, 0.0, 1.0, 1.0),
        size_bytes=size_bytes,
        output_sha256=None,
        priority=0,
    )


def _tools(tmp_path: Path, *, script: bool = True, pmtiles: bool = True) -> OrcMapsTools:
    return OrcMapsTools(
        home=tmp_path,
        inspect=tmp_path / "orcmap_pack_inspect.exe",
        verify=tmp_path / "orcmap_pack_verify.exe",
        provision_script=(tmp_path / "provision_pack.py") if script else None,
        pmtiles_cli=(tmp_path / "pmtiles.exe") if pmtiles else None,
    )


class TestSourceSelection:
    def test_only_packs_deep_enough_to_serve_the_zoom_qualify(self):
        packs = [_pack("shallow", max_zoom=9, size_bytes=261_000_000),
                 _pack("deep", max_zoom=13, size_bytes=84_000_000)]

        qualifying = source_packs_for(packs, 13)

        assert [p.stem for p in qualifying] == ["deep"]

    def test_the_smallest_qualifying_pack_is_preferred(self):
        """Any qualifier can serve the zoom, so take the least work to read."""
        packs = [_pack("huge", max_zoom=15, size_bytes=3_000_000_000),
                 _pack("small", max_zoom=13, size_bytes=84_000_000)]

        assert [p.stem for p in source_packs_for(packs, 13)] == ["small", "huge"]

    def test_a_missing_size_does_not_crash_the_ordering(self):
        packs = [_pack("unknown", max_zoom=13, size_bytes=None),  # type: ignore[arg-type]
                 _pack("known", max_zoom=13, size_bytes=10)]

        assert [p.stem for p in source_packs_for(packs, 13)] == ["known", "unknown"]

    def test_nothing_qualifies_when_every_pack_is_too_shallow(self):
        assert source_packs_for([_pack("shallow", max_zoom=9, size_bytes=1)], 13) == []


class TestModeChoice:
    def test_tools_and_a_deep_source_means_build(self, tmp_path):
        capability = detect_capability(
            _tools(tmp_path), [_pack("oregon", max_zoom=13, size_bytes=84_000_000)]
        )

        assert capability.mode == MODE_BUILD
        assert capability.can_build_locally is True

    def test_no_tools_but_a_catalogue_means_download(self, tmp_path):
        capability = detect_capability(
            None, [], catalog_available=True, catalog_reason="ignored"
        )

        assert capability.mode == MODE_DOWNLOAD
        assert capability.can_build_locally is False

    def test_neither_route_is_reported_as_unavailable(self, tmp_path):
        capability = detect_capability(None, [])

        assert capability.mode == MODE_UNAVAILABLE
        assert capability.mode_label == "Maps cannot be set up yet"

    def test_a_shallow_source_alone_is_not_enough_to_build(self, tmp_path):
        """z0-9 cannot yield street detail, so it must not claim it can."""
        capability = detect_capability(
            _tools(tmp_path), [_pack("us", max_zoom=9, size_bytes=261_000_000)]
        )

        assert capability.can_build_locally is True, "it can still cut z9 regions"
        assert capability.source_for(13) is None, "but not a z13 region"
        assert any("only reaches z9" in reason for reason in capability.reasons)

    def test_a_missing_provisioner_blocks_building(self, tmp_path):
        capability = detect_capability(
            _tools(tmp_path, script=False),
            [_pack("oregon", max_zoom=13, size_bytes=84_000_000)],
            catalog_available=True,
        )

        assert capability.can_build_locally is False
        assert capability.mode == MODE_DOWNLOAD
        assert any("provision_pack.py" in reason for reason in capability.reasons)

    def test_a_missing_pmtiles_binary_blocks_building(self, tmp_path):
        capability = detect_capability(
            _tools(tmp_path, pmtiles=False),
            [_pack("oregon", max_zoom=13, size_bytes=84_000_000)],
        )

        assert capability.can_build_locally is False
        assert any("go-pmtiles" in reason for reason in capability.reasons)

    def test_build_is_preferred_over_download_when_both_work(self, tmp_path):
        """Building needs no catalogue and no trust in a host."""
        capability = detect_capability(
            _tools(tmp_path),
            [_pack("oregon", max_zoom=13, size_bytes=84_000_000)],
            catalog_available=True,
        )

        assert capability.mode == MODE_BUILD


class TestReasons:
    def test_no_tools_explains_what_still_works(self):
        capability = detect_capability(None, [], catalog_available=True)

        assert any("Downloading ready-made packs does not need them" in r
                   for r in capability.reasons)

    def test_no_source_archive_says_what_is_needed(self, tmp_path):
        capability = detect_capability(_tools(tmp_path), [])

        assert any("state-level z13 archive" in reason for reason in capability.reasons)

    def test_a_missing_catalogue_reason_is_passed_through(self):
        capability = detect_capability(None, [], catalog_reason="No catalogue yet.")

        assert "No catalogue yet." in capability.reasons

    def test_reasons_are_always_present_when_nothing_is_possible(self):
        capability = detect_capability(None, [])

        assert capability.reasons, "an unavailable setup must explain itself"


class TestSummary:
    def test_build_summary_names_the_source(self, tmp_path):
        capability = detect_capability(
            _tools(tmp_path),
            [_pack("oregon", max_zoom=13, size_bytes=84_000_000, display_name="Oregon regional")],
        )

        assert "Oregon regional" in capability_summary(capability)

    def test_download_summary_promises_verification(self):
        capability = detect_capability(None, [], catalog_available=True)

        assert "verified" in capability_summary(capability)

    def test_unavailable_summary_points_at_the_details(self):
        capability = detect_capability(None, [])

        assert "cannot be set up" in capability_summary(capability)
