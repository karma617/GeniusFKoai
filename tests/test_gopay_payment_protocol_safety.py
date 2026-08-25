from __future__ import annotations

import json
from pathlib import Path

from platforms.gopay._opai_loader import ensure_opai_on_path

ensure_opai_on_path()
from opai.core.gopay_payment_protocol import GoPayPayment


SNAP_URL = "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111"


class FakePayment(GoPayPayment):
    def __init__(
        self, amount=0, currency="IDR", charge_error=False, transaction_body=None
    ):
        self.amount = amount
        self.currency = currency
        self.charge_error = charge_error
        self.transaction_body = transaction_body
        self.midtrans_posts = []

    def _sleep(self, _seconds, _cancel_check):
        return None

    def _midtrans_get(self, path, **_kwargs):
        if path.endswith("/gopay"):
            return {"status": 200, "body": {"account_status": "ENABLED"}}
        if self.transaction_body is not None:
            return {"status": 200, "body": self.transaction_body}
        return {
            "status": 200,
            "body": {
                "merchant": {"client_key": "client-key"},
                "transaction_details": {
                    "gross_amount": str(self.amount),
                    "currency": self.currency,
                },
            },
        }

    def _midtrans_post(self, path, body, **_kwargs):
        self.midtrans_posts.append(path)
        if path.endswith("/linking"):
            return {
                "status": 201,
                "body": {
                    "activation_link_url": "https://example.test/?reference=22222222-2222-2222-2222-222222222222"
                },
            }
        if path.endswith("/charge") and self.charge_error:
            raise TimeoutError("response lost")
        return {"status": 200, "body": {"transaction_status": "settlement"}}

    def _midtrans_delete(self, *_args, **_kwargs):
        return {"status": 200, "body": {}}

    def _gwa_post(self, path, _body, **_kwargs):
        if path == "/v1/linking/validate-otp":
            return {"status": 200, "body": {"challenge_id": "33333333-3333-3333-3333-333333333333"}}
        return {"status": 200, "body": {}}

    def _gwa_get(self, *_args, **_kwargs):
        return {"status": 200, "body": {}}

    def _pin_verify(self, *_args, **_kwargs):
        return "pin-token"


def _pay(payment, *, maximum=0):
    return payment.pay(
        SNAP_URL,
        phone="8123456789",
        country_code="62",
        pin="123456",
        wait_otp=lambda *_args: "1234",
        expected_currency="IDR",
        max_amount=maximum,
        require_zero_amount=maximum == 0,
        allow_one_idr_tokenization_verification=maximum == 0,
    )


def test_zero_limit_blocks_nonzero_amount_before_linking():
    payment = FakePayment(amount=1)
    result = _pay(payment, maximum=0)
    assert result["success"] is False
    assert "non-zero" in result["detail"]
    assert payment.midtrans_posts == []


def test_success_capture_allows_one_idr_enforced_tokenization_verification():
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "gopay_midtrans_tokenization_transaction.json"
    )
    transaction = json.loads(fixture_path.read_text(encoding="utf-8"))
    payment = FakePayment(transaction_body=transaction)

    result = _pay(payment, maximum=0)

    assert result["success"] is True
    assert result["amount"] == 1
    assert result["currency"] == "IDR"
    assert payment.midtrans_posts == [
        "/snap/v3/accounts/11111111-1111-1111-1111-111111111111/linking",
        "/snap/v2/transactions/11111111-1111-1111-1111-111111111111/charge",
    ]


def test_amount_or_currency_unavailable_fails_closed():
    payment = FakePayment(amount=None, currency="")
    result = _pay(payment, maximum=1000)
    assert result["success"] is False
    assert "amount unavailable" in result["detail"]
    assert payment.midtrans_posts == []


def test_charge_timeout_is_uncertain_and_keeps_snap_metadata():
    payment = FakePayment(amount=0, charge_error=True)
    result = _pay(payment, maximum=0)
    assert result["success"] is False
    assert result["uncertain"] is True
    assert result["charge_attempted"] is True
    assert result["snap"] == "11111111-1111-1111-1111-111111111111"


def test_cancel_check_stops_before_any_protocol_request():
    payment = FakePayment(amount=0)
    try:
        payment.pay(
            SNAP_URL,
            phone="8123456789",
            country_code="62",
            pin="123456",
            cancel_check=lambda: True,
            expected_currency="IDR",
            max_amount=0,
            require_zero_amount=True,
        )
    except Exception as exc:
        assert "cancelled" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected cancellation")
    assert payment.midtrans_posts == []


def test_log_redaction_masks_payment_secrets():
    from opai.core.log_redaction import redact_sensitive_log

    value = redact_sensitive_log(
        "phone=+628123456789 PIN=147258 otp=654321 access_token=secret "
        "proxy=http://user:pass@example.test:8080 {'refresh_token': 'json-secret'}"
    )
    assert "+628123456789" not in value
    assert "147258" not in value
    assert "654321" not in value
    assert "secret" not in value
    assert "json-secret" not in value
    assert "user:pass" not in value
    assert "***6789" in value
