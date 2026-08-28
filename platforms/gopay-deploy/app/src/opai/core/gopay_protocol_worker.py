"""
GoPay Pure-Protocol Worker — registration + payment parallel pipeline.

Self-contained deployment version — all imports are local (no C:\\tools dependency).

Each worker thread loops independently:
  1. Register GoPay account (rent phone → signup → refresh → PIN)
  2. Push account to inbox, wait for balance > 0
  3. Claim inbox job → pure-protocol Midtrans payment
  4. Done or failed → loop back to step 1
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import string
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tls_client

from .sms_helpers import (
    sms_api, sms_get_number, sms_wait_code, sms_request_another,
    sms_cancel, sms_done, api_call_with_retry, get_error_code,
    is_waf_block, is_rate_limited,
)
from .gojek_client import GojekClient, CLIENT_ID as _GOJEK_CLIENT_ID, CLIENT_SECRET as _GOJEK_CLIENT_SECRET

# 注册 + 设 PIN 已切换到 "GoPay App 纯协议"（com.gojek.gopay 2.10.0,
# gopay:consumer:app）。GojekClient（gojek:consumer:app）仍保留给历史/付款
# 代码引用，但 _register_one / _resume_account 走下面这套。
from .gopay_app_protocol import (
    GoPayProtocol,
    GoPayAppClient,
    EnhancedPythonXESigner,
    build_device_profile,
    login_with_known_pin,
    has_error_code,
    is_success_response,
    is_waf_html,
    is_phone_registered_error,
    extract_account_id,
    pick_first as _pick_first,
    AUTH as _GP_AUTH,
    API as _GP_API,
    CUSTOMER as _GP_CUSTOMER,
    AUTH_SECRET as _GP_AUTH_SECRET,
    AUTH_ID as _GP_AUTH_ID,
    SIGNUP_CLIENT_NAME as _GP_SIGNUP_CLIENT_NAME,
    SIGNUP_BASIC_SUFFIX as _GP_SIGNUP_BASIC_SUFFIX,
)

from .envelope_manager import EnvelopeManager
from .gopay_payment_protocol import GoPayPayment, GoPayFraudDenyError
from .payment_fingerprint import ensure_account_payment_fingerprint

from .log_redaction import install_sensitive_log_filter

log = logging.getLogger(__name__)
install_sensitive_log_filter(log)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INBOX_URL = os.environ.get("OPAI_PAYMENT_INBOX_BASE_URL", "")
INBOX_USER = os.environ.get("OPAI_PAYMENT_INBOX_BASIC_USER", "")
INBOX_PASS = os.environ.get("OPAI_PAYMENT_INBOX_BASIC_PASS", "")
POLL_INTERVAL = float(os.environ.get("OPAI_GOPAY_POLL_INTERVAL", "10"))
MIN_REMAINING_SEC = int(os.environ.get("OPAI_GOPAY_MIN_REMAINING_SEC", "300"))
DEFAULT_PIN = os.environ.get("OPAI_GOPAY_DEFAULT_PIN", "147258")
MIN_BALANCE_RP = int(os.environ.get("OPAI_GOPAY_MIN_BALANCE_RP", "1"))

GOPAY_ACCOUNT_TTL = int(os.environ.get("OPAI_GOPAY_ACCOUNT_TTL_SEC", "1200"))

_NOVPROXY_TPL = os.environ.get("OPAI_GOPAY_PROXY_TEMPLATE", "")


def _make_proxy() -> str:
    override = os.environ.get("OPAI_GOPAY_REGISTER_PROXY", "").strip()
    if override:
        return override
    if not _NOVPROXY_TPL:
        return ""
    sid = "gp" + "".join(random.choices(string.ascii_letters + string.digits, k=6))
    return _NOVPROXY_TPL.format(sid=sid)


# ---------------------------------------------------------------------------
# Inbox account sync
# ---------------------------------------------------------------------------

_INBOX_AUTH = None


def _inbox_auth_header() -> str:
    global _INBOX_AUTH
    if _INBOX_AUTH is None:
        _INBOX_AUTH = "Basic " + base64.b64encode(f"{INBOX_USER}:{INBOX_PASS}".encode()).decode()
    return _INBOX_AUTH


def _inbox_push_account(phone: str, data: dict):
    try:
        url = f"{INBOX_URL}/api/gopay-accounts"
        req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", _inbox_auth_header())
        urllib.request.urlopen(req, timeout=10)
        log.info("[inbox] %s pushed", phone)
    except Exception as e:
        log.warning("[inbox] %s push failed: %s", phone, e)


def _inbox_delete_account(phone: str):
    try:
        url = f"{INBOX_URL}/api/gopay-accounts/{urllib.parse.quote(phone, safe='')}"
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Authorization", _inbox_auth_header())
        urllib.request.urlopen(req, timeout=10)
        log.info("[inbox] %s deleted", phone)
    except Exception as e:
        log.debug("[inbox] %s delete failed: %s", phone, e)


def _inbox_ttl_cleanup():
    def _loop():
        while True:
            time.sleep(60)
            try:
                url = f"{INBOX_URL}/api/gopay-accounts"
                req = urllib.request.Request(url)
                req.add_header("Authorization", _inbox_auth_header())
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read().decode())
                now = time.time()
                for a in data.get("accounts", []):
                    added = a.get("added_at", "")
                    if not added:
                        continue
                    try:
                        ts = datetime.fromisoformat(added.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if now - ts > GOPAY_ACCOUNT_TTL:
                        phone = a.get("phone", "")
                        if phone:
                            log.info("[inbox-ttl] %s expired (%.0fs old), removing", phone, now - ts)
                            _inbox_delete_account(phone)
            except Exception as e:
                log.debug("[inbox-ttl] cleanup error: %s", e)

    t = threading.Thread(target=_loop, daemon=True, name="inbox-ttl")
    t.start()


# ---------------------------------------------------------------------------
# Deferred phone cancel
# ---------------------------------------------------------------------------

_CANCEL_MIN_AGE = 130


def _deferred_cancel_phone(api_key: str, activation_id: str, phone: str, rented_at: float):
    def _loop():
        _inbox_delete_account(phone)
        wait = max(0, _CANCEL_MIN_AGE - (time.time() - rented_at))
        if wait > 0:
            time.sleep(wait + 5)
        deadline = rented_at + 1200
        while time.time() < deadline:
            try:
                resp = sms_api(api_key, "setStatus", {"id": activation_id, "status": "8"})
                if "CANCEL" in (resp or "").upper() or "ACCESS" in (resp or "").upper():
                    log.info("[cancel] %s OK: %s", phone, resp)
                    return
                log.debug("[cancel] %s response: %s", phone, resp)
            except Exception as e:
                log.debug("[cancel] %s error: %s", phone, e)
            time.sleep(180)
        log.info("[cancel] %s gave up (hero-sms 20min auto-reclaim)", phone)

    t = threading.Thread(target=_loop, daemon=True, name=f"cancel-{phone}")
    t.start()


# ---------------------------------------------------------------------------
# Account persistence
# ---------------------------------------------------------------------------

ACCOUNTS_FILE = os.environ.get(
    "OPAI_GOPAY_ACCOUNTS_FILE",
    str(Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "gopay_worker_accounts.json"),
)
_accounts_lock = threading.Lock()


def _save_account(phone: str, local: str, pin: str, aid: str, client):
    entry = {
        "phone": phone,
        "local": local,
        "pin": pin,
        "activation_id": aid,
        "customer_id": getattr(client, "user_uuid", ""),
        "access_token": client.auth.access_token,
        "refresh_token": client.auth.refresh_token,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "balance": 0,
    }
    with _accounts_lock:
        accounts = []
        if os.path.exists(ACCOUNTS_FILE):
            try:
                accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
            except Exception:
                pass
        accounts.append(entry)
        open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, indent=2, ensure_ascii=False))
    log.info("[save] %s saved locally", phone)
    _inbox_push_account(phone, {**entry, "added_at": entry["registered_at"]})


def _update_account_balance(phone: str, balance: int, client):
    with _accounts_lock:
        accounts = []
        if os.path.exists(ACCOUNTS_FILE):
            try:
                accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
            except Exception:
                return
        for a in accounts:
            if a["phone"] == phone:
                a["balance"] = balance
                a["access_token"] = client.auth.access_token
                a["refresh_token"] = client.auth.refresh_token
                break
        open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, indent=2, ensure_ascii=False))
    log.info("[save] %s balance=%d updated locally", phone, balance)


def _check_balance(client) -> int:
    try:
        r = client.get_balance()
        if r["status"] == 200:
            data = r["body"].get("data", [])
            if isinstance(data, list) and data:
                return data[0].get("balance", {}).get("value", 0)
        return -1
    except Exception:
        return -1


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _load_account_payment_fingerprint(phone: str) -> Optional[dict]:
    """Load and persist the saved payment fingerprint for an account."""
    if not os.path.exists(ACCOUNTS_FILE):
        return None
    target = _phone_digits(phone)
    with _accounts_lock:
        try:
            accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
        except Exception:
            return None
        if not isinstance(accounts, list):
            return None
        for idx, account in enumerate(accounts):
            if not isinstance(account, dict):
                continue
            item_phone = _phone_digits(account.get("phone", ""))
            item_local = _phone_digits(account.get("local", ""))
            if target and (
                target == item_phone
                or (item_local and target == item_local)
                or (item_phone and item_phone.endswith(target))
                or (item_local and target.endswith(item_local))
            ):
                profile = ensure_account_payment_fingerprint(account)
                accounts[idx] = account
                open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, indent=2, ensure_ascii=False))
                return profile
    return None


def migrate_account_payment_fingerprints() -> dict:
    """Ensure every saved account has a stable payment fingerprint."""
    path = Path(ACCOUNTS_FILE)
    if not path.exists():
        return {"path": str(path), "total": 0, "updated": 0, "accounts": []}

    with _accounts_lock:
        try:
            accounts = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"read accounts failed: {exc}") from exc
        if not isinstance(accounts, list):
            raise RuntimeError("accounts file must contain a JSON list")

        updated = 0
        public_accounts = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            before = account.get("payment_fingerprint")
            profile = ensure_account_payment_fingerprint(account)
            if before != profile:
                updated += 1
            public_accounts.append({
                "phone": account.get("phone", ""),
                "local": account.get("local", ""),
                "profile_id": profile.get("profile_id", ""),
            })

        if updated:
            path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "path": str(path),
        "total": len(public_accounts),
        "updated": updated,
        "accounts": public_accounts,
    }


# ---------------------------------------------------------------------------
# Register one GoPay account
# ---------------------------------------------------------------------------

_INDO_NAMES = [
    "Budi Santoso", "Adi Pratama", "Siti Rahayu", "Dewi Lestari",
    "Rizky Ramadhan", "Putri Wulandari", "Agus Setiawan", "Rina Kusuma",
    "Hendra Wijaya", "Novi Anggraini", "Dian Permata", "Wahyu Hidayat",
    "Fitri Handayani", "Joko Susilo", "Ratna Sari", "Bambang Prasetyo",
    "Mega Puspita", "Eko Nugroho", "Sari Indah", "Yusuf Maulana",
    "Lina Marlina", "Arief Rahman", "Wati Suryani", "Dedi Kurniawan",
    "Ayu Lestari", "Rudi Hartono", "Nisa Fitriani", "Bayu Anggara",
    "Sri Mulyani", "Fajar Setiadi", "Indra Gunawan", "Tika Rahmawati",
]


def _make_app_signer():
    """构造 GoPay App 的 X-E1 签名器。

    默认 enhanced（纯 Python，免 adb/Frida），与参考脚本默认一致。
    可用环境变量 ``OPAI_GOPAY_XE_RESOLUTION_KEY`` 覆盖 HMAC key。
    """
    key = os.environ.get("OPAI_GOPAY_XE_RESOLUTION_KEY", "").strip()
    if key:
        return EnhancedPythonXESigner(resolution_key=key)
    return EnhancedPythonXESigner()


def _signup_with_variants(gp, local, full_name, country_code, verification_token):
    """跑参考脚本里的 customer_signup 多组凭证轮试 + WAF 重试。

    返回 ``(access_token, refresh_token, account_id, created_without_token,
    last_error)``。任一变体被服务端接受即停。号已注册抛
    ``_PhoneAlreadyRegistered``。
    """
    waf_retries = int(os.environ.get("OPAI_GOPAY_SIGNUP_WAF_RETRIES", "3"))
    waf_sleep = float(os.environ.get("OPAI_GOPAY_SIGNUP_WAF_SLEEP", "2.0"))
    variants = [
        {
            "label": "auth_id_authsecret_cc62_escaped",
            "client_name": "gopay:consumer:app",
            "client_secret": _GP_AUTH_SECRET,
            "basic": _GP_SIGNUP_BASIC_SUFFIX,
            "signed_up_country": "62",
            "escape": True,
        },
        {
            "label": "gopay_consumer_authsecret_cc62",
            "client_name": _GP_SIGNUP_CLIENT_NAME,
            "client_secret": _GP_AUTH_SECRET,
            "basic": _GP_SIGNUP_BASIC_SUFFIX,
            "signed_up_country": "62",
            "escape": False,
        },
        {
            "label": "gopay_consumer_authsecret_ID",
            "client_name": _GP_SIGNUP_CLIENT_NAME,
            "client_secret": _GP_AUTH_SECRET,
            "basic": _GP_SIGNUP_BASIC_SUFFIX,
            "signed_up_country": "ID",
            "escape": False,
        },
    ]
    access_token = refresh_token = None
    account_id = None
    created_without_token = False
    last_error = None
    for v in variants:
        for waf_try in range(waf_retries + 1):
            sc, data, _ = gp.customer_signup(
                local, full_name, country_code=country_code,
                verification_token=verification_token,
                signup_client_name=v["client_name"],
                signup_client_secret=v["client_secret"],
                signup_basic=v["basic"],
                signed_up_country=v["signed_up_country"],
                escape_client_name_colon=v["escape"],
            )
            if not is_waf_html(sc, data) or waf_try >= waf_retries:
                break
            log.warning("customer_signup WAF 403, retry %d/%d", waf_try + 1, waf_retries)
            time.sleep(waf_sleep)
        last_error = {"status": sc, "data": data, "variant": v["label"]}
        if is_phone_registered_error(data):
            raise _PhoneAlreadyRegistered()
        account_id = account_id or extract_account_id(data)
        access_token = access_token or _pick_first(data, ["access_token", "accessToken"])
        refresh_token = refresh_token or _pick_first(data, ["refresh_token", "refreshToken"])
        if access_token or is_success_response(sc, data, allow=(200, 201, 202, 206)):
            log.info("customer_signup accepted variant=%s access_token=%s", v["label"], bool(access_token))
            if not access_token:
                created_without_token = True
            break
    return access_token, refresh_token, account_id, created_without_token, last_error


_SUPPORTED_E164_CALLING_CODES = (
    "852", "44", "61", "62", "81", "86", "65", "49", "33", "91", "1",
)


def _split_e164_phone(phone: str) -> tuple[str, str, str]:
    raw = str(phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError("手机号为空")
    if not raw.startswith("+"):
        if digits.startswith("0"):
            digits = "62" + digits[1:]
        elif not digits.startswith("62"):
            digits = "62" + digits
    for calling_code in sorted(_SUPPORTED_E164_CALLING_CODES, key=len, reverse=True):
        if digits.startswith(calling_code) and len(digits) > len(calling_code) + 5:
            local = digits[len(calling_code):]
            return f"+{calling_code}", local, f"+{calling_code}{local}"
    raise ValueError(f"暂不支持该手机号国家区号: {raw}")


class _PhoneAlreadyRegistered(RuntimeError):
    pass


class _RebindFailed(RuntimeError):
    """号已注册、auto_rebind 换绑释放失败：不换号重试，直接判任务失败。"""
    pass


_TRANSIENT_NETWORK_MARKERS = (
    "ssl", "eof", "timed out", "timeout", "connection reset",
    "connection aborted", "remote disconnected", "temporarily unavailable",
)


def _call_with_transient_network_retry(label: str, callback, attempts: int = 3):
    """Retry only idempotent protocol reads after transient transport failures."""
    total = max(int(attempts or 0), 1)
    for attempt in range(1, total + 1):
        try:
            return callback()
        except Exception as exc:
            transient = any(
                marker in f"{type(exc).__name__}: {exc}".lower()
                for marker in _TRANSIENT_NETWORK_MARKERS
            )
            if not transient or attempt >= total:
                raise
            delay = attempt * 2
            log.warning(
                "%s transient network failure (%s), retry %d/%d in %ds",
                label,
                type(exc).__name__,
                attempt + 1,
                total,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"{label} retry exhausted")  # pragma: no cover


def _register_one(api_key: str, pin: str, proxy: str, envelope_did: str,
                  *, auto_rebind: bool = False, rebind_acquire=None) -> Optional[dict]:
    """完整注册 + 设 PIN（GoPay App 纯协议, com.gojek.gopay 2.10.0）。

    流程移植自 ``gopay-auto-protocol``（gopay:consumer:app + enhanced X-E1）：
      租号 -> login_methods(确认新号) -> cvs signup OTP -> customer_signup
      -> refresh 成正常 goto-auth session -> goto_pin_wa_sms 二次 OTP -> 设 PIN。
    设备指纹复用主项目的 ``generate_device_identity(seed)``（同号同指纹）。
    返回契约不变：``{phone, aid, pin, client, local}``。

    ``auto_rebind`` + ``rebind_acquire``：拿到的号若**已被注册**，不直接放弃，
    而是登录该已注册账号 -> 换绑到临时外国号 -> 释放本号 -> 用释放干净的本号
    重新走注册。``rebind_acquire()`` 返回 ``(new_phone, wait_otp, finish, cancel)``
    （由 application 层 ``_build_rebind_otp_callback`` 提供，换绑渠道独立于注册）。
    """
    phone, aid = sms_get_number(api_key)
    if not phone:
        log.error("No phone number available")
        return None

    rented_at = time.time()
    country_code, local, phone = _split_e164_phone(phone)

    log.info("[%s] Proxy: %s", phone, proxy.split("@")[-1] if "@" in proxy else "direct")

    device = build_device_profile(phone)
    gp = GoPayProtocol(
        device=device,
        signer=_make_app_signer(),
        client_id=_GP_AUTH_ID,
        client_secret=_GP_AUTH_SECRET,
        debug=False,
        proxy=proxy,
    )
    success = False

    try:
        # === Phase 1: login_methods —— 新号应返回 user:not_found ===
        time.sleep(2)
        sc, data, _ = gp.login_methods(local, country_code)
        if has_error_code(data, "auth:error:user:not_found"):
            # 新号，走 signup CVS。GoPay 把 methods->initiate->verify 当作同一
            # 会话，必须共用一个 transaction-id，否则 initiate 报 invalid_parameter。
            gp.new_cvs_session()
            sc, data, _ = gp.cvs_methods(local, flow="signup", country_code=country_code)
        elif sc in (200, 201, 202):
            log.info("[%s] Already registered", phone)
            # 号已被注册：所有渠道都先用**已知 PIN（默认147258）登录**尝试。
            #   - 登录成功 → 解绑 OpenAI LLC → 换绑到新印尼号 → 用新号付款
            #   - 登录失败 → 弃本号，换新号重注册
            # 登录走 known-PIN(goto_pin 1fa) + 2fa OTP，2fa 短信发到**当前这个
            # 已注册号**上（smsapi 固定号能收、herosms 一次性号也在手里能收），
            # 所以 2fa 用注册渠道的 api_key/aid 接。
            if auto_rebind and callable(rebind_acquire):
                log.info("[%s] auto_rebind 开启：用已保存 PIN 登录，准备换绑到新号…", phone)
                acquired = _login_rebind_to_new_phone(
                    phone, pin, proxy,
                    rebind_acquire=rebind_acquire,
                    login_api_key=api_key, login_sms_id=aid,
                )
                if acquired:
                    # 登录+换绑成功：返回绑了该账号的**新印尼号**，下游用它付款。
                    log.info("[%s] 已登录换绑到新号 %s，用新号付款", phone, acquired.get("phone"))
                    try:
                        sms_cancel(api_key, aid)  # 旧号已被换绑释放，退回接码平台
                    except Exception:
                        pass
                    success = True  # 防止 finally 再 cancel 新号的 aid
                    return acquired
                # 登录失败：弃本号、换新号重注册
                log.info("[%s] 已注册号登录失败，弃号换新号重注册", phone)
                try:
                    sms_cancel(api_key, aid)
                except Exception:
                    pass
                return None
            # 未开 auto_rebind：直接弃号换新号
            log.info("[%s] 号已被注册（未开 auto_rebind），放弃本号换新号重试", phone)
            try:
                sms_cancel(api_key, aid)
            except Exception:
                pass
            return None
        elif is_waf_html(sc, data) or sc == 403:
            log.warning("[%s] WAF 403, need new proxy IP", phone)
            return None

        if not is_success_response(sc, data):
            log.error("[%s] cvs_methods/login_methods failed: HTTP %d %s", phone, sc, data)
            return None

        verification_id = _pick_first(data, ["verification_id", "challenge_id"])
        # 当前所有接码渠道都只能读取 SMS，不能接收 GoPay 默认的 WhatsApp OTP。
        method = "otp_sms"
        if not verification_id:
            log.error("[%s] no verification_id from cvs_methods", phone)
            return None

        # === Phase 2: cvs_initiate 触发注册 OTP ===
        sc, data, _ = gp.cvs_initiate(local, str(verification_id), method=method, flow="signup", country_code=country_code)
        if not is_success_response(sc, data, allow=(200, 201, 202, 204)):
            if is_waf_html(sc, data) or sc == 403:
                log.warning("[%s] WAF 403 on cvs_initiate, need new proxy IP", phone)
            else:
                log.error("[%s] cvs_initiate failed: HTTP %d %s", phone, sc, data)
            return None
        otp_token = _pick_first(data, ["otp_token", "otpToken"])
        fixed_sms_channel = str(api_key or "").strip().lower() in {"smsapi", "api_sms"}
        log.info("[%s] Signup OTP requested via %s", phone, method)

        first_wait = 60 if fixed_sms_channel and otp_token else 180
        otp = sms_wait_code(api_key, aid, timeout=first_wait)
        if not otp and fixed_sms_channel and otp_token:
            log.info("[%s] Signup OTP not received; forcing otp_sms resend", phone)
            retry_status, retry_data, _ = gp.cvs_retry(
                str(otp_token), method="otp_sms", flow="signup"
            )
            if is_success_response(retry_status, retry_data, allow=(200, 201, 202, 204)):
                method = "otp_sms"
                otp_token = _pick_first(retry_data, ["otp_token", "otpToken"]) or otp_token
                log.info("[%s] Signup OTP SMS resend accepted", phone)
            else:
                log.warning(
                    "[%s] Signup OTP SMS resend rejected: HTTP %s",
                    phone,
                    retry_status,
                )
            otp = sms_wait_code(api_key, aid, timeout=120)
        if not otp:
            log.error("[%s] Signup OTP timeout", phone)
            return None
        log.info("[%s] Signup OTP received", phone)

        # === Phase 3: cvs_verify ===
        time.sleep(2)
        sc, data, _ = gp.cvs_verify(
            local, str(verification_id), str(otp), method=method, flow="signup",
            country_code=country_code, otp_token=str(otp_token) if otp_token else None,
        )
        if not is_success_response(sc, data):
            log.error("[%s] Signup verify failed: HTTP %d %s", phone, sc, data)
            return None
        # CVS 会话到 verify 结束；后续 customer_signup / token 不是 CVS 端点，
        # 用回每请求随机 transaction-id（与参考脚本已验证行为一致）。
        gp.clear_cvs_session()
        verification_token = _pick_first(data, ["verification_token", "verificationToken",
                                                "device_verification_token", "device_verification_token_id"])
        access_token = _pick_first(data, ["access_token", "accessToken"])
        refresh_token = _pick_first(data, ["refresh_token", "refreshToken"])
        auth_code = _pick_first(data, ["authorization_code", "auth_code", "code"])

        # === Phase 4: customer_signup（多凭证轮试 + WAF 重试）===
        account_id = None
        if verification_token and not access_token:
            try:
                (access_token, refresh_token, account_id,
                 _created_without_token, last_err) = _signup_with_variants(
                    gp, local, random.choice(_INDO_NAMES), country_code, str(verification_token),
                )
            except _PhoneAlreadyRegistered:
                log.info("[%s] Already registered, skipping", phone)
                return None
            if not access_token and not _created_without_token and last_err is not None:
                log.error("[%s] customer_signup failed: HTTP %s %s", phone, last_err["status"], last_err["data"])
                return None

        # === Phase 5: token 兑换（没直接拿到 access_token 时）===
        if not access_token:
            if verification_token:
                sc, data, _ = gp.token(verification_token=str(verification_token), account_id=str(account_id or local))
            elif auth_code:
                sc, data, _ = gp.token(authorization_code=str(auth_code), account_id=str(account_id or local))
            else:
                log.error("[%s] no access_token / verification_token / auth_code after verify", phone)
                return None
            if not is_success_response(sc, data):
                log.error("[%s] token exchange failed: HTTP %d %s", phone, sc, data)
                return None
            access_token = _pick_first(data, ["access_token", "accessToken"])
            refresh_token = _pick_first(data, ["refresh_token", "refreshToken"]) or refresh_token
        if not access_token:
            log.error("[%s] no access_token after token exchange", phone)
            return None

        # signup 直返的 RS256 token 会被 customer API 判 "Session is revoked"，
        # 必须用 refresh_token 换正常 goto-auth session（幂等）。
        if refresh_token:
            sc, data_rt, _ = gp.token(refresh_token=str(refresh_token), account_id="")
            if is_success_response(sc, data_rt):
                access_token = _pick_first(data_rt, ["access_token", "accessToken"]) or access_token
                refresh_token = _pick_first(data_rt, ["refresh_token", "refreshToken"]) or refresh_token
                log.info("[%s] refreshed signup token for customer APIs", phone)

        log.info("[%s] Signup success", phone)
        customer_id = extract_account_id(data if isinstance(data, dict) else {}) or str(account_id or "")

        # === Phase 6: 设 PIN（goto_pin_wa_sms 二次 CVS OTP）===
        log.info("[%s] 进入设 PIN 阶段（pin_allowed）", phone)
        sc, data, _ = _call_with_transient_network_retry(
            "pin_allowed",
            lambda: gp.pin_allowed(str(access_token), pin),
        )
        if not is_success_response(sc, data):
            log.error("[%s] pin_allowed failed: HTTP %d %s", phone, sc, data)
            return None

        # PIN 是另一段独立 CVS 流程（goto_pin_wa_sms），开新的会话 transaction-id。
        gp.new_cvs_session()
        sc, data, _ = _call_with_transient_network_retry(
            "pin_cvs_methods",
            lambda: gp.cvs_methods_pin(str(access_token)),
        )
        if not is_success_response(sc, data):
            log.error("[%s] pin_cvs_methods failed: HTTP %d %s", phone, sc, data)
            return None
        pin_vid = _pick_first(data, ["verification_id", "challenge_id"])
        # 接码渠道只支持 SMS，不能回退到 GoPay 默认的 WhatsApp OTP。
        pin_method = "otp_sms"
        if not pin_vid:
            log.error("[%s] pin_cvs_methods no verification_id", phone)
            return None

        # 让接码平台准备下一条短信（同 aid 复用），再触发 PIN OTP
        sms_request_another(api_key, aid)
        time.sleep(2)
        sc, data, _ = gp.cvs_initiate_pin(str(access_token), str(pin_vid), method=pin_method)
        if not is_success_response(sc, data, allow=(200, 201, 202, 204)):
            log.error("[%s] pin_cvs_initiate failed: HTTP %d %s", phone, sc, data)
            return None
        pin_otp_token = _pick_first(data, ["otp_token", "otpToken"])
        if not pin_otp_token:
            log.error("[%s] pin_cvs_initiate no otp_token", phone)
            return None

        # PIN OTP 拿码：每 60 秒一段，没拿到新码就重新触发 GoPay 发码
        # （cvs_retry_pin），而不是死等 180 秒。最多 3 段。``ignore_code=otp``
        # 排除注册阶段的旧码（herosms setStatus=3 后会回 STATUS_WAIT_RETRY:<旧码>）。
        pin_code = None
        for pin_round in range(1, 4):
            pin_code = sms_wait_code(api_key, aid, timeout=60, ignore_code=str(otp or ""))
            if pin_code:
                break
            if pin_round < 3:
                log.warning("[%s] PIN OTP 60s 没到，重新触发 GoPay 发码（第 %d 次）", phone, pin_round)
                sc, data_r, _ = gp.cvs_retry_pin(str(access_token), str(pin_otp_token), method=pin_method)
                if is_success_response(sc, data_r, allow=(200, 201, 202, 204)):
                    pin_otp_token = _pick_first(data_r, ["otp_token", "otpToken"]) or pin_otp_token
                else:
                    # retry 不行就重新 initiate 一次
                    sc, data_i, _ = gp.cvs_initiate_pin(str(access_token), str(pin_vid), method=pin_method)
                    if is_success_response(sc, data_i, allow=(200, 201, 202, 204)):
                        pin_otp_token = _pick_first(data_i, ["otp_token", "otpToken"]) or pin_otp_token
                sms_request_another(api_key, aid)
                time.sleep(2)
        if not pin_code:
            log.error("[%s] PIN OTP not received", phone)
            return None
        log.info("[%s] PIN OTP received", phone)

        time.sleep(2)
        sc, data, _ = gp.cvs_verify_pin(str(access_token), str(pin_vid), str(pin_code), str(pin_otp_token), method=pin_method)
        if not is_success_response(sc, data):
            log.error("[%s] pin_cvs_verify failed: HTTP %d %s", phone, sc, data)
            return None
        pin_verification_token = _pick_first(data, ["verification_token", "verificationToken"])
        if not pin_verification_token:
            log.error("[%s] pin_cvs_verify no verification_token", phone)
            return None
        # PIN CVS 会话结束，后续 pin_setup_token / profile 用随机 txn。
        gp.clear_cvs_session()

        sc, data, _ = gp.pin_setup_token_after_otp(str(access_token), pin, str(pin_verification_token))
        if not is_success_response(sc, data):
            log.error("[%s] pin_setup failed: HTTP %d %s", phone, sc, data)
            return None

        # 非破坏性完成校验：profile.is_pin_setup 明确为 False 才算失败
        sc, data_prof, _ = _call_with_transient_network_retry(
            "pin_profile_verify",
            lambda: gp.user_profile(str(access_token)),
        )
        is_pin_setup = _pick_first(data_prof, ["is_pin_setup", "isPinSetup"])
        if is_pin_setup is False:
            log.error("[%s] profile says PIN not set: %s", phone, data_prof)
            return None
        log.info("[%s] PIN set OK", phone)

        # === 组装下游兼容 client + 落库 ===
        client = GoPayAppClient(
            gp,
            phone=phone,
            local=local,
            user_uuid=customer_id,
            access_token=str(access_token),
            refresh_token=str(refresh_token or ""),
        )
        _save_account(phone, local, pin, aid, client)

        success = True
        return {
            "phone": phone, "aid": aid, "pin": pin, "client": client,
            "local": local, "country_code": country_code,
        }

    except _RebindFailed:
        # 换绑释放失败：不吞，向上抛让 plugin 直接判任务失败（不换号重试）。
        raise
    except Exception as e:
        log.exception("[%s] Registration exception: %s", phone, e)
        return None
    finally:
        if not success:
            try:
                gp.close()
            except Exception:
                pass
            _deferred_cancel_phone(api_key, aid, phone, rented_at)


def _login_one(phone: str, pin: str, proxy: str, *, use_pin: bool = False, api_key: str = "", sms_id: str = "") -> Optional[dict]:
    """登录一个**已注册**的 GoPay 号（真机抓包对齐，两段式 1fa+2fa）。

    真机 GoPay 登录实测：1fa 之后服务端**强制 2FA OTP**，必须再接一条短信才
    能拿 access_token（见 gopay-auto-protocol/20260603/login 抓包分析）。所以
    无论 PIN 登录还是 OTP 登录，都要 ``api_key`` + ``sms_id`` 指向**该号能接码**
    的渠道/订单。

    - ``use_pin=True``：用已知 PIN 走 goto_pin 完成 1fa（PIN→validation_jwt），
      再接 2fa 短信。适合我们自己用已知 PIN 注册的成熟号。
    - ``use_pin=False``：1fa 也走短信 OTP（otp_sms），再接 2fa 短信（两条都从
      同一 sms_id 续接）。适合 PIN 未知但号在手里能收短信的场景。

    返回与 _register_one 相同契约：``{phone, aid, pin, client, local}``，失败 None。
    """
    country_code, local, phone_e164 = _split_e164_phone(phone)
    sms_id = str(sms_id or "").strip() or phone_e164

    device = build_device_profile(phone_e164)
    gp = GoPayProtocol(
        device=device, signer=_make_app_signer(),
        client_id=_GP_AUTH_ID, client_secret=_GP_AUTH_SECRET,
        debug=False, proxy=proxy,
    )

    # 2FA / OTP 短信回调：从注册同一接码渠道续接。两段 OTP（1fa otp_sms /
    # 2fa otp_sms）都用它。
    def _wait_login_otp(_phone_arg: str = "", timeout: int = 180) -> Optional[str]:
        try:
            sms_request_another(api_key, sms_id)
        except Exception:
            pass
        time.sleep(2)
        return sms_wait_code(api_key, sms_id, timeout=timeout)

    try:
        if use_pin:
            # === PIN 登录（goto_pin 1fa + 2fa OTP），真机对齐 ===
            log.info("[%s] login via known PIN (goto_pin 1fa + 2fa OTP)", phone_e164)
            access_token, refresh_token = login_with_known_pin(
                gp, phone_e164, str(pin),
                log=lambda m: log.info("[%s] %s", phone_e164, m),
                wait_2fa_otp=_wait_login_otp,
            )
            if not access_token:
                log.warning("[%s] PIN 登录失败", phone_e164)
                try:
                    gp.close()
                except Exception:
                    pass
                return None
            # account_id 从 profile 反查（known-pin 内部已拿到，但没回传；这里
            # 用 user_profile 补一个，失败置空不影响付款）
            account_id = ""
            try:
                sc_p, data_p, _ = gp.user_profile(str(access_token))
                account_id = str(extract_account_id(data_p) or "")
            except Exception:
                pass
            client = GoPayAppClient(
                gp, phone=phone_e164, local=local, user_uuid=account_id,
                access_token=str(access_token), refresh_token=str(refresh_token or ""),
            )
            log.info("[%s] PIN 登录成功 (account_id=%s)", phone_e164, account_id)
            return {"phone": phone_e164, "aid": sms_id, "pin": pin, "client": client, "local": local}

        # === OTP 登录（1fa otp_sms + 2fa otp_sms），真机对齐 ===
        gp.new_cvs_session()
        log.info("[%s] login: login_methods…", phone_e164)
        sc, data, _ = gp.login_methods(local, country_code)
        methods = _pick_first(data, ["allowed_methods", "methods"]) or []
        vid = _pick_first(data, ["verification_id"])
        if not vid:
            sc, data, _ = gp.cvs_methods(local, flow="login_1fa", country_code=country_code)
            if not is_success_response(sc, data):
                log.error("[%s] login cvs_methods failed: HTTP %d %s", phone_e164, sc, data)
                return None
            vid = _pick_first(data, ["verification_id"])
        if not vid:
            log.error("[%s] login no verification_id", phone_e164)
            return None

        # 1fa: otp_sms initiate -> 接码 -> verify
        sc, data, _ = gp.cvs_initiate_login(local, str(vid), method="otp_sms",
                                            flow="login_1fa", country_code=country_code,
                                            is_multiple_method=True)
        if not is_success_response(sc, data, allow=(200, 201, 202, 204)):
            log.error("[%s] login_1fa initiate failed: HTTP %d %s", phone_e164, sc, data)
            return None
        otp_token = _pick_first(data, ["otp_token", "otpToken"])
        log.info("[%s] login: 等待 1fa OTP（sms_id=%s）…", phone_e164, sms_id)
        otp = _wait_login_otp(phone_e164, 180)
        if not otp:
            log.error("[%s] login_1fa OTP timeout", phone_e164)
            return None
        sc, data, _ = gp.cvs_verify(local, str(vid), str(otp), method="otp_sms",
                                    flow="login_1fa", country_code=country_code,
                                    otp_token=str(otp_token) if otp_token else None)
        if not is_success_response(sc, data):
            log.error("[%s] login_1fa verify failed: HTTP %d %s", phone_e164, sc, data)
            return None
        vtoken_1fa = _pick_first(data, ["verification_token", "verificationToken"])
        if not vtoken_1fa:
            log.error("[%s] login_1fa no verification_token", phone_e164)
            return None

        # accountlist -> account_id + 1fa_token
        sc, data_acct, _ = gp.accountlist(str(vtoken_1fa))
        if not is_success_response(sc, data_acct):
            log.error("[%s] login accountlist failed: HTTP %d %s", phone_e164, sc, data_acct)
            return None
        account_id = extract_account_id(data_acct) or ""
        one_fa = _pick_first(data_acct, ["1fa_token", "one_fa_token", "token"]) or vtoken_1fa

        # token(grant=cvs) -> 期望 403 need_2fa + 2fa_token
        sc, data_tok, _ = gp.token(verification_token=str(one_fa), account_id=str(account_id))
        access_token = _pick_first(data_tok, ["access_token", "accessToken"])
        refresh_token = _pick_first(data_tok, ["refresh_token", "refreshToken"])

        if not access_token:
            twofa_token = _pick_first(data_tok, ["2fa_token", "two_fa_token"])
            vid_2fa = _pick_first(data_tok, ["verification_id"])
            if not twofa_token or not vid_2fa:
                log.error("[%s] login 1fa 后无 access_token/2fa_token: HTTP %d %s",
                          phone_e164, sc, json.dumps(data_tok, ensure_ascii=False)[:300])
                return None
            log.info("[%s] login 需要 2FA，走第二段 OTP", phone_e164)
            # 2fa 延续 1fa 同一 txn（真机确认），不要 new_cvs_session。
            sc, d2, _ = gp.cvs_initiate_login(local, str(vid_2fa), method="otp_sms",
                                             flow="login_2fa", country_code=country_code,
                                             is_multiple_method=None)
            if not is_success_response(sc, d2, allow=(200, 201, 202, 204)):
                log.error("[%s] login_2fa initiate failed: HTTP %d %s", phone_e164, sc, d2)
                return None
            otp_token2 = _pick_first(d2, ["otp_token", "otpToken"])
            otp2 = _wait_login_otp(phone_e164, 180)
            if not otp2:
                log.error("[%s] login_2fa OTP timeout", phone_e164)
                return None
            sc, d2v, _ = gp.cvs_verify(local, str(vid_2fa), str(otp2), method="otp_sms",
                                       flow="login_2fa", country_code=country_code,
                                       otp_token=str(otp_token2) if otp_token2 else None)
            if not is_success_response(sc, d2v):
                log.error("[%s] login_2fa verify failed: HTTP %d %s", phone_e164, sc, d2v)
                return None
            vtoken_2fa = _pick_first(d2v, ["verification_token", "verificationToken"])
            sc, data_tok, _ = gp.token_2fa(str(twofa_token), str(vtoken_2fa), account_id=str(account_id))
            gp.clear_cvs_session()
            access_token = _pick_first(data_tok, ["access_token", "accessToken"])
            refresh_token = _pick_first(data_tok, ["refresh_token", "refreshToken"])

        if not access_token:
            log.error("[%s] login token exchange failed: HTTP %d %s",
                      phone_e164, sc, json.dumps(data_tok, ensure_ascii=False)[:300])
            return None

        client = GoPayAppClient(
            gp, phone=phone_e164, local=local, user_uuid=str(account_id),
            access_token=str(access_token), refresh_token=str(refresh_token or ""),
        )
        log.info("[%s] login success (account_id=%s)", phone_e164, account_id)
        return {"phone": phone_e164, "aid": sms_id, "pin": pin, "client": client, "local": local}
    except Exception as e:
        log.exception("[%s] login exception: %s", phone_e164, e)
        try:
            gp.close()
        except Exception:
            pass
        return None


def _rebind_one(client, *, new_phone: str, pin: str, wait_otp, email: str = "", otp_timeout: int = 180) -> dict:
    """对已登录的 GoPayAppClient 执行换绑（改绑新号 + 释放旧号）。"""
    if not hasattr(client, "rebind_phone"):
        return {"success": False, "detail": "client 不支持 rebind_phone（非 GoPayAppClient）"}
    return client.rebind_phone(
        new_phone=new_phone, pin=pin, wait_otp=wait_otp,
        email=email, otp_timeout=otp_timeout, log=log.info,
    )


def _rebind_release_registered(phone: str, pin: str, proxy: str, *, rebind_acquire,
                               login_api_key: str = "", login_sms_id: str = "") -> bool:
    """[已弃用] 旧的"登录已注册号→换绑释放本号"。保留以兼容历史调用。

    新流程见 ``_login_rebind_to_new_phone``：登录已注册号→解绑→换绑到**新印尼号**
    →返回新号用于付款（不再"释放本号重注册"）。
    """
    res = _login_rebind_to_new_phone(
        phone, pin, proxy, rebind_acquire=rebind_acquire,
        login_api_key=login_api_key, login_sms_id=login_sms_id,
    )
    return bool(res)


def _login_rebind_to_new_phone(phone: str, pin: str, proxy: str, *, rebind_acquire,
                               login_api_key: str = "", login_sms_id: str = "") -> Optional[dict]:
    """号已被注册时的正确处理：用已知 PIN 登录 → 解绑 OpenAI → 换绑到新印尼号。

    返回付款可用的账号 dict（``phone``=新印尼号，``client`` 已绑到该号，
    ``aid``/``api_key``/``sms_provider`` 指向**换绑渠道**——付款 OTP 要从新号接），
    失败返回 ``None``。

    流程：
      1. ``_login_one(use_pin=True)`` —— 用 PIN（默认147258）登录已注册号。
         goto_pin 完成 1fa，再接 2fa OTP（短信发到当前号，用注册渠道
         login_api_key/login_sms_id 接）。所有渠道都尝试登录（含 herosms）。
      2. 登录失败 → 返回 None（上层弃号重注册）。
      3. ``rebind_acquire()`` 买一个新印尼号 → ``client.rebind_phone`` 内部先
         解绑 OpenAI LLC，再 PATCH /v5/customers 换绑到新号、新号接换绑 OTP。
      4. 成功 → client.phone 已是新号，返回新号 + 换绑渠道接码信息供付款。
    """
    # 1. 用已知 PIN 登录（所有渠道一律先试 147258；2fa 短信从当前注册号接）
    logged = _login_one(phone, pin, proxy, use_pin=True,
                        api_key=login_api_key, sms_id=login_sms_id)
    if not logged or not logged.get("client"):
        log.warning("[%s] 已注册号 PIN 登录失败（PIN 可能不对，或 2fa 没接到）", phone)
        return None
    client = logged["client"]

    # 2. 买新印尼号 + 接码回调
    acq = rebind_acquire()
    # 兼容新(5元组含 meta)/旧(4元组)签名
    if acq and len(acq) == 5:
        new_phone, wait_otp, finish, cancel, meta = acq
    elif acq and len(acq) == 4:
        new_phone, wait_otp, finish, cancel = acq
        meta = {}
    else:
        new_phone = None
        wait_otp = finish = cancel = None
        meta = {}
    if not new_phone:
        log.warning("[%s] 换绑新号获取失败", phone)
        return None

    # 3. 解绑 OpenAI + 换绑到新号（rebind_phone 内部 unlink_openai_first 默认开）
    res = _rebind_one(client, new_phone=new_phone, pin=pin, wait_otp=wait_otp)
    ok = bool(res.get("success"))
    if not ok:
        log.warning("[%s] 解绑/换绑到新号失败: %s", phone, res.get("detail"))
        try:
            (cancel or (lambda: None))()
        except Exception:
            pass
        return None

    # 4. 换绑成功：client 现在绑定新号。组装付款可用 dict。
    new_local = str(new_phone).lstrip("+")
    if new_local.startswith("62"):
        new_local = new_local[2:]
    client.phone = new_phone
    client.local = new_local
    log.info("[%s] 解绑 OpenAI + 换绑成功 → 新印尼号 %s", phone, new_phone)
    # finish() 不在这里调——新号还要拿去付款接 OTP；付款流程结束后再归还。
    return {
        "phone": new_phone,
        "aid": str(meta.get("aid") or new_phone),
        "pin": pin,
        "client": client,
        "local": new_local,
        # 付款 OTP 要从换绑渠道的新号接（不是注册渠道），透传给上层
        "rebind_provider": str(meta.get("provider") or ""),
        "rebind_sms_key": str(meta.get("sms_key") or ""),
        "rebind_finish": finish,
    }


# ---------------------------------------------------------------------------
# 正确换绑获号：成熟老账号 + 未注册新号 -> 老账号改绑到新号 -> 用新号付款
#
# 背景（用户已验证）：Midtrans/GoPay 的风控判的是**账号本身**不是手机号。
# 全新注册的号秒付会被 FDS 判 202 FRAUD DENIED。正确做法是拿一个**成熟老
# 账号**（refresh_token 还活着、注册有一段时间了、风控信任）改绑到一个**新
# 的未注册号**上，再用这个新号进支付流程——这样付款走的是受信任的老账号
# 身份，但手机号是干净的新号。
# ---------------------------------------------------------------------------

# 本进程内已被换绑用掉的成熟号（换绑后该老号的 phone 已变更，但本地 json
# 不一定即时更新，靠内存集合去重，避免同一个老账号被重复换绑打架）。
_used_mature_phones: set = set()
_mature_lock = threading.Lock()


def _load_mature_accounts() -> list:
    """从 ``gopay_worker_accounts.json`` 读出可用的成熟号（带 refresh_token）。

    只挑同时有 ``phone`` + ``refresh_token`` + ``pin`` 的条目；按注册时间升序
    （越老越受风控信任，优先用最老的）。
    """
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with _accounts_lock:
            accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
    except Exception:
        return []
    if not isinstance(accounts, list):
        return []
    out = [
        a for a in accounts
        if isinstance(a, dict) and a.get("phone") and a.get("refresh_token") and a.get("pin")
    ]
    out.sort(key=lambda a: str(a.get("registered_at") or ""))
    return out


def _pick_mature_account(exclude: Optional[set] = None) -> Optional[dict]:
    """挑一个未被本进程用过的成熟号。"""
    exclude = exclude or set()
    with _mature_lock:
        for a in _load_mature_accounts():
            ph = str(a.get("phone") or "")
            if ph in exclude or ph in _used_mature_phones:
                continue
            _used_mature_phones.add(ph)
            return a
    return None


def _release_mature_account(phone: str) -> None:
    """换绑失败时把成熟号放回可用集合（让它能被下次再尝试）。"""
    with _mature_lock:
        _used_mature_phones.discard(str(phone or ""))


def _update_mature_account_after_rebind(old_phone: str, new_phone: str, new_local: str,
                                        access_token: str, refresh_token: str) -> None:
    """换绑成功后，把本地 json 里那条成熟号的手机号/token 更新成新号。

    这样这个成熟账号身份就"迁移"到新号上了，旧印尼号被释放，下次还能再被
    挑出来（但 phone 已经是新号）。失败静默忽略（不影响付款）。
    """
    if not os.path.exists(ACCOUNTS_FILE):
        return
    try:
        with _accounts_lock:
            accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
            if not isinstance(accounts, list):
                return
            for a in accounts:
                if isinstance(a, dict) and str(a.get("phone") or "") == str(old_phone):
                    a["phone"] = new_phone
                    a["local"] = new_local
                    if access_token:
                        a["access_token"] = access_token
                    if refresh_token:
                        a["refresh_token"] = refresh_token
                    a["rebound_from"] = old_phone
                    a["rebound_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    break
            open(ACCOUNTS_FILE, "w", encoding="utf-8").write(
                json.dumps(accounts, indent=2, ensure_ascii=False)
            )
    except Exception as exc:
        log.debug("[mature-rebind] update json failed: %s", exc)


def _resume_mature_client(account: dict, proxy: str) -> Optional["GoPayAppClient"]:
    """用成熟号的 refresh_token 刷新拿 client；失败回退 known-PIN(goto_pin) 登录。

    **设备指纹按老账号原始手机号 seed 派生**（同号同指纹），这点很重要——
    异设备/异指纹登录老账号容易被 GoPay 判风控拒绝。
    """
    old_phone = str(account.get("phone") or "")
    pin = str(account.get("pin") or DEFAULT_PIN)
    local = str(account.get("local") or old_phone.lstrip("+"))
    if local.startswith("62"):
        local = local[2:]

    # 关键：用老账号注册时的设备指纹（按 phone seed 确定性派生）
    device = build_device_profile(old_phone)
    gp = GoPayProtocol(
        device=device, signer=_make_app_signer(),
        client_id=_GP_AUTH_ID, client_secret=_GP_AUTH_SECRET,
        debug=False, proxy=proxy,
    )

    access_token = ""
    refresh_token = str(account.get("refresh_token") or "")

    # 路线 A：refresh_token 刷新（最稳，不触发任何 OTP/2FA）
    try:
        sc, data, _ = gp.token(refresh_token=refresh_token, account_id="")
        log.info("[mature-rebind] [%s] refresh_token 刷新 -> HTTP %d", old_phone, sc)
        if is_success_response(sc, data):
            access_token = str(_pick_first(data, ["access_token", "accessToken"]) or "")
            refresh_token = str(_pick_first(data, ["refresh_token", "refreshToken"]) or refresh_token)
    except Exception as exc:
        log.warning("[mature-rebind] [%s] refresh 异常: %s", old_phone, exc)

    # 路线 B：refresh 失败 -> 用已知 PIN 走 goto_pin 登录（仅对本项目自注册、
    # PIN 已知的老号有效）
    if not access_token:
        log.info("[mature-rebind] [%s] refresh 失败，回退 known-PIN(goto_pin) 登录", old_phone)
        try:
            at, rt = login_with_known_pin(gp, old_phone, pin, log=lambda m: log.info("[mature-rebind] %s", m))
            access_token = at or access_token
            refresh_token = rt or refresh_token
        except Exception as exc:
            log.warning("[mature-rebind] [%s] known-PIN 登录异常: %s", old_phone, exc)

    if not access_token:
        log.warning("[mature-rebind] [%s] 成熟号登录失败（refresh + known-PIN 都没成）", old_phone)
        try:
            gp.close()
        except Exception:
            pass
        return None

    client = GoPayAppClient(
        gp, phone=old_phone, local=local,
        user_uuid=str(account.get("customer_id") or ""),
        access_token=access_token, refresh_token=refresh_token,
    )
    return client


def _acquire_unregistered_phone(api_key: str, proxy: str, *, max_tries: int = 4):
    """从注册接码渠道租一个号，并确认它在 GoPay **未注册**。

    返回 ``(phone, aid, gp_probe)`` —— gp_probe 是探测用的 GoPayProtocol（已确认
    not_found，可丢弃）。号已注册的就 cancel 退回、换下一个。全失败返回
    ``(None, None, None)``。
    """
    for attempt in range(1, max_tries + 1):
        phone, aid = sms_get_number(api_key)
        if not phone:
            log.warning("[mature-rebind] 第 %d 次拿号失败（接码无号）", attempt)
            continue
        local = phone.lstrip("+")
        if local.startswith("62"):
            local = local[2:]
        gp = GoPayProtocol(
            device=build_device_profile(phone), signer=_make_app_signer(),
            client_id=_GP_AUTH_ID, client_secret=_GP_AUTH_SECRET,
            debug=False, proxy=proxy,
        )
        try:
            time.sleep(1)
            sc, data, _ = gp.login_methods(local, "+62")
            if has_error_code(data, "auth:error:user:not_found"):
                log.info("[mature-rebind] 新号未注册可用: %s", phone)
                return phone, aid, gp
            log.info("[mature-rebind] 新号 %s 已被注册，退回换下一个", phone)
        except Exception as exc:
            log.warning("[mature-rebind] 探测新号 %s 异常: %s", phone, exc)
        try:
            gp.close()
        except Exception:
            pass
        try:
            sms_cancel(api_key, aid)
        except Exception:
            pass
    return None, None, None


def _acquire_via_mature_rebind(api_key: str, pin: str, proxy: str) -> Optional[dict]:
    """正确换绑获号（用户最新指正的方向）。

    1. 取一个**未注册**新号（注册接码渠道，换绑 OTP + 后续付款 OTP 都从它接）
    2. 取一个**成熟老账号**（json 池里 refresh_token 活的，注册有一段时间）
    3. 老账号 refresh_token 刷新登录（失败回退 known-PIN goto_pin）
    4. ``customers_update_phone`` 把老账号改绑到新号 -> 新号接换绑 OTP ->
       ``customers_verify_update`` 完成
    5. 返回 ``{phone:新号, aid, pin:老账号PIN, client(已绑新号), local}``，
       下游用这个新号 + 老账号身份进支付流程（绕开新号秒付被 FDS 拒）。

    失败返回 None。``aid`` 用注册渠道的，付款阶段同号续接 OTP。
    """
    # 1. 未注册新号
    new_phone, aid, gp_probe = _acquire_unregistered_phone(api_key, proxy)
    if not new_phone:
        log.error("[mature-rebind] 没拿到可用的未注册新号")
        return None
    try:
        gp_probe.close()
    except Exception:
        pass
    new_local = new_phone.lstrip("+")
    if new_local.startswith("62"):
        new_local = new_local[2:]
    rented_at = time.time()

    # 2 + 3. 取成熟老账号并登录
    used: set = set()
    client = None
    account = None
    for _ in range(3):
        account = _pick_mature_account(exclude=used)
        if not account:
            log.error("[mature-rebind] 没有可用的成熟老账号（json 池空或都用过了）")
            try:
                sms_cancel(api_key, aid)
            except Exception:
                pass
            return None
        old_phone = str(account.get("phone") or "")
        log.info("[mature-rebind] 选中成熟号 %s（注册于 %s），登录中…",
                 old_phone, account.get("registered_at"))
        client = _resume_mature_client(account, proxy)
        if client:
            break
        used.add(old_phone)
        _release_mature_account(old_phone)  # 登录失败的放回，但本轮 exclude 掉
        account = None

    if not client or not account:
        log.error("[mature-rebind] 成熟号登录均失败，放弃")
        try:
            sms_cancel(api_key, aid)
        except Exception:
            pass
        return None

    old_phone = str(account.get("phone") or "")
    acct_pin = str(account.get("pin") or pin or DEFAULT_PIN)

    # 4. 老账号改绑到新号（换绑 OTP 从新号 = 注册渠道接）
    def _wait_rebind_otp(_phone_arg: str = "", timeout: int = 180) -> Optional[str]:
        try:
            sms_request_another(api_key, aid)
        except Exception:
            pass
        time.sleep(2)
        return sms_wait_code(api_key, aid, timeout=timeout)

    log.info("[mature-rebind] 成熟号 %s 改绑到新号 %s …", old_phone, new_phone)
    res = client.rebind_phone(
        new_phone=new_phone, pin=acct_pin, wait_otp=_wait_rebind_otp,
        otp_timeout=180, log=log.info,
    )
    if not res.get("success"):
        log.error("[mature-rebind] 换绑失败: %s", res.get("detail"))
        _release_mature_account(old_phone)
        try:
            sms_cancel(api_key, aid)
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        return None

    # 换绑成功：client 现在绑定新号。同步本地 json（老账号身份迁到新号）。
    client.phone = new_phone
    client.local = new_local
    _update_mature_account_after_rebind(
        old_phone, new_phone, new_local,
        client.auth.access_token, client.auth.refresh_token,
    )
    log.info("[mature-rebind] 换绑成功：老账号 %s 已迁到新号 %s（用时 %.0fs）",
             old_phone, new_phone, time.time() - rented_at)
    # 落库新号（沿用注册号的存储格式，PIN=老账号PIN）
    try:
        _save_account(new_phone, new_local, acct_pin, aid, client)
    except Exception as exc:
        log.debug("[mature-rebind] _save_account 忽略: %s", exc)

    return {"phone": new_phone, "aid": aid, "pin": acct_pin, "client": client, "local": new_local}


# ---------------------------------------------------------------------------
# Job handling
# ---------------------------------------------------------------------------

def _job_remaining_sec(job: dict) -> float:
    expires = job.get("expires_at", "")
    if not expires:
        return 3600
    try:
        exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        return (exp - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return 3600


def _get_envelope_did() -> str:
    try:
        url = f"{INBOX_URL}/api/envelopes"
        req = urllib.request.Request(url)
        cred = base64.b64encode(f"{INBOX_USER}:{INBOX_PASS}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        for e in data.get("envelopes", []):
            if e.get("status") == "active":
                return e["deeplink_id"]
    except Exception as exc:
        log.debug("Failed to fetch envelope from inbox: %s", exc)
    return ""


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

def _pay_job(job: dict, account: dict, inbox_client, api_key: str, pin: str, proxy: str = "") -> tuple[bool, str]:
    job_id = job["id"]
    midtrans_url = job.get("provider_url") or job.get("paypal_url") or ""
    phone = account["local"]
    log.info("[job:%s] Paying with %s (protocol)", job_id[:8], account["phone"])

    try:
        payment_profile = ensure_account_payment_fingerprint(account)
        log.info("[job:%s] payment profile_id=%s", job_id[:8], payment_profile.get("profile_id", ""))
        payment = GoPayPayment(proxy=proxy, payment_fingerprint=payment_profile)

        def wait_otp(ph: str, timeout: int = 120) -> Optional[str]:
            try:
                sms_api(api_key, "setStatus", {"id": account["aid"], "status": "3"})
            except Exception:
                pass
            time.sleep(2)
            return sms_wait_code(api_key, account["aid"], timeout=timeout)

        result = payment.pay(
            midtrans_url=midtrans_url,
            phone=phone,
            country_code="62",
            pin=pin,
            wait_otp=wait_otp,
        )

        detail = result.get("detail", "")
        if result.get("success"):
            log.info("[job:%s] Payment SUCCESS!", job_id[:8])
            try:
                inbox_client._req("PUT", f"/api/jobs/{job_id}/paid")
            except Exception as e:
                log.error("[job:%s] Mark paid failed: %s", job_id[:8], e)
            return True, detail
        else:
            log.warning("[job:%s] Payment failed: %s", job_id[:8], detail)
            try:
                inbox_client._req("PUT", f"/api/jobs/{job_id}/cancel")
            except Exception:
                pass
            return False, detail

    except GoPayFraudDenyError as e:
        log.warning("[job:%s] FRAUD DENIED: %s", job_id[:8], e)
        try:
            inbox_client._req("PUT", f"/api/jobs/{job_id}/cancel")
        except Exception:
            pass
        return False, "fraud_deny -- phone burned"

    except Exception as e:
        log.exception("[job:%s] Payment exception: %s", job_id[:8], e)
        try:
            inbox_client._req("PUT", f"/api/jobs/{job_id}/cancel")
        except Exception:
            pass
        return False, str(e)


def _claim_job(inbox, min_remaining: float = MIN_REMAINING_SEC) -> Optional[dict]:
    try:
        job = inbox._req("POST", "/api/jobs/claim_next", data={
            "prefer_paypal_url": False, "prefer_oldest": True, "provider": "gopay",
        })
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            log.warning("Inbox poll error: %s", e)
        return None
    except Exception as e:
        log.warning("Inbox poll error: %s", e)
        return None

    if job is None:
        return None

    url = job.get("provider_url") or job.get("paypal_url") or ""
    if "midtrans" not in url:
        return None

    remaining = _job_remaining_sec(job)
    if remaining < min_remaining:
        log.info("Job %s: %.0fs left < %ds, cancelling", job["id"][:8], remaining, min_remaining)
        try:
            inbox._req("PUT", f"/api/jobs/{job['id']}/cancel")
        except Exception:
            pass
        return None

    return job


# ---------------------------------------------------------------------------
# Phone reactivation
# ---------------------------------------------------------------------------

_PHONE_LIFETIME = 1080


def _sms_reactivate(api_key: str, activation_id: str) -> Optional[str]:
    try:
        s = tls_client.Session(client_identifier="chrome_120")
        r = s.post("https://hero-sms.com/stubs/handler_api.php", params={
            "api_key": api_key, "action": "reactivate", "id": activation_id,
        }, timeout_seconds=15)
        log.info("[reactivate] aid=%s -> %d: %s", activation_id, r.status_code, r.text[:200])
        if r.status_code == 200:
            data = r.json()
            new_aid = str(data.get("activationId", ""))
            if new_aid:
                return new_aid
        return None
    except Exception as e:
        log.warning("[reactivate] aid=%s failed: %s", activation_id, e)
        return None


def _resume_account(phone: str, proxy: str = "") -> Optional[dict]:
    if not os.path.exists(ACCOUNTS_FILE):
        log.error("[resume] %s not found", ACCOUNTS_FILE)
        return None
    accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
    digits = phone.strip().lstrip("+")
    entry = None
    for a in accounts:
        a_digits = a["phone"].strip().lstrip("+")
        if a_digits == digits or a.get("local", "") == digits or digits.endswith(a.get("local", "\x00")):
            entry = a
            break
    if not entry:
        log.error("[resume] phone %s not found in %s", phone, ACCOUNTS_FILE)
        return None

    if not proxy:
        proxy = _make_proxy()

    # 用 GoPay App 协议重建 client（设备指纹按手机号确定性派生，同号同指纹）。
    device = build_device_profile(entry["phone"])
    gp = GoPayProtocol(
        device=device,
        signer=_make_app_signer(),
        client_id=_GP_AUTH_ID,
        client_secret=_GP_AUTH_SECRET,
        debug=False,
        proxy=proxy,
    )
    client = GoPayAppClient(
        gp,
        phone=entry["phone"],
        local=entry.get("local", ""),
        user_uuid=entry.get("customer_id", ""),
        access_token=entry.get("access_token", ""),
        refresh_token=entry.get("refresh_token", ""),
    )

    log.info("[resume] Refreshing token for %s...", entry["phone"])
    try:
        r = client.refresh_token()
        if r["status"] in (200, 201):
            log.info("[resume] Token refreshed OK for %s", entry["phone"])
        else:
            log.warning("[resume] Token refresh returned %d, trying with existing token", r["status"])
    except Exception as e:
        log.warning("[resume] Token refresh failed: %s, trying with existing token", e)

    # 为账号持久化/装载稳定支付指纹（同号同指纹）。
    payment_profile = ensure_account_payment_fingerprint(entry)
    with _accounts_lock:
        try:
            accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
            for idx, a in enumerate(accounts):
                if isinstance(a, dict) and (str(a.get("phone", "")).strip().lstrip("+") == digits or str(a.get("local", "")).strip() == digits):
                    accounts[idx] = entry
                    break
            open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, ensure_ascii=False, indent=2))
        except Exception as exc:
            log.warning("[resume] persist payment_fingerprint failed: %s", exc)

    return {
        "phone": entry["phone"],
        "client": client,
        "aid": entry.get("activation_id", ""),
        "pin": entry.get("pin", DEFAULT_PIN),
        "local": entry.get("local", ""),
        "payment_fingerprint": payment_profile,
        "resumed": True,
    }


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _worker_loop(
    inbox, api_key: str, pin: str, stop: threading.Event,
    worker_id: int,
    resume_phone: str = "",
):
    tag = f"[w{worker_id}]"
    envelope_did = _get_envelope_did()

    while not stop.is_set():
        # === Register or resume ===
        if resume_phone:
            log.info("%s Resuming account %s...", tag, resume_phone)
            proxy = _make_proxy()
            account = _resume_account(resume_phone, proxy)
            resume_phone = ""
        else:
            new_did = _get_envelope_did()
            if new_did:
                envelope_did = new_did
            log.info("%s Registering new GoPay account...", tag)
            proxy = _make_proxy()
            account = _register_one(api_key, pin, proxy, envelope_did)

        if not account:
            log.warning("%s Registration/resume failed, retry in 10s", tag)
            stop.wait(10)
            continue

        phone = account["phone"]
        client = account["client"]
        aid = account["aid"]
        is_resumed = account.get("resumed", False)
        register_time = 0 if is_resumed else time.time()
        log.info("%s Account ready: %s%s", tag, phone, " (resumed)" if is_resumed else "")

        # === Wait for balance >= MIN_BALANCE_RP ===
        balance_ok = False
        max_wait = 3600
        wait_start = time.time()
        phone_activated_at = register_time
        reactivate_count = 0
        max_reactivates = 3
        while not stop.is_set():
            if time.time() - wait_start > max_wait:
                log.warning("%s Waited %ds for balance, giving up", tag, max_wait)
                break

            phone_age = time.time() - phone_activated_at
            if phone_age > _PHONE_LIFETIME - 120:
                if reactivate_count < max_reactivates:
                    log.info("%s Phone expiring during balance wait, reactivating (%d/%d)...",
                             tag, reactivate_count + 1, max_reactivates)
                    new_aid = _sms_reactivate(api_key, aid)
                    if new_aid:
                        aid = new_aid
                        account["aid"] = new_aid
                        phone_activated_at = time.time()
                        reactivate_count += 1
                    else:
                        log.warning("%s Reactivate failed during balance wait, phone may be lost", tag)
                        reactivate_count += 1

            bal = _check_balance(client)
            if bal >= MIN_BALANCE_RP:
                log.info("%s Balance=%d Rp (>=%d), ready!", tag, bal, MIN_BALANCE_RP)
                _update_account_balance(phone, bal, client)
                _inbox_delete_account(phone)
                balance_ok = True
                break
            elif bal >= 0:
                waited = int(time.time() - wait_start)
                log.info("%s Balance=%d Rp (need >=%d), waiting 15s... (%ds elapsed)", tag, bal, MIN_BALANCE_RP, waited)
                stop.wait(15)
            else:
                log.warning("%s Balance check failed, trying token refresh", tag)
                try:
                    client.refresh_token()
                except Exception:
                    pass
                stop.wait(30)

        if not balance_ok:
            log.info("%s No balance after waiting, registering new account", tag)
            continue

        # === Payment loop ===
        while not stop.is_set():
            phone_age = time.time() - phone_activated_at
            if phone_age > _PHONE_LIFETIME - 120:
                if reactivate_count >= max_reactivates:
                    log.info("%s Max reactivates (%d) reached, retiring phone", tag, max_reactivates)
                    break
                log.info("%s Phone expiring, reactivating (%d/%d)...", tag, reactivate_count + 1, max_reactivates)
                new_aid = _sms_reactivate(api_key, aid)
                if new_aid:
                    aid = new_aid
                    account["aid"] = new_aid
                    phone_activated_at = time.time()
                    reactivate_count += 1
                    log.info("%s Reactivated, new aid=%s", tag, new_aid)
                else:
                    log.warning("%s Reactivate failed, retiring phone", tag)
                    break

            job = _claim_job(inbox)
            if not job:
                stop.wait(POLL_INTERVAL)
                continue

            remaining = _job_remaining_sec(job)
            phone_left = _PHONE_LIFETIME - (time.time() - phone_activated_at)
            log.info("%s Job %s -> %s (job %.0fs, phone %.0fs)",
                     tag, job["id"][:8], phone, remaining, phone_left)

            success, detail = _pay_job(job, account, inbox, api_key, pin, proxy=proxy)
            if success:
                log.info("%s Job %s paid!", tag, job["id"][:8])
                break

            if "fraud_deny" in detail.lower() or "fraud denied" in detail.lower() or "burned" in detail.lower():
                log.warning("%s FRAUD DENIED, retiring phone", tag)
                break

            if "already linked" in detail.lower():
                log.warning("%s Already linked, retiring phone", tag)
                break

            log.warning("%s Job %s failed (%s), next job", tag, job["id"][:8], detail[:60])

        # === Release phone ===
        try:
            sms_done(api_key, aid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_worker(
    max_workers: int = 3,
    pin: str = DEFAULT_PIN,
    poll_interval: float = POLL_INTERVAL,
    resume_phones: Optional[list] = None,
    api_key: str = "",
):
    from .payment_inbox import PaymentInboxClient

    if not api_key:
        api_key = os.environ.get("OPAI_HEROSMS_API_KEY", "")
    if not api_key:
        api_key_file = os.environ.get("OPAI_HEROSMS_API_KEY_FILE", "")
        if api_key_file and os.path.exists(api_key_file):
            api_key = open(api_key_file).read().strip()
    if not api_key:
        log.error("No hero-sms API key. Set OPAI_HEROSMS_API_KEY or OPAI_HEROSMS_API_KEY_FILE")
        return

    inbox = PaymentInboxClient(base_url=INBOX_URL, basic_auth=(INBOX_USER, INBOX_PASS))
    stop = threading.Event()

    resume_phones = resume_phones or []
    actual_workers = max(max_workers, len(resume_phones))
    log.info("Worker started: workers=%d poll=%.0fs resume=%s ttl=%ds",
             actual_workers, poll_interval, resume_phones or "(none)", GOPAY_ACCOUNT_TTL)
    _inbox_ttl_cleanup()

    threads = []
    for i in range(actual_workers):
        rp = resume_phones[i] if i < len(resume_phones) else ""
        t = threading.Thread(
            target=_worker_loop,
            args=(inbox, api_key, pin, stop, i),
            kwargs={"resume_phone": rp},
            daemon=True, name=f"w{i}",
        )
        t.start()
        threads.append(t)
        time.sleep(2)

    try:
        while True:
            alive = sum(1 for t in threads if t.is_alive())
            if alive == 0:
                log.error("All workers dead, exiting")
                break
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Shutting down")
        stop.set()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GoPay Protocol Worker")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--pin", default=DEFAULT_PIN)
    parser.add_argument("--poll", type=float, default=POLL_INTERVAL)
    parser.add_argument("--api-key", default="", help="Hero-SMS API key (or set OPAI_HEROSMS_API_KEY)")
    parser.add_argument("--dry-run", action="store_true", help="Register one account only, no inbox")
    parser.add_argument("--resume", nargs="+", metavar="PHONE", help="Resume from existing accounts")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    if args.dry_run:
        log.info("=== DRY RUN: register one account ===")
        api_key = args.api_key or os.environ.get("OPAI_HEROSMS_API_KEY", "")
        if not api_key:
            log.error("No API key")
            return
        proxy = _make_proxy()
        envelope_did = _get_envelope_did()
        result = _register_one(api_key, args.pin, proxy, envelope_did)
        if result:
            log.info("SUCCESS: %s", result["phone"])
            sms_done(api_key, result["aid"])
        else:
            log.error("FAILED")
        return

    run_worker(max_workers=args.workers, pin=args.pin, poll_interval=args.poll,
               resume_phones=args.resume, api_key=args.api_key)


if __name__ == "__main__":
    main()
