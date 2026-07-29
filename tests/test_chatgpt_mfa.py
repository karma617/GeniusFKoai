from __future__ import annotations

import base64

from application import accounts as accounts_module
from application import tasks as tasks_module
from core.account_display import build_account_display_summary
from domain.accounts import AccountRecord
from infrastructure.accounts_repository import _matches_tag_filter
from platforms.chatgpt import mfa


def test_generate_totp_code_matches_rfc_vector():
    secret = base64.b32encode(b"12345678901234567890").decode("ascii")

    assert mfa.generate_totp_code(secret, at_time=59, digits=8) == "94287082"


def test_enable_totp_mfa_protocol_flow(monkeypatch):
    secret = base64.b32encode(b"12345678901234567890").decode("ascii")
    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self):
            return self._payload

    class Session:
        headers = {}

        def post(self, url, headers=None, json=None, timeout=None):
            calls.append(("POST", url, headers or {}, json or {}, timeout))
            if url == mfa.MFA_ENROLL_URL:
                return Response(
                    200,
                    {
                        "secret": secret,
                        "session_id": "session-123",
                        "factor": {"id": "factor-123", "factor_type": "totp"},
                    },
                )
            assert json["code"] == mfa.generate_totp_code(secret, at_time=59)
            return Response(200, {"success": True})

        def get(self, url, headers=None, timeout=None):
            calls.append(("GET", url, headers or {}, {}, timeout))
            return Response(
                200,
                {
                    "mfa_enabled": True,
                    "mfa_enabled_v2": True,
                    "native_default_factor_id": "factor-123",
                    "factors": {"totp": [{"id": "factor-123", "factor_type": "totp"}]},
                },
            )

    monkeypatch.setattr(mfa, "build_protocol_session", lambda **_kwargs: Session())
    monkeypatch.setattr(mfa.time, "time", lambda: 59)

    result = mfa.enable_totp_mfa(
        cookies="oai-did=device-123; __Secure-next-auth.session-token=st",
        access_token="access-token-123",
    )

    assert result["ok"] is True
    assert result["totp_secret"] == secret
    assert result["mfa_factor_id"] == "factor-123"
    assert [item[0] for item in calls] == ["POST", "POST", "GET"]
    assert all(item[2].get("authorization") == "Bearer access-token-123" for item in calls)


def test_auto_enable_chatgpt_2fa_after_register_is_temporarily_disabled(monkeypatch):
    class Account:
        extra = {
            "access_token": "access-token-123",
            "cookies": "__Secure-next-auth.session-token=st",
            "account_overview": {},
        }

    class Logger:
        def __init__(self):
            self.messages = []

        def log(self, message, level="info"):
            self.messages.append((level, message))

    def fake_enable(**kwargs):
        raise AssertionError("auto 2FA should be disabled")

    monkeypatch.setattr("platforms.chatgpt.mfa.enable_totp_mfa", fake_enable)
    account = Account()
    logger = Logger()

    tasks_module._auto_enable_chatgpt_2fa_after_register(account, logger)

    assert "totp_secret" not in account.extra
    assert "mfa_enabled" not in account.extra
    assert any("注册后自动设置已临时关闭" in message for _level, message in logger.messages)


def test_chatgpt_mfa_badge_and_tag_filter():
    summary = build_account_display_summary(
        platform="chatgpt",
        email="user@example.com",
        lifecycle_status="registered",
        validity_status="unknown",
        plan_state="unknown",
        plan_name="",
        display_status="registered",
        overview={"mfa_enabled": True},
    )
    labels = [item["label"] for item in summary["badges"]]
    record = AccountRecord(
        id=1,
        platform="chatgpt",
        email="user@example.com",
        password="pw",
        overview={"mfa_enabled": True},
        display_summary=summary,
    )

    assert "2FA已绑" in labels
    assert _matches_tag_filter(record, "2FA已绑")


def test_accounts_service_generates_totp_code(monkeypatch):
    secret = base64.b32encode(b"12345678901234567890").decode("ascii")

    class Repository:
        def get(self, account_id):
            assert account_id == 12
            return AccountRecord(
                id=12,
                platform="chatgpt",
                email="user@example.com",
                password="pw",
                credentials=[
                    {
                        "scope": "platform",
                        "key": "totp_secret",
                        "value": secret,
                    }
                ],
            )

    monkeypatch.setattr(accounts_module.time, "time", lambda: 59)

    result = accounts_module.AccountsService(repository=Repository()).get_totp_code(12)

    assert result["code"] == "287082"
    assert result["valid_for_seconds"] == 1
