from __future__ import annotations

import pytest

from core.base_mailbox import MailboxAccount
from platforms.chatgpt import protocol_android


class _Response:
    def __init__(self, payload, *, url: str, status_code: int = 200):
        self.status_code = status_code
        self.url = url
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _EmailVerificationSession:
    instances = []

    def __init__(self):
        self.trust_env = True
        self.proxies = {}
        self.post_calls = []
        self._first_party_responses = iter(
            [
                {"type": "about_you"},
                {"type": "token_exchange", "payload": {"code": "auth-code"}},
            ]
        )
        self.__class__.instances.append(self)

    def get(self, _url, **_kwargs):
        return _Response({}, url="https://auth.openai.com/email-verification")

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if url == protocol_android.FIRST_PARTY_AUTHORIZE_URL:
            return _Response(next(self._first_party_responses), url=url)
        if url == protocol_android.TOKEN_URL:
            return _Response(
                {"access_token": "access-token", "refresh_token": "refresh-token"},
                url=url,
            )
        raise AssertionError(f"unexpected POST: {url}")


class _LoginSession(_EmailVerificationSession):
    def get(self, _url, **_kwargs):
        return _Response({}, url="https://auth.openai.com/log-in")


class _Mailbox:
    def __init__(self):
        self.marked = []
        self.baseline_reads = 0

    def get_current_ids(self, _account):
        self.baseline_reads += 1
        return {"mail-before"}

    def wait_for_code(self, _account, **kwargs):
        assert kwargs["before_ids"] == {"mail-before"}
        return "123456"

    def mark_registration_success(self, account):
        self.marked.append(account.email)
        return ["已注册"]


def _worker(mailbox):
    return protocol_android.ChatGPTAndroidProtocolWorker(
        mailbox=mailbox,
        mailbox_account=MailboxAccount(email="user@example.com", account_id="mail-1"),
        log_fn=lambda _message: None,
    )


def test_android_signup_email_verification_continues_without_duplicate_trigger(monkeypatch):
    _EmailVerificationSession.instances.clear()
    monkeypatch.setattr(protocol_android.requests, "Session", _EmailVerificationSession)
    monkeypatch.setattr(protocol_android, "_extract_chatgpt_account_id", lambda _token: "account-1")
    mailbox = _Mailbox()

    result = _worker(mailbox).run(email="user@example.com", password="Secret123!")

    assert result.success is True
    assert mailbox.baseline_reads == 1
    assert mailbox.marked == []
    first_party_calls = [
        call
        for call in _EmailVerificationSession.instances[0].post_calls
        if call[0] == protocol_android.FIRST_PARTY_AUTHORIZE_URL
    ]
    assert [call[1]["json"]["origin_page_type"] for call in first_party_calls] == [
        "email_otp_verification",
        "about_you",
    ]


def test_android_login_page_still_marks_existing_email(monkeypatch):
    monkeypatch.setattr(protocol_android.requests, "Session", _LoginSession)
    mailbox = _Mailbox()

    try:
        _worker(mailbox).run(email="user@example.com", password="Secret123!")
    except RuntimeError as exc:
        assert "进入登录页面" in str(exc)
    else:
        raise AssertionError("登录页面应结束注册流程")

    assert mailbox.marked == ["user@example.com"]
    assert _LoginSession.instances[-1].post_calls == []

class _RefreshSession:
    instances = []

    def __init__(self, response):
        self.trust_env = True
        self.proxies = {}
        self.calls = []
        self._response = response
        self.__class__.instances.append(self)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


class _BrokenJsonResponse:
    status_code = 502
    text = "<html>Bad Gateway</html>"

    def json(self):
        raise ValueError("not json")


def _refresh_response(payload, *, status_code: int = 200):
    return _Response(payload, url=protocol_android.TOKEN_URL, status_code=status_code)


def test_refresh_android_oauth_tokens_sends_android_refresh_request():
    session = _RefreshSession(
        _refresh_response(
            {
                "access_token": "access-new",
                "refresh_token": "rt-new",
                "id_token": "id-token-new",
                "token_type": "Bearer",
                "scope": protocol_android.SCOPES,
                "expires_in": 3600,
            }
        )
    )

    tokens = protocol_android.refresh_android_oauth_tokens("rt-old", session=session, timeout=17)

    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == protocol_android.TOKEN_URL == "https://auth.openai.com/oauth/token"
    assert kwargs["timeout"] == 17
    assert kwargs["json"] == {
        "grant_type": "refresh_token",
        "client_id": protocol_android.CLIENT_ID,
        "refresh_token": "rt-old",
        "scope": protocol_android.SCOPES,
    }
    headers = kwargs["headers"]
    assert headers["User-Agent"] == protocol_android.APP_USER_AGENT
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert tokens["access_token"] == "access-new"
    assert tokens["refresh_token"] == "rt-new"
    assert tokens["id_token"] == "id-token-new"
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == protocol_android.SCOPES
    assert tokens["expires_in"] == 3600
    assert tokens["expires_at"].endswith("Z")


def test_refresh_android_oauth_tokens_preserves_server_expires_at():
    session = _RefreshSession(
        _refresh_response(
            {
                "access_token": "access-new",
                "expires_at": "2026-08-31T00:00:00Z",
                "expires_in": 3600,
            }
        )
    )

    tokens = protocol_android.refresh_android_oauth_tokens("rt-old", session=session)

    assert tokens["expires_at"] == "2026-08-31T00:00:00Z"
    assert tokens["expires_in"] == 3600


def test_refresh_android_oauth_tokens_creates_trust_env_session_with_proxy(monkeypatch):
    created = []

    class _CreatedSession(_RefreshSession):
        def __init__(self):
            super().__init__(_refresh_response({"access_token": "access-new"}))
            created.append(self)

    monkeypatch.setattr(protocol_android.requests, "Session", _CreatedSession)

    tokens = protocol_android.refresh_android_oauth_tokens(
        "rt-old", proxy_url="http://127.0.0.1:7897"
    )

    assert tokens["access_token"] == "access-new"
    assert created[0].trust_env is False
    assert created[0].proxies == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }
    assert created[0].calls[0][1]["timeout"] == 30


def test_refresh_android_oauth_tokens_default_session_without_proxy(monkeypatch):
    created = []

    class _CreatedSession(_RefreshSession):
        def __init__(self):
            super().__init__(_refresh_response({"access_token": "access-new"}))
            created.append(self)

    monkeypatch.setattr(protocol_android.requests, "Session", _CreatedSession)

    protocol_android.refresh_android_oauth_tokens("rt-old")

    assert created[0].trust_env is False
    assert created[0].proxies == {}


def test_refresh_android_oauth_tokens_keeps_passed_session_proxies():
    session = _RefreshSession(_refresh_response({"access_token": "access-new"}))
    session.trust_env = True
    session.proxies = {"http": "preset", "https": "preset"}

    protocol_android.refresh_android_oauth_tokens("rt-old", session=session)

    assert session.trust_env is True
    assert session.proxies == {"http": "preset", "https": "preset"}


def test_refresh_android_oauth_tokens_reuses_refresh_token_when_not_rotated():
    session = _RefreshSession(
        _refresh_response(
            {
                "access_token": "access-new",
                "id_token": "id-token-new",
                "token_type": "Bearer",
                "scope": protocol_android.SCOPES,
            }
        )
    )

    tokens = protocol_android.refresh_android_oauth_tokens("rt-old", session=session)

    assert tokens["refresh_token"] == "rt-old"
    assert tokens["id_token"] == "id-token-new"
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == protocol_android.SCOPES
    assert tokens["expires_at"] == ""
    assert tokens["expires_in"] == 0


def test_refresh_android_oauth_tokens_rejects_blank_refresh_token():
    session = _RefreshSession(_refresh_response({"access_token": "access-new"}))

    with pytest.raises(ValueError, match="refresh_token"):
        protocol_android.refresh_android_oauth_tokens("   ", session=session)

    assert session.calls == []


def test_refresh_android_oauth_tokens_raises_on_http_error():
    session = _RefreshSession(
        _refresh_response({"error": "refresh_token_reused"}, status_code=400)
    )

    with pytest.raises(RuntimeError, match="HTTP 400"):
        protocol_android.refresh_android_oauth_tokens("rt-old", session=session)


def test_refresh_android_oauth_tokens_raises_on_non_json_body():
    session = _RefreshSession(_BrokenJsonResponse())

    with pytest.raises(RuntimeError, match="非 JSON"):
        protocol_android.refresh_android_oauth_tokens("rt-old", session=session)


def test_refresh_android_oauth_tokens_requires_access_token():
    session = _RefreshSession(_refresh_response({"error": "server_error"}))

    with pytest.raises(RuntimeError, match="access_token"):
        protocol_android.refresh_android_oauth_tokens("rt-old", session=session)
