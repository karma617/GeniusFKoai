"""get_rt 协议登录 state 恢复测试（无网络，全部使用假 session/engine）。"""

from __future__ import annotations

import json

import pytest

from platforms.chatgpt import protocol_get_rt


AUTH_URL = "https://auth.openai.com/authorize?client_id=test-client"
LOGIN_PAGE_HTML = "<html><body>OpenAI log in</body></html>"
CF_CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async>'
    "</script></body></html>"
)
CONTINUE_CALLBACK_URL = "https://auth.openai.com/authorize/resume?code=auth-code-1&state=state-abc"


class _Resp:
    def __init__(self, *, status_code=200, text="", json_data=None, headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json_data = json_data
        self.url = url

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


def _login_page():
    return _Resp(status_code=200, text=LOGIN_PAGE_HTML, url=AUTH_URL)


def _cf_challenge(*, status_code=403):
    return _Resp(
        status_code=status_code,
        text=CF_CHALLENGE_HTML,
        headers={"content-type": "text/html; charset=UTF-8"},
        url=AUTH_URL,
    )


def _continue_success():
    return _Resp(
        status_code=200,
        json_data={"continue_url": CONTINUE_CALLBACK_URL},
        url=protocol_get_rt.OPENAI_API_ENDPOINTS["signup"],
    )


def _invalid_state():
    payload = {"error": {"code": "invalid_state", "message": "state no longer valid"}}
    return _Resp(
        status_code=409,
        text=json.dumps(payload),
        json_data=payload,
        url=protocol_get_rt.OPENAI_API_ENDPOINTS["signup"],
    )


def _account_deactivated():
    payload = {"error": {"code": "account_deactivated", "message": "account is deactivated"}}
    return _Resp(
        status_code=403,
        text=json.dumps(payload),
        json_data=payload,
        url=protocol_get_rt.OPENAI_API_ENDPOINTS["signup"],
    )


class _FakeSession:
    def __init__(self, get_responses, post_responses):
        self._get_responses = list(get_responses)
        self._post_responses = list(post_responses)
        self.calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url))
        return self._get_responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url))
        self.post_calls.append((url, kwargs))
        return self._post_responses.pop(0)


class _FakeEngine:
    def __init__(self):
        self.sentinel_builds = 0

    def _build_sentinel_header_for_client(self, _client, _device_id, _flow):
        self.sentinel_builds += 1
        return f"sentinel-{self.sentinel_builds}"

    def _platform_json_headers(self, *, device_id, referer):
        return {"content-type": "application/json", "referer": referer}

    @staticmethod
    def _set_oai_did_for_session(_session, _device_id):
        return None

    @staticmethod
    def _is_invalid_state_response(response) -> bool:
        text = str(getattr(response, "text", "") or "")
        if "invalid_state" in text or "no longer valid" in text:
            return True
        try:
            data = response.json()
        except Exception:
            return False
        error = data.get("error") if isinstance(data, dict) else {}
        return isinstance(error, dict) and str(error.get("code") or "") == "invalid_state"


class _FakeHTTPClient:
    def __init__(self, session):
        self.session = session


class _OAuthStart:
    auth_url = AUTH_URL
    state = "state-abc"
    code_verifier = "verifier-abc"


@pytest.fixture(autouse=True)
def _patch_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(protocol_get_rt.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def _patch_flow(monkeypatch, *, session, engine, submit_calls):
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", lambda **_kwargs: engine)
    monkeypatch.setattr(
        protocol_get_rt,
        "OpenAIHTTPClient",
        lambda **_kwargs: _FakeHTTPClient(session=session),
    )
    monkeypatch.setattr(protocol_get_rt, "generate_oauth_url", lambda **_kwargs: _OAuthStart())

    def _submit_callback(**kwargs):
        submit_calls.append(kwargs)
        return json.dumps(
            {"access_token": "access-1", "refresh_token": "refresh-1", "id_token": "id-1"}
        )

    monkeypatch.setattr(protocol_get_rt, "submit_callback_url", _submit_callback)


def _run(logs):
    return protocol_get_rt.run_protocol_get_rt(
        email="user@example.com",
        password="",
        proxy="http://127.0.0.1:7890",
        otp_callback=lambda **_kwargs: "",
        log_fn=logs.append,
    )

def test_cloudflare_challenge_detection_helpers():
    assert protocol_get_rt._is_cloudflare_challenge_response(_cf_challenge()) is True
    assert (
        protocol_get_rt._is_cloudflare_challenge_response(
            _Resp(status_code=200, text="<html>just a moment</html>")
        )
        is True
    )
    assert (
        protocol_get_rt._is_cloudflare_challenge_response(_account_deactivated()) is False
    )
    assert (
        protocol_get_rt._is_cloudflare_challenge_response(
            _Resp(
                status_code=403,
                text="<html>blocked</html>",
                headers={"server": "cloudflare", "content-type": "text/html"},
            )
        )
        is True
    )
    assert protocol_get_rt._is_authorize_page_ready(_login_page()) is True
    assert protocol_get_rt._is_authorize_page_ready(_cf_challenge()) is False
    assert protocol_get_rt._is_authorize_page_ready(_Resp(status_code=500, text="oops")) is False


def test_authorize_cf_challenge_recovers_before_continue(monkeypatch, _patch_sleep):
    logs = []
    submit_calls = []
    engine = _FakeEngine()
    session = _FakeSession(
        get_responses=[_cf_challenge(), _login_page()],
        post_responses=[_continue_success()],
    )
    _patch_flow(monkeypatch, session=session, engine=engine, submit_calls=submit_calls)

    token_info = _run(logs)

    assert token_info["access_token"] == "access-1"
    assert [kind for kind, _url in session.calls] == ["get", "get", "post"]
    assert len(session.post_calls) == 1
    continue_url, continue_kwargs = session.post_calls[0]
    assert continue_url == protocol_get_rt.OPENAI_API_ENDPOINTS["signup"]
    assert continue_kwargs["headers"]["openai-sentinel-token"] == "sentinel-1"
    assert json.loads(continue_kwargs["data"]) == {
        "username": {"kind": "email", "value": "user@example.com"},
    }
    assert engine.sentinel_builds == 1
    assert _patch_sleep == [5]
    assert any(
        "attempt=1/3" in line and "status=403" in line for line in logs
    )
    assert any(
        "attempt=2/3" in line and "status=200" in line for line in logs
    )


def test_authorize_cf_challenge_exhaustion_raises_restart(monkeypatch, _patch_sleep):
    logs = []
    engine = _FakeEngine()
    session = _FakeSession(
        get_responses=[_cf_challenge(), _cf_challenge(), _cf_challenge()],
        post_responses=[],
    )
    _patch_flow(monkeypatch, session=session, engine=engine, submit_calls=[])

    with pytest.raises(RuntimeError) as excinfo:
        _run(logs)

    assert "GET_RT_LOGIN_RESTART_REQUIRED" in str(excinfo.value)
    assert "authorize/bootstrap" in str(excinfo.value)
    assert session.post_calls == []
    assert [kind for kind, _url in session.calls] == ["get", "get", "get"]
    assert engine.sentinel_builds == 0
    assert _patch_sleep == [5, 10]
    assert sum(1 for line in logs if "attempt=" in line and "authorize attempt" in line) >= 3


def test_authorize_non_cf_error_exhaustion_raises_restart(monkeypatch, _patch_sleep):
    logs = []
    engine = _FakeEngine()
    fatal = _Resp(status_code=503, text="service unavailable", url=AUTH_URL)
    session = _FakeSession(get_responses=[fatal, fatal, fatal], post_responses=[])
    _patch_flow(monkeypatch, session=session, engine=engine, submit_calls=[])

    with pytest.raises(RuntimeError) as excinfo:
        _run(logs)

    assert "GET_RT_LOGIN_RESTART_REQUIRED" in str(excinfo.value)
    assert session.post_calls == []
    assert _patch_sleep == [5, 10]


def test_continue_invalid_state_recovers_multiple_rounds(monkeypatch, _patch_sleep):
    logs = []
    submit_calls = []
    engine = _FakeEngine()
    session = _FakeSession(
        get_responses=[_login_page(), _login_page(), _login_page()],
        post_responses=[_invalid_state(), _invalid_state(), _continue_success()],
    )
    _patch_flow(monkeypatch, session=session, engine=engine, submit_calls=submit_calls)

    token_info = _run(logs)

    assert token_info["access_token"] == "access-1"
    assert len(session.post_calls) == 3
    assert [kind for kind, _url in session.calls] == ["get", "post", "get", "post", "get", "post"]
    assert engine.sentinel_builds == 3
    sentinel_tokens = [
        kwargs["headers"]["openai-sentinel-token"] for _url, kwargs in session.post_calls
    ]
    assert sentinel_tokens == ["sentinel-1", "sentinel-2", "sentinel-3"]
    for _url, kwargs in session.post_calls:
        assert json.loads(kwargs["data"]) == {
            "username": {"kind": "email", "value": "user@example.com"},
        }
    assert _patch_sleep == [5, 10]
    assert any(
        "invalid_state" in line and "attempt=1/3" in line and "delay=5s" in line
        for line in logs
    )


def test_continue_invalid_state_exhaustion_raises_restart(monkeypatch, _patch_sleep):
    logs = []
    engine = _FakeEngine()
    session = _FakeSession(
        get_responses=[_login_page(), _login_page(), _login_page()],
        post_responses=[_invalid_state(), _invalid_state(), _invalid_state()],
    )
    _patch_flow(monkeypatch, session=session, engine=engine, submit_calls=[])

    with pytest.raises(RuntimeError) as excinfo:
        _run(logs)

    assert "GET_RT_LOGIN_RESTART_REQUIRED" in str(excinfo.value)
    assert "invalid_state" in str(excinfo.value)
    assert len(session.post_calls) == 3
    assert [kind for kind, _url in session.calls] == ["get", "post", "get", "post", "get", "post"]
    assert _patch_sleep == [5, 10]
    assert any(
        "恢复耗尽" in line and "attempts=3/3" in line for line in logs
    )


def test_continue_account_deactivated_not_recovered(monkeypatch, _patch_sleep):
    logs = []
    engine = _FakeEngine()
    session = _FakeSession(
        get_responses=[_login_page()],
        post_responses=[_account_deactivated()],
    )
    _patch_flow(monkeypatch, session=session, engine=engine, submit_calls=[])

    with pytest.raises(RuntimeError) as excinfo:
        _run(logs)

    message = str(excinfo.value)
    assert "account_deactivated" in message
    assert "GET_RT_LOGIN_RESTART_REQUIRED" not in message
    assert len(session.post_calls) == 1
    assert [kind for kind, _url in session.calls] == ["get", "post"]
    assert engine.sentinel_builds == 1
    assert _patch_sleep == []
