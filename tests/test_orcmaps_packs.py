"""Tests for OrcMaps pack management — card layout, provenance, verify, cut.

These cover the second OrcMaps surface: getting a pack onto a device's card.
The rules under test mix OrcMaps' contract with OrcMesh's own guardrails:

* a card root and a pack directory are different things, and handing the wrong
  one to the verify tool reports a good card as unusable;
* a build that cannot attribute itself to a commit refuses to run rather than
  writing a false provenance record;
* an exit status of 0 is falsy in Python, so a successful build must be reported
  as a success, not a failure;
* a built pack is never reported as success until OrcMaps' own discovery accepts
  the staged layout.

Tests needing the real OrcMaps checkout, its built tools, or an installed pack
skip cleanly when this machine has none. Nothing here touches the network: the
cut-a-pack test runs ``--dry-run``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from meshchat.controllers import orcmaps_controller
from meshchat.controllers.orcmaps_controller import _OrcMapsWorker
from meshchat.services import orcmaps
from meshchat.services.orcmaps import (
    DEVICE_PACK_DIRNAME,
    OrcMapsError,
    OrcMapsTools,
    PinPackRequest,
    checkout_commit,
    default_pack_directories,
    device_pack_dir,
    discover_packs,
    find_tools,
    provision_pin_pack,
    resolve_pack_directory,
    valid_pack_name,
    verify_directory,
)

# The real OrcMaps install, when this machine has one (docs/orcmaps-integration.md).
_TOOLS = find_tools()
_PACKS = discover_packs(default_pack_directories(_TOOLS)) if _TOOLS else []

needs_tools = pytest.mark.skipif(_TOOLS is None, reason="OrcMaps host tools are not built here")
needs_verify = pytest.mark.skipif(
    _TOOLS is None or _TOOLS.verify is None, reason="orcmap_pack_verify is not built here",
)
needs_pack = pytest.mark.skipif(not _PACKS, reason="no OrcMaps pack is installed here")
needs_script = pytest.mark.skipif(
    _TOOLS is None or _TOOLS.provision_script is None,
    reason="OrcMaps' provision_pack.py is not in this checkout",
)


def _request(tmp_path: Path, **overrides) -> PinPackRequest:
    fields = {
        "source_manifest": tmp_path / "source.manifest.json",
        "lat": 44.0,
        "lon": -122.0,
        "radius_km": 3.0,
        "name": "pin-3km",
        "card_root": tmp_path / "CARD",
        "dry_run": True,
    }
    fields.update(overrides)
    return PinPackRequest(**fields)  # type: ignore[arg-type]


def _stage_card(card_root: Path, pack) -> Path:
    """Lay a real pack out on a card the way OrcMaps' discovery expects it."""
    pack_dir = device_pack_dir(card_root)
    pack_dir.mkdir(parents=True, exist_ok=True)
    for source in (pack.manifest, pack.pmtiles):
        (pack_dir / source.name).write_bytes(source.read_bytes())
    return pack_dir


def _fake_checkout(tmp_path: Path, body: str = "") -> tuple[Path, Path]:
    """A directory shaped like an OrcMaps checkout plus a stub provisioner."""
    home = tmp_path / "orcmaps"
    script = home / "tools" / "pack-builder" / "provision_pack.py"
    script.parent.mkdir(parents=True)
    script.write_text(body, encoding="utf-8")
    return home, script


def _tools(home: Path, script: Path | None = None, **overrides) -> OrcMapsTools:
    fields = {
        "home": home,
        "inspect": home / "build-pack-inspect" / "Release" / "orcmap_pack_inspect.exe",
        "provision_script": script,
    }
    fields.update(overrides)
    return OrcMapsTools(**fields)  # type: ignore[arg-type]


# ── Card layout: a card root is not a pack directory ────────────────────────


def test_packs_live_in_the_orcmaps_directory_of_a_card():
    assert device_pack_dir(Path("G:/")) == Path("G:/") / DEVICE_PACK_DIRNAME


def test_a_card_root_is_resolved_to_its_pack_directory(tmp_path):
    card = tmp_path / "CARD"
    device_pack_dir(card).mkdir(parents=True)

    assert resolve_pack_directory(card) == device_pack_dir(card)


def test_a_pack_directory_is_left_alone(tmp_path):
    pack_dir = device_pack_dir(tmp_path / "CARD")
    pack_dir.mkdir(parents=True)

    assert resolve_pack_directory(pack_dir) == pack_dir


def test_a_directory_without_a_pack_subdirectory_is_already_a_pack_directory(tmp_path):
    """Otherwise a plain folder of packs would be verified as an empty card."""
    plain = tmp_path / "LOOSE"
    plain.mkdir()

    assert resolve_pack_directory(plain) == plain


# ── Provenance: the builder commit is read, never invented ──────────────────


def test_a_non_git_directory_yields_no_commit(tmp_path):
    assert checkout_commit(tmp_path) == ""


@needs_tools
@pytest.mark.skipif(
    _TOOLS is None or not (_TOOLS.home / ".git").exists(),
    reason="the OrcMaps checkout here is not a git work tree",
)
def test_the_orcmaps_checkout_supplies_a_full_commit():
    commit = checkout_commit(_TOOLS.home)

    assert len(commit) == 40
    assert all(character in "0123456789abcdef" for character in commit)


def test_a_request_without_a_card_has_nowhere_to_stage(tmp_path):
    assert _request(tmp_path, card_root=None).pack_dir is None


def test_a_request_stages_into_the_cards_pack_directory(tmp_path):
    card = tmp_path / "CARD"

    assert _request(tmp_path, card_root=card).pack_dir == device_pack_dir(card)


def test_a_build_without_a_readable_commit_is_refused(tmp_path):
    """Refusing beats recording a pack as built by an unknown revision."""
    home, script = _fake_checkout(tmp_path, "raise SystemExit('must not run')\n")

    with pytest.raises(OrcMapsError, match="Refusing to write a false provenance record"):
        provision_pin_pack(_tools(home, script), _request(tmp_path))


# ── Pack names ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["pin-25km", "A", "2026.09.1", "a.b-c.9"])
def test_orcmaps_style_pack_names_are_accepted(name):
    assert valid_pack_name(name)


@pytest.mark.parametrize("name", ["", "pin pack", "pin/pack", "pin_pack", "café", "pack\n"])
def test_names_orcmaps_would_reject_are_rejected(name):
    assert not valid_pack_name(name)


def test_a_bad_pack_name_stops_before_the_tool_runs(tmp_path, monkeypatch):
    home, script = _fake_checkout(tmp_path)
    monkeypatch.setattr(orcmaps, "checkout_commit", lambda _home: "b" * 40)

    with pytest.raises(OrcMapsError, match="not a usable pack name"):
        provision_pin_pack(_tools(home, script), _request(tmp_path, name="pin pack"))


# ── The tool is invoked with the pin, the name, and the commit ──────────────


def test_the_pin_radius_name_and_commit_reach_the_provisioner(tmp_path, monkeypatch):
    home, script = _fake_checkout(tmp_path, "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    monkeypatch.setattr(orcmaps, "checkout_commit", lambda _home: "b" * 40)
    card = tmp_path / "CARD"
    pmtiles = tmp_path / "pmtiles.exe"
    request = _request(
        tmp_path, lat=45.1234567, lon=-122.6543219, radius_km=3.0,
        name="pin-3km", display_name="Pin 3 km", card_root=card, dry_run=False,
    )

    ok, output = provision_pin_pack(_tools(home, script, pmtiles_cli=pmtiles), request)

    assert ok is True, output
    assert json.loads(output) == [
        "--source-manifest", str(tmp_path / "source.manifest.json"),
        "--lat", "45.123457",
        "--lon", "-122.654322",
        "--radius-km", "3",
        "--name", "pin-3km",
        "--sd-root", str(card),
        "--min-zoom", "1",
        "--max-zoom", "13",
        "--priority", "20",
        "--builder-commit", "b" * 40,
        "--display-name", "Pin 3 km",
        "--pmtiles-cli", str(pmtiles),
    ]


def test_a_preview_asks_the_provisioner_for_a_dry_run(tmp_path, monkeypatch):
    home, script = _fake_checkout(tmp_path, "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    monkeypatch.setattr(orcmaps, "checkout_commit", lambda _home: "b" * 40)

    ok, output = provision_pin_pack(_tools(home, script), _request(tmp_path))

    assert ok is True, output
    assert "--dry-run" in json.loads(output)


def test_a_successful_build_is_not_reported_as_a_failure(tmp_path, monkeypatch):
    """Exit status 0 is falsy, so the old code made every success read as a failure."""
    home, script = _fake_checkout(tmp_path, "print('staged 12 tiles')\n")
    monkeypatch.setattr(orcmaps, "checkout_commit", lambda _home: "b" * 40)

    ok, output = provision_pin_pack(_tools(home, script), _request(tmp_path))

    assert ok is True
    assert output == "staged 12 tiles"


def test_an_empty_pin_pair_is_reported_as_a_failed_build(tmp_path, monkeypatch):
    home, script = _fake_checkout(tmp_path, "print('no tiles in radius')\nraise SystemExit(2)\n")
    monkeypatch.setattr(orcmaps, "checkout_commit", lambda _home: "b" * 40)

    ok, output = provision_pin_pack(_tools(home, script), _request(tmp_path))

    assert ok is False
    assert output == "no tiles in radius"


def test_a_missing_provisioner_is_refused(tmp_path):
    with pytest.raises(OrcMapsError, match="provision_pack.py was not found"):
        provision_pin_pack(_tools(tmp_path, None), _request(tmp_path))


# ── Verification reports a result instead of raising ────────────────────────


def test_verifying_without_the_verifier_built_says_how_to_build_it(tmp_path):
    with pytest.raises(OrcMapsError, match="orcmap_pack_verify has not been built"):
        verify_directory(_tools(tmp_path, None, verify=None), tmp_path)


@needs_verify
def test_verifying_a_missing_directory_is_a_result_not_an_exception(tmp_path):
    ok, output = verify_directory(_TOOLS, tmp_path / "not-here")

    assert ok is False
    assert output  # the tool's own explanation goes to the log


@needs_verify
@needs_pack
def test_a_card_root_is_verified_through_its_pack_directory(tmp_path):
    """The regression that matters: a file dialog returns a card root."""
    _stage_card(tmp_path, _PACKS[0])

    ok, output = verify_directory(_TOOLS, tmp_path)

    assert ok is True, output


@needs_tools
def test_an_empty_card_is_reported_as_unusable(tmp_path):
    device_pack_dir(tmp_path).mkdir(parents=True)

    ok, _output = verify_directory(_TOOLS, tmp_path)

    assert ok is False


# ── The worker closes the loop: built is not the same as installed ──────────


def _worker_result(monkeypatch, request: PinPackRequest, provision, verify) -> tuple:
    monkeypatch.setattr(orcmaps_controller, "provision_pin_pack", provision)
    monkeypatch.setattr(orcmaps_controller, "verify_directory", verify)
    worker = _OrcMapsWorker()
    results: list[tuple] = []
    worker.completed.connect(lambda *args: results.append(args))
    worker.log.connect(lambda _line: None)

    worker.provision(_tools(Path("orcmaps"), Path("provision_pack.py")), request)

    assert len(results) == 1, f"expected exactly one completion, got {results}"
    return results[0]


def test_a_built_pack_is_confirmed_with_orcmaps_before_it_is_called_done(tmp_path, monkeypatch):
    request = _request(tmp_path, dry_run=False)

    operation, ok, detail = _worker_result(
        monkeypatch, request,
        provision=lambda *_a, **_k: (True, "staged"),
        verify=lambda *_a, **_k: (True, "RESULT: OK -- all 1 pack(s) would install."),
    )

    assert (operation, ok) == ("provision", True)
    assert str(request.pack_dir) in detail


def test_a_built_pack_orcmaps_rejects_is_reported_as_a_failure(tmp_path, monkeypatch):
    operation, ok, detail = _worker_result(
        monkeypatch, _request(tmp_path, dry_run=False),
        provision=lambda *_a, **_k: (True, "staged"),
        verify=lambda *_a, **_k: (False, "RESULT: NO USABLE PACKS"),
    )

    assert (operation, ok) == ("provision", False)
    assert "failed OrcMaps' own verification" in detail


def test_a_preview_is_not_verified_as_if_it_had_cut_something(tmp_path, monkeypatch):
    """A preview writes nothing, so verifying it would slander an empty card."""
    verified: list[tuple] = []

    operation, ok, detail = _worker_result(
        monkeypatch, _request(tmp_path, dry_run=True),
        provision=lambda *_a, **_k: (True, "plan only"),
        verify=lambda *args, **_k: verified.append(args) or (False, "NO USABLE PACKS"),
    )

    assert (operation, ok) == ("provision", True)
    assert "nothing was cut" in detail
    assert verified == []


def test_a_failed_build_surfaces_the_tools_last_line(tmp_path, monkeypatch):
    operation, ok, detail = _worker_result(
        monkeypatch, _request(tmp_path, dry_run=False),
        provision=lambda *_a, **_k: (False, "creating pack\npmtiles: executable not found"),
        verify=lambda *_a, **_k: (True, "unused"),
    )

    assert (operation, ok) == ("provision", False)
    assert detail == "Pack build failed: pmtiles: executable not found"


def test_cancelling_terminates_the_running_tool(tmp_path):
    """Shutdown must not wait on a stuck pmtiles binary."""
    worker = _OrcMapsWorker()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        worker._on_start(child)

        worker.cancel()

        # terminate() is asynchronous on Windows, so wait for the exit rather
        # than polling once; a surviving tool raises TimeoutExpired here.
        assert child.wait(timeout=10) != 0, "the external tool exited cleanly after cancel()"
        assert worker._cancelled is True
    finally:
        child.kill()


# ── Tool discovery: a wrong path silently degrades to "cannot build packs" ──


def test_the_provisioner_is_only_found_at_orcmaps_own_path(tmp_path):
    home = tmp_path / "orcmaps"
    (home / "tools").mkdir(parents=True)

    assert orcmaps._find_script(home, "provision_pack.py") is None

    script = home / "tools" / "pack-builder" / "provision_pack.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n", encoding="utf-8")

    assert orcmaps._find_script(home, "provision_pack.py") == script


def test_pmtiles_is_taken_from_the_newest_pinned_copy(tmp_path):
    home = tmp_path / "orcmaps"
    assert orcmaps._find_pmtiles_cli(home) is None

    for version in ("1.20.0", "1.21.0"):
        version_dir = home / "data" / "local" / "tools" / "go-pmtiles" / version
        version_dir.mkdir(parents=True)
        (version_dir / "pmtiles.exe").write_bytes(b"MZ")

    found = orcmaps._find_pmtiles_cli(home)

    assert found is not None
    assert found.parent.name == "1.21.0"


@needs_verify
def test_the_located_tools_can_verify_a_directory(tmp_path):
    ok, output = verify_directory(_TOOLS, tmp_path)

    assert ok is False, "an empty directory must not verify as usable"
    assert output


@needs_script
@needs_pack
def test_a_preview_of_a_real_pack_leaves_the_card_untouched(tmp_path):
    """End-to-end through OrcMaps' own provisioner, without cutting anything."""
    pack = _PACKS[0]
    if pack.bounds is None:
        pytest.skip("the installed pack records no bounds to centre a pin on")
    min_lon, min_lat, max_lon, max_lat = pack.bounds
    card = tmp_path / "CARD"
    request = _request(
        tmp_path,
        source_manifest=pack.manifest,
        lat=(min_lat + max_lat) / 2,
        lon=(min_lon + max_lon) / 2,
        radius_km=1.0,
        name="preview",
        card_root=card,
        dry_run=True,
    )

    ok, output = provision_pin_pack(_TOOLS, request)

    assert ok is True, output
    assert not device_pack_dir(card).exists()
