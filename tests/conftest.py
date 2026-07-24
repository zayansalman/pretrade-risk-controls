import pytest

from pretrade_gate import InMemoryStateStore


@pytest.fixture
def store() -> InMemoryStateStore:
    """A fresh in-memory store per test.

    Tests that pair a control function with a gate MUST pass this same
    instance to both — that shared store IS the write-side/read-side seam.
    """
    return InMemoryStateStore()
