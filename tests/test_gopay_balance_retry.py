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


# --------------------------------------------------------------------------
# 套餐元数据回归：GoPay 是钱包平台，不存在 ChatGPT Free 套餐。
# register() 的 account_overview 必须从第一次保存起就带 plan_name/plan_state，
# check_valid() 不得再产出 plan='free' 语义。
# --------------------------------------------------------------------------

def _balance_info(balance_rp):
    return {
        "balance_rp": balance_rp,
        "balance_query_status": "ok",
        "balance_check_error": "",
        "balance_query_attempts": 1,
    }


def _make_gopay_platform(monkeypatch):
    # get_platform_capabilities 会开 DB session，测试里替换掉避免触碰真实库。
    import core.registry as registry

    from core.base_platform import RegisterConfig

    monkeypatch.setattr(registry, "get_platform_capabilities", lambda _name: {})
    return plugin.GoPayPlatform(
        RegisterConfig(executor_type="protocol", extra={"gopay_pin": "123456"})
    )


def _patch_register_worker(monkeypatch):
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core import gopay_protocol_worker as worker

    monkeypatch.setattr(
        worker,
        "_register_one",
        lambda *_args, **_kwargs: {
            "phone": "+628111111111",
            "local": "628111111111",
            "aid": "654321",
            "country_code": "+62",
            "client": object(),
        },
    )


def _patch_rebind_worker(monkeypatch):
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core import gopay_protocol_worker as worker

    monkeypatch.setattr(
        worker,
        "_acquire_via_mature_rebind",
        lambda *_args, **_kwargs: {
            "phone": "+628222222222",
            "local": "628222222222",
            "aid": "765432",
            "pin": "123456",
            "client": object(),
        },
    )


def _patch_check_valid_worker(monkeypatch, balance_rp):
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core import gopay_protocol_worker as worker

    monkeypatch.setattr(
        worker,
        "_resume_account",
        lambda _phone, proxy="": {"client": object()},
    )
    monkeypatch.setattr(
        plugin.GoPayPlatform,
        "_query_balance_info",
        staticmethod(lambda _client, attempts=3: _balance_info(balance_rp)),
    )


def _assert_gopay_plan_fields(overview, expected_plan_state):
    assert overview["plan_name"] == "GoPay"
    assert overview["plan_state"] == expected_plan_state
    # 不出现 ChatGPT Free 套餐语义
    assert "free" not in {overview["plan"], overview["plan_name"], overview["plan_state"]}


def test_register_overview_writes_gopay_plan_metadata(monkeypatch):
    platform = _make_gopay_platform(monkeypatch)
    _patch_register_worker(monkeypatch)
    monkeypatch.setattr(platform, "_setup_sms_channel", lambda *_args, **_kwargs: "test-api-key")
    monkeypatch.setattr(
        plugin.GoPayPlatform,
        "_safe_initial_balance",
        staticmethod(lambda _client: _balance_info(12000)),
    )

    account = platform.register()

    overview = account.extra["account_overview"]
    _assert_gopay_plan_fields(overview, "active")
    assert overview["plan"] == "gopay"
    # 原有字段保持
    assert overview["balance_rp"] == 12000
    assert overview["balance_query_status"] == "ok"
    assert overview["phone"] == "+628111111111"
    assert overview["phone_local"] == "628111111111"
    assert overview["country_code"] == "+62"
    assert overview["pin_set"] is True
    assert overview["herosms_activation_id"] == "654321"
    assert overview["sms_provider"] == "herosms"
    assert overview["sms_acquired_at"]
    assert account.extra["balance_rp"] == 12000
    assert account.email == "+628111111111"
    assert account.password == "123456"
    assert account.status.value == "registered"


def test_register_zero_balance_marks_registered_plan_state(monkeypatch):
    platform = _make_gopay_platform(monkeypatch)
    _patch_register_worker(monkeypatch)
    monkeypatch.setattr(platform, "_setup_sms_channel", lambda *_args, **_kwargs: "test-api-key")
    monkeypatch.setattr(
        plugin.GoPayPlatform,
        "_safe_initial_balance",
        staticmethod(lambda _client: _balance_info(0)),
    )

    account = platform.register()

    overview = account.extra["account_overview"]
    _assert_gopay_plan_fields(overview, "registered")
    assert overview["plan"] == "gopay"
    assert overview["balance_rp"] == 0
    assert overview["pin_set"] is True


def test_rebind_overview_writes_gopay_plan_metadata(monkeypatch):
    platform = _make_gopay_platform(monkeypatch)
    _patch_rebind_worker(monkeypatch)
    monkeypatch.setattr(platform, "_setup_sms_channel", lambda *_args, **_kwargs: "test-api-key")
    monkeypatch.setattr(
        plugin.GoPayPlatform,
        "_safe_initial_balance",
        staticmethod(lambda _client: _balance_info(1)),
    )

    account = platform.acquire_via_rebind()

    overview = account.extra["account_overview"]
    _assert_gopay_plan_fields(overview, "active")
    assert overview["plan"] == "gopay"
    assert overview["balance_rp"] == 1
    assert overview["acquired_via"] == "mature_rebind"
    assert account.email == "+628222222222"


def test_check_valid_positive_balance_reports_active_wallet_plan(monkeypatch):
    platform = _make_gopay_platform(monkeypatch)
    _patch_check_valid_worker(monkeypatch, balance_rp=1)
    from core.base_platform import Account

    account = Account(
        platform="gopay",
        email="+628111111111",
        password="123456",
        user_id="+628111111111",
    )

    assert platform.check_valid(account) is True

    overview = platform.get_last_check_overview()
    _assert_gopay_plan_fields(overview, "active")
    assert overview["plan"] == "gopay"
    assert overview["balance_rp"] == 1
    assert overview["balance_query_status"] == "ok"


def test_check_valid_zero_balance_reports_registered_wallet_plan(monkeypatch):
    platform = _make_gopay_platform(monkeypatch)
    _patch_check_valid_worker(monkeypatch, balance_rp=0)
    from core.base_platform import Account

    account = Account(
        platform="gopay",
        email="+628111111111",
        password="123456",
        user_id="+628111111111",
    )

    assert platform.check_valid(account) is True

    overview = platform.get_last_check_overview()
    _assert_gopay_plan_fields(overview, "registered")
    assert overview["plan"] == "gopay"
    assert overview["balance_rp"] == 0
    assert overview["balance_query_status"] == "ok"
