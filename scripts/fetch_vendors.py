"""
Download Leaflet + MarkerCluster assets for offline / packaged use, and write
qwebchannel.js from Qt's own resource.

Run manually:  python scripts/fetch_vendors.py
Called by:     scripts/build.ps1  (auto, when any vendor asset is missing)
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
VENDOR = ROOT / "src" / "meshchat" / "ui" / "map" / "web" / "vendor"

LEAFLET_VERSION = "1.9.4"
MARKERCLUSTER_VERSION = "1.5.3"

ASSETS: list[tuple[str, str, str]] = [
    # (url, dest_relative_to_VENDOR, sha256_prefix_8)
    (
        f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js",
        "leaflet/leaflet.js",
        "",  # no checksum check — just a convenience download
    ),
    (
        f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.css",
        "leaflet/leaflet.css",
        "",
    ),
    (
        f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/images/marker-icon.png",
        "leaflet/images/marker-icon.png",
        "",
    ),
    (
        f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/images/marker-shadow.png",
        "leaflet/images/marker-shadow.png",
        "",
    ),
    (
        f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/images/marker-icon-2x.png",
        "leaflet/images/marker-icon-2x.png",
        "",
    ),
    (
        f"https://unpkg.com/leaflet.markercluster@{MARKERCLUSTER_VERSION}/dist/leaflet.markercluster.js",
        "markercluster/leaflet.markercluster.js",
        "",
    ),
    (
        f"https://unpkg.com/leaflet.markercluster@{MARKERCLUSTER_VERSION}/dist/MarkerCluster.css",
        "markercluster/MarkerCluster.css",
        "",
    ),
    (
        f"https://unpkg.com/leaflet.markercluster@{MARKERCLUSTER_VERSION}/dist/MarkerCluster.Default.css",
        "markercluster/MarkerCluster.Default.css",
        "",
    ),
]


def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {dest.name} ...", end=" ", flush=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OrcMesh-build/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        dest.write_bytes(data)
        print(f"ok ({len(data)//1024} KB)")
    except Exception as exc:
        print(f"FAILED: {exc}")
        raise


def write_qwebchannel_asset() -> bool:
    """Write vendor/qwebchannel.js from the Qt resource that ships with PySide6.

    index.html needs this file to build its Python↔JS bridge, and nothing used
    to produce it: this script only downloaded Leaflet/MarkerCluster, so a
    vendor dir could look complete while the map had no working bridge at all
    (no node pins, no pin clicks). Qt already ships the matching script, so
    taking it from there needs no download and can't drift from the installed
    Qt version.
    """
    dest = VENDOR / "qwebchannel.js"
    if dest.exists() and dest.stat().st_size > 0:
        print("  skipping qwebchannel.js (already present)")
        return True

    sys.path.insert(0, str(ROOT / "src"))
    try:
        from meshchat.ui.map.map_widget import ensure_qwebchannel_asset
    except Exception as exc:
        print(f"  FAILED: could not import the Qt asset helper: {exc}")
        return False

    if ensure_qwebchannel_asset(VENDOR.parent) is None:
        print("  FAILED: Qt resource :/qtwebchannel/qwebchannel.js is unavailable")
        return False
    print(f"  wrote qwebchannel.js from the Qt resource ({dest.stat().st_size // 1024} KB)")
    return True


def main() -> int:
    print(f"Fetching Leaflet {LEAFLET_VERSION} + MarkerCluster {MARKERCLUSTER_VERSION}...")
    errors = 0
    for url, rel, _sha in ASSETS:
        dest = VENDOR / rel
        if dest.exists():
            print(f"  skipping {dest.name} (already present)")
            continue
        try:
            fetch(url, dest)
        except Exception:
            errors += 1

    # Local, offline — deliberately not counted as a download.
    print("Preparing qwebchannel.js...")
    if not write_qwebchannel_asset():
        errors += 1

    if errors:
        print(f"\n{errors} asset(s) could not be prepared.  Check your internet connection.")
        return 1

    print(f"\nAll assets saved to {VENDOR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
