from __future__ import annotations

from core.base_platform import Account, AccountStatus, RegisterConfig
from core.db import engine
from platforms.chatgpt import plugin as chatgpt_plugin
from platforms.chatgpt import switch as chatgpt_switch
from sqlmodel import SQLModel


def _account() -> Account:
    return Account(
        platform="chatgpt",
        email="user@example.com",
        password="secret",
        status=AccountStatus.REGISTERED,
        token="access-token",
        extra={"access_token": "access-token", "session_token": "session-token"},
    )


def test_get_account_state_alias_uses_query_state(monkeypatch):
    seen = {}

    def fake_fetch(**kwargs):
        seen["kwargs"] = kwargs
        seen["proxy"] = kwargs.get("proxy")
        return {
            "plan": "free",
            "source": "test",
            "remote_user": {"email": "user@example.com"},
        }

    monkeypatch.setattr(
        chatgpt_plugin,
        "_resolve_action_proxy",
        lambda *args, **kwargs: "http://127.0.0.1:7897",
    )
    monkeypatch.setattr(
        "platforms.chatgpt.switch.fetch_chatgpt_account_state",
        fake_fetch,
    )

    SQLModel.metadata.create_all(engine)
    platform = chatgpt_plugin.ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action("get_account_state", _account(), {})

    assert result["ok"] is True
    assert result["data"]["plan"] == "free"
    assert seen["proxy"] == "http://127.0.0.1:7897"
    assert seen["kwargs"]["chatgpt_account_id"] == ""
    assert seen["kwargs"]["existing_extra"]["access_token"] == "access-token"


def test_fetch_account_state_reads_codex_usage_headers(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 429
        text = ""
        headers = {
            "x-codex-primary-used-percent": "80",
            "x-codex-primary-reset-after-seconds": "604800",
            "x-codex-primary-window-minutes": "10080",
            "x-codex-secondary-used-percent": "12",
            "x-codex-secondary-reset-after-seconds": "18000",
            "x-codex-secondary-window-minutes": "300",
        }

        def close(self):
            seen["closed"] = True

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        seen["proxy"] = kwargs["proxies"]["https"]
        return FakeResponse()

    monkeypatch.setattr(chatgpt_switch, "_fetch_profile", lambda access_token, proxy=None: (True, {"email": "user@example.com", "accounts": [{"account": {"account_id": "acc_123"}}]}))
    monkeypatch.setattr(chatgpt_switch.curl_requests, "post", fake_post)

    import platforms.chatgpt.payment as payment

    monkeypatch.setattr(payment, "check_subscription_status", lambda account, proxy=None: "free")

    state = chatgpt_switch.fetch_chatgpt_account_state(
        access_token="access-token",
        proxy="http://127.0.0.1:7897",
        existing_extra={},
    )

    assert state["valid"] is True
    assert state["account_id"] == "acc_123"
    assert seen["url"] == chatgpt_switch.CODEX_RESPONSES_URL
    assert seen["headers"]["Authorization"] == "Bearer access-token"
    assert seen["headers"]["chatgpt-account-id"] == "acc_123"
    assert seen["proxy"] == "http://127.0.0.1:7897"
    assert seen["closed"] is True
    assert state["codex_usage"]["source"] == "active"
    assert state["codex_usage"]["five_hour"]["utilization"] == 12.0
    assert state["codex_usage"]["seven_day"]["utilization"] == 80.0
    assert state["usage_breakdowns"][0]["display_name"] == "Codex 5h"
    assert state["usage_breakdowns"][0]["current_usage"] == "12%"
