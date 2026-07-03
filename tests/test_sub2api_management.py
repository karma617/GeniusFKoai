from __future__ import annotations

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
