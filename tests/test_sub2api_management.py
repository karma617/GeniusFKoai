from __future__ import annotations

import time

import pytest

from domain.accounts import AccountQuery, AccountRecord

from api.sub2api_management import _export_account_count
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
            items = [
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
            ]
            if "group=2" in path:
                items = [item for item in items if 2 in item["group_ids"]]
            return {
                "items": items,
                "total": len(items),
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


def test_sub2api_account_tags_can_assign_and_filter(monkeypatch):
    service = Sub2ApiManagementService()
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    monkeypatch.setattr(service, "_context", lambda: ctx)

    def fake_request(_origin, path, **_kwargs):
        if path == "/api/v1/admin/groups/all":
            return [{"id": 1, "name": "codex", "platform": "openai"}]
        if path.startswith("/api/v1/admin/accounts"):
            return {
                "items": [
                    {
                        "id": "10",
                        "name": "a@example.com",
                        "status": "active",
                        "group_ids": [1],
                        "credentials": {"plan_type": "k12"},
                    },
                    {
                        "id": "11",
                        "name": "b@example.com",
                        "status": "error",
                        "group_ids": [1],
                        "credentials": {"plan_type": "k12"},
                    },
                ],
                "total": 2,
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "_request_json", fake_request)

    tag = service.create_tag(name="待重登", color="red")["tag"]
    service.update_account_tags(account_ids=["10"], tag_ids=[tag["id"]], action="add")

    all_result = service.list_inventory()
    tagged = [item for item in all_result["accounts"] if item["id"] == "10"][0]
    untagged = [item for item in all_result["accounts"] if item["id"] == "11"][0]
    filtered = service.list_inventory(tag_id=tag["id"])
    untagged_filtered = service.list_inventory(untagged=True)

    assert all_result["tags"][0]["name"] == "待重登"
    assert all_result["tags"][0]["account_count"] == 1
    assert tagged["tags"] == [{"id": tag["id"], "name": "待重登", "color": "red"}]
    assert untagged["tags"] == []
    assert [item["id"] for item in filtered["accounts"]] == ["10"]
    assert [item["id"] for item in untagged_filtered["accounts"]] == ["11"]

    service.update_account_tags(account_ids=["10"], tag_ids=[tag["id"]], action="remove")
    removed = service.list_inventory(tag_id=tag["id"])

    assert removed["accounts"] == []


def test_list_inventory_uses_remote_pagination(monkeypatch):
    service = Sub2ApiManagementService()
    monkeypatch.setattr(service, "_context", lambda: Sub2ApiContext("https://sub2api.test", "token"))
    account_paths = []

    def fake_request(_origin, path, **_kwargs):
        if path == "/api/v1/admin/groups/all":
            return [{"id": 1, "name": "codex", "platform": "openai"}]
        if path.startswith("/api/v1/admin/accounts"):
            account_paths.append(path)
            return {
                "items": [
                    {
                        "id": "21",
                        "name": "page2@example.com",
                        "status": "active",
                        "group_ids": [1],
                        "credentials": {"plan_type": "k12"},
                    }
                ],
                "total": 31,
                "page": 2,
                "page_size": 10,
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "_request_json", fake_request)

    result = service.list_inventory(status="active", page=2, page_size=10)

    assert account_paths == ["/api/v1/admin/accounts?page=2&page_size=10&status=active"]
    assert result["total"] == 31
    assert result["page"] == 2
    assert result["page_size"] == 10
    assert result["accounts"][0]["id"] == "21"
    assert result["stats"] == {"total": 31, "active": 31, "error": 0, "k12": 1}


def test_export_accounts_data_calls_remote_data_endpoint(monkeypatch):
    service = Sub2ApiManagementService()
    monkeypatch.setattr(service, "_context", lambda: Sub2ApiContext("https://sub2api.test", "token"))
    seen = {}

    def fake_request(origin, path, **kwargs):
        seen["origin"] = origin
        seen["path"] = path
        seen["token"] = kwargs.get("token")
        seen["timeout"] = kwargs.get("timeout")
        return {"exportedAt": "2026-07-04T00:00:00Z", "accounts": [{"name": "a"}], "proxies": []}

    monkeypatch.setattr(module, "_request_json", fake_request)

    result = service.export_accounts_data(account_ids=["4916", "4915"], timezone_name="Asia/Shanghai")

    assert seen["origin"] == "https://sub2api.test"
    assert seen["path"] == "/api/v1/admin/accounts/data?ids=4916,4915&timezone=Asia%2FShanghai"
    assert seen["token"] == "token"
    assert seen["timeout"] == 90
    assert result["accounts"] == [{"name": "a"}]


def test_export_account_count_deduplicates_selected_ids():
    assert _export_account_count(["4916", "4915", "4916", "", "  "]) == 2


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


def test_bulk_check_without_ids_uses_filter_scope(monkeypatch):
    service = Sub2ApiManagementService()
    monkeypatch.setattr(service, "_context", lambda: Sub2ApiContext("https://sub2api.test", "token"))
    fetch_args = {}
    monkeypatch.setattr(
        service,
        "_fetch_all_accounts",
        lambda _ctx, **kwargs: fetch_args.update(kwargs) or [
            {"id": "10", "name": "a@example.com"},
            {"id": "11", "name": "b@example.com"},
        ],
    )
    tag = service.create_tag(name="待测活", color="red")["tag"]
    service.update_account_tags(account_ids=["11"], tag_ids=[tag["id"]], action="add")
    tested = []
    monkeypatch.setattr(service, "test_account", lambda _ctx, account_id, model_id: tested.append(account_id) or ("ok", "done"))
    monkeypatch.setattr(service, "set_account_error", lambda _ctx, account_id: False)

    result = service.bulk_check(
        account_ids=[],
        group_id=2,
        status="active",
        search="example.com",
        tag_id=tag["id"],
        concurrency=1,
    )

    assert fetch_args == {"group_id": 2, "status": "active", "search": "example.com"}
    assert tested == ["11"]
    assert [item["account_id"] for item in result["results"]] == ["11"]


def test_bulk_check_without_ids_reports_empty_filter_scope(monkeypatch):
    service = Sub2ApiManagementService()
    monkeypatch.setattr(service, "_context", lambda: Sub2ApiContext("https://sub2api.test", "token"))
    monkeypatch.setattr(service, "_fetch_all_accounts", lambda _ctx, **_kwargs: [])

    with pytest.raises(ValueError, match="当前筛选条件下没有可测活的 Sub2API 账号"):
        service.bulk_check(account_ids=[], concurrency=1)


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


def test_relogin_phone_required_deletes_remote_account(monkeypatch):
    local = AccountRecord(id=1, platform="chatgpt", email="phone@example.com", password="pw")
    service = Sub2ApiManagementService(
        repository=FakeRepository([local]),
        browser_relogin=lambda _account, logs: logs.append("final=https://auth.openai.com/add-phone") or {
            "session": {"accessToken": "token"},
            "cookies": "x=y",
        },
    )
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    deleted = []
    monkeypatch.setattr(service, "delete_account", lambda _ctx, account_id: deleted.append(account_id) or (True, "deleted"))

    result = service._relogin_one(
        ctx,
        {
            "id": "9",
            "name": "phone@example.com",
            "status": "error",
            "credentials": {"plan_type": "k12", "organization_id": "workspace-1"},
        },
    )

    assert result["status"] == "deleted"
    assert deleted == ["9"]


def test_relogin_login_failure_deletes_remote_account(monkeypatch):
    local = AccountRecord(id=1, platform="chatgpt", email="fail@example.com", password="pw")
    service = Sub2ApiManagementService(
        repository=FakeRepository([local]),
        browser_relogin=lambda _account, _logs: {"error": "protocol relogin failed"},
    )
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    deleted = []
    monkeypatch.setattr(service, "delete_account", lambda _ctx, account_id: deleted.append(account_id) or (True, "deleted"))

    result = service._relogin_one(
        ctx,
        {
            "id": "10",
            "name": "fail@example.com",
            "status": "error",
            "credentials": {"plan_type": "k12", "organization_id": "workspace-1"},
        },
    )

    assert result["status"] == "deleted"
    assert deleted == ["10"]


def test_relogin_k12_replace_failure_deletes_remote_account(monkeypatch):
    local = AccountRecord(id=1, platform="chatgpt", email="k12@example.com", password="pw")
    service = Sub2ApiManagementService(
        repository=FakeRepository([local]),
        browser_relogin=lambda _account, _logs: {
            "session": {"accessToken": "token", "sessionToken": "session"},
            "cookies": "__Secure-next-auth.session-token=session",
        },
    )
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    deleted = []
    monkeypatch.setattr(service, "_replace_k12_account", lambda *_args, **_kwargs: {"ok": False, "message": "K12 替换失败"})
    monkeypatch.setattr(service, "delete_account", lambda _ctx, account_id: deleted.append(account_id) or (True, "deleted"))

    result = service._relogin_one(
        ctx,
        {
            "id": "12",
            "name": "k12@example.com",
            "status": "error",
            "credentials": {"plan_type": "k12", "organization_id": "workspace-1"},
        },
    )

    assert result["status"] == "deleted"
    assert deleted == ["12"]


def test_find_local_account_matches_gmail_api_code_resource_family():
    local = AccountRecord(
        id=1,
        platform="chatgpt",
        email="phkong8269@gmail.com",
        password="pw",
        provider_resources=[
            {
                "provider_type": "mailbox",
                "provider_name": "gmail_api_code",
                "resource_type": "mailbox",
                "resource_identifier": "phkong8269@gmail.com",
                "handle": "phkong8269@gmail.com",
                "metadata": {
                    "email": "phkong8269@gmail.com",
                    "code_url": "https://gapi.mailsapi.com/api/code/fetch?token=token&uid=uid",
                },
            }
        ],
    )
    service = Sub2ApiManagementService(repository=FakeRepository([local]))
    logs = []

    result = service._find_local_account("phkong8269+1133@gmail.com", log_fn=logs.append)

    assert result is local
    assert any("通过 Gmail 邮箱服务匹配本地账号" in item for item in logs)
    assert any("provider=gmail_api_code" in item for item in logs)


def test_find_local_account_matches_gmail_oauth_master_resource_family():
    local = AccountRecord(
        id=1,
        platform="chatgpt",
        email="k12-local@example.com",
        password="pw",
        provider_resources=[
            {
                "provider_type": "mailbox",
                "provider_name": "gmail_oauth_fission",
                "resource_type": "mailbox",
                "resource_identifier": "phkong8269@gmail.com",
                "handle": "phkong8269+old@gmail.com",
                "metadata": {
                    "email": "phkong8269+old@gmail.com",
                    "master_email": "phkong8269@gmail.com",
                },
            }
        ],
    )
    service = Sub2ApiManagementService(repository=FakeRepository([local]))

    result = service._find_local_account("p.h.kong8269+1133@googlemail.com")

    assert result is local


def test_protocol_relogin_uses_gmail_alias_login_and_parent_inbox(monkeypatch):
    local = AccountRecord(
        id=1,
        platform="chatgpt",
        email="phkong8269@gmail.com",
        password="pw",
        provider_resources=[
            {
                "provider_type": "mailbox",
                "provider_name": "gmail_api_code",
                "resource_type": "mailbox",
                "resource_identifier": "phkong8269@gmail.com",
                "handle": "phkong8269@gmail.com",
                "display_name": "phkong8269@gmail.com",
                "metadata": {
                    "email": "phkong8269@gmail.com",
                    "code_url": "https://gapi.mailsapi.com/api/code/fetch?token=token&uid=uid",
                },
            }
        ],
    )
    service = Sub2ApiManagementService(repository=FakeRepository([local]))
    logs = []
    relogin_account = service._prepare_gmail_alias_relogin_account(
        local,
        "phkong8269+1133@gmail.com",
        logs.append,
    )
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
            assert self.email == "phkong8269+1133@gmail.com"
            created = self.email_service.create_email()
            assert created["email"] == "phkong8269+1133@gmail.com"
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

    result = service._run_protocol_relogin(relogin_account, logs)

    assert result["session"]["accessToken"] == "web-token"
    assert seen_accounts == [
        ("baseline", "phkong8269@gmail.com", "phkong8269@gmail.com"),
        ("wait", "phkong8269@gmail.com", "phkong8269@gmail.com"),
    ]
    assert any("Gmail 别名重登映射" in item for item in logs)
    assert any("检测到别名邮箱" in item and "parent=phkong8269@gmail.com" in item for item in logs)


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


def test_replace_k12_account_uploads_each_workspace(monkeypatch):
    service = Sub2ApiManagementService(repository=FakeRepository([]))
    ctx = Sub2ApiContext("https://sub2api.test", "token")
    calls = {"join": 0, "delete": 0}
    uploaded_workspace_ids = []

    from platforms.chatgpt import k12_join

    monkeypatch.setattr(k12_join, "parse_workspace_ids", lambda _raw: ["workspace-a", "workspace-b"])
    monkeypatch.setattr(k12_join, "ensure_chatgpt_session_cookie", lambda cookies, session_token="": f"{cookies}; __Secure-next-auth.session-token={session_token}")
    monkeypatch.setattr(
        k12_join,
        "exchange_workspace_session",
        lambda workspace_id, **_kwargs: {"accessToken": f"k12-token-{workspace_id}", "sessionToken": f"k12-session-{workspace_id}"},
    )

    def fake_join(**_kwargs):
        calls["join"] += 1
        return [{"ok": True}]

    def fake_upload(_session, **kwargs):
        uploaded_workspace_ids.append(kwargs.get("workspace_id"))
        return True, f"uploaded {kwargs.get('workspace_id')}"

    monkeypatch.setattr(k12_join, "send_workspace_join_requests", fake_join)
    monkeypatch.setattr(k12_join, "upload_session_to_sub2api", fake_upload)
    monkeypatch.setattr(service, "delete_account", lambda _ctx, _account_id: calls.__setitem__("delete", calls["delete"] + 1) or (True, "deleted"))

    result = service._replace_k12_account(
        ctx,
        {"id": "13"},
        {
            "session": {"accessToken": "web-token", "sessionToken": "web-session"},
            "cookies": "__Secure-next-auth.session-token=web-session",
        },
        "workspace-a,workspace-b",
    )

    assert result["ok"] is True
    assert calls == {"join": 0, "delete": 1}
    assert uploaded_workspace_ids == ["workspace-a", "workspace-b"]
    assert [item["workspace_id"] for item in result["k12_sessions"]] == ["workspace-a", "workspace-b"]
