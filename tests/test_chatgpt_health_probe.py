"""ChatGPT 批量测活轻量路径 focused tests。

- 已有 access_token 时，每次尝试只请求 GET https://chatgpt.com/backend-api/me。
- 缺少 access_token 时保留 GET /api/auth/session 刷新，成功后再请求一次 /me。
- 禁止 cdn-cgi/trace / 订阅 me / wham/usage / codex/responses。
- 任务层 _run_single_chatgpt_health_check 必须走轻量函数，
  完整探测 fetch_chatgpt_account_state 不得被批量测活触发。
- 数据库使用 tmp_path 隔离 sqlite 引擎（SQLModel.metadata.create_all）。
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from sqlmodel import Session, SQLModel

import application.tasks as tasks_module
import core.config_store as config_store_module
import platforms.chatgpt.switch as switch_module
from core.account_graph import load_account_graphs
from core.db import (
    AccountCredentialModel,
    AccountModel,
    create_configured_engine,
)

HEALTH_ME_URL = "https://chatgpt.com/backend-api/me"
FORBIDDEN_URL_PARTS = ("cdn-cgi/trace", "wham/usage", "codex/responses")


class _Logger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def log(self, message: str, level: str = "info", detail=None) -> None:
        self.entries.append((message, level))


class _Response:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _CurlStub:
    """替换 platforms.chatgpt.switch.curl_requests，记录全部出站请求。

    任何非 GET {me} 的 URL 一律断言失败，用于证明 trace / 订阅 me /
    wham / codex 等请求都不会从轻量 profile 请求发出。
    """

    def __init__(self, responder):
        self.calls: list[tuple[str, str]] = []
        self.kwargs: list[dict] = []
        self._responder = responder

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        self.kwargs.append(kwargs)
        return self._responder("GET", url, kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        self.kwargs.append(kwargs)
        return self._responder("POST", url, kwargs)

    def Session(self, *args, **kwargs):  # 完整探测才会创建 Session
        raise AssertionError("轻量测活不应创建 curl Session")


def _guard_url(method: str, url: str) -> None:
    assert (method, url) == ("GET", HEALTH_ME_URL), f"测活只允许 GET {HEALTH_ME_URL}，实际请求 {method} {url}"
    for part in FORBIDDEN_URL_PARTS:
        assert part not in url, f"测活禁止请求 {part}"


@pytest.fixture()
def curl_stub(monkeypatch):
    """安装 curl_requests 替身；responder(method, url, kwargs) -> _Response。"""

    def _install(responder):
        def _guarded(method, url, kwargs):
            _guard_url(method, url)
            return responder(method, url, kwargs)

        stub = _CurlStub(_guarded)
        monkeypatch.setattr(switch_module, "curl_requests", stub)
        monkeypatch.setattr(
            switch_module,
            "_build_proxy_request_kwargs",
            lambda proxy=None: ({} if not proxy else {"proxies": {"http": proxy, "https": proxy}}),
        )
        _install.calls = stub.calls
        _install.kwargs = stub.kwargs
        return stub

    _install.calls = []
    _install.kwargs = []
    return _install


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    engine = create_configured_engine(
        "sqlite:///" + str(tmp_path / "health_probe.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(tasks_module, "engine", engine)
    monkeypatch.setattr(config_store_module, "engine", engine)
    return engine


class _PoolStub:
    def __init__(self):
        self.successes: list[str] = []
        self.fails: list[str] = []

    def report_success(self, proxy):
        self.successes.append(proxy)

    def report_fail(self, proxy):
        self.fails.append(proxy)


@pytest.fixture()
def task_layer_stubs(monkeypatch):
    """隔离任务层外部依赖：代理解析固定返回测试代理，代理池只记录调用。"""
    monkeypatch.setattr(tasks_module, "CHATGPT_HEALTH_CHECK_MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        "platforms.chatgpt.plugin._resolve_action_proxy",
        lambda *args, **kwargs: "http://proxy.test:8080",
    )
    pool = _PoolStub()
    monkeypatch.setattr("platforms.chatgpt.plugin.proxy_pool", pool)
    return pool


@pytest.fixture()
def no_full_probe(monkeypatch):
    monkeypatch.setattr(
        switch_module,
        "fetch_chatgpt_account_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("批量测活不应触发完整探测 fetch_chatgpt_account_state")
        ),
    )


def _create_account(engine, *, email: str, credentials: list[tuple[str, str]] | None = None) -> int:
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email=email, password="pw")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id)
        for key, value in credentials or []:
            session.add(
                AccountCredentialModel(
                    account_id=account_id,
                    scope="platform",
                    provider_name="chatgpt",
                    credential_type="token",
                    key=key,
                    value=value,
                )
            )
        session.commit()
    return account_id


def _overview_summary(engine, account_id: int) -> dict:
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id]).get(account_id, {})
    return dict(graph.get("overview") or {})


# ---------------------------------------------------------------------------
# 轻量探测函数（switch 层）
# ---------------------------------------------------------------------------


def test_probe_success_sends_single_me_request(curl_stub):
    curl_stub(lambda method, url, kwargs: _Response(200, {"id": "u-1", "email": "a@example.com"}))

    state = switch_module.fetch_chatgpt_health_probe(access_token="tok-abc", proxy="http://p:1")

    assert len(curl_stub.calls) == 1, curl_stub.calls
    method, url = curl_stub.calls[0]
    assert (method, url) == ("GET", HEALTH_ME_URL)
    request_kwargs = curl_stub.kwargs[0]
    assert request_kwargs["headers"]["authorization"] == "Bearer tok-abc"
    assert request_kwargs["timeout"] == 20
    assert request_kwargs["proxies"] == {"http": "http://p:1", "https": "http://p:1"}
    assert state["valid"] is True
    assert state["remote_user"]["email"] == "a@example.com"


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500])
def test_probe_non200_sends_single_me_request_and_marks_invalid(curl_stub, status_code):
    curl_stub(lambda method, url, kwargs: _Response(status_code, text="denied"))

    state = switch_module.fetch_chatgpt_health_probe(access_token="tok-abc")

    assert len(curl_stub.calls) == 1, curl_stub.calls
    assert state["valid"] is False
    assert state["profile_error"]["status_code"] == status_code
    assert "denied" in state["profile_error"]["body"]


def test_probe_network_error_single_request_and_retryable_text(curl_stub):
    def _boom(method, url, kwargs):
        raise RuntimeError("curl: (28) Operation timed out after 20000 milliseconds")

    curl_stub(_boom)

    state = switch_module.fetch_chatgpt_health_probe(access_token="tok-abc")

    assert len(curl_stub.calls) == 1, curl_stub.calls
    assert state["valid"] is False
    error_text = tasks_module._chatgpt_health_error_text(state["profile_error"])
    assert "timed out" in error_text
    # 网络异常文本必须保留可重试语义，任务层重试循环据此再次尝试。
    assert tasks_module._is_chatgpt_health_check_retryable_error(0, error_text) is True


def test_probe_missing_all_tokens_sends_no_request(curl_stub):
    curl_stub(lambda method, url, kwargs: (_ for _ in ()).throw(AssertionError("不应发出任何请求")))

    state = switch_module.fetch_chatgpt_health_probe(access_token="")

    assert curl_stub.calls == []
    assert state["valid"] is False
    assert "缺少 access_token/session_token" in str(state["profile_error"])


def test_probe_refreshes_session_then_requests_me(curl_stub, monkeypatch):
    refresh_calls = []

    class _RefreshManager:
        def __init__(self, proxy_url=None):
            refresh_calls.append(("init", proxy_url))

        def refresh_by_session_token(self, token):
            refresh_calls.append(("refresh", token))
            return SimpleNamespace(success=True, access_token="refreshed-token", error_message="")

    monkeypatch.setattr("platforms.chatgpt.token_refresh.TokenRefreshManager", _RefreshManager)
    curl_stub(lambda method, url, kwargs: _Response(200, {"id": "u-2", "email": "s@example.com"}))

    state = switch_module.fetch_chatgpt_health_probe(
        session_token="session-token",
        proxy="http://p:1",
    )

    assert refresh_calls == [("init", "http://p:1"), ("refresh", "session-token")]
    assert curl_stub.calls == [("GET", HEALTH_ME_URL)]
    assert curl_stub.kwargs[0]["headers"]["authorization"] == "Bearer refreshed-token"
    assert state["valid"] is True


def test_probe_refresh_failure_does_not_request_me(curl_stub, monkeypatch):
    class _RefreshManager:
        def __init__(self, proxy_url=None):
            pass

        def refresh_by_session_token(self, token):
            return SimpleNamespace(success=False, access_token="", error_message="Session token 刷新失败: HTTP 401")

    monkeypatch.setattr("platforms.chatgpt.token_refresh.TokenRefreshManager", _RefreshManager)
    curl_stub(lambda method, url, kwargs: (_ for _ in ()).throw(AssertionError("刷新失败后不应请求 /me")))

    state = switch_module.fetch_chatgpt_health_probe(session_token="bad-session")

    assert curl_stub.calls == []
    assert state["valid"] is False
    assert "HTTP 401" in state["token_refresh_error"]


# ---------------------------------------------------------------------------
# 任务层（application/tasks.py）
# ---------------------------------------------------------------------------


def test_health_runner_success_single_request_and_persist(curl_stub, isolated_db, task_layer_stubs, no_full_probe):
    account_id = _create_account(
        isolated_db,
        email="ok@example.com",
        credentials=[("access_token", "tok-ok")],
    )
    curl_stub(lambda method, url, kwargs: _Response(200, {"id": "u-1", "email": "ok@example.com"}))
    logger = _Logger()

    result = tasks_module._run_single_chatgpt_health_check(account_id, logger)

    assert len(curl_stub.calls) == 1, curl_stub.calls
    assert curl_stub.calls[0] == ("GET", HEALTH_ME_URL)
    assert curl_stub.kwargs[0]["proxies"]["https"] == "http://proxy.test:8080"
    assert result["valid"] is True
    assert result["status_code"] == 200
    assert result["account_state"]["remote_email"] == "ok@example.com"
    assert task_layer_stubs.successes == ["http://proxy.test:8080"]
    assert task_layer_stubs.fails == []
    assert any("测活存活" in message for message, _level in logger.entries)

    summary = _overview_summary(isolated_db, account_id)
    assert summary["valid"] is True
    assert summary["validity_status"] == "valid"
    assert summary["health_status_code"] == 200
    assert summary["checked_at"]


def test_health_runner_401_single_request_relogin_required(curl_stub, isolated_db, task_layer_stubs, no_full_probe):
    account_id = _create_account(
        isolated_db,
        email="expired@example.com",
        credentials=[("access_token", "tok-expired")],
    )
    curl_stub(lambda method, url, kwargs: _Response(401, text="Unauthorized"))
    logger = _Logger()

    result = tasks_module._run_single_chatgpt_health_check(account_id, logger)

    assert len(curl_stub.calls) == 1, curl_stub.calls
    assert result["valid"] is False
    assert result["status_code"] == 401
    assert result.get("relogin_required") is True
    assert result.get("transient") is True
    assert task_layer_stubs.fails == ["http://proxy.test:8080"]
    assert any("需要重登验证" in message for message, _level in logger.entries)

    summary = _overview_summary(isolated_db, account_id)
    assert summary["validity_status"] == "unknown"


def test_health_runner_session_refresh_then_single_me(curl_stub, isolated_db, task_layer_stubs, no_full_probe, monkeypatch):
    refresh_calls = []

    class _RefreshManager:
        def __init__(self, proxy_url=None):
            refresh_calls.append(("init", proxy_url))

        def refresh_by_session_token(self, token):
            refresh_calls.append(("refresh", token))
            return SimpleNamespace(success=True, access_token="refreshed-token", error_message="")

    monkeypatch.setattr("platforms.chatgpt.token_refresh.TokenRefreshManager", _RefreshManager)
    account_id = _create_account(
        isolated_db,
        email="session@example.com",
        credentials=[("session_token", "sess-xyz")],
    )
    curl_stub(lambda method, url, kwargs: _Response(200, {"id": "u-session", "email": "session@example.com"}))
    logger = _Logger()

    result = tasks_module._run_single_chatgpt_health_check(account_id, logger)

    assert refresh_calls == [("init", "http://proxy.test:8080"), ("refresh", "sess-xyz")]
    assert curl_stub.calls == [("GET", HEALTH_ME_URL)]
    assert result["valid"] is True


def test_health_runner_without_credentials_zero_requests(curl_stub, isolated_db, task_layer_stubs, no_full_probe):
    curl_stub(lambda method, url, kwargs: (_ for _ in ()).throw(AssertionError("无凭据不应发请求")))
    account_id = _create_account(isolated_db, email="notoken@example.com")
    logger = _Logger()

    result = tasks_module._run_single_chatgpt_health_check(account_id, logger)

    assert curl_stub.calls == []
    assert result["valid"] is False
    assert result["status_code"] == 0
    assert "缺少 access_token/session_token/cookies" in result["error"]
    assert any("缺少状态查询凭据" in message for message, _level in logger.entries)

    summary = _overview_summary(isolated_db, account_id)
    assert summary["validity_status"] == "invalid"


def test_health_runner_network_error_retries_then_succeeds(curl_stub, isolated_db, task_layer_stubs, no_full_probe):
    account_id = _create_account(
        isolated_db,
        email="flaky@example.com",
        credentials=[("access_token", "tok-flaky")],
    )
    attempts = {"n": 0}

    def _flaky(method, url, kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("curl: (56) Recv failure: Connection was reset")
        return _Response(200, {"id": "u-2", "email": "flaky@example.com"})

    curl_stub(_flaky)
    logger = _Logger()

    result = tasks_module._run_single_chatgpt_health_check(account_id, logger)

    assert attempts["n"] == 2, curl_stub.calls
    assert all(url == HEALTH_ME_URL for _method, url in curl_stub.calls)
    assert result["valid"] is True
    assert any("测活网络错误第 1/4 次" in message for message, _level in logger.entries)


def test_execute_health_check_task_uses_light_probe_for_all_accounts(curl_stub, isolated_db, task_layer_stubs, no_full_probe):
    id_ok = _create_account(isolated_db, email="t1@example.com", credentials=[("access_token", "tok-1")])
    id_bad = _create_account(isolated_db, email="t2@example.com", credentials=[("access_token", "tok-2")])

    responses = {id_ok: _Response(200, {"id": "u-1", "email": "t1@example.com"})}

    def _responder(method, url, kwargs):
        token = str(kwargs.get("headers", {}).get("authorization") or "")
        if token.endswith("tok-2"):
            return _Response(401, text="Unauthorized")
        return responses[id_ok]

    curl_stub(_responder)

    created = tasks_module.create_account_health_check_task(platform="chatgpt", ids=[id_ok, id_bad])
    logger = tasks_module.TaskLogger(created["id"])
    tasks_module._execute_account_health_check_task({"platform": "chatgpt", "ids": [id_ok, id_bad]}, logger)

    # 两个账号各只发一次 GET /backend-api/me，且无任何禁止 URL。
    assert len(curl_stub.calls) == 2, curl_stub.calls
    assert all(call == ("GET", HEALTH_ME_URL) for call in curl_stub.calls)

    summary_ok = _overview_summary(isolated_db, id_ok)
    summary_bad = _overview_summary(isolated_db, id_bad)
    assert summary_ok["validity_status"] == "valid"
    assert summary_bad["validity_status"] == "unknown"
