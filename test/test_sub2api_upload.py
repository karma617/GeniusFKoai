from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from platforms.chatgpt import sub2api_upload as mod


def _jwt(payload: dict) -> str:
    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{part({'alg': 'none'})}.{part(payload)}.sig"


def test_upload_to_sub2api_imports_codex_session(monkeypatch):
    """SUB2API 主路径：登录、查分组、调用 codex-session 导入。"""
    captured: dict[str, dict] = {}

    def fake_request_json(origin, path, **kwargs):
        captured[path] = kwargs
        assert origin == "https://sub.example"
        if path == "/api/v1/auth/login":
            assert kwargs["body"] == {"email": "admin@example.com", "password": "secret"}
            return {"access_token": "admin-token"}
        if path == "/api/v1/admin/groups/all":
            assert kwargs["token"] == "admin-token"
            return [{"id": 7, "name": "codex", "platform": "openai"}]
        if path == "/api/v1/admin/accounts/import/codex-session":
            return {"total": 1, "created": 1, "updated": 0, "skipped": 0, "failed": 0}
        raise AssertionError(path)

    monkeypatch.setattr(mod, "_request_json", fake_request_json)
    account = SimpleNamespace(
        email="user@example.com",
        access_token=_jwt({"exp": 1893456000, "email": "user@example.com"}),
        refresh_token="rt",
        session={"user": {"email": "user@example.com"}},
        extra={"session": {"user": {"email": "user@example.com"}}},
    )

    ok, message = mod.upload_to_sub2api(
        account,
        api_url="https://sub.example/admin/accounts",
        email="admin@example.com",
        password="secret",
        group_name="codex",
        account_priority="3",
    )

    assert ok is True
    assert "新建 1" in message
    body = captured["/api/v1/admin/accounts/import/codex-session"]["body"]
    assert body["group_ids"] == [7]
    assert body["priority"] == 3
    assert body["name"] == "user@example.com"
    content = json.loads(body["content"])
    assert content["accessToken"] == account.access_token
    assert content["refreshToken"] == "rt"


def test_upload_to_sub2api_falls_back_to_direct_account_create(monkeypatch):
    """旧版 SUB2API 若无 codex-session 导入接口，应降级为直接创建账号。"""
    captured: dict[str, dict] = {}

    def fake_request_json(origin, path, **kwargs):
        captured[path] = kwargs
        if path == "/api/v1/auth/login":
            return {"access_token": "admin-token"}
        if path == "/api/v1/admin/groups/all":
            return [{"id": 9, "name": "codex", "platform": "openai"}]
        if path == "/api/v1/admin/accounts/import/codex-session":
            raise mod.Sub2ApiRequestError("not found", status_code=404, path=path)
        if path == "/api/v1/admin/accounts":
            return {"id": 12}
        raise AssertionError(path)

    monkeypatch.setattr(mod, "_request_json", fake_request_json)
    token = _jwt({
        "exp": 1893456000,
        "email": "user@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct_123",
            "chatgpt_user_id": "user_123",
            "organization_id": "org_123",
        },
    })
    account = SimpleNamespace(email="user@example.com", access_token=token, refresh_token="rt", extra={})

    ok, message = mod.upload_to_sub2api(
        account,
        api_url="sub.example",
        email="admin@example.com",
        password="secret",
    )

    assert ok is True
    assert "#12" in message
    body = captured["/api/v1/admin/accounts"]["body"]
    assert body["group_ids"] == [9]
    assert body["credentials"]["access_token"] == token
    assert body["credentials"]["chatgpt_account_id"] == "acct_123"
    assert body["credentials"]["organization_id"] == "org_123"


def test_upload_to_sub2api_rejects_invalid_priority(monkeypatch):
    """优先级必须在发起网络请求前完成校验。"""
    monkeypatch.setattr(mod, "_request_json", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no request")))
    account = SimpleNamespace(email="user@example.com", access_token="token", extra={})

    ok, message = mod.upload_to_sub2api(
        account,
        api_url="https://sub.example",
        email="admin@example.com",
        password="secret",
        account_priority="0",
    )

    assert ok is False
    assert "优先级" in message


def test_upload_to_sub2api_requires_refresh_token(monkeypatch):
    """仅注册号只有 access_token 时不能导入 SUB2API。"""

    def fake_request_json(origin, path, **kwargs):
        if path == "/api/v1/auth/login":
            return {"access_token": "admin-token"}
        if path == "/api/v1/admin/groups/all":
            return [{"id": 7, "name": "codex", "platform": "openai"}]
        raise AssertionError(f"no import request expected: {path}")

    monkeypatch.setattr(mod, "_request_json", fake_request_json)
    account = SimpleNamespace(
        email="user@example.com",
        access_token=_jwt({"exp": 1893456000, "email": "user@example.com"}),
        refresh_token="",
        session={"user": {"email": "user@example.com"}},
        extra={},
    )

    ok, message = mod.upload_to_sub2api(
        account,
        api_url="https://sub.example",
        email="admin@example.com",
        password="secret",
    )

    assert ok is False
    assert "未获取 rt" in message
