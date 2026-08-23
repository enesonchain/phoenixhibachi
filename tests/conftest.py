import pytest

import deltabot.strategy.execution as execution


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Shrink retry backoff and position-poll timings so failure-path tests
    run instantly."""
    monkeypatch.setattr(execution, "RETRY_BACKOFF", (0.0, 0.0, 0.0))
    monkeypatch.setattr(execution, "POSITION_POLL_S", 0.001)
    monkeypatch.setattr(execution, "AMBIGUOUS_TIMEOUT_S", 0.01)
    monkeypatch.setattr(execution, "UNCERTAIN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(execution, "FLATTEN_TIMEOUT_S", 0.05)
