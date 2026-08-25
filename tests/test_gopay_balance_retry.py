from platforms.gopay import plugin


class _SequenceBalanceClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get_balance(self):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _balance_response(value):
    return {
        "status": 200,
        "body": {"data": [{"balance": {"value": value}}]},
    }


def test_balance_query_retries_transient_disconnect(monkeypatch):
    client = _SequenceBalanceClient(
        [RuntimeError("Server disconnected without sending a response."), _balance_response(25000)]
    )
    sleeps = []
    monkeypatch.setattr(plugin.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = plugin.GoPayPlatform._query_balance_info(client)

    assert result["balance_query_status"] == "ok"
    assert result["balance_rp"] == 25000
    assert result["balance_query_attempts"] == 2
    assert client.calls == 2
    assert sleeps == [1]


def test_balance_query_does_not_retry_business_failure(monkeypatch):
    client = _SequenceBalanceClient(
        [{"status": 401, "body": {"message": "session revoked"}}]
    )
    monkeypatch.setattr(
        plugin.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("must not retry")),
    )

    result = plugin.GoPayPlatform._query_balance_info(client)

    assert result["balance_query_status"] == "error"
    assert result["balance_query_attempts"] == 1
    assert "HTTP 401" in result["balance_check_error"]
    assert client.calls == 1


def test_balance_query_stops_at_retry_limit(monkeypatch):
    client = _SequenceBalanceClient(
        [TimeoutError("request timed out"), TimeoutError("request timed out"), TimeoutError("request timed out")]
    )
    sleeps = []
    monkeypatch.setattr(plugin.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = plugin.GoPayPlatform._query_balance_info(client)

    assert result["balance_query_status"] == "error"
    assert result["balance_query_attempts"] == 3
    assert client.calls == 3
    assert sleeps == [1, 2]
