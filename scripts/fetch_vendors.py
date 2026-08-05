"""
Download Leaflet + MarkerCluster assets for offline / packaged use.

Run manually:  python scripts/fetch_vendors.py
Called by:     scripts/build.ps1  (auto, when vendor/ is missing)
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).parent.parent / "src" / "meshchat" / "ui" / "map" / "web" / "vendor"

LEAFLET_VERSION = "1.9.4"
MARKERCLUSTER_VERSION = "1.5.3"

ASSETS: list[tuple[str, str, str]] = [
    # (url, dest_relative_to_VENDOR, sha256_prefix_8)
    (
        f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js",
        f"leaflet/leaflet.js",
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
        req = urllib.request.Request(url, headers={"User-Agent": "MeshChat-build/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        dest.write_bytes(data)
        print(f"ok ({len(data)//1024} KB)")
    except Exception as exc:
        print(f"FAILED: {exc}")
        raise


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

    if errors:
        print(f"\n{errors} download(s) failed.  Check your internet connection.")
        return 1

    print(f"\nAll assets saved to {VENDOR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
