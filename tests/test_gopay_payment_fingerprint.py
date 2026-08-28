"""测 payment_fingerprint 体系：指纹构造、请求头注入、漂移校验。

不真实联网 —— 全部用最小构造 / 直接调用模块函数覆盖。
"""
from __future__ import annotations

import pytest

from platforms.gopay._opai_loader import ensure_opai_on_path

ensure_opai_on_path()

from opai.core.payment_fingerprint import (  # noqa: E402
    build_payment_fingerprint,
    ensure_account_payment_fingerprint,
    normalize_payment_fingerprint,
    payment_fingerprint_headers,
)
from opai.core.gopay_payment_protocol import (  # noqa: E402
    GoPayPayment,
    GoPayPaymentError,
)


def _profile(phone="8123456789", local="8123456789", account_id="acct-1"):
    return build_payment_fingerprint(
        seed="deterministic", phone=phone, local=local, account_id=account_id
    )


def test_build_payment_fingerprint_is_deterministic_and_complete():
    a = _profile()
    b = _profile()
    assert a["version"] == 1
    assert a["profile_id"]
    assert len(a["profile_id"]) == 16
    # 同一 seed 派生恒等
    assert a == b
    # 全部关键头字段存在
    for key in (
        "user_agent",
        "locale",
        "timezone",
        "sec_ch_ua",
        "sec_ch_ua_mobile",
        "sec_ch_ua_platform",
    ):
        assert key in a and a[key]


def test_build_payment_fingerprint_differs_across_accounts():
    a = _profile(phone="8123456789")
    b = _profile(phone="8123456790")
    assert a["profile_id"] != b["profile_id"]


def test_normalize_preserves_saved_values_and_fills_gaps():
    saved = {"user_agent": "Custom-UA", "profile_id": "abc123"}
    normalized = normalize_payment_fingerprint(saved, phone="8123456789")
    assert normalized["user_agent"] == "Custom-UA"
    assert normalized["profile_id"] == "abc123"
    # 缺失字段回退到派生值
    assert normalized["timezone"] == "Asia/Jakarta"
    assert normalized["viewport"]["width"] > 0


def test_normalize_none_or_non_dict_falls_back():
    fallback = normalize_payment_fingerprint(None, phone="8123456789")
    assert isinstance(fallback, dict)
    assert fallback["profile_id"]
    fallback2 = normalize_payment_fingerprint("bad", phone="8123456789")
    assert fallback2["profile_id"]


def test_payment_fingerprint_headers_include_fingerprint_fields():
    headers = payment_fingerprint_headers(_profile())
    # 指纹字段被注入
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "Sec-CH-UA" in headers
    assert "Sec-CH-UA-Platform" in headers
    assert "Accept-Language" in headers
    assert "X-Timezone" in headers
    assert "Viewport-Width" in headers


def test_ensure_account_payment_fingerprint_sets_account_deterministically():
    account = {"phone": "8123456789", "local": "8123456789", "account_id": "acct-1"}
    profile = ensure_account_payment_fingerprint(account)
    assert account["payment_fingerprint"] == profile
    assert profile["profile_id"]
    # 再次调用应复用（不变）
    profile2 = ensure_account_payment_fingerprint(account)
    assert profile2 == profile


def test_gopay_payment_constructor_injects_fingerprint_headers():
    payment = GoPayPayment(proxy="", payment_fingerprint=_profile())
    assert isinstance(payment.payment_fingerprint, dict)
    assert payment.profile_id
    assert payment._headers["User-Agent"].startswith("Mozilla/5.0")
    # 漂移期望已建立
    assert payment._fingerprint_expectations["User-Agent"] == payment._headers["User-Agent"]


def test_request_headers_pass_when_untouched():
    payment = GoPayPayment(proxy="", payment_fingerprint=_profile())
    headers = payment._request_headers({"Origin": "https://x.example"})
    assert headers["Origin"] == "https://x.example"
    assert headers["User-Agent"] == payment._headers["User-Agent"]


def test_drift_check_raises_when_fingerprint_header_mutated():
    payment = GoPayPayment(proxy="", payment_fingerprint=_profile())
    bad = {**payment._headers, "User-Agent": "Spoofed-Agent"}
    with pytest.raises(GoPayPaymentError) as exc:
        payment._assert_fingerprint_headers(bad)
    assert "payment fingerprint drift" in str(exc.value)
    assert "User-Agent" in str(exc.value)


def test_drift_check_raises_via_request_headers():
    payment = GoPayPayment(proxy="", payment_fingerprint=_profile())
    with pytest.raises(GoPayPaymentError):
        payment._request_headers({"Viewport-Width": "9999"})
