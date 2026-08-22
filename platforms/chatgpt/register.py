"""

注册流程引擎

从 main.py 中提取并重构的注册流程

"""



import re

import json

import time

import uuid

import base64

import random

import logging
import hashlib

import secrets

import string
import urllib.parse

from typing import Optional, Dict, Any, Tuple, Callable

from dataclasses import dataclass

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo



from curl_cffi import requests as cffi_requests



from .oauth import OAuthManager, OAuthStart, generate_oauth_url, submit_callback_url

from .http_client import OpenAIHTTPClient, HTTPClientError, RequestConfig

# from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType  # removed: external dep

# from ..database import crud  # removed: external dep

# from ..database.session import get_db  # removed: external dep

from .constants import (

    OPENAI_API_ENDPOINTS,

    OPENAI_PAGE_TYPES,

    generate_random_user_info,

    OTP_CODE_PATTERN,

    DEFAULT_PASSWORD_LENGTH,

    PASSWORD_CHARSET,

    AccountStatus,

    TaskStatus,

    get_latest_sentinel_sdk_url,
    get_latest_sentinel_frame_url,

    OAUTH_REDIRECT_URI,

    OAUTH_CLIENT_ID,

)

# from ..config.settings import get_settings  # removed: external dep





CHATGPT_EMAIL_OTP_DEFAULT_TIMEOUT_SECONDS = 30
CHATGPT_EMAIL_OTP_MIN_TIMEOUT_SECONDS = 10
LATEST_CHATGPT_FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)
# Headed Camoufox HAR 2026-07-31 register + post-register security capture.
LATEST_CHATGPT_OAI_CLIENT_VERSION = "prod-fc98dd36cc7acf295fb888b3b2c9e7c00ad14591"
LATEST_CHATGPT_OAI_CLIENT_BUILD_NUMBER = "8823441"
LATEST_CHATGPT_SENTINEL_SCREEN = 2500
LATEST_CHATGPT_SENTINEL_CORES = 8
LATEST_CHATGPT_ACCEPT_LANGUAGE = "ja-JP,ja;q=0.5"
LATEST_CHATGPT_LANGUAGE = "ja-JP"
LATEST_CHATGPT_TIMEZONE = "Asia/Tokyo"
LATEST_CHATGPT_TIMEZONE_OFFSET_MIN = -540
LATEST_CHATGPT_CF_JSD_SCRIPT_URL = (
    "https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload"
)
LATEST_CHATGPT_CHAT_REQUIREMENTS_SCRIPT_URL = "https://accounts.google.com/gsi/client"
LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL = "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
LATEST_CHATGPT_ADD_PASSWORD_PAGE_URL = "https://auth.openai.com/reset-password/new-password"
LATEST_CHATGPT_ADD_PASSWORD_API_URL = "https://auth.openai.com/api/accounts/password/add"


@dataclass(frozen=True)
class ChatGPTProtocolProfile:
    country_code: str
    accept_language: str
    language: str
    timezone_name: str
    timezone_offset_min: int
    pricing_country: str
    timezone_label: str


_CHATGPT_PROTOCOL_PROFILE_SPECS: dict[str, tuple[str, str, str, int, str]] = {
    "JP": ("ja-JP,ja;q=0.5", "ja-JP", "Asia/Tokyo", -540, "日本標準時"),
    "GB": ("en-GB,en;q=0.5", "en-GB", "Europe/London", 0, "Greenwich Mean Time"),
    "US": ("en-US,en;q=0.5", "en-US", "America/New_York", 300, "Eastern Standard Time"),
    "CA": ("en-CA,en;q=0.5", "en-CA", "America/Toronto", 300, "Eastern Standard Time"),
    "AU": ("en-AU,en;q=0.5", "en-AU", "Australia/Sydney", -600, "Australian Eastern Standard Time"),
    "SG": ("en-SG,en;q=0.5", "en-SG", "Asia/Singapore", -480, "Singapore Standard Time"),
    "HK": ("zh-HK,zh;q=0.5", "zh-HK", "Asia/Hong_Kong", -480, "Hong Kong Standard Time"),
    "TW": ("zh-TW,zh;q=0.5", "zh-TW", "Asia/Taipei", -480, "Taipei Standard Time"),
    "KR": ("ko-KR,ko;q=0.5", "ko-KR", "Asia/Seoul", -540, "Korean Standard Time"),
    "TH": ("th-TH,th;q=0.5", "th-TH", "Asia/Bangkok", -420, "Indochina Time"),
    "VN": ("vi-VN,vi;q=0.5", "vi-VN", "Asia/Ho_Chi_Minh", -420, "Indochina Time"),
    "PH": ("en-PH,en;q=0.5", "en-PH", "Asia/Manila", -480, "Philippine Standard Time"),
    "ID": ("id-ID,id;q=0.5", "id-ID", "Asia/Jakarta", -420, "Western Indonesia Time"),
    "MY": ("en-MY,en;q=0.5", "en-MY", "Asia/Kuala_Lumpur", -480, "Malaysia Time"),
    "TR": ("tr-TR,tr;q=0.5", "tr-TR", "Europe/Istanbul", -180, "Turkey Time"),
    "BR": ("pt-BR,pt;q=0.5", "pt-BR", "America/Sao_Paulo", 180, "Brasilia Standard Time"),
    "MX": ("es-MX,es;q=0.5", "es-MX", "America/Mexico_City", 360, "Central Standard Time"),
    "IN": ("en-IN,en;q=0.5", "en-IN", "Asia/Kolkata", -330, "India Standard Time"),
}


def _last_sunday_utc(year: int, month: int, hour: int) -> datetime:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    day = next_month - timedelta(days=1)
    day -= timedelta(days=(day.weekday() - 6) % 7)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


def _nth_sunday_utc(year: int, month: int, nth: int, hour: int) -> datetime:
    day = datetime(year, month, 1, tzinfo=timezone.utc)
    day += timedelta(days=(6 - day.weekday()) % 7 + (nth - 1) * 7)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


def _fallback_timezone_offset_min(timezone_name: str, fallback: int) -> int:
    now_utc = datetime.now(timezone.utc)
    if timezone_name == "Europe/London":
        start = _last_sunday_utc(now_utc.year, 3, 1)
        end = _last_sunday_utc(now_utc.year, 10, 1)
        return -60 if start <= now_utc < end else 0
    if timezone_name in {"America/New_York", "America/Toronto"}:
        start = _nth_sunday_utc(now_utc.year, 3, 2, 7)
        end = _nth_sunday_utc(now_utc.year, 11, 1, 6)
        return 240 if start <= now_utc < end else 300
    return int(fallback)


def _safe_timezone_offset_min(timezone_name: str, fallback: int) -> int:
    try:
        offset = datetime.now(ZoneInfo(timezone_name)).utcoffset()
        if offset is not None:
            return -int(offset.total_seconds() // 60)
    except Exception:
        pass
    return _fallback_timezone_offset_min(timezone_name, fallback)


def _infer_chatgpt_proxy_region(proxy_url: str | None) -> str:
    text = str(proxy_url or "")
    for pattern in (
        r"(?:^|[^A-Za-z])area[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
        r"(?:^|[^A-Za-z])region[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
        r"(?:^|[^A-Za-z])country[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
        r"(?:^|[^A-Za-z])loc[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return ""


def build_chatgpt_protocol_profile(region_code: str = "", proxy_url: str | None = None) -> ChatGPTProtocolProfile:
    code = str(region_code or _infer_chatgpt_proxy_region(proxy_url) or "JP").strip().upper()
    spec = _CHATGPT_PROTOCOL_PROFILE_SPECS.get(code) or _CHATGPT_PROTOCOL_PROFILE_SPECS["JP"]
    accept_language, language, timezone_name, fallback_offset, timezone_label = spec
    offset = _safe_timezone_offset_min(timezone_name, fallback_offset)
    if timezone_name == "Europe/London" and offset == -60:
        timezone_label = "British Summer Time"
    elif timezone_name in {"America/New_York", "America/Toronto"} and offset == 240:
        timezone_label = "Eastern Daylight Time"
    return ChatGPTProtocolProfile(
        country_code=code if code in _CHATGPT_PROTOCOL_PROFILE_SPECS else "JP",
        accept_language=accept_language,
        language=language,
        timezone_name=timezone_name,
        timezone_offset_min=offset,
        pricing_country=code if code in _CHATGPT_PROTOCOL_PROFILE_SPECS else "JP",
        timezone_label=timezone_label,
    )


def _browser_date_for_profile(profile: ChatGPTProtocolProfile) -> str:
    local_tz = timezone(timedelta(minutes=-profile.timezone_offset_min))
    value = datetime.now(local_tz)
    total = -profile.timezone_offset_min
    sign = "+" if total >= 0 else "-"
    absolute = abs(total)
    return value.strftime("%a %b %d %Y %H:%M:%S ") + f"GMT{sign}{absolute // 60:02d}{absolute % 60:02d} ({profile.timezone_label})"


logger = logging.getLogger(__name__)


PLATFORM_AUTHORIZE_CLOUDFLARE_MANAGED_CHALLENGE = "platform_authorize_cloudflare_managed_challenge"


def is_cloudflare_managed_challenge_html(body: str) -> bool:
    text = str(body or "")
    if not text:
        return False
    lowered = text.lower()
    return (
        "just a moment" in lowered
        and "challenges.cloudflare.com" in lowered
        and "window._cf_chl_opt" in lowered
        and "ctype" in lowered
        and "managed" in lowered
    )


class CloudflareManagedChallengeError(RuntimeError):
    """auth.openai.com returned Cloudflare managed challenge before protocol auth."""

    def __init__(self, *, status: int | str = "unknown", final_url: str = "") -> None:
        message = (
            f"{PLATFORM_AUTHORIZE_CLOUDFLARE_MANAGED_CHALLENGE}: "
            "auth.openai.com 返回 Cloudflare Managed Challenge，协议请求无法直接调用 YesCaptcha；"
            "请切换低风险代理/IP，或改用可执行 JS 的浏览器/Camoufox/本地 solver 路径"
        )
        if status:
            message += f" status={status}"
        if final_url:
            message += f" final_url={final_url}"
        super().__init__(message)





@dataclass

class RegistrationResult:

    """注册结果"""

    success: bool

    email: str = ""

    password: str = ""  # 注册密码

    account_id: str = ""

    workspace_id: str = ""

    access_token: str = ""

    refresh_token: str = ""

    id_token: str = ""

    session_token: str = ""  # 会话令牌

    error_message: str = ""

    logs: list = None

    metadata: dict = None

    source: str = "register"  # 'register' 或 'login'，区分账号来源



    def to_dict(self) -> Dict[str, Any]:

        """转换为字典"""

        return {

            "success": self.success,

            "email": self.email,

            "password": self.password,

            "account_id": self.account_id,

            "workspace_id": self.workspace_id,

            "access_token": self.access_token[:20] + "..." if self.access_token else "",

            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",

            "id_token": self.id_token[:20] + "..." if self.id_token else "",

            "session_token": self.session_token[:20] + "..." if self.session_token else "",

            "error_message": self.error_message,

            "logs": self.logs or [],

            "metadata": self.metadata or {},

            "source": self.source,

        }





@dataclass

class SignupFormResult:

    """提交注册表单的结果"""

    success: bool

    page_type: str = ""  # 响应中的 page.type 字段

    is_existing_account: bool = False  # 是否为已注册账号

    response_data: Dict[str, Any] = None  # 完整的响应数据

    error_message: str = ""





@dataclass

class SentinelPayload:

    """Sentinel 请求结果。"""

    p: str

    c: str

    flow: str

    t: str = ""

    so_token: str = ""





@dataclass
class ProtocolFingerprint:
    """单个协议注册任务内稳定复用的浏览器指纹。"""

    device_id: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_full: str
    sec_ch_ua_platform: str = '"Windows"'
    sec_ch_ua_platform_version: str = '"10.0.0"'
    sec_ch_ua_arch: str = '"x86_64"'
    sec_ch_ua_bitness: str = '"64"'
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_model: str = '""'
    accept_language: str = "en-US,en;q=0.9"
    auth_session_logging_id: str = ""

    @classmethod
    def create(cls) -> "ProtocolFingerprint":
        chrome_versions = (
            "136.0.7103.114",
            "137.0.7151.120",
            "138.0.7204.101",
            "139.0.7258.128",
            "140.0.7339.80",
            "141.0.7390.78",
            "142.0.7444.60",
            "143.0.7499.40",
            "144.0.7540.32",
            "145.0.7588.24",
        )
        full_version = secrets.choice(chrome_versions)
        major = full_version.split(".", 1)[0]
        brand_version = str(major)
        grease_version = str(secrets.choice((8, 24, 99)))
        brand_orders = [
            [
                f'"Google Chrome";v="{brand_version}"',
                f'"Chromium";v="{brand_version}"',
                f'"Not:A-Brand";v="{grease_version}"',
            ],
            [
                f'"Chromium";v="{brand_version}"',
                f'"Not?A_Brand";v="{grease_version}"',
                f'"Google Chrome";v="{brand_version}"',
            ],
            [
                f'"Not)A;Brand";v="{grease_version}"',
                f'"Google Chrome";v="{brand_version}"',
                f'"Chromium";v="{brand_version}"',
            ],
        ]
        full_orders = [
            [
                f'"Google Chrome";v="{full_version}"',
                f'"Chromium";v="{full_version}"',
                f'"Not:A-Brand";v="{grease_version}.0.0.0"',
            ],
            [
                f'"Chromium";v="{full_version}"',
                f'"Not?A_Brand";v="{grease_version}.0.0.0"',
                f'"Google Chrome";v="{full_version}"',
            ],
            [
                f'"Not)A;Brand";v="{grease_version}.0.0.0"',
                f'"Google Chrome";v="{full_version}"',
                f'"Chromium";v="{full_version}"',
            ],
        ]
        order_index = secrets.randbelow(len(brand_orders))
        return cls(
            device_id=str(uuid.uuid4()),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{full_version} Safari/537.36"
            ),
            sec_ch_ua=", ".join(brand_orders[order_index]),
            sec_ch_ua_full=", ".join(full_orders[order_index]),
            auth_session_logging_id=str(uuid.uuid4()),
        )

    def apply_to_client(self, client: OpenAIHTTPClient) -> None:
        client.default_headers["User-Agent"] = self.user_agent
        client.default_headers["Accept-Language"] = self.accept_language


def _decode_jwt_payload_no_verify(token: str) -> dict:
    """不验签解 JWT payload，仅用于读取 ChatGPT Web session 中的账号标识。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_chatgpt_account_id(access_token: str) -> str:
    """从 ChatGPT Web accessToken 提取账号 ID，兼容无 _account cookie 的 free 注册。"""
    payload = _decode_jwt_payload_no_verify(access_token)
    auth_info = payload.get("https://api.openai.com/auth") or {}
    if isinstance(auth_info, dict):
        account_id = str(auth_info.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return str(payload.get("sub") or "").strip()


def _iter_cookie_records(cookies):
    if not cookies:
        return
    if isinstance(cookies, dict):
        for name, value in cookies.items():
            yield {
                "name": str(name or "").strip(),
                "value": str(value or ""),
                "domain": "",
                "path": "/",
            }
        return

    jar = getattr(cookies, "jar", None)
    yielded = False
    if jar is not None:
        try:
            for cookie in jar:
                name = str(getattr(cookie, "name", "") or "").strip()
                value = str(getattr(cookie, "value", "") or "")
                if name and value != "":
                    yielded = True
                    yield {
                        "name": name,
                        "value": value,
                        "domain": str(getattr(cookie, "domain", "") or ""),
                        "path": str(getattr(cookie, "path", "") or "/"),
                    }
        except Exception:
            yielded = False
        if yielded:
            return

    yielded = False
    try:
        for cookie in list(cookies or []):
            if isinstance(cookie, dict):
                name = str(cookie.get("name") or "").strip()
                value = str(cookie.get("value") or "")
                domain = str(cookie.get("domain") or "")
                path = str(cookie.get("path") or "/")
            else:
                name = str(getattr(cookie, "name", "") or "").strip()
                value = str(getattr(cookie, "value", "") or "")
                domain = str(getattr(cookie, "domain", "") or "")
                path = str(getattr(cookie, "path", "") or "/")
            if name and value != "":
                yielded = True
                yield {"name": name, "value": value, "domain": domain, "path": path}
    except Exception:
        yielded = False
    if yielded:
        return

    if hasattr(cookies, "items"):
        try:
            for name, value in cookies.items():
                name = str(name or "").strip()
                value = str(value or "")
                if name and value != "":
                    yielded = True
                    yield {"name": name, "value": value, "domain": "", "path": "/"}
        except Exception:
            yielded = False
        if yielded:
            return

    if hasattr(cookies, "get_dict"):
        try:
            for name, value in cookies.get_dict().items():
                name = str(name or "").strip()
                value = str(value or "")
                if name and value != "":
                    yield {"name": name, "value": value, "domain": "", "path": "/"}
        except Exception:
            return


def _cookie_value(cookies, name: str) -> str:
    target = str(name or "").strip()
    if not target:
        return ""
    value = ""
    for cookie in _iter_cookie_records(cookies) or []:
        if str(cookie.get("name") or "") == target:
            value = str(cookie.get("value") or "")
    return value


def _cookies_to_header(cookies) -> str:
    """将当前会话 cookie 转为 Cookie header，便于后续按 Chat2API 方式校验 session。"""
    parts: list[str] = []
    for cookie in _iter_cookie_records(cookies) or []:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name and value != "":
            parts.append(f"{name}={value}")
    return "; ".join(parts)


PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = "https://platform.openai.com/auth/callback"
PLATFORM_OAUTH_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_OAUTH_SCOPE = "openid profile email offline_access"
PLATFORM_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
PLATFORM_BASE = "https://platform.openai.com"
PLATFORM_REFERENCE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
PLATFORM_REFERENCE_SEC_CH_UA = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
PLATFORM_REFERENCE_SEC_CH_UA_FULL = (
    '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", '
    '"Google Chrome";v="145.0.0.0"'
)


def _b64url_no_pad(raw: bytes) -> str:
    """Base64 URL 编码去掉填充；Platform OAuth PKCE 使用。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _generate_pkce_pair() -> tuple[str, str]:
    """生成 Platform OAuth 所需 code_verifier/code_challenge。"""
    import hashlib

    code_verifier = _b64url_no_pad(secrets.token_bytes(64))
    code_challenge = _b64url_no_pad(hashlib.sha256(code_verifier.encode("ascii")).digest())
    return code_verifier, code_challenge


def _extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    """从 OAuth callback URL 中提取 code/state/scope。"""
    if not url:
        return None
    try:
        import urllib.parse

        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {
        "code": code,
        "state": str((params.get("state") or [""])[0]).strip(),
        "scope": str((params.get("scope") or [""])[0]).strip(),
    }


# ─── Sentinel helpers (ported from browser_register.py) ──────────



def _generate_datadog_trace_headers() -> dict:

    trace_hex = secrets.token_hex(8).rjust(16, "0")

    parent_hex = secrets.token_hex(8).rjust(16, "0")

    trace_id = str(int(trace_hex, 16))

    parent_id = str(int(parent_hex, 16))

    return {

        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",

        "tracestate": "dd=s:1;o:rum",

        "x-datadog-origin": "rum",

        "x-datadog-parent-id": parent_id,

        "x-datadog-sampling-priority": "1",

        "x-datadog-trace-id": trace_id,

    }





class _SentinelTokenGenerator:
    """Dynamic sentinel token generator aligned with headed Firefox HAR."""

    _MINUS = "\u2212"  # U+2212 minus used by real Sentinel SDK probe strings
    _UNSET = object()

    def __init__(
        self,
        device_id: str,
        user_agent: str,
        *,
        client_version: str = "",
        profile: Optional[ChatGPTProtocolProfile] = None,
    ):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent
        self.client_version = str(client_version or LATEST_CHATGPT_OAI_CLIENT_VERSION)
        self.profile = profile or build_chatgpt_protocol_profile()
        self.sid = str(uuid.uuid4())
        self._origin_ms = int(time.time() * 1000) - random.randint(8000, 40000)
        self._is_firefox = "Firefox/" in (user_agent or "")
        self._is_mac = "Macintosh" in (user_agent or "") or "Mac OS X" in (user_agent or "")

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _capability_probe(self, *, stage: str) -> str:
        minus = self._MINUS
        if stage == "chat_prepare":
            return f"requestMediaKeySystemAccess{minus}function requestMediaKeySystemAccess() {{ [native code] }}"
        if stage == "sentinel_req":
            if self._is_firefox:
                return (
                    f"mozGetUserMedia{minus}function mozGetUserMedia() {{\n"
                    f"    [native code]\n}}"
                )
            return f"webkitGetUserMedia{minus}function webkitGetUserMedia() {{ [native code] }}"
        # final enforcement token (create_account)
        if self._is_firefox:
            return f"plugins{minus}[object PluginArray]"
        return f"languages{minus}en-US,en"

    def _event_probe(self, *, stage: str) -> str:
        if stage == "chat_prepare":
            return "setInterval"
        if stage == "sentinel_req":
            return random.choice(["ondragstart", "onanimationstart", "ontransitionrun", "onlostpointercapture"])
        return random.choice(["matchMedia", "location", "onbeforetoggle", "onbeforeunload"])

    def _react_probe(self, *, stage: str) -> str:
        if stage == "chat_prepare":
            return f"__reactResources${secrets.token_hex(6)}"
        return f"_reactListening{secrets.token_hex(6)}"

    def _config(self, *, stage: str = "final") -> list:
        # Headed HAR shape:
        # [screen, date, null, nonce, ua, script_url, client_or_null, lang, langs, elapsed,
        #  capability, react, event, perf, sid, "", cores, origin_ms, zeros..., flags]
        now_ms = int(time.time() * 1000)
        if stage == "chat_prepare":
            perf_now = random.randint(3000, 12000)
            elapsed = random.randint(1, 8)
            script_url = LATEST_CHATGPT_CHAT_REQUIREMENTS_SCRIPT_URL
            client_or_null = self.client_version
        elif stage == "sentinel_req":
            perf_now = random.randint(15000, 35000)
            elapsed = random.randint(20, 90)
            script_url = get_latest_sentinel_sdk_url()  # versioned /sentinel/<ver>/sdk.js
            client_or_null = None
        else:
            perf_now = random.randint(20000, 45000)
            elapsed = random.randint(3, 20)
            script_url = LATEST_CHATGPT_SENTINEL_ENTRY_SDK_URL  # backend-api/sentinel/sdk.js
            client_or_null = None

        locale_date = _browser_date_for_profile(self.profile)
        screen = LATEST_CHATGPT_SENTINEL_SCREEN if self._is_mac or self._is_firefox else 4800
        cores = LATEST_CHATGPT_SENTINEL_CORES if self._is_mac or self._is_firefox else random.choice([8, 12, 16])
        return [
            screen,
            locale_date,
            None,
            random.random(),
            self.user_agent,
            script_url,
            client_or_null,
            self.profile.language,
            ",".join(part.split(";", 1)[0].strip() for part in self.profile.accept_language.split(",") if part.strip()),
            elapsed,
            self._capability_probe(stage=stage),
            self._react_probe(stage=stage),
            self._event_probe(stage=stage),
            int(perf_now),
            self.sid,
            "",
            cores,
            self._origin_ms,
            0,
            0,
            0,
            0,
            0,
            1,
            1,
        ]

    def generate_requirements_token(self) -> str:
        """Initial /sentinel/req requirements p from headed HAR (versioned sdk.js, gAAAAAC...~S)."""
        cfg = self._config(stage="sentinel_req")
        cfg[3] = random.randint(1, 4)
        return "gAAAAAC" + self._b64(cfg) + "~S"

    def generate_chat_requirements_token(self, *, script_url: Any = _UNSET) -> str:
        """chatgpt.com chat-requirements/prepare p from headed HAR."""
        cfg = self._config(stage="chat_prepare")
        cfg[3] = 1
        if script_url is not self._UNSET:
            cfg[5] = script_url
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        """Final enforcement p used on create_account (backend-api/sdk.js, gAAAAAB...~S)."""
        max_attempts = 500000
        cfg = self._config(stage="final")
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = max(1, round(int(time.time() * 1000) - start_ms))
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None) + "~S"


class RegistrationEngine:

    """

    注册引擎

    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用

    """



    def __init__(

        self,

        email_service: Any,

        proxy_url: Optional[str] = None,

        callback_logger: Optional[Callable[[str], None]] = None,

        task_uuid: Optional[str] = None,

        region_code: str = "",

    ):

        """

        初始化注册引擎



        Args:

            email_service: 邮箱服务实例

            proxy_url: 代理 URL

            callback_logger: 日志回调函数

            task_uuid: 任务 UUID（用于数据库记录）

        """

        self.email_service = email_service

        self.proxy_url = proxy_url

        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))

        self.task_uuid = task_uuid

        self.protocol_profile = build_chatgpt_protocol_profile(region_code=region_code, proxy_url=proxy_url)
        self.protocol_fingerprint = ProtocolFingerprint(
            device_id=str(uuid.uuid4()),
            user_agent=LATEST_CHATGPT_FIREFOX_USER_AGENT,
            sec_ch_ua="",
            sec_ch_ua_full="",
            sec_ch_ua_platform='"macOS"',
            sec_ch_ua_platform_version='"10.15.0"',
            sec_ch_ua_arch='"x86"',
            sec_ch_ua_bitness='"64"',
            sec_ch_ua_mobile="?0",
            sec_ch_ua_model='""',
            accept_language=self.protocol_profile.accept_language,
            auth_session_logging_id=str(uuid.uuid4()),
        )
        self._sentinel_runtime_state: dict[str, Any] = {}



        # 创建 HTTP 客户端

        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)
        self.protocol_fingerprint.apply_to_client(self.http_client)



        # 创建 OAuth 管理器

        from .constants import OAUTH_CLIENT_ID, OAUTH_AUTH_URL, OAUTH_TOKEN_URL, OAUTH_REDIRECT_URI, OAUTH_SCOPE

        self.oauth_manager = OAuthManager(

            client_id=OAUTH_CLIENT_ID,

            auth_url=OAUTH_AUTH_URL,

            token_url=OAUTH_TOKEN_URL,

            redirect_uri=OAUTH_REDIRECT_URI,

            scope=OAUTH_SCOPE,

            proxy_url=proxy_url  # 传递代理配置

        )



        # 状态变量

        self.email: Optional[str] = None

        self.password: Optional[str] = None  # 注册密码

        self.email_info: Optional[Dict[str, Any]] = None

        self.oauth_start: Optional[OAuthStart] = None

        self.session: Optional[cffi_requests.Session] = None

        self.session_token: Optional[str] = None  # 会话令牌

        self.logs: list = []

        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳

        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self.prefer_password_totp_login: bool = False  # 已保存密码和 2FA 时重登优先密码+TOTP

        self._device_id: Optional[str] = None

        self._sentinel_token: Optional[str] = None

        self._signup_sentinel: Optional[SentinelPayload] = None

        self._password_sentinel: Optional[SentinelPayload] = None

        self._create_account_continue_url: Optional[str] = None

        self._email_otp_continue_url: Optional[str] = None

        self._email_otp_page_loaded: bool = False

        self._otp_continue_url: Optional[str] = None

        self._otp_page_type: Optional[str] = None
        self._latest_chatgpt_init_final_url: str = ""
        self._email_otp_exhausted: bool = False
        self._email_otp_failure_reason: str = ""
        self._last_about_you_error: str = ""

        self._user_already_exists: bool = False
        self._last_create_account_error_code: str = ""
        self._last_create_account_transport_error: str = ""
        self._latest_chatgpt_cf_attempted: bool = False
        self._chatgpt_client_version: str = LATEST_CHATGPT_OAI_CLIENT_VERSION
        self._chatgpt_client_build_number: str = LATEST_CHATGPT_OAI_CLIENT_BUILD_NUMBER
        self._chatgpt_client_observation_id: str = ""
        self.set_password_after_register: bool = False
        self.totp_secret: str = ""
        self._password_registered_during_flow: bool = False
        self._post_register_password_set: bool = False
        self._post_register_password_error: str = ""

        self._platform_authorize_final_url: str = ""



    def _log(self, message: str, level: str = "info"):

        """记录日志"""

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")

        log_message = f"[{timestamp}] {message}"



        # 添加到日志列表

        self.logs.append(log_message)



        # 调用回调函数

        if self.callback_logger:

            self.callback_logger(message)



        # 记录到数据库（如果有关联任务）

        if self.task_uuid:

            try:

                with get_db() as db:

                    crud.append_task_log(db, self.task_uuid, message)

            except Exception as e:

                logger.warning(f"记录任务日志失败: {e}")



        # 根据级别记录到日志系统

        if level == "error":

            logger.error(message)

        elif level == "warning":

            logger.warning(message)

        else:

            logger.info(message)

    @staticmethod
    def _diag_hash(value: Any, length: int = 10) -> str:
        text = str(value or "")
        if not text:
            return "-"
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:length]

    @classmethod
    def _diag_shape(cls, value: Any, *, prefix: int = 0) -> str:
        text = str(value or "")
        if not text:
            return "no"
        prefix_text = f" prefix={text[:prefix]}" if prefix else ""
        return f"yes len={len(text)} sha={cls._diag_hash(text)}{prefix_text}"

    @staticmethod
    def _diag_bool(value: Any) -> str:
        return "yes" if bool(value) else "no"

    def _diag_cookie_names_text(self) -> str:
        names = sorted(self._latest_chatgpt_cookie_names())
        return ",".join(names) if names else "-"

    def _diag_header_summary(self, headers: dict | None) -> str:
        headers = dict(headers or {})
        lower = {str(k).lower(): v for k, v in headers.items()}
        parts = [
            f"ua={lower.get('user-agent') or '-'}",
            f"accept_language={lower.get('accept-language') or '-'}",
            f"sec_ch_ua={lower.get('sec-ch-ua') or '-'}",
            f"sec_ch_platform={lower.get('sec-ch-ua-platform') or '-'}",
            f"target_route={lower.get('x-openai-target-route') or '-'}",
            f"target_path={lower.get('x-openai-target-path') or '-'}",
            f"oai_client={lower.get('oai-client-version') or '-'}",
            f"oai_build={lower.get('oai-client-build-number') or '-'}",
            f"auth={self._diag_bool(lower.get('authorization'))}",
            f"sentinel={self._diag_shape(lower.get('openai-sentinel-token'))}",
            f"so={self._diag_shape(lower.get('openai-sentinel-so-token'))}",
            f"x_access_flow={self._diag_shape(lower.get('x-access-flow-invocation-id'))}",
        ]
        return " ".join(parts)


    @classmethod
    def _diag_sentinel_p_summary(cls, p_value: Any) -> str:
        text = str(p_value or "")
        if not text:
            return "p=no"
        raw = text[:-2] if text.endswith("~S") else text
        suffix = "~S" if text.endswith("~S") else ""
        prefix = ""
        for candidate in ("gAAAAAC", "gAAAAAB", "gAAAAA"):
            if raw.startswith(candidate):
                prefix = candidate
                raw = raw[len(candidate):]
                break
        idx = raw.find("Wz")
        if idx >= 0:
            raw = raw[idx:]
        try:
            arr = json.loads(base64.b64decode(raw + "=" * ((-len(raw)) % 4)).decode("utf-8"))
        except Exception as exc:
            return f"p_len={len(text)} prefix={prefix or '-'} suffix={suffix or '-'} decode=error:{exc}"
        if not isinstance(arr, list):
            return f"p_len={len(text)} prefix={prefix or '-'} suffix={suffix or '-'} decode=non_list"

        def _safe(index: int, limit: int = 72) -> str:
            if index >= len(arr):
                return "-"
            value = str(arr[index])
            value = " ".join(value.split())
            if len(value) > limit:
                value = value[:limit] + "..."
            return value

        return (
            f"p_len={len(text)} prefix={prefix or '-'} suffix={suffix or '-'} "
            f"screen={_safe(0)} ua={_safe(4, 90)} script={_safe(5, 90)} "
            f"client={_safe(6, 70)} lang={_safe(7)} langs={_safe(8)} "
            f"elapsed={_safe(9)} probe={_safe(10, 90)} react={_safe(11)} "
            f"event={_safe(12)} perf={_safe(13)} cores={_safe(16)} origin_ms={_safe(17)}"
        )

    def _diag_url_summary(self, url: str) -> str:
        text = str(url or "")
        if not text:
            return "-"
        try:
            parsed = urllib.parse.urlparse(text)
            query = urllib.parse.parse_qs(parsed.query)
        except Exception:
            return f"raw_len={len(text)} sha={self._diag_hash(text)}"
        selected: list[str] = []
        for key in (
            "client_id",
            "scope",
            "state",
            "device_id",
            "ext-oai-did",
            "auth_session_logging_id",
            "screen_hint",
            "login_hint",
            "code",
        ):
            value = str((query.get(key) or [""])[0] or "")
            if not value:
                continue
            if key in {"client_id", "screen_hint"}:
                selected.append(f"{key}={value}")
            else:
                selected.append(f"{key}_len={len(value)} {key}_sha={self._diag_hash(value)}")
        return f"host={parsed.netloc or '-'} path={parsed.path or '/'} {' '.join(selected)}".strip()

    def _diag_payload_keys(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return "-"
        keys = sorted(str(k) for k in payload.keys())
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        parts = [f"keys={','.join(keys) if keys else '-'}"]
        if page:
            parts.append(f"page_type={page.get('type') or '-'}")
        if error:
            parts.append(f"error_code={error.get('code') or '-'}")
            parts.append(f"error_type={error.get('type') or '-'}")
        continue_url = str(payload.get("continue_url") or "")
        if continue_url:
            parts.append(f"continue_url=({self._diag_url_summary(continue_url)})")
        return " ".join(parts)

    def _diag_sentinel_challenge_summary(self, data: Any) -> str:
        if not isinstance(data, dict):
            return "-"
        pow_meta = data.get("proofofwork") if isinstance(data.get("proofofwork"), dict) else {}
        turnstile = data.get("turnstile") if isinstance(data.get("turnstile"), dict) else {}
        so_meta = data.get("so") if isinstance(data.get("so"), dict) else {}
        return (
            f"keys={','.join(sorted(str(k) for k in data.keys())) or '-'} "
            f"token={self._diag_shape(data.get('token'))} "
            f"pow_required={self._diag_bool(pow_meta.get('required'))} "
            f"pow_seed={self._diag_shape(pow_meta.get('seed'))} "
            f"pow_difficulty={pow_meta.get('difficulty') or '-'} "
            f"turnstile_dx={self._diag_shape(turnstile.get('dx'))} "
            f"so_required={self._diag_bool(so_meta.get('required'))} "
            f"snapshot_dx={self._diag_shape(so_meta.get('snapshot_dx'))}"
        )


    def _read_oai_did_cookie(self) -> str:

        """读取 OpenAI 设备标识 cookie，兼容多域 CookieJar 冲突场景。"""

        if not self.session:

            return ""

        try:

            return str(self.session.cookies.get("oai-did") or "")

        except Exception:

            return _cookie_value(self.session.cookies, "oai-did")

        return ""


    def _seed_oai_did_cookie(self, device_id: str) -> str:

        """将单个注册会话的设备 ID同步到所有相关站点 Cookie。"""

        did = str(device_id or "").strip() or str(uuid.uuid4())

        if not self.session:

            return did

        try:

            self.session.cookies.set("oai-did", did)

        except Exception:

            try:

                self.session.cookies["oai-did"] = did

            except Exception:

                pass

        for domain in (
            ".auth.openai.com",
            "auth.openai.com",
            ".chatgpt.com",
            "chatgpt.com",
        ):

            try:

                self.session.cookies.set("oai-did", did, domain=domain, path="/")

            except TypeError:

                try:

                    self.session.cookies.set("oai-did", did, domain=domain)

                except Exception:

                    pass

            except Exception:

                pass

        return did


    def _ensure_protocol_device_identity(self, stage: str) -> str:

        """保持 fingerprint、会话字段、oai-did Cookie 使用同一个设备 ID。"""

        fingerprint_did = self._protocol_device_id()
        cookie_did = self._read_oai_did_cookie()
        if cookie_did and cookie_did != fingerprint_did:
            self._log(
                "[REG-DIAG][protocol] device identity mismatch "
                f"stage={stage} fingerprint={self._diag_shape(fingerprint_did)} "
                f"cookie={self._diag_shape(cookie_did)}; normalize_to=fingerprint",
                "warning",
            )
        did = self._seed_oai_did_cookie(fingerprint_did)
        self._device_id = did
        self._log(
            "[REG-DIAG][protocol] device identity "
            f"stage={stage} fingerprint={self._diag_shape(fingerprint_did)} "
            f"session={self._diag_shape(self._device_id)} "
            f"cookie={self._diag_shape(self._read_oai_did_cookie())}"
        )
        return did


    def _protocol_device_id(self) -> str:

        fingerprint = getattr(self, "protocol_fingerprint", None)
        did = str(getattr(fingerprint, "device_id", "") or "").strip()
        if did:
            return did
        return str(uuid.uuid4())



    def _clear_auth_openai_cookies(self) -> int:

        """清理 auth.openai.com 会话 cookie；用于 invalid_state 后重建授权状态。"""

        if not self.session:

            return 0

        removed = 0

        try:

            cookies = list(self.session.cookies)

        except Exception:

            cookies = []

        for cookie in cookies:

            domain = str(getattr(cookie, "domain", "") or "")

            if "auth.openai.com" not in domain:

                continue

            try:

                self.session.cookies.clear(

                    domain=domain,

                    path=getattr(cookie, "path", "/") or "/",

                    name=getattr(cookie, "name", ""),

                )

                removed += 1

            except Exception:

                continue

        return removed



    def _has_cookie(self, name: str) -> bool:

        """粗略判断当前 session 是否存在指定 cookie，仅用于诊断日志。"""

        if not self.session:

            return False

        try:

            if self.session.cookies.get(name):

                return True

        except Exception:

            pass

        return bool(_cookie_value(self.session.cookies, name))



    def _log_auth_state_cookies(self, prefix: str) -> None:

        """打印授权状态关键 cookie，定位 invalid_state 来源。"""

        self._log(

            f"{prefix}: oai-client-auth-session={'yes' if self._has_cookie('oai-client-auth-session') else 'no'}, "

            f"login={'yes' if self._has_cookie('login') else 'no'}, "

            f"oai-did={'yes' if self._has_cookie('oai-did') else 'no'}"

        )



    def _refresh_signup_authorize_state(self, did: str, *, regenerate_oauth: bool = False) -> Optional[SentinelPayload]:

        """重建 authorize 状态并重新生成 authorize_continue Sentinel。"""

        if regenerate_oauth:

            self._log("invalid_state 恢复: 重新从 chatgpt.com 获取 OAuth URL...", "warning")

            if not self._start_oauth():

                self._log("invalid_state 恢复失败: 重新获取 OAuth URL 失败", "warning")

                return None

        if not self.oauth_start or not self.oauth_start.auth_url:

            self._log("invalid_state 恢复失败: 缺少 OAuth URL", "warning")

            return None

        removed = self._clear_auth_openai_cookies()

        did = self._seed_oai_did_cookie(did)

        self._log(f"invalid_state 恢复: 已清理 auth.openai.com cookie {removed} 个，重新访问 authorize")

        try:

            from .constants import CHATGPT_APP

            response = self.session.get(

                self.oauth_start.auth_url,

                headers=self._platform_nav_headers(referer=f"{CHATGPT_APP}/"),

                timeout=30,

                allow_redirects=True,

            )

            self._log(

                f"invalid_state 恢复 authorize 状态: {getattr(response, 'status_code', 'unknown')} "

                f"url={getattr(response, 'url', '')}"

            )

        except Exception as exc:

            self._log(f"invalid_state 恢复 authorize 异常: {exc}", "warning")

            return None

        self._log_auth_state_cookies("invalid_state 恢复后 cookie")

        return self._check_sentinel(did, flow="authorize_continue")



    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:

        """生成随机密码"""

        # OpenAI 注册页对纯字母数字密码存在更高概率拒绝，补一个符号位更稳。

        specials = ",._!@#"

        if length < 10:

            length = 10

        core = ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length - 2))

        return (

            secrets.choice("abcdefghijklmnopqrstuvwxyz")

            + secrets.choice("0123456789")

            + secrets.choice(specials)

            + core

        )[:length]



    def _load_create_account_password_page(self) -> bool:

        """预加载 create-account/password 页面，拿到页面阶段 cookie。"""

        try:

            response = self.session.get(

                "https://auth.openai.com/create-account/password",

                headers=self._latest_chatgpt_nav_headers(referer="https://chatgpt.com/", sec_fetch_site="none"),

                timeout=20,

            )

            self._log(f"加载密码页状态: {response.status_code}")

            return response.status_code == 200

        except Exception as e:

            self._log(f"加载密码页失败: {e}", "warning")

            return False



    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:

        """检查 IP 地理位置"""

        try:

            return self.http_client.check_ip_location()

        except Exception as e:

            self._log(f"检查 IP 地理位置失败: {e}", "error")

            return False, None



    def _create_email(self) -> bool:

        """创建邮箱"""

        try:

            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")

            self.email_info = self.email_service.create_email()



            if not self.email_info or "email" not in self.email_info:

                self._log("创建邮箱失败: 返回信息不完整", "error")

                return False



            self.email = self.email_info["email"]

            self._log(f"成功创建邮箱: {self.email}")

            return True



        except Exception as e:

            self._log(f"创建邮箱失败: {e}", "error")

            return False



    def _start_oauth(self) -> bool:

        """通过 chatgpt.com NextAuth 发起 OAuth 流程"""

        try:

            from .constants import CHATGPT_APP

            self._log("通过 chatgpt.com NextAuth 发起 OAuth...")



            # 1. 访问 chatgpt.com 获取基础 cookie

            self.session.get(f"{CHATGPT_APP}/", timeout=15)

            oai_did = self._ensure_protocol_device_identity("legacy_start_oauth")

            self._log(f"chatgpt.com oai-did 已统一: {oai_did[:20]}...")



            # 2. 获取 CSRF token

            csrf_resp = self.session.get(f"{CHATGPT_APP}/api/auth/csrf", timeout=15)

            csrf_data = csrf_resp.json()

            csrf_token = csrf_data.get("csrfToken", "")

            if not csrf_token:

                # 从 cookie 中提取

                csrf_cookie = self.session.cookies.get("__Host-next-auth.csrf-token", "")

                csrf_token = csrf_cookie.split("%7C")[0] if "%7C" in csrf_cookie else csrf_cookie.split("|")[0]

            self._log(f"CSRF token: {csrf_token[:20]}...")



            # 3. 调用 signin/openai 获取 authorize URL

            signin_query = urllib.parse.urlencode(
                {
                    "prompt": "login",
                    "ext-oai-did": oai_did or "",
                    "auth_session_logging_id": self.protocol_fingerprint.auth_session_logging_id,
                    "screen_hint": "login_or_signup",
                    "login_hint": self.email or "",
                }
            )
            signin_url = f"{CHATGPT_APP}/api/auth/signin/openai?{signin_query}"

            signin_body = urllib.parse.urlencode(
                {
                    "callbackUrl": f"{CHATGPT_APP}/",
                    "csrfToken": csrf_token,
                    "json": "true",
                }
            )
            signin_resp = self.session.post(

                signin_url,

                headers={

                    "accept": "application/json",

                    "content-type": "application/x-www-form-urlencoded",

                    "origin": CHATGPT_APP,

                    "referer": f"{CHATGPT_APP}/",

                },

                data=signin_body,

                allow_redirects=True,

                timeout=15,

            )

            self._log(f"signin/openai 状态: {signin_resp.status_code}")



            if signin_resp.status_code != 200:

                self._log(f"signin/openai 失败: {signin_resp.text}", "error")

                return False



            signin_data = signin_resp.json()

            auth_url = signin_data.get("url", "")

            if not auth_url:

                self._log("signin/openai 未返回 authorize URL", "error")

                return False

            parsed_auth_url = urllib.parse.urlsplit(str(auth_url))
            normalized_path = parsed_auth_url.path.rstrip("/")
            if (
                parsed_auth_url.netloc.endswith("chatgpt.com")
                and normalized_path in {"/api/auth/signin", "/auth/login"}
            ):
                self._log(f"signin/openai 返回登录页/CSRF fallback，未建立 NextAuth OAuth: {auth_url}", "warning")
                return False



            self._log(f"OAuth URL: {auth_url}")



            # 存储为 OAuthStart (不需要 code_verifier，由 chatgpt.com 后端处理)

            self.oauth_start = OAuthStart(

                auth_url=auth_url,

                state="",  # state 由 NextAuth 管理

                code_verifier="",  # 不需要

                redirect_uri="",  # 不需要

            )

            return True



        except Exception as e:

            self._log(f"NextAuth OAuth 流程失败: {e}", "error")

            return False



    def _init_session(self) -> bool:

        """初始化会话"""

        try:

            self.session = self.http_client.session

            return True

        except Exception as e:

            self._log(f"初始化会话失败: {e}", "error")

            return False


    def _init_latest_chatgpt_session(self) -> bool:
        """按 chatgpt_register 最新脚本的 firefox144 请求形态初始化会话。"""
        try:
            self.http_client = OpenAIHTTPClient(
                proxy_url=self.proxy_url,
                config=RequestConfig(timeout=60, max_retries=3, impersonate="firefox135"),
            )
            self.http_client.default_headers["User-Agent"] = LATEST_CHATGPT_FIREFOX_USER_AGENT
            self.http_client.default_headers["Accept-Language"] = self.protocol_profile.accept_language
            self.session = self.http_client.session
            self._log("chatgpt_register 最新链路 HTTP 指纹: firefox135")
            self._log(
                "[REG-DIAG][protocol] session_init "
                f"impersonate=firefox135 proxy={self._diag_bool(self.proxy_url)} "
                f"effective_ua={self._latest_chatgpt_user_agent()} "
                f"effective_accept_language={self._latest_chatgpt_accept_language()} "
                f"profile_region={self.protocol_profile.country_code} "
                f"profile_timezone={self.protocol_profile.timezone_name} "
                f"profile_offset={self.protocol_profile.timezone_offset_min} "
                f"fp_device_id={self._diag_shape(self.protocol_fingerprint.device_id)} "
                f"fp_auth_session_logging_id={self._diag_shape(self.protocol_fingerprint.auth_session_logging_id)} "
                f"fp_ua={self.protocol_fingerprint.user_agent} "
                f"fp_sec_ch_ua={self.protocol_fingerprint.sec_ch_ua} "
                f"fp_sec_ch_platform={self.protocol_fingerprint.sec_ch_ua_platform}"
            )
            return True
        except Exception as e:
            self._log(f"初始化 chatgpt_register 最新会话失败: {e}", "error")
            return False



    @staticmethod
    def _response_json_dict(response) -> dict:
        try:
            data = response.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}


    @staticmethod
    def _openai_error_code_from_payload(payload: dict) -> str:
        error = payload.get("error") if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            return str(error.get("code") or "").strip()
        return ""


    @staticmethod
    def _short_response_excerpt(response, max_length: int = 180) -> str:
        text = str(getattr(response, "text", "") or "")
        if not text:
            return ""
        excerpt = " ".join(text.split())
        if len(excerpt) > max_length:
            return excerpt[:max_length] + "..."
        return excerpt


    def _latest_chatgpt_signin_no_authorize_error(self, response, payload: dict) -> str:
        status = getattr(response, "status_code", 0)
        body = str(getattr(response, "text", "") or "")
        if is_cloudflare_managed_challenge_html(body):
            return f"signin_no_authorize_url_http_{status}:cloudflare_managed_challenge"
        error_code = self._openai_error_code_from_payload(payload)
        if error_code:
            return f"signin_no_authorize_url_http_{status}:{error_code}"
        excerpt = self._short_response_excerpt(response)
        if excerpt:
            return f"signin_no_authorize_url_http_{status}:body={excerpt}"
        return f"signin_no_authorize_url_http_{status}"


    def _latest_chatgpt_user_agent(self) -> str:
        http_client = getattr(self, "http_client", None)
        default_headers = getattr(http_client, "default_headers", {}) if http_client else {}
        return str(
            default_headers.get("User-Agent")
            or default_headers.get("user-agent")
            or LATEST_CHATGPT_FIREFOX_USER_AGENT
        )


    def _latest_chatgpt_accept_language(self) -> str:
        http_client = getattr(self, "http_client", None)
        default_headers = getattr(http_client, "default_headers", {}) if http_client else {}
        configured = str(default_headers.get("Accept-Language") or default_headers.get("accept-language") or "")
        if configured:
            return configured
        if "Firefox/" in self._latest_chatgpt_user_agent():
            return self.protocol_profile.accept_language
        fingerprint = getattr(self, "protocol_fingerprint", None)
        return str(getattr(fingerprint, "accept_language", "en-US,en;q=0.9") or "en-US,en;q=0.9")


    def _latest_chatgpt_sentinel_sdk_url(self, *, force: bool = False) -> str:
        return get_latest_sentinel_sdk_url(
            force=force,
            session=self.session,
            accept_language=self._latest_chatgpt_accept_language(),
            user_agent=self._latest_chatgpt_user_agent(),
        )


    def _latest_chatgpt_sentinel_frame_url(self, *, force: bool = False) -> str:
        return get_latest_sentinel_frame_url(
            force=force,
            session=self.session,
            accept_language=self._latest_chatgpt_accept_language(),
            user_agent=self._latest_chatgpt_user_agent(),
        )


    def _latest_chatgpt_client_version(self) -> str:
        return str(getattr(self, "_chatgpt_client_version", "") or LATEST_CHATGPT_OAI_CLIENT_VERSION)


    def _latest_chatgpt_client_build_number(self) -> str:
        return str(getattr(self, "_chatgpt_client_build_number", "") or LATEST_CHATGPT_OAI_CLIENT_BUILD_NUMBER)


    def _latest_chatgpt_client_observation(self, *, refresh: bool = False) -> str:
        current = str(getattr(self, "_chatgpt_client_observation_id", "") or "").strip()
        if refresh or not current:
            current = "v1.r.p." + secrets.token_urlsafe(12)[:16]
            self._chatgpt_client_observation_id = current
        return current


    def _latest_chatgpt_update_client_build_from_html(self, html: str, *, source: str) -> None:
        text = str(html or "")
        if not text:
            return
        version_match = re.search(r"""data-build=["']([^"']+)["']""", text)
        build_match = re.search(r"""data-seq=["']([^"']+)["']""", text)
        version = str(version_match.group(1)).strip() if version_match else ""
        build = str(build_match.group(1)).strip() if build_match else ""
        changed = False
        if version and version != self._latest_chatgpt_client_version():
            self._chatgpt_client_version = version
            changed = True
        if build and build != self._latest_chatgpt_client_build_number():
            self._chatgpt_client_build_number = build
            changed = True
        if changed:
            self._log(
                "[REG-DIAG][protocol] client_build updated "
                f"source={source} version={self._latest_chatgpt_client_version()} "
                f"build={self._latest_chatgpt_client_build_number()}"
            )


    def _latest_chatgpt_browser_headers(
        self,
        *,
        accept: str,
        referer: str = "",
        origin: str = "",
        content_type: str = "",
        sec_fetch_dest: str = "empty",
        sec_fetch_mode: str = "cors",
        sec_fetch_site: str = "same-origin",
        include_datadog: bool = False,
    ) -> dict:
        """最新版 chatgpt_register 链路的浏览器态请求头。"""
        headers = {
            "accept": accept,
            "accept-language": self._latest_chatgpt_accept_language(),
            "sec-fetch-dest": sec_fetch_dest,
            "sec-fetch-mode": sec_fetch_mode,
            "sec-fetch-site": sec_fetch_site,
            "user-agent": self._latest_chatgpt_user_agent(),
        }
        if content_type:
            headers["content-type"] = content_type
        if origin:
            headers["origin"] = origin
        if referer:
            headers["referer"] = referer

        user_agent = headers["user-agent"]
        if "Chrome/" in user_agent and "Firefox/" not in user_agent:
            fingerprint = getattr(self, "protocol_fingerprint", None)
            headers.update(
                {
                    "priority": "u=1, i",
                    "sec-ch-ua": getattr(fingerprint, "sec_ch_ua", PLATFORM_REFERENCE_SEC_CH_UA),
                    "sec-ch-ua-mobile": getattr(fingerprint, "sec_ch_ua_mobile", "?0"),
                    "sec-ch-ua-platform": getattr(fingerprint, "sec_ch_ua_platform", '"Windows"'),
                }
            )
        if include_datadog:
            headers.update(_generate_datadog_trace_headers())
        return headers


    def _latest_chatgpt_json_headers(self, *, referer: str, origin: str = "https://auth.openai.com") -> dict:
        headers = self._latest_chatgpt_browser_headers(
            accept="application/json",
            referer=referer,
            origin=origin,
            content_type="application/json",
            include_datadog=True,
        )
        # Headed auth.openai.com JSON (email-otp/create_account) does not send oai-device-id;
        # device identity is carried by oai-did cookie + sentinel id field.
        headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
        return headers


    def _latest_chatgpt_sentinel_headers(self, *, user_agent: str = "", accept_language: str = "") -> dict:
        """Sentinel iframe/SDK 内部 req 请求头，保持与当前注册会话 UA/语言一致。"""
        return {
            "accept": "*/*",
            "accept-language": accept_language or self._latest_chatgpt_accept_language(),
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://sentinel.openai.com",
            "referer": self._latest_chatgpt_sentinel_frame_url(),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent or self._latest_chatgpt_user_agent(),
        }


    def _latest_chatgpt_nav_headers(
        self,
        *,
        referer: str = "",
        sec_fetch_site: str = "same-origin",
    ) -> dict:
        headers = self._latest_chatgpt_browser_headers(
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=referer,
            sec_fetch_dest="document",
            sec_fetch_mode="navigate",
            sec_fetch_site=sec_fetch_site,
        )
        headers["upgrade-insecure-requests"] = "1"
        return headers


    def _latest_chatgpt_add_password_headers(self) -> dict:
        """HAR 对齐的 auth.openai.com 添加密码 JSON 请求头。"""
        headers = self._latest_chatgpt_browser_headers(
            accept="application/json",
            referer=LATEST_CHATGPT_ADD_PASSWORD_PAGE_URL,
            origin="https://auth.openai.com",
            content_type="application/json",
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-origin",
        )
        headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
        return headers


    def _latest_chatgpt_add_password_complete_reauth_otp(self, current_url: str):
        """设置密码 reauth 进入邮箱验证页时，完成该 OTP 分支后返回添加密码页响应。"""
        self._email_otp_continue_url = current_url or "https://auth.openai.com/email-verification"
        self._email_otp_page_loaded = True
        self._otp_sent_at = time.time()
        self._log("设置帐号密码: reauth 进入邮箱验证页，等待邮箱验证码")
        code = self._get_verification_code(mark_invalid_on_timeout=False, resend_on_timeout=False)
        if not code:
            self._post_register_password_error = "reauth_email_otp_timeout"
            self._log("设置帐号密码: reauth 邮箱验证码超时，跳过 2FA 前置密码设置", "warning")
            return None
        payload = self._latest_chatgpt_validate_email_otp(code)
        candidates = [str(payload.get("continue_url") or "").strip()]
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        candidates.append(str(page_payload.get("url") or "").strip())
        for candidate in candidates:
            if not candidate:
                continue
            page_url = urllib.parse.urljoin("https://auth.openai.com/", candidate)
            if LATEST_CHATGPT_ADD_PASSWORD_PAGE_URL not in page_url:
                continue
            headers = self._latest_chatgpt_nav_headers(
                referer="https://auth.openai.com/email-verification",
                sec_fetch_site="same-origin",
            )
            self._log(
                "[REG-DIAG][protocol] add_password reauth otp page request "
                f"url={page_url} headers=({self._diag_header_summary(headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
            return self.session.get(page_url, headers=headers, timeout=30)
        self._post_register_password_error = f"reauth_otp_no_password_page: {self._diag_payload_keys(payload)}"
        self._log("设置帐号密码: reauth OTP 后未返回添加密码页，跳过", "warning")
        return None


    def _latest_chatgpt_add_password_after_register(self, access_token: str = "") -> bool:
        """在 ChatGPT callback/session 建立后，按 HAR 的 reauth=password 链路添加账号密码。"""
        self._post_register_password_error = ""
        if not bool(getattr(self, "set_password_after_register", True)):
            self._log("设置帐号密码: 未勾选，跳过")
            return False
        if bool(getattr(self, "_password_registered_during_flow", False)):
            self._post_register_password_set = True
            self._log("设置帐号密码: 注册密码阶段已完成，跳过重复添加")
            return True
        if not self.session:
            self._post_register_password_error = "missing_session"
            self._log("设置帐号密码: 缺少 auth 会话，跳过", "warning")
            return False
        session_token = _cookie_value(self.session.cookies, "__Secure-next-auth.session-token").strip()
        if not session_token:
            self._post_register_password_error = "missing_chatgpt_session"
            self._log("设置帐号密码: ChatGPT session 尚未建立，跳过", "warning")
            return False
        password = str(self.password or "").strip()
        if not password:
            password = self._generate_password()
            self.password = password
        try:
            from .constants import CHATGPT_APP

            did = str(self._device_id or self._read_oai_did_cookie() or "").strip()
            if not did:
                did = self._seed_oai_did_cookie(self._protocol_device_id())
            self._device_id = did
            if str(access_token or "").strip():
                self._latest_chatgpt_warmup_security_settings(
                    str(access_token or "").strip(),
                    label="before_add_password",
                )
            self._refresh_mailbox_before_ids()
            auth_json_headers = self._latest_chatgpt_browser_headers(
                accept="*/*",
                referer=f"{CHATGPT_APP}/",
                content_type="application/json",
                sec_fetch_dest="empty",
                sec_fetch_mode="cors",
                sec_fetch_site="same-origin",
            )
            providers_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/providers",
                headers=auth_json_headers,
                timeout=20,
            )
            self._log(
                "[REG-DIAG][protocol] add_password providers "
                f"status={getattr(providers_resp, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(auth_json_headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
            csrf_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/csrf",
                headers=auth_json_headers,
                timeout=20,
            )
            csrf_data = self._response_json_dict(csrf_resp)
            csrf_token = str(csrf_data.get("csrfToken") or "").strip()
            if not csrf_token:
                csrf_cookie = str(self.session.cookies.get("__Host-next-auth.csrf-token", "") or "")
                csrf_token = csrf_cookie.split("%7C")[0] if "%7C" in csrf_cookie else csrf_cookie.split("|")[0]
            if not csrf_token:
                self._post_register_password_error = "csrf_token_missing"
                self._log("设置帐号密码: 获取 CSRF token 失败，跳过", "warning")
                return False

            query = urllib.parse.urlencode(
                {
                    "connection": "password",
                    "login_hint": self.email or "",
                    "reauth": "password",
                    "post_login_add_password": "true",
                    "max_age": "0",
                    "ext-oai-did": did,
                }
            )
            signin_headers = self._latest_chatgpt_browser_headers(
                accept="*/*",
                content_type="application/x-www-form-urlencoded",
                origin=CHATGPT_APP,
                referer=f"{CHATGPT_APP}/",
                sec_fetch_dest="empty",
                sec_fetch_mode="cors",
                sec_fetch_site="same-origin",
            )
            signin_resp = self.session.post(
                f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
                headers=signin_headers,
                data=urllib.parse.urlencode(
                    {
                        "callbackUrl": f"{CHATGPT_APP}/",
                        "csrfToken": csrf_token,
                        "json": "true",
                    }
                ),
                allow_redirects=False,
                timeout=20,
            )
            signin_data = self._response_json_dict(signin_resp)
            next_url = str(signin_data.get("url") or signin_resp.headers.get("Location") or "").strip()
            self._log(
                "[REG-DIAG][protocol] add_password signin/openai "
                f"status={getattr(signin_resp, 'status_code', 0)} "
                f"query=({self._diag_url_summary(f'{CHATGPT_APP}/api/auth/signin/openai?{query}')}) "
                f"csrf={self._diag_shape(csrf_token)} "
                f"next=({self._diag_url_summary(next_url)}) "
                f"headers=({self._diag_header_summary(signin_headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
            if not next_url:
                self._post_register_password_error = self._latest_chatgpt_signin_no_authorize_error(
                    signin_resp,
                    signin_data,
                )
                self._log(f"设置帐号密码: reauth 初始化失败: {self._post_register_password_error}", "warning")
                return False

            current_url = next_url
            page_resp = None
            for redirect_index in range(12):
                page_resp = self.session.get(
                    current_url,
                    headers=self._latest_chatgpt_nav_headers(
                        referer=f"{CHATGPT_APP}/" if redirect_index == 0 else current_url,
                        sec_fetch_site="cross-site" if redirect_index == 0 else "same-origin",
                    ),
                    allow_redirects=False,
                    timeout=30,
                )
                location = str(page_resp.headers.get("Location") or page_resp.headers.get("location") or "").strip()
                self._log(
                    "[REG-DIAG][protocol] add_password auth redirect "
                    f"idx={redirect_index + 1} status={getattr(page_resp, 'status_code', 0)} "
                    f"url=({self._diag_url_summary(current_url)}) "
                    f"location=({self._diag_url_summary(location)}) "
                    f"cookies={self._diag_cookie_names_text()}"
                )
                if 300 <= int(getattr(page_resp, "status_code", 0) or 0) < 400 and location:
                    current_url = urllib.parse.urljoin(current_url, location)
                    continue
                break

            final_url = str(getattr(page_resp, "url", "") or current_url or "")
            if LATEST_CHATGPT_ADD_PASSWORD_PAGE_URL not in final_url and (
                "/email-verification" in final_url or "/email-otp" in final_url
            ):
                page_resp = self._latest_chatgpt_add_password_complete_reauth_otp(final_url)
                final_url = str(
                    getattr(page_resp, "url", "")
                    or str(getattr(self, "_otp_continue_url", "") or "")
                    or final_url
                )
            if page_resp is None:
                if not self._post_register_password_error:
                    self._post_register_password_error = "reauth_no_password_page"
                self._log("设置帐号密码: reauth 未返回添加密码页，跳过", "warning")
                return False
            page_status = int(getattr(page_resp, "status_code", 0) or 0)
            final_url = str(getattr(page_resp, "url", "") or final_url or current_url)
            self._log(
                f"设置帐号密码: 打开添加密码页状态: {page_status} final_url={final_url}"
            )
            if LATEST_CHATGPT_ADD_PASSWORD_PAGE_URL not in final_url:
                self._post_register_password_error = f"reauth_unexpected_page: {final_url}"
                self._log(f"设置帐号密码: reauth 未进入添加密码页: {final_url}", "warning")
                return False
            if page_status >= 400:
                self._post_register_password_error = (
                    f"page_http_{page_status}: {self._short_response_excerpt(page_resp) or '(empty)'}"
                )
                self._log(f"设置帐号密码: 添加密码页失败: {self._post_register_password_error}", "warning")
                return False

            body = json.dumps({"password": password}, separators=(",", ":"))
            headers = self._latest_chatgpt_add_password_headers()
            password_sentinel = self._check_sentinel(did, flow="password_reset")
            if password_sentinel:
                headers["openai-sentinel-token"] = self._sentinel_payload_header(password_sentinel, did)
                self._log(
                    f"设置帐号密码 Sentinel 已获取: flow={password_sentinel.flow} "
                    f"t_len={len(password_sentinel.t)}"
                )
            self._log(
                "[REG-DIAG][protocol] add_password request "
                f"endpoint={LATEST_CHATGPT_ADD_PASSWORD_API_URL} "
                f"body_len={len(body)} "
                f"headers=({self._diag_header_summary(headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
            response = self.session.post(
                LATEST_CHATGPT_ADD_PASSWORD_API_URL,
                headers=headers,
                data=body,
                timeout=30,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            payload = self._response_json_dict(response)
            response_text = str(getattr(response, "text", "") or "")
            self._log(
                "[REG-DIAG][protocol] add_password response "
                f"status={status} {self._diag_payload_keys(payload)} "
                f"body_len={len(response_text)} cookies={self._diag_cookie_names_text()}"
            )
            if status != 200:
                self._post_register_password_error = f"http_{status}: {response_text[:240]}"
                self._log(f"设置帐号密码失败: {self._post_register_password_error}", "warning")
                return False
            continue_url = str(payload.get("continue_url") or "").strip()
            if continue_url:
                callback_url = urllib.parse.urljoin("https://auth.openai.com/", continue_url)
                callback_headers = self._latest_chatgpt_nav_headers(
                    referer=LATEST_CHATGPT_ADD_PASSWORD_PAGE_URL,
                    sec_fetch_site="cross-site",
                )
                self._log(
                    "[REG-DIAG][protocol] add_password callback request "
                    f"url=({self._diag_url_summary(callback_url)}) "
                    f"headers=({self._diag_header_summary(callback_headers)}) "
                    f"cookies={self._diag_cookie_names_text()}"
                )
                callback_resp = self.session.get(
                    callback_url,
                    headers=callback_headers,
                    allow_redirects=True,
                    timeout=45,
                )
                callback_status = int(getattr(callback_resp, "status_code", 0) or 0)
                self._log(
                    "[REG-DIAG][protocol] add_password callback response "
                    f"status={callback_status} "
                    f"final_url=({self._diag_url_summary(str(getattr(callback_resp, 'url', '') or ''))}) "
                    f"cookies={self._diag_cookie_names_text()}"
                )
                if callback_status >= 400:
                    self._post_register_password_error = (
                        f"callback_http_{callback_status}: "
                        f"{self._short_response_excerpt(callback_resp) or '(empty)'}"
                    )
                    self._log(f"设置帐号密码 callback 失败: {self._post_register_password_error}", "warning")
                    return False
                self._chatgpt_client_observation_id = ""
                self._chatgpt_oai_session_id = str(uuid.uuid4())
                self._log("设置帐号密码完成，并已刷新 ChatGPT session")
            else:
                self._log("设置帐号密码完成，但响应未返回新的 callback，继续使用原 callback", "warning")
            self._post_register_password_set = True
            return True
        except Exception as exc:
            self._post_register_password_error = str(exc)[:240]
            self._log(f"设置帐号密码异常，继续保留当前注册结果: {exc}", "warning")
            return False


    @staticmethod
    def _is_latest_chatgpt_init_retryable_error(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        retry_markers = ("signin_no_authorize_url_http_403",) + RegistrationEngine._CHATGPT_TRANSPORT_RETRY_MARKERS
        return any(marker in text for marker in retry_markers)


    _CHATGPT_TRANSPORT_RETRY_MARKERS: tuple[str, ...] = (
        "tls connect error",
        "openssl_internal",
        "boringssl",
        "bad_decrypt",
        "curl: (28)",
        "curl: (35)",
        "curl: (55)",
        "curl: (56)",
        "curl: (97)",
        "connection reset",
        "connection aborted",
        "connection closed",
        "remote end closed connection",
        "connect timeout",
        "connection timeout",
        "timed out",
        "timeout",
        "proxyerror",
        "proxy error",
        "network error",
        "failed to perform",
        "ssl_read",
    )


    @classmethod
    def _is_chatgpt_transport_retryable_error(cls, message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        return any(marker in text for marker in cls._CHATGPT_TRANSPORT_RETRY_MARKERS)


    def _close_sentinel_runtime(self) -> None:
        try:
            from .authflow_experimental.sentinel_quickjs import close_sentinel_runtime

            def close_tree(state: dict[str, Any]) -> None:
                for value in list(state.values()):
                    if isinstance(value, dict):
                        close_tree(value)
                close_sentinel_runtime(state)

            close_tree(self._sentinel_runtime_state)
            self._sentinel_runtime_state.clear()
        except Exception:
            pass


    def close(self) -> None:
        self._close_sentinel_runtime()
        try:
            close = getattr(self.session, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


    def __del__(self) -> None:
        try:
            self._close_sentinel_runtime()
        except Exception:
            pass

    def _reset_latest_chatgpt_session_for_retry(self) -> None:
        self._close_sentinel_runtime()
        try:
            close = getattr(self.session, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        self._init_latest_chatgpt_session()
        self._device_id = None
        self._create_account_continue_url = None
        self._email_otp_continue_url = None
        self._email_otp_page_loaded = False
        self._otp_continue_url = None
        self._otp_page_type = None
        self._latest_chatgpt_init_final_url = ""
        self._last_about_you_error = ""
        self._last_create_account_transport_error = ""
        self._chatgpt_client_observation_id = ""
        self._sentinel_runtime_state = {}


    def _latest_chatgpt_chatgpt_client_headers(
        self,
        *,
        referer: str = "https://chatgpt.com/",
        target_path: str = "",
    ) -> dict:
        """chatgpt.com backend headers seen in headed-browser HAR."""
        headers = self._latest_chatgpt_browser_headers(
            accept="*/*",
            referer=referer,
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-origin",
        )
        if self._device_id:
            headers["oai-device-id"] = self._device_id
        headers["oai-language"] = self.protocol_profile.language
        headers["oai-client-version"] = self._latest_chatgpt_client_version()
        headers["oai-client-build-number"] = self._latest_chatgpt_client_build_number()
        session_id = str(getattr(self, "_chatgpt_oai_session_id", "") or "").strip()
        if not session_id:
            session_id = str(uuid.uuid4())
            self._chatgpt_oai_session_id = session_id
        headers["oai-session-id"] = session_id
        if target_path:
            headers["x-openai-target-path"] = target_path
            target_route = target_path
            if target_path.endswith("/accounts/check/v4-2023-04-27"):
                target_route = target_path[: -len("v4-2023-04-27")] + "{version}"
            elif "/checkout_pricing_config/configs/" in target_path:
                target_route = re.sub(r"/configs/[^/?#]+$", "/configs/{country_code}", target_path)
            headers["x-openai-target-route"] = target_route
        if target_path.startswith("/backend-api/"):
            headers["x-oai-is-client-observation"] = self._latest_chatgpt_client_observation()
        return headers


    def _seed_named_cookie(self, name: str, value: str, domains: tuple[str, ...] = (".auth.openai.com", "auth.openai.com", ".openai.com", "openai.com")) -> None:
        """把关键 cookie 种进协议会话，兼容 curl_cffi CookieJar 的多种 set 签名。"""
        if not self.session or not name or not value:
            return
        try:
            self.session.cookies.set(name, value)
        except Exception:
            pass
        for domain in domains:
            try:
                self.session.cookies.set(name, value, domain=domain, path="/")
            except TypeError:
                try:
                    self.session.cookies.set(name, value, domain=domain)
                except Exception:
                    pass
            except Exception:
                pass

    def _delete_named_cookie(self, name: str) -> None:
        if not self.session or not name:
            return
        cookies = getattr(self.session, "cookies", None)
        for domain in (None, "chatgpt.com", ".chatgpt.com", "auth.openai.com", ".auth.openai.com"):
            try:
                if domain:
                    cookies.delete(name, domain=domain, path="/")
                else:
                    cookies.delete(name)
            except Exception:
                pass


    def _latest_chatgpt_cookie_names(self) -> set[str]:
        names: set[str] = set()
        if not self.session:
            return names
        for cookie in _iter_cookie_records(self.session.cookies) or []:
            name = str(cookie.get("name") or "").strip()
            if name:
                names.add(name)
        return names

    def _latest_chatgpt_has_cf_clearance(self) -> bool:
        return "cf_clearance" in self._latest_chatgpt_cookie_names()

    def _solve_session_observer_token(
        self,
        *,
        device_id: str,
        flow: str,
        challenge: dict,
        request_p: str,
        user_agent: str,
    ) -> str:
        """用本地 Sentinel VM 解 so.snapshot_dx，生成 HAR 同形态的 openai-sentinel-so-token。"""
        so_meta = challenge.get("so") if isinstance(challenge, dict) else {}
        if not isinstance(so_meta, dict) or not so_meta.get("required"):
            return ""
        snapshot_dx = str(so_meta.get("snapshot_dx") or "").strip()
        if not snapshot_dx:
            return ""
        try:
            from .sentinel_vm import solve_turnstile_dx

            so_value = solve_turnstile_dx(
                snapshot_dx,
                request_p,
                user_agent=user_agent,
                sdk_url=self._latest_chatgpt_sentinel_sdk_url(),
            )
        except Exception as exc:
            self._log(f"Sentinel so VM 失败: flow={flow} {exc}", "warning")
            return ""
        so_value = str(so_value or "").strip()
        if not so_value:
            return ""
        token_c = str(challenge.get("token") or "").strip()
        payload = {
            "so": so_value,
            "id": device_id,
            "flow": flow,
        }
        if token_c:
            payload["c"] = token_c
        return json.dumps(payload, separators=(",", ":"))

    def _latest_chatgpt_protocol_cookie_items(self) -> list[dict[str, str]]:
        """导出协议会话 cookie，供无头浏览器同会话注入。"""
        items: list[dict[str, str]] = []
        if not self.session:
            return items
        seen: set[tuple[str, str, str]] = set()

        def _add(name: str, value: str, domain: str = "", path: str = "/") -> None:
            name = str(name or "").strip()
            value = str(value or "")
            if not name or value == "":
                return
            domain = str(domain or "").strip() or ".auth.openai.com"
            path = str(path or "/").strip() or "/"
            key = (name, domain, path)
            if key in seen:
                return
            seen.add(key)
            item = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
            }
            items.append(item)

        for cookie in _iter_cookie_records(self.session.cookies) or []:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "")
            path = str(cookie.get("path") or "/")
            _add(name, value, domain=domain, path=path)
            if not domain:
                _add(name, value, domain=".auth.openai.com", path="/")
                _add(name, value, domain="auth.openai.com", path="/")
            if name.startswith("__Host-") or name.startswith("__Secure-") or name in {
                "oai-did",
                "__cflb",
                "__cf_bm",
                "_cfuvid",
                "cf_clearance",
                "oai-sc",
            }:
                _add(name, value, domain=".chatgpt.com", path="/")
                _add(name, value, domain="chatgpt.com", path="/")
        if self._device_id:
            _add("oai-did", str(self._device_id), domain=".auth.openai.com", path="/")
            _add("oai-did", str(self._device_id), domain="auth.openai.com", path="/")
            _add("oai-did", str(self._device_id), domain=".chatgpt.com", path="/")
            _add("oai-did", str(self._device_id), domain="chatgpt.com", path="/")
        return items

    def _latest_chatgpt_import_browser_cookies(
        self,
        cookies: list[dict],
        *,
        only_names: set[str] | frozenset[str] | None = None,
    ) -> None:
        """把无头浏览器 cookie 回填到协议会话。

        only_names 用于只回填 Cloudflare 相关 cookie，避免 page.goto 产生的新 auth cookie
        覆盖协议会话中的 oai-client-auth-session / login_session 等关键状态。
        """
        if not self.session:
            return
        allowed = {str(n).strip() for n in (only_names or set()) if str(n).strip()}
        for cookie in cookies or []:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            if not name or value == "":
                continue
            if allowed and name not in allowed:
                continue
            domain = str(cookie.get("domain") or "").strip()
            domains = []
            if domain:
                domains.append(domain)
            # 关键域名兜底，避免 CookieJar 域匹配失败。
            if "auth.openai.com" in domain or not domain:
                domains.extend([".auth.openai.com", "auth.openai.com", ".openai.com", "openai.com"])
            if "chatgpt.com" in domain:
                domains.extend([".chatgpt.com", "chatgpt.com"])
            if name in {"cf_clearance", "__cf_bm", "__cflb", "_cfuvid", "oai-did"}:
                domains.extend([".auth.openai.com", "auth.openai.com", ".openai.com", "openai.com"])
            # de-dup while preserving order
            uniq_domains = []
            for d in domains:
                if d and d not in uniq_domains:
                    uniq_domains.append(d)
            self._seed_named_cookie(name, value, domains=tuple(uniq_domains or (".auth.openai.com", "auth.openai.com")))

    def _latest_chatgpt_camoufox_launch_opts(self) -> dict[str, Any]:
        proxy_cfg = None
        proxy_url = str(getattr(self, "proxy_url", "") or "").strip()
        if proxy_url:
            try:
                from platforms.chatgpt.browser_register import _build_proxy_config

                proxy_cfg = _build_proxy_config(proxy_url)
            except Exception:
                proxy_cfg = {"server": proxy_url}
        launch_opts: dict[str, Any] = {
            "headless": True,
            "os": "windows",
            "humanize": False,
        }
        if proxy_cfg:
            launch_opts["proxy"] = proxy_cfg
            try:
                from platforms.chatgpt.browser_register import _is_local_proxy

                if not _is_local_proxy(proxy_url):
                    launch_opts["geoip"] = True
            except Exception:
                pass
        return launch_opts

    def _latest_chatgpt_seed_cf_clearance_via_headless(
        self,
        device_id: str,
        *,
        target_url: str = "https://auth.openai.com/email-verification",
        force: bool = False,
    ) -> bool:
        """在已有 auth 会话 cookie 基础上，后台无头浏览器解 CF jsd 并回填。"""
        if not self.session or not device_id:
            return False
        if (not force) and self._latest_chatgpt_has_cf_clearance():
            return True
        if (not force) and getattr(self, "_latest_chatgpt_cf_attempted", False):
            self._log("chatgpt_register 本次注册已尝试无头浏览器 cf_clearance，跳过重复启动", "warning")
            return False
        if str(__import__("os").environ.get("OPENAI_PROTOCOL_DISABLE_HEADLESS_CF") or "").strip():
            self._log("chatgpt_register 已禁用无头浏览器 cf_clearance 补齐", "warning")
            return False
        if str(__import__("os").environ.get("PYTEST_CURRENT_TEST") or "").strip():
            # 单元测试不启动 Camoufox，避免卡死；生产注册链路仍走无头浏览器参数计算。
            return False
        self._latest_chatgpt_cf_attempted = True
        try:
            from camoufox.sync_api import Camoufox
        except Exception as exc:
            self._log(f"chatgpt_register 无头浏览器 cf_clearance 不可用: {exc}", "warning")
            return False

        ua = self._latest_chatgpt_user_agent()
        cookie_items = self._latest_chatgpt_protocol_cookie_items()
        launch_opts = self._latest_chatgpt_camoufox_launch_opts()
        try:
            with Camoufox(**launch_opts) as browser:
                page = browser.new_page()
                try:
                    page.set_extra_http_headers(
                        {
                            "user-agent": ua,
                            "accept-language": self._latest_chatgpt_accept_language(),
                        }
                    )
                except Exception:
                    pass
                if cookie_items:
                    try:
                        page.context.add_cookies(cookie_items)
                    except Exception as exc:
                        self._log(f"chatgpt_register 注入协议 cookie 到无头浏览器失败: {exc}", "warning")
                page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                deadline = time.time() + 25
                cf_value = ""
                while time.time() < deadline:
                    try:
                        for cookie in page.context.cookies("https://auth.openai.com"):
                            if cookie.get("name") == "cf_clearance" and cookie.get("value"):
                                cf_value = str(cookie.get("value") or "")
                                break
                    except Exception:
                        pass
                    if cf_value:
                        break
                    time.sleep(0.4)
                browser_cookies = []
                try:
                    browser_cookies = page.context.cookies()
                except Exception:
                    browser_cookies = []
                if browser_cookies:
                    # 只回填 CF 相关 cookie，禁止覆盖协议 auth 会话 cookie。
                    self._latest_chatgpt_import_browser_cookies(
                        browser_cookies,
                        only_names={"cf_clearance", "__cf_bm", "__cflb", "_cfuvid"},
                    )
                if not cf_value and not self._latest_chatgpt_has_cf_clearance():
                    self._log("chatgpt_register 无头浏览器未拿到 cf_clearance", "warning")
                    return False
                self._log(
                    f"chatgpt_register 无头浏览器已补齐 cf_clearance "
                    f"(len={len(cf_value) if cf_value else 'existing'}, cookies={len(browser_cookies)})"
                )
                return True
        except Exception as exc:
            self._log(f"chatgpt_register 无头浏览器 cf_clearance 失败: {exc}", "warning")
            return False

    def _latest_chatgpt_headless_auth_json(
        self,
        *,
        url: str,
        body: str,
        referer: str,
        headers: dict,
        label: str,
    ) -> tuple[int, dict, str]:
        """关键 Auth JSON 请求改走无头浏览器同源 fetch，保证 CF/TLS 与有头会话一致。"""
        # 默认关闭关键 Auth JSON 的无头执行：page.goto 容易污染 oai-client-auth-session 并触发 invalid_state。
        # 仅当显式 OPENAI_PROTOCOL_ENABLE_HEADLESS_AUTH=1 时启用；无头浏览器只用于 CF/参数计算。
        enable_headless_auth = str(__import__("os").environ.get("OPENAI_PROTOCOL_ENABLE_HEADLESS_AUTH") or "").strip()
        if not enable_headless_auth:
            raise RuntimeError("headless_auth_disabled_by_default")
        if str(__import__("os").environ.get("OPENAI_PROTOCOL_DISABLE_HEADLESS_AUTH") or "").strip():
            raise RuntimeError("headless_auth_disabled")
        if str(__import__("os").environ.get("PYTEST_CURRENT_TEST") or "").strip():
            raise RuntimeError("headless_auth_disabled_in_pytest")
        try:
            from camoufox.sync_api import Camoufox
            from platforms.chatgpt.browser_register import _browser_fetch
        except Exception as exc:
            raise RuntimeError(f"headless_auth_unavailable: {exc}") from exc

        ua = self._latest_chatgpt_user_agent()
        cookie_items = self._latest_chatgpt_protocol_cookie_items()
        launch_opts = self._latest_chatgpt_camoufox_launch_opts()
        with Camoufox(**launch_opts) as browser:
            page = browser.new_page()
            try:
                page.set_extra_http_headers(
                    {
                        "user-agent": ua,
                        "accept-language": self._latest_chatgpt_accept_language(),
                    }
                )
            except Exception:
                pass
            if cookie_items:
                try:
                    page.context.add_cookies(cookie_items)
                except Exception as exc:
                    self._log(f"{label} 注入 cookie 失败: {exc}", "warning")
            page.goto(referer, wait_until="domcontentloaded", timeout=45000)
            # 页面内完成 jsd 后再发关键请求。
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    names = {c.get("name") for c in page.context.cookies("https://auth.openai.com")}
                    if "cf_clearance" in names:
                        break
                except Exception:
                    pass
                time.sleep(0.4)

            fetch_headers = {
                "accept": str(headers.get("accept") or "application/json"),
                "content-type": str(headers.get("content-type") or "application/json"),
                "origin": str(headers.get("origin") or "https://auth.openai.com"),
                "referer": referer,
                "user-agent": ua,
                "accept-language": self._latest_chatgpt_accept_language(),
            }
            for key in (
                "openai-sentinel-token",
                "openai-sentinel-so-token",
                "oai-device-id",
                "x-access-flow-invocation-id",
                "x-datadog-origin",
                "x-datadog-parent-id",
                "x-datadog-sampling-priority",
                "x-datadog-trace-id",
                "traceparent",
                "tracestate",
            ):
                if headers.get(key):
                    fetch_headers[key] = headers[key]

            self._log(
                f"[REG-DIAG][protocol-headless] {label} request "
                f"url=({self._diag_url_summary(url)}) referer=({self._diag_url_summary(referer)}) "
                f"headers=({self._diag_header_summary(fetch_headers)}) "
                f"body_len={len(body)} browser_cookies={len(page.context.cookies())}"
            )
            result = _browser_fetch(
                page,
                url,
                method="POST",
                headers=fetch_headers,
                body=body,
                redirect="follow",
                timeout_ms=45000,
            )
            try:
                # 关键 Auth 步骤只允许回填 CF cookie，防止 headless 导航重写 auth state。
                self._latest_chatgpt_import_browser_cookies(
                    page.context.cookies(),
                    only_names={"cf_clearance", "__cf_bm", "__cflb", "_cfuvid"},
                )
            except Exception:
                pass
            status = int(result.get("status") or 0)
            text = str(result.get("text") or "")
            data = result.get("data")
            if not isinstance(data, dict):
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {}
            self._log(f"{label} 无头浏览器执行状态: {status}")
            self._log(
                f"[REG-DIAG][protocol-headless] {label} response "
                f"status={status} {self._diag_payload_keys(data)} body_len={len(text)}"
            )
            return status, data if isinstance(data, dict) else {}, text

    def _latest_chatgpt_fetch_session_via_headless_callback(
        self,
        callback_url: str,
    ) -> tuple[int, int, dict, str, str, str] | None:
        """用浏览器完成 ChatGPT NextAuth callback，并回填 session cookie。"""
        if str(__import__("os").environ.get("PYTEST_CURRENT_TEST") or "").strip():
            return None
        try:
            from camoufox.sync_api import Camoufox
            from platforms.chatgpt.browser_register import _browser_fetch
        except Exception as exc:
            self._log(f"chatgpt_register callback 浏览器兜底不可用: {exc}", "warning")
            return None

        from .constants import CHATGPT_APP

        ua = self._latest_chatgpt_user_agent()
        cookie_items = self._latest_chatgpt_protocol_cookie_items()
        launch_opts = self._latest_chatgpt_camoufox_launch_opts()
        try:
            with Camoufox(**launch_opts) as browser:
                page = browser.new_page()
                try:
                    page.set_extra_http_headers(
                        {
                            "user-agent": ua,
                            "accept-language": self._latest_chatgpt_accept_language(),
                        }
                    )
                except Exception:
                    pass
                if cookie_items:
                    try:
                        page.context.add_cookies(cookie_items)
                    except Exception as exc:
                        self._log(f"chatgpt_register callback 浏览器注入 cookie 失败: {exc}", "warning")

                self._log(
                    "[REG-DIAG][protocol-headless] callback_fallback request "
                    f"url=({self._diag_url_summary(callback_url)}) "
                    f"ua={ua} accept_language={self._latest_chatgpt_accept_language()} "
                    f"injected_cookies={len(cookie_items)}"
                )
                callback_resp = page.goto(callback_url, wait_until="domcontentloaded", timeout=60000)
                callback_status = int(getattr(callback_resp, "status", 0) or 0)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                session_result = _browser_fetch(
                    page,
                    f"{CHATGPT_APP}/api/auth/session",
                    method="GET",
                    headers={"accept": "application/json"},
                    redirect="follow",
                    timeout_ms=30000,
                )
                session_status = int(session_result.get("status") or 0)
                session_data = session_result.get("data")
                if not isinstance(session_data, dict):
                    session_data = {}

                browser_cookies = []
                try:
                    browser_cookies = page.context.cookies()
                except Exception:
                    browser_cookies = []
                if browser_cookies:
                    self._latest_chatgpt_import_browser_cookies(browser_cookies)

                session_token = ""
                account_cookie = ""
                for cookie in browser_cookies:
                    name = str(cookie.get("name") or "")
                    if name == "__Secure-next-auth.session-token":
                        session_token = str(cookie.get("value") or "")
                    elif name == "_account":
                        account_cookie = str(cookie.get("value") or "")

                session_cookies_header = _cookies_to_header(self.session.cookies)
                self._log(
                    "chatgpt_register callback 浏览器兜底完成: "
                    f"callback_status={callback_status}, session_status={session_status}, cookies={len(browser_cookies)}"
                )
                self._log(
                    "[REG-DIAG][protocol-headless] callback_fallback response "
                    f"callback_status={callback_status} session_status={session_status} "
                    f"{self._diag_payload_keys(session_data)} "
                    f"session_token={self._diag_shape(session_token)} "
                    f"account_cookie={self._diag_shape(account_cookie)} "
                    f"browser_cookie_names={','.join(sorted({str(c.get('name') or '') for c in browser_cookies if c.get('name')})) if browser_cookies else '-'}"
                )
                return callback_status, session_status, session_data, session_token, account_cookie, session_cookies_header
        except Exception as exc:
            self._log(f"chatgpt_register callback 浏览器兜底失败: {exc}", "warning")
            return None

    def _latest_chatgpt_warmup_chatgpt_anon_session(self, device_id: str) -> None:
        """Replay headed-browser pre-auth warmup: accounts/check + prepare/finalize + cf_clearance."""
        from .constants import CHATGPT_APP

        if not self.session or not device_id:
            return
        try:
            obi = secrets.token_urlsafe(16)[:22]
            try:
                self.session.cookies.set("__obi", obi, domain="chatgpt.com", path="/")
            except Exception:
                self.session.cookies.set("__obi", obi)
            sync_path = "/backend-anon/bazaar/obi/sync-token"
            sync_headers = self._latest_chatgpt_chatgpt_client_headers(target_path=sync_path)
            sync_headers["content-type"] = "application/json"
            sync_headers["origin"] = CHATGPT_APP
            sync_resp = self.session.post(
                f"{CHATGPT_APP}{sync_path}",
                headers=sync_headers,
                data=json.dumps({"operation": "set", "obi": obi}, separators=(",", ":")),
                timeout=20,
            )
            sync_status = int(getattr(sync_resp, "status_code", 0) or 0)
            sync_data = self._response_json_dict(sync_resp)
            sync_token = str(sync_data.get("token") or "").strip()
            self._chatgpt_obi_sync_token = sync_token if sync_status == 200 else ""
            self._log(
                "[REG-DIAG][protocol] warmup bazaar/obi/sync-token "
                f"status={sync_status} token={self._diag_shape(sync_token)}"
            )
            if sync_status != 200 or not sync_token:
                self._delete_named_cookie("__obi")
                self._log(
                    "chatgpt_register OBI 同步未建立，已移除本地 __obi，后续请求不携带无效 OBI 状态",
                    "warning",
                )
        except Exception as exc:
            self._log(f"chatgpt_register 预热 bazaar/obi/sync-token 失败: {exc}", "warning")

        try:
            check_headers = self._latest_chatgpt_chatgpt_client_headers(
                target_path="/backend-anon/accounts/check/v4-2023-04-27",
            )
            check_resp = self.session.get(
                f"{CHATGPT_APP}/backend-anon/accounts/check/v4-2023-04-27",
                params={"timezone_offset_min": str(self.protocol_profile.timezone_offset_min)},
                headers=check_headers,
                timeout=20,
            )
            self._log(
                "[REG-DIAG][protocol] warmup accounts/check "
                f"status={getattr(check_resp, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(check_headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
        except Exception as exc:
            self._log(f"chatgpt_register 预热 accounts/check 失败: {exc}", "warning")

        try:
            me_headers = self._latest_chatgpt_chatgpt_client_headers(
                target_path="/backend-anon/me",
            )
            me_resp = self.session.get(
                f"{CHATGPT_APP}/backend-anon/me",
                headers=me_headers,
                timeout=20,
            )
            self._log(
                "[REG-DIAG][protocol] warmup backend-anon/me "
                f"status={getattr(me_resp, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(me_headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
        except Exception as exc:
            self._log(f"chatgpt_register 预热 backend-anon/me 失败: {exc}", "warning")

        try:
            for mode in ("basic", "plugins"):
                path = "/backend-anon/system_hints"
                headers = self._latest_chatgpt_chatgpt_client_headers(target_path=path)
                response = self.session.get(
                    f"{CHATGPT_APP}{path}",
                    params={"mode": mode, "suggestions": "true"},
                    headers=headers,
                    timeout=15,
                )
                self._log(
                    "[REG-DIAG][protocol] warmup system_hints "
                    f"mode={mode} status={getattr(response, 'status_code', 0)} "
                    f"headers=({self._diag_header_summary(headers)})"
                )

            models_path = "/backend-anon/models"
            models_headers = self._latest_chatgpt_chatgpt_client_headers(target_path=models_path)
            models_resp = self.session.get(
                f"{CHATGPT_APP}{models_path}",
                params={
                    "iim": "false",
                    "is_gizmo": "false",
                    "supports_model_picker_upgrade_presets": "true",
                },
                headers=models_headers,
                timeout=15,
            )
            self._log(
                "[REG-DIAG][protocol] warmup models "
                f"status={getattr(models_resp, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(models_headers)})"
            )

            init_path = "/backend-anon/conversation/init"
            init_headers = self._latest_chatgpt_chatgpt_client_headers(target_path=init_path)
            init_headers["content-type"] = "application/json"
            init_headers["origin"] = CHATGPT_APP
            init_resp = self.session.post(
                f"{CHATGPT_APP}{init_path}",
                headers=init_headers,
                data=json.dumps(
                    {
                        "requested_default_model": None,
                        "conversation_id": None,
                        "timezone": self.protocol_profile.timezone_name,
                        "timezone_offset_min": self.protocol_profile.timezone_offset_min,
                        "conversation_origin": None,
                    },
                    separators=(",", ":"),
                ),
                timeout=15,
            )
            self._log(
                "[REG-DIAG][protocol] warmup conversation/init "
                f"status={getattr(init_resp, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(init_headers)})"
            )

            pricing_path = f"/backend-anon/checkout_pricing_config/configs/{self.protocol_profile.pricing_country}"
            pricing_headers = self._latest_chatgpt_chatgpt_client_headers(target_path=pricing_path)
            pricing_resp = self.session.get(
                f"{CHATGPT_APP}{pricing_path}",
                headers=pricing_headers,
                timeout=15,
            )
            self._log(
                "[REG-DIAG][protocol] warmup checkout_pricing_config "
                f"status={getattr(pricing_resp, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(pricing_headers)})"
            )

            voices_path = "/backend-anon/settings/voices"
            voices_headers = self._latest_chatgpt_chatgpt_client_headers(target_path=voices_path)
            voices_resp = self.session.get(
                f"{CHATGPT_APP}{voices_path}",
                params={"voice_mode": "advanced"},
                headers=voices_headers,
                timeout=15,
            )
            self._log(
                "[REG-DIAG][protocol] warmup settings/voices "
                f"status={getattr(voices_resp, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(voices_headers)})"
            )
        except Exception as exc:
            self._log(f"chatgpt_register 预热补充 backend-anon 失败: {exc}", "warning")

        try:
            prepare_p = self._quickjs_chat_requirements_p(
                page_url=CHATGPT_APP,
                script_url=LATEST_CHATGPT_CHAT_REQUIREMENTS_SCRIPT_URL,
                label="预热 chat-requirements/prepare",
                runtime_key="chat_requirements_anon",
            )
            if not prepare_p:
                raise RuntimeError("实时 SDK 未生成预热 prepare p")
            prepare_headers = self._latest_chatgpt_chatgpt_client_headers(
                target_path="/backend-anon/sentinel/chat-requirements/prepare",
            )
            prepare_headers["content-type"] = "application/json"
            prepare_headers["origin"] = CHATGPT_APP
            self._log(
                "[REG-DIAG][protocol] warmup prepare request "
                f"p={self._diag_shape(prepare_p, prefix=8)} "
                f"{self._diag_sentinel_p_summary(prepare_p)} "
                f"headers=({self._diag_header_summary(prepare_headers)})"
            )
            resp = self.session.post(
                f"{CHATGPT_APP}/backend-anon/sentinel/chat-requirements/prepare",
                headers=prepare_headers,
                data=json.dumps({"p": prepare_p}, separators=(",", ":")),
                timeout=20,
            )
            status = getattr(resp, "status_code", 0)
            if status != 200:
                self._log(f"chatgpt_register 预热 chat-requirements/prepare 状态: {status}", "warning")
            else:
                self._log("chatgpt_register 预热 chat-requirements/prepare 完成")
                prepare_data = self._response_json_dict(resp)
                self._log(
                    "[REG-DIAG][protocol] warmup prepare response "
                    f"status={status} {self._diag_sentinel_challenge_summary(prepare_data)}"
                )
                for prepare_state, prepare_dispatch, prepare_source in (
                    ("none", "debounced", "composer_editor_state"),
                    ("sent", "immediate", "context_change"),
                ):
                    conversation_prepare_path = "/backend-anon/f/conversation/prepare"
                    conversation_prepare_headers = self._latest_chatgpt_chatgpt_client_headers(
                        target_path=conversation_prepare_path,
                    )
                    conversation_prepare_headers["content-type"] = "application/json"
                    conversation_prepare_headers["origin"] = CHATGPT_APP
                    conversation_prepare_body = {
                        "action": "next",
                        "parent_message_id": "client-created-root",
                        "model": "auto",
                        "client_prepare_state": prepare_state,
                        "client_prepare_dispatch": prepare_dispatch,
                        "client_prepare_source": prepare_source,
                        "timezone_offset_min": self.protocol_profile.timezone_offset_min,
                        "timezone": self.protocol_profile.timezone_name,
                        "conversation_mode": {"kind": "primary_assistant"},
                        "system_hints": [],
                        "model_response_contracts": [
                            {
                                "id": "photo_upload_action.v1",
                                "protocol_version": 1,
                                "presets": ["cap:image", "cap:file", "placement:end"],
                            }
                        ],
                        "supports_buffering": True,
                        "supported_encodings": ["v1"],
                        "client_contextual_info": {
                            "app_name": "chatgpt.com",
                            "has_web_push_capabilities": True,
                            "web_push_notification_permission": "default",
                        },
                    }
                    conversation_prepare_resp = self.session.post(
                        f"{CHATGPT_APP}{conversation_prepare_path}",
                        headers=conversation_prepare_headers,
                        data=json.dumps(conversation_prepare_body, separators=(",", ":")),
                        timeout=20,
                    )
                    self._log(
                        "[REG-DIAG][protocol] warmup conversation/prepare "
                        f"state={prepare_state} status={getattr(conversation_prepare_resp, 'status_code', 0)}"
                    )
                prepare_token = str(prepare_data.get("prepare_token") or "").strip()
                pow_meta = prepare_data.get("proofofwork") or {}
                turnstile = prepare_data.get("turnstile") or {}
                finalize_body: dict[str, Any] = {}
                if prepare_token:
                    finalize_body["prepare_token"] = prepare_token
                if (
                    prepare_token
                    and isinstance(pow_meta, dict)
                    and pow_meta.get("required")
                    and pow_meta.get("seed")
                    and str((turnstile or {}).get("dx") or "").strip()
                ):
                    solution = self._quickjs_chat_requirements_solution(
                        request_p=prepare_p,
                        challenge=prepare_data,
                        page_url=CHATGPT_APP,
                        script_url=LATEST_CHATGPT_CHAT_REQUIREMENTS_SCRIPT_URL,
                        label="预热 chat-requirements/finalize",
                        runtime_key="chat_requirements_anon",
                    )
                    if solution:
                        finalize_body["proofofwork"] = solution["final_p"]
                        finalize_body["turnstile"] = solution["t"]

                if prepare_token and finalize_body.get("proofofwork") and finalize_body.get("turnstile"):
                    finalize_headers = self._latest_chatgpt_chatgpt_client_headers(
                        target_path="/backend-anon/sentinel/chat-requirements/finalize",
                    )
                    finalize_headers["content-type"] = "application/json"
                    finalize_headers["origin"] = CHATGPT_APP
                    self._log(
                        "[REG-DIAG][protocol] warmup finalize request "
                        f"prepare_token={self._diag_shape(finalize_body.get('prepare_token'))} "
                        f"pow={self._diag_shape(finalize_body.get('proofofwork'), prefix=8)} "
                        f"turnstile={self._diag_shape(finalize_body.get('turnstile'))} "
                        f"headers=({self._diag_header_summary(finalize_headers)})"
                    )
                    finalize_resp = self.session.post(
                        f"{CHATGPT_APP}/backend-anon/sentinel/chat-requirements/finalize",
                        headers=finalize_headers,
                        data=json.dumps(finalize_body, separators=(",", ":")),
                        timeout=30,
                    )
                    finalize_status = getattr(finalize_resp, "status_code", 0)
                    if finalize_status == 200:
                        self._log("chatgpt_register 预热 chat-requirements/finalize 完成")
                    else:
                        self._log(
                            f"chatgpt_register 预热 chat-requirements/finalize 状态: {finalize_status}",
                            "warning",
                        )
        except Exception as exc:
            self._log(f"chatgpt_register 预热 chat-requirements/prepare 失败: {exc}", "warning")


    def _latest_chatgpt_init_email_oauth(self) -> tuple[bool, str]:
        """按 chatgpt_register 最新流程初始化邮箱注册，并记录 OpenAI 返回的下一步页面。"""
        from .constants import CHATGPT_APP

        if not self.session:
            return False, "session_not_initialized"
        if not self.email:
            return False, "email_not_initialized"

        otp_maybe_sent_at = time.time()
        self._otp_sent_at = None
        self._latest_chatgpt_init_final_url = ""
        try:
            home_resp = self.session.get(
                f"{CHATGPT_APP}/",
                headers=self._latest_chatgpt_nav_headers(sec_fetch_site="none"),
                timeout=20,
            )
            self._latest_chatgpt_update_client_build_from_html(
                str(getattr(home_resp, "text", "") or ""),
                source="init_home",
            )
            did = self._ensure_protocol_device_identity("latest_init_home")
            self._log(
                "[REG-DIAG][protocol] init home "
                f"status={getattr(home_resp, 'status_code', 0)} "
                f"device_id={self._diag_shape(did)} "
                f"oai_did_cookie={self._diag_shape(self._read_oai_did_cookie())} "
                f"cookies={self._diag_cookie_names_text()}"
            )

            csrf_headers = self._latest_chatgpt_browser_headers(
                accept="application/json",
                referer=f"{CHATGPT_APP}/",
                sec_fetch_dest="empty",
                sec_fetch_mode="cors",
                sec_fetch_site="same-origin",
            )
            csrf_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/csrf",
                headers=csrf_headers,
                timeout=20,
            )
            if getattr(csrf_resp, "status_code", 0) != 200:
                return False, f"csrf_http_{getattr(csrf_resp, 'status_code', 0)}"

            csrf_data = self._response_json_dict(csrf_resp)
            csrf_token = str(csrf_data.get("csrfToken") or "").strip()
            if not csrf_token:
                csrf_cookie = str(self.session.cookies.get("__Host-next-auth.csrf-token", "") or "")
                csrf_token = csrf_cookie.split("%7C")[0] if "%7C" in csrf_cookie else csrf_cookie.split("|")[0]
            if not csrf_token:
                return False, "csrf_token_missing"
            self._log(
                "[REG-DIAG][protocol] csrf "
                f"status={getattr(csrf_resp, 'status_code', 0)} "
                f"csrf={self._diag_shape(csrf_token)} "
                f"csrf_cookie={self._diag_shape(self.session.cookies.get('__Host-next-auth.csrf-token', ''))} "
                f"headers=({self._diag_header_summary(csrf_headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )

            # Headed-browser HAR warms chatgpt.com backend-anon after CSRF and before signin.
            self._latest_chatgpt_warmup_chatgpt_anon_session(did)

            query = urllib.parse.urlencode(
                {
                    "prompt": "login",
                    "ext-oai-did": did,
                    "auth_session_logging_id": self.protocol_fingerprint.auth_session_logging_id,
                    "screen_hint": "login_or_signup",
                    "login_hint": self.email,
                }
            )
            signin_headers = self._latest_chatgpt_browser_headers(
                accept="application/json",
                content_type="application/x-www-form-urlencoded",
                origin=CHATGPT_APP,
                referer=f"{CHATGPT_APP}/",
                sec_fetch_dest="empty",
                sec_fetch_mode="cors",
                sec_fetch_site="same-origin",
            )
            signin_resp = self.session.post(
                f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
                headers=signin_headers,
                data=urllib.parse.urlencode(
                    {
                        "callbackUrl": f"{CHATGPT_APP}/",
                        "csrfToken": csrf_token,
                        "json": "true",
                    }
                ),
                allow_redirects=False,
                timeout=20,
            )

            signin_data = self._response_json_dict(signin_resp)
            next_url = str(signin_data.get("url") or signin_resp.headers.get("Location") or "").strip()
            self._log(
                "[REG-DIAG][protocol] signin/openai "
                f"status={getattr(signin_resp, 'status_code', 0)} "
                f"query=({self._diag_url_summary(f'{CHATGPT_APP}/api/auth/signin/openai?{query}')}) "
                f"csrf={self._diag_shape(csrf_token)} "
                f"next=({self._diag_url_summary(next_url)}) "
                f"headers=({self._diag_header_summary(signin_headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
            if not next_url:
                init_error = self._latest_chatgpt_signin_no_authorize_error(signin_resp, signin_data)
                self._log(f"chatgpt_register signin/openai 未返回 authorize URL: {init_error}", "warning")
                return False, init_error

            final_resp = None
            current_url = next_url
            for redirect_index in range(15):
                final_resp = self.session.get(
                    current_url,
                    headers=self._latest_chatgpt_nav_headers(
                        referer=f"{CHATGPT_APP}/" if redirect_index == 0 else current_url,
                        sec_fetch_site="none" if redirect_index == 0 else "same-origin",
                    ),
                    allow_redirects=False,
                    timeout=30,
                )
                location = str(final_resp.headers.get("Location") or final_resp.headers.get("location") or "").strip()
                self._log(
                    f"chatgpt_register 初始化重定向 {redirect_index + 1}: "
                    f"status={getattr(final_resp, 'status_code', 0)} url={current_url}"
                )
                self._log(
                    "[REG-DIAG][protocol] init redirect "
                    f"idx={redirect_index + 1} status={getattr(final_resp, 'status_code', 0)} "
                    f"url=({self._diag_url_summary(current_url)}) "
                    f"location=({self._diag_url_summary(location)}) "
                    f"cookies={self._diag_cookie_names_text()}"
                )
                if not location:
                    break
                current_url = urllib.parse.urljoin(current_url, location)

            self._latest_chatgpt_init_final_url = current_url
            if "/email-verification" in current_url or "/email-otp" in current_url:
                self._email_otp_continue_url = current_url
                self._otp_sent_at = otp_maybe_sent_at

            self._log(f"chatgpt_register 初始化完成，device_id={did[:12]}...")
            self._log(
                "[REG-DIAG][protocol] init final "
                f"final_url=({self._diag_url_summary(current_url)}) "
                f"final_status={getattr(final_resp, 'status_code', 0) if final_resp is not None else 0} "
                f"otp_sent_at_set={self._diag_bool(self._otp_sent_at)} "
                f"cookies={self._diag_cookie_names_text()}"
            )
            if final_resp is not None:
                self._log(f"chatgpt_register 初始化最终状态: {getattr(final_resp, 'status_code', 0)}")
            if current_url:
                self._log(f"chatgpt_register 初始化最终页面: {current_url}")
            return True, ""
        except Exception as exc:
            return False, str(exc)


    def _latest_chatgpt_validate_email_otp(self, code: str) -> dict:
        headers = self._latest_chatgpt_json_headers(referer="https://auth.openai.com/email-verification")
        if self._device_id:
            otp_sentinel = self._check_sentinel(self._device_id, flow="email_otp_validate")
            if otp_sentinel:
                headers["openai-sentinel-token"] = self._sentinel_payload_header(otp_sentinel, self._device_id)
                if otp_sentinel.so_token:
                    headers["openai-sentinel-so-token"] = otp_sentinel.so_token
                self._log(
                    f"chatgpt_register OTP validate Sentinel 已附带: "
                    f"t_len={len(otp_sentinel.t)} so={'yes' if otp_sentinel.so_token else 'no'}"
                )
        body = json.dumps({"code": code}, separators=(",", ":"))
        self._log(
            "[REG-DIAG][protocol] otp_validate request "
            f"endpoint={OPENAI_API_ENDPOINTS['validate_otp']} "
            f"code_len={len(str(code or '').strip())} "
            f"referer=https://auth.openai.com/email-verification "
            f"device_id={self._diag_shape(self._device_id)} "
            f"headers=({self._diag_header_summary(headers)}) "
            f"body_len={len(body)} cookies={self._diag_cookie_names_text()}"
        )
        status = 0
        payload: dict = {}
        response_text = ""
        used_headless = False
        try:
            status, payload, response_text = self._latest_chatgpt_headless_auth_json(
                url=OPENAI_API_ENDPOINTS["validate_otp"],
                body=body,
                referer="https://auth.openai.com/email-verification",
                headers=headers,
                label="chatgpt_register OTP validate",
            )
            used_headless = True
            # headless 一旦 409/invalid_state/非 200，立刻回退协议，避免继续污染会话。
            if status != 200:
                self._log(
                    f"chatgpt_register OTP validate 无头浏览器返回 {status}/"
                    f"{self._openai_error_code_from_payload(payload) or 'non_200'}，回退协议请求",
                    "warning",
                )
                used_headless = False
        except Exception as exc:
            if "disabled" not in str(exc).lower():
                self._log(f"chatgpt_register OTP validate 无头浏览器执行失败，回退协议请求: {exc}", "warning")
            used_headless = False

        if not used_headless:
            response = self.session.post(OPENAI_API_ENDPOINTS["validate_otp"], headers=headers, data=body, timeout=30)
            status = int(getattr(response, "status_code", 0) or 0)
            payload = self._response_json_dict(response)
            response_text = str(getattr(response, "text", "") or "")

        class _Resp:
            def __init__(self, status_code: int, text: str, data: dict):
                self.status_code = status_code
                self.text = text
                self._data = data

            def json(self):
                return self._data

        response = _Resp(status, response_text, payload)
        self._otp_page_type = str(((payload.get("page") or {}).get("type")) or "")
        self._otp_continue_url = str(payload.get("continue_url") or "").strip()
        self._log(
            f"chatgpt_register OTP validate 状态: {status} "
            f"page_type={self._otp_page_type or '(empty)'} continue_url={self._otp_continue_url or '(empty)'}"
            f"{' [headless]' if used_headless else ''}"
        )
        self._log(
            "[REG-DIAG][protocol] otp_validate response "
            f"status={status} used_headless={self._diag_bool(used_headless)} "
            f"{self._diag_payload_keys(payload)} "
            f"body_len={len(response_text)} cookies={self._diag_cookie_names_text()}"
        )
        if status != 200:
            error_code = self._openai_error_code_from_payload(payload)
            if error_code in {"account_deactivated", "wrong_email_otp_code", "invalid_state"}:
                raise RuntimeError(error_code)
            if self._device_id:
                ca_sentinel = self._check_sentinel(self._device_id, flow="email_otp_validate")
                if ca_sentinel:
                    retry_headers = dict(headers)
                    retry_headers["openai-sentinel-token"] = self._sentinel_payload_header(ca_sentinel, self._device_id)
                    self._log("chatgpt_register OTP validate 首次失败，补 email_otp_validate Sentinel 后重试", "warning")
                    self._log(
                        "[REG-DIAG][protocol] otp_validate retry request "
                        f"headers=({self._diag_header_summary(retry_headers)})",
                        "warning",
                    )
                    response = self.session.post(
                        OPENAI_API_ENDPOINTS["validate_otp"],
                        headers=retry_headers,
                        data=body,
                        timeout=30,
                    )
                    payload = self._response_json_dict(response)
                    self._otp_page_type = str(((payload.get("page") or {}).get("type")) or "")
                    self._otp_continue_url = str(payload.get("continue_url") or "").strip()
                    self._log(
                        f"chatgpt_register OTP validate Sentinel 重试状态: {getattr(response, 'status_code', 0)} "
                        f"page_type={self._otp_page_type or '(empty)'} continue_url={self._otp_continue_url or '(empty)'}"
                    )
                    self._log(
                        "[REG-DIAG][protocol] otp_validate retry response "
                        f"status={getattr(response, 'status_code', 0)} "
                        f"{self._diag_payload_keys(payload)} "
                        f"cookies={self._diag_cookie_names_text()}",
                        "warning" if getattr(response, "status_code", 0) != 200 else "info",
                    )
                    if getattr(response, "status_code", 0) == 200:
                        error_code = self._openai_error_code_from_payload(payload)
                        if error_code:
                            raise RuntimeError(error_code)
                        return payload
                    error_code = self._openai_error_code_from_payload(payload)
            raise RuntimeError(error_code or f"email_otp_validate_http_{getattr(response, 'status_code', 0)}")
        error_code = self._openai_error_code_from_payload(payload)
        if error_code:
            raise RuntimeError(error_code)
        return payload


    def _latest_chatgpt_refresh_email_otp_after_invalid_state(self) -> Optional[str]:
        """最新版注册链 OTP state 失效后，仅刷新当前邮箱的 OAuth/OTP 会话。"""
        self._log("chatgpt_register OTP validate 返回 invalid_state，刷新当前邮箱 OTP 会话后重试一次...", "warning")
        self._refresh_mailbox_before_ids()
        self._reset_latest_chatgpt_session_for_retry()
        init_ok, init_error = self._latest_chatgpt_init_email_oauth()
        if not init_ok:
            raise RuntimeError(f"invalid_state_retry_init_failed: {init_error}")
        init_final_url = str(getattr(self, "_latest_chatgpt_init_final_url", "") or "")
        if "/create-account/password" in init_final_url:
            password_ok, _registered_password = self._register_password()
            if not password_ok:
                raise RuntimeError("invalid_state_retry_password_failed")
        elif init_final_url and "/email-verification" not in init_final_url and "/email-otp" not in init_final_url:
            raise RuntimeError(f"invalid_state_retry_unexpected_step: {init_final_url}")
        if not self._send_verification_code():
            raise RuntimeError("invalid_state_retry_send_otp_failed")
        code = self._get_verification_code(mark_invalid_on_timeout=False, resend_on_timeout=False)
        if not code:
            raise RuntimeError("invalid_state_retry_no_otp")
        return code

    def _latest_chatgpt_resend_email_otp_after_rejected_code(self, reason: str) -> Optional[str]:
        """验证码被服务端拒绝后，刷新邮箱基线、重发并只读取新验证码。"""
        self._log(f"chatgpt_register OTP validate 返回 {reason}，刷新邮箱基线后重发验证码...", "warning")
        self._refresh_mailbox_before_ids()
        if not self._send_verification_code():
            raise RuntimeError(f"{reason}_retry_send_otp_failed")
        code = self._get_verification_code(mark_invalid_on_timeout=False, resend_on_timeout=False)
        if not code:
            raise RuntimeError(f"{reason}_retry_no_otp")
        return code


    def _latest_chatgpt_send_login_email_otp(self, *, referer: str) -> dict:
        """按最新 auth.openai.com XHR 请求显式触发登录邮箱 OTP。"""
        headers = self._latest_chatgpt_json_headers(referer=referer)
        response = self.session.get(
            OPENAI_API_ENDPOINTS["send_otp"],
            headers=headers,
            allow_redirects=False,
            timeout=30,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        payload = self._response_json_dict(response)
        response_text = str(getattr(response, "text", "") or "")
        self._log(
            "[REG-DIAG][refresh-session][protocol] email_otp/send "
            f"status={status} referer={referer} "
            f"headers=({self._diag_header_summary(headers)}) "
            f"{self._diag_payload_keys(payload)} "
            f"body_len={len(response_text)} cookies={self._diag_cookie_names_text()}"
        )
        if status not in (200, 302):
            raise RuntimeError(f"email_otp_send_http_{status}: {response_text[:240]}")
        self._otp_sent_at = time.time()
        self._email_otp_continue_url = str(payload.get("continue_url") or "https://auth.openai.com/email-verification")
        self._email_otp_page_loaded = True
        self._otp_page_type = str(((payload.get("page") or {}).get("type")) or "email_otp_verification")
        return payload


    def _latest_chatgpt_send_passwordless_login_otp(self) -> dict:
        """登录密码页优先走 passwordless OTP，保持与有头浏览器“使用邮箱验证码登录”一致。"""
        from .constants import OPENAI_AUTH

        referer = f"{OPENAI_AUTH}/log-in/password"
        headers = self._latest_chatgpt_json_headers(referer=referer)
        response = self.session.post(
            f"{OPENAI_AUTH}/api/accounts/passwordless/send-otp",
            headers=headers,
            data="",
            allow_redirects=False,
            timeout=30,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        payload = self._response_json_dict(response)
        response_text = str(getattr(response, "text", "") or "")
        self._log(
            "[REG-DIAG][refresh-session][protocol] passwordless/send-otp "
            f"status={status} headers=({self._diag_header_summary(headers)}) "
            f"{self._diag_payload_keys(payload)} "
            f"body_len={len(response_text)} cookies={self._diag_cookie_names_text()}"
        )
        if status != 200:
            raise RuntimeError(f"passwordless_send_otp_http_{status}: {response_text[:240]}")
        self._otp_sent_at = time.time()
        self._email_otp_continue_url = str(payload.get("continue_url") or "https://auth.openai.com/email-verification")
        self._email_otp_page_loaded = True
        self._otp_page_type = str(((payload.get("page") or {}).get("type")) or "email_otp_verification")
        return payload


    def _latest_chatgpt_verify_login_password(self) -> dict:
        """passwordless 不可用时，按最新 Sentinel token 提交已保存的登录密码。"""
        from .constants import OPENAI_AUTH

        if not self.password:
            raise RuntimeError("login_password_missing")
        referer = f"{OPENAI_AUTH}/log-in/password"
        headers = self._latest_chatgpt_json_headers(referer=referer)
        did = str(self._device_id or self._read_oai_did_cookie() or "").strip()
        if did:
            password_sentinel = self._check_sentinel(did, flow="password_verify")
            if password_sentinel:
                headers["openai-sentinel-token"] = self._sentinel_payload_header(password_sentinel, did)
                if password_sentinel.so_token:
                    headers["openai-sentinel-so-token"] = password_sentinel.so_token
                self._log(
                    f"重新登录密码 Sentinel 已获取: flow={password_sentinel.flow} "
                    f"t_len={len(password_sentinel.t)} so={'yes' if password_sentinel.so_token else 'no'}"
                )
        body = json.dumps({"password": self.password}, separators=(",", ":"))
        response = self.session.post(
            f"{OPENAI_AUTH}/api/accounts/password/verify",
            headers=headers,
            data=body,
            allow_redirects=False,
            timeout=30,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        payload = self._response_json_dict(response)
        response_text = str(getattr(response, "text", "") or "")
        self._log(
            "[REG-DIAG][refresh-session][protocol] password/verify "
            f"status={status} headers=({self._diag_header_summary(headers)}) "
            f"{self._diag_payload_keys(payload)} "
            f"body_len={len(response_text)} cookies={self._diag_cookie_names_text()}"
        )
        if status != 200:
            raise RuntimeError(f"password_verify_http_{status}: {response_text[:240]}")
        page_type = str(((payload.get("page") or {}).get("type")) or "")
        continue_url = str(payload.get("continue_url") or "").strip()
        if page_type in {"email_otp_send", "email_otp_verification"} or "email-verification" in continue_url:
            self._email_otp_continue_url = continue_url or "https://auth.openai.com/email-verification"
            self._email_otp_page_loaded = True
            self._otp_page_type = page_type or "email_otp_verification"
        return payload


    def _latest_chatgpt_prepare_refresh_session_login_step(self) -> dict:
        """把最新 NextAuth 登录初始化后的页面推进到 callback 或 email OTP 可提交状态。"""
        final_url = str(getattr(self, "_latest_chatgpt_init_final_url", "") or "").strip()
        final_payload = {"continue_url": final_url, "page": {"type": ""}}
        if self._chatgpt_callback_url_from_payload(final_payload):
            return final_payload
        if "/email-verification" in final_url or "/email-otp" in final_url:
            self._email_otp_continue_url = final_url
            self._email_otp_page_loaded = True
            if not self._otp_sent_at:
                self._otp_sent_at = time.time()
            return {"continue_url": final_url, "page": {"type": "email_otp_verification"}}
        if "/log-in/password" in final_url:
            if (
                bool(getattr(self, "prefer_password_totp_login", False))
                and self.password
                and str(getattr(self, "totp_secret", "") or "").strip()
            ):
                self._log("重新登录检测到已保存密码和 2FA，直接使用密码 + TOTP 登录，不触发邮箱验证码")
                return self._latest_chatgpt_verify_login_password()
            try:
                payload = self._latest_chatgpt_send_passwordless_login_otp()
                self._log("重新登录密码页已切换为邮箱验证码登录")
                return payload
            except Exception as exc:
                self._log(f"重新登录 passwordless OTP 不可用，改用密码校验: {exc}", "warning")
                payload = self._latest_chatgpt_verify_login_password()
                page_type = str(((payload.get("page") or {}).get("type")) or "")
                if page_type == "email_otp_send":
                    return self._latest_chatgpt_send_login_email_otp(referer="https://auth.openai.com/email-verification")
                if page_type == "email_otp_verification" and not self._otp_sent_at:
                    self._otp_sent_at = time.time()
                return payload
        return final_payload


    def _latest_chatgpt_refresh_session_validate_otp_with_retry(self) -> dict:
        """重新登录专用 OTP 等待与校验，复用最新 email_otp_validate Sentinel 计算。"""
        self._log("重新登录: 等待邮箱验证码...")
        code = self._get_verification_code(mark_invalid_on_timeout=False, resend_on_timeout=False)
        if not code:
            raise RuntimeError(self._email_otp_failure_message("重新登录验证码获取失败"))

        last_error = ""
        for attempt in range(1, 4):
            try:
                if attempt > 1:
                    self._log(f"重新登录: 重新提交邮箱验证码 ({attempt}/3)...")
                return self._latest_chatgpt_validate_email_otp(code)
            except RuntimeError as exc:
                last_error = str(exc)
                if last_error in {"invalid_state", "wrong_email_otp_code"} and attempt < 3:
                    self._log(f"重新登录 OTP 返回 {last_error}，刷新登录会话并读取新验证码", "warning")
                    self._refresh_mailbox_before_ids()
                    self._reset_latest_chatgpt_session_for_retry()
                    init_ok, init_error = self._latest_chatgpt_init_email_oauth()
                    if not init_ok:
                        raise RuntimeError(f"refresh_session_retry_init_failed: {init_error}")
                    login_payload = self._latest_chatgpt_prepare_refresh_session_login_step()
                    if self._chatgpt_callback_url_from_payload(login_payload):
                        return login_payload
                    code = self._get_verification_code(mark_invalid_on_timeout=False, resend_on_timeout=False)
                    if not code:
                        raise RuntimeError("refresh_session_retry_no_otp")
                    continue
                raise
        raise RuntimeError(last_error or "refresh_session_email_otp_validate_failed")


    @staticmethod
    def _latest_chatgpt_payload_page_type(payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        return str(page.get("type") or "").strip()


    @staticmethod
    def _latest_chatgpt_payload_continue_url(payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("continue_url") or "").strip()


    def _latest_chatgpt_extract_mfa_challenge(self, payload: dict) -> tuple[str, str, str, int]:
        """从 auth 返回的 mfa_challenge payload 中提取当前因子。"""
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        continue_url = self._latest_chatgpt_payload_continue_url(payload)
        factor_id = str(page_payload.get("factor_id") or page_payload.get("id") or "").strip()
        factor_type = str(page_payload.get("factor_type") or page_payload.get("type") or "").strip()

        factors: list[dict] = []
        for key in ("factors", "mfa_factors", "available_factors"):
            raw = page_payload.get(key)
            if isinstance(raw, list):
                factors.extend([item for item in raw if isinstance(item, dict)])

        if not factor_id and continue_url:
            try:
                parsed = urllib.parse.urlsplit(continue_url)
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[-2] == "mfa-challenge":
                    factor_id = parts[-1]
            except Exception:
                factor_id = ""

        for item in factors:
            item_id = str(item.get("id") or "").strip()
            if factor_id and item_id != factor_id:
                continue
            item_type = str(item.get("factor_type") or item.get("type") or "").strip()
            if item_type:
                factor_type = factor_type or item_type
            if item_id:
                factor_id = factor_id or item_id
            if factor_id and factor_type:
                break

        if not factor_id:
            totp_factors = [
                item
                for item in factors
                if str(item.get("factor_type") or item.get("type") or "").strip() == "totp"
                and str(item.get("id") or "").strip()
            ]
            if len(totp_factors) == 1:
                factor_id = str(totp_factors[0].get("id") or "").strip()
                factor_type = "totp"

        return factor_id, (factor_type or "totp").lower(), continue_url, len(factors)


    def _latest_chatgpt_complete_mfa_challenge(self, payload: dict) -> dict:
        """已绑定 2FA 的账号在重登 OTP 后继续提交 TOTP，并返回下一步 payload。"""
        from .constants import OPENAI_AUTH
        from .mfa import generate_totp_code

        page_type = self._latest_chatgpt_payload_page_type(payload)
        continue_url = self._latest_chatgpt_payload_continue_url(payload)
        if page_type != "mfa_challenge" and "/mfa-challenge" not in continue_url:
            return payload

        secret = str(getattr(self, "totp_secret", "") or "").strip()
        if not secret:
            raise RuntimeError("重新登录需要 2FA 验证码但本地未保存 totp_secret")

        factor_id, factor_type, challenge_url, factor_count = self._latest_chatgpt_extract_mfa_challenge(payload)
        if not factor_id:
            raise RuntimeError("重新登录 2FA challenge 缺少 factor_id")
        if factor_type != "totp":
            raise RuntimeError(f"重新登录 2FA 当前因子不是 TOTP: {factor_type}")

        challenge_url = urllib.parse.urljoin(OPENAI_AUTH, challenge_url or f"{OPENAI_AUTH}/mfa-challenge/{factor_id}")
        self._log(
            "[REG-DIAG][refresh-session][protocol] mfa_challenge "
            f"page_type={page_type or '(empty)'} factor_id={self._diag_shape(factor_id)} "
            f"factor_type={factor_type} factors={factor_count} url=({self._diag_url_summary(challenge_url)})"
        )
        try:
            page_resp = self.session.get(
                challenge_url,
                headers=self._latest_chatgpt_nav_headers(referer="https://auth.openai.com/email-verification"),
                timeout=30,
            )
            page_status = int(getattr(page_resp, "status_code", 0) or 0)
            page_text = str(getattr(page_resp, "text", "") or "")
            self._latest_chatgpt_update_client_build_from_html(page_text, source="mfa_challenge")
            self._log(
                "[REG-DIAG][refresh-session][protocol] mfa_challenge page "
                f"status={page_status} final_url=({self._diag_url_summary(getattr(page_resp, 'url', '') or challenge_url)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
        except Exception as exc:
            self._log(f"重新登录 2FA challenge 页面预热失败，继续提交 TOTP: {exc}", "warning")

        code = generate_totp_code(secret)
        headers = self._latest_chatgpt_json_headers(referer=challenge_url)
        body = json.dumps({"id": factor_id, "type": "totp", "code": code}, separators=(",", ":"))
        self._log(
            "[REG-DIAG][refresh-session][protocol] mfa_verify request "
            f"endpoint={OPENAI_AUTH}/api/accounts/mfa/verify "
            f"code_len={len(code)} factor_id={self._diag_shape(factor_id)} "
            f"headers=({self._diag_header_summary(headers)}) "
            f"body_len={len(body)} cookies={self._diag_cookie_names_text()}"
        )
        response = self.session.post(
            f"{OPENAI_AUTH}/api/accounts/mfa/verify",
            headers=headers,
            data=body,
            timeout=30,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        next_payload = self._response_json_dict(response)
        response_text = str(getattr(response, "text", "") or "")
        next_page_type = self._latest_chatgpt_payload_page_type(next_payload)
        next_continue_url = self._latest_chatgpt_payload_continue_url(next_payload)
        self._otp_page_type = next_page_type
        self._otp_continue_url = next_continue_url
        self._log(
            f"重新登录 2FA TOTP 提交状态: {status} "
            f"page_type={next_page_type or '(empty)'} continue_url={next_continue_url or '(empty)'}"
        )
        self._log(
            "[REG-DIAG][refresh-session][protocol] mfa_verify response "
            f"status={status} {self._diag_payload_keys(next_payload)} "
            f"body_len={len(response_text)} cookies={self._diag_cookie_names_text()}"
        )
        if status != 200:
            error_code = self._openai_error_code_from_payload(next_payload)
            raise RuntimeError(error_code or f"mfa_verify_http_{status}: {response_text[:240]}")
        error_code = self._openai_error_code_from_payload(next_payload)
        if error_code:
            raise RuntimeError(error_code)
        return next_payload


    def run_chatgpt_refresh_session_latest(
        self,
        result: Optional[RegistrationResult] = None,
        *,
        session_source: str = "latest_refresh_session_login",
    ) -> RegistrationResult:
        """按最新 chatgpt.com NextAuth + Sentinel 链路重新登录并获取 session/at。"""
        result = result or RegistrationResult(success=False, logs=self.logs)
        previous_set_password_after_register = bool(getattr(self, "set_password_after_register", False))
        self.set_password_after_register = False
        try:
            self._log("=" * 60)
            self._log("开始重新登录流程（chatgpt_register 最新协议链路）")
            self._log("=" * 60)
            if not self.email:
                result.error_message = "重新登录缺少邮箱"
                return result
            result.email = self.email
            result.password = self.password or ""

            if not self.email_info and self.email_service is not None:
                try:
                    email_info = self.email_service.create_email()
                    if isinstance(email_info, dict):
                        self.email_info = email_info
                except Exception as exc:
                    self._log(f"重新登录邮箱资源预处理失败，继续使用当前邮箱: {exc}", "warning")

            if not self._init_latest_chatgpt_session():
                result.error_message = "重新登录初始化会话失败"
                return result

            self._is_existing_account = True
            self._latest_chatgpt_session_source = session_source
            self._refresh_mailbox_before_ids()
            self._log("重新登录: 使用最新 chatgpt.com login_hint 初始化 OAuth 登录...")

            init_error = ""
            for init_attempt in range(1, 4):
                if init_attempt > 1:
                    self._log(f"重新登录初始化失败，重试 ({init_attempt}/3)...", "warning")
                    self._refresh_mailbox_before_ids()
                    self._reset_latest_chatgpt_session_for_retry()
                init_ok, init_error = self._latest_chatgpt_init_email_oauth()
                if init_ok:
                    break
                if not self._is_latest_chatgpt_init_retryable_error(init_error) or init_attempt >= 3:
                    result.error_message = f"重新登录初始化失败: {init_error}"
                    return result
                self._log(f"重新登录初始化失败: {init_error}", "warning")
                time.sleep(2)

            login_payload = self._latest_chatgpt_prepare_refresh_session_login_step()
            callback_url = self._chatgpt_callback_url_from_payload(login_payload)
            if callback_url:
                self._create_account_continue_url = callback_url
                self._log("重新登录初始化已返回 ChatGPT callback，直接获取 session/at")
                return self._latest_chatgpt_fetch_session_result(result)

            if (
                self._latest_chatgpt_payload_page_type(login_payload) == "mfa_challenge"
                or "/mfa-challenge" in self._latest_chatgpt_payload_continue_url(login_payload)
            ):
                self._log("重新登录密码校验后进入 2FA challenge，自动提交本地 TOTP")
                login_payload = self._latest_chatgpt_complete_mfa_challenge(login_payload)
                callback_url = self._chatgpt_callback_url_from_payload(login_payload)
                if not callback_url:
                    result.error_message = "重新登录 2FA 通过但未返回 ChatGPT callback URL"
                    return result
                self._create_account_continue_url = callback_url
                self._log("重新登录密码 + 2FA 校验通过，开始获取 ChatGPT session/at")
                return self._latest_chatgpt_fetch_session_result(result)

            page_type = str(((login_payload.get("page") or {}).get("type")) or "")
            continue_url = str(login_payload.get("continue_url") or "").strip()
            is_email_otp_step = (
                page_type in {"email_otp_send", "email_otp_verification"}
                or "/email-verification" in continue_url
                or "/email-otp" in continue_url
            )
            if not is_email_otp_step:
                if bool(getattr(self, "prefer_password_totp_login", False)):
                    result.error_message = (
                        "重新登录密码+2FA链路未进入 2FA、邮箱验证码或 callback 步骤: "
                        f"page={page_type or '(empty)'} url={continue_url or getattr(self, '_latest_chatgpt_init_final_url', '')}"
                    )
                else:
                    result.error_message = (
                        "重新登录未进入邮箱验证码或 callback 步骤: "
                        f"page={page_type or '(empty)'} url={continue_url or getattr(self, '_latest_chatgpt_init_final_url', '')}"
                    )
                return result

            validate_payload = self._latest_chatgpt_refresh_session_validate_otp_with_retry()
            callback_url = self._chatgpt_callback_url_from_payload(validate_payload)
            if (
                not callback_url
                and (
                    self._latest_chatgpt_payload_page_type(validate_payload) == "mfa_challenge"
                    or "/mfa-challenge" in self._latest_chatgpt_payload_continue_url(validate_payload)
                )
            ):
                self._log("重新登录 OTP 后进入 2FA challenge，自动提交本地 TOTP")
                validate_payload = self._latest_chatgpt_complete_mfa_challenge(validate_payload)
                callback_url = self._chatgpt_callback_url_from_payload(validate_payload)
            if not callback_url:
                result.error_message = "重新登录 OTP 通过但未返回 ChatGPT callback URL"
                return result
            self._create_account_continue_url = callback_url
            self._log("重新登录 OTP 校验通过，开始获取 ChatGPT session/at")
            return self._latest_chatgpt_fetch_session_result(result)
        except Exception as exc:
            self._log(f"重新登录最新协议流程异常: {exc}", "error")
            result.error_message = str(exc)
            return result
        finally:
            self.set_password_after_register = previous_set_password_after_register


    def _latest_chatgpt_open_about_you(self, url: str) -> bool:
        """保持 OTP 响应中的 SPA 状态，不额外请求 /about-you 页面。"""
        self._last_about_you_error = ""
        if not url:
            self._last_about_you_error = "missing_continue_url"
            self._log("chatgpt_register OTP validate 未返回 about-you continue_url", "warning")
            return False
        target = urllib.parse.urljoin("https://auth.openai.com/", url)
        if urllib.parse.urlparse(target).path.rstrip("/") != "/about-you":
            self._last_about_you_error = f"unexpected_continue_url:{target}"
            self._log(f"chatgpt_register OTP 后页面不是 about-you: {target}", "warning")
            return False
        self._email_otp_continue_url = target
        self._log("chatgpt_register OTP 后沿用前端 about-you 状态，跳过额外页面 GET")
        return True


    def _latest_chatgpt_create_user_account(self, user_info: dict | None = None) -> bool:
        """按 chatgpt_register 最新链路创建账号资料，不插入旧状态推进请求。"""
        try:
            self._last_create_account_error_code = ""
            self._last_create_account_transport_error = ""
            if user_info is None:
                user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            headers = self._latest_chatgpt_json_headers(referer="https://auth.openai.com/about-you")
            if self._device_id:
                ca_sentinel = self._check_sentinel(self._device_id, flow="oauth_create_account")
                if ca_sentinel:
                    headers["openai-sentinel-token"] = self._sentinel_payload_header(ca_sentinel, self._device_id)
                    if ca_sentinel.so_token:
                        headers["openai-sentinel-so-token"] = ca_sentinel.so_token
                    self._log(
                        f"create_account Sentinel 已获取: "
                        f"flow={ca_sentinel.flow} t_len={len(ca_sentinel.t)} "
                        f"so={'yes' if ca_sentinel.so_token else 'no'}"
                    )
            body = json.dumps(user_info, separators=(",", ":"))
            self._log(
                "[REG-DIAG][protocol] create_account request "
                f"endpoint={OPENAI_API_ENDPOINTS['create_account']} "
                f"referer=https://auth.openai.com/about-you "
                f"device_id={self._diag_shape(self._device_id)} "
                f"body_len={len(body)} body_keys={','.join(sorted(user_info.keys()))} "
                f"headers=({self._diag_header_summary(headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
            status = 0
            payload: dict = {}
            response_text = ""
            used_headless = False
            # 默认协议直发；仅显式启用 headless auth 时尝试浏览器同源 fetch。
            try:
                status, payload, response_text = self._latest_chatgpt_headless_auth_json(
                    url=OPENAI_API_ENDPOINTS["create_account"],
                    body=body,
                    referer="https://auth.openai.com/about-you",
                    headers=headers,
                    label="create_account",
                )
                used_headless = True
            except Exception as exc:
                # 默认关闭 headless auth 时静默走协议，避免每个账号都刷“disabled”日志。
                if "disabled" not in str(exc).lower():
                    self._log(f"create_account 无头浏览器执行失败，回退协议请求: {exc}", "warning")
                used_headless = False

            if used_headless:
                invalid_state = (
                    status != 200
                    and (
                        self._openai_error_code_from_payload(payload) == "invalid_state"
                        or "invalid_state" in response_text
                        or "no longer valid" in response_text
                        or status in (401, 403, 409)
                    )
                )
                if invalid_state:
                    self._log(
                        f"create_account 无头浏览器返回 {status}/"
                        f"{self._openai_error_code_from_payload(payload) or 'non_200'}，回退协议请求",
                        "warning",
                    )
                    used_headless = False

            if not used_headless:
                response = self.session.post(
                    OPENAI_API_ENDPOINTS["create_account"],
                    headers=headers,
                    data=body,
                    timeout=30,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                payload = self._response_json_dict(response)
                response_text = str(getattr(response, "text", "") or "")

            class _Resp:
                def __init__(self, status_code: int, text: str, data: dict):
                    self.status_code = status_code
                    self.text = text
                    self._data = data

                def json(self):
                    return self._data

            response = _Resp(status, response_text, payload)
            self._log(f"账户创建状态: {status}{' [headless]' if used_headless else ''}")
            self._log(
                "[REG-DIAG][protocol] create_account response "
                f"status={status} used_headless={self._diag_bool(used_headless)} "
                f"{self._diag_payload_keys(payload)} "
                f"body_len={len(response_text)} cookies={self._diag_cookie_names_text()}"
            )
            if status != 200:
                self._log(f"账户创建失败: {response_text}", "warning")
                self._last_create_account_error_code = self._openai_error_code_from_payload(payload)
                if self._is_deleted_or_deactivated_account_response(response):
                    self._last_create_account_error_code = "account_deactivated"
                    self._log("OpenAI 判定该邮箱关联账号已删除或停用，将改走已注册账号登录流程", "warning")
                if self._is_user_already_exists_response(response):
                    self._log("OpenAI 返回 user_already_exists，父邮箱别名配额已耗尽，标记父邮箱为别名已上限", "warning")
                    self._mark_parent_email_exhausted("openai_user_already_exists")
                    self._user_already_exists = True
                return False
            self._create_account_continue_url = str(payload.get("continue_url") or "")
            if self._create_account_continue_url:
                self._log(f"create_account continue_url: {self._create_account_continue_url}")
            return True
        except Exception as exc:
            self._last_create_account_transport_error = str(exc)
            self._log(f"创建账户失败: {exc}", "error")
            return False


    def _latest_chatgpt_create_account_with_retry(self) -> bool:
        user_info = generate_random_user_info()
        for attempt in range(1, 4):
            self._last_create_account_error_code = ""
            self._last_create_account_transport_error = ""
            if self._latest_chatgpt_create_user_account(user_info=user_info):
                return True
            transport_error = str(getattr(self, "_last_create_account_transport_error", "") or "")
            if transport_error and self._is_chatgpt_transport_retryable_error(transport_error) and attempt < 3:
                self._log(f"create_account 网络/TLS失败，重试 ({attempt + 1}/3)...", "warning")
                time.sleep(2)
                continue
            error_code = str(getattr(self, "_last_create_account_error_code", "") or "")
            if error_code == "registration_disallowed":
                self._log("registration_disallowed，OpenAI 已明确拒绝当前邮箱，停止重试并标记父邮箱子号耗尽", "warning")
                self._mark_parent_email_exhausted("registration_disallowed")
            return False
        return False


    @staticmethod
    def _extract_chatgpt_session_access_token_from_html(html: str) -> str:
        text = str(html or "")
        match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1).strip()
        return ""


    def _latest_chatgpt_warmup_authenticated_session(self, access_token: str) -> bool:
        from .constants import CHATGPT_APP

        token = str(access_token or "").strip()
        if not token or not self.session or not self._device_id:
            return False

        completed = False
        self._log(
            "[REG-DIAG][session][protocol] authenticated_warmup start "
            f"access_token={self._diag_shape(token)} device_id={self._diag_shape(self._device_id)} "
            f"cookies={self._diag_cookie_names_text()}"
        )
        for path in (
            "/backend-api/user_granular_consent",
            "/backend-api/accounts/optimized/check",
            "/backend-api/me",
            "/backend-api/accounts/check/v4-2023-04-27",
            "/backend-api/settings/user",
        ):
            try:
                headers = self._latest_chatgpt_chatgpt_client_headers(target_path=path)
                headers["authorization"] = f"Bearer {token}"
                response = self.session.get(
                    f"{CHATGPT_APP}{path}",
                    params={"timezone_offset_min": str(self.protocol_profile.timezone_offset_min)} if path.endswith("/accounts/check/v4-2023-04-27") else None,
                    headers=headers,
                    timeout=20,
                )
                self._log(
                    f"chatgpt_register 注册后 {path} 状态: {getattr(response, 'status_code', 0)}"
                )
                self._log(
                    "[REG-DIAG][session][protocol] authenticated_warmup response "
                    f"path={path} status={getattr(response, 'status_code', 0)} "
                    f"headers=({self._diag_header_summary(headers)}) "
                    f"cookies={self._diag_cookie_names_text()}"
                )
                completed = True
            except Exception as exc:
                self._log(f"chatgpt_register 注册后 {path} 失败: {exc}", "warning")

        try:
            prepare_p = self._quickjs_chat_requirements_p(
                page_url=CHATGPT_APP,
                script_url=LATEST_CHATGPT_CF_JSD_SCRIPT_URL,
                label="authenticated_prepare",
                runtime_key="chat_requirements_authenticated",
            )
            if not prepare_p:
                raise RuntimeError("实时 SDK 未生成 authenticated prepare p")
            prep_headers = self._latest_chatgpt_chatgpt_client_headers(
                target_path="/backend-api/sentinel/chat-requirements/prepare",
            )
            prep_headers["authorization"] = f"Bearer {token}"
            prep_headers["content-type"] = "application/json"
            prep_headers["origin"] = CHATGPT_APP
            self._log(
                "[REG-DIAG][session][protocol] authenticated_prepare request "
                f"p={self._diag_shape(prepare_p, prefix=8)} "
                f"{self._diag_sentinel_p_summary(prepare_p)} "
                f"headers=({self._diag_header_summary(prep_headers)})"
            )
            prep_resp = self.session.post(
                f"{CHATGPT_APP}/backend-api/sentinel/chat-requirements/prepare",
                headers=prep_headers,
                data=json.dumps({"p": prepare_p}, separators=(",", ":")),
                timeout=20,
            )
            self._log(
                f"chatgpt_register 注册后 chat-requirements/prepare 状态: "
                f"{getattr(prep_resp, 'status_code', 0)}"
            )
            self._log(
                "[REG-DIAG][session][protocol] authenticated_prepare response "
                f"status={getattr(prep_resp, 'status_code', 0)} "
                f"{self._diag_sentinel_challenge_summary(self._response_json_dict(prep_resp))}"
            )
            prep_data = self._response_json_dict(prep_resp)
            if getattr(prep_resp, "status_code", 0) == 200:
                for prepare_state, prepare_dispatch, prepare_source in (
                    ("none", "debounced", "composer_editor_state"),
                    ("sent", "immediate", "context_change"),
                ):
                    conversation_prepare_path = "/backend-api/f/conversation/prepare"
                    conversation_prepare_headers = self._latest_chatgpt_chatgpt_client_headers(
                        target_path=conversation_prepare_path,
                    )
                    conversation_prepare_headers["authorization"] = f"Bearer {token}"
                    conversation_prepare_headers["content-type"] = "application/json"
                    conversation_prepare_headers["origin"] = CHATGPT_APP
                    conversation_prepare_body = {
                        "action": "next",
                        "parent_message_id": "client-created-root",
                        "model": "auto",
                        "client_prepare_state": prepare_state,
                        "client_prepare_dispatch": prepare_dispatch,
                        "client_prepare_source": prepare_source,
                        "timezone_offset_min": self.protocol_profile.timezone_offset_min,
                        "timezone": self.protocol_profile.timezone_name,
                        "conversation_mode": {"kind": "primary_assistant"},
                        "system_hints": [],
                        "model_response_contracts": [
                            {
                                "id": "photo_upload_action.v1",
                                "protocol_version": 1,
                                "presets": ["cap:image", "cap:file", "placement:end"],
                            }
                        ],
                        "supports_buffering": True,
                        "supported_encodings": ["v1"],
                        "client_contextual_info": {
                            "app_name": "chatgpt.com",
                            "has_web_push_capabilities": True,
                            "web_push_notification_permission": "default",
                        },
                    }
                    conversation_prepare_resp = self.session.post(
                        f"{CHATGPT_APP}{conversation_prepare_path}",
                        headers=conversation_prepare_headers,
                        data=json.dumps(conversation_prepare_body, separators=(",", ":")),
                        timeout=20,
                    )
                    self._log(
                        "[REG-DIAG][session][protocol] authenticated_conversation/prepare "
                        f"state={prepare_state} status={getattr(conversation_prepare_resp, 'status_code', 0)}"
                    )
                prepare_token = str(prep_data.get("prepare_token") or "").strip()
                pow_meta = prep_data.get("proofofwork") if isinstance(prep_data.get("proofofwork"), dict) else {}
                turnstile = prep_data.get("turnstile") if isinstance(prep_data.get("turnstile"), dict) else {}
                finalize_body: dict[str, Any] = {}
                if prepare_token:
                    finalize_body["prepare_token"] = prepare_token
                if (
                    prepare_token
                    and isinstance(pow_meta, dict)
                    and pow_meta.get("required")
                    and pow_meta.get("seed")
                    and str((turnstile or {}).get("dx") or "").strip()
                ):
                    solution = self._quickjs_chat_requirements_solution(
                        request_p=prepare_p,
                        challenge=prep_data,
                        page_url=CHATGPT_APP,
                        script_url=LATEST_CHATGPT_CF_JSD_SCRIPT_URL,
                        label="authenticated_finalize",
                        runtime_key="chat_requirements_authenticated",
                    )
                    if solution:
                        finalize_body["proofofwork"] = solution["final_p"]
                        finalize_body["turnstile"] = solution["t"]

                if prepare_token and finalize_body.get("proofofwork") and finalize_body.get("turnstile"):
                    finalize_headers = self._latest_chatgpt_chatgpt_client_headers(
                        target_path="/backend-api/sentinel/chat-requirements/finalize",
                    )
                    finalize_headers["authorization"] = f"Bearer {token}"
                    finalize_headers["content-type"] = "application/json"
                    finalize_headers["origin"] = CHATGPT_APP
                    self._log(
                        "[REG-DIAG][session][protocol] authenticated_finalize request "
                        f"prepare_token={self._diag_shape(finalize_body.get('prepare_token'))} "
                        f"pow={self._diag_shape(finalize_body.get('proofofwork'), prefix=8)} "
                        f"turnstile={self._diag_shape(finalize_body.get('turnstile'))} "
                        f"headers=({self._diag_header_summary(finalize_headers)})"
                    )
                    finalize_resp = self.session.post(
                        f"{CHATGPT_APP}/backend-api/sentinel/chat-requirements/finalize",
                        headers=finalize_headers,
                        data=json.dumps(finalize_body, separators=(",", ":")),
                        timeout=30,
                    )
                    self._log(
                        "chatgpt_register 注册后 chat-requirements/finalize 状态: "
                        f"{getattr(finalize_resp, 'status_code', 0)}"
                    )
                    self._log(
                        "[REG-DIAG][session][protocol] authenticated_finalize response "
                        f"status={getattr(finalize_resp, 'status_code', 0)}"
                    )
            completed = True
        except Exception as exc:
            self._log(f"chatgpt_register 注册后 chat-requirements/prepare 失败: {exc}", "warning")

        try:
            for path, params in (
                ("/backend-api/system_hints", {"mode": "basic"}),
                ("/backend-api/system_hints", {"mode": "plugins", "suggestions": "true"}),
                ("/backend-api/system_hints", {"mode": "custom_agents"}),
                (
                    "/backend-api/models",
                    {"iim": "false", "is_gizmo": "false", "supports_model_picker_upgrade_presets": "true"},
                ),
            ):
                headers = self._latest_chatgpt_chatgpt_client_headers(target_path=path)
                headers["authorization"] = f"Bearer {token}"
                response = self.session.get(
                    f"{CHATGPT_APP}{path}",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                self._log(
                    "[REG-DIAG][session][protocol] authenticated_bootstrap_get "
                    f"path={path} status={getattr(response, 'status_code', 0)} "
                    f"headers=({self._diag_header_summary(headers)})"
                )
        except Exception as exc:
            self._log(f"chatgpt_register 注册后 system_hints/models 预热失败: {exc}", "warning")

        try:
            for path, params in (
                ("/backend-api/settings/voices", {"voice_mode": "advanced"}),
                (f"/backend-api/checkout_pricing_config/configs/{self.protocol_profile.pricing_country}", None),
            ):
                headers = self._latest_chatgpt_chatgpt_client_headers(target_path=path)
                headers["authorization"] = f"Bearer {token}"
                response = self.session.get(
                    f"{CHATGPT_APP}{path}",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                self._log(
                    "[REG-DIAG][session][protocol] authenticated_extra_get "
                    f"path={path} status={getattr(response, 'status_code', 0)} "
                    f"headers=({self._diag_header_summary(headers)})"
                )

            init_path = "/backend-api/conversation/init"
            init_headers = self._latest_chatgpt_chatgpt_client_headers(target_path=init_path)
            init_headers["authorization"] = f"Bearer {token}"
            init_headers["content-type"] = "application/json"
            init_headers["origin"] = CHATGPT_APP
            init_response = self.session.post(
                f"{CHATGPT_APP}{init_path}",
                headers=init_headers,
                data=json.dumps(
                    {
                        "requested_default_model": None,
                        "conversation_id": None,
                        "timezone": self.protocol_profile.timezone_name,
                        "timezone_offset_min": self.protocol_profile.timezone_offset_min,
                        "conversation_origin": None,
                    },
                    separators=(",", ":"),
                ),
                timeout=15,
            )
            self._log(
                "[REG-DIAG][session][protocol] authenticated_conversation/init "
                f"status={getattr(init_response, 'status_code', 0)} "
                f"headers=({self._diag_header_summary(init_headers)})"
            )
        except Exception as exc:
            self._log(f"chatgpt_register 注册后补充 backend-api 预热失败: {exc}", "warning")
        return completed


    def _latest_chatgpt_warmup_security_settings(self, access_token: str, *, label: str) -> bool:
        from .constants import CHATGPT_APP

        token = str(access_token or "").strip()
        if not token or not self.session or not self._device_id:
            return False

        completed = False
        for index, path in enumerate((
            "/backend-api/accounts/mfa_info",
            "/backend-api/accounts/security_settings/info",
            "/backend-api/accounts/change_password/eligibility",
            "/backend-api/accounts/add_password/eligibility",
            "/backend-api/accounts/sessions",
        )):
            try:
                if index in (0, 1):
                    self._latest_chatgpt_client_observation(refresh=True)
                headers = self._latest_chatgpt_chatgpt_client_headers(target_path=path)
                headers["authorization"] = f"Bearer {token}"
                response = self.session.get(
                    f"{CHATGPT_APP}{path}",
                    headers=headers,
                    timeout=20,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                self._log(
                    "[REG-DIAG][security][protocol] warmup "
                    f"label={label} path={path} status={status} "
                    f"headers=({self._diag_header_summary(headers)}) "
                    f"cookies={self._diag_cookie_names_text()}"
                )
                if status < 400:
                    completed = True
            except Exception as exc:
                self._log(
                    f"[REG-DIAG][security][protocol] warmup failed label={label} path={path}: {exc}",
                    "warning",
                )
        return completed


    def _latest_chatgpt_fetch_session_result(self, result: RegistrationResult) -> RegistrationResult:
        from .constants import CHATGPT_APP

        callback_url = str(self._create_account_continue_url or "").strip()
        if not callback_url:
            result.error_message = "create_account 未返回 callback URL"
            return result
        callback_url = urllib.parse.urljoin("https://auth.openai.com/", callback_url)

        callback_referer = "https://auth.openai.com/"
        callback_headers = self._latest_chatgpt_nav_headers(referer=callback_referer, sec_fetch_site="cross-site")
        self._log(
            "[REG-DIAG][session][protocol] callback request "
            f"url=({self._diag_url_summary(callback_url)}) "
            f"referer=({self._diag_url_summary(callback_referer)}) "
            f"headers=({self._diag_header_summary(callback_headers)}) "
            f"cookies={self._diag_cookie_names_text()}"
        )
        cb_resp = self.session.get(
            callback_url,
            headers=callback_headers,
            allow_redirects=False,
            timeout=45,
        )
        callback_status = int(getattr(cb_resp, "status_code", 0) or 0)
        self._log(f"chatgpt_register callback 状态: {callback_status}")
        self._log(
            "[REG-DIAG][session][protocol] callback response "
            f"status={callback_status} final_url=({self._diag_url_summary(str(getattr(cb_resp, 'url', '') or ''))}) "
            f"cookies={self._diag_cookie_names_text()} "
            f"session_cookie={self._diag_shape(_cookie_value(self.session.cookies, '__Secure-next-auth.session-token'))} "
            f"account_cookie={self._diag_shape(_cookie_value(self.session.cookies, '_account'))}"
        )
        if callback_status >= 400:
            callback_body = str(getattr(cb_resp, "text", "") or "")
            callback_kind = (
                "cloudflare_managed_challenge"
                if is_cloudflare_managed_challenge_html(callback_body)
                else "http_rejected"
            )
            self._log(
                f"chatgpt_register callback 拒绝诊断: type={callback_kind} "
                f"body={self._short_response_excerpt(cb_resp) or '(empty)'}",
                "warning",
            )

        session_token = _cookie_value(self.session.cookies, "__Secure-next-auth.session-token").strip()
        account_cookie = _cookie_value(self.session.cookies, "_account").strip()
        session_cookies_header = ""

        home_access_token = ""
        authenticated_warmup_done = False
        try:
            home_headers = self._latest_chatgpt_nav_headers(
                referer="https://auth.openai.com/",
                sec_fetch_site="cross-site",
            )
            home_resp = self.session.get(
                f"{CHATGPT_APP}/",
                headers=home_headers,
                timeout=15,
            )
            home_html = str(getattr(home_resp, "text", "") or "")
            self._latest_chatgpt_update_client_build_from_html(home_html, source="home_after_callback")
            home_access_token = self._extract_chatgpt_session_access_token_from_html(
                home_html
            )
            self._log(
                "[REG-DIAG][session][protocol] home_after_callback "
                f"status={getattr(home_resp, 'status_code', 0)} "
                f"access_token={self._diag_shape(home_access_token)} "
                f"headers=({self._diag_header_summary(home_headers)}) "
                f"cookies={self._diag_cookie_names_text()}"
            )
            if home_access_token:
                authenticated_warmup_done = self._latest_chatgpt_warmup_authenticated_session(home_access_token)
        except Exception as exc:
            self._log(f"[REG-DIAG][session][protocol] home_after_callback failed: {exc}", "warning")

        session_headers = self._latest_chatgpt_browser_headers(
            accept="application/json",
            referer=f"{CHATGPT_APP}/",
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-origin",
        )
        session_resp = self.session.get(
            f"{CHATGPT_APP}/api/auth/session",
            headers=session_headers,
            timeout=20,
        )
        session_status = int(getattr(session_resp, "status_code", 0) or 0)
        self._log(f"chatgpt_register session API 状态: {session_status}")
        if session_status >= 400:
            session_body = str(getattr(session_resp, "text", "") or "")
            session_kind = (
                "cloudflare_managed_challenge"
                if is_cloudflare_managed_challenge_html(session_body)
                else "http_rejected"
            )
            self._log(
                f"chatgpt_register session 拒绝诊断: type={session_kind} "
                f"body={self._short_response_excerpt(session_resp) or '(empty)'}",
                "warning",
            )
        session_data = self._response_json_dict(session_resp)
        access_token = str(session_data.get("accessToken") or home_access_token or "").strip()
        session_cookies_header = _cookies_to_header(self.session.cookies)
        session_token = str(
            session_data.get("sessionToken")
            or _cookie_value(self.session.cookies, "__Secure-next-auth.session-token")
            or session_token
            or ""
        ).strip()
        account_cookie = (_cookie_value(self.session.cookies, "_account") or account_cookie or "").strip()
        self._log(
            "[REG-DIAG][session][protocol] session_api response "
            f"status={session_status} "
            f"headers=({self._diag_header_summary(session_headers)}) "
            f"payload_keys={','.join(sorted(str(k) for k in session_data.keys())) if session_data else '-'} "
            f"access_token={self._diag_shape(session_data.get('accessToken') or home_access_token)} "
            f"session_token_json={self._diag_shape(session_data.get('sessionToken'))} "
            f"session_cookie={self._diag_shape(session_token)} "
            f"account_cookie={self._diag_shape(account_cookie)} "
            f"cookies={self._diag_cookie_names_text()} "
            f"cookie_header_count={len([part for part in session_cookies_header.split(';') if part.strip()])}"
        )
        if access_token and not authenticated_warmup_done:
            authenticated_warmup_done = self._latest_chatgpt_warmup_authenticated_session(access_token)
        if not access_token and (callback_status >= 400 or session_status >= 400):
            browser_session = self._latest_chatgpt_fetch_session_via_headless_callback(callback_url)
            if browser_session:
                (
                    callback_status,
                    session_status,
                    session_data,
                    browser_session_token,
                    browser_account_cookie,
                    session_cookies_header,
                ) = browser_session
                access_token = str(session_data.get("accessToken") or "").strip()
                if browser_session_token:
                    session_token = browser_session_token
                if browser_account_cookie:
                    account_cookie = browser_account_cookie
        session_cookies_header = _cookies_to_header(self.session.cookies)
        session_token = str(
            session_data.get("sessionToken")
            or _cookie_value(self.session.cookies, "__Secure-next-auth.session-token")
            or session_token
            or ""
        ).strip()
        account_cookie = (_cookie_value(self.session.cookies, "_account") or account_cookie or "").strip()
        account_data = session_data.get("account") if isinstance(session_data.get("account"), dict) else {}
        user_data = session_data.get("user") if isinstance(session_data.get("user"), dict) else {}
        account_id = (
            str(account_data.get("id") or "").strip()
            or _extract_chatgpt_account_id(access_token)
            or account_cookie
        )
        if not access_token:
            result.error_message = (
                "chatgpt.com session 未返回 accessToken "
                f"(callback_status={callback_status}, session_status={session_status})"
            )
            return result
        if not account_id:
            result.error_message = "chatgpt.com session 未返回 account_id"
            return result

        if self._latest_chatgpt_add_password_after_register(access_token):
            session_cookies_header = _cookies_to_header(self.session.cookies)
            session_token = str(_cookie_value(self.session.cookies, "__Secure-next-auth.session-token") or session_token or "").strip()
            account_cookie = str(_cookie_value(self.session.cookies, "_account") or account_cookie or "").strip()
            try:
                refreshed_session_resp = self.session.get(
                    f"{CHATGPT_APP}/api/auth/session",
                    headers=session_headers,
                    timeout=20,
                )
                refreshed_session_data = self._response_json_dict(refreshed_session_resp)
                refreshed_access_token = str(refreshed_session_data.get("accessToken") or "").strip()
                self._log(
                    "[REG-DIAG][session][protocol] session_api after add_password "
                    f"status={getattr(refreshed_session_resp, 'status_code', 0)} "
                    f"payload_keys={','.join(sorted(str(k) for k in refreshed_session_data.keys())) if refreshed_session_data else '-'} "
                    f"access_token={self._diag_shape(refreshed_access_token)} "
                    f"cookies={self._diag_cookie_names_text()}"
                )
                if refreshed_access_token:
                    session_data = refreshed_session_data
                    access_token = refreshed_access_token
                    session_cookies_header = _cookies_to_header(self.session.cookies)
                    session_token = str(
                        refreshed_session_data.get("sessionToken")
                        or _cookie_value(self.session.cookies, "__Secure-next-auth.session-token")
                        or session_token
                        or ""
                    ).strip()
                    account_cookie = str(_cookie_value(self.session.cookies, "_account") or account_cookie or "").strip()
                    self._latest_chatgpt_warmup_authenticated_session(refreshed_access_token)
                    account_data = session_data.get("account") if isinstance(session_data.get("account"), dict) else {}
                    user_data = session_data.get("user") if isinstance(session_data.get("user"), dict) else user_data
                    account_id = (
                        str(account_data.get("id") or "").strip()
                        or _extract_chatgpt_account_id(access_token)
                        or account_cookie
                        or account_id
                    )
            except Exception as exc:
                self._log(f"设置帐号密码后刷新 session 失败: {exc}", "warning")
        session_cookies_header = _cookies_to_header(self.session.cookies)
        self._log(
            "[REG-DIAG][session][protocol] saved_cookie_header "
            f"names={self._diag_cookie_names_text()} "
            f"count={len([part for part in session_cookies_header.split(';') if part.strip()])}"
        )

        result.success = True
        result.email = self.email or ""
        result.password = self.password or ""
        result.account_id = account_id
        result.access_token = access_token
        result.refresh_token = ""
        result.id_token = access_token
        result.session_token = session_token
        result.source = "login" if self._is_existing_account else "register"
        session_source = str(
            getattr(
                self,
                "_latest_chatgpt_session_source",
                "latest_otp_external_callback" if self._is_existing_account else "latest_create_account_callback",
            )
            or ""
        )
        service_type = getattr(getattr(self.email_service, "service_type", None), "value", "")
        result.metadata = {
            "email_service": service_type,
            "proxy_used": self.proxy_url,
            "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "is_existing_account": self._is_existing_account,
            "cookies": session_cookies_header,
            "login_state_cookie": session_cookies_header,
            "cookie_header": session_cookies_header,
            "profile": user_data,
            "expires_at": str(session_data.get("expires") or ""),
            "session": session_data,
            "auth_source": "chatgpt_register_latest",
            "chatgpt_session_source": session_source,
            "chatgpt_user_agent": self._latest_chatgpt_user_agent(),
            "chatgpt_accept_language": self._latest_chatgpt_accept_language(),
            "chatgpt_oai_client_version": self._latest_chatgpt_client_version(),
            "chatgpt_oai_client_build_number": self._latest_chatgpt_client_build_number(),
            "chatgpt_oai_device_id": self._device_id or "",
            "chatgpt_oai_session_id": str(getattr(self, "_chatgpt_oai_session_id", "") or ""),
            "password_set_after_register": bool(
                getattr(self, "_password_registered_during_flow", False)
                or getattr(self, "_post_register_password_set", False)
            ),
            "post_register_password_error": str(getattr(self, "_post_register_password_error", "") or ""),
            "openai_register_reference": r"D:\work\ai\chatgpt_register\chatgpt_register.py",
        }
        self._log("=" * 60)
        self._log("注册成功! (chatgpt_register 最新邮箱 OAuth 流程)")
        self._log(f"邮箱: {result.email}")
        self._log(f"Account ID: {result.account_id}")
        self._log("=" * 60)
        return result


    def _latest_chatgpt_login_after_account_deactivated(self, result: RegistrationResult) -> RegistrationResult:
        """account_deactivated 时改走已注册账号登录，成功后保存 ChatGPT session。"""
        self._log("检测到 account_deactivated，改走已注册账号登录流程获取 session", "warning")
        actual = self.run_chatgpt_refresh_session_latest(
            result,
            session_source="latest_account_deactivated_login",
        )
        error_text = str(actual.error_message or "")
        if not actual.success and (
            self._is_login_auth_rejected_error(error_text)
            or "account_deactivated" in error_text.lower()
        ):
            self._mark_current_email_invalid("account_deactivated_login_rejected")
            actual.error_message = f"account_deactivated 登录失败，已标记无效邮箱: {actual.error_message}"
            self._log(actual.error_message, "warning")
        return actual


    def run_chatgpt_register_latest(self) -> RegistrationResult:
        """执行 D:\\work\\ai\\chatgpt_register 的最新版邮箱注册主链。"""
        result = RegistrationResult(success=False, logs=self.logs)
        try:
            self._log("=" * 60)
            self._log("开始注册流程（chatgpt_register 最新版）")
            self._log("=" * 60)

            self._log("1. 准备邮箱...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result
            result.email = self.email

            if not self.password:
                self.password = self._generate_password()
            result.password = self.password or ""

            self._log("2. 初始化会话...")
            if not self._init_latest_chatgpt_session():
                result.error_message = "初始化会话失败"
                return result

            self._refresh_mailbox_before_ids()
            self._log("3. 使用 chatgpt.com login_hint 初始化邮箱 OAuth 注册...")
            init_error = ""
            for init_attempt in range(1, 4):
                if init_attempt > 1:
                    self._log(f"chatgpt_register 初始化网络失败，重试 ({init_attempt}/3)...", "warning")
                    self._refresh_mailbox_before_ids()
                    self._reset_latest_chatgpt_session_for_retry()
                init_ok, init_error = self._latest_chatgpt_init_email_oauth()
                if init_ok:
                    break
                if not self._is_latest_chatgpt_init_retryable_error(init_error) or init_attempt >= 3:
                    result.error_message = f"chatgpt_register 初始化失败: {init_error}"
                    return result
                self._log(f"chatgpt_register 初始化失败: {init_error}", "warning")
                time.sleep(2)

            init_final_url = str(getattr(self, "_latest_chatgpt_init_final_url", "") or "")
            if "/create-account/password" in init_final_url:
                self._log("4. 初始化进入注册密码页，先提交密码...")
                password_ok, registered_password = self._register_password()
                if not password_ok:
                    result.error_message = "注册密码失败"
                    return result
                if registered_password:
                    result.password = registered_password
                if not self._email_otp_continue_url:
                    result.error_message = "注册密码后未进入邮箱验证码步骤"
                    return result
                if self._otp_page_type == "email_otp_send":
                    self._log("密码提交返回 email_otp_send，显式触发邮箱验证码发送...")
                    if not self._send_verification_code():
                        result.error_message = "注册密码后发送邮箱验证码失败"
                        return result
            elif init_final_url and "/email-verification" not in init_final_url and "/email-otp" not in init_final_url:
                result.error_message = f"chatgpt_register 初始化后未进入邮箱验证码步骤: {init_final_url}"
                return result

            self._log("5. 等待邮箱验证码...")
            code = self._get_verification_code(resend_on_timeout=False)
            if not code:
                result.error_message = self._email_otp_failure_message()
                return result

            self._log("6. 提交邮箱验证码...")
            validate_payload = None
            last_otp_error = ""
            for otp_validate_attempt in range(1, 4):
                try:
                    if otp_validate_attempt > 1:
                        self._log(f"重新提交邮箱验证码 ({otp_validate_attempt}/3)...")
                    validate_payload = self._latest_chatgpt_validate_email_otp(code)
                    break
                except RuntimeError as exc:
                    last_otp_error = str(exc)
                    if last_otp_error == "account_deactivated":
                        return self._latest_chatgpt_login_after_account_deactivated(result)
                    if last_otp_error == "invalid_state" and otp_validate_attempt < 3:
                        code = self._latest_chatgpt_refresh_email_otp_after_invalid_state()
                        self._log("invalid_state 恢复: 重新提交刷新后的邮箱验证码...")
                        continue
                    if last_otp_error == "wrong_email_otp_code" and otp_validate_attempt < 3:
                        code = self._latest_chatgpt_resend_email_otp_after_rejected_code(last_otp_error)
                        continue
                    raise
            if validate_payload is None:
                raise RuntimeError(last_otp_error or "email_otp_validate_failed")
            callback_url = self._chatgpt_callback_url_from_payload(validate_payload)
            if callback_url:
                self._create_account_continue_url = callback_url
                self._is_existing_account = True
                self._latest_chatgpt_session_source = "latest_otp_external_callback"
                self._log(
                    "chatgpt_register OTP validate 已返回 ChatGPT callback，"
                    "跳过 about-you/create_account，直接获取 session"
                )
                self._log("8. 跟随 callback 并获取 chatgpt.com session...")
                return self._latest_chatgpt_fetch_session_result(result)
            about_you_url = str(validate_payload.get("continue_url") or "").strip()
            if not self._latest_chatgpt_open_about_you(about_you_url):
                detail = str(getattr(self, "_last_about_you_error", "") or "").strip()
                result.error_message = f"OTP 后 about-you 导航失败: {detail}" if detail else "OTP 后 about-you 导航失败"
                return result

            self._log("7. 创建账号资料...")
            if not self._latest_chatgpt_create_account_with_retry():
                if str(getattr(self, "_last_create_account_error_code", "") or "") == "account_deactivated":
                    return self._latest_chatgpt_login_after_account_deactivated(result)
                if str(getattr(self, "_last_create_account_error_code", "") or "") == "registration_disallowed":
                    result.error_message = "registration_disallowed，当前邮箱已标记为已注册且子邮箱耗尽"
                    return result
                result.error_message = (
                    "EMAIL_ALIAS_PARENT_EXHAUSTED: user_already_exists - parent email alias quota exhausted"
                    if getattr(self, "_user_already_exists", False)
                    else "创建用户账户失败"
                )
                return result

            self._log("8. 跟随 callback 并获取 chatgpt.com session...")
            return self._latest_chatgpt_fetch_session_result(result)
        except Exception as exc:
            self._log(f"chatgpt_register 最新注册流程异常: {exc}", "error")
            result.error_message = str(exc)
            return result



    def _get_device_id(self) -> Optional[str]:

        """获取 Device ID"""

        try:

            if not self.oauth_start:

                return None



            response = self.session.get(

                self.oauth_start.auth_url,

                timeout=15

            )

            did = self._read_oai_did_cookie()

            if not did:

                did = self._seed_oai_did_cookie(self._protocol_device_id())

                self._log(f"Device ID 未由 OpenAI 返回，已本地生成: {did}")

            else:

                self._log(f"Device ID: {did}")

            return did



        except Exception as e:

            self._log(f"获取 Device ID 失败: {e}", "error")

            return None



    def _check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> Optional[SentinelPayload]:

        """检查 Sentinel 拦截（动态生成 token + 处理 PoW）"""

        try:

            ua = self.http_client.default_headers.get("User-Agent", "")

            quickjs_payload = self._quickjs_sentinel_payload(
                getattr(self.http_client, "session", None) or self.session,
                did,
                flow=flow,
                user_agent=ua,
                accept_language=self._latest_chatgpt_accept_language(),
                label="主注册链路实时 SDK",
            )
            if quickjs_payload:
                self._log(
                    "[REG-DIAG][sentinel][protocol] using realtime_sdk "
                    f"flow={flow} p={self._diag_shape(quickjs_payload.p, prefix=8)} "
                    f"t={self._diag_shape(quickjs_payload.t)} "
                    f"c={self._diag_shape(quickjs_payload.c)} "
                    f"so={self._diag_shape(quickjs_payload.so_token)}"
                )
                return quickjs_payload

            generator = _SentinelTokenGenerator(did, ua, client_version=self._latest_chatgpt_client_version(), profile=self.protocol_profile)

            sent_p = generator.generate_requirements_token()

            sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": flow}, separators=(",", ":"))
            sen_headers = self._latest_chatgpt_sentinel_headers(user_agent=ua)
            self._log(
                "[REG-DIAG][sentinel][protocol] req "
                f"flow={flow} endpoint={OPENAI_API_ENDPOINTS['sentinel']} "
                f"p={self._diag_shape(sent_p, prefix=8)} "
                f"{self._diag_sentinel_p_summary(sent_p)} "
                f"id={self._diag_shape(did)} "
                f"headers=({self._diag_header_summary(sen_headers)}) "
                f"sdk={self._latest_chatgpt_sentinel_sdk_url()} frame={self._latest_chatgpt_sentinel_frame_url()}"
            )



            response = self.http_client.post(

                OPENAI_API_ENDPOINTS["sentinel"],

                headers=sen_headers,

                data=sen_req_body,

            )



            if response.status_code == 200:

                data = response.json()
                self._log(
                    "[REG-DIAG][sentinel][protocol] challenge "
                    f"flow={flow} status={response.status_code} "
                    f"{self._diag_sentinel_challenge_summary(data)}"
                )

                sen_token = str(data.get("token") or "")

                turnstile = data.get("turnstile") or {}



                # Handle proofofwork challenge if required

                initial_p = sent_p  # keep for dx decryption

                pow_meta = data.get("proofofwork") or {}

                # Headed HAR create_account always uses final enforcement p
                # (gAAAAAB + backend-api/sentinel/sdk.js), not the initial requirements p.
                if pow_meta.get("required") and pow_meta.get("seed"):
                    sent_p = generator.generate_token(
                        str(pow_meta.get("seed") or ""),
                        str(pow_meta.get("difficulty") or "0"),
                    )
                    self._log(f"Sentinel PoW solved: flow={flow}")
                else:
                    sent_p = generator.generate_token(
                        str(pow_meta.get("seed") or ""),
                        str(pow_meta.get("difficulty") or "0"),
                    )



                # Solve turnstile dx with VM

                t_value = ""

                dx_b64 = str(turnstile.get("dx") or "")

                if dx_b64:

                    try:

                        from .sentinel_vm import solve_turnstile_dx

                        t_value = solve_turnstile_dx(dx_b64, initial_p, user_agent=ua, sdk_url=self._latest_chatgpt_sentinel_sdk_url())

                        self._log(f"Sentinel VM solved: t_len={len(t_value)} flow={flow}")

                    except Exception as vm_err:

                        self._log(f"Sentinel VM failed: {vm_err}", "warning")

                so_meta = data.get("so") or {}
                so_token = ""
                need_t = bool(dx_b64 and not t_value)
                need_so = bool(isinstance(so_meta, dict) and so_meta.get("required"))
                # HAR create_account 总是带 openai-sentinel-so-token；优先本地 VM 解 snapshot_dx。
                if need_so:
                    so_token = self._solve_session_observer_token(
                        device_id=did,
                        flow=flow,
                        challenge=data if isinstance(data, dict) else {},
                        request_p=initial_p,
                        user_agent=ua,
                    )
                    if so_token:
                        self._log(f"Sentinel so VM solved: so_len={len(so_token)} flow={flow}")
                if need_t or (need_so and not so_token):
                    quickjs_payload = self._quickjs_sentinel_payload(
                        getattr(self.http_client, "session", None) or self.session,
                        did,
                        flow=flow,
                        user_agent=ua,
                        accept_language=self._latest_chatgpt_accept_language(),
                        label="主注册链路 VM/PoW t/so 补齐",
                    )
                    if quickjs_payload:
                        # HAR shows create_account needs a real long VM t.
                        # Only take QuickJS t when VM failed; keep VM t when already solved.
                        if need_t and quickjs_payload.t:
                            t_value = quickjs_payload.t
                            if quickjs_payload.p:
                                sent_p = quickjs_payload.p
                            if quickjs_payload.c:
                                sen_token = quickjs_payload.c
                        if need_so and not so_token and quickjs_payload.so_token:
                            so_token = quickjs_payload.so_token

                payload = SentinelPayload(
                    p=sent_p,
                    c=sen_token,
                    flow=flow,
                    t=t_value,
                    so_token=so_token,
                )

                self._log(
                    f"Sentinel token 获取成功: flow={flow} t_len={len(payload.t)} "
                    f"so_len={len(payload.so_token)}"
                )
                self._log(
                    "[REG-DIAG][sentinel][protocol] solved "
                    f"flow={flow} "
                    f"p={self._diag_shape(payload.p, prefix=8)} "
                    f"{self._diag_sentinel_p_summary(payload.p)} "
                    f"c={self._diag_shape(payload.c)} "
                    f"t={self._diag_shape(payload.t)} "
                    f"so={self._diag_shape(payload.so_token)}"
                )

                return payload

            else:

                self._log(f"Sentinel 检查失败: flow={flow} status={response.status_code}", "warning")
                self._log(
                    "[REG-DIAG][sentinel][protocol] req_failed "
                    f"flow={flow} status={getattr(response, 'status_code', 0)} "
                    f"body={self._short_response_excerpt(response) or '(empty)'}",
                    "warning",
                )

                quickjs_payload = self._quickjs_sentinel_payload(
                    getattr(self.http_client, "session", None) or self.session,
                    did,
                    flow=flow,
                    user_agent=ua,
                    accept_language=self._latest_chatgpt_accept_language(),
                    label="主注册链路 VM/PoW 失败后",
                )

                return quickjs_payload



        except Exception as e:

            self._log(f"Sentinel VM/PoW 检查异常: flow={flow} {e}", "warning")

            try:

                quickjs_payload = self._quickjs_sentinel_payload(
                    getattr(self.http_client, "session", None) or self.session,
                    did,
                    flow=flow,
                    user_agent=self.http_client.default_headers.get("User-Agent", ""),
                    accept_language=self._latest_chatgpt_accept_language(),
                    label="主注册链路 VM/PoW 异常后",
                )

                return quickjs_payload

            except Exception as quickjs_error:

                self._log(f"Sentinel QuickJS 兜底异常: flow={flow} {quickjs_error}", "warning")

            return None



    @staticmethod
    def _sentinel_payload_header(payload: SentinelPayload, device_id: str) -> str:

        return json.dumps(
            {
                "p": payload.p,
                "t": payload.t,
                "c": payload.c,
                "id": device_id,
                "flow": payload.flow,
            },
            separators=(",", ":"),
        )


    def _parse_sentinel_header_payload(
        self,
        token: str,
        *,
        flow: str,
        label: str,
    ) -> Optional[SentinelPayload]:

        try:

            data = json.loads(token or "")

        except Exception as exc:

            self._log(f"{label} QuickJS Sentinel 返回非 JSON: {exc}", "warning")

            return None

        if not isinstance(data, dict):

            self._log(f"{label} QuickJS Sentinel 返回结构异常", "warning")

            return None

        p_value = str(data.get("p") or "").strip()

        c_value = str(data.get("c") or "").strip()

        if not p_value or not c_value:

            self._log(f"{label} QuickJS Sentinel 缺少 p/c，回退原 Sentinel VM", "warning")

            return None

        return SentinelPayload(
            p=p_value,
            t=str(data.get("t") or "").strip(),
            c=c_value,
            flow=str(data.get("flow") or flow).strip() or flow,
        )


    def _quickjs_chat_requirements_p(
        self,
        *,
        page_url: str,
        script_url: str,
        label: str,
        runtime_key: str,
    ) -> Optional[str]:
        """Generate chat-requirements p with the same realtime Sentinel worker."""
        try:
            from .authflow_experimental.sentinel_quickjs import (
                get_sentinel_requirements_via_quickjs,
            )

            session = getattr(self.http_client, "session", None) or self.session
            runtime_state = self._sentinel_runtime_state.setdefault(runtime_key, {})
            if not isinstance(runtime_state, dict):
                raise RuntimeError(f"Sentinel runtime key invalid: {runtime_key}")
            result = get_sentinel_requirements_via_quickjs(
                session,
                self._device_id,
                user_agent=self._latest_chatgpt_user_agent(),
                accept_language=self._latest_chatgpt_accept_language(),
                client_version=self._latest_chatgpt_client_version(),
                script_url=script_url,
                page_url=page_url,
                timezone_name=self.protocol_profile.timezone_name,
                timezone_offset_min=self.protocol_profile.timezone_offset_min,
                runtime_state=runtime_state,
                log=lambda message: self._log(f"{label} {message}"),
            )
            request_p = str((result or {}).get("request_p") or "").strip()
            if not request_p:
                raise RuntimeError("requirements 未返回 p")
            self._log(
                f"[REG-DIAG][sentinel][protocol] {label} requirements "
                f"p={self._diag_shape(request_p, prefix=8)} "
                f"{self._diag_sentinel_p_summary(request_p)} "
                f"sdk={(result or {}).get('sdk_url') or '-'} "
                f"sdk_source={(result or {}).get('sdk_source') or '-'} "
                f"collector_id={(result or {}).get('collector_id') or '-'}"
            )
            return request_p
        except Exception as exc:
            self._log(f"{label} realtime SDK requirements 失败: {exc}", "warning")
            return None


    def _quickjs_chat_requirements_solution(
        self,
        *,
        request_p: str,
        challenge: dict[str, Any],
        page_url: str,
        script_url: str,
        label: str,
        runtime_key: str,
        flow: str = "chat_requirements",
    ) -> Optional[dict[str, str]]:
        """Solve chat-requirements finalize with the worker that produced request_p."""
        try:
            from .authflow_experimental.sentinel_quickjs import (
                solve_sentinel_challenge_via_quickjs,
            )

            session = getattr(self.http_client, "session", None) or self.session
            runtime_state = self._sentinel_runtime_state.setdefault(runtime_key, {})
            if not isinstance(runtime_state, dict):
                raise RuntimeError(f"Sentinel runtime key invalid: {runtime_key}")
            result = solve_sentinel_challenge_via_quickjs(
                session,
                self._device_id,
                request_p=request_p,
                challenge=challenge,
                flow=flow,
                user_agent=self._latest_chatgpt_user_agent(),
                accept_language=self._latest_chatgpt_accept_language(),
                client_version=self._latest_chatgpt_client_version(),
                script_url=script_url,
                page_url=page_url,
                timezone_name=self.protocol_profile.timezone_name,
                timezone_offset_min=self.protocol_profile.timezone_offset_min,
                runtime_state=runtime_state,
                log=lambda message: self._log(f"{label} {message}"),
            )
            if not result:
                raise RuntimeError("实时 SDK 未生成 finalize challenge")
            self._log(
                f"[REG-DIAG][sentinel][protocol] {label} solution "
                f"p={self._diag_shape(result.get('final_p'), prefix=8)} "
                f"t={self._diag_shape(result.get('t'))} "
                f"collector_id={result.get('collector_id') or '-'} "
                f"runtime_id={result.get('runtime_id') or '-'}"
            )
            return result
        except Exception as exc:
            self._log(f"{label} realtime SDK finalize 失败: {exc}", "warning")
            return None

    def _quickjs_sentinel_payload(
        self,
        session,
        device_id: str,
        *,
        flow: str,
        user_agent: str,
        label: str,
        accept_language: str = "",
    ) -> Optional[SentinelPayload]:

        """VM/PoW 失败后兜底使用真实 OpenAI Sentinel SDK。"""

        if not session:

            return None

        import os as _os_sentinel

        if _os_sentinel.environ.get("OPENAI_SENTINEL_DISABLE_QUICKJS"):

            return None

        try:

            from .authflow_experimental.sentinel_quickjs import get_sentinel_tokens_via_quickjs

            token_bundle = get_sentinel_tokens_via_quickjs(
                session,
                device_id=device_id,
                flow=flow,
                user_agent=user_agent,
                accept_language=accept_language,
                timezone_name=self.protocol_profile.timezone_name,
                timezone_offset_min=self.protocol_profile.timezone_offset_min,
                runtime_state=self._sentinel_runtime_state,
                log=lambda message: self._log(f"{label} {message}"),
            )
            token = str((token_bundle or {}).get("token") or "")
            so_token = str((token_bundle or {}).get("so_token") or "")
            self._log(
                f"[REG-DIAG][sentinel][protocol] quickjs bundle "
                f"label={label} token={self._diag_shape(token)} so={self._diag_shape(so_token)} "
                f"ua={user_agent or '-'} accept_language={accept_language or '-'} "
                f"sdk={(token_bundle or {}).get('sdk_url') or '-'} "
                f"sdk_source={(token_bundle or {}).get('sdk_source') or '-'} "
                f"runtime_id={(token_bundle or {}).get('runtime_id') or '-'} "
                f"time_origin={(token_bundle or {}).get('time_origin') or '-'} "
                f"collector_id={(token_bundle or {}).get('collector_id') or '-'}"
            )

        except Exception as exc:

            self._log(f"{label} QuickJS Sentinel 调用异常: {exc}", "warning")

            return None

        payload = self._parse_sentinel_header_payload(token or "", flow=flow, label=label) if token else None

        if not payload:

            return None

        payload.so_token = so_token

        self._log(
            f"{label} QuickJS Sentinel 已生成: "
            f"flow={payload.flow} t_len={len(payload.t)} so_len={len(payload.so_token)} "
            f"{self._diag_sentinel_p_summary(payload.p)}"
        )

        return payload


    def _submit_signup_form(self, did: str, sen_payload: Optional[SentinelPayload]) -> SignupFormResult:

        """

        提交注册表单（通过 authorize/continue 建立 session）



        Returns:

            SignupFormResult: 提交结果，包含账号状态判断

        """

        try:

            self._device_id = did

            self._signup_sentinel = sen_payload

            self._sentinel_token = sen_payload.c if sen_payload else None

            signup_body = json.dumps(
                {"username": {"value": self.email, "kind": "email"}, "screen_hint": "signup"},
                separators=(",", ":"),
            )

            def _build_signup_headers(current_sentinel: Optional[SentinelPayload]) -> dict:
                headers = {
                    "referer": "https://auth.openai.com/create-account",
                    "accept": "application/json",
                    "content-type": "application/json",
                    "sec-fetch-site": "same-origin",
                    **_generate_datadog_trace_headers(),
                }
                if did:
                    headers["oai-device-id"] = did
                if current_sentinel:
                    sentinel = json.dumps(
                        {
                            "p": current_sentinel.p,
                            "t": current_sentinel.t,
                            "c": current_sentinel.c,
                            "id": did,
                            "flow": current_sentinel.flow,
                        },
                        separators=(",", ":"),
                    )
                    headers["openai-sentinel-token"] = sentinel
                return headers

            def _post_signup(current_sentinel: Optional[SentinelPayload], label: str):
                headers = _build_signup_headers(current_sentinel)
                self._log_auth_state_cookies(f"提交注册表单前 cookie({label})")
                self._log(
                    f"提交注册表单请求({label}): POST {OPENAI_API_ENDPOINTS['signup']} "
                    f"sentinel={'yes' if current_sentinel else 'no'} "
                    f"flow={getattr(current_sentinel, 'flow', '') or ''}"
                )
                return self.session.post(
                    OPENAI_API_ENDPOINTS["signup"],
                    headers=headers,
                    data=signup_body,
                    timeout=15,
                )

            response = _post_signup(sen_payload, "initial")

            for retry_index, regenerate_oauth in enumerate((False, True), start=1):
                if response.status_code != 409 or not self._is_invalid_state_response(response):
                    break
                self._log(
                    f"提交注册表单 invalid_state，第 {retry_index}/2 次重建 authorize 后重试: {response.text}",
                    "warning",
                )
                refreshed_sentinel = self._refresh_signup_authorize_state(
                    did,
                    regenerate_oauth=regenerate_oauth,
                )
                if refreshed_sentinel:
                    sen_payload = refreshed_sentinel
                    self._signup_sentinel = refreshed_sentinel
                    self._sentinel_token = refreshed_sentinel.c
                else:
                    self._log("invalid_state 恢复未取得新 Sentinel，仍按当前会话尝试提交", "warning")
                response = _post_signup(sen_payload, f"retry{retry_index}")



            self._log(f"提交注册表单状态: {response.status_code}")



            if response.status_code != 200:

                return SignupFormResult(

                    success=False,

                    error_message=f"HTTP {response.status_code}: {response.text}"

                )



            try:

                response_data = response.json()

            except Exception as parse_error:

                self._log(f"signup 响应非 JSON: {parse_error}, body={response.text}", "warning")

                return SignupFormResult(

                    success=False,

                    error_message=f"signup 返回非 JSON: {response.text}",

                    response_data={},

                )



            if isinstance(response_data, dict):

                err = response_data.get("error") or response_data.get("detail") or ""

                if err:

                    err_msg = err if isinstance(err, str) else json.dumps(err)

                    self._log(f"signup 返回错误: {err_msg}", "warning")



            page_type = response_data.get("page", {}).get("type", "")

            continue_url = str(response_data.get("continue_url") or "")

            self._log(f"响应页面类型: {page_type}")



            is_existing = False

            if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:

                self._email_otp_continue_url = continue_url or "https://auth.openai.com/email-verification"

                self._log("已进入邮箱 OTP 验证流程，将显式发送验证码")



            return SignupFormResult(

                success=True,

                page_type=page_type,

                is_existing_account=is_existing,

                response_data=response_data

            )



        except Exception as e:

            self._log(f"提交注册表单失败: {e}", "error")

            return SignupFormResult(success=False, error_message=str(e))



    def _register_password(self) -> Tuple[bool, Optional[str]]:

        """注册密码"""

        try:

            candidates = []

            while len(candidates) < 3:

                pwd = self._generate_password()

                if pwd not in candidates:

                    candidates.append(pwd)



            for index, password in enumerate(candidates, start=1):

                self.password = password



                # Reload page + refresh sentinel for each attempt (tokens are single-use)

                self._load_create_account_password_page()

                if self._device_id:

                    self._password_sentinel = self._check_sentinel(self._device_id, flow="username_password_create")

                    if self._password_sentinel:

                        self._log(

                            f"密码阶段 Sentinel 已刷新: flow={self._password_sentinel.flow} "

                            f"turnstile={'yes' if self._password_sentinel.t else 'no'}"

                        )



                self._log(f"生成密码[{index}/{len(candidates)}]: {password}")



                register_body = json.dumps({

                    "password": password,

                    "username": self.email

                })



                register_headers = self._latest_chatgpt_json_headers(
                    referer="https://auth.openai.com/create-account/password"
                )

                if self._device_id:

                    register_headers["oai-device-id"] = self._device_id

                if self._password_sentinel and self._device_id:

                    register_headers["openai-sentinel-token"] = json.dumps({

                        "p": self._password_sentinel.p,

                        "t": self._password_sentinel.t,

                        "c": self._password_sentinel.c,

                        "id": self._device_id,

                        "flow": self._password_sentinel.flow,

                    }, separators=(",", ":"))



                response = self.session.post(

                    OPENAI_API_ENDPOINTS["register"],

                    headers=register_headers,

                    data=register_body,

                    timeout=15,

                )



                self._log(f"提交密码状态[{index}/{len(candidates)}]: {response.status_code}")



                if response.status_code == 200:

                    # 解析响应，检测已注册账号

                    try:

                        resp_data = response.json()

                        page_type = resp_data.get("page", {}).get("type", "")

                        continue_url = str(resp_data.get("continue_url") or "")

                        self._log(f"注册响应页面类型: {page_type}")

                        if page_type in (
                            OPENAI_PAGE_TYPES.get("EMAIL_OTP_VERIFICATION", "email_otp_verification"),
                            "email_otp_send",
                        ):

                            self._log("密码提交后进入邮箱 OTP 验证流程")
                            self._otp_page_type = page_type
                            self._email_otp_continue_url = continue_url or "https://auth.openai.com/email-verification"
                            self._email_otp_page_loaded = False
                            self._otp_sent_at = None

                            if continue_url:

                                self._log(f"密码响应 continue_url: {continue_url}")

                    except Exception:

                        pass

                    self._password_registered_during_flow = True
                    return True, password



                error_text = response.text

                self._log(f"密码注册失败[{index}/{len(candidates)}]: {error_text}", "warning")



                try:

                    error_json = response.json()

                    error_msg = error_json.get("error", {}).get("message", "")

                    error_code = error_json.get("error", {}).get("code", "")



                    if "already" in error_msg.lower() or "exists" in error_msg.lower() or error_code == "user_exists":

                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")

                        self._mark_email_as_registered()

                        return False, None

                except Exception:

                    pass



            return False, None



        except Exception as e:

            self._log(f"密码注册失败: {e}", "error")

            return False, None



    def _mark_email_as_registered(self):

        """标记邮箱为已注册状态（用于防止重复尝试）"""

        try:

            with get_db() as db:

                # 检查是否已存在该邮箱的记录

                existing = crud.get_account_by_email(db, self.email)

                if not existing:

                    # 创建一个失败记录，标记该邮箱已注册过

                    crud.create_account(

                        db,

                        email=self.email,

                        password="",  # 空密码表示未成功注册

                        email_service=self.email_service.service_type.value,

                        email_service_id=self.email_info.get("service_id") if self.email_info else None,

                        status="failed",

                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}

                    )

                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")

        except Exception as e:

            logger.warning(f"标记邮箱状态失败: {e}")



    def _send_verification_code(self) -> bool:

        """发送验证码"""

        try:

            raw_continue_url = str(self._email_otp_continue_url or "").strip()
            email_verification_url = raw_continue_url or "https://auth.openai.com/email-verification"
            continue_is_send_api = "/api/accounts/email-otp/send" in email_verification_url
            if continue_is_send_api:
                email_verification_url = "https://auth.openai.com/email-verification"

            self._log(f"邮箱验证页 URL: {email_verification_url}")

            send_url = (
                urllib.parse.urljoin("https://auth.openai.com/", raw_continue_url)
                if continue_is_send_api
                else OPENAI_API_ENDPOINTS["send_otp"]
            )
            password_referer = "https://auth.openai.com/create-account/password"
            csrf_token = ""

            if continue_is_send_api:
                self._email_otp_page_loaded = True

            if not self._email_otp_page_loaded:

                try:
                    page_resp = self.session.get(
                        email_verification_url,
                        headers=self._platform_nav_headers(referer="https://auth.openai.com/create-account"),
                        timeout=15,
                        allow_redirects=True,
                    )
                    page_status = getattr(page_resp, "status_code", 0)
                    page_text = getattr(page_resp, "text", "") or ""
                    self._log(f"邮箱验证码页加载状态: {page_status}, body_len={len(page_text)}")
                    if page_status not in (200, 304):
                        self._log(f"邮箱验证码页加载异常，仍继续尝试发送验证码: {page_status}", "warning")
                    csrf_match = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', page_text)
                    if csrf_match:
                        csrf_token = csrf_match.group(1)
                        self._log(f"从页面提取到 CSRF token: {csrf_token[:20]}...")
                except Exception as page_err:
                    self._log(f"邮箱验证码页加载异常，仍继续尝试发送验证码: {page_err}", "warning")
                finally:
                    self._email_otp_page_loaded = True

                # 给页面资源加载留一点时间（模拟浏览器行为，避免连续请求被风控）。
                time.sleep(1.5)

            last_error = ""
            referer_candidates = [password_referer]
            if email_verification_url not in referer_candidates:
                referer_candidates.append(email_verification_url)

            def _header_value(response: Any, name: str) -> str:
                """兼容 requests/curl_cffi，读取响应头。"""
                headers = getattr(response, "headers", {}) or {}
                try:
                    return str(headers.get(name) or headers.get(name.lower()) or "")
                except Exception:
                    return ""

            def _build_send_headers(referer: str) -> dict:
                """发送验证码接口需按 XHR 调用，避免服务端返回整页 HTML。"""
                headers = self._platform_json_headers(
                    device_id=self._device_id or "",
                    referer=referer,
                )
                if not self._device_id:
                    headers.pop("oai-device-id", None)
                if csrf_token:
                    headers["x-csrf-token"] = csrf_token
                return headers

            def _is_send_success(response: Any, *, method: str) -> bool:
                """判定发信接口是否真的触发，不能把最终网页误判为接口成功。"""
                status = getattr(response, "status_code", 0)
                resp_text = getattr(response, "text", "") or ""
                location = _header_value(response, "location")
                content_type = _header_value(response, "content-type")
                self._log(
                    f"{method} 验证码发送状态: {status} "
                    f"(location={location or '-'}, content_type={content_type or '-'})"
                )
                if resp_text:
                    self._log(f"{method} 验证码发送响应: {resp_text}")

                if status in (301, 302, 303, 307, 308):
                    if "/email-verification" in location:
                        self._log(f"{method} 验证码发送接口返回验证页重定向，判定已触发发信")
                        return True
                    return False

                if status == 200:
                    body = None
                    try:
                        body = response.json()
                    except Exception:
                        try:
                            body = json.loads(resp_text) if resp_text.strip().startswith(("{", "[")) else None
                        except Exception:
                            body = None
                    if isinstance(body, dict):
                        detail = body.get("detail") or body.get("error") or body.get("message") or ""
                        if detail:
                            self._log(f"{method} 验证码发送API返回消息: {detail}")
                        return True
                    self._log(f"{method} 验证码发送返回非 JSON 200，未确认发信，继续重试", "warning")
                return False

            for attempt, referer in enumerate(referer_candidates, start=1):
                send_headers = _build_send_headers(referer)

                try:
                    self._log(f"验证码发送请求: GET {send_url} referer={referer} allow_redirects=False")
                    response = self.session.get(
                        send_url,
                        headers=send_headers,
                        timeout=15,
                        allow_redirects=False,
                    )
                except Exception as req_err:
                    last_error = str(req_err)
                    self._log(f"验证码发送请求异常 (attempt {attempt}): {req_err}", "warning")
                    time.sleep(2)
                    continue

                if _is_send_success(response, method=f"GET attempt {attempt}"):
                    self._otp_sent_at = time.time()
                    return True

                status = getattr(response, "status_code", 0)
                resp_text = getattr(response, "text", "") or ""
                last_error = f"HTTP {status}: {resp_text}"
                self._log(
                    f"验证码发送失败 ({last_error})，{'切换 referer 重试' if attempt < len(referer_candidates) else '放弃'}",
                    "warning" if attempt < len(referer_candidates) else "error",
                )
                if attempt < len(referer_candidates):
                    time.sleep(3)

            # 非标准环境下 GET 可能被挡；保留 POST 兜底并完整记录响应。
            try:
                post_headers = self._platform_json_headers(
                    device_id=self._device_id or "",
                    referer=email_verification_url,
                )
                if not self._device_id:
                    post_headers.pop("oai-device-id", None)
                if csrf_token:
                    post_headers["x-csrf-token"] = csrf_token
                self._log(f"验证码发送兜底请求: POST {send_url} referer={email_verification_url} allow_redirects=False")
                response = self.session.post(
                    send_url,
                    headers=post_headers,
                    json={},
                    timeout=15,
                    allow_redirects=False,
                )
                if _is_send_success(response, method="POST"):
                    self._otp_sent_at = time.time()
                    return True
                last_error = f"HTTP {getattr(response, 'status_code', 0)}: {getattr(response, 'text', '') or ''}"
            except Exception as post_err:
                last_error = str(post_err)
                self._log(f"POST 验证码发送异常: {post_err}", "warning")



            if last_error:

                self._log(f"验证码发送最终失败: {last_error}", "error")



            return False



        except Exception as e:

            self._log(f"发送验证码失败: {e}", "error")

            return False



    def _get_verification_code(
        self,
        *,
        mark_invalid_on_timeout: bool = True,
        resend_on_timeout: bool = True,
    ) -> Optional[str]:

        """获取验证码；默认沿用旧链路超时重发，最新版主链可关闭重发。"""

        try:

            self._email_otp_failure_reason = ""

            email_id = self.email_info.get("service_id") if self.email_info else None

            import os as _os_otp_timeout

            try:

                otp_timeout = int(
                    (
                        _os_otp_timeout.environ.get("CHATGPT_OTP_TIMEOUT_SECONDS", "")
                        or str(CHATGPT_EMAIL_OTP_DEFAULT_TIMEOUT_SECONDS)
                    ).strip()
                )

            except Exception:

                otp_timeout = CHATGPT_EMAIL_OTP_DEFAULT_TIMEOUT_SECONDS

            try:

                max_attempts = int((_os_otp_timeout.environ.get("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "") or "3").strip())

            except Exception:

                max_attempts = 3

            if otp_timeout < CHATGPT_EMAIL_OTP_MIN_TIMEOUT_SECONDS:

                otp_timeout = CHATGPT_EMAIL_OTP_MIN_TIMEOUT_SECONDS

            max_attempts = max(1, min(max_attempts, 5))
            mailbox_transport_failed = False

            for attempt in range(1, max_attempts + 1):

                if attempt > 1:

                    if resend_on_timeout:

                        self._log(f"邮箱验证码 {otp_timeout}s 未收到，重新发送验证码 ({attempt}/{max_attempts})...")

                        if not self._send_verification_code():

                            self._log(f"第 {attempt}/{max_attempts} 次重发验证码失败", "warning")

                            continue

                    else:

                        self._log(f"邮箱验证码 {otp_timeout}s 未收到，继续等待已触发的验证码 ({attempt}/{max_attempts})...")

                elapsed_since_send = "?"

                if self._otp_sent_at:

                    elapsed_since_send = f"{time.time() - self._otp_sent_at:.0f}s"

                self._log(

                    f"正在等待邮箱 {self.email} 的验证码 "

                    f"(第 {attempt}/{max_attempts} 轮, 超时: {otp_timeout}s, OTP已发送: {elapsed_since_send}前)..."

                )

                try:

                    code = self.email_service.get_verification_code(

                        email=self.email,

                        email_id=email_id,

                        timeout=otp_timeout,

                        pattern=OTP_CODE_PATTERN,

                        otp_sent_at=self._otp_sent_at,

                    )

                except TimeoutError as exc:

                    if self._is_unrecoverable_mailbox_otp_error(exc):
                        self._log(f"邮箱账号不可读，停止等待验证码: {exc}", "error")
                        self._email_otp_exhausted = True
                        self._email_otp_failure_reason = "mailbox_account_not_found"
                        if mark_invalid_on_timeout:
                            self._mark_current_email_invalid("mailbox_account_not_found")
                        return None
                    if self._is_temporary_mailbox_otp_error(exc):
                        mailbox_transport_failed = True
                    self._log(f"第 {attempt}/{max_attempts} 轮等待验证码超时: {exc}", "warning")

                    code = None

                if code:

                    self._log(f"成功获取验证码: {code}")

                    return code

                self._log(

                    f"第 {attempt}/{max_attempts} 轮等待验证码超时",

                    "warning" if attempt < max_attempts else "error",

                )

            self._log(f"等待验证码超时，已尝试 {max_attempts} 轮", "error")
            self._email_otp_exhausted = True
            if mailbox_transport_failed:
                self._email_otp_failure_reason = "mailbox_transport_timeout"
                self._log("邮箱接口传输超时，已保留当前邮箱标签", "error")
                return None
            self._email_otp_failure_reason = "invalid_email_no_otp"
            if mark_invalid_on_timeout:
                self._mark_current_email_invalid("invalid_email_no_otp")

            return None



        except TimeoutError as e:

            self._log(f"等待验证码超时: {e}", "error")

            return None

        except Exception as e:

            self._log(f"获取验证码失败: {e}", "error")

            return None



    @staticmethod
    def _is_unrecoverable_mailbox_otp_error(exc: Exception) -> bool:
        """识别邮箱服务已明确判定当前邮箱不可读的验证码等待错误。"""
        text = str(exc or "").lower()
        if not text:
            return False
        markers = (
            "account_not_found",
            "账号不存在",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_temporary_mailbox_otp_error(exc: Exception) -> bool:
        """识别邮箱接口传输失败，避免将服务超时误判为邮箱未收到验证码。"""
        text = str(exc or "").lower()
        if "最后一次错误" not in text and "last error" not in text:
            return False
        markers = (
            "read timed out",
            "connectionpool",
            "connection aborted",
            "connection reset",
            "connection refused",
            "http 502",
            "http 503",
            "http 504",
            "bad gateway",
            "temporarily unavailable",
            "请求异常",
            "超过本轮截止时间",
        )
        return any(marker in text for marker in markers)



    def _email_otp_failure_message(self, fallback: str = "获取验证码失败") -> str:
        reason = str(getattr(self, "_email_otp_failure_reason", "") or "")
        if reason == "mailbox_account_not_found":
            return "邮箱账号不存在或不可读，已标记无效邮箱"
        if reason == "mailbox_transport_timeout":
            return "邮箱接口传输超时，已保留当前邮箱标签"
        if self._email_otp_exhausted:
            return "邮箱验证码三轮未收到，已标记无效邮箱"
        return fallback



    @staticmethod
    def _is_invalid_state_response(response) -> bool:
        """判断邮箱 OTP 校验是否因 OpenAI 会话 state 过期失败。"""
        text = str(getattr(response, "text", "") or "")
        if "invalid_state" in text or "no longer valid" in text:
            return True
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else {}
            if isinstance(error, dict):
                return str(error.get("code") or "") == "invalid_state"
        except Exception:
            return False
        return False

    @staticmethod
    def _is_invalid_auth_step_response(response) -> bool:
        """判断当前请求是否不符合 OpenAI 当前授权页面步骤。"""
        text = str(getattr(response, "text", "") or "").lower()
        if "invalid_auth_step" in text or "invalid authorization step" in text:
            return True
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else {}
            if isinstance(error, dict):
                return str(error.get("code") or "").lower() == "invalid_auth_step"
        except Exception:
            return False
        return False

    @staticmethod
    def _is_auth_error_url(url: str, code: str = "") -> bool:
        """判断 auth.openai.com 是否跳到了业务错误页。"""
        value = str(url or "")
        parsed = urllib.parse.urlsplit(value)
        if parsed.path.rstrip("/") != "/error":
            return False
        if not code:
            return True
        query = urllib.parse.parse_qs(parsed.query)
        payload = str((query.get("payload") or [""])[0] or "")
        if code in value:
            return True
        if payload:
            try:
                payload += "=" * (-len(payload) % 4)
                data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
                return str(data.get("errorCode") or "") == code
            except Exception:
                return False
        return False

    @staticmethod
    def _is_deleted_or_deactivated_account_response(response) -> bool:
        """判断 OpenAI 是否拒绝已删除/停用过的邮箱继续创建账号。"""
        marker = "deleted or deactivated"
        text = str(getattr(response, "text", "") or "").lower()
        if marker in text or "account_deactivated" in text:
            return True
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else {}
            code = str((error or {}).get("code") or "").lower() if isinstance(error, dict) else ""
            message = str((error or {}).get("message") or "").lower() if isinstance(error, dict) else ""
            return code == "account_deactivated" or marker in message
        except Exception:
            return False

    @staticmethod
    def _is_login_auth_rejected_error(message: str) -> bool:
        text = str(message or "").lower()
        return any(
            marker in text
            for marker in (
                "http_401",
                "http_403",
                "status=401",
                "status=403",
                " 401",
                " 403",
            )
        )

    def _delete_current_email_after_openai_reject(self, reason: str) -> None:
        """OpenAI 明确判定邮箱不可用后，调用邮箱 provider 清理当前邮箱。"""
        delete = getattr(self.email_service, "delete_current_email", None)
        if not callable(delete):
            self._log(f"当前邮箱服务不支持自动删除邮箱: {self.email}", "warning")
            return
        try:
            deleted = bool(delete(reason=reason))
            if deleted:
                self._log(f"已通过邮箱接口删除不可用邮箱: {self.email}", "warning")
            else:
                self._log(f"邮箱接口未删除不可用邮箱: {self.email}", "warning")
        except Exception as exc:
            self._log(f"删除不可用邮箱失败: {exc}", "error")

    def _mark_current_email_invalid(self, reason: str = "invalid_email_no_otp") -> list[str]:
        """邮箱连续多轮收不到验证码时打“无效邮箱”标签，令后续选池跳过。"""
        marker = getattr(getattr(self, "email_service", None), "mark_invalid_email", None)
        if not callable(marker):
            self._log(f"当前邮箱服务不支持无效邮箱打标: {self.email}", "warning")
            return []
        try:
            applied = list(marker(reason=reason) or [])
            if applied:
                self._log(f"邮箱无效打标完成: 当前邮箱 {self.email}; {', '.join(applied)}", "warning")
            else:
                self._log(f"邮箱无效打标未返回标签: {self.email}", "warning")
            return applied
        except Exception as exc:
            self._log(f"给无效邮箱打标失败: {exc}", "error")
            return []

    @staticmethod
    def _is_user_already_exists_response(response) -> bool:
        """Check if OpenAI rejects account creation because the email already exists."""
        text = str(getattr(response, "text", "") or "").lower()
        if "user_already_exists" in text or "an account already exists for this email" in text:
            return True
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else {}
            if isinstance(error, dict):
                code = str(error.get("code") or "").lower()
                message = str(error.get("message") or "").lower()
                if code == "user_already_exists" or "an account already exists for this email" in message:
                    return True
        except Exception:
            pass
        return False

    def _mark_parent_email_exhausted(self, reason: str = "openai_user_already_exists") -> list[str]:
        """Mark the parent mailbox as exhausted when OpenAI returns user_already_exists.

        This forces the mailbox provider to tag the parent email as "registered",
        so the next get_email() call skips it and allocates a new parent.
        """
        marker = getattr(getattr(self, "email_service", None), "mark_parent_exhausted", None)
        if not callable(marker):
            self._log("当前邮箱服务不支持标记父邮箱耗尽: " + str(self.email), "warning")
            return []
        try:
            applied = list(marker(reason=reason) or [])
            if applied:
                self._log("已标记父邮箱为别名已上限: " + ", ".join(applied), "warning")
            else:
                self._log("父邮箱耗尽标记未返回标签: " + str(self.email), "warning")
            return applied
        except Exception as exc:
            self._log(f"标记父邮箱耗尽失败: {exc}", "error")
            return []

    def _refresh_mailbox_before_ids(self) -> None:
        """刷新已见邮件集合，避免重发 OTP 后再次读到旧验证码。"""
        refresh = getattr(getattr(self, "email_service", None), "refresh_before_ids", None)
        if callable(refresh):
            try:
                seen = refresh()
                self._log(f"已刷新邮箱已见邮件集合: {len(seen or [])} 封")
            except Exception as exc:
                self._log(f"刷新邮箱已见邮件集合失败: {exc}", "warning")

    def _retry_email_otp_after_invalid_state(self) -> bool:
        """OpenAI 邮箱 OTP state 过期后，重建 OAuth 会话并重发一次验证码。"""
        try:
            self._log("邮箱 OTP 会话已失效，准备刷新 OAuth 会话并重发验证码...", "warning")
            self._refresh_mailbox_before_ids()

            self.http_client = OpenAIHTTPClient(proxy_url=self.proxy_url)
            self.protocol_fingerprint.apply_to_client(self.http_client)
            self.session = self.http_client.session
            self.oauth_start = None
            self._device_id = self.protocol_fingerprint.device_id
            self._sentinel_token = None
            self._signup_sentinel = None
            self._email_otp_continue_url = None
            self._email_otp_page_loaded = False
            self._otp_continue_url = None
            self._otp_page_type = None

            if not self._start_oauth():
                self._log("刷新 OAuth 会话失败", "error")
                return False
            did = self._get_device_id()
            if not did:
                self._log("刷新 OAuth 后获取 Device ID 失败", "error")
                return False
            sen_payload = self._check_sentinel(did)
            signup_result = self._submit_signup_form(did, sen_payload)
            if not signup_result.success:
                self._log(f"刷新 OTP 会话失败: {signup_result.error_message}", "error")
                return False
            if signup_result.page_type != OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
                self._log(f"刷新 OTP 会话后页面类型异常: {signup_result.page_type}", "warning")

            if not self._send_verification_code():
                return False
            next_code = self._get_verification_code()
            if not next_code:
                return False
            return self._validate_verification_code(next_code, allow_state_retry=False)
        except Exception as exc:
            self._log(f"刷新 OTP 会话重试失败: {exc}", "error")
            return False

    def _validate_verification_code(self, code: str, *, allow_state_retry: bool = True) -> bool:

        """验证验证码"""

        try:

            code_body = f'{{"code":"{code}"}}'



            response = self.session.post(

                OPENAI_API_ENDPOINTS["validate_otp"],

                headers={

                    "referer": "https://auth.openai.com/email-verification",

                    "accept": "application/json",

                    "content-type": "application/json",

                },

                data=code_body,

            )



            self._log(f"验证码校验状态: {response.status_code}")

            if response.status_code != 200:

                self._log(f"验证码校验响应: {response.text}", "warning")
                if allow_state_retry and self._is_invalid_state_response(response):
                    return self._retry_email_otp_after_invalid_state()

                return False



            # 解析响应，存储 continue_url 和 page_type

            try:

                resp_data = response.json()

                self._otp_continue_url = resp_data.get("continue_url", "")

                self._otp_page_type = resp_data.get("page", {}).get("type", "")

                self._log(f"验证码校验 -> page_type={self._otp_page_type}")

            except Exception:

                self._otp_continue_url = ""

                self._otp_page_type = ""

            return True



        except Exception as e:

            self._log(f"验证验证码失败: {e}", "error")

            return False



    def _create_user_account(self) -> bool:

        """创建用户账户"""

        try:
            self._last_create_account_error_code = ""

            user_info = generate_random_user_info()

            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")

            create_account_body = json.dumps(user_info)



            # 调 client_auth_session_dump 推进服务器 auth 状态机

            try:

                dump_resp = self.session.get(

                    "https://auth.openai.com/api/accounts/client_auth_session_dump",

                    headers={

                        "referer": "https://auth.openai.com/email-verification",

                        "accept": "application/json",

                    },

                    timeout=20,

                )

                self._log(f"client_auth_session_dump 状态: {dump_resp.status_code}")

            except Exception as e:

                self._log(f"client_auth_session_dump 异常: {e}", "warning")



            create_headers = {

                "referer": "https://auth.openai.com/about-you",

                "accept": "application/json",

                "content-type": "application/json",

                "origin": "https://auth.openai.com",

                "sec-fetch-site": "same-origin",

                **_generate_datadog_trace_headers(),

            }

            if self._device_id:

                create_headers["oai-device-id"] = self._device_id



            # create_account 也需要 sentinel token (flow=oauth_create_account)

            if self._device_id:

                ca_sentinel = self._check_sentinel(self._device_id, flow="oauth_create_account")

                if ca_sentinel:

                    create_headers["openai-sentinel-token"] = json.dumps({

                        "p": ca_sentinel.p,

                        "t": ca_sentinel.t,

                        "c": ca_sentinel.c,

                        "id": self._device_id,

                        "flow": ca_sentinel.flow,

                    }, separators=(",", ":"))

                    if ca_sentinel.so_token:

                        create_headers["openai-sentinel-so-token"] = ca_sentinel.so_token

                    self._log(
                        f"create_account Sentinel 已获取: "
                        f"flow={ca_sentinel.flow} t_len={len(ca_sentinel.t)} "
                        f"so={'yes' if ca_sentinel.so_token else 'no'}"
                    )



            response = self.session.post(

                OPENAI_API_ENDPOINTS["create_account"],

                headers=create_headers,

                data=create_account_body,

            )



            self._log(f"账户创建状态: {response.status_code}")



            if response.status_code != 200:

                self._log(f"账户创建失败: {response.text}", "warning")
                self._last_create_account_error_code = self._openai_error_code_from_payload(
                    self._response_json_dict(response)
                )
                if self._last_create_account_error_code == "registration_disallowed":
                    self._mark_parent_email_exhausted("registration_disallowed")

                if self._is_deleted_or_deactivated_account_response(response):
                    self._log("OpenAI 判定该邮箱关联账号已删除或停用，准备删除当前邮箱", "warning")
                    self._delete_current_email_after_openai_reject("openai_account_deleted_or_deactivated")

                if self._is_user_already_exists_response(response):
                    self._log("OpenAI 返回 user_already_exists，父邮箱别名配额已耗尽，标记父邮箱为别名已上限", "warning")
                    self._mark_parent_email_exhausted("openai_user_already_exists")
                    self._user_already_exists = True

                return False



            # 提取 continue_url（ChatGPT Web 流程直接返回 OAuth callback URL）

            try:

                resp_data = response.json()

                self._create_account_continue_url = resp_data.get("continue_url", "")

                if self._create_account_continue_url:

                    self._log(f"create_account continue_url: {self._create_account_continue_url}")

            except Exception:

                pass



            return True



        except Exception as e:

            self._log(f"创建账户失败: {e}", "error")

            return False



    def _acquire_codex_callback(self) -> Optional[str]:

        """

        注册完成后，通过 Codex CLI OAuth 完整登录流程获取 callback URL。

        使用新 session，走 authorize → authorize/continue → OTP → callback 流程。

        """

        try:

            from .constants import (

                CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE,

                OPENAI_AUTH, OPENAI_API_ENDPOINTS,

            )

            import urllib.parse



            self._log("开始 Codex CLI 登录流程...")



            # 1. 创建新 HTTP client + session

            login_client = OpenAIHTTPClient(proxy_url=self.proxy_url)
            self.protocol_fingerprint.apply_to_client(login_client)

            login_session = login_client.session



            # 2. 生成 Codex CLI OAuth URL (Hydra)

            codex_oauth = generate_oauth_url(

                redirect_uri=CODEX_REDIRECT_URI,

                scope=CODEX_SCOPE,

                client_id=CODEX_CLIENT_ID,

            )

            self._codex_oauth = codex_oauth



            # 3. 访问 authorize URL 获取 device_id + session cookies

            response = login_session.get(codex_oauth.auth_url, timeout=15)

            did = login_session.cookies.get("oai-did")

            self._log(f"Codex login device_id: {did}")

            if not did:

                self._log("Codex login 获取 device_id 失败", "error")

                return None



            # 4. 获取 Sentinel token

            sen_payload = None

            try:

                ua = login_client.default_headers.get("User-Agent", "")

                generator = _SentinelTokenGenerator(did, ua, client_version=self._latest_chatgpt_client_version(), profile=self.protocol_profile)

                sent_p = generator.generate_requirements_token()

                sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": "authorize_continue"}, separators=(",", ":"))



                sen_resp = login_client.post(

                    OPENAI_API_ENDPOINTS["sentinel"],

                    headers={

                        "origin": "https://sentinel.openai.com",

                        "referer": self._latest_chatgpt_sentinel_frame_url(),

                        "content-type": "text/plain;charset=UTF-8",

                    },

                    data=sen_req_body,

                )

                if sen_resp.status_code == 200:

                    data = sen_resp.json()

                    turnstile = data.get("turnstile") or {}

                    pow_meta = data.get("proofofwork") or {}

                    if pow_meta.get("required") and pow_meta.get("seed"):

                        sent_p = generator.generate_token(

                            str(pow_meta.get("seed") or ""),

                            str(pow_meta.get("difficulty") or "0"),

                        )

                    t_raw = turnstile.get("dx", "")

                    t_val = ""

                    if t_raw:

                        try:

                            t_val = generator.decrypt_turnstile(t_raw, sent_p)

                        except Exception:

                            pass

                    sen_payload = SentinelPayload(p=sent_p, t=t_val, c=str(data.get("token") or ""), flow="authorize_continue")

                    self._log("Codex login Sentinel 已获取")

            except Exception as e:

                self._log(f"Codex login Sentinel 失败: {e}", "warning")



            # 5. authorize/continue 提交邮箱（登录已有账号）

            signup_body = f'{{"username":{{"value":"{self.email}","kind":"email"}},"screen_hint":"login"}}'

            headers = {

                "referer": "https://auth.openai.com/log-in",

                "accept": "application/json",

                "content-type": "application/json",

            }

            if sen_payload:

                headers["openai-sentinel-token"] = json.dumps({

                    "p": sen_payload.p, "t": sen_payload.t, "c": sen_payload.c,

                    "id": did, "flow": sen_payload.flow,

                }, separators=(",", ":"))



            resp = login_session.post(OPENAI_API_ENDPOINTS["signup"], headers=headers, data=signup_body)

            self._log(f"Codex login authorize/continue: {resp.status_code}")

            if resp.status_code != 200:

                self._log(f"Codex login authorize/continue 失败: {resp.text}", "error")

                return None



            resp_data = resp.json()

            page_type = resp_data.get("page", {}).get("type", "")

            self._log(f"Codex login page_type: {page_type}")



            # 6. 如果需要 OTP，等待第二次验证码

            if page_type == "email_otp_verification":

                login_session.get(OPENAI_API_ENDPOINTS["send_otp"], headers={

                    "referer": f"{OPENAI_AUTH}/email-verification",

                }, timeout=15)

                self._log("Codex login OTP 已显式发送")

                self._log("等待第二次验证码...")

                self._otp_sent_at = time.time()

                code = self._get_verification_code(mark_invalid_on_timeout=False)

                if not code:

                    self._log("Codex login 获取验证码失败", "error")

                    return None



                # 验证 OTP

                code_body = f'{{"code":"{code}"}}'

                otp_resp = login_session.post(

                    OPENAI_API_ENDPOINTS["validate_otp"],

                    headers={

                        "referer": "https://auth.openai.com/email-verification",

                        "accept": "application/json",

                        "content-type": "application/json",

                    },

                    data=code_body,

                )

                self._log(f"Codex login OTP 校验: {otp_resp.status_code}")

                if otp_resp.status_code != 200:

                    self._log(f"Codex login OTP 失败: {otp_resp.text}", "error")

                    return None



                otp_data = otp_resp.json()

                otp_page = otp_data.get("page", {}).get("type", "")

                self._log(f"Codex login OTP -> page_type={otp_page}")



                if otp_page == "add_phone":

                    self._log("Codex CLI 登录仍需 add_phone，无法跳过", "error")

                    return None



            # 7. 需要密码登录

            elif page_type in ("login_password", "create_account_password"):

                self._log(f"Codex login 提交密码...")

                if not self.password:

                    self._log("无密码可用", "error")

                    return None



                # 加载密码页获取 sentinel

                login_session.get(f"{OPENAI_AUTH}/log-in/password", timeout=15)

                pwd_sentinel = None

                try:

                    ua2 = login_client.default_headers.get("User-Agent", "")

                    gen2 = _SentinelTokenGenerator(did, ua2, profile=self.protocol_profile)

                    sp2 = gen2.generate_requirements_token()

                    sr2 = json.dumps({"p": sp2, "id": did, "flow": "login_password"}, separators=(",", ":"))

                    sr2_resp = login_client.post(

                        OPENAI_API_ENDPOINTS["sentinel"],

                        headers={"origin": "https://sentinel.openai.com", "referer": self._latest_chatgpt_sentinel_frame_url(), "content-type": "text/plain;charset=UTF-8"},

                        data=sr2,

                    )

                    if sr2_resp.status_code == 200:

                        d2 = sr2_resp.json()

                        pm2 = d2.get("proofofwork") or {}

                        if pm2.get("required") and pm2.get("seed"):

                            sp2 = gen2.generate_token(str(pm2.get("seed") or ""), str(pm2.get("difficulty") or "0"))

                        tr2 = (d2.get("turnstile") or {}).get("dx", "")

                        tv2 = ""

                        if tr2:

                            try: tv2 = gen2.decrypt_turnstile(tr2, sp2)

                            except: pass

                        pwd_sentinel = SentinelPayload(p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="login_password")

                        self._log("Codex login 密码 Sentinel 已获取")

                except Exception as e:

                    self._log(f"Codex login 密码 Sentinel 失败: {e}", "warning")



                pwd_headers = {

                    "origin": OPENAI_AUTH,

                    "referer": f"{OPENAI_AUTH}/log-in/password",

                    "accept": "application/json",

                    "content-type": "application/json",

                }

                if did:

                    pwd_headers["oai-device-id"] = did

                if pwd_sentinel:

                    pwd_headers["openai-sentinel-token"] = json.dumps({

                        "p": pwd_sentinel.p, "t": pwd_sentinel.t, "c": pwd_sentinel.c,

                        "id": did, "flow": pwd_sentinel.flow,

                    }, separators=(",", ":"))



                pwd_body = json.dumps({"password": self.password, "username": self.email})

                pwd_resp = login_session.post(OPENAI_API_ENDPOINTS["register"], headers=pwd_headers, data=pwd_body)

                self._log(f"Codex login 密码提交: {pwd_resp.status_code}")

                if pwd_resp.status_code != 200:

                    self._log(f"Codex login 密码失败: {pwd_resp.text}", "error")

                    return None



                pwd_data = pwd_resp.json()

                pwd_page = pwd_data.get("page", {}).get("type", "")

                self._log(f"Codex login 密码 -> page_type={pwd_page}")



                # 密码后可能需要 OTP

                if pwd_page == "email_otp_verification" or pwd_page == "email_otp_send":

                    login_session.get(OPENAI_API_ENDPOINTS["send_otp"], headers={

                        "referer": f"{OPENAI_AUTH}/email-verification",

                    }, timeout=15)

                    self._log("Codex login OTP 已显式发送")

                    self._log("Codex login: 等待验证码...")

                    self._otp_sent_at = time.time()

                    code = self._get_verification_code(mark_invalid_on_timeout=False)

                    if not code:

                        self._log("Codex login 获取验证码失败", "error")

                        return None

                    code_body = f'{{"code":"{code}"}}'

                    otp_resp = login_session.post(

                        OPENAI_API_ENDPOINTS["validate_otp"],

                        headers={"referer": f"{OPENAI_AUTH}/email-verification", "accept": "application/json", "content-type": "application/json"},

                        data=code_body,

                    )

                    self._log(f"Codex login OTP: {otp_resp.status_code}")

                    if otp_resp.status_code != 200:

                        self._log(f"Codex login OTP 失败: {otp_resp.text}", "error")

                        return None

                    otp_data = otp_resp.json()

                    otp_page = otp_data.get("page", {}).get("type", "")

                    self._log(f"Codex login OTP -> page_type={otp_page}")

                    if otp_page == "add_phone":

                        self._log("Codex CLI 登录仍需 add_phone", "error")

                        return None



            # 8. 重新访问 authorize URL 获取回调

            self._log("Codex login: 重新访问 OAuth URL 获取回调...")

            response = login_session.get(codex_oauth.auth_url, allow_redirects=False, timeout=15)

            max_redirects = 10

            current_url = codex_oauth.auth_url

            for i in range(max_redirects):

                if response.status_code not in (301, 302, 303, 307, 308):

                    break

                location = response.headers.get("Location", "")

                if not location:

                    break

                next_url = urllib.parse.urljoin(current_url, location)

                self._log(f"Codex login 重定向 {i+1}: {next_url}")

                if "code=" in next_url and "state=" in next_url:

                    self._log("找到 Codex CLI 回调 URL")

                    return next_url

                current_url = next_url

                response = login_session.get(current_url, allow_redirects=False, timeout=15)



            self._log(f"Codex login 最终: status={response.status_code}, url={current_url}", "warning")

            return None



        except Exception as e:

            self._log(f"Codex CLI 登录流程失败: {e}", "error")

            return None



    def _build_platform_oauth_start(self, email: str, device_id: str) -> OAuthStart:

        """构造 platform.openai.com OAuth 授权 URL，参考 chatgpt2api 协议注册。"""

        import urllib.parse

        from .constants import OPENAI_AUTH

        code_verifier, code_challenge = _generate_pkce_pair()

        state = secrets.token_urlsafe(32)

        params = {

            "issuer": OPENAI_AUTH,

            "client_id": PLATFORM_OAUTH_CLIENT_ID,

            "audience": PLATFORM_OAUTH_AUDIENCE,

            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,

            "device_id": device_id,

            "screen_hint": "login_or_signup",

            "max_age": "0",

            "login_hint": email,

            "scope": PLATFORM_OAUTH_SCOPE,

            "response_type": "code",

            "response_mode": "query",

            "state": state,

            "nonce": secrets.token_urlsafe(32),

            "code_challenge": code_challenge,

            "code_challenge_method": "S256",

            "auth0Client": PLATFORM_AUTH0_CLIENT,

        }

        return OAuthStart(

            auth_url=f"{OPENAI_AUTH}/api/accounts/authorize?{urllib.parse.urlencode(params)}",

            state=state,

            code_verifier=code_verifier,

            redirect_uri=PLATFORM_OAUTH_REDIRECT_URI,

            client_id=PLATFORM_OAUTH_CLIENT_ID,

        )


    def _platform_nav_headers(self, *, referer: str = "") -> dict:

        """Platform OAuth 导航请求头。"""

        default_ua = PLATFORM_REFERENCE_USER_AGENT
        http_client = getattr(self, "http_client", None)
        default_headers = getattr(http_client, "default_headers", {}) if http_client else {}
        fingerprint = getattr(self, "protocol_fingerprint", None)
        user_agent = default_headers.get("User-Agent") or default_ua

        headers = {

            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",

            "accept-language": getattr(fingerprint, "accept_language", "en-US,en;q=0.9"),

            "user-agent": user_agent,

            "sec-ch-ua": getattr(fingerprint, "sec_ch_ua", PLATFORM_REFERENCE_SEC_CH_UA),

            "sec-ch-ua-arch": getattr(fingerprint, "sec_ch_ua_arch", '"x86_64"'),

            "sec-ch-ua-bitness": getattr(fingerprint, "sec_ch_ua_bitness", '"64"'),

            "sec-ch-ua-full-version-list": getattr(fingerprint, "sec_ch_ua_full", PLATFORM_REFERENCE_SEC_CH_UA_FULL),

            "sec-ch-ua-mobile": getattr(fingerprint, "sec_ch_ua_mobile", "?0"),

            "sec-ch-ua-model": getattr(fingerprint, "sec_ch_ua_model", '""'),

            "sec-ch-ua-platform": getattr(fingerprint, "sec_ch_ua_platform", '"Windows"'),

            "sec-ch-ua-platform-version": getattr(fingerprint, "sec_ch_ua_platform_version", '"10.0.0"'),

            "sec-fetch-dest": "document",

            "sec-fetch-mode": "navigate",

            "sec-fetch-site": "same-origin",

            "upgrade-insecure-requests": "1",

        }

        if referer:

            headers["referer"] = referer

        return headers


    def _platform_json_headers(self, *, device_id: str, referer: str) -> dict:

        """Platform OAuth JSON 请求头。"""

        from .constants import OPENAI_AUTH

        default_ua = PLATFORM_REFERENCE_USER_AGENT
        http_client = getattr(self, "http_client", None)
        default_headers = getattr(http_client, "default_headers", {}) if http_client else {}
        fingerprint = getattr(self, "protocol_fingerprint", None)
        user_agent = default_headers.get("User-Agent") or default_ua

        headers = {

            "accept": "application/json",

            "accept-language": getattr(fingerprint, "accept_language", "en-US,en;q=0.9"),

            "content-type": "application/json",

            "origin": OPENAI_AUTH,

            "priority": "u=1, i",

            "referer": referer,

            "user-agent": user_agent,

            "oai-device-id": device_id,

            "sec-ch-ua": getattr(fingerprint, "sec_ch_ua", PLATFORM_REFERENCE_SEC_CH_UA),

            "sec-ch-ua-arch": getattr(fingerprint, "sec_ch_ua_arch", '"x86_64"'),

            "sec-ch-ua-bitness": getattr(fingerprint, "sec_ch_ua_bitness", '"64"'),

            "sec-ch-ua-full-version-list": getattr(fingerprint, "sec_ch_ua_full", PLATFORM_REFERENCE_SEC_CH_UA_FULL),

            "sec-ch-ua-mobile": getattr(fingerprint, "sec_ch_ua_mobile", "?0"),

            "sec-ch-ua-model": getattr(fingerprint, "sec_ch_ua_model", '""'),

            "sec-ch-ua-platform": getattr(fingerprint, "sec_ch_ua_platform", '"Windows"'),

            "sec-ch-ua-platform-version": getattr(fingerprint, "sec_ch_ua_platform_version", '"10.0.0"'),

            "sec-fetch-dest": "empty",

            "sec-fetch-mode": "cors",

            "sec-fetch-site": "same-origin",

        }

        headers.update(_generate_datadog_trace_headers())

        return headers


    def _build_sentinel_payload_for_client(self, client: OpenAIHTTPClient, device_id: str, flow: str) -> SentinelPayload:

        """为独立 Platform 登录 session 生成单次 Sentinel payload。"""

        ua = client.default_headers.get("User-Agent", "")
        accept_language = str(client.default_headers.get("Accept-Language") or client.default_headers.get("accept-language") or "")

        try:

            generator = _SentinelTokenGenerator(device_id, ua, client_version=self._latest_chatgpt_client_version(), profile=self.protocol_profile)

            sent_p = generator.generate_requirements_token()

            response = client.post(

                OPENAI_API_ENDPOINTS["sentinel"],

                headers=self._latest_chatgpt_sentinel_headers(user_agent=ua, accept_language=accept_language),

                data=json.dumps({"p": sent_p, "id": device_id, "flow": flow}, separators=(",", ":")),

            )

            if response.status_code != 200:

                raise RuntimeError(f"sentinel_req_failed_{response.status_code}")

            data = response.json()

            token = str(data.get("token") or "").strip()

            if not token:

                raise RuntimeError("sentinel_req_no_token")

            pow_meta = data.get("proofofwork") or {}

            initial_p = sent_p

            if pow_meta.get("required") and pow_meta.get("seed"):

                sent_p = generator.generate_token(str(pow_meta.get("seed") or ""), str(pow_meta.get("difficulty") or "0"))

            t_value = ""

            dx_b64 = str((data.get("turnstile") or {}).get("dx") or "")

            if dx_b64:

                try:

                    from .sentinel_vm import solve_turnstile_dx

                    t_value = solve_turnstile_dx(dx_b64, initial_p, user_agent=ua, sdk_url=self._latest_chatgpt_sentinel_sdk_url())

                except Exception as exc:

                    self._log(f"Platform Sentinel VM 失败: {exc}", "warning")

            so_meta = data.get("so") or {}
            so_token = ""
            need_t = bool(dx_b64 and not t_value)
            need_so = bool(isinstance(so_meta, dict) and so_meta.get("required"))
            if need_so:
                so_token = self._solve_session_observer_token(
                    device_id=device_id,
                    flow=flow,
                    challenge=data if isinstance(data, dict) else {},
                    request_p=initial_p,
                    user_agent=ua,
                )
            if need_t or (need_so and not so_token):
                quickjs_payload = self._quickjs_sentinel_payload(
                    getattr(client, "session", None),
                    device_id,
                    flow=flow,
                    user_agent=ua,
                    accept_language=accept_language or self._latest_chatgpt_accept_language(),
                    label="Platform 注册链路 VM/PoW t/so 补齐",
                )
                if quickjs_payload:
                    if need_t and quickjs_payload.t:
                        t_value = quickjs_payload.t
                        if quickjs_payload.p:
                            sent_p = quickjs_payload.p
                        if quickjs_payload.c:
                            token = quickjs_payload.c
                    if need_so and not so_token and quickjs_payload.so_token:
                        so_token = quickjs_payload.so_token

            return SentinelPayload(p=sent_p, t=t_value, c=token, flow=flow, so_token=so_token)

        except Exception as exc:

            self._log(f"Platform Sentinel VM/PoW 失败，尝试 QuickJS 兜底: {exc}", "warning")

            quickjs_payload = self._quickjs_sentinel_payload(
                getattr(client, "session", None),
                device_id,
                flow=flow,
                user_agent=ua,
                accept_language=accept_language or self._latest_chatgpt_accept_language(),
                label="Platform 注册链路 VM/PoW 失败后",
            )

            if quickjs_payload:

                return quickjs_payload

            raise


    def _build_sentinel_header_for_client(self, client: OpenAIHTTPClient, device_id: str, flow: str) -> str:

        """为独立 Platform 登录 session 生成 Sentinel header。"""

        payload = self._build_sentinel_payload_for_client(client, device_id, flow)

        return self._sentinel_payload_header(payload, device_id)


    @staticmethod
    def _set_oai_did_for_session(session, device_id: str) -> None:

        """给独立登录 session 写入 oai-did，减少 device_id 丢失。"""

        for domain in (".auth.openai.com", "auth.openai.com"):

            try:

                session.cookies.set("oai-did", device_id, domain=domain, path="/")

            except TypeError:

                try:

                    session.cookies.set("oai-did", device_id, domain=domain)

                except Exception:

                    pass

            except Exception:

                pass


    def _send_platform_login_otp(self, client: OpenAIHTTPClient) -> bool:

        """独立 Platform 登录触发邮箱 OTP 发送。"""

        from .constants import OPENAI_AUTH

        try:

            resp = client.session.get(

                OPENAI_API_ENDPOINTS["send_otp"],

                headers=self._platform_nav_headers(referer=f"{OPENAI_AUTH}/email-verification"),

                allow_redirects=True,

                timeout=15,

            )

            ok = int(getattr(resp, "status_code", 0) or 0) in (200, 302)

            self._log(f"Platform 登录验证码发送状态: {getattr(resp, 'status_code', 0)}")

            if ok:

                self._otp_sent_at = time.time()

            return ok

        except Exception as exc:

            self._log(f"Platform 登录发送验证码失败: {exc}", "warning")

            return False


    def _wait_platform_login_code(self, client: OpenAIHTTPClient) -> Optional[str]:

        """等待独立 Platform 登录邮箱 OTP；10 秒未到则重发，最多 3 轮。"""

        import os as _os_otp_timeout

        try:

            otp_timeout = int(
                (
                    _os_otp_timeout.environ.get("CHATGPT_OTP_TIMEOUT_SECONDS", "")
                    or str(CHATGPT_EMAIL_OTP_DEFAULT_TIMEOUT_SECONDS)
                ).strip()
            )

        except Exception:

            otp_timeout = CHATGPT_EMAIL_OTP_DEFAULT_TIMEOUT_SECONDS

        try:

            max_attempts = int((_os_otp_timeout.environ.get("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "") or "3").strip())

        except Exception:

            max_attempts = 3

        otp_timeout = max(CHATGPT_EMAIL_OTP_MIN_TIMEOUT_SECONDS, otp_timeout)

        max_attempts = max(1, min(max_attempts, 5))

        email_id = self.email_info.get("service_id") if self.email_info else None

        for attempt in range(1, max_attempts + 1):

            if attempt > 1 and not self._send_platform_login_otp(client):

                self._log(f"Platform 登录第 {attempt}/{max_attempts} 次重发验证码失败", "warning")

                continue

            elapsed_since_send = "?"

            if self._otp_sent_at:

                elapsed_since_send = f"{time.time() - self._otp_sent_at:.0f}s"

            self._log(

                f"Platform 登录等待邮箱 {self.email} 验证码 "

                f"(第 {attempt}/{max_attempts} 轮, 超时: {otp_timeout}s, OTP已发送: {elapsed_since_send}前)..."

            )

            try:

                code = self.email_service.get_verification_code(

                    email=self.email,

                    email_id=email_id,

                    timeout=otp_timeout,

                    pattern=OTP_CODE_PATTERN,

                    otp_sent_at=self._otp_sent_at,

                )

            except TimeoutError as exc:

                self._log(f"Platform 登录第 {attempt}/{max_attempts} 轮等待验证码超时: {exc}", "warning")

                code = None

            if code:

                self._log(f"Platform 登录获取验证码: {code}")

                return code

        return None


    def _validate_platform_login_otp(self, client: OpenAIHTTPClient, device_id: str, code: str):

        """校验独立 Platform 登录邮箱 OTP；失败时补 Sentinel 再试一次。"""

        from .constants import OPENAI_AUTH

        headers = self._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/email-verification")

        response = client.session.post(

            OPENAI_API_ENDPOINTS["validate_otp"],

            headers=headers,

            data=json.dumps({"code": code}, separators=(",", ":")),

            timeout=15,

        )

        if response.status_code == 200:

            return response

        if self._is_deleted_or_deactivated_account_response(response):

            self._log("Platform 登录验证码校验返回账号已删除或停用，保留首次响应不重复提交 OTP", "warning")

            return response

        try:

            headers["openai-sentinel-token"] = self._build_sentinel_header_for_client(client, device_id, "authorize_continue")

            response = client.session.post(

                OPENAI_API_ENDPOINTS["validate_otp"],

                headers=headers,

                data=json.dumps({"code": code}, separators=(",", ":")),

                timeout=15,

            )

        except Exception as exc:

            self._log(f"Platform 登录验证码补 Sentinel 失败: {exc}", "warning")

        return response


    def _platform_request_with_retry(self, session, method: str, url: str, retry_attempts: int = 3, **kwargs):

        """照搬 chatgpt2api 注册器的本地重试封装，网络抖动时最多重试数次。"""

        last_error = ""

        for _ in range(max(1, retry_attempts)):

            try:

                kwargs.setdefault("timeout", 30)

                return session.request(method.upper(), url, **kwargs), ""

            except Exception as error:

                last_error = str(error)

                time.sleep(1)

        return None, last_error


    def _platform_reference_authorize(self, client: OpenAIHTTPClient, device_id: str) -> OAuthStart:

        """按 openai_register.py 的 platform authorize 方式初始化注册会话。"""

        oauth_start = self._build_platform_oauth_start(self.email or "", device_id)

        self.oauth_start = oauth_start

        self._set_oai_did_for_session(client.session, device_id)

        self._log("开始 platform authorize")

        self._log(f"platform authorize URL: {oauth_start.auth_url}")

        resp, error = self._platform_request_with_retry(

            client.session,

            "get",

            oauth_start.auth_url,

            headers=self._platform_nav_headers(referer=f"{PLATFORM_BASE}/"),

            allow_redirects=True,

        )

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        final_url = getattr(resp, "url", "") if resp is not None else ""

        self._platform_authorize_final_url = str(final_url or "")

        self._log(f"platform authorize 状态: {status}, final_url={final_url}")

        if resp is None or getattr(resp, "status_code", 0) != 200:

            body = getattr(resp, "text", "") if resp is not None else ""
            if is_cloudflare_managed_challenge_html(body):
                raise CloudflareManagedChallengeError(status=status, final_url=str(final_url or ""))

            raise RuntimeError(error or f"platform_authorize_http_{status}: {body}")

        self._log("platform authorize 完成")

        return oauth_start


    def _platform_reference_prepare_existing_login_otp(self, client: OpenAIHTTPClient, device_id: str) -> None:

        """已注册账号走 ChatGPT NextAuth 登录入口，让系统自动触发首封 OTP。"""

        from .constants import CHATGPT_APP

        if not self.email:

            raise RuntimeError("platform_existing_login_missing_email")

        self._is_existing_account = True

        self._log("检测到已注册账号登录页，准备登录邮箱验证码流程")

        client.session.get(f"{CHATGPT_APP}/", timeout=15)

        client.session.get(f"{CHATGPT_APP}/api/auth/providers", timeout=15)

        csrf_resp = client.session.get(f"{CHATGPT_APP}/api/auth/csrf", timeout=15)

        csrf_token = ""

        try:

            csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()

        except Exception:

            csrf_token = ""

        if not csrf_token:

            csrf_cookie = str(client.session.cookies.get("__Host-next-auth.csrf-token", "") or "")

            csrf_token = csrf_cookie.split("%7C")[0] if "%7C" in csrf_cookie else csrf_cookie.split("|")[0]

        if not csrf_token:

            raise RuntimeError("existing_login_missing_csrf")

        query = urllib.parse.urlencode(

            {

                "prompt": "login",

                "ext-passkey-client-capabilities": "11111",

                "ext-oai-did": device_id,

                "auth_session_logging_id": self.protocol_fingerprint.auth_session_logging_id,

                "screen_hint": "login_or_signup",

                "login_hint": self.email,

            }

        )

        body = urllib.parse.urlencode(

            {

                "callbackUrl": f"{CHATGPT_APP}/",

                "csrfToken": csrf_token,

                "json": "true",

            }

        )

        resp, error = self._platform_request_with_retry(

            client.session,

            "post",

            f"{CHATGPT_APP}/api/auth/signin/openai?{query}",

            headers={

                "accept": "application/json",

                "content-type": "application/x-www-form-urlencoded",

                "origin": CHATGPT_APP,

                "referer": f"{CHATGPT_APP}/",

                "sec-fetch-site": "same-origin",

            },

            data=body,

            allow_redirects=True,

        )

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        text = getattr(resp, "text", "") if resp is not None else ""

        self._log(f"signin/openai 登录入口状态: {status}")

        if text:

            self._log(f"signin/openai 登录入口响应: {text}")

        if resp is None or getattr(resp, "status_code", 0) != 200:

            raise RuntimeError(error or f"existing_login_signin_openai_http_{status}: {text}")

        try:

            signin_data = resp.json() or {}

        except Exception:

            signin_data = {}

        auth_url = str(signin_data.get("url") or "").strip()

        if not auth_url:

            raise RuntimeError(f"existing_login_signin_openai_missing_url: {text}")

        self._log(f"signin/openai 返回授权 URL: {auth_url}")

        auth_resp, auth_error = self._platform_request_with_retry(

            client.session,

            "get",

            auth_url,

            headers={

                **self._platform_nav_headers(referer=f"{CHATGPT_APP}/"),

                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",

                "sec-fetch-dest": "document",

                "sec-fetch-mode": "navigate",

                "sec-fetch-site": "none",

            },

            allow_redirects=True,

        )

        auth_status = getattr(auth_resp, "status_code", "unknown") if auth_resp is not None else "none"

        auth_final_url = str(getattr(auth_resp, "url", "") or "") if auth_resp is not None else ""

        self._log(f"signin/openai 授权跳转状态: {auth_status}, final_url={auth_final_url}")

        if auth_resp is None or getattr(auth_resp, "status_code", 0) != 200:

            auth_text = getattr(auth_resp, "text", "") if auth_resp is not None else ""

            raise RuntimeError(auth_error or f"existing_login_authorize_http_{auth_status}: {auth_text}")

        if self._is_auth_error_url(auth_final_url):

            raise RuntimeError(f"existing_login_authorize_error: final_url={auth_final_url}")

        if "email-verification" not in auth_final_url and "email-otp" not in auth_final_url:

            self._log(f"signin/openai 授权跳转未进入邮箱验证码页: {auth_final_url}", "warning")

        self._otp_sent_at = time.time()

        self._otp_page_type = "email_otp_verification"

        self._log("signin/openai 授权跳转已触发已注册账号邮箱验证码，先等待首封邮件")


    def _platform_reference_resend_otp(self, client: OpenAIHTTPClient) -> None:

        """已注册账号邮件未到时，按 HAR 使用 resend 重新发送。"""

        from .constants import OPENAI_AUTH

        self._log("开始重新发送登录邮箱验证码")

        resp, error = self._platform_request_with_retry(

            client.session,

            "post",

            f"{OPENAI_AUTH}/api/accounts/email-otp/resend",

            headers=self._platform_json_headers(device_id=self._device_id or "", referer=f"{OPENAI_AUTH}/email-verification"),

            data="",

            allow_redirects=False,

        )

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        text = getattr(resp, "text", "") if resp is not None else ""

        self._log(f"登录验证码重发状态: {status}")

        self._log(f"登录验证码重发响应: {text}")

        if resp is None or getattr(resp, "status_code", 0) != 200:

            raise RuntimeError(error or f"resend_otp_http_{status}: {text}")

        try:

            payload = resp.json() or {}

        except Exception:

            payload = {}

        if payload.get("success") is False:

            raise RuntimeError(f"resend_otp_failed: {text}")

        self._otp_sent_at = time.time()

        self._log("重新发送登录邮箱验证码完成")


    def _platform_reference_register_user(self, client: OpenAIHTTPClient, device_id: str) -> bool:

        """按 openai_register.py 提交注册密码。"""

        from .constants import OPENAI_AUTH

        if not self.email or not self.password:

            raise RuntimeError("platform_register_missing_email_or_password")

        self._log("开始提交注册密码")

        headers = self._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/create-account/password")

        headers["openai-sentinel-token"] = self._build_sentinel_header_for_client(

            client,

            device_id,

            "username_password_create",

        )

        body = json.dumps({"username": self.email, "password": self.password}, separators=(",", ":"))

        resp, error = self._platform_request_with_retry(

            client.session,

            "post",

            OPENAI_API_ENDPOINTS["register"],

            headers=headers,

            data=body,

        )

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        text = getattr(resp, "text", "") if resp is not None else ""

        self._log(f"提交注册密码状态: {status}")

        self._log(f"提交注册密码响应: {text}")

        if resp is None or getattr(resp, "status_code", 0) != 200:

            try:

                data = resp.json() if resp is not None else {}

            except Exception:

                data = {}

            if isinstance(data, dict) and data.get("message") == "Failed to create account. Please try again.":

                self._log("注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "warning")

            if resp is not None and self._is_invalid_auth_step_response(resp):

                self._is_existing_account = True

                self._log("提交注册密码返回 invalid_auth_step，判定当前邮箱已进入登录流程，跳过密码并继续发送邮箱验证码", "warning")

                return False

            raise RuntimeError(error or f"user_register_http_{status}: {text}")

        try:

            payload = resp.json() or {}

            continue_url = str(payload.get("continue_url") or "").strip()

            if continue_url:

                self._email_otp_continue_url = continue_url

        except Exception:

            pass

        self._log("提交注册密码完成")

        return True


    def _platform_reference_send_otp(self, client: OpenAIHTTPClient) -> None:

        """照 openai_register.py：GET email-otp/send，允许跳转到验证页。"""

        from .constants import OPENAI_AUTH

        self._log("开始发送验证码")

        referer = f"{OPENAI_AUTH}/email-verification" if self._is_existing_account else f"{OPENAI_AUTH}/create-account/password"

        resp, error = self._platform_request_with_retry(

            client.session,

            "get",

            OPENAI_API_ENDPOINTS["send_otp"],

            headers=self._platform_nav_headers(referer=referer),

            allow_redirects=True,

        )

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        final_url = getattr(resp, "url", "") if resp is not None else ""

        text = getattr(resp, "text", "") if resp is not None else ""

        self._log(f"验证码发送状态: {status}, final_url={final_url}")

        self._log(f"验证码发送响应: {text}")

        if resp is None or getattr(resp, "status_code", 0) not in (200, 302):

            raise RuntimeError(error or f"send_otp_http_{status}: {text}")

        if self._is_auth_error_url(str(final_url or ""), "invalid_auth_step"):

            raise RuntimeError(f"send_otp_invalid_auth_step: final_url={final_url}")

        self._otp_sent_at = time.time()

        self._log("发送验证码完成")


    def _wait_platform_reference_register_code(self, client: OpenAIHTTPClient) -> Optional[str]:

        """等待 platform 注册验证码；沿用本项目三轮 10s 规则，重发仍走参照 send_otp。"""

        import os as _os_otp_timeout

        try:

            otp_timeout = int(
                (
                    _os_otp_timeout.environ.get("CHATGPT_OTP_TIMEOUT_SECONDS", "")
                    or str(CHATGPT_EMAIL_OTP_DEFAULT_TIMEOUT_SECONDS)
                ).strip()
            )

        except Exception:

            otp_timeout = CHATGPT_EMAIL_OTP_DEFAULT_TIMEOUT_SECONDS

        try:

            max_attempts = int((_os_otp_timeout.environ.get("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "") or "3").strip())

        except Exception:

            max_attempts = 3

        otp_timeout = max(CHATGPT_EMAIL_OTP_MIN_TIMEOUT_SECONDS, otp_timeout)

        max_attempts = max(1, min(max_attempts, 5))

        email_id = self.email_info.get("service_id") if self.email_info else None

        for attempt in range(1, max_attempts + 1):

            if attempt > 1:

                self._log(f"邮箱验证码 {otp_timeout}s 未收到，按 platform 参照流程重发 ({attempt}/{max_attempts})...")

                try:

                    if self._is_existing_account:

                        self._platform_reference_resend_otp(client)

                    else:

                        self._platform_reference_send_otp(client)

                except Exception as exc:

                    self._log(f"第 {attempt}/{max_attempts} 次重发验证码失败: {exc}", "warning")

                    continue

            elapsed_since_send = "?"

            if self._otp_sent_at:

                elapsed_since_send = f"{time.time() - self._otp_sent_at:.0f}s"

            self._log(

                f"正在等待邮箱 {self.email} 的验证码 "

                f"(第 {attempt}/{max_attempts} 轮, 超时: {otp_timeout}s, OTP已发送: {elapsed_since_send}前)..."

            )

            try:

                code = self.email_service.get_verification_code(

                    email=self.email,

                    email_id=email_id,

                    timeout=otp_timeout,

                    pattern=OTP_CODE_PATTERN,

                    otp_sent_at=self._otp_sent_at,

                )

            except TimeoutError as exc:

                self._log(f"第 {attempt}/{max_attempts} 轮等待验证码超时: {exc}", "warning")

                code = None

            if code:

                self._log(f"成功获取验证码: {code}")

                return code

            self._log(

                f"第 {attempt}/{max_attempts} 轮等待验证码超时",

                "warning" if attempt < max_attempts else "error",

            )

        self._log(f"等待验证码超时，已尝试 {max_attempts} 轮", "error")

        self._email_otp_exhausted = True

        self._mark_current_email_invalid("invalid_email_no_otp")

        return None


    def _platform_reference_validate_otp(self, client: OpenAIHTTPClient, device_id: str, code: str) -> dict:

        """按 openai_register.py 校验邮箱验证码；首次失败补 authorize_continue Sentinel。"""

        self._log(f"开始校验验证码 {code}")

        resp = self._validate_platform_login_otp(client, device_id, code)

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        text = getattr(resp, "text", "") if resp is not None else ""

        self._log(f"验证码校验状态: {status}")

        self._log(f"验证码校验响应: {text}")

        if resp is None or getattr(resp, "status_code", 0) != 200:

            raise RuntimeError(f"validate_otp_http_{status}_body={text}")

        try:

            payload = resp.json() or {}

        except Exception:

            payload = {}

        self._otp_page_type = str(((payload.get("page") or {}).get("type")) or "")

        continue_url = str(payload.get("continue_url") or "").strip()

        if continue_url:

            self._create_account_continue_url = continue_url

        self._log("验证码校验完成")

        return payload


    def _platform_reference_create_account(self, client: OpenAIHTTPClient, device_id: str) -> None:

        """按 openai_register.py 创建账号资料。"""

        from .constants import OPENAI_AUTH

        user_info = generate_random_user_info()

        self._log(f"开始创建账号资料: {user_info.get('name')}, 生日: {user_info.get('birthdate')}")

        headers = self._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/about-you")

        create_sentinel = self._build_sentinel_payload_for_client(client, device_id, "oauth_create_account")

        headers["openai-sentinel-token"] = self._sentinel_payload_header(create_sentinel, device_id)

        if create_sentinel.so_token:

            headers["openai-sentinel-so-token"] = create_sentinel.so_token

        resp, error = self._platform_request_with_retry(

            client.session,

            "post",

            OPENAI_API_ENDPOINTS["create_account"],

            headers=headers,

            data=json.dumps(user_info, separators=(",", ":")),

        )

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        text = getattr(resp, "text", "") if resp is not None else ""

        self._log(f"创建账号资料状态: {status}")

        self._log(f"创建账号资料响应: {text}")

        if resp is None or getattr(resp, "status_code", 0) not in (200, 302):

            if resp is not None and self._is_deleted_or_deactivated_account_response(resp):

                self._log("OpenAI 判定该邮箱关联账号已删除或停用，准备删除当前邮箱", "warning")

                self._delete_current_email_after_openai_reject("openai_account_deleted_or_deactivated")

            if resp is not None and self._is_user_already_exists_response(resp):

                self._log("OpenAI 返回 user_already_exists，父邮箱别名配额已耗尽，标记父邮箱为别名已上限", "warning")

                self._mark_parent_email_exhausted("openai_user_already_exists")

                raise RuntimeError("EMAIL_ALIAS_PARENT_EXHAUSTED: user_already_exists - parent email alias quota exhausted")

            raise RuntimeError(error or f"create_account_http_{status}: {text}")

        try:

            payload = resp.json() or {}

        except Exception:

            payload = {}

        continue_url = str(payload.get("continue_url") or "").strip()

        if continue_url:

            self._create_account_continue_url = continue_url

            self._log(f"create_account continue_url: {continue_url}")

        self._log("创建账号资料完成")


    def _preferred_k12_workspace_id_from_payload(self, payload: dict) -> str:
        auth_session = payload.get("oai-client-auth-session") if isinstance(payload, dict) else {}
        auth_session = auth_session if isinstance(auth_session, dict) else {}
        workspaces = auth_session.get("workspaces") or []
        workspace_items = [item for item in workspaces if isinstance(item, dict)]
        workspace_ids = [str(item.get("id") or "").strip() for item in workspace_items if str(item.get("id") or "").strip()]
        configured_ids: list[str] = []
        try:
            from platforms.chatgpt.k12_join import parse_workspace_ids

            configured_ids = parse_workspace_ids(str(getattr(self, "k12_workspace_ids", "") or ""))
        except Exception:
            configured_ids = []
        for configured_id in configured_ids:
            if configured_id in workspace_ids:
                return configured_id
        for item in workspace_items:
            if str(item.get("kind") or "").strip().lower() == "organization":
                workspace_id = str(item.get("id") or "").strip()
                if workspace_id:
                    return workspace_id
        return workspace_ids[0] if workspace_ids else ""

    def _is_existing_k12_workspace_payload(self, payload: dict) -> bool:
        """OTP response is already on the auth.openai.com workspace selection branch."""
        if not isinstance(payload, dict):
            return False
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        page_type = str(page.get("type") or "").strip().lower()
        continue_url = str(payload.get("continue_url") or "").strip()
        parsed = urllib.parse.urlsplit(continue_url) if continue_url else None
        is_auth_workspace_url = bool(
            parsed
            and parsed.netloc.endswith("auth.openai.com")
            and parsed.path.rstrip("/") in {"/workspace", "/choose-an-account"}
        )
        if page_type in {"workspace", "workspace_selection", "organization_selection"} or is_auth_workspace_url:
            return True

        workspace_id = self._preferred_k12_workspace_id_from_payload(payload)
        if not workspace_id:
            return False
        try:
            from platforms.chatgpt.k12_join import parse_workspace_ids

            configured_ids = parse_workspace_ids(str(getattr(self, "k12_workspace_ids", "") or ""))
        except Exception:
            configured_ids = []
        return bool(configured_ids and workspace_id in configured_ids)

    @staticmethod
    def _chatgpt_callback_url_from_payload(payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""
        candidates = [
            str(payload.get("continue_url") or "").strip(),
        ]
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        candidates.append(str(page_payload.get("url") or "").strip())
        for candidate in candidates:
            if _extract_oauth_callback_params_from_url(candidate):
                return candidate
        return ""

    def _platform_reference_complete_existing_callback_session(
        self,
        client: OpenAIHTTPClient,
        validate_payload: dict,
    ) -> tuple[dict, str]:
        """已注册账号 OTP 返回 ChatGPT callback 时，直接落地 NextAuth session。"""
        from .constants import CHATGPT_APP, OPENAI_AUTH

        if not client or not client.session:
            return {}, ""
        session = client.session
        callback_url = self._chatgpt_callback_url_from_payload(validate_payload)
        if not callback_url:
            return {}, _cookies_to_header(session.cookies)
        referer = str((validate_payload or {}).get("continue_url") or f"{OPENAI_AUTH}/email-verification")
        try:
            callback_resp = session.get(
                callback_url,
                headers=self._platform_nav_headers(referer=referer),
                allow_redirects=True,
                timeout=45,
            )
            self._log(
                "已注册账号 ChatGPT callback 跟随状态 "
                f"{getattr(callback_resp, 'status_code', 0)}, url={getattr(callback_resp, 'url', '')}"
            )
        except Exception as exc:
            self._log(f"已注册账号 ChatGPT callback 跟随失败: {exc}", "warning")
            return {}, _cookies_to_header(session.cookies)

        try:
            session.get(f"{CHATGPT_APP}/", timeout=15)
        except Exception:
            pass
        session_resp = session.get(
            f"{CHATGPT_APP}/api/auth/session",
            headers={"accept": "application/json"},
            timeout=20,
        )
        self._log(f"已注册账号 ChatGPT session API 状态 {getattr(session_resp, 'status_code', 0)}")
        try:
            session_data = session_resp.json() or {}
        except Exception:
            session_data = {}
        if isinstance(session_data, dict) and session_data.get("accessToken"):
            self._log("已注册账号 ChatGPT Web session 获取成功")
            return session_data, _cookies_to_header(session.cookies)
        keys = list(session_data.keys()) if isinstance(session_data, dict) else type(session_data).__name__
        self._log(f"已注册账号 ChatGPT Web session 未返回 accessToken: keys={keys}", "warning")
        return session_data if isinstance(session_data, dict) else {}, _cookies_to_header(session.cookies)


    def _platform_reference_complete_existing_k12_session(
        self,
        client: OpenAIHTTPClient,
        device_id: str,
        validate_payload: dict,
    ) -> tuple[dict, str, str]:
        """已注册账号 OTP 后直接完成 ChatGPT Web session，供 K12 exchange 使用。"""
        from .constants import CHATGPT_APP, OPENAI_AUTH

        if not client or not client.session:
            return {}, "", ""
        session = client.session
        referer = str((validate_payload or {}).get("continue_url") or f"{OPENAI_AUTH}/workspace")
        workspace_id = self._preferred_k12_workspace_id_from_payload(validate_payload or {})
        callback_url = ""
        if workspace_id:
            try:
                ws_resp = session.post(
                    OPENAI_API_ENDPOINTS["select_workspace"],
                    headers=self._platform_json_headers(device_id=device_id, referer=referer),
                    data=json.dumps({"workspace_id": workspace_id}, separators=(",", ":")),
                    allow_redirects=False,
                    timeout=30,
                )
                self._log(
                    "已注册账号 K12 workspace/select "
                    f"workspace={workspace_id[:8]} 状态: {getattr(ws_resp, 'status_code', 0)}"
                )
                next_url = str((getattr(ws_resp, "headers", {}) or {}).get("Location") or "").strip()
                ws_data = {}
                if not next_url:
                    try:
                        ws_data = ws_resp.json() or {}
                    except Exception:
                        ws_data = {}
                    if isinstance(ws_data, dict):
                        next_url = str(ws_data.get("continue_url") or "").strip()
                        if not next_url:
                            org_url = self._select_first_organization_for_nextauth(
                                ws_data,
                                device_id=device_id,
                                referer=referer,
                            )
                            if org_url:
                                next_url = org_url
                if next_url:
                    next_url = urllib.parse.urljoin(referer, next_url)
                    callback_url = (
                        next_url
                        if _extract_oauth_callback_params_from_url(next_url)
                        else self._follow_platform_redirects_for_callback(session, next_url)
                    )
            except Exception as exc:
                self._log(f"已注册账号 K12 workspace/select 失败: {exc}", "warning")

        if not callback_url:
            callback_url = self._resolve_chatgpt_nextauth_callback_via_workspace_select(
                device_id=device_id,
                referer=referer,
            )
        if callback_url:
            callback_resp = session.get(
                callback_url,
                headers=self._platform_nav_headers(referer=referer),
                allow_redirects=True,
                timeout=45,
            )
            self._log(
                "已注册账号 K12 ChatGPT callback 跟随状态 "
                f"{getattr(callback_resp, 'status_code', 0)}, url={getattr(callback_resp, 'url', '')}"
            )
        else:
            self._log("已注册账号 K12 未取得 ChatGPT callback URL", "warning")

        try:
            session.get(f"{CHATGPT_APP}/", timeout=15)
        except Exception:
            pass
        session_resp = session.get(
            f"{CHATGPT_APP}/api/auth/session",
            headers={"accept": "application/json"},
            timeout=20,
        )
        self._log(f"已注册账号 K12 ChatGPT session API 状态 {getattr(session_resp, 'status_code', 0)}")
        try:
            session_data = session_resp.json() or {}
        except Exception:
            session_data = {}
        if isinstance(session_data, dict) and session_data.get("accessToken"):
            self._log("已注册账号 K12 ChatGPT Web session 获取成功")
            return session_data, _cookies_to_header(session.cookies), workspace_id
        keys = list(session_data.keys()) if isinstance(session_data, dict) else type(session_data).__name__
        self._log(f"已注册账号 K12 ChatGPT Web session 未返回 accessToken: keys={keys}", "warning")
        return session_data if isinstance(session_data, dict) else {}, _cookies_to_header(session.cookies), workspace_id


    def _finish_existing_k12_platform_reference_result(
        self,
        result: RegistrationResult,
        chatgpt_session: dict,
        chatgpt_cookies: str,
        workspace_id: str = "",
        chatgpt_session_source: str = "existing_login_workspace_select",
    ) -> RegistrationResult:
        access_token = str(chatgpt_session.get("accessToken") or chatgpt_session.get("access_token") or "").strip()
        session_token = str(chatgpt_session.get("sessionToken") or chatgpt_session.get("session_token") or "").strip()
        chatgpt_user = chatgpt_session.get("user") if isinstance(chatgpt_session.get("user"), dict) else {}
        result.success = bool(access_token)
        result.email = self.email or ""
        result.password = self.password or ""
        result.account_id = _extract_chatgpt_account_id(access_token) or str(chatgpt_user.get("id") or "").strip()
        result.workspace_id = workspace_id or ""
        result.access_token = access_token
        result.refresh_token = ""
        result.id_token = access_token
        result.session_token = session_token
        result.source = "login"
        result.metadata = {
            "email_service": getattr(getattr(self.email_service, "service_type", None), "value", ""),
            "proxy_used": self.proxy_url,
            "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "auth_source": "platform_reference_existing_k12_login",
            "registration_refresh_token": "",
            "registration_refresh_token_usable": False,
            "refresh_token_source": "",
            "cookies": chatgpt_cookies,
            "login_state_cookie": chatgpt_cookies,
            "cookie_header": chatgpt_cookies,
            "profile": chatgpt_user,
            "expires_at": str(chatgpt_session.get("expires") or "") if isinstance(chatgpt_session, dict) else "",
            "session": chatgpt_session,
            "chatgpt_session_source": chatgpt_session_source,
            "k12_workspace_id": workspace_id or "",
        }
        if not access_token:
            result.error_message = "已注册账号 K12 ChatGPT Web session 获取失败"
        return result


    def _run_platform_reference_register(self, result: RegistrationResult) -> RegistrationResult:

        """按 E:\\AI\\chatgpt2api\\services\\register\\openai_register.py 主链注册。"""

        client = OpenAIHTTPClient(proxy_url=self.proxy_url)
        self.protocol_fingerprint.apply_to_client(client)

        self.http_client = client

        self.session = client.session

        device_id = self.protocol_fingerprint.device_id

        self._device_id = device_id

        self._set_oai_did_for_session(self.session, device_id)

        oauth_start = self._platform_reference_authorize(client, device_id)

        if not self.password:

            self.password = self._generate_password()

        result.password = self.password

        self._refresh_mailbox_before_ids()

        existing_login_url = "/log-in/password" in str(self._platform_authorize_final_url or "")

        if existing_login_url:

            self._platform_reference_prepare_existing_login_otp(client, device_id)

        else:

            password_submitted = self._platform_reference_register_user(client, device_id)

            if not password_submitted:

                self._log("已切换到已注册账号邮箱验证码流程")

                self._platform_reference_prepare_existing_login_otp(client, device_id)

            else:

                self._platform_reference_send_otp(client)

        validate_payload = {}

        otp_already_completed = bool(self._create_account_continue_url and "code=" in str(self._create_account_continue_url))

        if otp_already_completed:

            self._log("已获得 OAuth callback，跳过邮箱验证码等待")

        else:

            code = self._wait_platform_reference_register_code(client)

            if not code:

                result.error_message = self._email_otp_failure_message()

                return result

            validate_payload = self._platform_reference_validate_otp(client, device_id, code)

        if (
            self._is_existing_account
            and getattr(self, "k12_join_enabled", False)
            and self._is_existing_k12_workspace_payload(validate_payload if isinstance(validate_payload, dict) else {})
        ):

            self._log("已注册账号 K12 模式：跳过 Platform OAuth，直接建立 ChatGPT Web session")

            chatgpt_session, chatgpt_cookies, selected_workspace_id = self._platform_reference_complete_existing_k12_session(
                client,
                device_id,
                validate_payload if isinstance(validate_payload, dict) else {},
            )

            return self._finish_existing_k12_platform_reference_result(
                result,
                chatgpt_session,
                chatgpt_cookies,
                selected_workspace_id,
            )

        if self._is_existing_account and getattr(self, "k12_join_enabled", False):

            chatgpt_session, chatgpt_cookies = self._platform_reference_complete_existing_callback_session(
                client,
                validate_payload if isinstance(validate_payload, dict) else {},
            )

            if isinstance(chatgpt_session, dict) and chatgpt_session.get("accessToken"):

                self._log("已注册账号普通 ChatGPT callback 已完成，后续继续执行 K12 join")

                return self._finish_existing_k12_platform_reference_result(
                    result,
                    chatgpt_session,
                    chatgpt_cookies,
                    "",
                    "existing_login_callback",
                )

            self._log("已注册账号未取得 ChatGPT callback session，回退到 Platform OAuth 换 token", "warning")

        if self._is_existing_account:

            self._log("已注册账号验证码通过，跳过创建账号资料")

        else:

            self._platform_reference_create_account(client, device_id)

        if self._is_existing_account:

            continue_url = oauth_start.auth_url

            self._log("已注册账号登录完成，重新进入 Platform OAuth 授权换 token")

        else:

            continue_url = self._create_account_continue_url or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        token_info = self._complete_platform_oauth(client, device_id, oauth_start, continue_url)

        if not token_info:

            result.error_message = "token换取失败"

            return result

        access_token = str(token_info.get("access_token") or "").strip()

        refresh_token = str(token_info.get("refresh_token") or "").strip()

        id_token = str(token_info.get("id_token") or "").strip()

        payload = _decode_jwt_payload_no_verify(id_token) or _decode_jwt_payload_no_verify(access_token)

        account_id = _extract_chatgpt_account_id(access_token) or str(payload.get("sub") or "").strip()
        chatgpt_session, chatgpt_cookies = self._establish_chatgpt_web_session_for_platform_reference()
        chatgpt_user = chatgpt_session.get("user") if isinstance(chatgpt_session.get("user"), dict) else {}
        chatgpt_session_token = str(
            chatgpt_session.get("sessionToken") or chatgpt_session.get("session_token") or ""
        ).strip()

        result.success = True

        result.email = self.email or ""

        result.password = self.password or ""

        result.account_id = account_id

        result.access_token = access_token

        result.refresh_token = ""

        result.id_token = id_token
        if chatgpt_session_token:
            result.session_token = chatgpt_session_token

        result.source = "register"

        service_type = getattr(getattr(self.email_service, "service_type", None), "value", "")

        result.metadata = {

            "email_service": service_type,

            "proxy_used": self.proxy_url,

            "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),

            "auth_source": "platform_reference_register",

            "openai_register_reference": r"E:\AI\chatgpt2api\services\register\openai_register.py",

            "registration_refresh_token": refresh_token,

            "registration_refresh_token_usable": False,

            "refresh_token_source": "",
            "cookies": chatgpt_cookies,
            "login_state_cookie": chatgpt_cookies,
            "cookie_header": chatgpt_cookies,
            "profile": chatgpt_user,
            "expires_at": str(chatgpt_session.get("expires") or "") if isinstance(chatgpt_session, dict) else "",
            "session": chatgpt_session,
            "chatgpt_session_source": "nextauth_after_platform_reference",

        }

        self._log("=" * 60)

        self._log("注册成功! (platform reference)")

        self._log(f"邮箱: {result.email}")

        self._log(f"Account ID: {result.account_id}")

        self._log("=" * 60)

        return result


    def _decode_client_auth_session_cookie(self, session) -> dict:

        """解码 oai-client-auth-session，取得 workspace/org 选择所需信息。"""

        raw = ""

        try:

            raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")

        except Exception:

            try:

                raw = session.cookies.get("oai-client-auth-session")

            except Exception:

                raw = ""

        if not raw:

            return {}

        try:

            first_part = str(raw).split(".")[0]

            first_part += "=" * (-len(first_part) % 4)

            data = json.loads(base64.urlsafe_b64decode(first_part.encode("ascii")).decode("utf-8"))

            return data if isinstance(data, dict) else {}

        except Exception:

            return {}


    def _follow_platform_redirects_for_callback(self, session, start_url: str, *, max_redirects: int = 10) -> str:

        """跟随 Platform OAuth 重定向链，寻找带 code 的 callback。"""

        import urllib.parse

        current_url = start_url

        for idx in range(max_redirects):

            response = session.get(current_url, headers=self._platform_nav_headers(), allow_redirects=False, timeout=30)

            callback_params = _extract_oauth_callback_params_from_url(str(getattr(response, "url", "") or ""))

            if callback_params:

                return str(getattr(response, "url", "") or "")

            location = str(response.headers.get("Location") or "").strip()

            if not location:

                break

            next_url = urllib.parse.urljoin(current_url, location)

            if _extract_oauth_callback_params_from_url(next_url):

                return next_url

            if response.status_code not in (301, 302, 303, 307, 308):

                break

            current_url = next_url

            self._log(f"Platform OAuth 重定向 {idx + 1}: {current_url}")

        return ""


    def _complete_platform_oauth(self, client: OpenAIHTTPClient, device_id: str, oauth_start: OAuthStart, continue_url: str) -> Optional[dict]:

        """完成 Platform OAuth consent/workspace 选择并换 token。"""

        import urllib.parse

        from .constants import OPENAI_AUTH

        from .oauth import submit_callback_url

        session = client.session

        current_url = continue_url

        if current_url.startswith("/"):

            current_url = urllib.parse.urljoin(OPENAI_AUTH, current_url)

        if not current_url:

            current_url = f"{OPENAI_AUTH}/sign-in-with-chatgpt/codex/consent"

        callback_url = current_url if _extract_oauth_callback_params_from_url(current_url) else ""

        if not callback_url:

            callback_url = self._follow_platform_redirects_for_callback(session, current_url)

        if not callback_url:

            auth_session = self._decode_client_auth_session_cookie(session)

            workspaces = list(auth_session.get("workspaces") or [])

            workspace_id = str(((workspaces[0] if workspaces else {}) or {}).get("id") or "").strip()

            if workspace_id:

                headers = self._platform_json_headers(device_id=device_id, referer=current_url)

                ws_resp = session.post(

                    f"{OPENAI_AUTH}/api/accounts/workspace/select",

                    headers=headers,

                    data=json.dumps({"workspace_id": workspace_id}, separators=(",", ":")),

                    allow_redirects=False,

                    timeout=30,

                )

                self._log(f"Platform workspace/select 状态: {ws_resp.status_code}")

                location = str(ws_resp.headers.get("Location") or "").strip()

                ws_data = {}

                if not location:

                    try:

                        ws_data = ws_resp.json() or {}

                    except Exception:

                        ws_data = {}

                    location = str(ws_data.get("continue_url") or "").strip()

                if location:

                    location = urllib.parse.urljoin(current_url, location)

                    if _extract_oauth_callback_params_from_url(location):

                        callback_url = location

                    else:

                        callback_url = self._follow_platform_redirects_for_callback(session, location)

                if not callback_url and ws_data:

                    orgs = list((((ws_data.get("data") or {}).get("orgs")) or []))

                    if orgs and orgs[0].get("id"):

                        org_body = {"org_id": str(orgs[0].get("id") or "").strip()}

                        projects = list(orgs[0].get("projects") or [])

                        if projects and projects[0].get("id"):

                            org_body["project_id"] = str(projects[0].get("id") or "").strip()

                        org_resp = session.post(

                            f"{OPENAI_AUTH}/api/accounts/organization/select",

                            headers=self._platform_json_headers(device_id=device_id, referer=current_url),

                            data=json.dumps(org_body, separators=(",", ":")),

                            allow_redirects=False,

                            timeout=30,

                        )

                        self._log(f"Platform organization/select 状态: {org_resp.status_code}")

                        location = str(org_resp.headers.get("Location") or "").strip()

                        if location:

                            callback_url = self._follow_platform_redirects_for_callback(session, urllib.parse.urljoin(current_url, location))

        if not callback_url:

            self._log("Platform OAuth 未取得 callback code", "warning")

            return None

        token_json = submit_callback_url(

            callback_url=callback_url,

            expected_state=oauth_start.state,

            code_verifier=oauth_start.code_verifier,

            redirect_uri=PLATFORM_OAUTH_REDIRECT_URI,

            client_id=PLATFORM_OAUTH_CLIENT_ID,

            proxy_url=self.proxy_url,

        )

        token_info = json.loads(token_json)

        if not token_info.get("access_token") or not token_info.get("refresh_token"):

            self._log("Platform OAuth token 缺少 access_token/refresh_token", "warning")

            return None

        token_info["type"] = "platform"

        return token_info


    def _establish_chatgpt_web_session_for_platform_reference(self) -> tuple[dict, str]:
        """Platform 注册完成后补建 chatgpt.com NextAuth session，供 K12 workspace 切换使用。"""
        from .constants import CHATGPT_APP

        if not self.session:
            return {}, ""
        try:
            auth_url = ""
            max_oauth_attempts = 3
            for attempt in range(1, max_oauth_attempts + 1):
                if self._start_oauth():
                    auth_url = str(getattr(self.oauth_start, "auth_url", "") or "").strip()
                    if auth_url:
                        break
                    self._log(
                        "Platform reference: ChatGPT NextAuth OAuth URL 为空 "
                        f"({attempt}/{max_oauth_attempts})",
                        "warning",
                    )
                else:
                    self._log(
                        "Platform reference: ChatGPT NextAuth OAuth URL 获取失败 "
                        f"({attempt}/{max_oauth_attempts})",
                        "warning",
                    )
                if attempt < max_oauth_attempts:
                    self._log(
                        "Platform reference: ChatGPT NextAuth OAuth URL 获取失败，"
                        f"2s 后重试 ({attempt + 1}/{max_oauth_attempts})",
                        "warning",
                    )
                    time.sleep(2)
            if not auth_url:
                self._log("Platform reference: ChatGPT NextAuth OAuth URL 获取失败，已达到最大重试次数", "warning")
                return {}, _cookies_to_header(self.session.cookies)
            auth_url = self._add_login_hint_to_auth_url(auth_url)

            callback_resp = self.session.get(
                auth_url,
                headers=self._platform_nav_headers(referer=f"{CHATGPT_APP}/"),
                allow_redirects=True,
                timeout=45,
            )
            self._log(
                "Platform reference: ChatGPT NextAuth 回调状态 "
                f"{getattr(callback_resp, 'status_code', 0)}, url={getattr(callback_resp, 'url', '')}"
            )
            callback_url = ""
            callback_resp_url = str(getattr(callback_resp, "url", "") or "")
            if "email-verification" in callback_resp_url or "email-otp" in callback_resp_url:
                self._otp_sent_at = time.time()
                self._otp_page_type = "email_otp_verification"
                self._log("Platform reference: ChatGPT NextAuth 需要邮箱验证码，等待第二封验证码")
                code = self._wait_platform_login_code(self.http_client)
                if code:
                    otp_payload = self._platform_reference_validate_otp(
                        self.http_client,
                        self._device_id or self._protocol_device_id(),
                        code,
                    )
                    callback_url = self._chatgpt_callback_url_from_payload(otp_payload)
                    if not callback_url:
                        callback_url = self._resolve_chatgpt_nextauth_callback_via_workspace_select(
                            device_id=self._device_id or "",
                            referer=callback_resp_url or auth_url,
                        )
                else:
                    self._log("Platform reference: ChatGPT NextAuth 邮箱验证码未获取，无法建立 Web session", "warning")
            else:
                callback_url = self._resolve_chatgpt_nextauth_callback(
                    response=callback_resp,
                    device_id=self._device_id or "",
                    referer=auth_url,
                )
            if callback_url:
                callback_resp = self.session.get(
                    callback_url,
                    headers=self._platform_nav_headers(referer=str(getattr(callback_resp, "url", "") or auth_url)),
                    allow_redirects=True,
                    timeout=45,
                )
                self._log(
                    "Platform reference: ChatGPT NextAuth callback 跟随状态 "
                    f"{getattr(callback_resp, 'status_code', 0)}, url={getattr(callback_resp, 'url', '')}"
                )
            try:
                self.session.get(f"{CHATGPT_APP}/", timeout=15)
            except Exception:
                pass
            session_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/session",
                headers={"accept": "application/json"},
                timeout=20,
            )
            self._log(f"Platform reference: ChatGPT session API 状态 {session_resp.status_code}")
            try:
                session_data = session_resp.json() or {}
            except Exception:
                session_data = {}
            if isinstance(session_data, dict) and session_data.get("accessToken"):
                self._log("Platform reference: ChatGPT Web session 获取成功")
                return session_data, _cookies_to_header(self.session.cookies)
            keys = list(session_data.keys()) if isinstance(session_data, dict) else type(session_data).__name__
            self._log(f"Platform reference: ChatGPT Web session 未返回 accessToken: keys={keys}", "warning")
            return session_data if isinstance(session_data, dict) else {}, _cookies_to_header(self.session.cookies)
        except Exception as exc:
            self._log(f"Platform reference: ChatGPT Web session 建立失败: {exc}", "warning")
            return {}, _cookies_to_header(self.session.cookies)

    def _add_login_hint_to_auth_url(self, auth_url: str) -> str:
        """给 chatgpt.com NextAuth authorize URL 补 login_hint，减少 choose-an-account 分支。"""
        if not self.email:
            return auth_url
        try:
            parsed = urllib.parse.urlsplit(auth_url)
            params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            keys = {key for key, _value in params}
            if "login_hint" not in keys:
                params.append(("login_hint", self.email))
            if "screen_hint" not in keys:
                params.append(("screen_hint", "login"))
            query = urllib.parse.urlencode(params)
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
        except Exception:
            return auth_url

    def _resolve_chatgpt_nextauth_callback(self, *, response, device_id: str, referer: str) -> str:
        """处理 choose-an-account / workspace-select，返回 chatgpt.com callback URL。"""
        current_url = str(getattr(response, "url", "") or "")
        if _extract_oauth_callback_params_from_url(current_url):
            return current_url
        if "choose-an-account" not in current_url and "workspace" not in current_url:
            return ""
        callback_url = self._resolve_chatgpt_nextauth_callback_via_workspace_select(
            device_id=device_id,
            referer=current_url or referer,
        )
        if callback_url:
            return callback_url
        return self._submit_first_auth_form_for_callback(response=response, referer=referer)

    def _resolve_chatgpt_nextauth_callback_via_workspace_select(self, *, device_id: str, referer: str) -> str:
        """在 choose-an-account 后用 auth session dump 的 workspace 选择继续授权。"""
        from .constants import OPENAI_AUTH

        if not self.session:
            return ""
        workspace_id = ""
        try:
            dump_resp = self.session.get(
                f"{OPENAI_AUTH}/api/accounts/client_auth_session_dump",
                headers={"accept": "application/json", "referer": referer},
                allow_redirects=False,
                timeout=20,
            )
            dump = dump_resp.json() if getattr(dump_resp, "text", "") else {}
            if isinstance(dump, dict):
                workspace_id = self._workspace_id_from_auth_payload(dump)
            self._log(
                "Platform reference: client_auth_session_dump "
                f"状态 {getattr(dump_resp, 'status_code', 0)}, workspace={workspace_id[:8] if workspace_id else '-'}"
            )
        except Exception as exc:
            self._log(f"Platform reference: client_auth_session_dump 失败: {exc}", "warning")
        if not workspace_id:
            workspace_id = self._workspace_id_from_auth_payload(self._decode_client_auth_session_cookie(self.session))
        if not workspace_id:
            return ""
        try:
            ws_resp = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers=self._platform_json_headers(device_id=device_id or self._protocol_device_id(), referer=referer),
                data=json.dumps({"workspace_id": workspace_id}, separators=(",", ":")),
                allow_redirects=False,
                timeout=30,
            )
            self._log(f"Platform reference: ChatGPT workspace/select 状态 {getattr(ws_resp, 'status_code', 0)}")
            next_url = str((getattr(ws_resp, "headers", {}) or {}).get("Location") or "").strip()
            if not next_url:
                try:
                    data = ws_resp.json() or {}
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    next_url = str(data.get("continue_url") or "").strip()
                    if not next_url:
                        org_url = self._select_first_organization_for_nextauth(
                            data,
                            device_id=device_id,
                            referer=referer,
                        )
                        if org_url:
                            next_url = org_url
            next_url = urllib.parse.urljoin(referer, next_url)
            if _extract_oauth_callback_params_from_url(next_url):
                return next_url
            return self._follow_platform_redirects_for_callback(self.session, next_url) if next_url else ""
        except Exception as exc:
            self._log(f"Platform reference: ChatGPT workspace/select 失败: {exc}", "warning")
            return ""

    def _select_first_organization_for_nextauth(self, data: dict, *, device_id: str, referer: str) -> str:
        from .constants import OPENAI_AUTH

        orgs = list((((data.get("data") or {}).get("orgs")) or [])) if isinstance(data, dict) else []
        if not orgs or not isinstance(orgs[0], dict) or not orgs[0].get("id"):
            return ""
        body = {"org_id": str(orgs[0].get("id") or "").strip()}
        projects = list(orgs[0].get("projects") or [])
        if projects and isinstance(projects[0], dict) and projects[0].get("id"):
            body["project_id"] = str(projects[0].get("id") or "").strip()
        try:
            resp = self.session.post(
                f"{OPENAI_AUTH}/api/accounts/organization/select",
                headers=self._platform_json_headers(device_id=device_id or self._protocol_device_id(), referer=referer),
                data=json.dumps(body, separators=(",", ":")),
                allow_redirects=False,
                timeout=30,
            )
            self._log(f"Platform reference: ChatGPT organization/select 状态 {getattr(resp, 'status_code', 0)}")
            next_url = str((getattr(resp, "headers", {}) or {}).get("Location") or "").strip()
            if not next_url:
                try:
                    next_url = str((resp.json() or {}).get("continue_url") or "").strip()
                except Exception:
                    next_url = ""
            return urllib.parse.urljoin(referer, next_url) if next_url else ""
        except Exception as exc:
            self._log(f"Platform reference: ChatGPT organization/select 失败: {exc}", "warning")
            return ""

    @staticmethod
    def _workspace_id_from_auth_payload(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        auth_session = payload.get("oai-client-auth-session") or payload.get("auth_session") or payload
        if not isinstance(auth_session, dict):
            return ""
        workspaces = auth_session.get("workspaces") or []
        if not isinstance(workspaces, list) or not workspaces:
            return ""
        first = workspaces[0] if isinstance(workspaces[0], dict) else {}
        return str(first.get("id") or "").strip()

    def _submit_first_auth_form_for_callback(self, *, response, referer: str) -> str:
        """兜底提交 choose-an-account HTML 中第一个 form。"""
        from html.parser import HTMLParser

        class _FormParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms: list[dict[str, Any]] = []
                self._current: dict[str, Any] | None = None

            def handle_starttag(self, tag, attrs):
                attrs_dict = {str(k).lower(): str(v or "") for k, v in attrs}
                if tag.lower() == "form":
                    self._current = {
                        "action": attrs_dict.get("action", ""),
                        "method": attrs_dict.get("method", "get").upper(),
                        "fields": {},
                    }
                    self.forms.append(self._current)
                elif self._current is not None and tag.lower() in {"input", "button"}:
                    name = attrs_dict.get("name", "")
                    if not name:
                        return
                    if tag.lower() == "button" and name in self._current["fields"]:
                        return
                    self._current["fields"][name] = attrs_dict.get("value", "")

            def handle_endtag(self, tag):
                if tag.lower() == "form":
                    self._current = None

        try:
            parser = _FormParser()
            parser.feed(str(getattr(response, "text", "") or ""))
            if not parser.forms:
                return ""
            form = parser.forms[0]
            action = urllib.parse.urljoin(str(getattr(response, "url", "") or referer), str(form.get("action") or ""))
            method = str(form.get("method") or "GET").upper()
            fields = dict(form.get("fields") or {})
            if method == "POST":
                resp = self.session.post(
                    action,
                    headers={
                        **self._platform_nav_headers(referer=str(getattr(response, "url", "") or referer)),
                        "content-type": "application/x-www-form-urlencoded",
                        "origin": "https://auth.openai.com",
                    },
                    data=urllib.parse.urlencode(fields),
                    allow_redirects=False,
                    timeout=30,
                )
            else:
                query = urllib.parse.urlencode(fields)
                url = action + (("&" if "?" in action else "?") + query if query else "")
                resp = self.session.get(
                    url,
                    headers=self._platform_nav_headers(referer=str(getattr(response, "url", "") or referer)),
                    allow_redirects=False,
                    timeout=30,
                )
            self._log(f"Platform reference: choose-an-account form 提交状态 {getattr(resp, 'status_code', 0)}")
            next_url = str((getattr(resp, "headers", {}) or {}).get("Location") or "").strip()
            if not next_url:
                try:
                    next_url = str((resp.json() or {}).get("continue_url") or "").strip()
                except Exception:
                    next_url = ""
            next_url = urllib.parse.urljoin(action, next_url)
            if _extract_oauth_callback_params_from_url(next_url):
                return next_url
            return self._follow_platform_redirects_for_callback(self.session, next_url) if next_url else ""
        except Exception as exc:
            self._log(f"Platform reference: choose-an-account form 提交失败: {exc}", "warning")
            return ""


    def _acquire_platform_tokens(self) -> Optional[dict]:

        """注册完成后，按 chatgpt2api 的 platform.openai.com 协议登录换 refresh_token。"""

        from .constants import OPENAI_AUTH

        if not self.email or not self.password:

            return None

        client = OpenAIHTTPClient(proxy_url=self.proxy_url)
        self.protocol_fingerprint.apply_to_client(client)

        session = client.session

        device_id = self.protocol_fingerprint.device_id

        self._set_oai_did_for_session(session, device_id)

        oauth_start = self._build_platform_oauth_start(self.email, device_id)

        try:

            self._log("开始 Platform OAuth 登录换 token...")

            resp = session.get(

                oauth_start.auth_url,

                headers=self._platform_nav_headers(referer=f"{PLATFORM_BASE}/"),

                allow_redirects=True,

                timeout=30,

            )

            self._log(f"Platform authorize 状态: {getattr(resp, 'status_code', 0)}")

            headers = self._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/log-in?usernameKind=email")

            headers["openai-sentinel-token"] = self._build_sentinel_header_for_client(client, device_id, "authorize_continue")

            resp = session.post(

                OPENAI_API_ENDPOINTS["signup"],

                headers=headers,

                data=json.dumps({"username": {"kind": "email", "value": self.email}}, separators=(",", ":")),

                allow_redirects=False,

                timeout=30,

            )

            if resp.status_code == 409 and self._is_invalid_state_response(resp):

                self._log("Platform 邮箱提交 invalid_state，重新 authorize 后重试", "warning")

                self._set_oai_did_for_session(session, device_id)

                session.get(oauth_start.auth_url, headers=self._platform_nav_headers(referer=f"{PLATFORM_BASE}/"), allow_redirects=True, timeout=30)

                headers["openai-sentinel-token"] = self._build_sentinel_header_for_client(client, device_id, "authorize_continue")

                resp = session.post(

                    OPENAI_API_ENDPOINTS["signup"],

                    headers=headers,

                    data=json.dumps({"username": {"kind": "email", "value": self.email}}, separators=(",", ":")),

                    allow_redirects=False,

                    timeout=30,

                )

            self._log(f"Platform 邮箱提交状态: {resp.status_code}")

            if resp.status_code != 200:

                self._log(f"Platform 邮箱提交失败: {getattr(resp, 'text', '')}", "warning")

                return None

            pwd_headers = self._platform_json_headers(device_id=device_id, referer=f"{OPENAI_AUTH}/log-in/password")

            pwd_headers["openai-sentinel-token"] = self._build_sentinel_header_for_client(client, device_id, "password_verify")

            pwd_resp = session.post(

                f"{OPENAI_AUTH}/api/accounts/password/verify",

                headers=pwd_headers,

                data=json.dumps({"password": self.password}, separators=(",", ":")),

                allow_redirects=False,

                timeout=30,

            )

            self._log(f"Platform 密码校验状态: {pwd_resp.status_code}")

            if pwd_resp.status_code != 200:

                self._log(f"Platform 密码校验失败: {getattr(pwd_resp, 'text', '')}", "warning")

                return None

            payload = pwd_resp.json()

            continue_url = str(payload.get("continue_url") or "").strip()

            page_type = str(((payload.get("page") or {}).get("type")) or "")

            if page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url:

                self._log("Platform 登录需要邮箱 OTP")

                self._refresh_mailbox_before_ids()

                self._send_platform_login_otp(client)

                code = self._wait_platform_login_code(client)

                if not code:

                    self._log("Platform 登录等待验证码超时", "warning")

                    return None

                otp_resp = self._validate_platform_login_otp(client, device_id, code)

                self._log(f"Platform 登录验证码校验状态: {otp_resp.status_code}")

                if otp_resp.status_code != 200:

                    self._log(f"Platform 登录验证码校验失败: {getattr(otp_resp, 'text', '')}", "warning")

                    return None

                otp_payload = otp_resp.json()

                continue_url = str(otp_payload.get("continue_url") or continue_url).strip()

            token_info = self._complete_platform_oauth(client, device_id, oauth_start, continue_url)

            if token_info:

                self._log("Platform OAuth token 获取成功")

            return token_info

        except Exception as exc:

            self._log(f"Platform OAuth 登录换 token 失败: {exc}", "warning")

            return None

        finally:

            try:

                session.close()

            except Exception:

                pass


    def _get_workspace_id(self) -> Optional[str]:

        """获取 Workspace ID"""

        try:

            auth_cookie = self.session.cookies.get("oai-client-auth-session")

            if not auth_cookie:

                self._log("未能获取到授权 Cookie", "error")

                return None



            # 解码 JWT

            import base64

            import json as json_module



            try:

                segments = auth_cookie.split(".")

                if len(segments) < 1:

                    self._log("授权 Cookie 格式错误", "error")

                    return None



                # 解码第一个 segment

                payload = segments[0]

                pad = "=" * ((4 - (len(payload) % 4)) % 4)

                decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))

                auth_json = json_module.loads(decoded.decode("utf-8"))



                workspaces = auth_json.get("workspaces") or []

                if not workspaces:

                    self._log("授权 Cookie 里没有 workspace 信息", "error")

                    return None



                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()

                if not workspace_id:

                    self._log("无法解析 workspace_id", "error")

                    return None



                self._log(f"Workspace ID: {workspace_id}")

                return workspace_id



            except Exception as e:

                self._log(f"解析授权 Cookie 失败: {e}", "error")

                return None



        except Exception as e:

            self._log(f"获取 Workspace ID 失败: {e}", "error")

            return None



    def _select_workspace(self, workspace_id: str) -> Optional[str]:

        """选择 Workspace"""

        try:

            select_body = f'{{"workspace_id":"{workspace_id}"}}'



            response = self.session.post(

                OPENAI_API_ENDPOINTS["select_workspace"],

                headers={

                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",

                    "content-type": "application/json",

                },

                data=select_body,

            )



            if response.status_code != 200:

                self._log(f"选择 workspace 失败: {response.status_code}", "error")

                self._log(f"响应: {response.text}", "warning")

                return None



            continue_url = str((response.json() or {}).get("continue_url") or "").strip()

            if not continue_url:

                self._log("workspace/select 响应里缺少 continue_url", "error")

                return None



            self._log(f"Continue URL: {continue_url}")

            return continue_url



        except Exception as e:

            self._log(f"选择 Workspace 失败: {e}", "error")

            return None



    def _follow_redirects(self, start_url: str) -> Optional[str]:

        """跟随重定向链，寻找回调 URL"""

        try:

            current_url = start_url

            max_redirects = 6



            for i in range(max_redirects):

                self._log(f"重定向 {i+1}/{max_redirects}: {current_url}")



                response = self.session.get(

                    current_url,

                    allow_redirects=False,

                    timeout=15

                )



                location = response.headers.get("Location") or ""



                # 如果不是重定向状态码，停止

                if response.status_code not in [301, 302, 303, 307, 308]:

                    self._log(f"非重定向状态码: {response.status_code}")

                    break



                if not location:

                    self._log("重定向响应缺少 Location 头")

                    break



                # 构建下一个 URL

                import urllib.parse

                next_url = urllib.parse.urljoin(current_url, location)



                # 检查是否包含回调参数

                if "code=" in next_url and "state=" in next_url:

                    self._log(f"找到回调 URL: {next_url}")

                    return next_url



                current_url = next_url



            self._log("未能在重定向链中找到回调 URL", "error")

            return None



        except Exception as e:

            self._log(f"跟随重定向失败: {e}", "error")

            return None



    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:

        """处理 OAuth 回调"""

        try:

            if not self.oauth_start:

                self._log("OAuth 流程未初始化", "error")

                return None



            self._log("处理 OAuth 回调...")

            token_info = self.oauth_manager.handle_callback(

                callback_url=callback_url,

                expected_state=self.oauth_start.state,

                code_verifier=self.oauth_start.code_verifier

            )



            self._log("OAuth 授权成功")

            return token_info



        except Exception as e:

            self._log(f"处理 OAuth 回调失败: {e}", "error")

            return None



    def run(self) -> RegistrationResult:

        """

        执行完整的注册流程



        支持已注册账号自动登录：

        - 如果检测到邮箱已注册，自动切换到登录流程

        - 已注册账号跳过：设置密码、发送验证码、创建用户账户

        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调



        Returns:

            RegistrationResult: 注册结果

        """

        result = RegistrationResult(success=False, logs=self.logs)



        try:

            self._log("=" * 60)

            self._log("开始注册流程")

            self._log("=" * 60)



            # 1. 检查 IP 地理位置

            self._log("1. 检查 IP 地理位置...")

            ip_ok, location = self._check_ip_location()

            if not ip_ok:

                result.error_message = f"IP 地理位置不支持: {location}"

                self._log(f"IP 检查失败: {location}", "error")

                return result



            self._log(f"IP 位置: {location}")



            # 2. 创建邮箱

            self._log("2. 创建邮箱...")

            if not self._create_email():

                result.error_message = "创建邮箱失败"

                return result



            result.email = self.email



            # 3. 初始化会话

            self._log("3. 初始化会话...")

            if not self._init_session():

                result.error_message = "初始化会话失败"

                return result



            import os as _os_register_flow

            register_flow = (_os_register_flow.environ.get("CHATGPT_REGISTER_FLOW") or "platform_reference").strip().lower()

            use_platform_reference = (

                register_flow not in {"legacy", "nextauth", "chatgpt_nextauth"}

                and getattr(self, "http_client", None) is not None

            )

            if use_platform_reference:

                # 默认按 chatgpt2api/services/register/openai_register.py 的 Platform 注册主链执行。

                self._log("4. 使用 chatgpt2api openai_register.py 同款 Platform 注册流程...")

                return self._run_platform_reference_register(result)



            # 4. 开始 OAuth 流程

            self._log("4. 开始 OAuth 授权流程...")

            if not self._start_oauth():

                result.error_message = "开始 OAuth 流程失败"

                return result



            # 5. 获取 Device ID

            self._log("5. 获取 Device ID...")

            did = self._get_device_id()

            if not did:

                result.error_message = "获取 Device ID 失败"

                return result



            # 6. 检查 Sentinel 拦截

            self._log("6. 检查 Sentinel 拦截...")

            sen_payload = self._check_sentinel(did)

            if sen_payload:

                self._log("Sentinel 检查通过")

            else:

                self._log("Sentinel 检查失败或未启用", "warning")



            # 7. 提交注册表单 + 解析响应判断账号状态

            self._log("7. 提交注册表单...")

            signup_result = self._submit_signup_form(did, sen_payload)

            if not signup_result.success:

                result.error_message = f"提交注册表单失败: {signup_result.error_message}"

                return result



            signup_page_type = signup_result.page_type or ""



            # 8. 根据授权页状态决定是否需要密码步骤

            if signup_page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:

                self._log("8. 已进入邮箱验证码流程，跳过密码设置")

            elif self._is_existing_account:

                self._log("8. [已注册账号] 跳过密码设置")

            else:

                self._log("8. 注册密码...")

                password_ok, password = self._register_password()

                if not password_ok:

                    result.error_message = "注册密码失败"

                    return result



            # 9. 发送验证码（协议模式没有浏览器 JS 自动触发，必须显式调用 API）

            if self._is_existing_account:

                self._log("9. [已注册账号] 发送登录验证码...")

            else:

                self._log("9. 发送验证码...")

            if not self._send_verification_code():

                result.error_message = "发送验证码失败"

                return result



            # 10. 获取验证码

            self._log("10. 等待验证码...")

            code = self._get_verification_code()

            if not code:

                result.error_message = self._email_otp_failure_message()

                return result



            # 11. 验证验证码

            self._log("11. 验证验证码...")

            if not self._validate_verification_code(code):

                result.error_message = self._email_otp_failure_message("验证验证码失败")

                return result



            # 12. 根据 OTP 响应决定下一步

            if self._otp_page_type == "about_you" and not self._is_existing_account:

                # 正常注册流程: about_you → create_account

                self._log("12. 创建用户账户...")

                if not self._create_user_account():

                    result.error_message = (
                        "EMAIL_ALIAS_PARENT_EXHAUSTED: user_already_exists - parent email alias quota exhausted"
                        if getattr(self, "_user_already_exists", False)
                        else "创建用户账户失败"
                    )

                    return result

            elif self._is_existing_account:

                self._log("12. [已注册账号] 跳过创建用户账户")

            else:

                self._log(f"12. OTP page_type={self._otp_page_type}，尝试创建账户...")

                if not self._create_user_account():

                    result.error_message = (
                        "EMAIL_ALIAS_PARENT_EXHAUSTED: user_already_exists - parent email alias quota exhausted"
                        if getattr(self, "_user_already_exists", False)
                        else "创建用户账户失败"
                    )

                    return result



            # 13. 跟随 callback URL 到 chatgpt.com 获取 session

            callback_url = self._create_account_continue_url

            if not callback_url or "code=" not in str(callback_url):

                result.error_message = "create_account 未返回有效的 callback URL"

                return result



            self._log("13. 跟随 callback URL 到 chatgpt.com...")

            cb_resp = self.session.get(callback_url, timeout=20)

            self._log(f"callback 状态: {cb_resp.status_code}")



            # 提取 session cookie

            session_token = _cookie_value(self.session.cookies, "__Secure-next-auth.session-token")

            account_cookie = _cookie_value(self.session.cookies, "_account")

            if session_token:

                self._log(f"获取到 session-token: {session_token[:30]}...")

            if account_cookie:

                self._log(f"获取到 _account: {account_cookie}")



            # 14. 从 chatgpt.com/api/auth/session 获取 access_token

            from .constants import CHATGPT_APP

            self._log("14. 获取 session 信息...")

            session_resp = self.session.get(

                f"{CHATGPT_APP}/api/auth/session",

                headers={"accept": "application/json"},

                timeout=15,

            )

            self._log(f"session API 状态: {session_resp.status_code}")

            self._log(f"session API 响应: {session_resp.text}")



            session_data = session_resp.json()
            session_cookies_header = _cookies_to_header(self.session.cookies)

            access_token = session_data.get("accessToken", "")
            session_token = str(
                session_data.get("sessionToken")
                or _cookie_value(self.session.cookies, "__Secure-next-auth.session-token")
                or session_token
                or ""
            ).strip()
            account_cookie = str(_cookie_value(self.session.cookies, "_account") or account_cookie or "").strip()

            user_data = session_data.get("user", {})
            session_account_id = _extract_chatgpt_account_id(access_token)
            session_profile = user_data if isinstance(user_data, dict) else {}
            session_expires = str(session_data.get("expires") or "")

            self._log(f"session keys: {list(session_data.keys())}")

            self._log(f"accessToken 长度: {len(access_token)}")
            if session_account_id:
                self._log(f"session accessToken 解析 Account ID: {session_account_id}")
            elif account_cookie:
                self._log(f"session 使用 _account Account ID: {account_cookie}")



            if not access_token:

                result.error_message = "chatgpt.com session 未返回 accessToken"

                return result



            self._log("NextAuth session 获取成功")



            # 15. Codex CLI OTP 登录获取 refresh_token + id_token

            codex_token_info = None

            try:

                self._log("15. Codex CLI OTP 登录...")

                from .constants import (

                    CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE,

                    OPENAI_AUTH,

                )

                import urllib.parse



                codex_oauth = generate_oauth_url(

                    redirect_uri=CODEX_REDIRECT_URI,

                    scope=CODEX_SCOPE,

                    client_id=CODEX_CLIENT_ID,

                )



                # 用全新 session（Hydra 需要干净 session）

                login_client = OpenAIHTTPClient(proxy_url=self.proxy_url)
                self.protocol_fingerprint.apply_to_client(login_client)

                login_session = login_client.session



                # 访问 Codex OAuth URL，跟随重定向到 /log-in

                login_session.get(codex_oauth.auth_url, timeout=15)

                did2 = login_session.cookies.get("oai-did", "")

                self._log(f"Codex login did: {did2[:20]}...")



                # 获取 sentinel（用 login_client）

                sen2 = None

                try:

                    ua2 = login_client.default_headers.get("User-Agent", "")

                    gen2 = _SentinelTokenGenerator(did2, ua2, profile=self.protocol_profile)

                    sp2 = gen2.generate_requirements_token()

                    sr2 = json.dumps({"p": sp2, "id": did2, "flow": "authorize_continue"}, separators=(",", ":"))

                    sr2_resp = login_client.post(

                        OPENAI_API_ENDPOINTS["sentinel"],

                        headers={"origin": "https://sentinel.openai.com", "referer": self._latest_chatgpt_sentinel_frame_url(), "content-type": "text/plain;charset=UTF-8"},

                        data=sr2,

                    )

                    if sr2_resp.status_code == 200:

                        d2 = sr2_resp.json()

                        pm2 = d2.get("proofofwork") or {}

                        if pm2.get("required") and pm2.get("seed"):

                            sp2 = gen2.generate_token(str(pm2.get("seed") or ""), str(pm2.get("difficulty") or "0"))

                        tr2 = (d2.get("turnstile") or {}).get("dx", "")

                        tv2 = ""

                        if tr2:

                            try: tv2 = gen2.decrypt_turnstile(tr2, sp2)

                            except: pass

                        sen2 = SentinelPayload(p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="authorize_continue")

                        self._log("Codex sentinel 获取成功")

                except Exception as e:

                    self._log(f"Codex sentinel 失败: {e}", "warning")



                # authorize/continue 提交邮箱（不带 screen_hint，让 codex_cli_simplified_flow 决定）

                signup_headers = {

                    "referer": f"{OPENAI_AUTH}/log-in",

                    "accept": "application/json",

                    "content-type": "application/json",

                }

                if sen2 and did2:

                    signup_headers["openai-sentinel-token"] = json.dumps({

                        "p": sen2.p, "t": sen2.t, "c": sen2.c,

                        "id": did2, "flow": sen2.flow,

                    }, separators=(",", ":"))



                signup_body = json.dumps({"username": {"value": self.email, "kind": "email"}, "screen_hint": "signup"})

                signup_resp = login_session.post(

                    OPENAI_API_ENDPOINTS["signup"], headers=signup_headers, data=signup_body

                )

                self._log(f"Codex authorize/continue: {signup_resp.status_code}")

                if signup_resp.status_code != 200:

                    raise RuntimeError(f"authorize/continue 失败: {signup_resp.text}")



                page_type = signup_resp.json().get("page", {}).get("type", "")

                self._log(f"Codex page_type: {page_type}")



                # 如果返回 email_otp_send 或 email_otp_verification，走 OTP 流程

                if page_type in ("email_otp_send", "email_otp_verification"):

                    # email_otp_verification 也不保证邮件已自动发出，统一显式发送。

                    login_session.get(OPENAI_API_ENDPOINTS["send_otp"], headers={

                        "referer": f"{OPENAI_AUTH}/email-verification",

                    }, timeout=15)

                    self._log("Codex OTP 已显式发送")



                    # 等待 OTP

                    self._otp_sent_at = time.time()

                    code = self._get_verification_code(mark_invalid_on_timeout=False)

                    if not code:

                        raise RuntimeError("Codex OTP 获取失败")

                    self._log(f"Codex OTP: {code}")



                    # 验证 OTP

                    otp_resp = login_session.post(

                        OPENAI_API_ENDPOINTS["validate_otp"],

                        headers={

                            "referer": f"{OPENAI_AUTH}/email-verification",

                            "accept": "application/json",

                            "content-type": "application/json",

                        },

                        data=json.dumps({"code": code}),

                    )

                    self._log(f"Codex OTP validate: {otp_resp.status_code}")

                    if otp_resp.status_code != 200:

                        raise RuntimeError(f"Codex OTP 验证失败: {otp_resp.text}")



                    otp_data = otp_resp.json()

                    otp_page = otp_data.get("page", {}).get("type", "")

                    self._log(f"Codex OTP -> page_type={otp_page}")



                    if otp_page == "add_phone":

                        self._log("Codex CLI 仍需 add_phone，跳过", "warning")

                        raise RuntimeError("add_phone required")



                    # OTP 成功后，重新访问 OAuth URL 获取 callback

                    self._log("Codex: 重新访问 OAuth URL...")

                    resp = login_session.get(codex_oauth.auth_url, allow_redirects=False, timeout=15)

                    codex_callback = None

                    current_url = codex_oauth.auth_url

                    for i in range(15):

                        if resp.status_code not in (301, 302, 303, 307, 308):

                            break

                        location = resp.headers.get("Location", "")

                        if not location:

                            break

                        next_url = urllib.parse.urljoin(current_url, location)

                        self._log(f"Codex 重定向 {i+1}: {next_url}")

                        if "code=" in next_url and "state=" in next_url:

                            codex_callback = next_url

                            break

                        current_url = next_url

                        resp = login_session.get(current_url, allow_redirects=False, timeout=15)



                    if codex_callback:

                        self._log("Codex CLI callback 获取成功")

                        token_json = submit_callback_url(

                            callback_url=codex_callback,

                            expected_state=codex_oauth.state,

                            code_verifier=codex_oauth.code_verifier,

                            redirect_uri=CODEX_REDIRECT_URI,

                            client_id=CODEX_CLIENT_ID,

                            proxy_url=self.proxy_url,

                        )

                        codex_token_info = json.loads(token_json)

                        self._log(f"Codex token 成功: keys={list(codex_token_info.keys())}")

                    else:

                        self._log(f"Codex callback 未获取 (status={resp.status_code})", "warning")

                else:

                    self._log(f"Codex 非 OTP 流程 ({page_type})，跳过", "warning")

            except Exception as e:

                self._log(f"Codex CLI 登录失败: {e}", "warning")


            platform_token_info = None

            if not (codex_token_info and codex_token_info.get("access_token")):

                # chatgpt2api 的 free 协议注册走 platform.openai.com client；
                # Codex token 因手机号/风控失败时，用 Platform OAuth token 作为兜底。

                platform_token_info = self._acquire_platform_tokens()


            # 提取账户信息（优先 Codex token，其次 Platform token，最后 NextAuth session）

            if codex_token_info and codex_token_info.get("access_token"):

                self._log("使用 Codex CLI token（完整 refresh_token + id_token）")

                result.account_id = codex_token_info.get("account_id", "") or account_cookie or session_account_id or ""

                result.access_token = codex_token_info.get("access_token", "")

                result.refresh_token = codex_token_info.get("refresh_token", "")

                result.id_token = codex_token_info.get("id_token", "")

            elif platform_token_info and platform_token_info.get("access_token"):

                self._log("使用 Platform OAuth token（参考 chatgpt2api free 协议）")

                result.account_id = platform_token_info.get("account_id", "") or account_cookie or session_account_id or ""

                result.access_token = platform_token_info.get("access_token", "")

                result.refresh_token = platform_token_info.get("refresh_token", "")

                result.id_token = platform_token_info.get("id_token", "")

            else:

                self._log("使用 NextAuth session token", "warning")

                result.account_id = account_cookie or session_account_id or ""

                result.access_token = access_token

                result.refresh_token = ""

                # access_token JWT 包含 chatgpt_account_id 等同于 id_token 的 claims

                result.id_token = access_token



            result.password = self.password or ""

            result.source = "login" if self._is_existing_account else "register"



            if session_token:

                self.session_token = session_token

                result.session_token = session_token

                self._log(f"获取到 Session Token")



            # 17. 完成

            self._log("=" * 60)

            if self._is_existing_account:

                self._log("登录成功! (已注册账号)")

            else:

                self._log("注册成功!")

            self._log(f"邮箱: {result.email}")

            self._log(f"Account ID: {result.account_id}")

            self._log(f"Workspace ID: {result.workspace_id}")

            self._log("=" * 60)



            result.success = True

            result.metadata = {

                "email_service": self.email_service.service_type.value,

                "proxy_used": self.proxy_url,

                "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),

                "is_existing_account": self._is_existing_account,
                # Chat2API 的 free 账号逻辑以 ChatGPT Web session 为准；
                # 保存 cookie/session，便于后续用 /api/auth/session 复验。
                "cookies": session_cookies_header,
                "login_state_cookie": session_cookies_header,
                "cookie_header": session_cookies_header,
                "profile": session_profile,
                "expires_at": session_expires,
                "session": session_data,
                "auth_source": (
                    "codex_oauth"
                    if codex_token_info and codex_token_info.get("access_token")
                    else "platform_oauth"
                    if platform_token_info and platform_token_info.get("access_token")
                    else "chatgpt_web_session"
                ),

            }



            return result



        except Exception as e:

            self._log(f"注册过程中发生未预期错误: {e}", "error")

            result.error_message = str(e)

            return result



    def save_to_database(self, result: RegistrationResult) -> bool:

        """

        保存注册结果到数据库



        Args:

            result: 注册结果



        Returns:

            是否保存成功

        """

        if not result.success:

            return False



        return True  # 由 account_manager 统一处理存库

