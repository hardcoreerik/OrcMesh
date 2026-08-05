"""Shared pytest fixtures for MeshChat tests."""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
# Ensure src and project root (for `tests.*` imports) are on the path
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

import pytest

# ── Single, process-wide Qt application ─────────────────────────────────────
# Several test files construct a QApplication/QCoreApplication of their own,
# guarded by `.instance() or ...`. Both classes share one process-wide
# singleton, so whichever test module happens to run first "wins" it — and
# since a plain QCoreApplication never initialises the GUI/platform backend,
# a later test that constructs a real QWidget on top of it crashes the
# interpreter natively (no Python traceback, just an abnormal exit code).
#
# conftest.py is guaranteed to be imported before any test module in this
# directory, regardless of collection order, so creating the *stronger* type
# here — QApplication, a superset of QCoreApplication — makes the singleton
# GUI-capable before any test file gets a chance to create a weaker one.
# Individual test files can keep their own `QCoreApplication.instance() or
# QCoreApplication(...)` guards; `.instance()` will return this one and their
# `or` branch will simply never execute.
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv[:1])


@pytest.fixture
def fake_interface():
    """Return a FakeMeshtasticInterface."""
    from tests.fakes.fake_meshtastic_interface import FakeMeshtasticInterface
    return FakeMeshtasticInterface()


@pytest.fixture
def sample_nodes_by_num():
    return {
        111111: {
            "user": {
                "id": "!0001b207",
                "longName": "Alice Node",
                "shortName": "ALIC",
            }
        },
        222222: {
            "user": {
                "id": "!00036b2e",
                "longName": "Bob Node",
                "shortName": "BOB",
            }
        },
    }
