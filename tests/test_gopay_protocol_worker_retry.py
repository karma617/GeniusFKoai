import pytest

from platforms.gopay._opai_loader import ensure_opai_on_path

ensure_opai_on_path()
from opai.core import gopay_protocol_worker as worker


def test_idempotent_pin_read_retries_transient_tls_failure(monkeypatch):
    calls = []
    sleeps = []

    def callback():
        calls.append("called")
        if len(calls) == 1:
            raise RuntimeError("[SSL: UNEXPECTED_EOF_WHILE_READING]")
        return 200, {"ok": True}, {}

    monkeypatch.setattr(worker.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = worker._call_with_transient_network_retry("pin_allowed", callback)

    assert result[0] == 200
    assert len(calls) == 2
    assert sleeps == [2]


def test_idempotent_pin_read_does_not_retry_business_error(monkeypatch):
    calls = []
    monkeypatch.setattr(
        worker.time,
        "sleep",
        lambda _seconds: pytest.fail("business errors must not be retried"),
    )

    def callback():
        calls.append("called")
        raise ValueError("PIN is not allowed")

    with pytest.raises(ValueError, match="not allowed"):
        worker._call_with_transient_network_retry("pin_allowed", callback)

    assert len(calls) == 1


def test_idempotent_pin_read_stops_after_retry_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)

    def callback():
        calls.append("called")
        raise TimeoutError("request timed out")

    with pytest.raises(TimeoutError):
        worker._call_with_transient_network_retry(
            "pin_profile_verify", callback, attempts=3
        )

    assert len(calls) == 3
