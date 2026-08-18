from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.base_mailbox import MailboxAccount
from platforms.chatgpt import protocol_android
from platforms.chatgpt.plugin import ChatGPTPlatform


class _Response:
    def __init__(self, payload, *, url: str, status_code: int = 200):
        self.status_code = status_code
        self.url = url
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    instances = []

    def __init__(self):
        self.trust_env = True
        self.proxies = {}
        self.get_calls = []
        self.post_calls = []
        self._first_party_responses = iter(
            [
                {"type": "email_otp_verification"},
                {"type": "about_you"},
                {"type": "token_exchange", "payload": {"code": "ac_android_code"}},
            ]
        )
        self.__class__.instances.append(self)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _Response({"page": {"type": "create_account_password"}}, url="https://auth.openai.com/create-account/password")

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if url == protocol_android.FIRST_PARTY_AUTHORIZE_URL:
            return _Response(next(self._first_party_responses), url=url)
        if url == protocol_android.TOKEN_URL:
            return _Response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "id_token": "id-token",
                    "expires_in": 864000,
                },
                url=url,
            )
        raise AssertionError(f"unexpected POST: {url}")


class _Mailbox:
    def get_current_ids(self, account):
        assert account.email == "user@example.com"
        return {"mail-before"}

    def wait_for_code(self, account, **kwargs):
        assert account.email == "user@example.com"
        assert kwargs["before_ids"] == {"mail-before"}
        assert kwargs["code_pattern"]
        return "123456"


class _ExistingAuthorizeSession(_Session):
    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _Response({"page": {"type": "email_otp_verification"}}, url="https://auth.openai.com/email-verification")


class _MarkingMailbox(_Mailbox):
    def __init__(self):
        self.marked = []

    def mark_registration_success(self, account):
        self.marked.append(account.email)
        return ["已注册"]


def test_android_worker_mirrors_app_flow(monkeypatch):
    _Session.instances.clear()
    monkeypatch.setattr(protocol_android.requests, "Session", _Session)
    monkeypatch.setattr(protocol_android, "_extract_chatgpt_account_id", lambda _token: "account-1")
    monkeypatch.setattr(
        protocol_android,
        "generate_random_user_info",
        lambda: {"name": "Mark Example", "birthdate": "1995-06-15"},
    )

    worker = protocol_android.ChatGPTAndroidProtocolWorker(
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="user@example.com", account_id="mail-1"),
        proxy_url="http://127.0.0.1:7897",
        log_fn=lambda _message: None,
    )
    result = worker.run(email="user@example.com", password="Secret123!")

    assert result.success is True
    assert result.account_id == "account-1"
    assert result.access_token == "access-token"
    assert result.refresh_token == "refresh-token"
    assert result.metadata["protocol_variant"] == "android"
    assert result.metadata["redirect_uri"] == protocol_android.REDIRECT_URI
    assert result.metadata["expires_in"] == 864000
    expires_at = datetime.fromisoformat(result.metadata["expires_at"].replace("Z", "+00:00"))
    remaining_seconds = (expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 863995 <= remaining_seconds <= 864000

    session = _Session.instances[0]
    assert session.trust_env is False
    assert session.proxies == {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
    assert session.get_calls[0][1]["headers"]["OAI-Package-Name"] == "com.openai.chatgpt"
    assert session.get_calls[0][1]["headers"]["OAI-Client-Type"] == "android"

    first_party_calls = [call for call in session.post_calls if call[0] == protocol_android.FIRST_PARTY_AUTHORIZE_URL]
    assert first_party_calls[0][1]["json"] == {
        "origin_page_type": "create_account_password",
        "data": {"intent": "passwordless_signup_send_otp"},
    }
    assert first_party_calls[1][1]["json"] == {
        "origin_page_type": "email_otp_verification",
        "data": {"intent": "validate", "code": "123456"},
    }
    assert first_party_calls[2][1]["json"]["origin_page_type"] == "about_you"
    assert first_party_calls[2][1]["json"]["data"]["name"] == "Mark Example"
    assert first_party_calls[2][1]["json"]["data"]["birthday"] == "1995-06-15"
    assert first_party_calls[0][1]["headers"]["X-OpenAI-Target-Path"] == "/api/first_party_authorize/next"

    token_call = [call for call in session.post_calls if call[0] == protocol_android.TOKEN_URL][0]
    assert token_call[1]["json"]["client_id"] == protocol_android.CLIENT_ID
    assert token_call[1]["json"]["redirect_uri"] == protocol_android.REDIRECT_URI
    assert token_call[1]["json"]["code"] == "ac_android_code"
    assert token_call[1]["json"]["code_verifier"] == worker.verifier


def test_protocol_variant_builds_android_worker():
    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = _Mailbox()
    platform.config = SimpleNamespace(extra={"mail_provider": "gmail_api_code"})
    adapter = platform.build_protocol_mailbox_adapter()
    logs = []
    ctx = SimpleNamespace(
        identity=SimpleNamespace(
            identity_provider="mailbox",
            mailbox_account=MailboxAccount(email="user@example.com", account_id="mail-1"),
        ),
        extra={"chatgpt_protocol_variant": "android"},
        proxy=None,
        log=logs.append,
    )

    worker = adapter.worker_builder(ctx, SimpleNamespace())

    assert isinstance(worker, protocol_android.ChatGPTAndroidProtocolWorker)
    assert any("ANDROID" in message for message in logs)


def test_android_error_keeps_empty_body_and_trace_headers():
    response = _Response({}, url=protocol_android.FIRST_PARTY_AUTHORIZE_URL, status_code=400)
    response.text = ""
    response.headers = {"x-request-id": "req-1", "content-type": "application/json"}

    with pytest.raises(RuntimeError, match=r"HTTP 400: <empty>.*req-1"):
        protocol_android._safe_json(response, "提交账号资料")


def test_android_existing_email_page_skips_signup_and_marks_email(monkeypatch):
    monkeypatch.setattr(protocol_android.requests, "Session", _ExistingAuthorizeSession)
    mailbox = _MarkingMailbox()
    worker = protocol_android.ChatGPTAndroidProtocolWorker(
        mailbox=mailbox,
        mailbox_account=MailboxAccount(email="user@example.com", account_id="mail-1"),
        log_fn=lambda _message: None,
    )

    with pytest.raises(RuntimeError, match="已被识别为已有账号"):
        worker.run(email="user@example.com", password="Secret123!")

    assert mailbox.marked == ["user@example.com"]
    assert worker.session.post_calls == []
