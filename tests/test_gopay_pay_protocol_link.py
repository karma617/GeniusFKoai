"""GoPay 协议提链接入编排器的测试（注入假协议模块，不打真实 HTTP/DB）。"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from application import gopay_pay_chatgpt as flow


MIDTRANS = (
    "https://app.midtrans.com/snap/v4/redirection/"
    "11111111-1111-1111-1111-111111111111"
)
CASHIER = "https://chatgpt.com/checkout/openai_ie/oaics_abc123XYZ"


def _fake_protocol_module():
    mod = types.ModuleType("platforms.chatgpt.gopay_link_protocol")
    mod.checkout_url = staticmethod(lambda checkout: CASHIER)
    mod.extract_gopay_payment_link = staticmethod(
        lambda transport, **kwargs: {
            "provider_redirect_url": MIDTRANS,
            "checkout_session_id": "oaics_abc123XYZ",
            "processor_entity": "openai_ie",
            "checkout_provider": "stripe",
            "payment_method_type": "gopay",
            "payment_link_type": "gopay_custom_payment_method_redirect",
            "currency": "IDR",
            "payment_method_country": "ID",
        }
    )
    return mod


class _FakeTransport:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)

    def send(self, request):
        return {}

    def close(self):
        pass

    def probe_checkout_exit(self, expected_country):
        assert expected_country == "ID"
        return {"country": "ID", "ip": "180.244.154.144"}


class _FakeDbSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _model, _pk):
        return None

    def merge(self, model, load=False):
        return model

    def commit(self):
        pass


def _install_protocol_fakes(monkeypatch):
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.gopay_link_protocol", _fake_protocol_module())
    fake_transport_mod = types.ModuleType("platforms.chatgpt.gopay_link_transport")
    fake_transport_mod.CurlCffiTransport = _FakeTransport
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.gopay_link_transport", fake_transport_mod)
    monkeypatch.setattr(flow, "Session", lambda _engine: _FakeDbSession())
    monkeypatch.setattr(
        flow,
        "build_platform_account",
        lambda _session, _model: SimpleNamespace(
            extra={"access_token": "tok", "cookies": "", "account_id": "42"},
            email="buyer@example.com",
            token="tok",
        ),
    )
    from platforms.chatgpt import payment

    monkeypatch.setattr(
        payment, "_checkout_account_metadata",
        lambda _account: ("dev-1", "ver-1", "build-1"),
    )
    monkeypatch.setattr(payment, "_extract_chatgpt_account_id", lambda _account: "42")
    monkeypatch.setattr(
        payment, "fetch_billing_address",
        lambda _region: {
            "name": "James Smith",
            "email": "buyer@example.com",
            "line1": "Jalan M.H. Thamrin No. 1",
            "city": "Jakarta",
            "state": "DKI Jakarta",
            "postal_code": "10310",
        },
    )
    _FakeTransport.instances = []


def test_step_extract_requires_task_proxy(monkeypatch):
    _install_protocol_fakes(monkeypatch)
    with pytest.raises(RuntimeError, match="任务代理池"):
        flow.step_extract_gopay_link_protocol(SimpleNamespace(), log=lambda _m: None)


def test_step_extract_gopay_link_protocol_returns_cashier_and_midtrans(monkeypatch):
    _install_protocol_fakes(monkeypatch)
    result = flow.step_extract_gopay_link_protocol(
        SimpleNamespace(),
        proxy="http://proxy.example.test",
        log=lambda _message: None,
    )
    assert result["midtrans_url"] == MIDTRANS
    assert result["cashier_url"] == CASHIER
    assert _FakeTransport.instances, "应构造 CurlCffiTransport"
    transport_kwargs = _FakeTransport.instances[-1].kwargs
    assert transport_kwargs["access_token"] == "tok"
    assert transport_kwargs["proxy"] == "http://proxy.example.test"
    assert transport_kwargs["chatgpt_account_id"] == "42"


def test_step_extract_rejects_invalid_midtrans_url(monkeypatch):
    fake_proto = _fake_protocol_module()
    fake_proto.extract_gopay_payment_link = staticmethod(
        lambda transport, **kwargs: {**{
            "checkout_session_id": "oaics_x",
            "processor_entity": "openai_ie",
        }, "provider_redirect_url": "https://example.com/not-midtrans"}
    )
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.gopay_link_protocol", fake_proto)
    fake_transport_mod = types.ModuleType("platforms.chatgpt.gopay_link_transport")
    fake_transport_mod.CurlCffiTransport = _FakeTransport
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.gopay_link_transport", fake_transport_mod)
    monkeypatch.setattr(flow, "Session", lambda _engine: _FakeDbSession())
    monkeypatch.setattr(
        flow, "build_platform_account",
        lambda _session, _model: SimpleNamespace(
            extra={"access_token": "tok"}, email="x@example.com", token="tok",
        ),
    )
    from platforms.chatgpt import payment

    monkeypatch.setattr(payment, "_checkout_account_metadata", lambda _a: ("d", "", ""))
    monkeypatch.setattr(payment, "_extract_chatgpt_account_id", lambda _a: "")
    monkeypatch.setattr(payment, "fetch_billing_address", lambda _r: {})
    with pytest.raises(RuntimeError, match="未得到有效的 Midtrans Snap URL"):
        flow.step_extract_gopay_link_protocol(
            SimpleNamespace(), proxy="http://proxy.example.test", log=lambda _m: None,
        )


def _install_full_flow_fakes(monkeypatch):
    monkeypatch.setattr(flow, "Session", lambda _engine: _FakeDbSession())
    monkeypatch.setattr(
        flow, "find_chatgpt_account",
        lambda _aid: SimpleNamespace(
            id=_aid, platform="chatgpt", email="x@example.com", extra={},
        ),
    )
    monkeypatch.setattr(
        flow, "build_platform_account",
        lambda _session, _model: SimpleNamespace(
            extra={"access_token": "tok", "cookies": "", "sms_provider": "smsapi"},
            email="x@example.com",
            token="tok",
        ),
    )
    gopay_model = SimpleNamespace(
        id=7, platform="gopay", email="+628123456789",
        password="147258", extra={"sms_provider": "smsapi"}, created_at=None,
    )
    monkeypatch.setattr(flow, "pick_available_gopay_account", lambda **kwargs: gopay_model)
    monkeypatch.setattr(flow, "_resolve_gopay_client", lambda *_a, **_k: object())
    monkeypatch.setattr(flow, "wait_for_balance", lambda **kwargs: 50000)
    monkeypatch.setattr(
        flow, "step_pay_with_gopay",
        lambda *_a, **_k: {
            "success": True,
            "transaction_status": "settlement",
            "amount": "349000",
            "currency": "IDR",
        },
    )
    monkeypatch.setattr(flow, "_verify_chatgpt_subscription", lambda *_a, **_k: "plus")
    fake_worker = types.ModuleType("opai.core.gopay_protocol_worker")
    fake_worker._check_balance = lambda _client: 50000
    monkeypatch.setitem(sys.modules, "opai.core.gopay_protocol_worker", fake_worker)
    monkeypatch.setitem(
        sys.modules, "opai.core.gopay_payment_protocol",
        types.ModuleType("opai.core.gopay_payment_protocol"),
    )
    import application.gopay_payment_state as gps

    monkeypatch.setattr(gps, "acquire_gopay_lease", lambda **kwargs: True)


def test_execute_protocol_mode_uses_protocol_link_and_skips_browser(monkeypatch):
    _install_full_flow_fakes(monkeypatch)
    extract_calls = []

    def fake_extract(_chatgpt_model, **kwargs):
        extract_calls.append(kwargs)
        return {"cashier_url": CASHIER, "midtrans_url": MIDTRANS}

    monkeypatch.setattr(flow, "step_extract_gopay_link_protocol", fake_extract)
    browser_called = {"flag": False}
    monkeypatch.setattr(
        flow, "step_grab_midtrans_url",
        lambda *_a, **_k: browser_called.update(flag=True) or "x",
    )
    monkeypatch.setattr(
        flow, "step_generate_cashier_url",
        lambda *_a, **_k: browser_called.update(flag=True) or "x",
    )

    out = flow.execute_gopay_pay_chatgpt(
        chatgpt_account_id=1,
        proxy="http://proxy.example.test",
        link_mode="protocol",
        country="ID",
        currency="IDR",
        log=lambda _message: None,
    )

    assert extract_calls, "link_mode=protocol 应调用纯协议提链"
    assert extract_calls[0]["country"] == "ID"
    assert extract_calls[0]["currency"] == "IDR"
    assert extract_calls[0]["proxy"] == "http://proxy.example.test"
    assert browser_called["flag"] is False, "协议模式下不应打开浏览器/调 step_generate_cashier_url"
    assert out["midtrans_url"] == MIDTRANS
    assert out["cashier_url"] == CASHIER
    assert out["payment"]["success"] is True


def test_execute_browser_mode_still_uses_old_path(monkeypatch):
    _install_full_flow_fakes(monkeypatch)
    protocol_called = {"flag": False}
    monkeypatch.setattr(
        flow, "step_extract_gopay_link_protocol",
        lambda *_a, **_k: protocol_called.update(flag=True) or {},
    )
    monkeypatch.setattr(
        flow, "step_generate_cashier_url",
        lambda _account, **_k: "https://chatgpt.com/checkout/openai_llc/cs_live_x",
    )
    monkeypatch.setattr(
        flow, "step_grab_midtrans_url",
        lambda _url, **_k: MIDTRANS,
    )

    out = flow.execute_gopay_pay_chatgpt(
        chatgpt_account_id=1,
        proxy="http://proxy.example.test",
        link_mode="browser",
        country="ID",
        currency="IDR",
        log=lambda _message: None,
    )


    assert protocol_called["flag"] is False, "browser 模式不应走纯协议提链"
    assert out["midtrans_url"] == MIDTRANS
    assert out["cashier_url"].startswith("https://chatgpt.com/checkout/openai_llc/")

def test_execute_short_link_mode_uses_protocol_from_cashier_and_skips_browser(monkeypatch):
    _install_full_flow_fakes(monkeypatch)
    generate_calls = []
    extract_calls = []
    browser_called = {"flag": False}

    monkeypatch.setattr(
        flow, "step_generate_cashier_url",
        lambda _account, **kwargs: generate_calls.append(kwargs)
        or "https://chatgpt.com/checkout/openai_ie/oaics_short_link_x",
    )
    monkeypatch.setattr(
        flow, "step_extract_gopay_link_from_cashier",
        lambda _url, _model, **kwargs: extract_calls.append((_url, kwargs))
        or {"cashier_url": "https://chatgpt.com/checkout/openai_ie/oaics_short_link_x", "midtrans_url": MIDTRANS},
    )
    monkeypatch.setattr(
        flow, "step_grab_midtrans_url",
        lambda *_a, **_k: browser_called.update(flag=True) or "x",
    )
    from platforms.chatgpt import payment as chatgpt_payment
    monkeypatch.setattr(
        chatgpt_payment, "select_gopay_and_grab_midtrans",
        lambda *_a, **_k: browser_called.update(flag=True) or "x",
    )

    out = flow.execute_gopay_pay_chatgpt(
        chatgpt_account_id=1,
        proxy="http://proxy.example.test",
        link_mode="protocol",
        use_short_link=True,
        country="ID",
        currency="IDR",
        log=lambda _message: None,
    )

    assert generate_calls, "短链模式应先协议生成短链"
    assert generate_calls[0]["use_short_link"] is True
    assert extract_calls, "短链模式应走协议从既有 cashier 抓 midtrans"
    assert browser_called["flag"] is False, "短链模式不得打开浏览器"
    assert out["midtrans_url"] == MIDTRANS


def test_execute_cashier_url_override_uses_protocol_from_cashier_and_skips_browser(monkeypatch):
    _install_full_flow_fakes(monkeypatch)
    extract_calls = []
    browser_called = {"flag": False}

    monkeypatch.setattr(
        flow, "step_extract_gopay_link_from_cashier",
        lambda _url, _model, **kwargs: extract_calls.append((_url, kwargs))
        or {"cashier_url": _url, "midtrans_url": MIDTRANS},
    )
    monkeypatch.setattr(
        flow, "step_grab_midtrans_url",
        lambda *_a, **_k: browser_called.update(flag=True) or "x",
    )
    from platforms.chatgpt import payment as chatgpt_payment
    monkeypatch.setattr(
        chatgpt_payment, "select_gopay_and_grab_midtrans",
        lambda *_a, **_k: browser_called.update(flag=True) or "x",
    )

    override = "https://chatgpt.com/checkout/openai_ie/oaics_override_x"
    out = flow.execute_gopay_pay_chatgpt(
        chatgpt_account_id=1,
        proxy="http://proxy.example.test",
        link_mode="protocol",
        cashier_url_override=override,
        country="ID",
        currency="IDR",
        log=lambda _message: None,
    )

    assert extract_calls, "cashier_url_override 应走协议从既有 cashier 抓 midtrans"
    assert extract_calls[0][0] == override
    assert browser_called["flag"] is False, "cashier_url_override 不得打开浏览器"
    assert out["midtrans_url"] == MIDTRANS


def test_step_extract_falls_back_to_id_seed_for_invalid_postal(monkeypatch):
    """地址服务返回非印尼占位邮编（如英国 CH3O 3OF）时，必须兜底为印尼 seed。"""
    captured = {}
    fake_proto = types.ModuleType("platforms.chatgpt.gopay_link_protocol")
    fake_proto.checkout_url = staticmethod(lambda checkout: CASHIER)
    fake_proto.extract_gopay_payment_link = staticmethod(
        lambda transport, **kwargs: captured.update(billing=kwargs.get("billing"))
        or {
            "provider_redirect_url": MIDTRANS,
            "checkout_session_id": "oaics_x",
            "processor_entity": "openai_ie",
        }
    )
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.gopay_link_protocol", fake_proto)
    fake_tmod = types.ModuleType("platforms.chatgpt.gopay_link_transport")
    fake_tmod.CurlCffiTransport = _FakeTransport
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.gopay_link_transport", fake_tmod)
    monkeypatch.setattr(flow, "Session", lambda _engine: _FakeDbSession())
    monkeypatch.setattr(
        flow, "build_platform_account",
        lambda _session, _model: SimpleNamespace(
            extra={"access_token": "tok", "cookies": "", "account_id": "42"},
            email="buyer@example.com",
            token="tok",
        ),
    )
    from platforms.chatgpt import payment

    monkeypatch.setattr(payment, "_checkout_account_metadata", lambda _a: ("d", "", ""))
    monkeypatch.setattr(payment, "_extract_chatgpt_account_id", lambda _a: "")
    monkeypatch.setattr(
        payment, "fetch_billing_address",
        lambda _region: {
            "name": "Som Raj",
            "line1": "96 Corn Market",
            "city": "Newry",
            "state": "Nordirland",
            "postal_code": "CH3O 3OF",
            "phone": "+44 7726045703",
            "country": "ID",
            "email": "cdjlddchsk@iubridge.com",
        },
    )
    _FakeTransport.instances = []

    flow.step_extract_gopay_link_protocol(
        SimpleNamespace(),
        proxy="http://proxy.example.test",
        log=lambda _message: None,
    )

    billing = captured.get("billing") or {}
    assert billing["postal_code"] == "10310"
    assert billing["state"] == "DKI Jakarta"
    assert billing["email"] == "cdjlddchsk@iubridge.com"  # 外部临时邮箱保留


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class _FakeSessionSeq:
    """按调用顺序返回脚本化响应的假 curl_cffi 会话。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = SimpleNamespace(values={})

    def _next(self):
        if not self.responses:
            raise AssertionError(f"意外多余的请求: {self.calls[-1] if self.calls else 'none'}")
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if url.endswith("/cdn-cgi/trace"):
            return _FakeResponse(200, None, "ip=180.244.154.144\nloc=ID")
        return self._next()

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self._next()

    def close(self):
        pass


def test_real_protocol_and_transport_integration(monkeypatch):
    """真实协议模块 + 真实 CurlCffiTransport + 真实 step 函数端到端。

    仅把 curl_cffi 会话与 Sentinel 桩掉，走完整 OAICS 提链请求序列。
    """
    monkeypatch.setattr(flow, "Session", lambda _engine: _FakeDbSession())
    monkeypatch.setattr(
        flow, "build_platform_account",
        lambda _session, _model: SimpleNamespace(
            extra={"access_token": "tok", "cookies": "", "account_id": "42"},
            email="buyer@example.com",
            token="tok",
        ),
    )
    from platforms.chatgpt import payment

    monkeypatch.setattr(payment, "_checkout_account_metadata", lambda _a: ("dev-1", "ver-1", "build-1"))
    monkeypatch.setattr(payment, "_extract_chatgpt_account_id", lambda _a: "42")
    monkeypatch.setattr(
        payment, "fetch_billing_address",
        lambda _region: {
            "name": "James Smith",
            "email": "buyer@example.com",
            "line1": "Jalan M.H. Thamrin No. 1",
            "city": "Jakarta",
            "state": "DKI Jakarta",
            "postal_code": "10310",
        },
    )

    from platforms.chatgpt import gopay_link_protocol as real_proto

    oaics = {
        "checkout_session_id": "oaics_abc123XYZ",
        "processor_entity": "openai_ie",
        "stripe_publishable_key": (
            "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
        ),
        "billing_details": {"country": "ID", "currency": "IDR"},
        "checkout_state": {"total": {"total": {"minorUnitsAmount": "349000"}}},
        "payment_method_types": ["gopay"],
        "custom_payment_methods": [{"id": "cpmt_abc123XYZ"}],
    }
    responses = [
        _FakeResponse(200, {}),  # warmup GET /api/auth/csrf
        _FakeResponse(200, oaics),  # checkout.create
        _FakeResponse(200, oaics),  # checkout.fetch
        _FakeResponse(200, {  # stripe.elements.sessions
            "config_id": "cfg-1",
            "custom_payment_method_data": [
                {"type": "cpmt_abc123XYZ", "display_name": "GoPay"},
            ],
        }),
        _FakeResponse(200, {}),  # checkout.taxes
        _FakeResponse(200, oaics),  # checkout.fetch (税后)
        _FakeResponse(200, {"status": "success"}),  # checkout.custom_confirm
        _FakeResponse(200, {  # checkout.custom_payment_method.start
            "status": "requires_action",
            "next_action": {"url": MIDTRANS},
        }),
    ]
    session = _FakeSessionSeq(responses)
    monkeypatch.setattr(payment, "_create_checkout_session", lambda _proxy: session)
    monkeypatch.setattr(
        payment, "_build_checkout_sentinel_headers",
        lambda *args, **kwargs: {},
    )

    result = flow.step_extract_gopay_link_protocol(
        SimpleNamespace(),
        proxy="http://proxy.example.test",
        log=lambda _message: None,
    )

    assert result["midtrans_url"] == MIDTRANS
    assert result["cashier_url"] == CASHIER
    stages = [url for _kind, url, _kwargs in session.calls]
    assert any("backend-api/payments/checkout" in url for url in stages)
    assert any("elements/sessions" in url for url in stages)
    assert any("custom_payment_method/start" in url for url in stages)