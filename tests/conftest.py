"""Shared pytest fixtures for MeshChat tests."""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
# Ensure src and project root (for `tests.*` imports) are on the path
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

import pytest


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
