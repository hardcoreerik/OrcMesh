"""OrcMesh – services: OrcMaps offline-map bridge (separate-process boundary).

OrcMaps (a separate AGPL-3.0 project) is never linked, vendored, or copied
into OrcMesh (GPL-3.0-only): its host tools are run as child processes and
spoken to through files, exit codes, and stdout, which keeps both licenses
intact. See ``docs/orcmaps-integration.md`` for the design and the measured
numbers behind the choices here.

The rules mirrored from OrcMaps are deliberate and must not be softened:

- A pack is a stem-paired triplet — ``<name>.pmtiles`` plus
  ``<name>.manifest.json`` (and an optional ``<name>.sha256``). A manifest
  never carries a path; the archive is found by replacing the manifest suffix.
- An archive *without* a manifest is invisible. Metadata is what makes a pack
  usable, so OrcMesh does not render from a bare ``.pmtiles``.
- ``required_attribution`` must be surfaced wherever the basemap is shown.
  OrcMesh never invents pack metadata.
- Installability is decided by running ``orcmap_pack_verify`` (the same
  ``DiscoverPacks()`` the firmware runs), never by re-implementing the rules.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from html import escape
from urllib.parse import urlsplit
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

#: OrcMaps refuses manifests larger than this (docs/PACK_MANIFEST_SCHEMA.md).
_MAX_MANIFEST_BYTES = 64 * 1024

#: A 256x256 tile measured 45 ms cold / 27 ms warm against an 80 MB pack on
#: the dev machine, so this only needs to catch a hung child process.
_RENDER_TIMEOUT_S = 60.0

#: OrcMaps builtin style ids (OrcMaps src/render/style.cpp), mapped from
#: OrcMesh's existing light/dark map toggle.
STYLE_DARK = "orcsdr-dark"
STYLE_LIGHT = "standard-light"


class OrcMapsError(RuntimeError):
    """The OrcMaps tools are missing, or a render/verify call failed."""


@dataclass(frozen=True)
class OrcMapsTools:
    """Locations of the OrcMaps host tooling (executables and pack builders)."""

    home: Path
    inspect: Path
    verify: Path | None = None
    #: tools/pack-builder/provision_pack.py — the pin-radius pack cutter.
    provision_script: Path | None = None
    #: The go-pmtiles binary the builders shell out to (OrcMaps ships one).
    pmtiles_cli: Path | None = None


@dataclass(frozen=True)
class OrcMapsPack:
    """One manifest+archive pair, as OrcMaps discovery would see it."""

    stem: str
    pmtiles: Path
    manifest: Path
    sha256: Path | None
    display_name: str
    region_name: str
    min_zoom: int
    max_zoom: int
    pack_class: str
    pack_version: str
    schema_version: str
    content_profile: str
    attribution: tuple[str, ...]
    attribution_links: tuple[str, ...]
    #: (min_lon, min_lat, max_lon, max_lat), or None when the manifest omits it.
    bounds: tuple[float, float, float, float] | None
    size_bytes: int | None
    output_sha256: str | None
    priority: int

    @property
    def attribution_text(self) -> str:
        """Single line for the map's attribution control (may be empty)."""
        return " · ".join(self.attribution)

    @property
    def zoom_label(self) -> str:
        return f"z{self.min_zoom}-{self.max_zoom}"

    def covers(self, z: int, x: int, y: int) -> bool:
        """True when this pack can render the given tile.

        Zoom range and bounds come from the manifest; the archive's own header
        is verified separately by ``orcmap_pack_verify``. Antimeridian-spanning
        bounds are not handled — no current pack needs it.
        """
        if not (self.min_zoom <= z <= self.max_zoom):
            return False
        if self.bounds is None:
            return True
        west, south, east, north = tile_bounds_deg(z, x, y)
        min_lon, min_lat, max_lon, max_lat = self.bounds
        return not (east < min_lon or west > max_lon or north < min_lat or south > max_lat)


def attribution_html(pack: OrcMapsPack) -> str:
    """Leaflet-ready attribution HTML for a pack's required attributions.

    OrcMaps' manifest schema says ``required_attribution`` is "the exact list
    of attribution strings the runtime should surface", so these are shown
    verbatim (attribution and links both, as ODbL packs require). A manifest is
    data even when its source is trusted, so text is HTML-escaped and only
    http(s) links become clickable — anything else is shown as plain text
    rather than handed to the web view as a link.
    """
    texts = [escape(text) for text in pack.attribution]
    links: list[str] = []
    for url in pack.attribution_links:
        split = urlsplit(url)
        if split.scheme not in ("http", "https") or not split.netloc:
            texts.append(escape(url))
            continue
        links.append(
            '<a href="{href}" target="_blank" rel="noopener">{label}</a>'.format(
                href=escape(url, quote=True),
                label=escape(split.netloc),
            )
        )
    text = " · ".join(texts)
    if not links:
        return text
    joined = ", ".join(links)
    return f"{text} ({joined})" if text else joined


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def _candidate_homes(explicit: Path | None) -> list[Path]:
    """Checkout roots to probe, most specific first, de-duplicated."""
    homes: list[Path] = []
    if explicit is not None:
        homes.append(Path(explicit))
    for var in ("ORCMESH_ORCMAPS_HOME", "ORCMAPS_HOME"):
        value = os.environ.get(var)
        if value:
            homes.append(Path(value))
    # Sibling checkout — this is how F:\Ai\OrcMesh and F:\Ai\OrcMaps sit in the
    # current multi-root workspace.
    homes.append(Path(__file__).resolve().parents[3].parent / "OrcMaps")
    homes.append(Path.home() / "OrcMaps")
    # The local-clone convention documented in OrcMaps PROJECT_TRUTH.md.
    homes.append(Path("F:/Ai/OrcMaps"))

    seen: set[Path] = set()
    unique: list[Path] = []
    for home in homes:
        if home not in seen:
            seen.add(home)
            unique.append(home)
    return unique


#: Tool name → build directory inside an OrcMaps checkout. Not derivable from
#: the executable name (orcmap_pack_inspect lives in build-pack-inspect) —
#: these are the paths OrcMaps' own docs and CMake invocations use.
_BUILD_DIRS: dict[str, str] = {
    "orcmap_pack_inspect": "build-pack-inspect",
    "orcmap_pack_verify": "build-pack-verify",
}


def _build_paths(home: Path, name: str) -> list[Path]:
    """The build locations OrcMaps' own docs use (MSVC Release first)."""
    build_dir = home / _BUILD_DIRS.get(name, f"build-{name.replace('_', '-')}")
    return [
        build_dir / "Release" / f"{name}.exe",
        build_dir / f"{name}.exe",
        build_dir / "Release" / name,
        build_dir / name,
    ]


def _find_exe(home: Path, name: str) -> Path | None:
    for path in _build_paths(home, name):
        if path.is_file():
            return path
    return None


def _find_script(home: Path, name: str) -> Path | None:
    """A host-only Python tool in the checkout's tools/ directory."""
    script = home / "tools" / "pack-builder" / name
    return script if script.is_file() else None


def _find_pmtiles_cli(home: Path) -> Path | None:
    """The go-pmtiles binary OrcMaps keeps in the checkout, if present.

    provision_pack.py downloads nothing — it needs `pmtiles` on PATH or passed
    explicitly, and OrcMaps pins a copy under data/local/tools.
    """
    root = home / "data" / "local" / "tools" / "go-pmtiles"
    if not root.is_dir():
        return None
    matches = sorted(root.glob("*/pmtiles.exe")) + sorted(root.glob("*/pmtiles"))
    return matches[-1] if matches else None


def find_tools(home: Path | None = None) -> OrcMapsTools | None:
    """Locate the OrcMaps host tools, or None when they aren't built.

    Returns None rather than raising: an OrcMesh install without OrcMaps must
    still run, just without an offline basemap. Callers surface the reason.
    """
    override = os.environ.get("ORCMESH_ORCMAP_INSPECT")
    if override:
        exe = Path(override)
        if exe.is_file():
            sidecar = exe.with_name("orcmap_pack_verify.exe")
            return OrcMapsTools(
                home=exe.parent,
                inspect=exe,
                verify=sidecar if sidecar.is_file() else None,
            )
        log.warning("ORCMESH_ORCMAP_INSPECT points at a missing file: %s", exe)

    for candidate in _candidate_homes(home):
        inspect = _find_exe(candidate, "orcmap_pack_inspect")
        if inspect is None:
            continue
        verify = _find_exe(candidate, "orcmap_pack_verify")
        log.info("OrcMaps tools found in %s", candidate)
        return OrcMapsTools(
            home=candidate,
            inspect=inspect,
            verify=verify,
            provision_script=_find_script(candidate, "provision_pack.py"),
            pmtiles_cli=_find_pmtiles_cli(candidate),
        )

    log.info(
        "OrcMaps host tools not found — the offline basemap is unavailable. "
        "Build them with: cmake -S <orcmaps>/tools/pack-inspect -B <orcmaps>/build-pack-inspect"
    )
    return None


# ---------------------------------------------------------------------------
# Slippy-map tile math (Web Mercator, the same projection OrcMaps uses)
# ---------------------------------------------------------------------------

def tile_center_deg(z: int, x: int, y: int) -> tuple[float, float]:
    """(latitude, longitude) at the centre of a z/x/y slippy-map tile."""
    n = 2 ** z
    lon = (x + 0.5) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
    return lat, lon


def tile_index_deg(lat: float, lon: float, z: int) -> tuple[int, int]:
    """Tile index containing a coordinate — the inverse of tile_center_deg."""
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1 - math.asinh(math.tan(lat_rad)) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def _tile_edge_lat(z: int, y: int) -> float:
    n = 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def tile_bounds_deg(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Tile extent as (west, south, east, north) in degrees."""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    return west, _tile_edge_lat(z, y + 1), east, _tile_edge_lat(z, y)


# ---------------------------------------------------------------------------
# Pack discovery
# ---------------------------------------------------------------------------

def load_pack(manifest_path: Path) -> OrcMapsPack | None:
    """Parse one manifest and pair it with its archive.

    Returns None (logging why) for anything OrcMaps' own discovery would
    refuse, so one bad pack cannot hide the others.
    """
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            log.warning("Refusing oversized manifest (>64 KiB): %s", manifest_path)
            return None
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Unreadable pack manifest %s: %s", manifest_path, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("Not a manifest document: %s", manifest_path)
        return None

    # The archive is located by swapping the suffix — a manifest never carries
    # a path, so a file on a card cannot point the engine at something else.
    stem = manifest_path.name[: -len(".manifest.json")]
    pmtiles = manifest_path.with_name(f"{stem}.pmtiles")
    if not pmtiles.is_file():
        log.info("Skipping %s: its archive %s is missing", manifest_path.name, pmtiles.name)
        return None
    sidecar = manifest_path.with_name(f"{stem}.sha256")

    bounds_raw = raw.get("bounds") or {}
    bounds: tuple[float, float, float, float] | None = None
    if isinstance(bounds_raw, dict):
        try:
            bounds = (
                float(bounds_raw["min_lon"]), float(bounds_raw["min_lat"]),
                float(bounds_raw["max_lon"]), float(bounds_raw["max_lat"]),
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Manifest %s has unusable bounds", manifest_path.name)
            bounds = None

    def _zoom(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    priority_raw = raw.get("priority")
    priority = int(priority_raw) if isinstance(priority_raw, int) else 0

    return OrcMapsPack(
        stem=stem,
        pmtiles=pmtiles,
        manifest=manifest_path,
        sha256=sidecar if sidecar.is_file() else None,
        display_name=str(raw.get("display_name") or stem),
        region_name=str(raw.get("region_name") or ""),
        min_zoom=_zoom("min_zoom", 0),
        max_zoom=_zoom("max_zoom", 0),
        pack_class=str(raw.get("pack_class") or "unknown"),
        pack_version=str(raw.get("pack_version") or ""),
        schema_version=str(raw.get("schema_version") or ""),
        content_profile=str(raw.get("content_profile") or ""),
        attribution=tuple(str(a) for a in (raw.get("required_attribution") or [])),
        attribution_links=tuple(str(a) for a in (raw.get("attribution_links") or [])),
        bounds=bounds,
        size_bytes=raw.get("size_bytes") if isinstance(raw.get("size_bytes"), int) else None,
        output_sha256=raw.get("output_sha256") or None,
        priority=priority,
    )


def discover_packs(directories: list[Path]) -> list[OrcMapsPack]:
    """List usable packs in ``directories`` (non-recursive, like OrcMaps).

    ``/orcmaps/`` is a flat directory on the device and OrcMaps lists it once
    without walking subdirectories; this keeps the same shape so what OrcMesh
    offers to render matches what a device would find.
    """
    packs: list[OrcMapsPack] = []
    for directory in directories:
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            log.info("Pack directory unavailable %s: %s", directory, exc)
            continue
        for entry in entries:
            if entry.is_file() and entry.name.endswith(".manifest.json"):
                pack = load_pack(entry)
                if pack is not None:
                    packs.append(pack)
    # Highest priority first, then the widest zoom coverage.
    packs.sort(key=lambda p: (-p.priority, -p.max_zoom, p.display_name.lower()))
    return packs


def default_pack_directories(tools: OrcMapsTools | None) -> list[Path]:
    """Where to look for packs: OrcMaps' own data dir, then the SD contract."""
    dirs: list[Path] = []
    override = os.environ.get("ORCMESH_PACK_DIR")
    if override:
        dirs.append(Path(override))
    if tools is not None:
        dirs.append(tools.home / "data" / "local")
    return dirs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _no_window() -> dict:
    """Subprocess kwargs that stop console tools from flashing a window.

    Every OrcMaps call is a console executable or a Python script, and OrcMesh
    ships windowed — without this the user sees a console window per tile.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, **_no_window())
    except subprocess.TimeoutExpired as exc:
        raise OrcMapsError(f"OrcMaps render timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise OrcMapsError(f"Could not run {Path(argv[0]).name}: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise OrcMapsError(detail or f"{Path(argv[0]).name} exited {proc.returncode}")
    return proc


def _parse_ppm_p6(data: bytes) -> tuple[int, int, bytes]:
    """Parse a binary P6 PPM into (width, height, RGB bytes)."""
    if not data.startswith(b"P6"):
        raise OrcMapsError("OrcMaps returned an image that is not a binary PPM")
    pos = 2
    values: list[int] = []
    # Header is magic, width, height, maxval — separated by whitespace, and
    # comments starting with '#' may appear anywhere between the numbers.
    while len(values) < 3:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            while pos < len(data) and data[pos:pos + 1] != b"\n":
                pos += 1
            continue
        start = pos
        while pos < len(data) and data[pos:pos + 1].isdigit():
            pos += 1
        if start == pos:
            raise OrcMapsError("Malformed PPM header from OrcMaps")
        values.append(int(data[start:pos]))
    pos += 1  # the single whitespace byte after maxval
    width, height, maxval = values
    if maxval != 255:
        raise OrcMapsError(f"Unsupported PPM maxval {maxval}")
    expected = width * height * 3
    pixels = data[pos:pos + expected]
    if width <= 0 or height <= 0 or len(pixels) != expected:
        raise OrcMapsError("Truncated PPM from OrcMaps")
    return width, height, pixels


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + tag + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _ppm_to_png(ppm: bytes) -> bytes:
    """Convert the CLI's P6 PPM into a PNG for the web view.

    Deliberately stdlib-only rather than routed through Qt: this runs on the
    tile server's worker threads with no GUI involvement, and a QImage round
    trip drags in the image-format plugin machinery for what is a 20-line
    conversion. Keeping it here also makes it testable with no Qt, no
    subprocess, and no OrcMaps install.
    """
    width, height, pixels = _parse_ppm_p6(ppm)
    stride = width * 3
    # PNG requires a filter-type byte in front of every scanline; 0 = none.
    raw = b"".join(
        b"\x00" + pixels[row * stride:(row + 1) * stride] for row in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )


def render_tile(
    tools: OrcMapsTools,
    pack: OrcMapsPack,
    z: int,
    x: int,
    y: int,
    *,
    size: int = 256,
    style: str | None = None,
    timeout: float = _RENDER_TIMEOUT_S,
) -> bytes:
    """Render one tile to PNG bytes through the OrcMaps host CLI.

    ``orcmap_pack_inspect preview`` takes a centre lat/lon — not a tile index —
    so the tile's centre is computed here and the CLI renders a frame of
    ``size``x``size`` around it, which is the tile.
    """
    if not pack.covers(z, x, y):
        raise OrcMapsError(f"{pack.display_name} does not cover z{z}/{x}/{y}")
    lat, lon = tile_center_deg(z, x, y)

    with tempfile.TemporaryDirectory(prefix="orcmesh-tile-") as tmp:
        out = Path(tmp) / "tile.ppm"
        argv = [
            str(tools.inspect), "preview", str(pack.pmtiles),
            "--lat", f"{lat:.7f}", "--lon", f"{lon:.7f}", "--zoom", str(z),
            "--width", str(size), "--height", str(size), "--out", str(out),
        ]
        if style:
            argv += ["--style", style]
        started = time.monotonic()
        _run(argv, timeout)
        try:
            ppm = out.read_bytes()
        except OSError as exc:
            raise OrcMapsError(f"OrcMaps produced no tile image: {exc}") from exc
    log.debug(
        "Rendered %s z%d/%d/%d in %.0f ms", pack.stem, z, x, y,
        (time.monotonic() - started) * 1000,
    )
    return _ppm_to_png(ppm)


# ---------------------------------------------------------------------------
# Pack management — verify a card, cut a pack around a pin
# ---------------------------------------------------------------------------

#: On a device, packs live in one flat ``orcmaps/`` directory on the card
#: (OrcMaps docs/SD_CARD_LAYOUT.md). ``provision_pack.py --sd-root`` takes the
#: CARD ROOT and creates this directory; ``orcmap_pack_verify`` is given the
#: PACK directory itself. Handing verify the card root reports "NO USABLE
#: PACKS" for a card that is perfectly fine — the mistake resolve_pack_directory
#: exists to prevent.
DEVICE_PACK_DIRNAME = "orcmaps"

#: OrcMaps' IDENTITY_PART rule — the name becomes a filename and a pack_id part.
_PACK_NAME_RE = re.compile(r"[A-Za-z0-9.-]+")


def valid_pack_name(name: str) -> bool:
    """True when OrcMaps will accept ``name`` for a pack it builds.

    Checked before the build rather than after it: OrcMaps rejects a bad name in
    its identity stage, so catching it here saves an operator watching a build
    fail for a reason that was knowable up front.
    """
    return bool(_PACK_NAME_RE.fullmatch(name))


def device_pack_dir(card_root: Path) -> Path:
    """The pack directory inside a card root (``<card>/orcmaps``)."""
    return Path(card_root) / DEVICE_PACK_DIRNAME


def resolve_pack_directory(path: Path) -> Path:
    """Accept a card root *or* a pack directory; return the pack directory.

    The verify tool only understands the latter, while a card root is what a
    user naturally picks in a file dialog.
    """
    candidate = Path(path)
    nested = candidate / DEVICE_PACK_DIRNAME
    return nested if nested.is_dir() else candidate


def checkout_commit(home: Path) -> str:
    """The OrcMaps commit to attribute a pack build to, or "" when unknown.

    ``provision_pack.py`` requires ``--builder-commit`` and records it as the
    pack's provenance, so it is read from the checkout rather than invented;
    an unreadable commit refuses the build instead of writing a false record.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(home), "rev-parse", "HEAD"],
            capture_output=True, timeout=15, **_no_window(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("Could not read the OrcMaps commit: %s", exc)
        return ""
    commit = proc.stdout.decode("ascii", "replace").strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return ""
    return commit


@dataclass(frozen=True)
class PinPackRequest:
    """A "cut a pack around this point" build request."""

    source_manifest: Path
    lat: float
    lon: float
    radius_km: float
    name: str
    display_name: str = ""
    #: Card root; the finished pack is staged into ``<card_root>/orcmaps/``.
    card_root: Path | None = None
    min_zoom: int = 1
    max_zoom: int = 13
    priority: int = 20
    dry_run: bool = False
    force: bool = False

    @property
    def pack_dir(self) -> Path | None:
        """Where the finished triplet lands, once a card root is known."""
        return device_pack_dir(self.card_root) if self.card_root else None


def verify_directory(
    tools: OrcMapsTools, directory: Path, timeout: float = 120.0,
) -> tuple[bool, str]:
    """Run OrcMaps' own verification over a pack directory.

    This is the same ``DiscoverPacks()`` the firmware runs, so a directory that
    passes here behaves the same on the device. Returns (ok, output); a failed
    check is a result to display, not an exception, because "no usable packs" is
    exactly what the user needs to see.
    """
    if tools.verify is None:
        raise OrcMapsError(
            "orcmap_pack_verify has not been built. Build it with:\n"
            "  cmake -S <orcmaps>\\tools\\pack-verify -B <orcmaps>\\build-pack-verify\n"
            "  cmake --build <orcmaps>\\build-pack-verify --config Release"
        )
    directory = resolve_pack_directory(directory)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(tools.verify), str(directory)],
            capture_output=True, timeout=timeout, **_no_window(),
        )
    except subprocess.TimeoutExpired as exc:
        raise OrcMapsError(f"Pack verification timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise OrcMapsError(f"Could not run orcmap_pack_verify: {exc}") from exc
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    log.info(
        "Verified %s: %s (%.0f ms)", directory,
        "ok" if proc.returncode == 0 else f"failed (exit {proc.returncode})",
        (time.monotonic() - started) * 1000,
    )
    return proc.returncode == 0, output


def _stream(
    argv: list[str],
    on_output,
    on_start,
    timeout: float | None,
) -> tuple[int, str]:
    """Run argv, streaming output lines as they arrive. Returns (code, output).

    Streamed rather than captured because a pack build can take minutes and the
    user needs to watch it; ``on_start`` hands the Popen object out so the
    caller can cancel a build that is already running.
    """
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **_no_window(),
        )
    except OSError as exc:
        raise OrcMapsError(f"Could not run {Path(argv[0]).name}: {exc}") from exc

    if on_start is not None:
        on_start(proc)
    lines: list[str] = []
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.strip():
                lines.append(line)
                if on_output is not None:
                    on_output(line)
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait(timeout=10)
        raise OrcMapsError(f"{Path(argv[0]).name} did not finish in time") from exc
    finally:
        proc.stdout.close()
    return code, "\n".join(lines)


def provision_pin_pack(
    tools: OrcMapsTools,
    request: PinPackRequest,
    *,
    on_output=None,
    on_start=None,
    timeout: float | None = None,
) -> tuple[bool, str]:
    """Cut a pack around a pin from an existing pack, via OrcMaps' provisioner.

    ``provision_pack.py`` is deliberately the tool used here: it derives every
    provenance field (source snapshot, schema, attribution, pack class) from the
    SOURCE pack's manifest instead of asking an operator to retype ~25 flags —
    retyping them is how a wrong source snapshot or a missing credit gets
    recorded. OrcMesh supplies only the pin, radius, name, and the OrcMaps commit
    doing the build, and OrcMaps' own tool clamps the box to the source coverage.

    Returns (ok, output); the tool prints a JSON plan then the staged files.
    """
    if tools.provision_script is None:
        raise OrcMapsError(
            "OrcMaps' provision_pack.py was not found in this checkout "
            "(expected tools/pack-builder/provision_pack.py)"
        )
    if request.card_root is None:
        raise OrcMapsError("A card root is required to stage a pack")
    if not valid_pack_name(request.name):
        raise OrcMapsError(
            f"{request.name!r} is not a usable pack name — OrcMaps accepts letters, "
            "digits, periods and hyphens only"
        )

    commit = checkout_commit(tools.home)
    if not commit:
        raise OrcMapsError(
            "Could not read the OrcMaps commit to record as this pack's builder "
            f"(is {tools.home} a git checkout?). Refusing to write a false "
            "provenance record."
        )

    argv = [
        sys.executable, str(tools.provision_script),
        "--source-manifest", str(request.source_manifest),
        "--lat", f"{request.lat:.6f}",
        "--lon", f"{request.lon:.6f}",
        "--radius-km", f"{request.radius_km:g}",
        "--name", request.name,
        "--sd-root", str(request.card_root),
        "--min-zoom", str(request.min_zoom),
        "--max-zoom", str(request.max_zoom),
        "--priority", str(request.priority),
        "--builder-commit", commit,
    ]
    if request.display_name:
        argv += ["--display-name", request.display_name]
    if tools.pmtiles_cli is not None:
        argv += ["--pmtiles-cli", str(tools.pmtiles_cli)]
    if request.dry_run:
        argv.append("--dry-run")
    if request.force:
        argv.append("--force")

    code, output = _stream(argv, on_output, on_start, timeout)
    # code == 0, not the raw code: an exit status of 0 is falsy in Python, so
    # returning it would make every successful build read as a failure.
    return code == 0, output


# ---------------------------------------------------------------------------
# Loopback tile server
# ---------------------------------------------------------------------------

class TileCache:
    """Bounded LRU of rendered tiles, keyed by everything that affects them."""

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = max(1, capacity)
        self._entries: OrderedDict[tuple, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: tuple, value: bytes) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._entries),
                "capacity": self._capacity,
                "hits": self._hits,
                "misses": self._misses,
            }


class TileServer:
    """Loopback-only HTTP tile source for an OrcMaps pack set.

    Binds 127.0.0.1 on an ephemeral port — nothing off-machine can reach it,
    and it serves composited PNG tiles, never pack bytes. Renders are run from
    the request threads (subprocess calls hold no interpreter state), so a slow
    tile cannot block the GUI thread that owns this object's lifetime.
    """

    def __init__(
        self,
        tools: OrcMapsTools,
        packs: list[OrcMapsPack],
        *,
        style: str | None = STYLE_DARK,
        size: int = 256,
        cache_capacity: int = 512,
    ) -> None:
        if not packs:
            raise OrcMapsError("No OrcMaps packs to serve")
        self.tools = tools
        self.packs = list(packs)
        self.style = style
        self.size = size
        self.cache = TileCache(cache_capacity)
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._base = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        """Start serving and return the base URL."""
        if self._http is not None:
            return self._base
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._http.daemon_threads = True
        self._thread = threading.Thread(
            target=self._http.serve_forever, name="orcmesh-tiles", daemon=True,
        )
        self._thread.start()
        # Bound host/port read back from the socket (str()/int() so the types
        # are explicit — the socket address tuple is not reliably typed).
        bound_host = str(self._http.server_address[0])
        bound_port = int(self._http.server_address[1])
        self._base = f"http://{bound_host}:{bound_port}"
        log.info(
            "OrcMaps tile server on %s serving %d pack(s)", self._base, len(self.packs),
        )
        return self._base

    def stop(self) -> None:
        http, thread = self._http, self._thread
        self._http, self._thread = None, None
        if http is not None:
            http.shutdown()
            http.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    @property
    def url_template(self) -> str:
        """Leaflet-ready URL template."""
        return f"{self._base}/tiles/{{z}}/{{x}}/{{y}}.png"

    @property
    def base_url(self) -> str:
        """Loopback base URL; empty until start() has been called."""
        return self._base

    def set_style(self, style: str | None) -> None:
        """Switch render style (theme toggle); cached tiles no longer apply."""
        if style != self.style:
            self.style = style
            self.cache.clear()

    # -- serving -----------------------------------------------------------

    def pack_for(self, z: int, x: int, y: int) -> OrcMapsPack | None:
        """First pack (already priority-ordered) that covers this tile."""
        for pack in self.packs:
            if pack.covers(z, x, y):
                return pack
        return None

    def render(self, z: int, x: int, y: int) -> bytes:
        """Cached tile bytes, rendering on a miss."""
        pack = self.pack_for(z, x, y)
        if pack is None:
            raise OrcMapsError(f"No pack covers z{z}/{x}/{y}")
        key = (pack.stem, z, x, y, self.size, self.style)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        png = render_tile(
            self.tools, pack, z, x, y, size=self.size, style=self.style,
        )
        self.cache.put(key, png)
        return png

    def status(self) -> dict:
        return {
            "base_url": self._base,
            "style": self.style,
            "size": self.size,
            "cache": self.cache.stats(),
            "packs": [
                {
                    "stem": p.stem,
                    "display_name": p.display_name,
                    "zoom": [p.min_zoom, p.max_zoom],
                    "pack_class": p.pack_class,
                    "attribution": list(p.attribution),
                }
                for p in self.packs
            ],
        }


def _make_handler(server: TileServer):
    class _TileHandler(BaseHTTPRequestHandler):
        server_version = "OrcMeshTiles"

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            # Parse the path only: Leaflet appends a cache-busting query
            # (`?v=n`) after a style change, and including it in the segment
            # split made "185.png?v=1" fail the .png check — every tile 404'd.
            path = urlsplit(self.path).path
            if path == "/status":
                self._send_json(server.status())
                return
            parts = path.strip("/").split("/")
            # /tiles/{z}/{x}/{y}.png
            if len(parts) != 4 or parts[0] != "tiles" or not parts[3].endswith(".png"):
                self.send_error(404, "Not found")
                return
            try:
                z = int(parts[1])
                x = int(parts[2])
                y = int(parts[3][: -len(".png")])
            except ValueError:
                self.send_error(400, "Bad tile coordinates")
                return
            if z < 0 or z > 31 or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
                self.send_error(404, "Tile out of range")
                return
            try:
                png = server.render(z, x, y)
            except OrcMapsError as exc:
                # 404 (not 500) for "no pack here" so Leaflet simply leaves the
                # tile blank; anything else is a real render failure.
                if "No pack covers" in str(exc):
                    self.send_error(404, "No pack covers this tile")
                else:
                    log.error("Tile render failed for z%d/%d/%d: %s", z, x, y, exc)
                    self.send_error(500, "Tile render failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(png)

        def _send_json(self, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.debug("tile server: %s", fmt % args)

    return _TileHandler
