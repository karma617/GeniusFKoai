"""K12 会话转换 & workspace 参数解析回归测试。"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import platforms.chatgpt.k12_join as k12
from platforms.chatgpt.register import RegistrationEngine


def _make_access_token(exp_seconds: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b'=').decode()
    payload = {
        "exp": exp_seconds,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "abc123",
            "chatgpt_user_id": "user-1",
            "chatgpt_plan_type": "free",
        },
        "https://api.openai.com/profile": {"email": "tester@example.com"},
        "email": "tester@example.com",
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b'=').decode()
    return f'{header}.{body}.sig'


def test_parse_workspace_ids_supports_comma_and_newline():
    raw = 'ca0e29ed-a54c-42d9-a50b-2ba5e065296d, aaabbb\n  cccc  \n\n'
    assert k12.parse_workspace_ids(raw) == [
        'ca0e29ed-a54c-42d9-a50b-2ba5e065296d',
        'aaabbb',
        'cccc',
    ]


def test_ensure_chatgpt_session_cookie_appends_missing_token():
    cookies = "oai-did=device-1"
    assert k12.ensure_chatgpt_session_cookie(cookies, "session-1") == (
        "oai-did=device-1; __Secure-next-auth.session-token=session-1"
    )


def test_ensure_chatgpt_session_cookie_keeps_existing_token():
    cookies = "__Secure-next-auth.session-token=old; oai-did=device-1"
    assert k12.ensure_chatgpt_session_cookie(cookies, "new") == cookies


def test_convert_session_to_sub2api_account_shape():
    at = _make_access_token(int(time.time()) + 3600)
    session = {
        'accessToken': at,
        'user': {'email': 'tester@example.com', 'id': 'user-1'},
        'expires': '2030-01-01T00:00:00.000Z',
    }
    info = k12.convert_session_to_sub2api_account(session, source_name='k12-test')
    assert info['access_token'] == at
    assert info['email'] == 'tester@example.com'
    assert info['account_id'] == 'abc123'
    assert info['user_id'] == 'user-1'
    account = info['sub2api_account']
    assert account['platform'] == 'openai'
    assert account['type'] == 'oauth'
    assert account['credentials']['chatgpt_account_id'] == 'abc123'
    assert account['credentials']['plan_type'] == 'k12'
    assert account['extra']['source'] == 'chatgpt_web_session'
    assert info['id_token'] is not None


def test_convert_session_requires_access_token():
    import pytest

    with pytest.raises(ValueError):
        k12.convert_session_to_sub2api_account({'user': {'email': 'x@y.com'}})


def test_upload_session_to_sub2api_retries_request_error(monkeypatch):
    calls = []
    sleeps = []

    monkeypatch.setattr(k12, "login_sub2api", lambda *args, **kwargs: ("https://sub2api.example", "token"))
    monkeypatch.setattr(k12, "get_groups_by_names", lambda *args, **kwargs: [{"id": 12}])
    monkeypatch.setattr(k12.time, "sleep", sleeps.append)

    def fake_request_json(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) <= 3:
            raise k12.Sub2ApiRequestError("SUB2API 请求异常：TLS connect error", path="/api/v1/admin/accounts")
        return {"id": 456}

    monkeypatch.setattr(k12, "_request_json", fake_request_json)

    ok, message = k12.upload_session_to_sub2api(
        {"accessToken": _make_access_token(int(time.time()) + 3600), "user": {"email": "tester@example.com"}},
        api_url="https://sub2api.example",
        email="admin@example.com",
        password="password",
        group_name="codex",
        log=lambda _message: None,
    )

    assert ok is True
    assert message == "SUB2API 已创建账号 #456"
    assert len(calls) == 4
    assert sleeps == [k12.K12_SUB2API_UPLOAD_RETRY_DELAY_SECONDS] * 3


def test_upload_session_to_sub2api_k12_payload_does_not_set_rate_multiplier(monkeypatch):
    captured = {}

    monkeypatch.setattr(k12, "login_sub2api", lambda *args, **kwargs: ("https://sub2api.example", "token"))
    monkeypatch.setattr(k12, "get_groups_by_names", lambda *args, **kwargs: [{"id": 12}])

    def fake_request_json(*args, **kwargs):
        captured["body"] = kwargs["body"]
        return {"id": 789}

    monkeypatch.setattr(k12, "_request_json", fake_request_json)

    ok, message = k12.upload_session_to_sub2api(
        {"accessToken": _make_access_token(int(time.time()) + 3600), "user": {"email": "tester@example.com"}},
        api_url="https://sub2api.example",
        email="admin@example.com",
        password="password",
        group_name="codex",
        log=lambda _message: None,
    )

    assert ok is True
    assert message == "SUB2API 已创建账号 #789"
    assert "rate_multiplier" not in captured["body"]
    assert captured["body"]["credentials"]["plan_type"] == "k12"


def test_validate_workspace_exchange_session_accepts_chatgpt_account_id_with_mismatched_account_id():
    session = {
        "accessToken": "access-token",
        "account": {"id": "personal-workspace"},
        "chatgpt_account_id": "chatgpt-account-1",
    }

    ok, reason = k12.validate_workspace_exchange_session(session, "k12-workspace")

    assert ok is True
    assert "chatgpt_account_id=chatgpt-account-1" in reason


def test_validate_workspace_exchange_session_rejects_missing_chatgpt_account_id():
    session = {
        "accessToken": "access-token",
        "account": {"id": "personal-workspace"},
    }

    ok, reason = k12.validate_workspace_exchange_session(session, "k12-workspace")

    assert ok is False
    assert "缺少 chatgpt_account_id" in reason


def test_exchange_workspace_session_accepts_chatgpt_account_id_with_mismatched_account_id(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = json.dumps(
            {
                "accessToken": "access-token",
                "account": {"id": "personal-workspace"},
                "chatgpt_account_id": "chatgpt-account-1",
            }
        )

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(k12.cffi_requests, "request", fake_request)
    monkeypatch.setattr(k12.time, "sleep", lambda _seconds: None)
    logs = []

    result = k12.exchange_workspace_session(
        cookies="__Secure-next-auth.session-token=session",
        workspace_id="k12-workspace",
        log=logs.append,
        max_retries=0,
    )

    assert result is not None
    assert calls
    assert result["chatgpt_account_id"] == "chatgpt-account-1"
    assert any("已获取 ChatGPT account 标识" in message for message in logs)


def test_exchange_workspace_session_accepts_chatgpt_account_id_from_access_token_claims(monkeypatch):
    access_token = _make_access_token(int(time.time()) + 3600)

    class FakeResponse:
        status_code = 200
        text = json.dumps(
            {
                "accessToken": access_token,
                "account": {"id": "k12-workspace"},
            }
        )

    monkeypatch.setattr(k12.cffi_requests, "request", lambda *args, **kwargs: FakeResponse())

    result = k12.exchange_workspace_session(
        cookies="__Secure-next-auth.session-token=session",
        workspace_id="k12-workspace",
        log=lambda _message: None,
        max_retries=0,
    )

    assert result is not None
    assert result["accessToken"] == access_token


def test_join_200_success_false_is_not_success(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = '{"success":false,"error":"not joined"}'

    monkeypatch.setattr(k12.cffi_requests, "request", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(k12.time, "sleep", lambda _seconds: None)

    chosen, results = k12.send_workspace_join_and_pick_first_success(
        access_token="access-token",
        cookies="cookie=1",
        workspace_ids="workspace-1",
        log=lambda _message: None,
        max_retries=0,
    )

    assert chosen == ""
    assert results == [
        {
            "workspace_id": "workspace-1",
            "ok": False,
            "message": 'HTTP 200: {"success":false,"error":"not joined"}',
        }
    ]


def test_k12_flow_continues_next_workspace_after_exchange_mismatch(monkeypatch):
    from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

    import platforms.chatgpt.k12_join as k12_module

    join_calls = []
    exchange_calls = []
    uploaded = {}
    logs = []

    def fake_join_requests(**kwargs):
        join_calls.append(kwargs["workspace_ids"])
        return [
            {"workspace_id": kwargs["workspace_ids"], "ok": True, "message": "ok"},
        ]

    def fake_exchange_workspace_session(*, workspace_id, **_kwargs):
        exchange_calls.append(workspace_id)
        if workspace_id == "workspace-1":
            return None
        return {"accessToken": "k12-access-token", "account": {"id": "workspace-2"}}

    def fake_upload_session_to_sub2api(session, **_kwargs):
        uploaded["session"] = session
        return True, "uploaded"

    monkeypatch.setattr(k12_module, "send_workspace_join_requests", fake_join_requests)
    monkeypatch.setattr(k12_module, "exchange_workspace_session", fake_exchange_workspace_session)
    monkeypatch.setattr(k12_module, "upload_session_to_sub2api", fake_upload_session_to_sub2api)

    worker = ChatGPTProtocolMailboxWorker.__new__(ChatGPTProtocolMailboxWorker)
    worker.log_fn = logs.append
    worker.k12_workspace_ids = "workspace-1,workspace-2,workspace-3"
    worker.proxy_url = None
    result = SimpleNamespace(
        access_token="registration-token",
        session_token="session-token",
        metadata={
            "session": {"accessToken": "registration-token", "sessionToken": "session-token"},
            "cookies": "oai-did=device-1",
        },
    )

    worker._run_k12_flow(result)

    assert join_calls == ["workspace-1", "workspace-2"]
    assert exchange_calls == ["workspace-1", "workspace-2"]
    assert uploaded["session"]["accessToken"] == "k12-access-token"
    assert result.access_token == "k12-access-token"
    assert result.metadata["k12_workspace_id"] == "workspace-2"
    assert any("继续尝试下一个 workspace" in message for message in logs)


def test_platform_reference_nextauth_resolves_choose_account_via_workspace_select():
    calls = {"gets": [], "posts": []}

    class FakeCookies(dict):
        def set(self, name, value, **_kwargs):
            self[name] = value

    class FakeResponse:
        def __init__(self, status_code=200, *, url="", data=None, text="", headers=None):
            self.status_code = status_code
            self.url = url
            self._data = data
            self.text = text
            self.headers = headers or {}

        def json(self):
            if self._data is None:
                raise ValueError("no json")
            return self._data

    class FakeSession:
        def __init__(self):
            self.cookies = FakeCookies()

        def get(self, url, **kwargs):
            calls["gets"].append((url, kwargs))
            if url == "https://chatgpt.com/":
                return FakeResponse(200, url=url, data={})
            if url == "https://chatgpt.com/api/auth/csrf":
                return FakeResponse(200, url=url, data={"csrfToken": "csrf_1"})
            if url.startswith("https://auth.openai.com/api/accounts/authorize"):
                assert "login_hint=user%40example.com" in url
                return FakeResponse(200, url="https://auth.openai.com/choose-an-account", text="<html></html>")
            if url == "https://auth.openai.com/api/accounts/client_auth_session_dump":
                return FakeResponse(
                    200,
                    url=url,
                    data={"workspaces": [{"id": "workspace-1"}]},
                    text='{"workspaces":[{"id":"workspace-1"}]}',
                )
            if url.startswith("https://chatgpt.com/api/auth/callback/openai"):
                self.cookies["__Secure-next-auth.session-token"] = "session-token-1"
                return FakeResponse(200, url="https://chatgpt.com/", data={})
            if url == "https://chatgpt.com/api/auth/session":
                return FakeResponse(
                    200,
                    url=url,
                    data={"accessToken": "chatgpt-access", "sessionToken": "session-token-1"},
                    text='{"accessToken":"chatgpt-access"}',
                )
            return FakeResponse(200, url=url, data={})

        def post(self, url, **kwargs):
            calls["posts"].append((url, kwargs))
            if url.startswith("https://chatgpt.com/api/auth/signin/openai"):
                return FakeResponse(
                    200,
                    url=url,
                    data={"url": "https://auth.openai.com/api/accounts/authorize?state=state_1"},
                )
            if url == "https://auth.openai.com/api/accounts/workspace/select":
                return FakeResponse(
                    200,
                    url=url,
                    data={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=code_1&state=state_1"},
                    text='{"continue_url":"https://chatgpt.com/api/auth/callback/openai?code=code_1&state=state_1"}',
                )
            return FakeResponse(500, url=url, data={"unexpected": url})

    service = SimpleNamespace(service_type=SimpleNamespace(value="test"))
    engine = RegistrationEngine(email_service=service, callback_logger=lambda _message: None)
    engine.session = FakeSession()
    engine.email = "user@example.com"
    engine._device_id = "device-1"

    session_data, cookies = engine._establish_chatgpt_web_session_for_platform_reference()

    assert session_data["accessToken"] == "chatgpt-access"
    assert "__Secure-next-auth.session-token=session-token-1" in cookies
    assert any(url == "https://auth.openai.com/api/accounts/workspace/select" for url, _kwargs in calls["posts"])
