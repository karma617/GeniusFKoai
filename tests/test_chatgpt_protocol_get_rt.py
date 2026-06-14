from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from platforms.chatgpt import protocol_get_rt


class _FakeResponse:
    def __init__(self, status_code: int, *, url: str = "https://auth.openai.com/test", data=None, text: str = ""):
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": "application/json"}
        self._data = data
        self.text = text if text else json.dumps(data or {}, ensure_ascii=False)

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def test_protocol_get_rt_logs_authorize_continue_409_detail(monkeypatch):
    logs = []

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **_kwargs):
            return _FakeResponse(403, url=url, data={"error": "forbidden"})

        def post(self, url, **_kwargs):
            return _FakeResponse(
                409,
                url=url,
                data={"error": {"code": "invalid_state", "message": "state no longer valid"}},
            )

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.session = FakeSession()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.email = ""
            self.password = ""

        def _set_oai_did_for_session(self, session, device_id):
            session.cookies["oai-did"] = device_id

        def _build_sentinel_header_for_client(self, *_args, **_kwargs):
            return "sentinel"

        def _platform_json_headers(self, **_kwargs):
            return {"referer": "https://auth.openai.com/log-in", "content-type": "application/json"}

        @staticmethod
        def _is_invalid_state_response(_resp):
            return False

    monkeypatch.setattr(protocol_get_rt, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(protocol_get_rt, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_get_rt,
        "generate_oauth_url",
        lambda **_kwargs: SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
            state="state_1",
            code_verifier="verifier_1",
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        protocol_get_rt.run_protocol_get_rt(
            email="user@example.com",
            password="Secret123!",
            proxy=None,
            otp_callback=lambda: "123456",
            log_fn=logs.append,
        )

    assert "HTTP 409" in str(excinfo.value)
    assert "invalid_state" in str(excinfo.value)
    assert any("authorize/continue debug: status=409" in item for item in logs)
    assert any("authorize/continue body:" in item and "state no longer valid" in item for item in logs)
