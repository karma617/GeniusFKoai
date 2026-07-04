"""K12 会话转换 & workspace 参数解析回归测试。"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest

import platforms.chatgpt.k12_join as k12
from platforms.chatgpt.register import RegistrationEngine, RegistrationResult


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


def test_compact_chatgpt_session_cookies_keeps_only_exchange_required_cookies():
    cookies = (
        "oai-did=device-1; "
        "cf_clearance=" + ("x" * 9000) + "; "
        "__Secure-next-auth.session-token=session-1; "
        "_ga=tracking"
    )

    assert k12.compact_chatgpt_session_cookies(cookies) == (
        "oai-did=device-1; __Secure-next-auth.session-token=session-1"
    )


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
    assert account['credentials']['chatgpt_account_id'] == k12.K12_SUB2API_CHATGPT_ACCOUNT_ID
    assert account['credentials']['plan_type'] == 'k12'
    assert account['extra']['source'] == 'chatgpt_web_session'
    assert info['id_token'] is not None


def test_convert_session_to_sub2api_account_names_k12_workspace():
    at = _make_access_token(int(time.time()) + 3600)
    session = {
        'accessToken': at,
        'user': {'email': 'farrugia73367+8zvf73lv@gmail.com', 'id': 'user-1'},
    }

    info = k12.convert_session_to_sub2api_account(
        session,
        workspace_id='eb6642e8-b4a6-4652-9c18-67099f2781cc',
    )

    assert info['name'] == 'k12-farrugia73367+8zvf73lv@gmail.com-eb6642e8'
    assert info['sub2api_account']['name'] == 'k12-farrugia73367+8zvf73lv@gmail.com-eb6642e8'
    assert info['sub2api_account']['extra']['name'] == 'k12-farrugia73367+8zvf73lv@gmail.com-eb6642e8'
    assert info['sub2api_account']['extra']['workspace_id'] == 'eb6642e8-b4a6-4652-9c18-67099f2781cc'


def test_convert_session_prefers_access_token_chatgpt_account_id():
    at = _make_access_token(int(time.time()) + 3600)
    session = {
        'accessToken': at,
        'account': {'id': 'auth0|login-subject'},
        'user': {'email': 'tester@example.com', 'id': 'auth0|login-subject'},
        'chatgpt_account_id': 'auth0|login-subject',
    }

    info = k12.convert_session_to_sub2api_account(session)

    assert info['account_id'] == 'abc123'
    assert info['user_id'] == 'user-1'
    assert info['sub2api_account']['credentials']['chatgpt_account_id'] == k12.K12_SUB2API_CHATGPT_ACCOUNT_ID
    assert info['sub2api_account']['credentials']['chatgpt_user_id'] == 'user-1'


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
    assert captured["body"]["credentials"]["chatgpt_account_id"] == k12.K12_SUB2API_CHATGPT_ACCOUNT_ID
    assert captured["body"]["credentials"]["plan_type"] == "k12"


def test_upload_session_to_sub2api_uses_workspace_name(monkeypatch):
    captured = {}

    monkeypatch.setattr(k12, "login_sub2api", lambda *args, **kwargs: ("https://sub2api.example", "token"))
    monkeypatch.setattr(k12, "get_groups_by_names", lambda *args, **kwargs: [{"id": 12}])

    def fake_request_json(*args, **kwargs):
        captured["body"] = kwargs["body"]
        return {"id": 790}

    monkeypatch.setattr(k12, "_request_json", fake_request_json)

    ok, message = k12.upload_session_to_sub2api(
        {"accessToken": _make_access_token(int(time.time()) + 3600), "user": {"email": "farrugia73367+8zvf73lv@gmail.com"}},
        workspace_id="eb6642e8-b4a6-4652-9c18-67099f2781cc",
        api_url="https://sub2api.example",
        email="admin@example.com",
        password="password",
        group_name="codex",
        log=lambda _message: None,
    )

    assert ok is True
    assert message == "SUB2API 已创建账号 #790"
    assert captured["body"]["name"] == "k12-farrugia73367+8zvf73lv@gmail.com-eb6642e8"
    assert captured["body"]["credentials"]["chatgpt_account_id"] == k12.K12_SUB2API_CHATGPT_ACCOUNT_ID


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


def test_exchange_workspace_session_sends_compact_cookie_header(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 431
        text = ""

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(k12.cffi_requests, "request", fake_request)
    monkeypatch.setattr(k12.time, "sleep", lambda _seconds: None)

    k12.exchange_workspace_session(
        cookies=(
            "oai-did=device-1; "
            "cf_clearance=" + ("x" * 9000) + "; "
            "__Secure-next-auth.session-token=session-1; "
            "_ga=tracking"
        ),
        workspace_id="k12-workspace",
        log=lambda _message: None,
        max_retries=0,
    )

    assert calls
    sent_cookie = calls[0][2]["headers"]["cookie"]
    assert sent_cookie == "oai-did=device-1; __Secure-next-auth.session-token=session-1"
    assert "cf_clearance" not in sent_cookie


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


def test_protocol_fingerprint_is_stable_per_engine_and_distinct_between_engines():
    service = SimpleNamespace(service_type=SimpleNamespace(value="test"))
    engine_a = RegistrationEngine(email_service=service, callback_logger=lambda _message: None)
    engine_b = RegistrationEngine(email_service=service, callback_logger=lambda _message: None)

    assert engine_a.protocol_fingerprint.device_id != engine_b.protocol_fingerprint.device_id
    assert engine_a.protocol_fingerprint.auth_session_logging_id != engine_b.protocol_fingerprint.auth_session_logging_id

    nav_headers = engine_a._platform_nav_headers(referer="https://auth.openai.com/")
    json_headers = engine_a._platform_json_headers(
        device_id=engine_a.protocol_fingerprint.device_id,
        referer="https://auth.openai.com/log-in",
    )

    assert nav_headers["user-agent"] == engine_a.protocol_fingerprint.user_agent
    assert json_headers["user-agent"] == engine_a.protocol_fingerprint.user_agent
    assert nav_headers["sec-ch-ua"] == engine_a.protocol_fingerprint.sec_ch_ua
    assert json_headers["sec-ch-ua"] == engine_a.protocol_fingerprint.sec_ch_ua
    assert json_headers["oai-device-id"] == engine_a.protocol_fingerprint.device_id


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


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (404, '{"detail":"Not Found"}'),
        (500, '{"detail":"Internal Server Error"}'),
    ],
)
def test_join_unusable_workspace_response_does_not_retry(monkeypatch, status_code, body):
    calls = []

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.text = body

    def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr(k12.cffi_requests, "request", fake_request)
    monkeypatch.setattr(k12.time, "sleep", lambda _seconds: calls.append(("sleep", _seconds)))

    results = k12.send_workspace_join_requests(
        access_token="access-token",
        cookies="cookie=1",
        workspace_ids="workspace-1",
        log=lambda _message: None,
        max_retries=3,
    )

    assert len(calls) == 1
    assert results == [
        {
            "workspace_id": "workspace-1",
            "ok": False,
            "message": f"HTTP {status_code}: {body}",
        }
    ]


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (404, '{"detail":"Not Found"}'),
        (500, '{"detail":"Internal Server Error"}'),
    ],
)
def test_exchange_unusable_workspace_response_does_not_retry(monkeypatch, status_code, body):
    calls = []

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.text = body

    def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr(k12.cffi_requests, "request", fake_request)
    monkeypatch.setattr(k12.time, "sleep", lambda _seconds: calls.append(("sleep", _seconds)))

    result = k12.exchange_workspace_session(
        cookies="__Secure-next-auth.session-token=session",
        workspace_id="workspace-1",
        log=lambda _message: None,
        max_retries=3,
    )

    assert result is None
    assert len(calls) == 1


def test_k12_flow_uploads_every_successful_workspace_after_exchange_mismatch(monkeypatch):
    from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

    import platforms.chatgpt.k12_join as k12_module

    join_calls = []
    exchange_calls = []
    uploaded = []
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
        return {"accessToken": f"k12-access-token-{workspace_id}", "account": {"id": workspace_id}}

    def fake_upload_session_to_sub2api(session, **kwargs):
        uploaded.append((session, kwargs.get("workspace_id")))
        return True, "uploaded"

    monkeypatch.setattr(k12_module, "send_workspace_join_requests", fake_join_requests)
    monkeypatch.setattr(k12_module, "exchange_workspace_session", fake_exchange_workspace_session)
    monkeypatch.setattr(k12_module, "upload_session_to_sub2api", fake_upload_session_to_sub2api)

    worker = ChatGPTProtocolMailboxWorker.__new__(ChatGPTProtocolMailboxWorker)
    worker.log_fn = logs.append
    worker.k12_workspace_ids = "workspace-1,workspace-2,workspace-3"
    worker.proxy_url = None
    worker.remote_upload_enabled = True
    result = SimpleNamespace(
        access_token="registration-token",
        session_token="session-token",
        metadata={
            "session": {"accessToken": "registration-token", "sessionToken": "session-token"},
            "cookies": "oai-did=device-1",
        },
    )

    worker._run_k12_flow(result)

    assert join_calls == ["workspace-1", "workspace-2", "workspace-3"]
    assert exchange_calls == ["workspace-1", "workspace-2", "workspace-3"]
    assert [item[0]["accessToken"] for item in uploaded] == [
        "k12-access-token-workspace-2",
        "k12-access-token-workspace-3",
    ]
    assert [item[1] for item in uploaded] == ["workspace-2", "workspace-3"]
    assert result.access_token == "k12-access-token-workspace-3"
    assert result.metadata["k12_workspace_id"] == "workspace-3"
    assert result.metadata["k12_workspace_ids"] == ["workspace-2", "workspace-3"]
    assert [item["workspace_id"] for item in result.metadata["k12_workspace_sessions"]] == ["workspace-2", "workspace-3"]
    assert any("继续尝试下一个 workspace" in message for message in logs)


def test_k12_flow_default_saves_local_json_instead_of_remote_upload(monkeypatch):
    from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

    import platforms.chatgpt.k12_join as k12_module

    calls = {"upload": 0, "local": 0}
    logs = []

    monkeypatch.setattr(
        k12_module,
        "send_workspace_join_requests",
        lambda **kwargs: [{"workspace_id": kwargs["workspace_ids"], "ok": True, "message": "ok"}],
    )
    monkeypatch.setattr(
        k12_module,
        "exchange_workspace_session",
        lambda **_kwargs: {"accessToken": "k12-access-token", "user": {"email": "k12@example.com"}},
    )
    monkeypatch.setattr(
        k12_module,
        "upload_session_to_sub2api",
        lambda *args, **kwargs: calls.__setitem__("upload", calls["upload"] + 1) or (True, "uploaded"),
    )
    monkeypatch.setattr(
        k12_module,
        "save_session_to_local_upload_jsons",
        lambda *args, **kwargs: calls.__setitem__("local", calls["local"] + 1) or ("data/cpa/a.json", "data/sub2api/a.json"),
    )

    worker = ChatGPTProtocolMailboxWorker.__new__(ChatGPTProtocolMailboxWorker)
    worker.log_fn = logs.append
    worker.k12_workspace_ids = "workspace-1"
    worker.proxy_url = None
    worker.remote_upload_enabled = False
    result = SimpleNamespace(
        access_token="registration-token",
        session_token="session-token",
        metadata={
            "session": {"accessToken": "registration-token", "sessionToken": "session-token"},
            "cookies": "oai-did=device-1",
        },
    )

    worker._run_k12_flow(result)

    assert calls == {"upload": 0, "local": 1}
    assert any("SUB2API JSON 已保存" in message for message in logs)


def test_k12_flow_remote_upload_checkbox_keeps_remote_upload(monkeypatch):
    from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

    import platforms.chatgpt.k12_join as k12_module

    calls = {"upload": 0, "local": 0}

    monkeypatch.setattr(
        k12_module,
        "send_workspace_join_requests",
        lambda **kwargs: [{"workspace_id": kwargs["workspace_ids"], "ok": True, "message": "ok"}],
    )
    monkeypatch.setattr(
        k12_module,
        "exchange_workspace_session",
        lambda **_kwargs: {"accessToken": "k12-access-token", "user": {"email": "k12@example.com"}},
    )
    monkeypatch.setattr(
        k12_module,
        "upload_session_to_sub2api",
        lambda *args, **kwargs: calls.__setitem__("upload", calls["upload"] + 1) or (True, "uploaded"),
    )
    monkeypatch.setattr(
        k12_module,
        "save_session_to_local_upload_jsons",
        lambda *args, **kwargs: calls.__setitem__("local", calls["local"] + 1) or ("data/cpa/a.json", "data/sub2api/a.json"),
    )

    worker = ChatGPTProtocolMailboxWorker.__new__(ChatGPTProtocolMailboxWorker)
    worker.log_fn = lambda _message: None
    worker.k12_workspace_ids = "workspace-1"
    worker.proxy_url = None
    worker.remote_upload_enabled = True
    result = SimpleNamespace(
        access_token="registration-token",
        session_token="session-token",
        metadata={
            "session": {"accessToken": "registration-token", "sessionToken": "session-token"},
            "cookies": "oai-did=device-1",
        },
    )

    worker._run_k12_flow(result)

    assert calls == {"upload": 1, "local": 0}


def test_k12_flow_skips_workspace_when_chatgpt_web_session_missing(monkeypatch):
    from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

    import platforms.chatgpt.k12_join as k12_module

    calls = {"join": 0, "exchange": 0}
    logs = []

    def fake_join_requests(**_kwargs):
        calls["join"] += 1
        return [{"workspace_id": "workspace-1", "ok": True, "message": "ok"}]

    def fake_exchange_workspace_session(**_kwargs):
        calls["exchange"] += 1
        return {"accessToken": "k12-access-token"}

    monkeypatch.setattr(k12_module, "send_workspace_join_requests", fake_join_requests)
    monkeypatch.setattr(k12_module, "exchange_workspace_session", fake_exchange_workspace_session)

    worker = ChatGPTProtocolMailboxWorker.__new__(ChatGPTProtocolMailboxWorker)
    worker.log_fn = logs.append
    worker.k12_workspace_ids = "workspace-1"
    worker.proxy_url = None
    result = SimpleNamespace(
        access_token="registration-token",
        session_token="",
        metadata={"session": {"WARNING_BANNER": "warning"}, "cookies": "oai-did=device-1"},
    )

    worker._run_k12_flow(result)

    assert calls == {"join": 0, "exchange": 0}
    assert result.access_token == "registration-token"
    assert any("缺少 ChatGPT Web session accessToken" in message for message in logs)


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


def test_platform_reference_nextauth_waits_second_email_otp_for_web_session():
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
                return FakeResponse(200, url="https://auth.openai.com/email-verification", text="<html></html>")
            if url.startswith("https://chatgpt.com/api/auth/callback/openai"):
                self.cookies["__Secure-next-auth.session-token"] = "session-token-2"
                return FakeResponse(200, url="https://chatgpt.com/", data={})
            if url == "https://chatgpt.com/api/auth/session":
                return FakeResponse(
                    200,
                    url=url,
                    data={"accessToken": "chatgpt-access-2", "sessionToken": "session-token-2"},
                    text='{"accessToken":"chatgpt-access-2"}',
                )
            return FakeResponse(200, url=url, data={})

        def post(self, url, **kwargs):
            calls["posts"].append((url, kwargs))
            if url.startswith("https://chatgpt.com/api/auth/signin/openai"):
                return FakeResponse(
                    200,
                    url=url,
                    data={"url": "https://auth.openai.com/api/accounts/authorize?state=state_2"},
                )
            if url == "https://auth.openai.com/api/accounts/email-otp/validate":
                assert kwargs["data"] == '{"code":"654321"}'
                return FakeResponse(
                    200,
                    url=url,
                    data={
                        "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=code_2&state=state_2",
                        "page": {
                            "type": "external_url",
                            "payload": {
                                "url": "https://chatgpt.com/api/auth/callback/openai?code=code_2&state=state_2",
                            },
                        },
                    },
                    text='{"continue_url":"https://chatgpt.com/api/auth/callback/openai?code=code_2&state=state_2"}',
                )
            return FakeResponse(500, url=url, data={"unexpected": url})

    class FakeEmailService:
        service_type = SimpleNamespace(value="test")

        def get_verification_code(self, **_kwargs):
            return "654321"

    engine = RegistrationEngine(email_service=FakeEmailService(), callback_logger=lambda _message: None)
    engine.session = FakeSession()
    engine.http_client = SimpleNamespace(session=engine.session, default_headers={})
    engine.email = "user@example.com"
    engine.email_info = {"service_id": "mail-1"}
    engine._device_id = "device-1"

    session_data, cookies = engine._establish_chatgpt_web_session_for_platform_reference()

    assert session_data["accessToken"] == "chatgpt-access-2"
    assert "__Secure-next-auth.session-token=session-token-2" in cookies
    assert any(url == "https://auth.openai.com/api/accounts/email-otp/validate" for url, _kwargs in calls["posts"])


def test_platform_reference_nextauth_rejects_signin_csrf_fallback():
    from platforms.chatgpt.register import RegistrationEngine

    calls = {"posts": []}

    class FakeCookies(dict):
        def get(self, name, default=None, **_kwargs):
            return super().get(name, default)

    class FakeResponse:
        def __init__(self, status_code=200, *, url="", data=None, text=""):
            self.status_code = status_code
            self.url = url
            self._data = data
            self.text = text

        def json(self):
            if self._data is None:
                raise ValueError("no json")
            return self._data

    class FakeSession:
        def __init__(self):
            self.cookies = FakeCookies()

        def get(self, url, **_kwargs):
            if url == "https://chatgpt.com/":
                return FakeResponse(200, url=url, data={})
            if url == "https://chatgpt.com/api/auth/csrf":
                return FakeResponse(200, url=url, data={"csrfToken": "csrf_1"})
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, **kwargs):
            calls["posts"].append((url, kwargs))
            if url.startswith("https://chatgpt.com/api/auth/signin/openai"):
                return FakeResponse(
                    200,
                    url=url,
                    data={"url": "https://chatgpt.com/api/auth/signin?csrf=true"},
                )
            raise AssertionError(f"unexpected POST {url}")

    service = SimpleNamespace(service_type=SimpleNamespace(value="test"))
    engine = RegistrationEngine(email_service=service, callback_logger=lambda _message: None)
    engine.session = FakeSession()
    engine.email = "user@example.com"
    engine._device_id = "device-1"

    assert engine._start_oauth() is False
    assert engine.oauth_start is None
    signin_url, signin_kwargs = calls["posts"][0]
    assert "login_hint=user%40example.com" in signin_url
    assert "screen_hint=login_or_signup" in signin_url
    assert f"ext-oai-did={engine.protocol_fingerprint.device_id}" in signin_url
    assert f"auth_session_logging_id={engine.protocol_fingerprint.auth_session_logging_id}" in signin_url
    assert "callbackUrl=https%3A%2F%2Fchatgpt.com%2F" in signin_kwargs["data"]


def test_platform_reference_existing_k12_login_skips_platform_oauth(monkeypatch):
    from platforms.chatgpt import register as register_module

    calls = {"platform_oauth": 0, "k12_session": 0}
    access_token = _make_access_token(int(time.time()) + 3600)

    class FakeCookies(dict):
        def set(self, name, value, **_kwargs):
            self[name] = value

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = SimpleNamespace(cookies=FakeCookies())
            self.default_headers = {}

    service = SimpleNamespace(service_type=SimpleNamespace(value="test"))
    engine = RegistrationEngine(email_service=service, callback_logger=lambda _message: None)
    engine.email = "user@example.com"
    engine.password = "Secret123!"
    engine.k12_join_enabled = True
    engine.k12_workspace_ids = "workspace-k12"

    def fake_authorize(_client, _device_id):
        engine._platform_authorize_final_url = "https://auth.openai.com/log-in/password"
        return SimpleNamespace(auth_url="https://auth.openai.com/api/accounts/authorize?state=platform", state="state", code_verifier="verifier")

    def fake_prepare_existing(_client, _device_id):
        engine._is_existing_account = True

    def fake_complete_platform_oauth(*_args, **_kwargs):
        calls["platform_oauth"] += 1
        raise AssertionError("existing K12 login must not request Platform OAuth")

    def fake_complete_existing_k12(_client, _device_id, validate_payload):
        calls["k12_session"] += 1
        assert validate_payload["continue_url"] == "https://auth.openai.com/workspace"
        return (
            {"accessToken": access_token, "sessionToken": "session-token-1", "user": {"email": "user@example.com"}},
            "__Secure-next-auth.session-token=session-token-1",
            "workspace-k12",
        )

    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeClient)
    engine._platform_reference_authorize = fake_authorize
    engine._refresh_mailbox_before_ids = lambda: None
    engine._platform_reference_prepare_existing_login_otp = fake_prepare_existing
    engine._wait_platform_reference_register_code = lambda _client: "123456"
    engine._platform_reference_validate_otp = lambda _client, _device_id, _code: {
        "continue_url": "https://auth.openai.com/workspace",
        "page": {"type": "workspace"},
    }
    engine._platform_reference_complete_existing_k12_session = fake_complete_existing_k12
    engine._complete_platform_oauth = fake_complete_platform_oauth

    result = engine._run_platform_reference_register(RegistrationResult(success=False, logs=[]))

    assert result.success is True
    assert result.source == "login"
    assert result.access_token == access_token
    assert result.session_token == "session-token-1"
    assert result.metadata["chatgpt_session_source"] == "existing_login_workspace_select"
    assert result.metadata["k12_workspace_id"] == "workspace-k12"
    assert calls == {"platform_oauth": 0, "k12_session": 1}


def test_existing_k12_workspace_payload_detection_distinguishes_callback_login():
    service = SimpleNamespace(service_type=SimpleNamespace(value="test"))
    engine = RegistrationEngine(email_service=service, callback_logger=lambda _message: None)
    engine.k12_workspace_ids = "workspace-k12"

    workspace_payload = {
        "continue_url": "https://auth.openai.com/workspace",
        "page": {"type": "workspace"},
        "oai-client-auth-session": {"workspaces": [{"id": "workspace-k12", "kind": "organization"}]},
    }
    callback_payload = {
        "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ac_1&state=state-1",
        "page": {
            "type": "external_url",
            "payload": {"url": "https://chatgpt.com/api/auth/callback/openai?code=ac_1&state=state-1"},
        },
        "oai-client-auth-session": {"email_verified": True},
    }

    assert engine._is_existing_k12_workspace_payload(workspace_payload) is True
    assert engine._is_existing_k12_workspace_payload(callback_payload) is False
    assert engine._chatgpt_callback_url_from_payload(callback_payload).startswith(
        "https://chatgpt.com/api/auth/callback/openai?code="
    )


def test_platform_reference_existing_callback_login_uses_chatgpt_session_not_workspace(monkeypatch):
    from platforms.chatgpt import register as register_module

    calls = {"platform_oauth": 0, "k12_session": 0, "callback_session": 0}
    access_token = _make_access_token(int(time.time()) + 3600)

    class FakeCookies(dict):
        def set(self, name, value, **_kwargs):
            self[name] = value

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = SimpleNamespace(cookies=FakeCookies())
            self.default_headers = {}

    service = SimpleNamespace(service_type=SimpleNamespace(value="test"))
    engine = RegistrationEngine(email_service=service, callback_logger=lambda _message: None)
    engine.email = "user@example.com"
    engine.password = "Secret123!"
    engine.k12_join_enabled = True
    engine.k12_workspace_ids = "workspace-k12"

    validate_payload = {
        "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ac_1&state=state-1",
        "page": {
            "type": "external_url",
            "payload": {"url": "https://chatgpt.com/api/auth/callback/openai?code=ac_1&state=state-1"},
        },
        "oai-client-auth-session": {"email_verified": True},
    }

    def fake_authorize(_client, _device_id):
        engine._platform_authorize_final_url = "https://auth.openai.com/log-in/password"
        return SimpleNamespace(auth_url="https://auth.openai.com/api/accounts/authorize?state=platform", state="state", code_verifier="verifier")

    def fake_prepare_existing(_client, _device_id):
        engine._is_existing_account = True

    def fake_complete_existing_k12(*_args, **_kwargs):
        calls["k12_session"] += 1
        raise AssertionError("external_url callback must not be treated as existing K12 workspace login")

    def fake_complete_callback(_client, payload):
        calls["callback_session"] += 1
        assert payload is validate_payload
        return (
            {"accessToken": access_token, "sessionToken": "session-token-1", "user": {"email": "user@example.com"}},
            "__Secure-next-auth.session-token=session-token-1",
        )

    def fake_complete_platform_oauth(*_args, **_kwargs):
        calls["platform_oauth"] += 1
        raise AssertionError("existing ChatGPT callback login must not request Platform OAuth")

    monkeypatch.setattr(register_module, "OpenAIHTTPClient", FakeClient)
    engine._platform_reference_authorize = fake_authorize
    engine._refresh_mailbox_before_ids = lambda: None
    engine._platform_reference_prepare_existing_login_otp = fake_prepare_existing
    engine._wait_platform_reference_register_code = lambda _client: "123456"
    engine._platform_reference_validate_otp = lambda _client, _device_id, _code: validate_payload
    engine._platform_reference_complete_existing_k12_session = fake_complete_existing_k12
    engine._platform_reference_complete_existing_callback_session = fake_complete_callback
    engine._complete_platform_oauth = fake_complete_platform_oauth

    result = engine._run_platform_reference_register(RegistrationResult(success=False, logs=[]))

    assert result.success is True
    assert result.source == "login"
    assert result.access_token == access_token
    assert result.session_token == "session-token-1"
    assert result.metadata["chatgpt_session_source"] == "existing_login_callback"
    assert result.metadata["k12_workspace_id"] == ""
    assert calls == {"platform_oauth": 0, "k12_session": 0, "callback_session": 1}
