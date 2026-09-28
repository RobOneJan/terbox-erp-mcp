from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _terbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy backend credentials so TerboxSettings.from_env() never raises
    just because a test happens to exercise a code path that constructs a
    TerboxClient - no test in this suite makes a real network call, since
    each one either avoids the client entirely or monkeypatches it."""
    monkeypatch.setenv("TERBOX_API_BASE_URL", os.environ.get("TERBOX_API_BASE_URL", "https://api.example.com"))
    monkeypatch.setenv("TERBOX_API_KEY", os.environ.get("TERBOX_API_KEY", "dummy-test-key"))


@pytest.fixture(autouse=True)
def _reset_approval_store():
    """The approval store is a module-level dict (see approvals.py's module
    docstring - this server assumes a single process/instance, same as the
    rest of the codebase). Clear it around every test so tests never see
    approvals created by a previous test."""
    from terbox_mcp import approvals

    approvals._store.clear()
    yield
    approvals._store.clear()
