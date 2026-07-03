from __future__ import annotations

import time

from domain.accounts import AccountQuery, AccountRecord

from application import sub2api_management as module
from application.sub2api_management import Sub2ApiContext, Sub2ApiManagementService


class FakeRepository:
    def __init__(self, records=None):
        self.records = records or []
        self.updated = []

    def list(self, query: AccountQuery):
        items = [
            item
            for item in self.records
            if (not query.platform or item.platform == query.platform)
            and (not query.email or query.email.lower() in item.email.lower())
        ]
        return len(items), items

    def update(self, account_id, command):
        self.updated.append((account_id, command))
        return None


def test_list_inventory_filters_accounts_by_group(monkeypatch):
    monkeypatch.setattr(module, "login_sub2api", lambda *_args, **_kwargs: ("https://sub2api.test", "token"))

    def fake_request(_origin, path, **_kwargs):
        if path == "/api/v1/admin/groups/all":
            return [
                {"id": 1, "name": "codex", "platform": "openai"},
                {"id": 2, "name": "k12", "platform": "openai"},
            ]
        if path.startswith("/api/v1/admin/accounts"):
            return {
                "items": [
                    {
                        "id": 10,
                        "name": "a@example.com",
                        "status": "active",
                        "group_ids": [1],
                        "credentials": {"plan_type": "free"},
                    },
                    {
                        "id": 11,
                        "name": "b@example.com",
                        "status": "error",
                        "group_ids": [2],
                        "credentials": {"plan_type": "k12", "organization_id": "workspace-1"},
                    },
                ],
                "total": 2,
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "_request_json", fake_request)

    service = Sub2ApiManagementService()
    monkeypatch.setattr(service, "_context", lambda: Sub2ApiContext("https://sub2api.test", "token"))

    result = service.list_inventory(group_id=2)

    assert result["total"] == 1
    assert result["accounts"][0]["id"] == "11"
    assert result["accounts"][0]["plan_type"] == "k12"
    assert result["accounts"][0]["workspace_id"] == "workspace-1"


def test_bulk_check_marks_failed_accounts_error(monkeypatch):
    service = Sub2ApiManagementService()
    monkeypatch.setattr(service, "_context", lambda: Sub2ApiContext("https://sub2api.test", "token"))
    monkeypatch.setattr(service, "test_account", lambda _ctx, account_id, model_id: ("dead", "bad token") if account_id == "2" else ("ok", "done"))
    marked = []
    monkeypatch.setattr(service, "set_account_error", lambda _ctx, account_id: marked.append(account_id) or True)

    result = service.bulk_check(account_ids=["1", "2"], concurrency=2)

    assert result["summary"]["ok"] == 1
    assert result["summary"]["failed"] == 1
    assert result["summary"]["marked_error"] == 1
    assert marked == ["2"]


def test_bulk_check_does_not_mark_usage_limit_as_error(monkeypatch):
    service = Sub2ApiManagementService()
    monkeypatch.setattr(service, "_context", lambda: Sub2ApiContext("https://sub2api.test", "token"))
    monkeypatch.setattr(
        service,
        "test_account",
        lambda _ctx, account_id, model_id: (
            "rate_limited",
            'API returned 429: {"error":{"type":"usage_limit_reached","message":"The usage limit has been reached"}}',
        ),
    )
    marked = []
    monkeypatch.setattr(service, "set_account_error", lambda _ctx, account_id: marked.append(account_id) or True)

    result = service.bulk_check(account_ids=["1"], concurrency=1)

    assert result["summary"]["rate_limited"] == 1
    assert result["summary"]["skipped"] == 1
    assert result["summary"]["marked_error"] == 0
    assert result["results"][0]["result"] == "rate_limited"
    assert marked == []


def test_relogin_deactivated_error_deletes_remote_account(monkeypatch):
    local = AccountRecord(id=1, platform="chatgpt", email="dead@example.com", password="pw")
    service = Sub2ApiManagementService(
        repository=FakeRepository([local]),
        browser_relogin=lambda _account, _logs: {"error": "account_deactivated: deleted or deactivated"},
    )
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    monkeypatch.setattr(service, "_context", lambda: ctx)
    monkeypatch.setattr(
        service,
        "_fetch_all_accounts",
        lambda _ctx: [
            {
                "id": 7,
                "name": "dead@example.com",
                "status": "error",
                "group_ids": [],
                "credentials": {"plan_type": "k12"},
            }
        ],
    )
    monkeypatch.setattr(module, "_request_json", lambda *_args, **_kwargs: [])
    deleted = []
    monkeypatch.setattr(service, "delete_account", lambda _ctx, account_id: deleted.append(account_id) or (True, "deleted"))

    result = service.relogin_error_accounts()

    assert result["summary"]["deleted"] == 1
    assert deleted == ["7"]
    assert result["results"][0]["status"] == "deleted"


def test_relogin_free_account_skips_without_deleting(monkeypatch):
    local = AccountRecord(id=1, platform="chatgpt", email="free@example.com", password="pw")
    service = Sub2ApiManagementService(
        repository=FakeRepository([local]),
        browser_relogin=lambda _account, _logs: {"session": {"accessToken": "token"}, "cookies": "x=y"},
    )
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    monkeypatch.setattr(service, "_context", lambda: ctx)
    monkeypatch.setattr(
        service,
        "_fetch_all_accounts",
        lambda _ctx: [
            {
                "id": 8,
                "name": "free@example.com",
                "status": "error",
                "group_ids": [],
                "credentials": {"plan_type": "free"},
            }
        ],
    )
    monkeypatch.setattr(module, "_request_json", lambda *_args, **_kwargs: [])
    deleted = []
    monkeypatch.setattr(service, "delete_account", lambda _ctx, account_id: deleted.append(account_id) or (True, "deleted"))

    result = service.relogin_error_accounts()

    assert result["summary"]["free_skipped"] == 1
    assert deleted == []
    assert result["results"][0]["status"] == "free_skipped"


def test_relogin_phone_required_skips_even_with_session(monkeypatch):
    local = AccountRecord(id=1, platform="chatgpt", email="phone@example.com", password="pw")
    service = Sub2ApiManagementService(
        repository=FakeRepository([local]),
        browser_relogin=lambda _account, logs: logs.append("final=https://auth.openai.com/add-phone") or {
            "session": {"accessToken": "token"},
            "cookies": "x=y",
        },
    )
    ctx = Sub2ApiContext("https://sub2api.test", "token")

    result = service._relogin_one(
        ctx,
        {
            "id": "9",
            "name": "phone@example.com",
            "status": "error",
            "credentials": {"plan_type": "k12", "organization_id": "workspace-1"},
        },
    )

    assert result["status"] == "phone_skipped"


def test_run_protocol_relogin_uses_registration_engine(monkeypatch):
    local = AccountRecord(
        id=1,
        platform="chatgpt",
        email="proto@example.com",
        password="pw",
        provider_resources=[
            {
                "provider_type": "mailbox",
                "provider_name": "outlook_email",
                "resource_type": "mailbox",
                "resource_identifier": "mail-1",
                "handle": "proto@example.com",
                "metadata": {"email": "proto@example.com", "account_id": "mail-1"},
            }
        ],
    )
    service = Sub2ApiManagementService(repository=FakeRepository([local]))
    logs = []

    from platforms.chatgpt import register as register_module
    import core.base_mailbox as base_mailbox

    class FakeMailbox:
        def get_current_ids(self, _account):
            return set()

        def wait_for_code(self, _account, **_kwargs):
            return "123456"

    monkeypatch.setattr(base_mailbox, "create_mailbox", lambda *_args, **_kwargs: FakeMailbox())

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.callback_logger = callback_logger
            self.email = ""
            self.password = ""
            self.k12_join_enabled = False
            self.k12_workspace_ids = ""

        def run(self):
            self.email_service.create_email()
            if self.callback_logger:
                self.callback_logger("fake protocol engine ran")
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "metadata": {
                        "session": {"accessToken": "web-token", "sessionToken": "web-session"},
                        "cookies": "__Secure-next-auth.session-token=web-session",
                    },
                    "session_token": "web-session",
                    "access_token": "web-token",
                    "account_id": "acct-1",
                    "to_dict": lambda self: {"success": True},
                },
            )()

    monkeypatch.setattr(register_module, "RegistrationEngine", FakeEngine)

    result = service._run_protocol_relogin(local, logs)

    assert result["session"]["accessToken"] == "web-token"
    assert any("不启动浏览器" in item for item in logs)
    assert any("fake protocol engine ran" in item for item in logs)


def test_protocol_relogin_alias_mailbox_reads_parent_inbox(monkeypatch):
    local = AccountRecord(
        id=1,
        platform="chatgpt",
        email="main+alias@example.com",
        password="pw",
        provider_resources=[
            {
                "provider_type": "mailbox",
                "provider_name": "outlook_email_api",
                "resource_type": "mailbox",
                "resource_identifier": "parent-1",
                "handle": "main+alias@example.com",
                "display_name": "main+alias@example.com",
                "metadata": {
                    "account_id": "parent-1",
                    "email": "main+alias@example.com",
                },
            },
            {
                "provider_type": "mailbox",
                "provider_name": "outlook_email",
                "resource_type": "mailbox",
                "resource_identifier": "parent-1",
                "handle": "main+alias@example.com",
                "display_name": "main+alias@example.com",
                "metadata": {
                    "email": "Main@Example.com",
                    "account_id": "parent-1",
                    "alias_parent_email": "main@example.com",
                    "alias_parent_account_id": "parent-1",
                },
            }
        ],
    )
    service = Sub2ApiManagementService(repository=FakeRepository([local]))
    logs = []
    seen_accounts = []

    from platforms.chatgpt import register as register_module
    import core.base_mailbox as base_mailbox

    class FakeMailbox:
        def get_current_ids(self, account):
            seen_accounts.append(("baseline", account.email, account.account_id))
            return set()

        def wait_for_code(self, account, **_kwargs):
            seen_accounts.append(("wait", account.email, account.account_id))
            return "123456"

    monkeypatch.setattr(base_mailbox, "create_mailbox", lambda provider, **_kwargs: FakeMailbox())

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None):
            self.email_service = email_service
            self.callback_logger = callback_logger
            self.email = ""
            self.password = ""
            self.k12_join_enabled = False
            self.k12_workspace_ids = ""

        def run(self):
            created = self.email_service.create_email()
            assert created["email"] == "main+alias@example.com"
            assert self.email_service.get_verification_code(timeout=1) == "123456"
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "metadata": {"session": {"accessToken": "web-token"}, "cookies": "x=y"},
                    "session_token": "web-session",
                    "access_token": "web-token",
                    "account_id": "acct-1",
                    "to_dict": lambda self: {"success": True},
                },
            )()

    monkeypatch.setattr(register_module, "RegistrationEngine", FakeEngine)

    result = service._run_protocol_relogin(local, logs)

    assert result["session"]["accessToken"] == "web-token"
    assert seen_accounts == [
        ("baseline", "Main@Example.com", "parent-1"),
        ("wait", "Main@Example.com", "parent-1"),
    ]
    assert any("outlook_email -> outlook_email_api" in item for item in logs)
    assert any("检测到别名邮箱" in item and "parent=main@example.com" in item for item in logs)


def test_relogin_stream_emits_protocol_logs(monkeypatch):
    local = AccountRecord(id=1, platform="chatgpt", email="k12@example.com", password="pw")
    service = Sub2ApiManagementService(
        repository=FakeRepository([local]),
        browser_relogin=lambda _account, logs: logs.append("使用批量注册同款 Platform 协议链路重新登录，不启动浏览器") or {
            "session": {"accessToken": "token"},
            "cookies": "x=y",
        },
    )
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    monkeypatch.setattr(service, "_context", lambda: ctx)
    monkeypatch.setattr(
        service,
        "_fetch_all_accounts",
        lambda _ctx: [
            {
                "id": 10,
                "name": "k12@example.com",
                "status": "error",
                "group_ids": [],
                "credentials": {"plan_type": "free"},
            }
        ],
    )
    monkeypatch.setattr(module, "_request_json", lambda *_args, **_kwargs: [])

    events = list(service.relogin_error_account_events(concurrency=1))

    assert any(item.get("event") == "relogin_log" and "不启动浏览器" in item.get("message", "") for item in events)
    assert events[-1]["event"] == "relogin_finished"
    assert events[-1]["summary"]["free_skipped"] == 1


def test_relogin_stream_processes_accounts_serially(monkeypatch):
    service = Sub2ApiManagementService(repository=FakeRepository([]))
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    monkeypatch.setattr(service, "_context", lambda: ctx)
    monkeypatch.setattr(
        service,
        "_fetch_all_accounts",
        lambda _ctx: [
            {"id": 10, "name": "a@example.com", "status": "error", "group_ids": []},
            {"id": 11, "name": "b@example.com", "status": "error", "group_ids": []},
        ],
    )
    monkeypatch.setattr(module, "_request_json", lambda *_args, **_kwargs: [])
    active = {"count": 0, "max": 0}
    order = []

    def fake_relogin(_ctx, remote_account, **_kwargs):
        active["count"] += 1
        active["max"] = max(active["max"], active["count"])
        order.append(remote_account["id"])
        time.sleep(0.01)
        active["count"] -= 1
        return {"account_id": str(remote_account["id"]), "status": "skipped", "message": "ok"}

    monkeypatch.setattr(service, "_relogin_one", fake_relogin)

    events = list(service.relogin_error_account_events(concurrency=5))

    assert events[0]["concurrency"] == 1
    assert active["max"] == 1
    assert order == ["10", "11"]
    assert events[-1]["event"] == "relogin_finished"


def test_replace_k12_account_direct_exchange_skips_join(monkeypatch):
    service = Sub2ApiManagementService(repository=FakeRepository([]))
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    calls = {"join": 0, "delete": 0, "upload": 0}

    from platforms.chatgpt import k12_join

    monkeypatch.setattr(k12_join, "parse_workspace_ids", lambda raw: [raw])
    monkeypatch.setattr(k12_join, "ensure_chatgpt_session_cookie", lambda cookies, session_token="": f"{cookies}; __Secure-next-auth.session-token={session_token}")
    monkeypatch.setattr(k12_join, "exchange_workspace_session", lambda **_kwargs: {"accessToken": "k12-token", "sessionToken": "k12-session"})

    def fake_join(**_kwargs):
        calls["join"] += 1
        return [{"ok": True}]

    def fake_upload(_session, **_kwargs):
        calls["upload"] += 1
        return True, "uploaded"

    monkeypatch.setattr(k12_join, "send_workspace_join_requests", fake_join)
    monkeypatch.setattr(k12_join, "upload_session_to_sub2api", fake_upload)
    monkeypatch.setattr(service, "delete_account", lambda _ctx, _account_id: calls.__setitem__("delete", calls["delete"] + 1) or (True, "deleted"))

    result = service._replace_k12_account(
        ctx,
        {"id": "11"},
        {
            "session": {"accessToken": "web-token", "sessionToken": "web-session"},
            "cookies": "__Secure-next-auth.session-token=web-session",
        },
        "workspace-1",
    )

    assert result["ok"] is True
    assert calls == {"join": 0, "delete": 1, "upload": 1}
