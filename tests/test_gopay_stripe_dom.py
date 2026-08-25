import pytest

from platforms.chatgpt import payment


class _BodyLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self, timeout=0):
        return self.text


class _AmountPage:
    def __init__(self, text):
        self.text = text

    def locator(self, selector):
        assert selector == "body"
        return _BodyLocator(self.text)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("349,000.00", 349000.0),
        ("349.000,00", 349000.0),
        ("20.00", 20.0),
        ("349,000", 349000.0),
        ("0.00", 0.0),
    ],
)
def test_parse_checkout_amount_number_handles_stripe_locales(raw, expected):
    assert payment._parse_checkout_amount_number(raw) == expected


def test_verify_checkout_amount_nonzero_detects_idr_long_link_dom():
    page = _AmountPage(
        "Subscribe to ChatGPT Plus Subscription IDR 349,000.00 per month "
        "ChatGPT Plus Subscription IDR 349,000.00 per seat"
    )
    logs = []

    payment._verify_checkout_amount_nonzero(page, log=logs.append)

    assert any("金额校验通过" in item for item in logs)
    assert any("349000.00" in item for item in logs)


def test_verify_checkout_amount_nonzero_rejects_zero_idr():
    page = _AmountPage("Subscribe IDR 0.00 per month")

    with pytest.raises(RuntimeError, match="页面所有金额都为 0"):
        payment._verify_checkout_amount_nonzero(page, log=lambda _message: None)


class _FlowPage:
    def __init__(self, url):
        self.url = url

    def wait_for_timeout(self, _timeout):
        return None


def _install_ready_gopay_flow(monkeypatch, calls, *, missing=None):
    monkeypatch.setattr(payment, "_wait_checkout_page_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(payment, "_verify_checkout_amount_nonzero", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(payment, "_click_gopay_payment_method", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(payment, "_wait_checkout_billing_form_ready", lambda *_args, **_kwargs: True)

    def fill(_page, address, **_kwargs):
        calls["address"] = dict(address)
        return list(missing or [])

    monkeypatch.setattr(payment, "_fill_billing_until_complete", fill)
    monkeypatch.setattr(payment, "_accept_checkout_terms", lambda _page: True)


def test_grab_midtrans_fills_detected_stripe_billing_dom(monkeypatch):
    checkout_url = "https://pay.openai.com/c/pay/test-session"
    midtrans_url = (
        "https://app.midtrans.com/snap/v4/redirection/"
        "11111111-1111-1111-1111-111111111111"
    )
    page = _FlowPage(checkout_url)
    calls = {}
    _install_ready_gopay_flow(monkeypatch, calls)

    def submit(_page, **_kwargs):
        calls["submitted"] = True
        page.url = midtrans_url
        return True

    monkeypatch.setattr(payment, "_click_subscribe_button_burst", submit)
    address = {
        "name": "James Smith",
        "line1": "Jalan M.H. Thamrin No. 1",
        "city": "Jakarta",
        "state": "DKI Jakarta",
        "postal_code": "10310",
        "country": "ID",
    }

    result = payment._grab_midtrans_from_ready_page(
        page,
        checkout_url=checkout_url,
        address=address,
        timeout_seconds=30,
        log=lambda _message: None,
    )

    assert result == midtrans_url
    assert calls["address"] == address
    assert calls["submitted"] is True


def test_grab_midtrans_stops_before_submit_when_billing_dom_stays_empty(monkeypatch):
    checkout_url = "https://pay.openai.com/c/pay/test-session"
    page = _FlowPage(checkout_url)
    calls = {}
    _install_ready_gopay_flow(monkeypatch, calls, missing=["line1", "state"])
    monkeypatch.setattr(
        payment,
        "_click_subscribe_button_burst",
        lambda *_args, **_kwargs: calls.update(submitted=True),
    )

    with pytest.raises(RuntimeError, match="账单字段填写后仍缺失：line1,state"):
        payment._grab_midtrans_from_ready_page(
            page,
            checkout_url=checkout_url,
            address={"line1": "Jalan M.H. Thamrin No. 1", "state": "DKI Jakarta"},
            timeout_seconds=30,
            log=lambda _message: None,
        )

    assert "submitted" not in calls


class _VisibleLocator:
    def __init__(self, visible):
        self.first = self
        self.visible = visible

    def count(self):
        return 1 if self.visible else 0

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return True


class _ProcessingPage(_FlowPage):
    def __init__(self, url):
        super().__init__(url)
        self.processing = False

    def locator(self, selector):
        visible = self.processing and "submit-button-processing-label" in selector
        return _VisibleLocator(visible)


def test_submit_burst_stops_after_stripe_processing_dom_appears(monkeypatch):
    page = _ProcessingPage("https://pay.openai.com/c/pay/test-session")
    clicks = []
    logs = []

    def click(_page):
        clicks.append("click")
        page.processing = True
        return True

    monkeypatch.setattr(payment, "_click_subscribe_button", click)

    assert payment._click_subscribe_button_burst(
        page,
        checkout_url=page.url,
        log=logs.append,
        clicks=3,
        delay_ms=0,
    ) is True
    assert clicks == ["click"]
    assert any("停止重复点击" in item for item in logs)
