"""纯协议 GoPay 提链模块的单元测试（假 Transport，无真实 HTTP）。"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from platforms.chatgpt import gopay_link_protocol as proto


PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
CPMT = "cpmt_abc123XYZ"
OAICS = "oaics_abc123XYZ"
CS_LIVE = "cs_live_abc123XYZ"
MIDTRANS = (
    "https://app.midtrans.com/snap/v4/redirection/"
    "11111111-1111-1111-1111-111111111111"
)

BILLING = {
    "name": "James Smith",
    "email": "buyer@example.com",
    "line1": "Jalan M.H. Thamrin No. 1",
    "city": "Jakarta",
    "state": "DKI Jakarta",
    "postal_code": "10310",
}


class _QueueTransport:
    """按顺序消费脚本化响应；记录收到的 RequestSpec 便于断言调用序列。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"意外多余的请求: {request.stage}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _oaics_fetch_payload():
    return {
        "checkout_session_id": OAICS,
        "processor_entity": "openai_ie",
        "stripe_publishable_key": PK,
        "billing_details": {"country": "ID", "currency": "IDR"},
        "checkout_state": {"total": {"total": {"minorUnitsAmount": "349000"}}},
        "payment_method_types": ["gopay"],
        "custom_payment_methods": [{"id": CPMT}],
    }


def _oaics_responses():
    return [
        _oaics_fetch_payload(),  # checkout.create
        _oaics_fetch_payload(),  # checkout.fetch (权威金额/方法)
        {  # stripe.elements.sessions
            "config_id": "config_xyz",
            "custom_payment_method_data": [
                {"type": CPMT, "display_name": "GoPay"}
            ],
        },
        {},  # checkout.taxes
        _oaics_fetch_payload(),  # checkout.fetch (税后)
        {"status": "success"},  # checkout.custom_confirm
        {  # checkout.custom_payment_method.start
            "status": "requires_action",
            "next_action": {"url": MIDTRANS},
        },
    ]


def test_oaics_branch_extracts_midtrans_url():
    transport = _QueueTransport(_oaics_responses())
    result = proto.extract_gopay_payment_link(
        transport,
        plan_name="chatgptplusplan",
        billing=BILLING,
        coupon_id="none",
        expected_amount="349000",
        stripe_js_id="stripe-js-test",
    )

    assert result["provider_redirect_url"] == MIDTRANS
    assert result["long_url"] == MIDTRANS
    assert result["gopay_redirect_url"] == MIDTRANS
    assert result["payment_link_type"] == "gopay_custom_payment_method_redirect"
    assert result["payment_method_type"] == "gopay"
    assert result["checkout_session_id"] == OAICS
    assert result["custom_payment_method_type_id"] == CPMT
    assert result["currency"] == "IDR"

    stages = [req.stage for req in transport.requests]
    assert stages == [
        "checkout.create",
        "checkout.fetch",
        "stripe.elements.sessions",
        "checkout.taxes",
        "checkout.fetch",
        "checkout.custom_confirm",
        "checkout.custom_payment_method.start",
    ]


def test_oaics_trace_reports_stage_state_amount_and_provider_host_without_secrets():
    traces = []
    result = proto.extract_gopay_payment_link(
        _QueueTransport(_oaics_responses()),
        plan_name="chatgptplusplan",
        billing=BILLING,
        expected_amount="349000",
        stripe_js_id="stripe-js-test",
        trace=traces.append,
    )

    assert result["provider_redirect_url"] == MIDTRANS
    assert any("stage=checkout.create" in item for item in traces)
    assert any("stage=checkout.custom_payment_method.start" in item for item in traces)
    assert any("amount=349000" in item for item in traces)
    assert any("state=requires_action" in item for item in traces)
    assert any("provider_hosts=['app.midtrans.com']" in item for item in traces)
    assert all(OAICS not in item for item in traces)
    assert all(MIDTRANS not in item for item in traces)


def _cs_responses():
    return [
        {  # checkout.create
            "checkout_session_id": CS_LIVE,
            "processor_entity": "openai_llc",
            "stripe_publishable_key": PK,
            "billing_details": {"country": "ID", "currency": "IDR"},
            "payment_method_types": ["gopay"],
        },
        {  # stripe.init
            "stripe_hosted_url": f"https://checkout.stripe.com/c/pay/{CS_LIVE}",
            "payment_method_types": ["gopay", "card"],
            "total_summary": {"due": "349000"},
            "init_checksum": "checksum_xyz",
            "config_id": "config_xyz",
            "elements_session_id": "elements_xyz",
            "locale": "en",
        },
        {"id": "pm_abc123XYZ"},  # stripe.payment_methods
        {  # stripe.confirm
            "type": "redirect_to_url",
            "redirect_to_url": {"url": MIDTRANS},
        },
    ]


def test_standard_cs_branch_extracts_provider_redirect():
    transport = _QueueTransport(_cs_responses())
    result = proto.extract_gopay_payment_link(
        transport,
        plan_name="chatgptplusplan",
        billing=BILLING,
        coupon_id="none",
        expected_amount="349000",
        stripe_js_id="stripe-js-test",
    )

    assert result["provider_redirect_url"] == MIDTRANS
    assert result["payment_link_type"] == "gopay_redirect"
    assert result["payment_method_id"] == "pm_abc123XYZ"
    assert result["checkout_session_id"] == CS_LIVE

    stages = [req.stage for req in transport.requests]
    assert stages == [
        "checkout.create",
        "stripe.init",
        "stripe.payment_methods",
        "stripe.confirm",
    ]


def test_standard_cs_branch_ignores_openai_return_and_reads_payment_page_redirect():
    responses = [
        {
            "checkout_session_id": CS_LIVE,
            "processor_entity": "openai_llc",
            "stripe_publishable_key": PK,
            "billing_details": {"country": "ID", "currency": "IDR"},
            "payment_method_types": ["gopay"],
        },
        {
            "stripe_hosted_url": f"https://checkout.stripe.com/c/pay/{CS_LIVE}",
            "payment_method_types": ["gopay"],
            "total_summary": {"due": "349000"},
            "init_checksum": "checksum_xyz",
            "config_id": "config_xyz",
            "elements_session_id": "elements_xyz",
            "locale": "en",
        },
        {"id": "pm_abc123XYZ"},
        {
            "status": "requires_action",
            "next_action": {"redirect_to_url": {"url": "https://openai.com/"}},
        },
        {
            "status": "requires_action",
            "next_action": {"redirect_to_url": {"url": MIDTRANS}},
        },
    ]
    result = proto.extract_gopay_payment_link(
        _QueueTransport(responses),
        plan_name="chatgptplusplan",
        billing=BILLING,
        expected_amount="349000",
        stripe_js_id="stripe-js-test",
    )

    assert result["provider_redirect_url"] == MIDTRANS
    assert result["candidate_urls"] == [MIDTRANS]


def test_unknown_session_type_raises():
    transport = _QueueTransport([{"checkout_session_id": "weird_session_1"}])
    with pytest.raises(ValueError, match="缺少有效 checkout_session_id"):
        proto.extract_gopay_payment_link(
            transport,
            plan_name="chatgptplusplan",
            billing=BILLING,
        )


def test_oaics_missing_custom_methods_raises():
    payload = _oaics_fetch_payload()
    payload.pop("custom_payment_methods")
    transport = _QueueTransport([payload, payload])
    with pytest.raises(ValueError, match="缺少 custom_payment_methods"):
        proto.extract_gopay_payment_link(
            transport,
            plan_name="chatgptplusplan",
            billing=BILLING,
            coupon_id="none",
        )


def test_elements_not_mapped_to_gopay_raises():
    responses = [
        _oaics_fetch_payload(),  # create
        _oaics_fetch_payload(),  # fetch
        {  # elements：没有 GoPay 映射
            "custom_payment_method_data": [
                {"type": CPMT, "display_name": "Credit Card"}
            ],
        },
    ]
    transport = _QueueTransport(responses)
    with pytest.raises(ValueError, match="未映射到 GoPay"):
        proto.extract_gopay_payment_link(
            transport,
            plan_name="chatgptplusplan",
            billing=BILLING,
            coupon_id="none",
        )


def test_start_without_requires_action_raises():
    responses = [
        _oaics_fetch_payload(),  # create
        _oaics_fetch_payload(),  # fetch
        {"custom_payment_method_data": [{"type": CPMT, "display_name": "GoPay"}]},
        {},  # taxes
        _oaics_fetch_payload(),  # fetch
        {"status": "success"},  # confirm
        {"status": "failed"},  # start：未产生支付链接
    ]
    transport = _QueueTransport(responses)
    with pytest.raises(ValueError, match="未产生浏览器支付链接"):
        proto.extract_gopay_payment_link(
            transport,
            plan_name="chatgptplusplan",
            billing=BILLING,
            coupon_id="none",
        )


def test_is_provider_payment_url_excludes_stripe_and_fastly():
    assert proto.is_provider_payment_url(MIDTRANS) is True
    assert proto.is_provider_payment_url(
        "https://stripe-camo.global.ssl.fastly.net/abc/68747470733a2"
    ) is False
    assert proto.is_provider_payment_url("https://checkout.stripe.com/c/pay/cs_x") is False
    assert proto.is_provider_payment_url("https://api.stripe.com/v1/anything") is False
    assert proto.is_provider_payment_url("https://js.stripe.com/v3") is False
    assert proto.is_provider_payment_url("https://chatgpt.com/checkout/x/1") is False
    assert proto.is_provider_payment_url("https://pay.openai.com/c/pay/cs_x") is False
    assert proto.is_provider_payment_url("https://openai.com") is False
    assert proto.is_provider_payment_url("https://accounts.openai.com/login") is False
    assert proto.is_provider_payment_url("https://foo.fastly.net/asset.png") is False
    assert proto.is_provider_payment_url("https://app.midtrans.com/snap/v4/redirection/x") is True


def _camo_url(real: str, host: str = "d1wqzb5bdbcre6.cloudfront.net") -> str:
    hexed = real.encode("utf-8").hex()
    return f"https://{host}/87717978f78695d079dad368f8522493eb0724aceb7a4a24f2782025d5846c62/{hexed}"


def test_decode_camo_url_recovers_wrapped_url():
    camo = _camo_url(MIDTRANS)
    assert proto._decode_camo_url(camo) == MIDTRANS
    assert proto.is_provider_payment_url(camo) is False
    assert proto._resolve_provider_url(camo) == MIDTRANS


def test_redirect_extractor_unwraps_json_and_url_encoded_provider_url():
    payload = {
        "status": "requires_action",
        "next_action": json.dumps({"redirect_to_url": {"url": quote(MIDTRANS)}}),
        "return_url": "https://openai.com/",
    }
    assert proto.extract_stripe_provider_redirect(payload) == MIDTRANS


def test_redirect_extractor_rejects_openai_business_url():
    payload = {
        "status": "requires_action",
        "next_action": {"redirect_to_url": {"url": "https://openai.com/"}},
        "account_settings": {"business_url": "https://openai.com/"},
    }
    assert proto.extract_stripe_provider_redirect(payload) == ""


def test_cs_branch_extracts_camo_wrapped_provider_redirect():
    camo = _camo_url(MIDTRANS, host="stripe-camo.global.ssl.fastly.net")
    responses = [
        {
            "checkout_session_id": CS_LIVE,
            "processor_entity": "openai_llc",
            "stripe_publishable_key": PK,
            "billing_details": {"country": "ID", "currency": "IDR"},
            "payment_method_types": ["gopay"],
        },
        {
            "stripe_hosted_url": f"https://checkout.stripe.com/c/pay/{CS_LIVE}",
            "payment_method_types": ["gopay"],
            "total_summary": {"due": "349000"},
            "init_checksum": "checksum_xyz",
            "config_id": "cfg",
            "elements_session_id": "e1",
            "locale": "en",
        },
        {"id": "pm_abc123XYZ"},
        {"type": "redirect_to_url", "redirect_to_url": {"url": camo}},
    ]
    transport = _QueueTransport(responses)
    result = proto.extract_gopay_payment_link(
        transport,
        plan_name="chatgptplusplan",
        billing=BILLING,
        coupon_id="none",
        expected_amount="349000",
        stripe_js_id="stripe-js-test",
    )
    assert result["provider_redirect_url"] == MIDTRANS
    assert result["payment_link_type"] == "gopay_redirect"


def test_oaics_result_exposes_candidate_urls():
    transport = _QueueTransport(_oaics_responses())
    result = proto.extract_gopay_payment_link(
        transport,
        plan_name="chatgptplusplan",
        billing=BILLING,
        coupon_id="none",
        stripe_js_id="stripe-js-test",
    )
    assert MIDTRANS in result.get("candidate_urls", [])


def test_validate_coupon_amounts_100_percent_free():
    result = proto.validate_coupon_amounts("plus-1-month-free", "349000", "0")
    assert result["discount_check"] == "passed"
    assert result["discount_percentage"] == 100


def test_validate_coupon_amounts_50_percent():
    result = proto.validate_coupon_amounts("plus-1-month-50-pct-off", "349000", "174500")
    assert result["discount_check"] == "passed"
    assert result["discount_percentage"] == 50


def test_validate_coupon_amounts_rejects_wrong_discount():
    with pytest.raises(ValueError, match="优惠比例校验失败"):
        proto.validate_coupon_amounts("plus-1-month-50-pct-off", "349000", "300000")


def _from_checkout_oaics_responses():
    return [
        _oaics_fetch_payload(),  # checkout.fetch（入口拉权威 checkout）
        _oaics_fetch_payload(),  # checkout.fetch（OAICS 尾部）
        {  # stripe.elements.sessions
            "config_id": "config_xyz",
            "custom_payment_method_data": [
                {"type": CPMT, "display_name": "GoPay"}
            ],
        },
        {},  # checkout.taxes
        _oaics_fetch_payload(),  # checkout.fetch（税后）
        {"status": "success"},  # checkout.custom_confirm
        {  # checkout.custom_payment_method.start
            "status": "requires_action",
            "next_action": {"url": MIDTRANS},
        },
    ]


def test_extract_from_checkout_oaics_route():
    transport = _QueueTransport(_from_checkout_oaics_responses())
    result = proto.extract_gopay_payment_link_from_checkout(
        transport,
        checkout_session_id=OAICS,
        processor_entity="openai_ie",
        plan_name="chatgptplusplan",
        billing=BILLING,
        coupon_id="none",
        expected_amount="349000",
        stripe_js_id="stripe-js-test",
    )

    assert result["provider_redirect_url"] == MIDTRANS
    assert result["payment_link_type"] == "gopay_custom_payment_method_redirect"
    assert result["checkout_session_id"] == OAICS

    # 入口只发 checkout.fetch（不 create），随后复用 OAICS 尾部。
    stages = [req.stage for req in transport.requests]
    assert stages[0] == "checkout.fetch"
    assert "checkout.create" not in stages
    # 首条 fetch 请求的 URL 应指向既有会话。
    first = transport.requests[0]
    assert f"/checkout/openai_ie/{OAICS}" in first.url


def test_extract_from_checkout_cs_route():
    responses = [
        {  # checkout.fetch（入口拉权威 checkout）
            "checkout_session_id": CS_LIVE,
            "processor_entity": "openai_llc",
            "stripe_publishable_key": PK,
            "billing_details": {"country": "ID", "currency": "IDR"},
            "payment_method_types": ["gopay"],
        },
        {  # stripe.init
            "stripe_hosted_url": f"https://checkout.stripe.com/c/pay/{CS_LIVE}",
            "payment_method_types": ["gopay", "card"],
            "total_summary": {"due": "349000"},
            "init_checksum": "checksum_xyz",
            "config_id": "config_xyz",
            "elements_session_id": "elements_xyz",
            "locale": "en",
        },
        {"id": "pm_abc123XYZ"},  # stripe.payment_methods
        {  # stripe.confirm
            "type": "redirect_to_url",
            "redirect_to_url": {"url": MIDTRANS},
        },
    ]
    transport = _QueueTransport(responses)
    result = proto.extract_gopay_payment_link_from_checkout(
        transport,
        checkout_session_id=CS_LIVE,
        processor_entity="openai_llc",
        plan_name="chatgptplusplan",
        billing=BILLING,
        coupon_id="none",
        expected_amount="349000",
        stripe_js_id="stripe-js-test",
    )

    assert result["provider_redirect_url"] == MIDTRANS
    assert result["payment_link_type"] == "gopay_redirect"
    assert result["payment_method_id"] == "pm_abc123XYZ"

    stages = [req.stage for req in transport.requests]
    assert stages[0] == "checkout.fetch"
    assert "checkout.create" not in stages
    assert stages[-1] == "stripe.confirm"


def test_extract_from_checkout_oaics_missing_key_raises():
    # oaics_ 会话缺 pk / 权威金额时应在 elements 阶段报错。
    payload = _oaics_fetch_payload()
    payload.pop("stripe_publishable_key")
    transport = _QueueTransport([payload, payload])
    with pytest.raises(ValueError, match="Stripe publishable key"):
        proto.extract_gopay_payment_link_from_checkout(
            transport,
            checkout_session_id=OAICS,
            processor_entity="openai_ie",
            plan_name="chatgptplusplan",
            billing=BILLING,
            coupon_id="none",
        )


def test_extract_from_checkout_unknown_session_raises():
    transport = _QueueTransport([])
    with pytest.raises(ValueError, match="缺少有效 checkout_session_id"):
        proto.extract_gopay_payment_link_from_checkout(
            transport,
            checkout_session_id="weird_session_1",
            processor_entity="openai_llc",
            plan_name="chatgptplusplan",
            billing=BILLING,
        )
