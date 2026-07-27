"""outlookEmail mailbox provider tests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
import requests

from core import outlook_email_mailbox as outlook_module
from core.base_mailbox import MailboxAccount, create_mailbox
from core.outlook_email_mailbox import OutlookEmailMailbox, list_outlook_email_group_options
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
            ),
            FakeResponse({"success": True, "data": {"emails": []}}),
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


def test_outlook_email_group_options_load_from_admin_groups(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"success": True}),
            FakeResponse({"success": True, "csrf_token": "csrf-token"}),
            FakeResponse(
                {
                    "success": True,
                    "groups": [
                        {"id": 5, "name": "OpenAI 注册", "account_count": 12},
                        {"id": 6, "name": "备用"},
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    options = list_outlook_email_group_options(
        api_url="https://mail.example.test",
        admin_password="fake-admin-password",
    )

    assert options == [
        {"value": "5", "label": "OpenAI 注册 · 12 个邮箱", "id": "5", "name": "OpenAI 注册"},
        {"value": "6", "label": "备用", "id": "6", "name": "备用"},
    ]
    assert session.calls[0]["url"] == "https://mail.example.test/login"
    assert session.calls[2]["url"] == "https://mail.example.test/api/groups"
    assert session.calls[2]["headers"]["X-CSRFToken"] == "csrf-token"


def test_outlook_email_group_options_fallback_to_external_accounts(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {"id": 1, "email": "a@outlook.com", "group_id": 5, "group_name": "OpenAI 注册"},
                        {"id": 2, "email": "b@outlook.com", "group_id": 5, "group_name": "OpenAI 注册"},
                        {"id": 3, "email": "c@outlook.com", "group_id": 6, "group_name": "备用"},
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    options = list_outlook_email_group_options(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
    )

    assert options == [
        {"value": "5", "label": "OpenAI 注册", "id": "5", "name": "OpenAI 注册"},
        {"value": "6", "label": "备用", "id": "6", "name": "备用"},
    ]
    assert session.calls[0]["url"] == "https://mail.example.test/api/external/accounts"
    assert session.calls[0]["headers"]["X-API-Key"] == "fake-api-key"
    assert session.calls[0]["kwargs"]["params"] == {"limit": 1000, "offset": 0}
    assert session.trust_env is False


def test_outlook_email_reserves_selected_account_locally(monkeypatch):
    accounts_payload = {
        "success": True,
        "accounts": [
            {"id": 1, "email": "first@hotmail.com", "status": "active", "group_id": 7},
            {"id": 2, "email": "second@hotmail.com", "status": "active", "group_id": 7},
        ],
    }
    session = FakeSession(
        [
            FakeResponse(accounts_payload),
            FakeResponse(accounts_payload),
        ]
    )
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            super().__init__(
                [
                    FakeResponse({"success": True, "accounts": [{"id": 7, "email": "ok@outlook.com", "status": "active"}]}),
                    FakeResponse({"success": True, "data": {"emails": []}}),
                ]
            )
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            ),
            FakeResponse({"success": True, "data": {"emails": []}}),
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
            ),
            FakeResponse({"success": True, "data": {"emails": []}}),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
    )

    assert mailbox.get_email().email == "fresh@outlook.com"


def test_outlook_email_skips_non_normal_tags_by_default(monkeypatch):
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
                            "email": "alias-full@outlook.com",
                            "status": "active",
                            "tags": [{"id": 10, "name": "别名已上限"}],
                        },
                        {
                            "id": 3,
                            "email": "invalid@outlook.com",
                            "status": "active",
                            "tags": [{"id": 11, "name": "无效邮箱"}],
                        },
                        {
                            "id": 4,
                            "email": "fresh@outlook.com",
                            "status": "active",
                            "tags": [],
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
    )

    assert mailbox.get_email().email == "fresh@outlook.com"


def test_outlook_email_get_email_skips_readability_precheck(monkeypatch):
    logs: list[str] = []
    accounts_payload = {
        "success": True,
        "accounts": [
            {"id": 1, "email": "expired@outlook.com", "status": "active"},
            {"id": 2, "email": "fresh@outlook.com", "status": "active"},
        ],
    }
    session = FakeSession(
        [
            FakeResponse(accounts_payload),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
        log_fn=logs.append,
    )

    account = mailbox.get_email()

    assert account.email == "expired@outlook.com"
    assert len(session.calls) == 1
    assert all("/api/external/messages" not in call["url"] for call in session.calls)
    assert all(call.get("method") != "DELETE" for call in session.calls)
    assert "outlookEmail selecting mailbox candidate..." in logs
    assert any("outlookEmail candidate selected: expired@outlook.com" in item for item in logs)
    assert not any("readability precheck failed" in item for item in logs)
    assert not any("unreadable mailbox deleted" in item for item in logs)


def test_outlook_email_get_email_does_not_call_precheck_api(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {"id": 1, "email": "fresh@outlook.com", "status": "active"},
                    ],
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="bad-api-key",
        admin_password="fake-admin-password",
    )

    assert mailbox.get_email().email == "fresh@outlook.com"

    assert len(session.calls) == 1
    assert all(call.get("method") != "POST" for call in session.calls)
    assert all("/api/external/messages" not in call["url"] for call in session.calls)


def test_outlook_email_get_email_does_not_delete_cooldown_account_before_otp(monkeypatch):
    accounts_payload = {
        "success": True,
        "accounts": [
            {"id": 11, "email": "cooldown@outlook.com", "status": "active"},
            {"id": 12, "email": "fresh@outlook.com", "status": "active"},
        ],
    }
    session = FakeSession(
        [
            FakeResponse(accounts_payload),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        admin_password="fake-admin-password",
    )

    account = mailbox.get_email()

    assert account.email == "cooldown@outlook.com"
    assert len(session.calls) == 1
    assert all("/api/external/messages" not in call["url"] for call in session.calls)
    assert all(call.get("method") != "DELETE" for call in session.calls)


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


def test_outlook_email_extracts_openai_code_from_nested_html_detail(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "openai-html",
                            "subject": "OpenAI verification code",
                            "content_preview": "Enter this temporary verification code to continue:",
                            "folder": "inbox",
                        }
                    ],
                }
            ),
            FakeResponse(
                {
                    "success": True,
                    "email": {
                        "id": "openai-html",
                        "subject": "OpenAI verification code",
                        "body": {
                            "contentType": "html",
                            "content": (
                                '<td align="center"><p>Enter this temporary verification code to continue: </p>'
                                '<p style="font-family:Menlo">038818 </p>'
                                "<p>Please ignore this email if this wasn't you trying to create a OpenAI account.</p></td>"
                            ),
                        },
                        "folder": "inbox",
                    },
                }
            ),
            FakeResponse({"success": True, "data": {"emails": []}}),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        fixed_email="user@outlook.com",
        email_folder="inbox",
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=1) == "038818"


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


def test_outlook_email_plus_wait_for_code_prefers_async_probe_when_otp_time_known(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "probe_id": "probe-1",
                        "status": "pending",
                    },
                },
                status_code=202,
            ),
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "probe_id": "probe-1",
                        "status": "matched",
                        "message": {
                            "id": "new-code",
                            "subject": "OpenAI verification code",
                            "content_preview": "Your verification code is 445566",
                            "folder": "inbox",
                            "timestamp": 1_781_255_640,
                        },
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        email_folder="inbox",
        poll_interval=1,
    )
    account = mailbox._build_account(
        email="pool@outlook.com",
        account_id="12",
        source="outlook_email_plus_pool",
        raw={"account_id": 12, "email": "pool@outlook.com", "claim_token": "claim-token"},
    )

    code = mailbox.wait_for_code(
        account,
        keyword="OpenAI",
        before_ids={"old-code"},
        timeout=10,
        otp_sent_at=1_781_255_630.0,
    )

    assert code == "445566"
    assert session.calls[0]["url"] == "https://mail.example.test/api/external/wait-message"
    assert session.calls[0]["kwargs"]["params"]["mode"] == "async"
    assert session.calls[0]["kwargs"]["params"]["claim_token"] == "claim-token"
    assert session.calls[0]["kwargs"]["params"]["baseline_timestamp"] == 1_781_255_600
    assert session.calls[1]["url"] == "https://mail.example.test/api/external/probe/probe-1"


def test_outlook_email_plus_scans_inbox_before_requesting_junk(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "emails": [
                            {
                                "id": "latest-code",
                                "subject": "Your temporary ChatGPT verification code",
                                "body_preview": "Your code is 340139",
                            }
                        ]
                    },
                }
            )
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)
    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        email_folder="all",
    )
    mailbox._api_variant = "plus"
    account = mailbox._build_account(
        email="pool@outlook.com",
        account_id="12",
        source="outlook_email_plus_pool",
        raw={"account_id": 12, "email": "pool@outlook.com", "claim_token": "claim-token"},
    )

    assert mailbox.wait_for_code(account, timeout=10) == "340139"
    assert len(session.calls) == 1
    assert session.calls[0]["kwargs"]["params"]["folder"] == "inbox"
    assert session.calls[0]["kwargs"]["params"]["top"] == 3
    assert 5 < session.calls[0]["kwargs"]["timeout"] <= 10


def test_outlook_email_plus_checks_junk_when_inbox_transport_fails(monkeypatch):
    class InboxTimeoutSession(FakeSession):
        def get(self, url, **kwargs):
            self.calls.append({"url": url, "kwargs": kwargs, "headers": dict(self.headers)})
            if len(self.calls) == 1:
                raise requests.ReadTimeout("inbox timeout")
            return self.responses.pop(0)

    session = InboxTimeoutSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "emails": [
                            {
                                "id": "junk-code",
                                "subject": "Your temporary ChatGPT verification code",
                                "body_preview": "Your code is 771122",
                            }
                        ]
                    },
                }
            )
        ]
    )
    monkeypatch.setattr("requests.Session", lambda: session)
    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        email_folder="all",
    )
    mailbox._api_variant = "plus"
    account = mailbox._build_account(
        email="pool@outlook.com",
        account_id="12",
        source="outlook_email_plus_pool",
        raw={"account_id": 12, "email": "pool@outlook.com", "claim_token": "claim-token"},
    )

    assert mailbox.wait_for_code(account, timeout=10) == "771122"
    assert [call["kwargs"]["params"]["folder"] for call in session.calls] == ["inbox", "junkemail"]


def test_outlook_email_filters_alias_recipient_when_parent_inbox_has_multiple_alias_codes(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "emails": [
                            {
                                "id": "wrong-alias",
                                "subject": "OpenAI verification code",
                                "content_preview": "Your code is 111111",
                                "to_recipients": [{"email": "parent+a@outlook.com"}],
                            },
                            {
                                "id": "right-alias",
                                "subject": "OpenAI verification code",
                                "content_preview": "Your code is 222222",
                                "to_recipients": [{"email": "parent+b@outlook.com"}],
                            },
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
        email_folder="inbox",
    )
    account = MailboxAccount(
        email="parent@outlook.com",
        account_id="parent-1",
        extra={"email_alias": {"alias_email": "parent+b@outlook.com"}},
    )

    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "222222"


def test_outlook_email_plus_temporary_502_keeps_polling_for_code(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
            FakeResponse({"cloudflare_error": True, "detail": "Bad Gateway"}, status_code=502),
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
            FakeResponse({"success": True, "data": {"emails": []}}),
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

    with pytest.raises(outlook_module.OutlookEmailTemporaryUnavailable):
        mailbox.get_current_ids(account)
    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "112233"
    assert len(session.calls) == 7


def test_outlook_email_rejects_old_message_when_baseline_is_unavailable():
    mailbox = OutlookEmailMailbox(
        api_url="https://mail.example.test",
        api_key="fake-api-key",
        fixed_email="user@outlook.com",
    )
    account = mailbox.get_email()

    code = mailbox._code_from_message(
        account,
        {
            "id": "old-message",
            "subject": "Your ChatGPT code is 230473",
            "created_at": "2026-07-26T03:10:00Z",
        },
        keyword="",
        pattern=re.compile(outlook_module.DEFAULT_CODE_PATTERN),
        baseline_ids=set(),
        otp_sent_at=datetime(2026, 7, 26, 3, 15, tzinfo=timezone.utc).timestamp(),
    )

    assert code is None


def test_outlook_email_polling_respects_outer_deadline(monkeypatch):
    class TimeoutSession(FakeSession):
        def __init__(self):
            super().__init__([])

        def get(self, url, **kwargs):
            self.calls.append({"url": url, "kwargs": kwargs, "headers": dict(self.headers)})
            raise requests.ReadTimeout("slow local mailbox service")

    session = TimeoutSession()
    monkeypatch.setattr("requests.Session", lambda: session)
    mailbox = OutlookEmailMailbox(
        api_url="http://127.0.0.1:5000",
        api_key="fake-api-key",
        fixed_email="user@outlook.com",
        poll_interval=4,
    )
    account = mailbox.get_email()
    started = outlook_module.time.monotonic()

    with pytest.raises(TimeoutError):
        mailbox.wait_for_code(account, timeout=1)

    elapsed = outlook_module.time.monotonic() - started
    assert elapsed < 1.5
    assert session.calls
    assert all(0 < call["kwargs"]["timeout"] <= 1.0 for call in session.calls)


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


def test_outlook_email_marks_alias_exhausted_with_default_tag(monkeypatch):
    session = FakeSession(
        [
            FakeResponse({"success": True, "message": "ok"}),
            FakeResponse({"csrf_token": "csrf-token"}),
            FakeResponse({"success": True, "tags": []}),
            FakeResponse({"success": True, "tag": {"id": 10, "name": "别名已上限", "color": "#1a1a1a"}}),
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
        email="parent@outlook.com",
        account_id="2",
        source="account_list",
        raw={"id": 2, "email": "parent@outlook.com"},
    )

    assert mailbox.mark_alias_exhausted(account, reason="user_already_exists") == ["别名已上限"]
    assert session.calls[3]["url"] == "https://mail.example.test/api/tags"
    assert session.calls[4]["url"] == "https://mail.example.test/api/accounts/tags"
    assert session.calls[4]["kwargs"]["json"] == {"account_ids": [2], "tag_id": 10, "action": "add"}


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
    group_field = next(field for field in definition.get_fields() if field["key"] == "outlook_email_group_id")
    assert group_field["type"] == "async-select"
    assert group_field["asyncUrl"] == "/provider-settings/outlook-email/groups"
    assert group_field["asyncMethod"] == "POST"
    assert isinstance(mailbox, OutlookEmailMailbox)
    assert mailbox.get_email().email == "fixed@outlook.com"
