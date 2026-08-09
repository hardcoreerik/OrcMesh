"""OrcMesh version.

Single-sourced from the installed package metadata so this cannot drift from
pyproject.toml. The literal is only a fallback for running straight from a
source tree that was never installed.

Both spellings are exported: `__version__` is the Python convention, `VERSION`
is what several call sites import. Keeping both prevents the ImportError that
previously took out Help > About, Help > Copy Diagnostic Summary, and session
JSON export — all lazy imports, so they only failed when the user clicked them.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

_FALLBACK = "0.2.0"

try:
    __version__ = _pkg_version("orcmesh")
except PackageNotFoundError:  # running from an uninstalled source checkout
    __version__ = _FALLBACK

#: Alias — several modules import this spelling.
VERSION = __version__

__all__ = ["VERSION", "__version__"]
