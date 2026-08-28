"""CurlCffiTransport 适配器单测（stub curl_cffi session 与 Sentinel）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from platforms.chatgpt import gopay_link_transport as gt
from platforms.chatgpt import payment


class _FakeCookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, **kwargs):
        self.values[name] = value


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.cookies = _FakeCookies()
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response

    def close(self):
        pass


def _req(stage, url, *, headers=None, json=None, data=None, params=()):
    return SimpleNamespace(
        stage=stage,
        url=url,
        headers=headers or {},
        json=json,
        data=data,
        params=params,
    )


def _install(monkeypatch, response):
    session = _FakeSession(response)
    monkeypatch.setattr(payment, "_create_checkout_session", lambda proxy: session)
    monkeypatch.setattr(
        payment,
        "_build_checkout_sentinel_headers",
        lambda *a, **k: {"openai-sentinel-token": "sentinel-tok"},
    )
    return session


def test_openai_route_gets_auth_and_sentinel_headers(monkeypatch):
    session = _install(monkeypatch, _FakeResponse(200, {"ok": True}))
    transport = gt.CurlCffiTransport(
        access_token="tok123", device_id="dev-1", client_version="v1",
        country="ID", log=lambda _message: None,
    )

    out = transport.send(_req(
        "checkout.create",
        "https://chatgpt.com/backend-api/payments/checkout",
        json={"plan_name": "chatgptplusplan"},
    ))

    assert out == {"ok": True}
    # 首个调用是预热 GET，随后是业务 POST
    assert session.calls[0][0] == "get"
    post = session.calls[-1]
    assert post[0] == "post"
    headers = post[2]["headers"]
    assert headers["Authorization"] == "Bearer tok123"
    assert headers["openai-sentinel-token"] == "sentinel-tok"
    assert headers["oai-device-id"] == "dev-1"
    assert headers["oai-session-id"]
    assert headers["Content-Type"] == "application/json"
    assert post[2]["json"] == {"plan_name": "chatgptplusplan"}


def test_probe_checkout_exit_uses_same_transport_session(monkeypatch):
    session = _install(monkeypatch, _FakeResponse(200, {"ok": True}))
    monkeypatch.setattr(
        payment,
        "_probe_checkout_session_exit",
        lambda checked_session, **kwargs: {
            "ok": checked_session is session,
            "country": kwargs["expected_country"],
            "ip": "180.244.154.144",
        },
    )
    transport = gt.CurlCffiTransport(
        access_token="tok123",
        proxy="http://proxy.example.test",
        country="ID",
        log=lambda _message: None,
    )

    result = transport.probe_checkout_exit("ID")

    assert result == {"ok": True, "country": "ID", "ip": "180.244.154.144"}
    assert session.calls == []


def test_stripe_route_has_no_authorization_and_no_sentinel(monkeypatch):
    session = _install(monkeypatch, _FakeResponse(200, {"config_id": "cfg"}))
    transport = gt.CurlCffiTransport(
        access_token="tok123", log=lambda _message: None,
    )

    out = transport.send(_req(
        "stripe.elements.sessions",
        "https://api.stripe.com/v1/elements/sessions",
        params=(("key", "pk_live_x"),),
        headers={"Referer": "https://chatgpt.com"},
    ))

    assert out == {"config_id": "cfg"}
    assert len(session.calls) == 1  # stripe 路由不做预热/Sentinel
    call = session.calls[0]
    assert call[0] == "get"
    headers = call[2]["headers"]
    assert "Authorization" not in headers
    assert "openai-sentinel-token" not in headers
    assert call[2]["params"] == {"key": "pk_live_x"}


def test_form_data_post_sets_urlencoded_content_type(monkeypatch):
    session = _install(monkeypatch, _FakeResponse(200, {"id": "pm_x"}))
    transport = gt.CurlCffiTransport(
        access_token="tok123", log=lambda _message: None,
    )

    out = transport.send(_req(
        "stripe.payment_methods",
        "https://api.stripe.com/v1/payment_methods",
        data={"type": "gopay"},
    ))

    assert out == {"id": "pm_x"}
    call = session.calls[0]
    assert call[2]["data"] == {"type": "gopay"}
    assert call[2]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_non_2xx_raises_runtime_error(monkeypatch):
    session = _install(monkeypatch, _FakeResponse(500, None, "boom body"))
    transport = gt.CurlCffiTransport(
        access_token="tok123", log=lambda _message: None,
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        transport.send(_req(
            "checkout.fetch",
            "https://chatgpt.com/backend-api/payments/checkout/openai_ie/oaics_x",
        ))


def test_non_dict_json_falls_back_to_empty_dict(monkeypatch):
    session = _install(monkeypatch, _FakeResponse(200, ["not", "a", "dict"]))
    transport = gt.CurlCffiTransport(
        access_token="tok123", log=lambda _message: None,
    )

    out = transport.send(_req(
        "checkout.fetch",
        "https://chatgpt.com/backend-api/payments/checkout/openai_ie/oaics_x",
    ))
    assert out == {}
