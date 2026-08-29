"""ChatGPT 账号页"刷新 token"动作安卓协议分流 focused tests。

- ``account.extra.account_overview`` 顶层 / ``legacy_extra`` 中的安卓协议标记
  （android / android_app / android_protocol）都会把 refresh_token 动作
  路由到安卓协议分支。
- 安卓分支：从 ``extra.refresh_token`` / ``extra.registration_refresh_token``
  取 RT（前者优先），用 ``_resolve_action_proxy``（按账号 region、
  ``action_label="刷新安卓 token"``）解析项目代理，分支内调用
  ``platforms.chatgpt.protocol_android.refresh_android_oauth_tokens(rt, proxy_url=proxy)``，
  结果映射进 ``token_refresh_source="android_protocol"`` 的 data。
- 缺 RT / 协议异常返回明确错误；account_state 查询 best-effort 失败吞掉；
  Web 账号仍走 TokenRefreshManager 旧链路。
- 全程 monkeypatch（含 raising=False，兼容并行开发中的 protocol helper），
  不发真实网络。
"""

import pytest

import core.registry as registry_module
import platforms.chatgpt.plugin as plugin_module
import platforms.chatgpt.protocol_android as protocol_android_module
import platforms.chatgpt.switch as switch_module
import platforms.chatgpt.token_refresh as token_refresh_module
from core.base_platform import Account, RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform

ANDROID_TOKENS = {
    "access_token": "android-at",
    "refresh_token": "rotated-rt",
    "id_token": "android-idt",
    "expires_at": "2026-01-01T00:00:00Z",
    "expires_in": 3600,
    "token_type": "bearer",
    "scope": "openid email profile",
}


def _make_platform(monkeypatch, config_proxy=None):
    """构造 ChatGPTPlatform，但把能力查询从 DB 摘掉，避免测试碰真实库。"""
    monkeypatch.setattr(
        registry_module,
        "get_platform_capabilities",
        lambda name: {
            "supported_executors": ["protocol"],
            "supported_identity_modes": ["mailbox"],
            "supported_oauth_providers": [],
            "capabilities": ["refresh_token"],
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


def _patch_android_refresh(monkeypatch, *, tokens=None, error=None):
    """替换 Android OAuth 刷新请求并记录调用参数。"""
    calls = []

    def _fake_refresh(rt, proxy_url=None):
        calls.append({"rt": rt, "proxy_url": proxy_url})
        if error is not None:
            raise error
        return dict(tokens or ANDROID_TOKENS)

    monkeypatch.setattr(
        protocol_android_module,
        "refresh_android_oauth_tokens",
        _fake_refresh,
        raising=False,
    )
    return calls


def _patch_proxy_resolution(monkeypatch, resolved="http://pool-proxy:1"):
    """monkeypatch plugin._resolve_action_proxy，记录安卓分支的取参契约。"""
    calls = []

    def _fake_resolve(configured_proxy, *, region="", log_fn=None, action_label="操作"):
        calls.append(
            {
                "configured_proxy": configured_proxy,
                "region": region,
                "action_label": action_label,
            }
        )
        return resolved

    monkeypatch.setattr(plugin_module, "_resolve_action_proxy", _fake_resolve)
    return calls


def _patch_account_state(monkeypatch, payload=None, error=None):
    calls = []

    def _fake_state(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return dict(payload or {})

    monkeypatch.setattr(switch_module, "fetch_chatgpt_account_state", _fake_state)
    return calls


def test_overview_top_level_android_marker_uses_android_protocol(monkeypatch):
    platform = _make_platform(monkeypatch, config_proxy="http://config-proxy:7890")
    account = _make_account(
        {
            "account_overview": {"registration_protocol_variant": "android"},
            "refresh_token": "rt-primary",
            "registration_refresh_token": "rt-secondary",
        },
        region="US",
    )
    protocol_calls = _patch_android_refresh(monkeypatch)
    proxy_calls = _patch_proxy_resolution(monkeypatch, resolved="http://pool-proxy:1")
    state_calls = _patch_account_state(monkeypatch, {"plan_type": "plus"})

    result = platform._handle_refresh_token(account, {})

    assert result["ok"] is True
    data = result["data"]
    assert data["access_token"] == "android-at"
    assert data["refresh_token"] == "rotated-rt"
    assert data["id_token"] == "android-idt"
    assert data["expires_at"] == "2026-01-01T00:00:00Z"
    assert data["expires_in"] == 3600
    assert data["token_type"] == "bearer"
    assert data["scope"] == "openid email profile"
    assert data["token_refresh_source"] == "android_protocol"
    assert data["account_state"] == {"plan_type": "plus"}

    # RT 来源：extra.refresh_token 优先于 extra.registration_refresh_token
    assert protocol_calls == [{"rt": "rt-primary", "proxy_url": "http://pool-proxy:1"}]
    # 代理契约：按账号 region、action_label=刷新安卓 token
    assert proxy_calls == [
        {
            "configured_proxy": "http://config-proxy:7890",
            "region": "US",
            "action_label": "刷新安卓 token",
        }
    ]
    assert state_calls and state_calls[0]["access_token"] == "android-at"


@pytest.mark.parametrize("variant", ["android", "android_app", "android_protocol"])
def test_android_variant_values_route_to_android_protocol(monkeypatch, variant):
    platform = _make_platform(monkeypatch)
    account = _make_account(
        {
            "account_overview": {"chatgpt_protocol_variant": variant.upper()},
            "refresh_token": "rt",
        }
    )
    protocol_calls = _patch_android_refresh(monkeypatch)
    _patch_proxy_resolution(monkeypatch)
    _patch_account_state(monkeypatch, {})

    result = platform._handle_refresh_token(account, {})

    assert result["ok"] is True
    assert result["data"]["token_refresh_source"] == "android_protocol"
    assert protocol_calls and protocol_calls[0]["rt"] == "rt"


def test_legacy_extra_android_marker_uses_registration_refresh_token(monkeypatch):
    platform = _make_platform(monkeypatch)
    account = _make_account(
        {
            "account_overview": {
                "legacy_extra": {
                    "registration_mode": "protocol",
                    "registration_mode_label": "安卓协议",
                    "registration_protocol_variant": "android",
                }
            },
            "registration_refresh_token": "rt-registration",
        }
    )
    protocol_calls = _patch_android_refresh(monkeypatch)
    proxy_calls = _patch_proxy_resolution(monkeypatch, resolved=None)
    _patch_account_state(monkeypatch, {})

    result = platform._handle_refresh_token(account, {})

    assert result["ok"] is True
    assert result["data"]["token_refresh_source"] == "android_protocol"
    assert protocol_calls == [{"rt": "rt-registration", "proxy_url": None}]
    assert proxy_calls[0]["region"] == ""


def test_android_account_without_refresh_token_returns_clear_error(monkeypatch):
    platform = _make_platform(monkeypatch)
    account = _make_account(
        {"account_overview": {"registration_protocol_variant": "android"}}
    )
    protocol_calls = _patch_android_refresh(monkeypatch)
    proxy_calls = _patch_proxy_resolution(monkeypatch)

    result = platform._handle_refresh_token(account, {})

    assert result["ok"] is False
    assert "refresh_token" in result["error"]
    assert "安卓" in result["error"]
    # 缺 RT 直接失败，不应解析代理或触碰协议 helper
    assert protocol_calls == []
    assert proxy_calls == []


def test_android_refresh_failure_returns_android_error(monkeypatch):
    platform = _make_platform(monkeypatch)
    account = _make_account(
        {
            "account_overview": {"registration_protocol_variant": "android"},
            "refresh_token": "rt-bad",
        }
    )
    protocol_calls = _patch_android_refresh(
        monkeypatch, error=RuntimeError("ANDROID协议 HTTP 403")
    )
    _patch_proxy_resolution(monkeypatch, resolved="http://pool-proxy:2")
    state_calls = _patch_account_state(monkeypatch)

    result = platform._handle_refresh_token(account, {})

    assert result["ok"] is False
    assert "安卓 token 刷新失败" in result["error"]
    assert "403" in result["error"]
    assert protocol_calls[0]["rt"] == "rt-bad"
    # 协议失败后不做 account_state 查询
    assert state_calls == []


def test_android_account_state_query_failure_is_swallowed(monkeypatch):
    platform = _make_platform(monkeypatch)
    account = _make_account(
        {
            "account_overview": {"protocol_variant": "android_protocol"},
            "refresh_token": "rt",
        }
    )
    _patch_android_refresh(monkeypatch)
    _patch_proxy_resolution(monkeypatch)
    state_calls = _patch_account_state(
        monkeypatch, error=RuntimeError("state unavailable")
    )

    result = platform._handle_refresh_token(account, {})

    # account_state 查询失败吞掉，不影响刷新结果
    assert result["ok"] is True
    assert result["data"]["token_refresh_source"] == "android_protocol"
    assert "account_state" not in result["data"]
    assert len(state_calls) == 1


def test_web_account_still_uses_token_refresh_manager(monkeypatch):
    platform = _make_platform(monkeypatch, config_proxy="http://web-proxy:1")
    account = _make_account(
        {"refresh_token": "web-rt", "session_token": "web-session"}
    )
    manager_calls = []

    class _FakeResult:
        success = True
        access_token = "web-at"
        refresh_token = "web-rt-rotated"

    class _FakeManager:
        def __init__(self, proxy_url=None):
            manager_calls.append({"proxy_url": proxy_url, "target": None})

        def refresh_account(self, target):
            manager_calls[-1]["target"] = target
            return _FakeResult()

    monkeypatch.setattr(token_refresh_module, "TokenRefreshManager", _FakeManager)
    proxy_calls = _patch_proxy_resolution(monkeypatch)
    state_calls = _patch_account_state(monkeypatch, {"plan_type": "team"})
    protocol_calls = _patch_android_refresh(monkeypatch)

    result = platform._handle_refresh_token(account, {})

    assert result["ok"] is True
    assert result["data"]["access_token"] == "web-at"
    assert result["data"]["refresh_token"] == "web-rt-rotated"
    assert result["data"]["account_state"] == {"plan_type": "team"}
    # Web 账号不走安卓分支，也不走 _resolve_action_proxy
    assert protocol_calls == []
    assert proxy_calls == []
    assert manager_calls[0]["proxy_url"] == "http://web-proxy:1"
    target = manager_calls[0]["target"]
    assert target.refresh_token == "web-rt"
    assert target.email == "user@example.com"
    assert state_calls[0]["proxy"] == "http://web-proxy:1"
