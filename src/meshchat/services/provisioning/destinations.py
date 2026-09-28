"""Where provisioned packs should live.

Two destinations matter and they are not the same thing:

* the **app's own map directory**, so the desktop basemap can use a pack
  straight away, and
* a **card root**, because on a device packs live in one flat ``<card>/orcmaps/``
  directory (OrcMaps ``docs/SD_CARD_LAYOUT.md``).

Kept Qt-free so the wizard can be tested without a display, and so the paths can
be reasoned about in tests.
"""
from __future__ import annotations

from pathlib import Path

import platformdirs

#: Same legacy app name the store and logs use, so upgrades keep their data.
APP_NAME = "MeshChat"

#: Subdirectory of the app's data dir that holds provisioned packs.
MAPS_DIRNAME = "maps"


def default_map_dir() -> Path:
    """The app's own pack directory (created by the caller when writing)."""
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False)) / MAPS_DIRNAME


def is_pack_directory(path: Path) -> bool:
    """True when this looks like somewhere packs already live.

    Used to decide whether a chosen folder is a card root (which needs an
    ``orcmaps/`` subdirectory created for it) or a ready pack directory.
    """
    try:
        if not path.is_dir():
            return False
        return any(entry.name.endswith(".manifest.json") for entry in path.iterdir())
    except OSError:
        return False


def describe_destination(path: Path) -> str:
    """One line explaining what a chosen destination will receive."""
    if is_pack_directory(path):
        return f"Packs will be written into {path} (a pack directory)."
    return f"Packs will be written into {path}\\orcmaps (a card root)."
