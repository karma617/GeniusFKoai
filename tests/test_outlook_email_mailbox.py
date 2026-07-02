"""outlookEmail mailbox provider tests."""
from __future__ import annotations

import json

import pytest
import requests

from core import outlook_email_mailbox as outlook_module
from core.base_mailbox import create_mailbox
from core.outlook_email_mailbox import OutlookEmailMailbox
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.proxies = {}
        self.verify = True
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs, "headers": dict(self.headers)})
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs, "headers": dict(self.headers), "method": "POST"})
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)

    def delete(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs, "headers": dict(self.headers), "method": "DELETE"})
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def clear_outlook_email_reservations():
    """测试前后清空本机邮箱占用锁，避免用例互相影响。"""
    with outlook_module._OUTLOOK_EMAIL_RESERVATION_LOCK:
        outlook_module._OUTLOOK_EMAIL_RESERVED_ACCOUNTS.clear()
    yield
    with outlook_module._OUTLOOK_EMAIL_RESERVATION_LOCK:
        outlook_module._OUTLOOK_EMAIL_RESERVED_ACCOUNTS.clear()


def test_outlook_email_fixed_email_does_not_expose_provider_secrets():
    mailbox = OutlookEmailMailbox(
        api_url="mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
        fixed_email="fixed@outlook.com",
    )

    account = mailbox.get_email()

    serialized_extra = json.dumps(account.extra, ensure_ascii=False)
    assert mailbox.api == "https://mail.example.test"
    assert account.email == "fixed@outlook.com"
    assert account.account_id == "fixed@outlook.com"
    assert account.extra["provider_account"]["credentials"] == {}
    assert "fake-api-key" not in serialized_extra
    assert "fake-admin-password" not in serialized_extra


def test_outlook_email_selects_first_usable_account_from_external_accounts(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {"id": 1, "email": "bad@outlook.com", "status": "disabled"},
                        {
                            "id": 2,
                            "email": "ok@hotmail.com",
                            "status": "active",
                            "group_id": 7,
                            "group_name": "default",
                        },
                    ],
                }
            )
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        group_id="7",
        account_limit="2",
        account_sort_by="email",
        account_sort_order="asc",
        account_tag_ids="3,5",
        account_include_untagged="true",
    )

    account = mailbox.get_email()

    assert account.email == "ok@hotmail.com"
    assert account.account_id == "2"
    assert session.calls[0]["url"] == "https://mail.example.test/api/external/accounts"
    assert session.calls[0]["headers"]["X-API-Key"] == "fake-api-key"
    assert session.calls[0]["kwargs"]["params"] == {
        "limit": 2,
        "offset": 0,
        "group_id": "7",
        "sort_by": "email",
        "sort_order": "asc",
        "tag_ids": "3,5",
        "include_untagged": "true",
    }


def test_outlook_email_local_api_ignores_registration_proxy(monkeypatch):
    session = FakeSession([FakeResponse({"success": True, "accounts": []})])
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="http://127.0.0.1:5001",
        api_key="fake-api-key",
        proxy="http://proxy.example.test:8080",
    )

    assert mailbox._list_accounts() == []
    assert mailbox.proxy is None
    assert session.proxies == {}
    assert session.trust_env is False


def test_outlook_email_remote_api_keeps_explicit_proxy(monkeypatch):
    session = FakeSession([FakeResponse({"success": True, "accounts": []})])
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        proxy="http://proxy.example.test:8080",
    )

    assert mailbox._list_accounts() == []
    assert mailbox.proxy == {
        "http": "http://proxy.example.test:8080",
        "https": "http://proxy.example.test:8080",
    }
    assert session.proxies == mailbox.proxy
    assert session.trust_env is False


def test_outlook_email_reserves_selected_account_locally(monkeypatch):
    accounts_payload = {
        "success": True,
        "accounts": [
            {"id": 1, "email": "first@hotmail.com", "status": "active", "group_id": 7},
            {"id": 2, "email": "second@hotmail.com", "status": "active", "group_id": 7},
        ],
    }
    session = FakeSession([FakeResponse(accounts_payload), FakeResponse(accounts_payload)])
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        group_id="7",
    )

    first = mailbox.get_email()
    second = mailbox.get_email()

    assert first.email == "first@hotmail.com"
    assert second.email == "second@hotmail.com"
    assert first.extra["provider_account"]["metadata"]["local_reservation_key"]


def test_outlook_email_scans_next_page_when_first_page_has_only_skip_tags(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {
                            "id": 1,
                            "email": "registered1@outlook.com",
                            "status": "active",
                            "tags": [{"name": "已注册"}],
                        },
                        {
                            "id": 2,
                            "email": "registered2@outlook.com",
                            "status": "active",
                            "tags": [{"name": "已注册"}],
                        },
                    ],
                }
            ),
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {
                            "id": 3,
                            "email": "fresh@outlook.com",
                            "status": "active",
                            "tags": [],
                        }
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        group_id="4",
        account_limit="2",
        skip_tag_names="已注册",
    )

    account = mailbox.get_email()

    assert account.email == "fresh@outlook.com"
    assert session.calls[0]["kwargs"]["params"]["offset"] == 0
    assert session.calls[1]["kwargs"]["params"]["offset"] == 2


def test_outlook_email_plus_claims_pool_when_legacy_accounts_endpoint_missing(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": "HTTP_ERROR",
                    "message": "资源不存在",
                    "data": {"status": 404},
                },
                status_code=404,
            ),
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "account_id": 12,
                        "email": "pool@outlook.com",
                        "claim_token": "claim-token",
                        "claimed_at": "2026-06-11T08:00:00Z",
                        "lease_expires_at": "2026-06-11T08:10:00Z",
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(api_url="https://mail.example.test", api_key="fake-api-key")
    account = mailbox.get_email()
    metadata = account.extra["provider_account"]["metadata"]

    assert account.email == "pool@outlook.com"
    assert account.account_id == "12"
    assert metadata["claim_token"] == "claim-token"
    assert metadata["pool_caller_id"] == "GeniusFKoai"
    assert metadata["pool_task_id"].startswith("mailbox-")
    assert session.calls[0]["url"] == "https://mail.example.test/api/external/accounts"
    assert session.calls[1]["url"] == "https://mail.example.test/api/external/pool/claim-random"
    assert session.calls[1]["kwargs"]["json"]["caller_id"] == "GeniusFKoai"


def test_outlook_email_plus_peek_releases_claim_for_provider_test(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"code": "HTTP_ERROR", "message": "资源不存在"}, status_code=404),
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "account_id": 12,
                        "email": "pool@outlook.com",
                        "claim_token": "claim-token",
                    },
                }
            ),
            FakeResponse({"success": True, "data": {"account_id": 12, "pool_status": "available"}}),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(api_url="https://mail.example.test", api_key="fake-api-key")

    assert mailbox.peek_email() == "pool@outlook.com"
    assert session.calls[2]["url"] == "https://mail.example.test/api/external/pool/claim-release"
    assert session.calls[2]["kwargs"]["json"]["account_id"] == 12
    assert session.calls[2]["kwargs"]["json"]["claim_token"] == "claim-token"


def test_outlook_email_plus_uses_admin_accounts_when_password_is_configured(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"code": "HTTP_ERROR", "message": "资源不存在"}, status_code=404),
            FakeResponse({"success": True, "message": "ok"}),
            FakeResponse({"csrf_token": "csrf-token"}),
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {"id": 1, "email": "skip@outlook.com", "status": "active", "tags": [{"name": "已注册"}]},
                        {"id": 2, "email": "fresh@outlook.com", "status": "active", "tags": [{"name": "可用"}]},
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
        group_id="4",
        account_tag_ids="1,2,3",
        skip_tag_names="已注册",
    )

    account = mailbox.get_email()

    assert account.email == "fresh@outlook.com"
    assert account.account_id == "2"
    assert session.calls[1]["url"] == "https://mail.example.test/login"
    assert session.calls[3]["url"].startswith("https://mail.example.test/api/accounts?")
    assert "group_id=4" in session.calls[3]["url"]
    assert "tag_ids=1%2C2%2C3" in session.calls[3]["url"]


def test_outlook_email_external_accounts_502_falls_back_to_admin_accounts(monkeypatch):
    sessions: list[FakeSession] = []
    responses = [
        [
            FakeResponse({"error": {"message": "Bad Gateway"}}, status_code=502),
        ],
        [
            FakeResponse({"success": True, "message": "ok"}),
            FakeResponse({"csrf_token": "csrf-token"}),
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {"id": 2, "email": "fresh@outlook.com", "status": "active"},
                    ],
                }
            ),
        ],
    ]

    def make_session():
        session = FakeSession(responses[len(sessions)])
        sessions.append(session)
        return session

    monkeypatch.setattr("requests.Session", make_session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
        group_id="4",
    )

    account = mailbox.get_email()

    assert account.email == "fresh@outlook.com"
    assert mailbox._api_variant == "plus_admin"
    assert sessions[0].calls[0]["url"] == "https://mail.example.test/api/external/accounts"
    assert sessions[1].calls[0]["url"] == "https://mail.example.test/login"
    assert sessions[1].calls[2]["url"].startswith("https://mail.example.test/api/accounts?")


def test_outlook_email_request_exception_rebuilds_session_before_retry(monkeypatch):
    sessions: list[FakeSession] = []

    class BrokenThenOkSession(FakeSession):
        def __init__(self, *, broken: bool):
            super().__init__([FakeResponse({"success": True, "accounts": [{"id": 7, "email": "ok@outlook.com", "status": "active"}]})])
            self.broken = broken
            self.closed = False

        def get(self, url, **kwargs):
            self.calls.append({"url": url, "kwargs": kwargs, "headers": dict(self.headers)})
            if self.broken:
                raise requests.ConnectionError("connection reset")
            return self.responses.pop(0)

        def close(self):
            self.closed = True

    def make_session():
        session = BrokenThenOkSession(broken=not sessions)
        sessions.append(session)
        return session

    monkeypatch.setattr("requests.Session", make_session)
    monkeypatch.setattr(outlook_module.time, "sleep", lambda _seconds: None)

    mailbox = OutlookEmailMailbox(api_url="https://mail.example.test", api_key="fake-api-key")

    account = mailbox.get_email()

    assert account.email == "ok@outlook.com"
    assert len(sessions) == 2
    assert sessions[0].closed is True
    assert sessions[0].calls[0]["url"] == "https://mail.example.test/api/external/accounts"
    assert sessions[1].calls[0]["url"] == "https://mail.example.test/api/external/accounts"


def test_outlook_email_external_accounts_feature_disabled_falls_back_to_admin_accounts(monkeypatch):
    sessions: list[FakeSession] = []
    responses = [
        [
            FakeResponse({"error": {"code": "FEATURE_DISABLED", "message": "Feature disabled"}}, status_code=403),
        ],
        [
            FakeResponse({"success": True, "message": "ok"}),
            FakeResponse({"csrf_token": "csrf-token"}),
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {"id": 3, "email": "admin@outlook.com", "status": "active"},
                    ],
                }
            ),
        ],
    ]

    def make_session():
        session = FakeSession(responses[len(sessions)])
        sessions.append(session)
        return session

    monkeypatch.setattr("requests.Session", make_session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
    )

    account = mailbox.get_email()

    assert account.email == "admin@outlook.com"
    assert mailbox._api_variant == "plus_admin"


def test_outlook_email_skips_accounts_with_custom_tag(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {
                            "id": 1,
                            "email": "registered@outlook.com",
                            "status": "active",
                            "tags": [{"id": 9, "name": "已注册"}],
                        },
                        {
                            "id": 2,
                            "email": "fresh@outlook.com",
                            "status": "active",
                            "tags": [{"id": 10, "name": "可用"}],
                        },
                    ],
                }
            )
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        skip_tag_names="已注册",
    )

    assert mailbox.get_email().email == "fresh@outlook.com"


def test_outlook_email_skips_invalid_email_tag_by_default(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {
                            "id": 1,
                            "email": "silent@outlook.com",
                            "status": "active",
                            "tags": [{"id": 9, "name": "无效邮箱"}],
                        },
                        {
                            "id": 2,
                            "email": "fresh@outlook.com",
                            "status": "active",
                            "tags": [],
                        },
                    ],
                }
            )
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
    )

    assert mailbox.get_email().email == "fresh@outlook.com"


def test_outlook_email_filters_before_ids_and_extracts_code(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "old",
                            "subject": "OpenAI verification",
                            "body_preview": "Old code 000000",
                            "folder": "inbox",
                        }
                    ],
                }
            ),
            FakeResponse({"success": True, "emails": []}),
            FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "old",
                            "subject": "OpenAI verification",
                            "body_preview": "Old code 000000",
                            "folder": "inbox",
                        },
                        {
                            "id": "new",
                            "subject": "OpenAI verification code",
                            "body_preview": "Your code is 123456",
                            "folder": "junkemail",
                        },
                    ],
                }
            ),
            FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "new",
                            "subject": "OpenAI verification code",
                            "body_preview": "Your code is 123456",
                            "folder": "junkemail",
                        },
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        fixed_email="user@outlook.com",
        email_folder="all",
        email_top="10",
    )
    account = mailbox.get_email()
    before_ids = mailbox.get_current_ids(account)

    code = mailbox.wait_for_code(account, keyword="OpenAI", before_ids=before_ids, timeout=1)

    assert before_ids == {"old"}
    assert code == "123456"
    assert session.calls[0]["kwargs"]["params"]["folder"] == "inbox"
    assert session.calls[1]["kwargs"]["params"]["folder"] == "junkemail"
    assert session.calls[2]["url"] == "https://mail.example.test/api/external/emails"
    assert session.calls[2]["kwargs"]["params"] == {
        "email": "user@outlook.com",
        "folder": "inbox",
        "top": 10,
        "keyword": "OpenAI",
    }
    assert session.calls[3]["kwargs"]["params"]["folder"] == "junkemail"


def test_outlook_email_reads_junk_detail_when_summary_has_no_code(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"success": True, "emails": []}),
            FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "junk-new",
                            "subject": "OpenAI verification code",
                            "content_preview": "OpenAI verification message",
                            "folder": "junkemail",
                        }
                    ],
                }
            ),
            FakeResponse(
                {
                    "success": True,
                    "email": {
                        "id": "junk-new",
                        "subject": "OpenAI verification code",
                        "content": "Your verification code is 778899",
                        "folder": "junkemail",
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        fixed_email="user@outlook.com",
        email_folder="all",
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "778899"
    assert session.calls[0]["kwargs"]["params"]["folder"] == "inbox"
    assert session.calls[1]["kwargs"]["params"]["folder"] == "junkemail"
    assert session.calls[2]["url"] == "https://mail.example.test/api/external/messages/junk-new"
    assert session.calls[2]["kwargs"]["params"] == {
        "folder": "junkemail",
        "email": "user@outlook.com",
    }


def test_outlook_email_does_not_skip_seen_message_after_otp_sent(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "new-after-send",
                            "subject": "Your ChatGPT code is 192978",
                            "content_preview": "Enter this temporary verification code to continue: 192978.",
                            "folder": "junkemail",
                            "created_at": "2026-06-12T09:14:30Z",
                        }
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        fixed_email="user@outlook.com",
        email_folder="junkemail",
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(
        account,
        before_ids={"new-after-send"},
        timeout=1,
        otp_sent_at=1_781_255_630.0,
    ) == "192978"


def test_outlook_email_plus_reads_messages_with_claim_token_when_legacy_emails_missing(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"code": "HTTP_ERROR", "message": "资源不存在"}, status_code=404),
            FakeResponse({"success": True, "data": {"emails": []}}),
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "emails": [
                            {
                                "id": "new",
                                "subject": "OpenAI verification code",
                                "content_preview": "Your code is 654321",
                                "folder": "junkemail",
                                "from_address": "noreply@openai.com",
                            }
                        ]
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        email_folder="all",
        email_top="10",
    )
    account = mailbox._build_account(
        email="pool@outlook.com",
        account_id="12",
        source="outlook_email_plus_pool",
        raw={"account_id": 12, "email": "pool@outlook.com", "claim_token": "claim-token"},
    )

    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "654321"
    assert session.calls[0]["url"] == "https://mail.example.test/api/external/emails"
    assert session.calls[1]["url"] == "https://mail.example.test/api/external/messages"
    assert session.calls[1]["kwargs"]["params"]["folder"] == "inbox"
    assert session.calls[1]["kwargs"]["params"]["claim_token"] == "claim-token"
    assert session.calls[2]["kwargs"]["params"]["folder"] == "junkemail"


def test_outlook_email_plus_temporary_502_keeps_polling_for_code(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"success": True, "data": {"emails": []}}),
            FakeResponse({"success": True, "data": {"emails": []}}),
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "emails": [
                            {
                                "id": "new",
                                "subject": "OpenAI verification code",
                                "content_preview": "Your code is 112233",
                                "folder": "junkemail",
                            }
                        ]
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)
    monkeypatch.setattr(outlook_module.time, "sleep", lambda _seconds: None)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        email_folder="all",
        email_top="10",
        poll_interval=1,
    )
    account = mailbox._build_account(
        email="pool@outlook.com",
        account_id="12",
        source="outlook_email_plus_pool",
        raw={"account_id": 12, "email": "pool@outlook.com", "claim_token": "claim-token"},
    )

    assert mailbox.get_current_ids(account) == set()
    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "112233"
    assert len(session.calls) == 9


def test_outlook_email_adds_registration_success_tag_via_admin_api(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"success": True, "message": "ok"}),
            FakeResponse({"csrf_token": "csrf-token"}),
            FakeResponse({"success": True, "tags": []}),
            FakeResponse({"success": True, "tag": {"id": 8, "name": "已注册", "color": "#1a1a1a"}}),
            FakeResponse({"success": True, "message": "成功处理 1 个账号"}),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
        register_success_tag_names="已注册",
    )
    account = mailbox._build_account(
        email="fresh@outlook.com",
        account_id="2",
        source="account_list",
        raw={"id": 2, "email": "fresh@outlook.com"},
    )

    assert mailbox.mark_registration_success(account) == ["已注册"]
    assert session.calls[0]["url"] == "https://mail.example.test/login"
    assert session.calls[0]["kwargs"]["json"] == {"password": "fake-admin-password"}
    assert session.calls[3]["url"] == "https://mail.example.test/api/tags"
    assert session.calls[4]["url"] == "https://mail.example.test/api/accounts/tags"
    assert session.calls[4]["kwargs"]["json"] == {"account_ids": [2], "tag_id": 8, "action": "add"}
    assert session.calls[4]["headers"]["X-CSRFToken"] == "csrf-token"


def test_outlook_email_marks_invalid_email_with_default_tag(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"success": True, "message": "ok"}),
            FakeResponse({"csrf_token": "csrf-token"}),
            FakeResponse({"success": True, "tags": []}),
            FakeResponse({"success": True, "tag": {"id": 9, "name": "无效邮箱", "color": "#1a1a1a"}}),
            FakeResponse({"success": True, "message": "成功处理 1 个账号"}),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
    )
    account = mailbox._build_account(
        email="silent@outlook.com",
        account_id="2",
        source="account_list",
        raw={"id": 2, "email": "silent@outlook.com"},
    )

    assert mailbox.mark_invalid_email(account, reason="invalid_email_no_otp") == ["无效邮箱"]
    assert session.calls[3]["url"] == "https://mail.example.test/api/tags"
    assert session.calls[4]["url"] == "https://mail.example.test/api/accounts/tags"
    assert session.calls[4]["kwargs"]["json"] == {"account_ids": [2], "tag_id": 9, "action": "add"}


def test_outlook_email_deletes_account_via_admin_api(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"success": True, "message": "ok"}),
            FakeResponse({"csrf_token": "csrf-token"}),
            FakeResponse({"success": True}),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
    )
    account = mailbox._build_account(
        email="bad@outlook.com",
        account_id="2",
        source="account_list",
        raw={"id": 2, "email": "bad@outlook.com"},
    )

    assert mailbox.delete_account(account, reason="openai_account_deleted_or_deactivated") is True
    assert session.calls[2]["method"] == "DELETE"
    assert session.calls[2]["url"] == "https://mail.example.test/api/accounts/2"
    assert session.calls[2]["headers"]["X-CSRFToken"] == "csrf-token"


def test_outlook_email_provider_definition_and_factory_are_wired():
    ProviderDefinitionsRepository().ensure_seeded()

    definition = ProviderDefinitionsRepository().get_by_key("mailbox", "outlook_email_api")
    mailbox = create_mailbox(
        "outlook_email_api",
        extra={
            "outlook_email_api_url": "https://mail.example.test",
            "outlook_email_api_key": "fake-api-key",
            "outlook_email_fixed_email": "fixed@outlook.com",
        },
    )

    assert definition is not None
    assert definition.driver_type == "outlook_email_api"
    assert isinstance(mailbox, OutlookEmailMailbox)
    assert mailbox.get_email().email == "fixed@outlook.com"
