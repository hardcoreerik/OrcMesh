"""Tests for version exports and the lazily-imported symbols the UI depends on.

Background: `VERSION` was imported by three call sites but never defined, so
Help > About, Help > Copy Diagnostic Summary, and session JSON export all
raised ImportError the moment a user clicked them. Because they were lazy
imports inside functions, nothing failed at startup and no test caught it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import meshchat.version as version_mod

_SRC = Path(__file__).parent.parent / "src"


class TestVersionExports:
    def test_dunder_version_is_a_string(self):
        assert isinstance(version_mod.__version__, str)
        assert version_mod.__version__

    def test_uppercase_alias_exists(self):
        # Three modules import this spelling; losing it is a runtime crash.
        assert isinstance(version_mod.VERSION, str)

    def test_both_spellings_agree(self):
        assert version_mod.VERSION == version_mod.__version__

    def test_looks_like_a_version(self):
        assert re.match(r"^\d+\.\d+", version_mod.__version__), version_mod.__version__

    @pytest.mark.parametrize("name", ["VERSION", "__version__"])
    def test_importable_by_name(self, name):
        mod = __import__("meshchat.version", fromlist=[name])
        assert getattr(mod, name)


def _iter_from_imports(py_file: Path):
    """Yield (module, imported_name) for every `from X import Y` in a file,
    including the ones nested inside functions."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                yield node.module, alias.name


class TestLazyImportsResolve:
    """Lazy imports inside functions bypass startup checks entirely. Walk the
    AST and confirm every `from meshchat... import NAME` actually resolves."""

    def test_every_internal_from_import_resolves(self):
        failures: list[str] = []
        for py_file in _SRC.rglob("*.py"):
            for module, name in _iter_from_imports(py_file):
                if not module.startswith("meshchat"):
                    continue
                try:
                    mod = __import__(module, fromlist=[name])
                except Exception as exc:  # noqa: BLE001 - reported in bulk below
                    failures.append(f"{py_file.name}: import {module} failed: {exc}")
                    continue
                if not hasattr(mod, name):
                    rel = py_file.relative_to(_SRC)
                    failures.append(f"{rel}: `from {module} import {name}` — not defined")
        assert not failures, "Unresolvable imports:\n  " + "\n  ".join(failures)
