from __future__ import annotations

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
