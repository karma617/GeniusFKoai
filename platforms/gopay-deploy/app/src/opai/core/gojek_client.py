"""
Gojek/GoPay Complete Protocol Client

Covers the full lifecycle:
  1. Registration (api.gojekapi.com /v7/customers/signup)
  2. Login        (accounts.goto-products.com /goto-auth)
  3. OTP          (accounts.goto-products.com /cvs  +  api.gojekapi.com /v6/customers)
  4. GoPay Register (customer.gopayapi.com /v1/customer/payment-options/register)
  5. PIN Setup      (customer.gopayapi.com /v2/users/pin + /api/v1/users/pins/setup)
  6. Wallet Ops     (customer.gopayapi.com)
  7. Envelope Claim (customer.gopayapi.com POST /v1/festivals/link)

Verification status:
  ✅ VERIFIED  — GoPay customer API + V2 signing (Frida capture + live 200 OK)
  ✅ VERIFIED  — SignUp headers captured via Frida gadget (2026-05-14):
                  X-DeviceCheckToken = "LITMUS_DISABLED" (Play Integrity OFF)
                  X-Signature = "1003" (SDK version, not crypto)
                  X-Signature-Time = unix timestamp
  ✅ VERIFIED  — Envelope claim: POST /v1/festivals/link body={"link_id":"..."}
                  Captured via VM memory scan on BlueStacks (2026-05-16)
                  Response: 422 GoPay-36006 = expired, 200 = claimed
  ⚠️ UNVERIFIED — SSO, CVS, PIN endpoints (decompiled, not live-tested yet)

Device tokens (ALL can be generated/hardcoded, NO real device needed):
  D1            — DexGuard cert fingerprint, STATIC per APK version (hardcoded)
  X-UniqueId    — random hex, os.urandom(8).hex()
  X-M1          — device telemetry, format known, construct from template
  X-DeviceToken — FCM push token, can be empty
  X-DeviceCheckToken — "LITMUS_DISABLED" (Play Integrity disabled by RemoteConfig)
  X-Signature   — "1003" (SDK version number, not a signature)

RE source: jadx_classes (SignUpApi), jadx_c2 (PinApi), jadx_c4 (SCP Login SDK),
           jadx_c11 (CVS Verification), jadx_hi (PaymentWidgetCardService)
Signing: gopay_signer_v2.py (HMAC-SHA256, verified via Frida 2026-05-07)
"""

import base64
import hashlib
import json
import logging
import os
import random
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import tls_client

from .gopay_signer_v2 import sign_v2

from .log_redaction import install_sensitive_log_filter

log = logging.getLogger(__name__)
install_sensitive_log_filter(log)

CLIENT_ID = "gojek:consumer:app"
CLIENT_SECRET = "pGwQ7oi8bKqqwvid09UrjqpkMEHklb"
# Original APK signing cert D1 (same for all installs of this APK version)
ORIGINAL_D1 = "CF:43:60:94:46:9C:A0:8F:CB:5C:95:05:97:E9:03:51:40:0A:C7:33:EC:BA:40:71:F1:94:DC:CE:BA:AE:4C:A8"

SSO_BASE = "https://accounts.goto-products.com"
GOPAY_BASE = "https://customer.gopayapi.com"
GOJEK_API_BASE = "https://api.gojekapi.com"

# Indonesian phone device profiles for randomization
_DEVICE_PROFILES = [
    # (brand, manufacturer, model, board_platform, cpu_freq_mhz, cpu_cores, screen)
    ("samsung", "samsung",  "SM-A546E",   "exynos1380", 2400, 8, "1080x2340"),
    ("samsung", "samsung",  "SM-S911B",   "kalama",     3360, 8, "1080x2340"),
    ("samsung", "samsung",  "SM-A256E",   "exynos1280", 2400, 8, "1080x2340"),
    ("samsung", "samsung",  "SM-S908E",   "taro",       2000, 5, "1080x2340"),
    ("Xiaomi",  "Xiaomi",   "23053RN02A", "mt6768",     2000, 8, "1080x2400"),
    ("Xiaomi",  "Xiaomi",   "2201117TY",  "taro",       3000, 8, "1080x2400"),
    ("OPPO",    "OPPO",     "CPH2565",    "mt6833",     2200, 8, "720x1612"),
    ("vivo",    "vivo",     "V2248",      "mt6769",     2000, 8, "720x1612"),
    ("POCO",    "Xiaomi",   "23049PCD8G", "mt6833",     2200, 8, "1080x2400"),
    ("realme",  "realme",   "RMX3710",    "mt6833",     2200, 8, "1080x2400"),
    ("OnePlus", "OnePlus",  "KB2005",     "kona",       2840, 8, "1080x2400"),
    ("Google",  "Google",   "Pixel 7",    "tensor",     2850, 8, "1080x2400"),
]


def generate_device_identity(seed: str) -> dict:
    """Generate a deterministic, unique device identity from a seed (e.g. phone number).

    Same seed always produces the same identity. Different seeds produce
    different identities. All fields match real Android device patterns.

    Returns dict with all fields needed for GojekClient constructor.
    """
    h = hashlib.sha256(seed.encode()).digest()
    rng = random.Random(seed)

    profile = _DEVICE_PROFILES[rng.randint(0, len(_DEVICE_PROFILES) - 1)]
    brand, manufacturer, model_name, platform, cpu_freq, cpu_cores, screen = profile

    android_id = h[:8].hex()

    drm_id = hashlib.sha256(b"widevine:" + seed.encode()).digest()
    drm_id_b64 = base64.b64encode(drm_id).decode().rstrip("=")

    # WiFi MAC: locally-administered (bit 1 of first octet set)
    mac_bytes = h[8:14]
    mac_first = (mac_bytes[0] | 0x02) & 0xFE  # locally administered, unicast
    mac = f"{mac_first:02X}:{mac_bytes[1]:02X}:{mac_bytes[2]:02X}:{mac_bytes[3]:02X}:{mac_bytes[4]:02X}:{mac_bytes[5]:02X}"

    # Install timestamp (deterministic but realistic — within last 30 days)
    base_ts = int(time.time() * 1000) - rng.randint(86400_000, 2592000_000)
    install_random = struct.unpack(">Q", h[14:22])[0]

    # Disk size MB (realistic range)
    disk_mb = rng.choice([32768, 65536, 128000, 131072, 262144])

    # Session ID
    session_id = str(uuid.UUID(bytes=h[22:38] if len(h) >= 38 else h[:16]))

    # X-M1 telemetry string
    xm1 = (
        f"1:UNKNOWN,2:UNKNOWN"
        f",3:{base_ts}-{install_random}"
        f",4:{disk_mb}"
        f",5:{platform}|{cpu_freq}|{cpu_cores}"
        f",6:{mac}"
        f',7:<unknown ssid>'
        f",8:{screen}"
        r",9:passive\,fused\,gps"
        f",10:0"
        f",11:{drm_id_b64}"
        f",12:VKEY_DISABLED"
        f",13:1003"
        f",14:{int(time.time())}"
        f",16:0,17:1"
    )

    android_ver = rng.choice(["11", "12", "13", "14"])

    return {
        "d1": ORIGINAL_D1,
        "model": f"{brand},{model_name}",
        "uniqueid": android_id,
        "xm1": xm1,
        "phone_make": manufacturer,
        "os_info": f"Android,{android_ver}",
        "version": "5.60.1",
        "session_id": session_id,
    }


@dataclass
class AuthState:
    """Mutable auth state accumulated across the login flow."""

    transaction_id: str = ""
    verification_id: str = ""
    otp_token: str = ""
    otp_length: int = 4
    otp_channel: str = ""
    verification_token: str = ""
    onefa_token: str = ""
    account_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    twofa_token: str = ""
    twofa_methods: list = field(default_factory=list)
    user_registered: bool = True
    methods: list = field(default_factory=list)

    # PIN flow state
    pin_otp_auth_token: str = ""
    pin_challenge_id: str = ""
    pin_client_id: str = ""
    pin_token: str = ""


class GojekClient:
    """Complete Gojek/GoPay protocol client."""

    def __init__(
        self,
        *,
        d1: str,
        model: str,
        uniqueid: str,
        xm1: str,
        phone_make: str = "Google",
        os_info: str = "Android,13",
        appid: str = "com.gojek.app",
        version: str = "5.60.1",
        user_uuid: str = "",
        session_id: str = "",
        device_token: str = "",
        access_token: str = "",
        refresh_token: str = "",
        proxy: str = "",
    ):
        self.d1 = d1
        self.model = model
        self.uniqueid = uniqueid
        self.xm1_template = xm1
        self.phone_make = phone_make
        self.os_info = os_info
        self.appid = appid
        self.version = version
        self.user_uuid = user_uuid
        self.session_id = session_id or str(uuid.uuid4())
        self.device_token = device_token
        self.proxy = proxy

        self.auth = AuthState(
            access_token=access_token,
            refresh_token=refresh_token,
            transaction_id=str(uuid.uuid4()),
        )

        self._session = self._create_session()

    def _create_session(self) -> tls_client.Session:
        """Create TLS session with optional SOCKS5/HTTP proxy."""
        s = tls_client.Session(
            client_identifier="okhttp4_android_13",
            random_tls_extension_order=True,
            force_http1=True,
        )
        if self.proxy:
            s.proxies = {
                "http": self.proxy,
                "https": self.proxy,
            }
        return s

    @classmethod
    def from_phone(cls, phone: str, proxy: str = "") -> "GojekClient":
        """Create client with deterministic device identity derived from phone number.

        Same phone always gets the same device fingerprint (android_id, MAC, DRM ID, etc).
        Different phones get completely different identities.

        Args:
            phone: Phone number as seed for device identity
            proxy: SOCKS5/HTTP proxy URL, e.g. "socks5://user:pass@host:port"
                   or "http://user:pass@host:port"
        """
        identity = generate_device_identity(phone)
        identity["proxy"] = proxy
        return cls(**identity)

    @classmethod
    def from_device_info(
        cls,
        appinfo_path: str,
        headers_path: Optional[str] = None,
    ) -> "GojekClient":
        """Create from captured device appinfo + headers files."""
        with open(appinfo_path) as f:
            lines = f.read().strip().split("\n")
        fields = {}
        for line in lines:
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v

        hdrs = {}
        if headers_path:
            with open(headers_path) as f:
                for line in f.read().strip().split("\n"):
                    if (
                        ": " in line
                        and not line.startswith("URL:")
                        and not line.startswith("TIME:")
                        and line != "---END---"
                    ):
                        k, v = line.split(": ", 1)
                        hdrs[k] = v

        return cls(
            d1=fields.get("supportPdam", ""),
            model=fields.get("supportBpjs", "google,sdk_gphone64_x86_64"),
            uniqueid=fields.get("supportInsurance", ""),
            xm1=fields.get("supportInternetCable", ""),
            phone_make=hdrs.get("X-PhoneMake", "Google"),
            user_uuid=hdrs.get("User-uuid", ""),
            session_id=hdrs.get("X-Session-ID", ""),
            device_token=hdrs.get("X-DeviceToken", ""),
            access_token=fields.get("supportPulsa", ""),
        )

    # ========================================================================
    # Internal: header builders
    # ========================================================================

    def _build_xm1(self) -> str:
        ts_sec = str(int(time.time()))
        return re.sub(r"14:\d+", f"14:{ts_sec}", self.xm1_template)

    def _sso_headers(self, extra: Optional[dict] = None) -> dict:
        """Headers for SSO / CVS endpoints (no WibbleDazzle signing)."""
        h = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-UniqueId": self.uniqueid,
            "X-Platform": "Android",
            "X-DeviceOS": self.os_info,
            "X-PhoneMake": self.phone_make,
            "X-PhoneModel": self.model,
            "X-User-Type": "customer",
            "X-AuthSDK-Version": "3.103.0",
            "Transaction-ID": self.auth.transaction_id,
            "User-Agent": f"Gojek/{self.version} ({self.appid}; build:5602; {self.os_info})",
            "Accept-Language": "en-ID",
            "Accept-Encoding": "br,gzip",
        }
        if self.auth.access_token:
            tok = self.auth.access_token
            if not tok.startswith("Bearer "):
                tok = f"Bearer {tok}"
            h["Authorization"] = tok
        if extra:
            h.update(extra)
        return h

    def _gopay_signed_headers(
        self, path: str, method: str = "GET", body: str = "", extra: Optional[dict] = None
    ) -> dict:
        """Headers for GoPay customer API (with WibbleDazzle signing).

        Header set verified via VM memory scan (2026-05-16) against GoPay 2.7.0 app.
        """
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))

        sig = sign_v2(
            token=self.auth.access_token,
            timestamp_ms=ts,
            url=f"customer.gopayapi.com{path}",
            method=method,
            body=body,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )

        tok = self.auth.access_token
        if tok and not tok.startswith("Bearer "):
            tok = f"Bearer {tok}"

        h = {
            "User-Agent": f"Gojek/{self.version} ({self.appid}; build:5602; {self.os_info})",
            "D1": self.d1,
            "X-Session-ID": self.session_id,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "Authorization": tok,
            "X-User-Type": "customer",
            "X-AppType": "GOPAY",
            "X-DeviceOS": self.os_info,
            "User-uuid": self.user_uuid,
            "X-DeviceToken": self.device_token,
            "X-PhoneMake": self.phone_make,
            "X-PushTokenType": "FCM",
            "X-PhoneModel": self.model,
            "Accept-Language": "id-ID",
            "X-User-Locale": "id_ID",
            "X-Location": "-6.2088,106.8456",
            "X-Location-Accuracy": "5.0",
            "Gojek-Country-Code": "ID",
            "Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "br,gzip",
            "X-Dark-Mode": "false",
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-E3": sig["X-E3"],
            "AdjTs": "ts:A",
            "support-sdk-version": "0.49.1",
        }
        if extra:
            h.update(extra)
        return h

    def _gojek_api_signed_headers(
        self, path: str, method: str = "POST", body: str = "", extra: Optional[dict] = None
    ) -> dict:
        """Headers for api.gojekapi.com — needs WibbleDazzle signing like GoPay."""
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))

        sig = sign_v2(
            token=self.auth.access_token,
            timestamp_ms=ts,
            url=f"api.gojekapi.com{path}",
            method=method,
            body=body,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )

        tok = self.auth.access_token
        if tok and not tok.startswith("Bearer "):
            tok = f"Bearer {tok}"

        h = {
            "User-Agent": f"Gojek/{self.version} ({self.appid}; build:5602; {self.os_info})",
            "D1": self.d1,
            "X-Session-ID": self.session_id,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "User-uuid": self.user_uuid,
            "X-DeviceToken": self.device_token,
            "X-PhoneMake": self.phone_make,
            "X-PushTokenType": "FCM",
            "X-PhoneModel": self.model,
            "Accept-Language": "en-ID",
            "X-User-Locale": "en_ID",
            "X-Location": "-6.2088,106.8456",
            "X-Location-Accuracy": "5.0",
            "Gojek-Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "br,gzip",
            "X-Dark-Mode": "false",
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-E3": sig["X-E3"],
            "AdjTs": "ts:A",
            "support-sdk-version": "0.49.1",
        }
        if tok:
            h["Authorization"] = tok
        if extra:
            h.update(extra)
        return h

    # ========================================================================
    # Internal: HTTP helpers
    # ========================================================================

    def _gojek_api_post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        body_str = json.dumps(body)
        headers = self._gojek_api_signed_headers(path, "POST", body_str, extra_headers)
        log.debug("POST %s Authorization=%s", path, headers.get("Authorization", "(MISSING)"))
        resp = self._session.post(
            f"{GOJEK_API_BASE}{path}",
            headers=headers,
            data=body_str,
            timeout_seconds=15,
        )
        log.debug("GojekAPI POST %s → %d", path, resp.status_code)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def _sso_post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        body_str = json.dumps(body)
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))
        sig = sign_v2(
            token=self.auth.access_token,
            timestamp_ms=ts,
            url=f"accounts.goto-products.com{path}",
            method="POST",
            body=body_str,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )
        headers = self._sso_headers(extra_headers)
        headers.update({
            "D1": self.d1,
            "X-Session-ID": self.session_id,
            "X-M1": xm1,
            "X-CVSDK-Version": "3.73.0",
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-E3": sig["X-E3"],
            "AdjTs": "ts:A",
            "Gojek-Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "gzip",
        })
        for _retry in range(3):
            try:
                resp = self._session.post(
                    f"{SSO_BASE}{path}",
                    headers=headers,
                    data=body_str,
                    timeout_seconds=15,
                )
                break
            except Exception as e:
                if _retry < 2:
                    log.warning("SSO POST %s retry %d: %s", path, _retry + 1, e)
                    time.sleep(2)
                else:
                    raise
        log.debug("SSO POST %s → %d", path, resp.status_code)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def _gopay_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        body_str = json.dumps(body) if body else ""
        headers = self._gopay_signed_headers(path, method, body_str, extra_headers)
        url = f"{GOPAY_BASE}{path}"
        fn = getattr(self._session, method.lower())
        kwargs = {"headers": headers, "timeout_seconds": 15}
        if body_str:
            kwargs["data"] = body_str
        for _retry in range(3):
            try:
                resp = fn(url, **kwargs)
                break
            except Exception as e:
                if _retry < 2:
                    log.warning("GoPay %s %s retry %d: %s", method, path, _retry + 1, e)
                    time.sleep(2)
                else:
                    raise
        log.debug("GoPay %s %s → %d", method, path, resp.status_code)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def _gopay_get(self, path: str) -> dict:
        return self._gopay_request("GET", path)

    def _gopay_post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        return self._gopay_request("POST", path, body, extra_headers)

    def _gopay_put(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        return self._gopay_request("PUT", path, body, extra_headers)

    def _gopay_patch(self, path: str, body: dict) -> dict:
        return self._gopay_request("PATCH", path, body)

    def _gopay_delete(self, path: str) -> dict:
        return self._gopay_request("DELETE", path)

    # ========================================================================
    # Phase 0: Signup — Legacy registration (api.gojekapi.com)
    #   Source: jadx_classes/com/gojek/app/api/signup/SignUpApi.java
    #   Status: ⚠️ UNVERIFIED — X-DeviceCheckToken + X-Signature generation TBD
    # ========================================================================

    def _signup_headers(self, extra: Optional[dict] = None) -> dict:
        """Headers for signup endpoints — NO WibbleDazzle signing."""
        xm1 = self._build_xm1()
        h = {
            "D1": self.d1,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "User-Agent": f"Gojek/{self.version} ({self.appid}; build:5602; {self.os_info})",
            "X-Session-ID": self.session_id,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "X-PhoneMake": self.phone_make,
            "X-PushTokenType": "FCM",
            "X-PhoneModel": self.model,
            "X-M1": xm1,
            "X-CVSDK-Version": "3.73.0",
            "X-AuthSDK-Version": "3.103.0",
            "Accept-Language": "en-ID",
            "X-User-Locale": "en_ID",
            "X-DeviceCheckToken": "LITMUS_DISABLED",
            "Gojek-Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "gzip",
        }
        if extra:
            h.update(extra)
        return h

    def _signup_post(self, url: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        headers = self._signup_headers(extra_headers)
        body_str = json.dumps(body)
        resp = self._session.post(url, headers=headers, data=body_str, timeout_seconds=15)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def signup_request_otp(self, phone: str) -> dict:
        """Full SSO signup OTP flow: login/methods → cvs/v1/methods → cvs/v1/initiate.

        HAR-verified flow (2026-05-15):
          1. login/methods → 401 user:not_found (expected for new number)
          2. cvs/v1/methods (flow="signup_na") → verification_id + methods
          3. cvs/v1/initiate (flow="signup_na") → otp_token
        """
        local = phone.lstrip("+")
        if local.startswith("62"):
            local = local[2:]

        self.auth.transaction_id = str(uuid.uuid4())

        # Step 1: cvs/v1/methods to get verification_id
        methods_body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "country_code": "+62",
            "flow": "signup_na",
            "phone_number": local,
        }
        methods_result = self._sso_post("/cvs/v1/methods", methods_body)
        if methods_result["status"] not in (200, 201):
            return methods_result
        data = methods_result["body"].get("data", methods_result["body"])
        self.auth.verification_id = data.get("verification_id", "")
        self.auth.methods = data.get("methods", [])
        log.info("CVS methods: %s, vid=%s", self.auth.methods, self.auth.verification_id)

        # Step 2: cvs/v1/initiate to send OTP
        initiate_body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "country_code": "+62",
            "flow": "signup_na",
            "phone_number": local,
            "verification_id": self.auth.verification_id,
            "verification_method": "otp_sms",
        }
        result = self._sso_post("/cvs/v1/initiate", initiate_body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.otp_token = inner.get("otp_token", "")
            self.auth.otp_length = inner.get("otp_length", 4)
            log.info("Signup OTP sent: otp_length=%d, otp_token=%s...",
                     self.auth.otp_length, self.auth.otp_token[:20])
        return result

    def signup_verify_otp(self, otp: str, phone: str = "") -> dict:
        """POST /cvs/v1/verify (flow=signup_na) → returns JWE verification_token.

        HAR-verified: uses same flow/verification_method as initiate.
        """
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "otp": otp,
                "otp_token": self.auth.otp_token,
            },
            "flow": "signup_na",
            "verification_id": self.auth.verification_id,
            "verification_method": "otp_sms",
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            data = result["body"]
            inner = data.get("data", data)
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("Signup verified, token=%s...", self.auth.verification_token[:40])
        return result

    def signup_create_account(
        self,
        name: str,
        phone: str,
        email: str = "",
        country: str = "",
    ) -> dict:
        """POST /v7/customers/signup → create Gojek account.

        HAR-verified (2026-05-15): uses JWE from cvs/v1/verify as Verification-Token,
        Basic auth for gateway, WibbleDazzle signing (X-E1/E2/E3).
        """
        body = {
            "client_name": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "consent_given": True,
                "email": email,
                "name": name,
                "onboarding_partner": "android",
                "phone": phone,
                "signed_up_country": country or "ID",
            },
        }
        _GOJEK_API_KEY = "f3897109-8bcf-4658-a63d-10062562b581"
        client_auth = base64.b64encode(_GOJEK_API_KEY.encode()).decode()
        xm1 = self._build_xm1()
        body_str = json.dumps(body)
        ts = str(int(time.time() * 1000))
        sig = sign_v2(
            token="",
            timestamp_ms=ts,
            url="api.gojekapi.com/v7/customers/signup",
            method="POST",
            body=body_str,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )
        vtoken = self.auth.verification_token
        if vtoken.startswith("Bearer "):
            vtoken = vtoken[7:]
        headers = {
            "X-DeviceCheckToken": "LITMUS_DISABLED",
            "X-Signature": "1003",
            "X-Signature-Time": str(int(time.time())),
            "Verification-Token": f"Bearer {vtoken}",
            "Authorization": f"Basic {client_auth}",
            "X-Session-ID": self.session_id,
            "D1": self.d1,
            "User-Agent": f"Gojek/{self.version} ({self.appid}; build:5602; {self.os_info})",
            "X-AppVersion": self.version,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "Accept": "application/json",
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "X-PhoneMake": self.phone_make,
            "X-PhoneModel": self.model,
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-E3": sig["X-E3"],
            "AdjTs": "ts:A",
            "Content-Type": "application/json; charset=UTF-8",
            "Accept-Encoding": "gzip",
        }
        resp = self._session.post(
            f"{GOJEK_API_BASE}/v7/customers/signup",
            headers=headers,
            data=body_str,
            timeout_seconds=15,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        result = {"status": resp.status_code, "body": data}
        if result["status"] in (200, 201):
            data = result["body"].get("data", result["body"])
            self.auth.access_token = data.get("access_token", "")
            self.auth.refresh_token = data.get("refresh_token", "")
            uid = data.get("resource_owner_id", "")
            if uid:
                self.user_uuid = str(uid)
            log.info("Signup success: uid=%s, access_token=%s...", self.user_uuid, self.auth.access_token[:30])
        return result

    def signup_create_account_v2(
        self,
        name: str,
        phone: str,
        email: str = "",
        country: str = "",
    ) -> dict:
        """POST /v6/customers/register → create Gojek account (V2 / legacy).

        Uses PVToken header with JWT from /v6/customers/phone/verify.
        This is the correct endpoint for the newrequest→phone/verify flow.
        """
        body = {
            "client_name": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "name": name,
                "phone": phone,
                "email": email,
                "signed_up_country": country,
                "onboarding_partner": "android",
                "consent_given": True,
            },
        }
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))
        body_str = json.dumps(body)
        sig = sign_v2(
            token="",
            timestamp_ms=ts,
            url=f"api.gojekapi.com/v6/customers/register",
            method="POST",
            body=body_str,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )
        headers = {
            "X-DeviceCheckToken": "LITMUS_DISABLED",
            "X-Signature": "1003",
            "X-Signature-Time": str(int(time.time())),
            "PVToken": self.auth.verification_token,
            "D1": self.d1,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "User-Agent": f"Gojek/{self.version} ({self.appid}; build:5602; {self.os_info})",
            "X-Session-ID": self.session_id,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "X-PhoneMake": self.phone_make,
            "X-PhoneModel": self.model,
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-E3": sig["X-E3"],
            "AdjTs": "ts:A",
            "X-CVSDK-Version": "3.73.0",
            "X-AuthSDK-Version": "3.103.0",
            "Accept-Language": "en-ID",
            "Gojek-Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "gzip",
        }
        resp = self._session.post(
            f"{GOJEK_API_BASE}/v6/customers/register",
            headers=headers,
            data=body_str,
            timeout_seconds=15,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        result = {"status": resp.status_code, "body": data}
        if result["status"] in (200, 201):
            data = result["body"].get("data", result["body"])
            self.auth.access_token = data.get("access_token", "")
            self.auth.refresh_token = data.get("refresh_token", "")
            log.info("Signup V2 success: access_token=%s...", self.auth.access_token[:30])
        return result

    # ========================================================================
    # Phase 0b: GoPay Initialization (HAR-verified 2026-05-15)
    #
    # After signup + refresh_token:
    #   1. PUT customers/v1/country-change (empty body) → triggers GoPay wallet creation
    #   2. GET v2/payment-options/profiles → verify wallet exists
    #   3. GET v1/users/profile → check is_pin_setup
    #
    # NOTE: GoPay wallet is auto-created after country-change, no explicit register needed.
    # The old gopay_register endpoint may still work but is not used by the app.
    # ========================================================================

    def gopay_init(self) -> dict:
        """PUT /customers/v1/country-change → initialize GoPay wallet.

        HAR-verified: PUT with empty body, triggers wallet auto-creation.
        Must be called AFTER refresh_token (needs JWE access_token, not RS256).
        """
        return self._gopay_request("PUT", "/customers/v1/country-change")

    def gopay_get_profiles(self) -> dict:
        """GET /v2/payment-options/profiles → check GoPay wallet status."""
        return self._gopay_request("GET", "/v2/payment-options/profiles")

    def gopay_get_balances(self) -> dict:
        """GET /v1/payment-options/balances → get wallet balances."""
        return self._gopay_request("GET", "/v1/payment-options/balances")

    # ========================================================================
    # Phase 1: Login / Registration (SSO — accounts.goto-products.com)
    #   HAR-verified 2026-05-16 (ProxyPin5-16_13_05_54.har)
    #
    #   Login flow (existing user with PIN):
    #     1. login/methods → methods=[goto_pin, otp_wa, otp_sms], verification_id
    #     2. cvs/v1/initiate (flow=login_1fa, method=goto_pin) → challenge_id
    #     3. pin/tokens/nb (challenge_id, client_id, pin) → pin_token JWT
    #     4. cvs/v1/verify (data={challenge_id, validation_jwt=pin_token}) → JWE
    #     5. accountlist → account_id, 1fa_token
    #     6. goto-auth/token (grant_type=cvs, token=1fa_token) → 403 + 2fa_token
    #     7. cvs/v1/initiate (flow=login_2fa, method=otp_sms) → otp_token
    #     8. cvs/v1/verify (flow=login_2fa, otp) → JWE
    #     9. goto-auth/token (grant_type=challenge, token=2fa_token) → access_token!
    # ========================================================================

    LOGIN_PIN_CLIENT_ID = "6d11d261d7ae462dbd4be0dc5f36a697-MFAGOJEK"

    def get_login_methods(self, country_code: str, phone: str) -> dict:
        """Step 1: POST /goto-auth/login/methods → available auth methods."""
        self.auth.transaction_id = str(uuid.uuid4())
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "country_code": country_code,
            "device_verification_token_id": "",
            "email": "",
            "phone_number": phone,
        }
        result = self._sso_post("/goto-auth/login/methods", body)
        if result["status"] in (200, 201):
            data = result["body"].get("data", result["body"])
            self.auth.verification_id = data.get("verification_id", "")
            self.auth.methods = data.get("methods", [])
            log.info("Login methods: %s, vid=%s", self.auth.methods, self.auth.verification_id)
        return result

    def initiate_otp(
        self,
        country_code: str = "",
        phone: str = "",
        method: str = "",
        flow: str = "login_1fa",
        is_multiple_method: bool = True,
    ) -> dict:
        """POST /cvs/v1/initiate → trigger verification (PIN, OTP SMS, OTP WA).

        HAR-verified: body includes is_multiple_method for login flows.
        For goto_pin: returns challenge_id (not otp_token).
        For otp_sms/otp_wa: returns otp_token.
        """
        if not method:
            method = self.auth.methods[0] if self.auth.methods else "otp_sms"
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": flow,
            "verification_id": self.auth.verification_id,
            "verification_method": method,
        }
        if country_code:
            body["country_code"] = country_code
        if phone:
            body["phone_number"] = phone
        if is_multiple_method:
            body["is_multiple_method"] = True
        extra = {}
        if self.auth.access_token:
            tok = self.auth.access_token
            if not tok.startswith("Bearer "):
                tok = f"Bearer {tok}"
            extra["Authorization"] = tok
        result = self._sso_post("/cvs/v1/initiate", body, extra)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.otp_token = inner.get("otp_token", "")
            self.auth.otp_length = inner.get("otp_length", 4)
            self.auth.pin_challenge_id = inner.get("challenge_id", "")
            log.info("CVS initiate: otp_token=%s challenge_id=%s",
                     self.auth.otp_token[:20] if self.auth.otp_token else "(none)",
                     self.auth.pin_challenge_id or "(none)")
        return result

    def login_pin_verify(self, pin: str) -> dict:
        """POST /api/v1/users/pin/tokens/nb → verify PIN for login.

        HAR-verified: uses challenge_id from initiate(goto_pin), returns pin_token JWT.
        """
        body = {
            "challenge_id": self.auth.pin_challenge_id,
            "client_id": self.LOGIN_PIN_CLIENT_ID,
            "pin": pin,
        }
        result = self._gopay_post("/api/v1/users/pin/tokens/nb", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
            log.info("Login PIN verified, token=%s...", self.auth.pin_token[:40] if self.auth.pin_token else "(empty)")
        return result

    def verify_pin_via_cvs(self) -> dict:
        """POST /cvs/v1/verify with PIN token → JWE verification_token.

        HAR-verified: data={challenge_id, validation_jwt=pin_token}, flow=login_1fa.
        """
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "challenge_id": self.auth.pin_challenge_id,
                "validation_jwt": self.auth.pin_token,
            },
            "flow": "login_1fa",
            "verification_id": self.auth.verification_id,
            "verification_method": "goto_pin",
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("PIN CVS verified, token=%s...", self.auth.verification_token[:30])
        return result

    def verify_otp(self, otp: str, flow: str = "login_2fa") -> dict:
        """POST /cvs/v1/verify → submit OTP code.

        Returns verification_token (JWE).
        """
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "otp": otp,
                "otp_token": self.auth.otp_token,
            },
            "flow": flow,
            "verification_id": self.auth.verification_id,
            "verification_method": self.auth.otp_channel or "otp_sms",
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("OTP verified, token=%s...", self.auth.verification_token[:30])
        return result

    def retry_otp(self, flow: str = "login_2fa") -> dict:
        """POST /cvs/v2/retry → resend OTP."""
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {"otp_token": self.auth.otp_token},
            "flow": flow,
            "verification_method": "OTP",
            "verification_id": self.auth.verification_id,
        }
        return self._sso_post("/cvs/v2/retry", body)

    def check_otp_status(self) -> dict:
        """POST /cvs/v1/fallback-status → poll OTP delivery status."""
        body = {
            "otp_token": self.auth.otp_token,
            "verification_id": self.auth.verification_id,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        return self._sso_post("/cvs/v1/fallback-status", body)

    def get_account_list(self) -> dict:
        """POST /goto-auth/accountlist → account list + 1FA token."""
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        extra = {"Verification-Token": self.auth.verification_token}
        result = self._sso_post("/goto-auth/accountlist", body, extra)
        if result["status"] in (200, 201):
            data = result["body"].get("data", result["body"])
            self.auth.onefa_token = data.get("1fa_token", "")
            accounts = data.get("account_list", [])
            if accounts:
                self.auth.account_id = str(accounts[0].get("account_id", ""))
            log.info("Account list: %d accounts, account_id=%s", len(accounts), self.auth.account_id)
        return result

    def issue_token(self, grant_type: str = "cvs", token_value: str = "") -> dict:
        """POST /goto-auth/token → access_token + refresh_token.

        HAR-verified grant_type flow:
          1FA: grant_type="cvs", token=1fa_token → 403 (needs 2FA) → returns 2fa_token
          2FA: grant_type="challenge", token=2fa_token → 201 → access_token!
        """
        if not token_value:
            token_value = self.auth.onefa_token or self.auth.verification_token

        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": grant_type,
            "token": token_value,
            "account_id": self.auth.account_id,
            "scopes": [],
        }

        extra = {}
        if self.auth.onefa_token:
            extra["Verification-Token"] = self.auth.onefa_token
        elif self.auth.verification_token:
            extra["Verification-Token"] = self.auth.verification_token
        if self.auth.access_token:
            tok = self.auth.access_token
            if not tok.startswith("Bearer "):
                tok = f"Bearer {tok}"
            extra["Authorization"] = tok

        result = self._sso_post("/goto-auth/token", body, extra)
        status = result["status"]
        data = result["body"]

        if status in (200, 201):
            inner = data.get("data", data)
            self.auth.access_token = inner.get("access_token", "")
            self.auth.refresh_token = inner.get("refresh_token", "")
            log.info(
                "Token issued: access=%s..., refresh=%s...",
                self.auth.access_token[:30],
                self.auth.refresh_token[:30] if self.auth.refresh_token else "(none)",
            )
        elif status == 403:
            inner = data.get("data", data) if isinstance(data, dict) else {}
            self.auth.twofa_token = inner.get("2fa_token", "")
            self.auth.twofa_methods = inner.get("methods", [])
            vid = inner.get("verification_id", "")
            if vid:
                self.auth.verification_id = vid
            log.info("2FA required: methods=%s, 2fa_token=%s...",
                     self.auth.twofa_methods,
                     self.auth.twofa_token[:30] if self.auth.twofa_token else "(none)")
        else:
            log.warning("Token issue failed: %d %s", status, data)
        return result

    def login(self, country_code: str, phone: str, pin: str, otp_callback=None) -> dict:
        """Complete login flow: PIN (1FA) → OTP (2FA) → access_token.

        HAR-verified flow (2026-05-16):
          1. login/methods → goto_pin + otp_sms
          2. cvs/initiate(login_1fa, goto_pin) → challenge_id
          3. pin/tokens/nb → pin_token
          4. cvs/verify(login_1fa, pin_token) → JWE
          5. accountlist → 1fa_token
          6. goto-auth/token(cvs) → 403 + 2fa_token
          7. cvs/initiate(login_2fa, otp_sms) → otp_token
          8. [wait for OTP via otp_callback]
          9. cvs/verify(login_2fa, otp) → JWE
         10. goto-auth/token(challenge) → access_token!

        Args:
            otp_callback: function() -> str that returns OTP code (blocking wait)
        """
        # Step 1: login/methods
        methods = self.get_login_methods(country_code, phone)
        if methods["status"] not in (200, 201):
            return methods

        # Step 2: initiate PIN (1FA)
        has_pin = "goto_pin" in self.auth.methods
        if has_pin:
            init1 = self.initiate_otp(country_code, phone, method="goto_pin", flow="login_1fa")
            if init1["status"] not in (200, 201):
                return init1

            # Step 3: verify PIN
            pin_result = self.login_pin_verify(pin)
            if pin_result["status"] not in (200, 201):
                return pin_result

            # Step 4: CVS verify with PIN token
            cvs_pin = self.verify_pin_via_cvs()
            if cvs_pin["status"] not in (200, 201):
                return cvs_pin
        else:
            # No PIN, use OTP directly for 1FA
            init1 = self.initiate_otp(country_code, phone, method="otp_sms", flow="login_1fa")
            if init1["status"] not in (200, 201):
                return init1
            if otp_callback:
                otp = otp_callback()
                if not otp:
                    return {"status": 0, "body": {"error": "OTP not received"}}
                verify1 = self.verify_otp(otp, flow="login_1fa")
                if verify1["status"] not in (200, 201):
                    return verify1

        # Step 5: accountlist
        acct = self.get_account_list()
        if acct["status"] not in (200, 201):
            return acct

        # Step 6: issue token (1FA) → likely 403 needing 2FA
        token1 = self.issue_token(grant_type="cvs", token_value=self.auth.onefa_token)
        if token1["status"] in (200, 201):
            return token1  # Done! (no 2FA needed)

        if token1["status"] != 403 or not self.auth.twofa_token:
            return token1  # Unexpected error

        # Step 7: initiate OTP for 2FA
        self.auth.otp_channel = "otp_sms"
        init2 = self.initiate_otp(country_code, phone, method="otp_sms", flow="login_2fa")
        if init2["status"] not in (200, 201):
            return init2

        # Step 8: wait for OTP
        if not otp_callback:
            return {"status": 0, "body": {"error": "2FA OTP required but no callback", "otp_token": self.auth.otp_token}}
        otp = otp_callback()
        if not otp:
            return {"status": 0, "body": {"error": "2FA OTP not received"}}

        # Step 9: verify 2FA OTP
        verify2 = self.verify_otp(otp, flow="login_2fa")
        if verify2["status"] not in (200, 201):
            return verify2

        # Step 10: issue token with 2fa_token
        return self.issue_token(grant_type="challenge", token_value=self.auth.twofa_token)

    def refresh_token(self) -> dict:
        """Refresh access_token using refresh_token."""
        return self.issue_token(
            grant_type="refresh_token",
            token_value=self.auth.refresh_token,
        )

    def logout(self) -> dict:
        """DELETE /goto-auth/token → revoke tokens."""
        tok = self.auth.access_token
        if tok and not tok.startswith("Bearer "):
            tok = f"Bearer {tok}"
        headers = self._sso_headers({"Authorization": tok})
        resp = self._session.delete(
            f"{SSO_BASE}/goto-auth/token", headers=headers, timeout_seconds=15
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    # ========================================================================
    # Convenience: full login/register flow
    # ========================================================================

    def login_or_register(self, country_code: str, phone: str) -> dict:
        """Run steps 1-2: get methods + initiate OTP.

        After this call, wait for SMS and call verify_otp(otp).
        Returns the initiate_otp result.
        """
        methods_result = self.get_login_methods(country_code, phone)
        if methods_result["status"] != 200:
            return methods_result
        return self.initiate_otp(country_code, phone)

    def complete_login(self, otp: str) -> dict:
        """Run steps 3-5: verify OTP → account list → issue token.

        Returns the issue_token result. After success, self.auth.access_token is set.
        """
        verify_result = self.verify_otp(otp)
        if verify_result["status"] != 200:
            return verify_result

        acct_result = self.get_account_list()
        if acct_result["status"] != 200:
            return acct_result

        return self.issue_token()

    # ========================================================================
    # Phase 2: GoPay PIN Setup (HAR-verified 2026-05-15)
    #
    # Real flow:
    #   1. pins/allowed (check PIN validity)
    #   2. cvs/v1/methods (flow="goto_pin_wa_sms") → verification_id
    #   3. cvs/v1/initiate (flow="goto_pin_wa_sms", otp_sms) → otp_token
    #   4. cvs/v1/verify (flow="goto_pin_wa_sms") → JWE verification_token
    #   5. api/v2/users/pins/setup/tokens (pin + Verification-Token: JWE) → done
    # ========================================================================

    PIN_CLIENT_ID = "6fbe879a-e328-4428-84e2-d328b7488de6"

    def pin_check_allowed(self, pin: str) -> dict:
        """POST /api/v1/users/pins/allowed → check if PIN is valid/allowed."""
        return self._gopay_post("/api/v1/users/pins/allowed", {"pin": pin})

    def pin_request_otp(self) -> dict:
        """CVS flow for PIN setup: methods → initiate → returns otp_token.

        Uses flow="goto_pin_wa_sms". Requires valid access_token.
        """
        self.auth.transaction_id = str(uuid.uuid4())

        methods_body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": "goto_pin_wa_sms",
        }
        methods_result = self._sso_post("/cvs/v1/methods", methods_body)
        if methods_result["status"] not in (200, 201):
            return methods_result
        data = methods_result["body"].get("data", methods_result["body"])
        self.auth.verification_id = data.get("verification_id", "")
        self.auth.methods = data.get("methods", [])
        log.info("PIN CVS methods: %s, vid=%s", self.auth.methods, self.auth.verification_id)

        initiate_body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": "goto_pin_wa_sms",
            "verification_id": self.auth.verification_id,
            "verification_method": "otp_sms",
        }
        result = self._sso_post("/cvs/v1/initiate", initiate_body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.otp_token = inner.get("otp_token", "")
            self.auth.otp_length = inner.get("otp_length", 4)
            log.info("PIN OTP sent: otp_token=%s...", self.auth.otp_token[:20])
        return result

    def pin_verify_otp(self, otp: str) -> dict:
        """POST /cvs/v1/verify (flow=goto_pin_wa_sms) → JWE for PIN setup."""
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "otp": otp,
                "otp_token": self.auth.otp_token,
            },
            "flow": "goto_pin_wa_sms",
            "verification_id": self.auth.verification_id,
            "verification_method": "otp_sms",
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("PIN OTP verified, token=%s...", self.auth.verification_token[:40])
        return result

    def pin_setup(self, pin: str) -> dict:
        """POST /api/v2/users/pins/setup/tokens → set PIN.

        HAR-verified: uses Verification-Token (JWE from pin_verify_otp),
        body has pin + empty challenge_id + fixed client_id.
        """
        body = {
            "challenge_id": "",
            "client_id": self.PIN_CLIENT_ID,
            "pin": pin,
        }
        vtoken = self.auth.verification_token
        if not vtoken.startswith("Bearer "):
            vtoken = f"Bearer {vtoken}"
        extra = {
            "Verification-Token": vtoken,
            "is-token-required": "false",
        }
        result = self._gopay_post("/api/v2/users/pins/setup/tokens", body, extra)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
            log.info("PIN setup success")
        return result

    def setup_gopay_pin(self, pin: str, otp: str) -> dict:
        """Full PIN setup: check → CVS OTP verify → set PIN.

        Args:
            pin: 6-digit PIN
            otp: OTP received via SMS
        """
        allowed = self.pin_check_allowed(pin)
        if allowed["status"] not in (200, 201):
            return allowed

        verify = self.pin_verify_otp(otp)
        if verify["status"] not in (200, 201):
            return verify

        return self.pin_setup(pin)

    def get_user_profile(self) -> dict:
        """GET /v1/users/profile → check GoPay profile (is_pin_setup etc)."""
        return self._gopay_request("GET", "/v1/users/profile")

    def pin_post_registration_hook(self, payment_method: str = "GOPAY_WALLET") -> dict:
        """Step 9: POST /v1/customer/payment-options/post-registration-hook → activate GoPay."""
        body = {"payment_method": payment_method, "data": {}}
        return self._gopay_post("/v1/customer/payment-options/post-registration-hook", body)



    # ========================================================================
    # Phase 3: PIN Operations (post-activation)
    # ========================================================================

    def pin_create_challenge(self, flow: str = "SET_PIN") -> dict:
        """POST /api/v1/users/pin/challenges → create challenge for PIN verification.

        Returns challenge_id and client_id needed for pin_verify.
        """
        body = {"flow": flow}
        result = self._gopay_post("/api/v1/users/pin/challenges", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_challenge_id = inner.get("challenge_id", "")
            self.auth.pin_client_id = inner.get("client_id", "")
            log.info("PIN challenge: id=%s, client=%s", self.auth.pin_challenge_id, self.auth.pin_client_id)
        return result

    def pin_verify(self, pin: str, challenge_id: str = "", client_id: str = "") -> dict:
        """POST /api/v1/users/pin/tokens → verify PIN for transaction.

        Returns a pin_token used to authorize the transaction.
        """
        body = {
            "client_id": client_id or self.auth.pin_client_id,
            "pin": pin,
            "challenge_id": challenge_id or self.auth.pin_challenge_id,
        }
        result = self._gopay_post("/api/v1/users/pin/tokens", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
            log.info("PIN verified, pin_token=%s...", self.auth.pin_token[:30] if self.auth.pin_token else "(empty)")
        return result

    # ========================================================================
    # Phase 4: GoPay Envelope (Red Packet)
    # ========================================================================

    def envelope_get_details(self, link_id: str) -> dict:
        """GET /v1/festivals/envelope-requests/{link_id} → red packet details."""
        return self._gopay_request("GET", f"/v1/festivals/envelope-requests/{link_id}")

    def envelope_claim_by_link(self, link_id: str) -> dict:
        """POST /v1/festivals/link → claim red packet that has link_id.

        For envelopes whose deeplink contains link_id (older format).
        Body: {"link_id": "<link_id>"}
        Response 422 GoPay-36006 = expired/claimed.
        """
        return self._gopay_post("/v1/festivals/link", {"link_id": link_id})

    def envelope_claim(self, deeplink_id: str) -> dict:
        """Claim a red packet (envelope) - full flow.

        Captured via VM memory scan (2026-05-16):
          Step 1: GET /v1/festivals/envelope-requests/{deeplink_id}
                  → returns envelope details + generated envelope_request_id
          Step 2: POST /v1/festivals/envelope-requests
                  Body: {"envelope_request_id": "<from_step1>"}
                  → {"data":{"envelope_request_id":"..."},"success":true}

        No PIN required, no consent required.
        """
        import time
        r1 = self._gopay_get(f"/v1/festivals/envelope-requests/{deeplink_id}")
        if r1["status"] != 200:
            return r1
        eid = r1["body"]["data"]["envelope_request_id"]
        time.sleep(1)
        return self._gopay_post("/v1/festivals/envelope-requests", {"envelope_request_id": eid})

    def pin_validate(self, pin: str) -> dict:
        """POST /v1/users/pin/validate → legacy PIN validation."""
        return self._gopay_post("/v1/users/pin/validate", {"pin": pin})

    def pin_reset_v3(self, new_pin: str, otp: str) -> dict:
        """POST /api/v3/users/pins/reset/tokens → reset forgotten PIN."""
        body = {
            "client_id": None,
            "pin": new_pin,
            "challenge_id": self.auth.pin_challenge_id,
            "otp_token": self.auth.pin_otp_auth_token,
            "otp": otp,
        }
        result = self._gopay_post("/api/v3/users/pins/reset/tokens", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
        return result

    def pin_update_v3(self, new_pin: str, pin_token: str = "") -> dict:
        """PUT /v3/users/pin/update → change PIN (knows old PIN)."""
        body = {
            "new_pin": new_pin,
            "pin_token": pin_token or self.auth.pin_token,
        }
        return self._gopay_put("/v3/users/pin/update", body)

    def pin_check_allowed(self, flow: str = "SET_PIN") -> dict:
        """POST /api/v1/users/pins/allowed → check if PIN operation is allowed."""
        return self._gopay_post("/api/v1/users/pins/allowed", {"flow": flow})

    # ========================================================================
    # Phase 4: Wallet Operations
    # ========================================================================

    def get_profile(self) -> dict:
        """GET /v1/users/profile → user profile."""
        return self._gopay_get("/v1/users/profile")

    def get_balance(self) -> dict:
        """GET /v1/payment-options/balances → wallet balance."""
        return self._gopay_get("/v1/payment-options/balances")

    def get_payment_profiles(self) -> dict:
        """GET /v2/payment-options/profiles → payment method profiles."""
        return self._gopay_get("/v2/payment-options/profiles")

    def get_linked_apps(self) -> dict:
        """GET /v1/linkedapps → auto-debit mandates."""
        return self._gopay_get("/v1/linkedapps")

    def unlink_app(self, link_id: str) -> dict:
        """PATCH /v1/links?link_id=<id> → cancel auto-debit mandate."""
        return self._gopay_patch(f"/v1/links?link_id={link_id}", {})

    def get_payment_options(self) -> dict:
        """GET /v1/customer/payment-options/settings/list → payment settings."""
        return self._gopay_get("/v1/customer/payment-options/settings/list")

    def refresh_payment_options(self) -> dict:
        """PUT /v1/customer/payment-options/refresh → refresh payment options."""
        return self._gopay_put("/v1/customer/payment-options/refresh", {})

    def get_ewallet_consent(self) -> dict:
        """GET /v1/customers/consents/e-wallet → consent status."""
        return self._gopay_get("/v1/customers/consents/e-wallet")

    def set_ewallet_consent(self, consent: bool = True) -> dict:
        """PUT /v1/customers/consents/e-wallet → set consent."""
        return self._gopay_put("/v1/customers/consents/e-wallet", {"consent": consent})


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Gojek/GoPay protocol client")
    sub = parser.add_subparsers(dest="command")

    # --- login ---
    p_login = sub.add_parser("login", help="Start login/register flow")
    p_login.add_argument("--country-code", default="+62")
    p_login.add_argument("--phone", required=True)
    p_login.add_argument("--appinfo", default=r"C:\tools\gojek_capture\fresh_appinfo.txt")
    p_login.add_argument("--headers", default=r"C:\tools\gojek_capture\fresh_headers.txt")

    # --- verify ---
    p_verify = sub.add_parser("verify", help="Verify OTP and complete login")
    p_verify.add_argument("--otp", required=True)

    # --- pin ---
    p_pin = sub.add_parser("pin", help="Setup GoPay PIN")
    p_pin.add_argument("--pin", required=True)
    p_pin.add_argument("--otp", required=True, help="SMS OTP for PIN setup")

    # --- profile ---
    p_profile = sub.add_parser("profile", help="Get user profile")
    p_profile.add_argument("--appinfo", default=r"C:\tools\gojek_capture\fresh_appinfo.txt")
    p_profile.add_argument("--headers", default=r"C:\tools\gojek_capture\fresh_headers.txt")

    # --- balance ---
    p_balance = sub.add_parser("balance", help="Get wallet balance")
    p_balance.add_argument("--appinfo", default=r"C:\tools\gojek_capture\fresh_appinfo.txt")
    p_balance.add_argument("--headers", default=r"C:\tools\gojek_capture\fresh_headers.txt")

    args = parser.parse_args()

    if args.command == "login":
        client = GojekClient.from_device_info(args.appinfo, args.headers)
        result = client.login_or_register(args.country_code, args.phone)
        print(json.dumps(result, indent=2))
        print(f"\nOTP sent via {client.auth.otp_channel}. Length: {client.auth.otp_length}")
        print("Next: run 'verify --otp <code>' to complete login")

    elif args.command == "profile":
        client = GojekClient.from_device_info(args.appinfo, args.headers)
        result = client.get_profile()
        print(json.dumps(result, indent=2))

    elif args.command == "balance":
        client = GojekClient.from_device_info(args.appinfo, args.headers)
        result = client.get_balance()
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()
