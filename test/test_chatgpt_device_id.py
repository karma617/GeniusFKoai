from platforms.chatgpt import register as register_module
from platforms.chatgpt.register import RegistrationEngine


class FakeCookies(dict):
    def set(self, name, value, *args, **kwargs):
        self[name] = value


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload or {}
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.cookies = FakeCookies()
        self.calls = []

    def get(self, url, timeout=15):
        self.calls.append(("GET", url))
        if url.endswith("/api/auth/csrf"):
            return FakeResponse({"csrfToken": "csrf-token"})
        return FakeResponse()

    def post(self, url, headers=None, data=None, timeout=15):
        self.calls.append(("POST", url, data))
        return FakeResponse({"url": "https://auth.openai.com/api/accounts/authorize?client_id=test"})


def _engine_with_session(session):
    engine = object.__new__(RegistrationEngine)
    engine.session = session
    engine.logs = []
    engine.callback_logger = None
    engine.task_uuid = None
    engine.oauth_start = None
    return engine


def test_start_oauth_seeds_device_id_when_chatgpt_cookie_missing(monkeypatch):
    """OpenAI 未返回 oai-did 时，协议注册本地生成 Device ID 继续走后续流程。"""

    monkeypatch.setattr(register_module.uuid, "uuid4", lambda: "device-id-123")
    session = FakeSession()
    engine = _engine_with_session(session)

    assert engine._start_oauth() is True
    assert session.cookies["oai-did"] == "device-id-123"
    assert "ext-oai-did=device-id-123" in session.calls[2][1]
    assert engine._get_device_id() == "device-id-123"
    assert any("已本地生成" in item for item in engine.logs)
