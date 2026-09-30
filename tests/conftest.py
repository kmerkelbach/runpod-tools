import pytest


@pytest.fixture
def no_env(monkeypatch):
    """Strip every RUNPOD_* variable so tests start from a clean environment."""
    import os

    for key in list(os.environ):
        if key.startswith("RUNPOD"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch
