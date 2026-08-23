import pytest

import deltabot.strategy.execution as execution


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Zero out retry backoff so failure-path tests run instantly."""
    monkeypatch.setattr(execution, "RETRY_BACKOFF", (0.0, 0.0, 0.0))
