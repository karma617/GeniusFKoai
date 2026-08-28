"""
GoPay Pure-Protocol Payment — 不需要浏览器。

完整 Midtrans GoPay 支付流程：
  Phase A: Linking（绑定 GoPay）
    1. POST /snap/v3/accounts/{snap}/linking      → reference
    2. POST /v1/linking/validate-reference         → 验证
    3. POST /v1/linking/user-consent               → 同意
    4. POST /v1/linking/resend-otp                 → 强制 SMS OTP
    5. POST /v1/linking/validate-otp               → 验证 OTP → challenge_id
    6. POST /api/v1/users/pin/tokens/nb            → PIN → pin_token (MGUPA)
    7. POST /v1/linking/validate-pin               → 提交 pin_token

  Phase B: Charge（扣款）
    8. GET  /snap/v3/accounts/{snap}/gopay         → 轮询直到 linked
    9. POST /snap/v2/transactions/{snap}/charge    → 扣款 → challenge reference

  Phase C: Challenge（支付确认）
    10. GET  /v1/payment/validate                  → 验证支付
    11. POST /v1/payment/confirm                   → 确认
    12. POST /api/v1/users/pin/tokens/nb           → PIN (GWC)
    13. POST /v1/payment/process                   → 最终处理

  Phase D: 验证
    14. GET  /snap/v1/transactions/{snap}/status   → 交易状态

来源：HAR 抓包 chatgpt.com.free.plus.gopay.har (2026-05-01)
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Optional, Callable

import tls_client

from .payment_fingerprint import normalize_payment_fingerprint, payment_fingerprint_headers
from .log_redaction import install_sensitive_log_filter

log = logging.getLogger(__name__)
install_sensitive_log_filter(log)

MIDTRANS_BASE = "https://app.midtrans.com"
GWA_BASE = "https://gwa.gopayapi.com"
CUSTOMER_BASE = "https://customer.gopayapi.com"

PIN_CLIENT_LINKING = "51b5f09a-3813-11ee-be56-0242ac120002-MGUPA"
PIN_CLIENT_PAYMENT = "47180a8e-f56e-11ed-a05b-0242ac120003-GWC"
GOPAY_TOKENIZATION_VERIFICATION_AMOUNT_IDR = 1
LINK_RETRY_LIMIT = 2
LINK_RETRY_SLEEP_S = 12.0

# X-Snap-Signature（Midtrans Snap 请求签名）。来自抓包文档 2026-06-02：
#   Signing Key : 1feab063-bf3f-4025-90bf-3be6fa4f4cc2
#   Payload     : {absolute_path}:{timestamp_ms}:{minified_json_body}
#   sig_hex     = HMAC-SHA256(key, payload)
#   Mangle      : 每 4 字符一组交换 [c0,c1,c2,c3] -> [c2,c3,c0,c1]
SNAP_SIGN_KEY = os.environ.get(
    "OPAI_MIDTRANS_SNAP_SIGN_KEY", "1feab063-bf3f-4025-90bf-3be6fa4f4cc2"
)


def _snap_mangle(sig_hex: str) -> str:
    """每 4 字符一组做 [c0,c1,c2,c3] -> [c2,c3,c0,c1] 交换；不足 4 的尾部原样保留。"""
    chars = list(sig_hex)
    length = len(chars)
    for i in range(0, length - 3, 4):
        r = chars[i]
        o = chars[i + 1]
        chars[i] = chars[i + 2]
        chars[i + 1] = chars[i + 3]
        chars[i + 2] = r
        chars[i + 3] = o
    return "".join(chars)


def _snap_signature(path: str, body_text: str, ts: str) -> str:
    """生成 X-Snap-Signature。

    path: ``/snap`` 前缀的绝对路径（不含 host、不含 query）
    body_text: 紧凑 JSON body（与实际发送字节一致）；GET/无 body 传空串
    ts: **秒级**时间戳字符串（与 X-Timestamp header 同值）
    """
    full_path = path if path.startswith("/snap") else f"/snap{path}"
    payload = f"{full_path}:{ts}:{body_text or ''}"
    sig_hex = hmac.new(
        SNAP_SIGN_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return _snap_mangle(sig_hex)


def _tls_proxy(proxy: str) -> str:
    """把 ``socks5h://`` 归一成 ``socks5://`` 供 tls_client 使用。

    tls_client 的 Go 后端不支持 ``socks5h`` scheme，会报
    "scheme socks5h is not supported"。socks5 在它下面默认远程 DNS，等价。
    其它 scheme（http/https/socks5）原样返回。
    """
    p = str(proxy or "").strip()
    if p.lower().startswith("socks5h://"):
        return "socks5://" + p[len("socks5h://"):]
    return p


class GoPayPaymentError(Exception):
    pass


class GoPayFraudDenyError(GoPayPaymentError):
    pass


class GoPayPayment:
    """纯协议 GoPay 支付。"""

    def __init__(self, proxy: str = "", payment_fingerprint: Optional[dict] = None):
        self._session = tls_client.Session(client_identifier="chrome_120")
        if proxy:
            # tls_client（Go 后端）只认 ``socks5://``，不认 ``socks5h://``
            # （会报 "scheme socks5h is not supported"）。注册侧用 httpx 需要
            # socks5h（远程 DNS），付款侧用 tls_client 这里归一成 socks5。
            # socks5 在 tls_client 下默认也走远程 DNS，等价可用。
            proxy = _tls_proxy(proxy)
            self._session.proxies = {"http": proxy, "https": proxy}
        # 稳定浏览器式支付指纹：请求头注入 + 漂移校验。不传则自动派生一个。
        self.payment_fingerprint = normalize_payment_fingerprint(payment_fingerprint)
        self._headers = payment_fingerprint_headers(self.payment_fingerprint)
        self._fingerprint_expectations = {
            key: self._headers.get(key, "")
            for key in (
                "User-Agent",
                "Accept-Language",
                "Sec-CH-UA",
                "Sec-CH-UA-Mobile",
                "Sec-CH-UA-Platform",
                "X-Timezone",
                "Viewport-Width",
            )
        }

    @property
    def profile_id(self) -> str:
        return str(self.payment_fingerprint.get("profile_id") or "")

    def _request_headers(self, extra: Optional[dict] = None) -> dict:
        headers = {**self._headers}
        if extra:
            headers.update(extra)
        self._assert_fingerprint_headers(headers)
        return headers

    def _assert_fingerprint_headers(self, headers: dict) -> None:
        for key, expected in self._fingerprint_expectations.items():
            if expected and headers.get(key) != expected:
                raise GoPayPaymentError(
                    f"payment fingerprint drift: {key} expected={expected!r} got={headers.get(key)!r}"
                )

    @staticmethod
    def _extract_challenge_id(body: dict) -> str:
        """从响应里递归找 challenge_id，兼容多种嵌套格式。"""
        if not isinstance(body, dict):
            return ""
        for key in ("challenge_id",):
            if body.get(key):
                return str(body[key])
        for key in ("data", "challenge", "action", "value"):
            nested = body.get(key)
            if isinstance(nested, dict):
                found = GoPayPayment._extract_challenge_id(nested)
                if found:
                    return found
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        found = GoPayPayment._extract_challenge_id(item)
                        if found:
                            return found
        return ""

    def _snap_headers(self, path: str, body_text: str = "", extra_headers: dict = None) -> dict:
        """生成 Midtrans Snap 请求头：X-Snap-Signature + X-Timestamp（秒）+ X-Source*。

        签名 payload 里的 timestamp 必须和 ``X-Timestamp`` header 完全一致；
        服务端还要求 X-Source / X-Source-App-Type / X-Source-Version 这组头。
        """
        ts = str(int(time.time()))  # **秒级**，不是毫秒
        h = dict(self._headers)
        try:
            h["X-Snap-Signature"] = _snap_signature(path, body_text, ts)
            h["X-Timestamp"] = ts
            h["X-Source"] = "snap"
            h["X-Source-App-Type"] = "redirection"
            h["X-Source-Version"] = "2.3.0"
        except Exception as exc:
            log.warning("X-Snap-Signature 生成失败（继续不带签名）: %s", exc)
        if extra_headers:
            h.update(extra_headers)
        self._assert_fingerprint_headers(h)
        return h

    def _midtrans_get(self, path: str, extra_headers: dict = None, timeout: int = 15) -> dict:
        url = f"{MIDTRANS_BASE}{path}"
        headers = self._snap_headers(path, "", extra_headers)
        r = self._session.get(url, headers=headers, timeout_seconds=timeout)
        log.debug("[MT GET] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _midtrans_post(self, path: str, body: dict, extra_headers: dict = None, timeout: int = 15) -> dict:
        url = f"{MIDTRANS_BASE}{path}"
        body_text = json.dumps(body, separators=(",", ":"))
        headers = self._snap_headers(path, body_text, extra_headers)
        r = self._session.post(url, headers=headers, data=body_text, timeout_seconds=timeout)
        log.debug("[MT POST] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _midtrans_delete(self, path: str, extra_headers: dict = None, timeout: int = 15) -> dict:
        url = f"{MIDTRANS_BASE}{path}"
        headers = self._snap_headers(path, "", extra_headers)
        r = self._session.delete(url, headers=headers, timeout_seconds=timeout)
        log.debug("[MT DELETE] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _gwa_post(self, path: str, body: dict, timeout: int = 15) -> dict:
        url = f"{GWA_BASE}{path}"
        headers = self._request_headers({"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"})
        r = self._session.post(url, headers=headers, data=json.dumps(body), timeout_seconds=timeout)
        log.debug("[GWA POST] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _gwa_get(self, path: str, timeout: int = 15) -> dict:
        url = f"{GWA_BASE}{path}"
        headers = self._request_headers({"Origin": "https://merchants-gws-app.gopayapi.com", "Referer": "https://merchants-gws-app.gopayapi.com/"})
        r = self._session.get(url, headers=headers, timeout_seconds=timeout)
        log.debug("[GWA GET] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _pin_verify(self, challenge_id: str, pin: str, client_id: str) -> str:
        """POST /api/v1/users/pin/tokens/nb → 返回 pin_token (JWT)。"""
        url = f"{CUSTOMER_BASE}/api/v1/users/pin/tokens/nb"
        body = {"challenge_id": challenge_id, "client_id": client_id, "pin": pin}
        headers = self._request_headers({"Origin": "https://pin-web-client.gopayapi.com", "Referer": "https://pin-web-client.gopayapi.com/"})
        r = self._session.post(url, headers=headers, data=json.dumps(body), timeout_seconds=15)
        log.debug("[PIN] challenge=%s client=%s → %d", challenge_id[:12], client_id[-6:], r.status_code)
        if r.status_code != 200:
            raise GoPayPaymentError(f"PIN verify failed: {r.status_code} {r.text[:200]}")
        try:
            data = r.json()
            token = data.get("data", {}).get("token", "")
            if not token:
                token = data.get("token", "")
            return token
        except Exception:
            raise GoPayPaymentError(f"PIN verify parse error: {r.text[:200]}")

    @staticmethod
    def _extract_transaction_amount(body: dict) -> tuple[Optional[int], str]:
        amount = None
        currency = ""
        preferred = [
            body.get("transaction_details"),
            body.get("order_details"),
            body.get("transaction"),
            body,
        ]
        for candidate in preferred:
            if not isinstance(candidate, dict):
                continue
            for key in ("gross_amount", "total_amount", "amount"):
                if key not in candidate:
                    continue
                try:
                    amount = int(float(str(candidate[key]).replace(",", "")))
                    break
                except (TypeError, ValueError):
                    pass
            if amount is not None:
                break
        for candidate in preferred:
            if isinstance(candidate, dict):
                value = candidate.get("currency") or candidate.get("currency_code")
                if value:
                    currency = str(value).upper()
                    break
        return amount, currency

    @staticmethod
    def _is_one_idr_tokenization_verification(
        body: dict, amount: Optional[int], currency: str
    ) -> bool:
        """识别 Midtrans 的 GoPay 强制绑定 1 IDR 验证交易。

        2026-08-22 的成功抓包同时满足：金额 1 IDR、gopay.tokenization /
        enforce_tokenization 为 true，且 enabled_payments 中 GoPay 状态为 up、
        mode 包含 tokenization。四项必须同时命中，避免把普通 1 IDR 订单误当验证。
        """
        if amount != GOPAY_TOKENIZATION_VERIFICATION_AMOUNT_IDR or currency != "IDR":
            return False
        gopay = body.get("gopay") if isinstance(body, dict) else None
        if not isinstance(gopay, dict):
            return False
        if (
            gopay.get("tokenization") is not True
            or gopay.get("enforce_tokenization") is not True
        ):
            return False
        payments = body.get("enabled_payments")
        if not isinstance(payments, list):
            return False
        for payment in payments:
            if (
                not isinstance(payment, dict)
                or str(payment.get("type") or "").lower() != "gopay"
            ):
                continue
            status = str(payment.get("status") or "").lower()
            modes = payment.get("mode") or []
            if isinstance(modes, str):
                modes = [modes]
            normalized_modes = {str(mode).lower() for mode in modes}
            if status == "up" and "tokenization" in normalized_modes:
                return True
        return False

    @staticmethod
    def _check_cancel(cancel_check: Optional[Callable[[], bool]]) -> None:
        if cancel_check and cancel_check():
            raise GoPayPaymentError("payment cancelled")

    @classmethod
    def _sleep(cls, seconds: float, cancel_check: Optional[Callable[[], bool]]) -> None:
        deadline = time.monotonic() + max(float(seconds or 0), 0)
        while time.monotonic() < deadline:
            cls._check_cancel(cancel_check)
            time.sleep(min(0.25, max(deadline - time.monotonic(), 0)))

    def inspect_transaction(
        self,
        midtrans_url: str,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Query an existing Snap transaction without linking or charging again."""
        match = re.search(r"/snap/v[34]/redirection/([0-9a-f-]{36})", midtrans_url)
        if not match:
            return {"success": False, "uncertain": False, "detail": "invalid midtrans URL"}
        self._check_cancel(cancel_check)
        snap = match.group(1)
        response = self._midtrans_get(f"/snap/v1/transactions/{snap}/status")
        body = response.get("body") if isinstance(response.get("body"), dict) else {}
        status = str(body.get("transaction_status") or "unknown").lower()
        amount, currency = self._extract_transaction_amount(body)
        terminal_success = status in {"settlement", "capture"}
        uncertain = status in {"pending", "authorize", "unknown", ""} or response.get("status") != 200
        return {
            "success": terminal_success,
            "uncertain": uncertain,
            "detail": f"transaction_status={status}",
            "transaction_status": status,
            "snap": snap,
            "amount": amount,
            "currency": currency,
            "http_status": response.get("status"),
        }

    def pay(
        self,
        midtrans_url: str,
        phone: str,
        country_code: str,
        pin: str,
        wait_otp: Callable[[str, int], Optional[str]] = None,
        otp_total_timeout: int = 120,
        otp_resend_after: int = 60,
        cancel_check: Optional[Callable[[], bool]] = None,
        expected_currency: str = "",
        max_amount: Optional[int] = None,
        require_zero_amount: bool = False,
        allow_one_idr_tokenization_verification: bool = False,
        status_timeout: int = 45,
        status_poll_interval: float = 2.0,
        progress: Optional[Callable[[str], None]] = None,
        midtrans_client_key: str = "",
    ) -> dict:
        """
        执行完整的 GoPay 支付流程。

        Args:
            midtrans_url: Midtrans snap redirect URL
            phone: 手机号（不含国际码，如 85142447768）
            country_code: 国际码（如 62）
            pin: 6 位 GoPay PIN
            wait_otp: 等待 OTP 的回调函数 (phone, timeout) → code or None
            otp_total_timeout: 等 OTP 的总超时秒数（默认 120=2 分钟）
            otp_resend_after: 第一段等待多少秒没收到码就重新触发 GoPay
                发码（默认 60=1 分钟），之后继续等到总超时
            allow_one_idr_tokenization_verification: 允许元数据完整命中的
                GoPay 强制绑定 1 IDR 验证交易越过 0 元上限

        Returns:
            {"success": bool, "detail": str, "transaction_status": str}
        """
        def note(message: str) -> None:
            log.info("[pay] %s", message)
            if progress:
                try:
                    progress(message)
                except Exception:
                    log.debug("[pay] progress callback failed", exc_info=True)

        # 提取 snap token
        m = re.search(r"/snap/v[34]/redirection/([0-9a-f-]{36})", midtrans_url)
        if not m:
            return {"success": False, "detail": "invalid midtrans URL"}
        snap = m.group(1)
        self._charge_attempted = False
        self._check_cancel(cancel_check)
        _fp = getattr(self, "payment_fingerprint", None) or {}
        log.info("[pay] snap=%s phone=***%s profile_id=%s", snap[:12], str(phone)[-4:], str(_fp.get("profile_id") or ""))

        # === Phase A: Linking ===

        # 先拉交易详情，取商户 client_key（linking 的 Basic Authorization 用）。
        log.info("[pay] 拉交易详情取 client_key…")
        tx_r = self._midtrans_get(f"/snap/v1/transactions/{snap}")
        client_key = str(midtrans_client_key or "")
        tx_body = tx_r.get("body") if isinstance(tx_r.get("body"), dict) else {}
        if not client_key and tx_r["status"] == 200:
            client_key = tx_body.get("merchant", {}).get("client_key", "")
        amount, detected_currency = self._extract_transaction_amount(tx_body)
        is_tokenization_verification = bool(
            allow_one_idr_tokenization_verification
            and self._is_one_idr_tokenization_verification(
                tx_body, amount, detected_currency
            )
        )
        constraints_enabled = bool(
            expected_currency or max_amount is not None or require_zero_amount
        )
        if constraints_enabled and amount is None:
            return {
                "success": False,
                "uncertain": False,
                "detail": "transaction amount unavailable; refusing to charge",
                "snap": snap,
            }
        if expected_currency and detected_currency != str(expected_currency).upper():
            return {
                "success": False,
                "uncertain": False,
                "detail": f"currency mismatch: expected={expected_currency} actual={detected_currency or 'unknown'}",
                "snap": snap,
                "amount": amount,
                "currency": detected_currency,
            }
        if require_zero_amount and amount != 0 and not is_tokenization_verification:
            return {
                "success": False,
                "uncertain": False,
                "detail": f"non-zero transaction blocked: amount={amount}",
                "snap": snap,
                "amount": amount,
                "currency": detected_currency,
            }
        if (
            max_amount is not None
            and amount is not None
            and amount > int(max_amount)
            and not is_tokenization_verification
        ):
            return {
                "success": False,
                "uncertain": False,
                "detail": f"transaction amount {amount} exceeds limit {max_amount}",
                "snap": snap,
                "amount": amount,
                "currency": detected_currency,
            }
        if is_tokenization_verification:
            log.info(
                "[pay] detected enforced GoPay tokenization verification: %d %s",
                amount,
                detected_currency,
            )
        if not client_key:
            log.warning("[pay] 未取到 client_key（继续尝试不带 Authorization）: %s", tx_r["status"])
        link_extra = {}
        if client_key:
            auth_str = base64.b64encode(f"{client_key}:".encode("utf-8")).decode("utf-8")
            link_extra = {"Authorization": f"Basic {auth_str}"}
            log.info("[pay] merchant client key resolved")

        # Step 1: linking
        # 有限重试：LINK_RETRY_LIMIT 次（每次间隔 LINK_RETRY_SLEEP_S）。
        #  - 429：sleep 后重试，耗尽后明确中文报错（参考版）。
        #  - 406：先做当前 unlink-relink 策略，仍 406 则 sleep 重试，耗尽后参考版中文报错。
        note("Step 1: linking")
        link_body = {
            "type": "gopay",
            "country_code": country_code,
            "phone_number": phone,
        }
        link_r = {}
        unlink_done = False
        for attempt in range(1, LINK_RETRY_LIMIT + 2):
            self._check_cancel(cancel_check)
            link_r = self._midtrans_post(
                f"/snap/v3/accounts/{snap}/linking",
                link_body,
                extra_headers=link_extra,
            )
            if link_r["status"] in (200, 201):
                break
            if link_r["status"] == 429:
                if attempt <= LINK_RETRY_LIMIT:
                    log.info("[pay] linking 429 rate limited, sleep %.0fs retry %d/%d",
                             LINK_RETRY_SLEEP_S, attempt, LINK_RETRY_LIMIT)
                    self._sleep(LINK_RETRY_SLEEP_S, cancel_check)
                    continue
                return {"success": False, "detail": "linking 429 rate limited，请换新 Midtrans 链接或稍后重试"}
            if link_r["status"] == 406:
                body_text = json.dumps(link_r.get("body", {}), ensure_ascii=False)[:300]
                if not unlink_done:
                    log.info("[pay] Already linked, unlinking first...")
                    ul = self._midtrans_delete(f"/snap/v3/accounts/{snap}/gopay")
                    log.info("[pay] Unlink response: %d", ul["status"])
                    self._sleep(1, cancel_check)
                    unlink_done = True
                if attempt <= LINK_RETRY_LIMIT:
                    log.info("[pay] linking 406/pending linked state (%s), sleep %.0fs retry %d/%d",
                             body_text, LINK_RETRY_SLEEP_S, attempt, LINK_RETRY_LIMIT)
                    self._sleep(LINK_RETRY_SLEEP_S, cancel_check)
                    continue
                return {
                    "success": False,
                    "detail": (
                        "Midtrans 链接已有未完成的 GoPay 绑定状态，不能重复绑定；"
                        "请重新用 AT 生成一条新的 Midtrans 链接后再支付"
                    ),
                }
            break
        if link_r["status"] not in (200, 201):
            log.warning("[pay] linking failed: %d", link_r["status"])
            return {"success": False, "detail": f"linking failed: {link_r['status']}"}

        # 从 response 提取 reference
        body = link_r["body"]
        act_url = body.get("activation_link_url", "")
        ref_m = re.search(r"reference=([0-9a-f-]{36})", act_url)
        if not ref_m:
            return {"success": False, "detail": "no reference in linking response"}
        reference = ref_m.group(1)
        log.info("[pay] linking reference=%s...", reference[:8])

        self._sleep(1, cancel_check)

        # Step 2: validate-reference
        note("Step 2: validate-reference")
        vr = self._gwa_post("/v1/linking/validate-reference", {"reference_id": reference})
        if vr["status"] != 200:
            return {"success": False, "detail": f"validate-reference failed: {vr['status']}"}

        self._sleep(1, cancel_check)

        # Step 3: user-consent
        note("Step 3: user-consent")
        uc = self._gwa_post("/v1/linking/user-consent", {"reference_id": reference})
        if uc["status"] != 200:
            return {"success": False, "detail": f"user-consent failed: {uc['status']}"}

        self._sleep(1, cancel_check)

        # Step 4: resend-otp (强制 SMS)
        note("Step 4: resend-otp (force SMS)")
        resend = self._gwa_post("/v1/linking/resend-otp", {
            "reference_id": reference,
            "otp_channel": "SMS",
        })
        log.info("[pay] resend-otp: %d", resend["status"])

        # 等待 OTP
        if not wait_otp:
            return {"success": False, "detail": "no OTP callback provided"}
        full_phone = f"+{country_code}{phone}"

        def _trigger_gopay_resend() -> None:
            r = self._gwa_post("/v1/linking/resend-otp", {
                "reference_id": reference,
                "otp_channel": "SMS",
            })
            log.info("[pay] resend-otp(retry): %d", r["status"])

        # 分段等待：先等 otp_resend_after 秒；没收到就重新触发 GoPay 发码，
        # 再等剩余时间到 otp_total_timeout。两段都拿不到才算 OTP timeout。
        total = max(int(otp_total_timeout or 0), 1)
        first_wait = max(min(int(otp_resend_after or 0), total), 0)
        otp_code = None
        if first_wait > 0:
            log.info("[pay] Waiting for OTP on ***%s (first %ds)...", full_phone[-4:], first_wait)
            self._check_cancel(cancel_check)
            otp_code = wait_otp(full_phone, first_wait)
            self._check_cancel(cancel_check)
        if not otp_code:
            remaining = total - first_wait
            if remaining > 0:
                log.info("[pay] OTP not received in %ds, re-triggering GoPay resend...", first_wait)
                try:
                    _trigger_gopay_resend()
                except Exception as exc:
                    log.warning("[pay] resend-otp retry failed: %s", exc)
                log.info("[pay] Waiting for OTP on ***%s (remaining %ds)...", full_phone[-4:], remaining)
                self._check_cancel(cancel_check)
                otp_code = wait_otp(full_phone, remaining)
                self._check_cancel(cancel_check)
        if not otp_code:
            return {"success": False, "detail": "OTP timeout"}
        log.info("[pay] OTP received")

        self._sleep(1, cancel_check)

        # Step 5: validate-otp
        note("Step 5: validate-otp")
        vo = self._gwa_post("/v1/linking/validate-otp", {
            "reference_id": reference,
            "otp": otp_code,
        })
        if vo["status"] != 200:
            return {"success": False, "detail": f"validate-otp failed: {vo['status']} {str(vo['body'])[:200]}"}

        # 提取 challenge_id
        vo_body = vo.get("body", {})
        log.info("[pay] validate-otp response received")

        # 尝试多种路径提取 challenge_id
        challenge_id = ""
        if isinstance(vo_body, dict):
            challenge_id = (vo_body.get("challenge_id", "")
                          or vo_body.get("data", {}).get("challenge_id", ""))
            # 可能在 redirect_url / pin_url 里
            for key in ("redirect_url", "pin_url", "url", "callback_url"):
                url_val = vo_body.get(key, "") or vo_body.get("data", {}).get(key, "")
                if url_val:
                    m = re.search(r"challengeId=([0-9a-f-]{36})", url_val)
                    if m:
                        challenge_id = m.group(1)
                        break
        # 如果还没有，尝试从整个 response 文本里搜
        if not challenge_id:
            body_str = json.dumps(vo_body, ensure_ascii=False)
            m = re.search(r"[Cc]hallenge[_]?[Ii]d[\"':=\s]+([0-9a-f-]{36})", body_str)
            if m:
                challenge_id = m.group(1)
        if not challenge_id:
            log.error("[pay] No challenge_id found in validate-otp response")
            return {"success": False, "detail": "no challenge_id after OTP validation"}

        log.info("[pay] challenge_id=%s", challenge_id[:16])
        self._sleep(1, cancel_check)

        # Step 6: PIN verify (linking)
        note("Step 6: PIN verify (MGUPA)")
        pin_token = self._pin_verify(challenge_id, pin, PIN_CLIENT_LINKING)
        log.info("[pay] linking PIN token received")

        self._sleep(1, cancel_check)

        # Step 7: validate-pin
        note("Step 7: validate-pin")
        vp = self._gwa_post("/v1/linking/validate-pin", {
            "reference_id": reference,
            "token": pin_token,
        })
        if vp["status"] != 200:
            return {"success": False, "detail": f"validate-pin failed: {vp['status']}"}
        log.info("[pay] Linking complete!")

        # === Phase B: Charge ===

        # Step 8: poll gopay status
        note("Step 8: poll gopay linked status")
        for _ in range(10):
            self._sleep(2, cancel_check)
            gs = self._midtrans_get(f"/snap/v3/accounts/{snap}/gopay")
            if gs["status"] == 200:
                acct_status = gs["body"].get("account_status", "")
                if acct_status == "ENABLED" or "linked" in str(gs["body"]).lower():
                    log.info("[pay] GoPay linked: %s", acct_status)
                    break
        else:
            return {"success": False, "detail": "gopay not linked after polling"}

        self._sleep(1, cancel_check)

        # Step 9: charge
        note("Step 9: charge")
        self._check_cancel(cancel_check)
        self._charge_attempted = True
        try:
            charge = self._midtrans_post(f"/snap/v2/transactions/{snap}/charge", {
                "payment_type": "gopay",
                "tokenization": "true",
                "promo_details": None,
            })
        except Exception as exc:
            return {
                "success": False,
                "uncertain": True,
                "charge_attempted": True,
                "detail": f"charge response unavailable: {exc}",
                "transaction_status": "unknown",
                "snap": snap,
                "amount": amount,
                "currency": detected_currency,
            }
        charge_body = charge["body"]
        log.info("[pay] charge response received: HTTP %s", charge.get("status"))

        # fraud check（HTTP 可能是 200 但 body 里 status_code=202 + fraud_status=deny）
        body_status = str(charge_body.get("status_code", ""))
        fraud = charge_body.get("fraud_status", "")
        txn_status = charge_body.get("transaction_status", "")
        if fraud == "deny" or txn_status == "deny":
            raise GoPayFraudDenyError("FRAUD DENIED")
        if charge["status"] not in (200, 201) and body_status not in ("200", "201"):
            return {"success": False, "uncertain": True, "charge_attempted": True, "detail": f"charge failed: HTTP {charge['status']} body_status={body_status}", "snap": snap, "amount": amount, "currency": detected_currency}

        # charge 直接 settlement（无需 challenge）
        if txn_status in ("settlement", "capture"):
            log.info("[pay] charge already settled, no challenge needed")
            return {
                "success": True,
                "uncertain": False,
                "charge_attempted": True,
                "detail": "payment completed (direct settlement)",
                "transaction_status": txn_status,
                "snap": snap,
                "amount": amount,
                "currency": detected_currency,
            }

        challenge_ref = ""
        actions = charge_body.get("actions") or []
        for act in actions:
            u = act.get("url") or ""
            ref_m2 = re.search(r"reference=([A-Za-z0-9]+)", u)
            if ref_m2:
                challenge_ref = ref_m2.group(1)
                break
        if not challenge_ref:
            for key in ("gopay_verification_link_url", "redirect_url", "url", "deeplink_url"):
                u = charge_body.get(key) or ""
                ref_m2 = re.search(r"reference=([A-Za-z0-9]+)", u)
                if ref_m2:
                    challenge_ref = ref_m2.group(1)
                    break
        if not challenge_ref:
            log.warning("[pay] no challenge ref, charge_body keys: %s", list(charge_body.keys()))
            return {"success": False, "uncertain": True, "charge_attempted": True, "detail": "no challenge ref in charge response", "snap": snap, "amount": amount, "currency": detected_currency}
        log.info("[pay] charge challenge reference received")

        # === Phase C: Challenge ===

        # HAR 里在 validate 之前先访问了 challenge 页面（可能设 cookie/session）
        verification_url = charge_body.get("gopay_verification_link_url") or ""
        if verification_url:
            log.info("[pay] GET challenge page")
            try:
                vr = self._session.get(verification_url, headers=self._request_headers({
                    "Referer": "https://app.midtrans.com/",
                }), timeout_seconds=15)
                log.info("[pay] challenge page: %d (%d bytes)", vr.status_code, len(vr.text))
            except Exception as e:
                log.warning("[pay] challenge page fetch failed: %s", e)

        self._sleep(1, cancel_check)

        # Step 10: payment validate
        note("Step 10: payment validate")
        pv = self._gwa_get(f"/v1/payment/validate?reference_id={challenge_ref}")
        log.info("[pay] validate response: %d", pv["status"])
        if pv["status"] != 200:
            return {"success": False, "uncertain": True, "charge_attempted": True, "detail": f"payment validate failed: {pv['status']}", "snap": snap, "amount": amount, "currency": detected_currency}

        # 提取支付阶段的 challenge_id（可能嵌套在多层结构里）
        pv_body = pv.get("body", {})
        pay_challenge_id = self._extract_challenge_id(pv_body)

        self._sleep(1, cancel_check)

        # Step 11: payment confirm
        note("Step 11: payment confirm")
        pc = self._gwa_post(f"/v1/payment/confirm?reference_id={challenge_ref}", {
            "payment_instructions": [],
        })
        log.info("[pay] confirm response: %d", pc["status"])
        if pc["status"] != 200:
            return {"success": False, "uncertain": True, "charge_attempted": True, "detail": f"payment confirm failed: {pc['status']}", "snap": snap, "amount": amount, "currency": detected_currency}

        # 从 confirm response 提取 challenge_id（如果 validate 没给）
        if not pay_challenge_id:
            pc_body = pc.get("body", {})
            pay_challenge_id = self._extract_challenge_id(pc_body)
        if not pay_challenge_id:
            return {"success": False, "uncertain": True, "charge_attempted": True, "detail": "no challenge_id for payment PIN", "snap": snap, "amount": amount, "currency": detected_currency}
        log.info("[pay] payment challenge received")

        self._sleep(1, cancel_check)

        # Step 12: PIN verify (payment)
        note("Step 12: PIN verify (GWC)")
        pay_pin_token = self._pin_verify(pay_challenge_id, pin, PIN_CLIENT_PAYMENT)

        self._sleep(1, cancel_check)

        # Step 13: payment process
        note("Step 13: payment process")
        pp = self._gwa_post(f"/v1/payment/process?reference_id={challenge_ref}", {
            "challenge": {
                "type": "GOPAY_PIN_CHALLENGE",
                "value": {"pin_token": pay_pin_token},
            },
        })
        if pp["status"] != 200:
            return {"success": False, "uncertain": True, "charge_attempted": True, "detail": f"payment process failed: {pp['status']}", "snap": snap, "amount": amount, "currency": detected_currency}
        log.info("[pay] Payment process OK!")

        # === Phase D: 验证 ===
        deadline = time.monotonic() + max(int(status_timeout or 0), 1)
        txn_status = "unknown"
        while time.monotonic() < deadline:
            self._sleep(status_poll_interval, cancel_check)
            note("Step 14: check transaction status")
            inspected = self.inspect_transaction(midtrans_url, cancel_check=cancel_check)
            txn_status = str(inspected.get("transaction_status") or "unknown").lower()
            log.info("[pay] Transaction status: %s", txn_status)
            if txn_status in {"settlement", "capture"}:
                return {
                    "success": True,
                    "uncertain": False,
                    "charge_attempted": True,
                    "detail": "payment completed",
                    "transaction_status": txn_status,
                    "snap": snap,
                    "amount": amount,
                    "currency": detected_currency,
                }
            if txn_status in {"deny", "cancel", "cancelled", "expire", "expired", "failure"}:
                return {
                    "success": False,
                    "uncertain": False,
                    "charge_attempted": True,
                    "detail": f"transaction_status={txn_status}",
                    "transaction_status": txn_status,
                    "snap": snap,
                    "amount": amount,
                    "currency": detected_currency,
                }
        return {
            "success": False,
            "uncertain": True,
            "charge_attempted": True,
            "detail": f"payment status remains {txn_status}",
            "transaction_status": txn_status,
            "snap": snap,
            "amount": amount,
            "currency": detected_currency,
        }
