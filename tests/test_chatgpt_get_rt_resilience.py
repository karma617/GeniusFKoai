"""ChatGPT 获取rt 地区解析与登录完整重启韧性 focused tests（无网络）。

- ``_account_action_region``：account.region → extra.region →
  account_overview.region → overview.registration_ip_country_code/country_code →
  legacy_extra 同名字段，统一规范为大写；get_rt / get_rt(绕过) 租项目代理时
  按该地区选择（region 空但 registration_ip_country_code=VN 时租 VN 代理）。
- ``_run_get_rt_protocol_with_restarts``：只对 GET_RT_LOGIN_RESTART_REQUIRED
  标记错误整体重启，其它错误立即抛出；每次重启先按 5s/10s/…（上限 20s）
  指数退避，再刷新邮箱 OTP baseline，日志说明保持同一 leased proxy URL。
- ``_handle_get_rt``：登录完整重启次数独立于 phone_change_limit，
  默认 3、上限 5，可由 params.login_restart_limit 覆盖。
- 全程 monkeypatch，不发真实网络、不开浏览器、不触碰手机接码次数/价格。
"""

import pytest

import core.registry as registry_module
import platforms.chatgpt.plugin as plugin_module
from core.base_platform import Account, RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform


def _make_platform(monkeypatch, config_proxy=None):
    """构造 ChatGPTPlatform，但把能力查询从 DB 摘掉，避免测试碰真实库。"""
    monkeypatch.setattr(
        registry_module,
        "get_platform_capabilities",
        lambda name: {
            "supported_executors": ["protocol"],
            "supported_identity_modes": ["mailbox"],
            "supported_oauth_providers": [],
            "capabilities": ["get_rt"],
        },
    )
    return ChatGPTPlatform(
        RegisterConfig(executor_type="protocol", proxy=config_proxy),
        mailbox=None,
    )


def _make_account(extra, region=""):
    return Account(
        platform="chatgpt",
        email="user@example.com",
        password="pw",
        region=region,
        extra=extra,
    )


class _OtpCallback:
    def __init__(self):
        self.refresh_calls = 0

    def refresh_before_ids(self):
        self.refresh_calls += 1
        return set()


# ── 地区解析 helper ────────────────────────────────────────────────


def test_region_account_region_wins_over_everything():
    account = _make_account(
        extra={
            "region": "jp",
            "account_overview": {
                "region": "kr",
                "registration_ip_country_code": "vn",
                "legacy_extra": {"registration_ip_country_code": "sg"},
            },
        },
        region="us",
    )
    assert plugin_module._account_action_region(account) == "US"


def test_region_falls_back_to_extra_region():
    account = _make_account(extra={"region": "jp"})
    assert plugin_module._account_action_region(account) == "JP"


def test_region_falls_back_to_overview_region():
    account = _make_account(extra={"account_overview": {"region": "kr"}})
    assert plugin_module._account_action_region(account) == "KR"


def test_region_falls_back_to_registration_ip_country_code():
    account = _make_account(
        extra={"account_overview": {"registration_ip_country_code": "vn"}}
    )
    assert plugin_module._account_action_region(account) == "VN"


def test_region_falls_back_to_overview_country_code():
    account = _make_account(extra={"account_overview": {"country_code": "sg"}})
    assert plugin_module._account_action_region(account) == "SG"


def test_region_falls_back_to_legacy_extra_registration_ip_country_code():
    account = _make_account(
        extra={
            "account_overview": {
                "legacy_extra": {"registration_ip_country_code": "vn"}
            }
        }
    )
    assert plugin_module._account_action_region(account) == "VN"


def test_region_falls_back_to_legacy_extra_country_code():
    account = _make_account(
        extra={"account_overview": {"legacy_extra": {"country_code": "sg"}}}
    )
    assert plugin_module._account_action_region(account) == "SG"


def test_region_empty_everywhere_returns_empty():
    assert plugin_module._account_action_region(_make_account({})) == ""


# ── 登录完整重启次数解析 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 3),
        ("", 3),
        ("abc", 3),
        (1, 1),
        (0, 1),
        (2, 2),
        ("4", 4),
        (99, 5),
    ],
)
def test_login_restart_limit_default_three_cap_five(raw, expected):
    assert (
        plugin_module._resolve_get_rt_login_restart_limit(
            {"login_restart_limit": raw}
        )
        == expected
    )


def test_login_restart_limit_missing_param_defaults_three():
    assert plugin_module._resolve_get_rt_login_restart_limit({}) == 3


# ── 重启 wrapper ──────────────────────────────────────────────────


def test_wrapper_retries_marker_error_with_backoff_until_third_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr(plugin_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    logs = []
    otp = _OtpCallback()
    attempts = []

    def run_once():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("GET_RT_LOGIN_RESTART_REQUIRED: 授权状态失效")
        return {"refresh_token": "rt"}

    result = plugin_module._run_get_rt_protocol_with_restarts(
        run_once,
        otp_callback=otp,
        log_fn=logs.append,
        max_attempts=3,
        proxy_url="http://user:pass@host:1",
    )

    assert result == {"refresh_token": "rt"}
    assert len(attempts) == 3
    # 每次重启前指数退避：5s / 10s
    assert sleeps == [5.0, 10.0]
    assert otp.refresh_calls == 2
    # 日志说明保持同一 leased proxy URL 完整重启（代理值脱敏展示）
    assert any(
        "同一 leased proxy" in message and "http://***@host:1" in message
        for message in logs
    )


def test_wrapper_waits_before_refreshing_mailbox_baseline(monkeypatch):
    events = []
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _seconds: events.append("sleep"))
    otp = _OtpCallback()
    otp.refresh_before_ids = lambda: events.append("baseline")
    attempts = {"count": 0}

    def run_once():
        attempts["count"] += 1
        events.append("run")
        if attempts["count"] == 1:
            raise RuntimeError("GET_RT_LOGIN_RESTART_REQUIRED: stale state")
        return {"refresh_token": "rt"}

    plugin_module._run_get_rt_protocol_with_restarts(
        run_once,
        otp_callback=otp,
        log_fn=lambda *_: None,
        max_attempts=2,
    )

    assert events == ["run", "sleep", "baseline", "run"]


def test_wrapper_does_not_retry_other_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr(plugin_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    otp = _OtpCallback()
    calls = []

    def run_once():
        calls.append(1)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        plugin_module._run_get_rt_protocol_with_restarts(
            run_once,
            otp_callback=otp,
            log_fn=lambda *_: None,
            max_attempts=3,
        )

    assert len(calls) == 1
    assert sleeps == []
    assert otp.refresh_calls == 0


def test_wrapper_backoff_capped_at_20s(monkeypatch):
    sleeps = []
    monkeypatch.setattr(plugin_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    state = {"n": 0}

    def run_once():
        state["n"] += 1
        if state["n"] < 5:
            raise RuntimeError("GET_RT_LOGIN_RESTART_REQUIRED")
        return {"ok": True}

    result = plugin_module._run_get_rt_protocol_with_restarts(
        run_once,
        otp_callback=_OtpCallback(),
        log_fn=lambda *_: None,
        max_attempts=5,
    )

    assert result == {"ok": True}
    assert sleeps == [5.0, 10.0, 20.0, 20.0]


def test_wrapper_exhausts_attempts_then_raises(monkeypatch):
    sleeps = []
    monkeypatch.setattr(plugin_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    calls = []

    def run_once():
        calls.append(1)
        raise RuntimeError("GET_RT_LOGIN_RESTART_REQUIRED")

    with pytest.raises(RuntimeError, match="GET_RT_LOGIN_RESTART_REQUIRED"):
        plugin_module._run_get_rt_protocol_with_restarts(
            run_once,
            otp_callback=_OtpCallback(),
            log_fn=lambda *_: None,
            max_attempts=3,
        )

    assert len(calls) == 3
    # 最后一次失败直接抛出原始错误，不再退避
    assert sleeps == [5.0, 10.0]


# ── get_rt / get_rt(绕过) 接线 ─────────────────────────────────────


class _StopTest(Exception):
    pass


def _patch_proxy_with_source(monkeypatch, resolved="http://leased:1", source="pool_lease"):
    calls = []

    def _fake(configured_proxy, *, region="", log_fn=None, action_label="操作", lease=False):
        calls.append(
            {
                "configured_proxy": configured_proxy,
                "region": region,
                "action_label": action_label,
                "lease": lease,
            }
        )
        return resolved, source

    monkeypatch.setattr(plugin_module, "_resolve_action_proxy_with_source", _fake)
    return calls


def _patch_login_restarts(monkeypatch):
    calls = []

    def _fake(run_once, *, otp_callback, log_fn, max_attempts, proxy_url=None):
        calls.append({"max_attempts": max_attempts, "proxy_url": proxy_url})
        return {}

    monkeypatch.setattr(plugin_module, "_run_get_rt_protocol_with_restarts", _fake)
    return calls


def _patch_mailbox_otp(platform):
    platform._build_get_rt_mailbox_otp_callback = lambda account, log_fn, proxy: (
        _OtpCallback(),
        "",
    )


def test_get_rt_leases_proxy_by_registration_ip_region(monkeypatch):
    platform = _make_platform(monkeypatch)
    proxy_calls = _patch_proxy_with_source(monkeypatch)
    wrapper_calls = _patch_login_restarts(monkeypatch)
    _patch_mailbox_otp(platform)
    account = _make_account(
        extra={"account_overview": {"registration_ip_country_code": "vn"}}
    )

    result = platform._handle_get_rt(account, {"executor_type": "protocol"})

    assert result["ok"] is True
    # region 为空但注册 IP 国别码=VN → 按 VN 租代理
    assert proxy_calls[0]["region"] == "VN"
    assert proxy_calls[0]["lease"] is True
    assert proxy_calls[0]["action_label"] == "获取rt"
    assert wrapper_calls[0]["proxy_url"] == "http://leased:1"


def test_get_rt_account_region_wins_over_registration_ip(monkeypatch):
    platform = _make_platform(monkeypatch)
    proxy_calls = _patch_proxy_with_source(monkeypatch)
    _patch_login_restarts(monkeypatch)
    _patch_mailbox_otp(platform)
    account = _make_account(
        extra={"account_overview": {"registration_ip_country_code": "vn"}},
        region="us",
    )

    result = platform._handle_get_rt(account, {"executor_type": "protocol"})

    assert result["ok"] is True
    assert proxy_calls[0]["region"] == "US"


@pytest.mark.parametrize(
    "params,expected_limit",
    [
        ({"executor_type": "protocol"}, 3),
        ({"executor_type": "protocol", "login_restart_limit": 2}, 2),
        ({"executor_type": "protocol", "login_restart_limit": 99}, 5),
        # phone_change_limit 不再决定登录完整重启次数
        ({"executor_type": "protocol", "phone_change_limit": 10}, 3),
    ],
)
def test_get_rt_passes_independent_login_restart_limit(
    monkeypatch, params, expected_limit
):
    platform = _make_platform(monkeypatch)
    _patch_proxy_with_source(monkeypatch)
    wrapper_calls = _patch_login_restarts(monkeypatch)
    _patch_mailbox_otp(platform)
    account = _make_account(extra={})

    result = platform._handle_get_rt(account, dict(params))

    assert result["ok"] is True
    assert wrapper_calls[0]["max_attempts"] == expected_limit


def test_get_rt_bypass_uses_registration_ip_region(monkeypatch):
    platform = _make_platform(monkeypatch)
    calls = []

    def _fake_resolve(configured_proxy, *, region="", log_fn=None, action_label="操作"):
        calls.append({"region": region, "action_label": action_label})
        raise _StopTest()

    monkeypatch.setattr(plugin_module, "_resolve_action_proxy", _fake_resolve)
    account = _make_account(
        extra={"account_overview": {"registration_ip_country_code": "vn"}}
    )

    # 代理解析后即中断，避免进入真实浏览器链路
    with pytest.raises(_StopTest):
        platform._handle_get_rt_bypass(account, {})

    assert calls[0]["region"] == "VN"
    assert calls[0]["action_label"] == "获取rt(绕过)"
