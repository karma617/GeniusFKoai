"""CF Worker/cloud-mail mailbox compatibility tests."""
from __future__ import annotations

import pytest

from core.base_mailbox import CFWorkerMailbox, MailboxAccount, _create_cfworker


class FakeResponse:
    def __init__(self, payload=None, *, text="", status_code=200, json_error: Exception | None = None):
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self.payload


def test_cfworker_factory_ignores_registration_proxy_pool_proxy():
    mailbox = _create_cfworker(
        {
            "cfworker_api_url": "https://mail.edu.hsxhome.com",
            "cfworker_admin_token": "public-token",
            "cfworker_domain": "edu.hsxhome.com",
        },
        "http://pool.proxy:1000",
    )

    assert mailbox.proxy is None


def test_cfworker_factory_uses_explicit_mailbox_proxy_only():
    mailbox = _create_cfworker(
        {
            "cfworker_api_url": "https://mail.edu.hsxhome.com",
            "cfworker_admin_token": "public-token",
            "cfworker_domain": "edu.hsxhome.com",
            "mailbox_proxy": "http://127.0.0.1:7897",
        },
        "http://pool.proxy:1000",
    )

    assert mailbox.proxy == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }


def test_cfworker_falls_back_to_cloud_mail_public_api(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        return FakeResponse(text="not found", status_code=404, json_error=ValueError("not json"))

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/admin/new_address"):
            return FakeResponse(text="<html>not found</html>", status_code=404, json_error=ValueError("not json"))
        if url.endswith("/api/public/addUser"):
            assert kwargs["headers"]["Authorization"] == "public-token"
            email = kwargs["json"]["list"][0]["email"]
            assert email.endswith("@edu.hsxhome.com")
            return FakeResponse({"code": 200, "message": "success", "data": None})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )

    account = mailbox.get_email()

    assert account.email.endswith("@edu.hsxhome.com")
    assert account.account_id == account.email
    assert calls[0][0].endswith("/admin/new_address")
    assert calls[1][0].endswith("/api/public/addUser")


def test_cfworker_detects_cloud_mail_and_skips_legacy_admin_api(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        assert url.endswith("/api/setting/websiteConfig")
        return FakeResponse({"code": 200, "data": {"domainList": ["@edu.hsxhome.com"]}})

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        assert not url.endswith("/admin/new_address")
        assert url.endswith("/api/public/addUser")
        return FakeResponse({"code": 200, "message": "success", "data": None})

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )

    account = mailbox.get_email()

    assert account.email.endswith("@edu.hsxhome.com")
    assert len(calls) == 1


def test_cfworker_cloud_mail_email_list_is_normalized(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        assert url.endswith("/api/public/emailList")
        assert kwargs["headers"]["Authorization"] == "public-token"
        assert kwargs["json"]["toEmail"] == "user@edu.hsxhome.com"
        return FakeResponse(
            {
                "code": 200,
                "message": "success",
                "data": [
                    {
                        "emailId": 42,
                        "subject": "Your code",
                        "content": "<p>Code 654321</p>",
                        "text": "Code 654321",
                    }
                ],
            }
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cloud_mail"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="user@edu.hsxhome.com")

    assert mailbox.get_current_ids(account) == {"42"}
    assert mailbox.wait_for_code(account, timeout=1) == "654321"
    assert len(calls) == 2


def test_cfworker_cloud_mail_email_list_falls_back_to_fuzzy_match(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"]["toEmail"])
        assert url.endswith("/api/public/emailList")
        if kwargs["json"]["toEmail"] == "user@edu.hsxhome.com":
            return FakeResponse({"code": 200, "message": "success", "data": []})
        if kwargs["json"]["toEmail"] == "%user@edu.hsxhome.com%":
            return FakeResponse(
                {
                    "code": 200,
                    "message": "success",
                    "data": [
                        {
                            "emailId": 99,
                            "subject": "Your code",
                            "content": "<strong>112233</strong>",
                            "text": "",
                        }
                    ],
                }
            )
        raise AssertionError(f"Unexpected toEmail: {kwargs['json']['toEmail']}")

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cloud_mail"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="user@edu.hsxhome.com")

    assert mailbox.get_current_ids(account) == {"99"}
    assert mailbox.wait_for_code(account, timeout=1) == "112233"
    assert calls == [
        "user@edu.hsxhome.com",
        "%user@edu.hsxhome.com%",
        "user@edu.hsxhome.com",
        "%user@edu.hsxhome.com%",
    ]


def test_cfworker_wait_for_code_skips_messages_before_otp_sent_at(monkeypatch):
    def fake_post(url, **kwargs):
        assert url.endswith("/api/public/emailList")
        return FakeResponse(
            {
                "code": 200,
                "message": "success",
                "data": [
                    {
                        "emailId": 99,
                        "subject": "Old code",
                        "timestamp": 2_000.0,
                        "content": "<p>Code 111111</p>",
                        "text": "Code 111111",
                    },
                    {
                        "emailId": 2,
                        "subject": "New code",
                        "timestamp": 2_100.0,
                        "content": "<p>Code 222222</p>",
                        "text": "Code 222222",
                    },
                ],
            }
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cloud_mail"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="user@edu.hsxhome.com")

    assert mailbox.wait_for_code(
        account,
        timeout=1,
        before_ids={"99"},
        otp_sent_at=2_100.0,
    ) == "222222"


def test_cfworker_wait_for_code_keeps_new_id_even_if_provider_timestamp_is_old(monkeypatch):
    def fake_post(url, **kwargs):
        assert url.endswith("/api/public/emailList")
        return FakeResponse(
            {
                "code": 200,
                "message": "success",
                "data": [
                    {
                        "emailId": 99,
                        "subject": "Old code",
                        "timestamp": 2_000.0,
                        "content": "<p>Code 111111</p>",
                    },
                    {
                        "emailId": 2,
                        "subject": "New code",
                        "timestamp": 2_000.0,
                        "content": "<p>Code 222222</p>",
                    },
                ],
            }
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cloud_mail"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="user@edu.hsxhome.com")

    assert mailbox.wait_for_code(
        account,
        timeout=1,
        before_ids={"99"},
        otp_sent_at=2_100.0,
    ) == "222222"


def test_cfworker_wait_for_code_prefers_contextual_openai_code(monkeypatch):
    def fake_post(url, **kwargs):
        assert url.endswith("/api/public/emailList")
        return FakeResponse(
            {
                "code": 200,
                "message": "success",
                "data": [
                    {
                        "emailId": 2,
                        "subject": "OpenAI",
                        "content": "<div data-id='353740'>Your verification code is <b>222333</b></div>",
                    },
                ],
            }
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cloud_mail"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="user@edu.hsxhome.com")

    assert mailbox.wait_for_code(account, timeout=1) == "222333"


def test_cfworker_wait_for_code_keeps_only_mail_when_baseline_empty_even_if_timestamp_old(monkeypatch):
    def fake_post(url, **kwargs):
        assert url.endswith("/api/public/emailList")
        return FakeResponse(
            {
                "code": 200,
                "message": "success",
                "data": [
                    {
                        "emailId": 99,
                        "subject": "OpenAI code",
                        "timestamp": 2_000.0,
                        "content": "<p>Code 111111</p>",
                        "text": "Code 111111",
                    }
                ],
            }
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cloud_mail"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="user@edu.hsxhome.com")

    assert mailbox.wait_for_code(account, timeout=1, otp_sent_at=2_100.0) == "111111"


def test_cfworker_cloud_mail_public_credential_error_falls_back_to_admin_mails(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url))
        assert url.endswith("/admin/mails")
        return FakeResponse(
            {
                "results": [
                    {
                        "id": "msg-new",
                        "subject": "OpenAI verification",
                        "body": {"content": "<html>Your code is <b>333444</b></html>"},
                    }
                ]
            }
        )

    def fake_post(url, **kwargs):
        calls.append(("POST", url))
        assert url.endswith("/api/public/emailList")
        return FakeResponse(text="Invalid address credential", status_code=401, json_error=ValueError("not json"))

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="public-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cloud_mail"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="user@edu.hsxhome.com")

    assert mailbox.wait_for_code(account, timeout=1, otp_sent_at=2_100.0) == "333444"
    assert calls == [
        ("POST", "https://mail.edu.hsxhome.com/api/public/emailList"),
        ("GET", "https://mail.edu.hsxhome.com/admin/mails"),
    ]


def test_cfworker_legacy_wait_for_code_extracts_nested_body_content(monkeypatch):
    def fake_get(url, **kwargs):
        assert url.endswith("/admin/mails")
        return FakeResponse(
            {
                "results": [
                    {
                        "id": "msg-new",
                        "subject": "OpenAI verification",
                        "body": {"content": "<html>Your code is <b>333444</b></html>"},
                    }
                ]
            }
        )

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="admin-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cfworker"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="token")

    assert mailbox.wait_for_code(account, timeout=1) == "333444"


def test_cfworker_wait_for_code_timeout_reports_mail_count(monkeypatch):
    def fake_get(url, **kwargs):
        assert url.endswith("/admin/mails")
        return FakeResponse(
            {
                "results": [
                    {
                        "id": "msg-no-code",
                        "subject": "OpenAI notice",
                        "body": {"content": "No numeric code here"},
                    }
                ]
            }
        )

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    mailbox = CFWorkerMailbox(
        api_url="https://mail.edu.hsxhome.com",
        admin_token="admin-token",
        domain="edu.hsxhome.com",
    )
    mailbox._api_mode = "cfworker"

    account = MailboxAccount(email="user@edu.hsxhome.com", account_id="token")

    with pytest.raises(TimeoutError) as exc:
        mailbox.wait_for_code(account, timeout=1)

    assert "最后取信 1 封" in str(exc.value)
    assert "最近主题: OpenAI notice" in str(exc.value)
