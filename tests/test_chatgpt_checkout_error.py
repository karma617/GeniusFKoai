from types import SimpleNamespace

import pytest

from application import gopay_pay_chatgpt as flow
from platforms.chatgpt import payment
from platforms.chatgpt.authflow_experimental import sentinel_quickjs


class _CheckoutResponse:
    status_code = 400
    text = '{"detail":"Our systems have detected unusual activity. Please try again later."}'

    def json(self):
        return {"detail": "Our systems have detected unusual activity. Please try again later."}


class _CheckoutCookies:
    def __init__(self):
        self.values = []

    def set(self, name, value, **kwargs):
        self.values.append((name, value, kwargs))


class _CheckoutSession:
    def __init__(self, response, trace_text="ip=180.246.204.135\nloc=ID"):
        self.response = response
        self.trace_text = trace_text
        self.cookies = _CheckoutCookies()
        self.warmups = []
        self.posts = []
        self.closed = False

    def get(self, url, **kwargs):
        self.warmups.append((url, kwargs))
        if url == payment.CHECKOUT_EXIT_TRACE_URL:
            return SimpleNamespace(status_code=200, text=self.trace_text)
        return SimpleNamespace(status_code=200, text="")

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


def _install_checkout_session(monkeypatch, response, *, trace_text="ip=180.246.204.135\nloc=ID"):
    session = _CheckoutSession(response, trace_text=trace_text)
    monkeypatch.setattr(payment, "_create_checkout_session", lambda _proxy: session)
    monkeypatch.setattr(
        payment,
        "_build_checkout_sentinel_headers",
        lambda *_args, **_kwargs: {"openai-sentinel-token": "sentinel-token"},
    )
    return session


def test_sentinel_quickjs_propagates_retryable_tls_error(monkeypatch):
    error = (
        "Failed to perform, curl: (35) TLS connect error: "
        "OPENSSL_internal:invalid library (0)"
    )
    logs = []

    def fail_sdk_download(*_args, **_kwargs):
        raise RuntimeError(error)

    monkeypatch.setattr(sentinel_quickjs, "_ensure_sdk_file", fail_sdk_download)

    with pytest.raises(RuntimeError, match=r"curl: \(35\) TLS connect error"):
        sentinel_quickjs.get_sentinel_tokens_via_quickjs(
            object(),
            "device-id",
            flow="chatgpt_checkout",
            log=logs.append,
        )

    assert any("Sentinel QuickJS 异常" in item and "curl: (35)" in item for item in logs)


def test_checkout_sentinel_retries_tls_error_before_single_post(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        text="",
        json=lambda: {
            "checkout_session_id": "cs_live_retryvalue",
            "processor_entity": "openai_llc",
        },
    )
    session = _CheckoutSession(response)
    calls = []
    sleeps = []
    logs = []

    def generate_sentinel(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(
                "Failed to perform, curl: (35) TLS connect error: "
                "OPENSSL_internal:invalid library (0)"
            )
        return {"token": "sentinel-token", "so_token": ""}

    monkeypatch.setattr(payment, "_create_checkout_session", lambda _proxy: session)
    monkeypatch.setattr(
        sentinel_quickjs,
        "get_sentinel_tokens_via_quickjs",
        generate_sentinel,
    )
    monkeypatch.setattr(payment.time, "sleep", sleeps.append)

    url = payment.generate_plus_link(
        SimpleNamespace(access_token="access-token", cookies="", extra={}),
        country="ID",
        currency="IDR",
        use_short_link=True,
        response_log=logs.append,
    )

    assert url.endswith("cs_live_retryvalue")
    assert len(calls) == 2
    assert sleeps == [0.5]
    assert len(session.posts) == 1
    assert any("Sentinel 网络错误（第 1/3 次）" in item for item in logs)


def test_checkout_sentinel_network_retry_exhaustion(monkeypatch):
    calls = []
    sleeps = []

    def fail_sentinel(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("curl: (35) TLS connect error")

    monkeypatch.setattr(
        sentinel_quickjs,
        "get_sentinel_tokens_via_quickjs",
        fail_sentinel,
    )
    monkeypatch.setattr(payment.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="网络请求重试 3 次后仍失败"):
        payment._build_checkout_sentinel_headers(
            object(),
            device_id="device-id",
            country="ID",
            client_version="",
            log=None,
        )

    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_checkout_sentinel_protocol_error_is_not_retried(monkeypatch):
    calls = []
    sleeps = []

    def fail_sentinel(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("SDK timeOrigin mismatch")

    monkeypatch.setattr(
        sentinel_quickjs,
        "get_sentinel_tokens_via_quickjs",
        fail_sentinel,
    )
    monkeypatch.setattr(payment.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="SDK timeOrigin mismatch"):
        payment._build_checkout_sentinel_headers(
            object(),
            device_id="device-id",
            country="ID",
            client_version="",
            log=None,
        )

    assert len(calls) == 1
    assert sleeps == []


def test_generate_plus_link_preserves_checkout_error_detail(monkeypatch):
    session = _install_checkout_session(monkeypatch, _CheckoutResponse())
    account = SimpleNamespace(access_token="access-token", cookies="", extra={})
    logs = []

    with pytest.raises(payment.ChatGPTCheckoutError) as caught:
        payment.generate_plus_link(
            account,
            country="ID",
            currency="IDR",
            use_short_link=True,
            response_log=logs.append,
        )

    captured = session.posts[0][1]
    assert caught.value.status_code == 400
    assert "unusual activity" in str(caught.value).lower()
    assert "access-token" not in str(caught.value)
    assert captured["headers"]["Origin"] == "https://chatgpt.com"
    assert captured["headers"]["Referer"] == "https://chatgpt.com/"
    assert captured["headers"]["openai-sentinel-token"] == "sentinel-token"
    assert session.warmups and session.closed
    assert logs and "原始响应（已脱敏，HTTP 400）" in logs[-1]
    assert "unusual activity" in logs[-1].lower()


def test_generate_plus_link_uses_sentinel_and_response_processor_entity(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        text="",
        json=lambda: {
            "checkout_session_id": "cs_live_testvalue",
            "processor_entity": "openai_ie",
            "checkout_ui_mode": "custom",
        },
    )
    session = _install_checkout_session(monkeypatch, response)
    account = SimpleNamespace(
        access_token="access-token",
        cookies="oai-did=device-old; oai-did=device-current",
        chatgpt_account_id="account-id",
        extra={},
    )

    url = payment.generate_plus_link(
        account,
        country="ID",
        currency="IDR",
        use_short_link=True,
    )

    request = session.posts[0][1]
    assert url == "https://chatgpt.com/checkout/openai_ie/cs_live_testvalue"
    assert request["headers"]["Chatgpt-Account-Id"] == "account-id"
    assert request["headers"]["oai-device-id"] == "device-current"
    assert request["headers"]["x-openai-target-path"] == "/backend-api/payments/checkout"
    assert "cookie" not in request["headers"]
    assert request["json"]["entry_point"] == "all_plans_pricing_modal"
    assert request["json"]["checkout_ui_mode"] == "custom"
    assert request["json"]["check_card_proxy"] is True
    assert request["json"]["cancel_url"] == "https://chatgpt.com/"


def test_checkout_response_log_redacts_sensitive_values():
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "checkout_session_id": "cs_live_sensitivevalue",
            "url": "https://pay.openai.com/c/pay/cs_live_sensitivevalue",
            "access_token": "secret-access-token",
            "nested": {
                "email": "person@example.test",
                "phone": "+628123456789",
                "proxy": "http://user:pass@proxy.example.test:8080/path",
            },
            "payment_method_types": ["card", "gopay"],
        },
        text="",
    )

    rendered = payment._sanitize_checkout_response(response)

    assert "secret-access-token" not in rendered
    assert "cs_live_sensitivevalue" not in rendered
    assert "person@example.test" not in rendered
    assert "+628123456789" not in rendered
    assert "user:pass" not in rendered
    assert "payment_method_types" in rendered
    assert "gopay" in rendered


def test_checkout_unusual_activity_refreshes_auth_once(monkeypatch):
    built_account = SimpleNamespace(
        email="masked@example.test",
        token="old-token",
        extra={"access_token": "old-token", "cookies": ""},
    )
    refreshed_account = SimpleNamespace(
        email="masked@example.test",
        token="new-token",
        extra={"access_token": "new-token", "cookies": ""},
    )
    monkeypatch.setattr(flow, "build_platform_account", lambda *_args: built_account)
    refreshes = []
    monkeypatch.setattr(
        flow,
        "_refresh_chatgpt_checkout_auth",
        lambda *_args, **_kwargs: refreshes.append("refresh") or refreshed_account,
    )
    calls = []
    generated_kwargs = []

    def generate(account, **_kwargs):
        calls.append(account.access_token)
        generated_kwargs.append(_kwargs)
        if len(calls) == 1:
            raise payment.ChatGPTCheckoutError(
                400, "Our systems have detected unusual activity. Please try again later."
            )
        return "https://chatgpt.com/checkout/openai_llc/test-session"

    monkeypatch.setattr(payment, "generate_plus_link", generate)

    checkout_context = {}
    url = flow.step_generate_cashier_url(
        SimpleNamespace(),
        country="ID",
        currency="IDR",
        proxy="http://proxy.example.test",
        use_short_link=True,
        checkout_context=checkout_context,
        log=lambda _message: None,
    )

    assert url.endswith("test-session")
    assert calls == ["old-token", "new-token"]
    assert refreshes == ["refresh"]
    assert all(item["expected_exit_country"] == "ID" for item in generated_kwargs)
    assert all(item["checkout_context"] is checkout_context for item in generated_kwargs)


def test_checkout_unusual_activity_is_classified_by_status_and_detail():
    assert flow._is_checkout_unusual_activity(
        payment.ChatGPTCheckoutError(
            400, "Our systems have detected unusual activity. Please try again later."
        )
    )
    assert not flow._is_checkout_unusual_activity(
        payment.ChatGPTCheckoutError(400, "invalid billing country")
    )

def _successful_checkout_response():
    return SimpleNamespace(
        status_code=200,
        text="",
        json=lambda: {
            "checkout_session_id": "cs_live_exitcheck",
            "processor_entity": "openai_llc",
            "checkout_ui_mode": "custom",
        },
    )


def test_generate_plus_link_records_indonesia_exit_context(monkeypatch):
    session = _install_checkout_session(monkeypatch, _successful_checkout_response())
    account = SimpleNamespace(access_token="access-token", cookies="", extra={})
    checkout_context = {}
    logs = []

    url = payment.generate_plus_link(
        account,
        proxy="http://user:pass@proxy.example.test:8080",
        country="ID",
        currency="IDR",
        use_short_link=True,
        response_log=logs.append,
        expected_exit_country="ID",
        checkout_context=checkout_context,
    )

    assert url.endswith("cs_live_exitcheck")
    assert checkout_context == {
        "exit_ip": "180.246.204.135",
        "exit_country": "ID",
        "exit_source": payment.CHECKOUT_EXIT_TRACE_URL,
    }
    assert session.warmups[0][0] == payment.CHECKOUT_EXIT_TRACE_URL
    assert session.posts
    assert any("提链出口校验通过" in item for item in logs)
    assert all("user:pass" not in item for item in logs)


def test_generate_plus_link_rejects_non_indonesia_exit(monkeypatch):
    session = _install_checkout_session(
        monkeypatch,
        _successful_checkout_response(),
        trace_text="ip=203.0.113.8\nloc=US",
    )
    account = SimpleNamespace(access_token="access-token", cookies="", extra={})

    with pytest.raises(RuntimeError, match="提链出口国家校验失败"):
        payment.generate_plus_link(
            account,
            proxy="http://proxy.example.test:8080",
            country="ID",
            currency="IDR",
            use_short_link=True,
            expected_exit_country="ID",
            checkout_context={},
        )

    assert not session.posts
    assert session.closed


class _ProbeBody:
    def __init__(self, page):
        self.page = page

    def inner_text(self, timeout=0):
        return self.page.body_text


class _ProbePage:
    def __init__(self, trace_text):
        self.trace_text = trace_text
        self.body_text = ""
        self.visited = []

    def goto(self, url, **_kwargs):
        self.visited.append(url)
        if url == payment.CHECKOUT_EXIT_TRACE_URL:
            self.body_text = self.trace_text
        elif "ipify" in url:
            self.body_text = '{"ip":"198.51.100.9"}'
        else:
            self.body_text = '{"origin":"198.51.100.9"}'

    def locator(self, _selector):
        return _ProbeBody(self)


def test_browser_exit_probe_accepts_matching_indonesia_ip():
    logs = []
    page = _ProbePage("ip=180.246.204.135\nloc=ID")

    result = payment._probe_camoufox_proxy_exit(
        page,
        log=logs.append,
        expected_country="ID",
        expected_ip="180.246.204.135",
        proxy="http://user:pass@proxy.example.test:8080",
    )

    assert result["ok"] is True
    assert result["country"] == "ID"
    assert result["ip"] == "180.246.204.135"
    assert page.visited == [payment.CHECKOUT_EXIT_TRACE_URL]
    assert all("user:pass" not in item for item in logs)


@pytest.mark.parametrize(
    ("trace_text", "expected_ip", "message"),
    [
        ("ip=203.0.113.8\nloc=US", "203.0.113.8", "支付页出口国家校验失败"),
        ("ip=180.246.204.136\nloc=ID", "180.246.204.135", "出口 IP 不一致"),
    ],
)
def test_browser_exit_probe_rejects_country_or_ip_mismatch(
    trace_text, expected_ip, message
):
    page = _ProbePage(trace_text)

    with pytest.raises(RuntimeError, match=message):
        payment._probe_camoufox_proxy_exit(
            page,
            log=lambda _message: None,
            expected_country="ID",
            expected_ip=expected_ip,
            proxy="http://proxy.example.test:8080",
        )


def test_step_grab_midtrans_forwards_expected_exit(monkeypatch):
    from platforms import _browser_backend

    backend = SimpleNamespace(backend="camoufox", window_mode="headed")
    monkeypatch.setattr(_browser_backend, "parse_checkout_mode", lambda *_args, **_kwargs: backend)
    captured = {}

    def select(_cashier_url, **kwargs):
        captured.update(kwargs)
        return "https://app.midtrans.com/snap/v4/redirection/test-token"

    monkeypatch.setattr(payment, "select_gopay_and_grab_midtrans", select)

    result = flow.step_grab_midtrans_url(
        "https://chatgpt.com/checkout/openai_llc/test-session",
        proxy="http://proxy.example.test:8080",
        expected_exit_country="ID",
        expected_exit_ip="180.246.204.135",
        log=lambda _message: None,
    )

    assert result.endswith("test-token")
    assert captured["expected_exit_country"] == "ID"
    assert captured["expected_exit_ip"] == "180.246.204.135"
    assert captured["proxy"] == "http://proxy.example.test:8080"


def test_gopay_checkout_requires_task_proxy_before_account_lookup():
    with pytest.raises(RuntimeError, match="任务代理池"):
        flow.execute_gopay_pay_chatgpt(chatgpt_account_id=3265, proxy=None)
