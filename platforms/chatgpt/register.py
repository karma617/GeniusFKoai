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

import secrets

import string

from typing import Optional, Dict, Any, Tuple, Callable

from dataclasses import dataclass

from datetime import datetime, timezone



from curl_cffi import requests as cffi_requests



from .oauth import OAuthManager, OAuthStart, generate_oauth_url, submit_callback_url

from .http_client import OpenAIHTTPClient, HTTPClientError

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

    SENTINEL_SDK_URL,

    OAUTH_REDIRECT_URI,

    OAUTH_CLIENT_ID,

)

# from ..config.settings import get_settings  # removed: external dep





logger = logging.getLogger(__name__)





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


def _cookies_to_header(cookies) -> str:
    """将当前会话 cookie 转为 Cookie header，便于后续按 Chat2API 方式校验 session。"""
    parts: list[str] = []
    if hasattr(cookies, "items"):
        try:
            for name, value in cookies.items():
                if name and value not in (None, ""):
                    parts.append(f"{name}={value}")
            return "; ".join(parts)
        except Exception:
            parts = []
    try:
        for cookie in cookies or []:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "")
            if name and value:
                parts.append(f"{name}={value}")
    except Exception:
        return ""
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

    """Dynamic sentinel token generator – mirrors browser_register._SentinelTokenGenerator."""



    def __init__(self, device_id: str, user_agent: str):

        self.device_id = device_id or str(uuid.uuid4())

        self.user_agent = user_agent

        self.sid = str(uuid.uuid4())



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



    def _config(self) -> list:

        perf_now = 1000 + random.random() * 49000

        return [

            "1920x1080",

            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),

            4294705152,

            random.random(),

            self.user_agent,

            SENTINEL_SDK_URL,

            None,

            None,

            "en-US",

            "en-US,en",

            random.random(),

            "webkitTemporaryStorage\u2212undefined",

            "location",

            "Object",

            perf_now,

            self.sid,

            "",

            random.choice([4, 8, 12, 16]),

            int(time.time() * 1000 - perf_now),

        ]



    def generate_requirements_token(self) -> str:

        cfg = self._config()

        cfg[3] = 1

        cfg[9] = round(5 + random.random() * 45)

        return "gAAAAAC" + self._b64(cfg)



    def generate_token(self, seed: str, difficulty: str) -> str:

        max_attempts = 500000

        cfg = self._config()

        start_ms = int(time.time() * 1000)

        diff = str(difficulty or "0")

        for nonce in range(max_attempts):

            cfg[3] = nonce

            cfg[9] = round(int(time.time() * 1000) - start_ms)

            encoded = self._b64(cfg)

            digest = self._fnv1a32((seed or "") + encoded)

            if digest[: len(diff)] <= diff:

                return "gAAAAAB" + encoded + "~S"

        return "gAAAAAB" + self._b64(None)





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

        task_uuid: Optional[str] = None

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



        # 创建 HTTP 客户端

        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)



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

        self._device_id: Optional[str] = None

        self._sentinel_token: Optional[str] = None

        self._signup_sentinel: Optional[SentinelPayload] = None

        self._password_sentinel: Optional[SentinelPayload] = None

        self._create_account_continue_url: Optional[str] = None

        self._email_otp_continue_url: Optional[str] = None

        self._email_otp_page_loaded: bool = False

        self._otp_continue_url: Optional[str] = None

        self._otp_page_type: Optional[str] = None
        self._email_otp_exhausted: bool = False



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


    def _read_oai_did_cookie(self) -> str:

        """读取 OpenAI 设备标识 cookie，兼容多域 CookieJar 冲突场景。"""

        if not self.session:

            return ""

        try:

            return str(self.session.cookies.get("oai-did") or "")

        except Exception:

            try:

                for cookie in self.session.cookies:

                    if getattr(cookie, "name", "") == "oai-did":

                        return str(getattr(cookie, "value", "") or "")

            except Exception:

                return ""

        return ""


    def _seed_oai_did_cookie(self, device_id: str) -> str:

        """OpenAI 未下发 oai-did 时，本地生成并种入会话，避免协议流空设备 ID 中断。"""

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

        for domain in (".auth.openai.com", "auth.openai.com"):

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

        try:

            return any(getattr(cookie, "name", "") == name for cookie in self.session.cookies)

        except Exception:

            return False



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

                headers={

                    "referer": "https://chatgpt.com/",

                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",

                },

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

            oai_did = self._read_oai_did_cookie()

            if not oai_did:

                oai_did = self._seed_oai_did_cookie(str(uuid.uuid4()))

                self._log(f"chatgpt.com 未返回 oai-did，已本地生成: {oai_did[:20]}...")

            else:

                self._log(f"chatgpt.com oai-did: {oai_did[:20]}...")



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

            signin_url = f"{CHATGPT_APP}/api/auth/signin/openai"

            if oai_did:

                signin_url += f"?prompt=login&ext-oai-did={oai_did}"



            signin_resp = self.session.post(

                signin_url,

                headers={

                    "content-type": "application/x-www-form-urlencoded",

                    "origin": CHATGPT_APP,

                    "referer": f"{CHATGPT_APP}/",

                },

                data=f"callbackUrl={CHATGPT_APP}%2F&csrfToken={csrf_token}&json=true",

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

                did = self._seed_oai_did_cookie(str(uuid.uuid4()))

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

            generator = _SentinelTokenGenerator(did, ua)

            sent_p = generator.generate_requirements_token()

            sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": flow}, separators=(",", ":"))



            from .constants import SENTINEL_FRAME_URL

            response = self.http_client.post(

                OPENAI_API_ENDPOINTS["sentinel"],

                headers={

                    "origin": "https://sentinel.openai.com",

                    "referer": SENTINEL_FRAME_URL,

                    "content-type": "text/plain;charset=UTF-8",

                },

                data=sen_req_body,

            )



            if response.status_code == 200:

                data = response.json()

                sen_token = str(data.get("token") or "")

                turnstile = data.get("turnstile") or {}



                # Handle proofofwork challenge if required

                initial_p = sent_p  # keep for dx decryption

                pow_meta = data.get("proofofwork") or {}

                if pow_meta.get("required") and pow_meta.get("seed"):

                    sent_p = generator.generate_token(

                        str(pow_meta.get("seed") or ""),

                        str(pow_meta.get("difficulty") or "0"),

                    )

                    self._log(f"Sentinel PoW solved: flow={flow}")



                # Solve turnstile dx with VM

                t_value = ""

                dx_b64 = str(turnstile.get("dx") or "")

                if dx_b64:

                    try:

                        from .sentinel_vm import solve_turnstile_dx

                        from .constants import SENTINEL_SDK_URL

                        t_value = solve_turnstile_dx(dx_b64, initial_p, user_agent=ua, sdk_url=SENTINEL_SDK_URL)

                        self._log(f"Sentinel VM solved: t_len={len(t_value)} flow={flow}")

                    except Exception as vm_err:

                        self._log(f"Sentinel VM failed: {vm_err}", "warning")



                payload = SentinelPayload(

                    p=sent_p,

                    c=sen_token,

                    flow=flow,

                    t=t_value,

                )

                self._log(f"Sentinel token 获取成功: flow={flow}")

                return payload

            else:

                self._log(f"Sentinel 检查失败: flow={flow} status={response.status_code}", "warning")

                return None



        except Exception as e:

            self._log(f"Sentinel 检查异常: flow={flow} {e}", "warning")

            return None



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

            ua = self.http_client.default_headers.get("User-Agent", "")

            chrome_match = re.search(r"Chrome/(\d+)", ua)

            chrome_major = str(chrome_match.group(1) if chrome_match else "136")

            sec_ch_ua = f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", "Not.A/Brand";v="99"'



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



                register_headers = {

                    "origin": "https://auth.openai.com",

                    "referer": "https://auth.openai.com/create-account/password",

                    "accept": "application/json",

                    "content-type": "application/json",

                    "accept-language": "en-US,en;q=0.9",

                    "sec-ch-ua": sec_ch_ua,

                    "sec-ch-ua-mobile": "?0",

                    "sec-ch-ua-platform": '"Windows"',

                    "sec-fetch-dest": "empty",

                    "sec-fetch-mode": "cors",

                    "sec-fetch-site": "same-origin",

                    **_generate_datadog_trace_headers(),

                }

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

                        if page_type == OPENAI_PAGE_TYPES.get("EMAIL_OTP_VERIFICATION", "email_otp_verification"):

                            self._log("密码提交后进入邮箱 OTP 验证流程")

                            if continue_url:

                                self._email_otp_continue_url = continue_url

                                self._log(f"密码响应 continue_url: {continue_url}")

                    except Exception:

                        pass

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

            email_verification_url = self._email_otp_continue_url or "https://auth.openai.com/email-verification"

            self._log(f"邮箱验证页 URL: {email_verification_url}")

            send_url = OPENAI_API_ENDPOINTS["send_otp"]
            password_referer = "https://auth.openai.com/create-account/password"
            csrf_token = ""

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



    def _get_verification_code(self, *, mark_invalid_on_timeout: bool = True) -> Optional[str]:

        """获取验证码；每轮等 60 秒，未收到则重发，默认共 3 轮。"""

        try:

            email_id = self.email_info.get("service_id") if self.email_info else None

            import os as _os_otp_timeout

            try:

                otp_timeout = int((_os_otp_timeout.environ.get("CHATGPT_OTP_TIMEOUT_SECONDS", "") or "60").strip())

            except Exception:

                otp_timeout = 60

            try:

                max_attempts = int((_os_otp_timeout.environ.get("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "") or "3").strip())

            except Exception:

                max_attempts = 3

            if otp_timeout < 30:

                otp_timeout = 30

            max_attempts = max(1, min(max_attempts, 5))

            for attempt in range(1, max_attempts + 1):

                if attempt > 1:

                    self._log(f"邮箱验证码 {otp_timeout}s 未收到，重新发送验证码 ({attempt}/{max_attempts})...")

                    if not self._send_verification_code():

                        self._log(f"第 {attempt}/{max_attempts} 次重发验证码失败", "warning")

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
    def _is_deleted_or_deactivated_account_response(response) -> bool:
        """判断 OpenAI 是否拒绝已删除/停用过的邮箱继续创建账号。"""
        marker = "deleted or deactivated"
        text = str(getattr(response, "text", "") or "").lower()
        if marker in text:
            return True
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else {}
            message = str((error or {}).get("message") or "").lower() if isinstance(error, dict) else ""
            return marker in message
        except Exception:
            return False

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
        marker = getattr(self.email_service, "mark_invalid_email", None)
        if not callable(marker):
            self._log(f"当前邮箱服务不支持无效邮箱打标: {self.email}", "warning")
            return []
        try:
            applied = list(marker(reason=reason) or [])
            if applied:
                self._log(f"已给邮箱 {self.email} 打标: {', '.join(applied)}", "warning")
            else:
                self._log(f"邮箱无效打标未返回标签: {self.email}", "warning")
            return applied
        except Exception as exc:
            self._log(f"给无效邮箱打标失败: {exc}", "error")
            return []

    def _refresh_mailbox_before_ids(self) -> None:
        """刷新已见邮件集合，避免重发 OTP 后再次读到旧验证码。"""
        refresh = getattr(self.email_service, "refresh_before_ids", None)
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
            self.session = self.http_client.session
            self.oauth_start = None
            self._device_id = None
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

                    self._log(f"create_account Sentinel 已获取: flow={ca_sentinel.flow}")



            response = self.session.post(

                OPENAI_API_ENDPOINTS["create_account"],

                headers=create_headers,

                data=create_account_body,

            )



            self._log(f"账户创建状态: {response.status_code}")



            if response.status_code != 200:

                self._log(f"账户创建失败: {response.text}", "warning")
                if self._is_deleted_or_deactivated_account_response(response):
                    self._log("OpenAI 判定该邮箱关联账号已删除或停用，准备删除当前邮箱", "warning")
                    self._delete_current_email_after_openai_reject("openai_account_deleted_or_deactivated")

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

                generator = _SentinelTokenGenerator(did, ua)

                sent_p = generator.generate_requirements_token()

                sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": "authorize_continue"}, separators=(",", ":"))



                from .constants import SENTINEL_FRAME_URL

                sen_resp = login_client.post(

                    OPENAI_API_ENDPOINTS["sentinel"],

                    headers={

                        "origin": "https://sentinel.openai.com",

                        "referer": SENTINEL_FRAME_URL,

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

                    gen2 = _SentinelTokenGenerator(did, ua2)

                    sp2 = gen2.generate_requirements_token()

                    sr2 = json.dumps({"p": sp2, "id": did, "flow": "login_password"}, separators=(",", ":"))

                    from .constants import SENTINEL_FRAME_URL as SF2

                    sr2_resp = login_client.post(

                        OPENAI_API_ENDPOINTS["sentinel"],

                        headers={"origin": "https://sentinel.openai.com", "referer": SF2, "content-type": "text/plain;charset=UTF-8"},

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
        user_agent = default_headers.get("User-Agent") or default_ua

        headers = {

            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",

            "accept-language": "en-US,en;q=0.9",

            "user-agent": user_agent,

            "sec-ch-ua": PLATFORM_REFERENCE_SEC_CH_UA,

            "sec-ch-ua-arch": '"x86_64"',

            "sec-ch-ua-bitness": '"64"',

            "sec-ch-ua-full-version-list": PLATFORM_REFERENCE_SEC_CH_UA_FULL,

            "sec-ch-ua-mobile": "?0",

            "sec-ch-ua-model": '""',

            "sec-ch-ua-platform": '"Windows"',

            "sec-ch-ua-platform-version": '"10.0.0"',

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
        user_agent = default_headers.get("User-Agent") or default_ua

        headers = {

            "accept": "application/json",

            "accept-language": "en-US,en;q=0.9",

            "content-type": "application/json",

            "origin": OPENAI_AUTH,

            "priority": "u=1, i",

            "referer": referer,

            "user-agent": user_agent,

            "oai-device-id": device_id,

            "sec-ch-ua": PLATFORM_REFERENCE_SEC_CH_UA,

            "sec-ch-ua-arch": '"x86_64"',

            "sec-ch-ua-bitness": '"64"',

            "sec-ch-ua-full-version-list": PLATFORM_REFERENCE_SEC_CH_UA_FULL,

            "sec-ch-ua-mobile": "?0",

            "sec-ch-ua-model": '""',

            "sec-ch-ua-platform": '"Windows"',

            "sec-ch-ua-platform-version": '"10.0.0"',

            "sec-fetch-dest": "empty",

            "sec-fetch-mode": "cors",

            "sec-fetch-site": "same-origin",

        }

        headers.update(_generate_datadog_trace_headers())

        return headers


    def _build_sentinel_header_for_client(self, client: OpenAIHTTPClient, device_id: str, flow: str) -> str:

        """为独立 Platform 登录 session 生成 Sentinel header。"""

        from .constants import SENTINEL_FRAME_URL

        ua = client.default_headers.get("User-Agent", "")

        generator = _SentinelTokenGenerator(device_id, ua)

        sent_p = generator.generate_requirements_token()

        response = client.post(

            OPENAI_API_ENDPOINTS["sentinel"],

            headers={

                "origin": "https://sentinel.openai.com",

                "referer": SENTINEL_FRAME_URL,

                "content-type": "text/plain;charset=UTF-8",

            },

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

                from .constants import SENTINEL_SDK_URL

                t_value = solve_turnstile_dx(dx_b64, initial_p, user_agent=ua, sdk_url=SENTINEL_SDK_URL)

            except Exception as exc:

                self._log(f"Platform Sentinel VM 失败: {exc}", "warning")

        return json.dumps({"p": sent_p, "t": t_value, "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))


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

        """等待独立 Platform 登录邮箱 OTP；60 秒未到则重发，最多 3 轮。"""

        import os as _os_otp_timeout

        try:

            otp_timeout = int((_os_otp_timeout.environ.get("CHATGPT_OTP_TIMEOUT_SECONDS", "") or "60").strip())

        except Exception:

            otp_timeout = 60

        try:

            max_attempts = int((_os_otp_timeout.environ.get("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "") or "3").strip())

        except Exception:

            max_attempts = 3

        otp_timeout = max(30, otp_timeout)

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

        self._log(f"platform authorize 状态: {status}, final_url={final_url}")

        if resp is None or getattr(resp, "status_code", 0) != 200:

            body = getattr(resp, "text", "") if resp is not None else ""

            raise RuntimeError(error or f"platform_authorize_http_{status}: {body}")

        self._log("platform authorize 完成")

        return oauth_start


    def _platform_reference_register_user(self, client: OpenAIHTTPClient, device_id: str) -> None:

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

            raise RuntimeError(error or f"user_register_http_{status}: {text}")

        try:

            payload = resp.json() or {}

            continue_url = str(payload.get("continue_url") or "").strip()

            if continue_url:

                self._email_otp_continue_url = continue_url

        except Exception:

            pass

        self._log("提交注册密码完成")


    def _platform_reference_send_otp(self, client: OpenAIHTTPClient) -> None:

        """照 openai_register.py：GET email-otp/send，允许跳转到验证页。"""

        from .constants import OPENAI_AUTH

        self._log("开始发送验证码")

        resp, error = self._platform_request_with_retry(

            client.session,

            "get",

            OPENAI_API_ENDPOINTS["send_otp"],

            headers=self._platform_nav_headers(referer=f"{OPENAI_AUTH}/create-account/password"),

            allow_redirects=True,

        )

        status = getattr(resp, "status_code", "unknown") if resp is not None else "none"

        final_url = getattr(resp, "url", "") if resp is not None else ""

        text = getattr(resp, "text", "") if resp is not None else ""

        self._log(f"验证码发送状态: {status}, final_url={final_url}")

        self._log(f"验证码发送响应: {text}")

        if resp is None or getattr(resp, "status_code", 0) not in (200, 302):

            raise RuntimeError(error or f"send_otp_http_{status}: {text}")

        self._otp_sent_at = time.time()

        self._log("发送验证码完成")


    def _wait_platform_reference_register_code(self, client: OpenAIHTTPClient) -> Optional[str]:

        """等待 platform 注册验证码；沿用本项目三轮 60s 规则，重发仍走参照 send_otp。"""

        import os as _os_otp_timeout

        try:

            otp_timeout = int((_os_otp_timeout.environ.get("CHATGPT_OTP_TIMEOUT_SECONDS", "") or "60").strip())

        except Exception:

            otp_timeout = 60

        try:

            max_attempts = int((_os_otp_timeout.environ.get("CHATGPT_EMAIL_OTP_MAX_ATTEMPTS", "") or "3").strip())

        except Exception:

            max_attempts = 3

        otp_timeout = max(30, otp_timeout)

        max_attempts = max(1, min(max_attempts, 5))

        email_id = self.email_info.get("service_id") if self.email_info else None

        for attempt in range(1, max_attempts + 1):

            if attempt > 1:

                self._log(f"邮箱验证码 {otp_timeout}s 未收到，按 platform 参照流程重发 ({attempt}/{max_attempts})...")

                try:

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

        headers["openai-sentinel-token"] = self._build_sentinel_header_for_client(

            client,

            device_id,

            "oauth_create_account",

        )

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


    def _run_platform_reference_register(self, result: RegistrationResult) -> RegistrationResult:

        """按 E:\\AI\\chatgpt2api\\services\\register\\openai_register.py 主链注册。"""

        client = OpenAIHTTPClient(proxy_url=self.proxy_url)

        # 参照源注册器固定使用 Chrome 145 指纹，Sentinel 与业务请求须保持一致。
        client.default_headers["User-Agent"] = PLATFORM_REFERENCE_USER_AGENT

        self.http_client = client

        self.session = client.session

        device_id = str(uuid.uuid4())

        self._device_id = device_id

        self._set_oai_did_for_session(self.session, device_id)

        oauth_start = self._platform_reference_authorize(client, device_id)

        if not self.password:

            self.password = self._generate_password()

        result.password = self.password

        self._refresh_mailbox_before_ids()

        self._platform_reference_register_user(client, device_id)

        self._platform_reference_send_otp(client)

        code = self._wait_platform_reference_register_code(client)

        if not code:

            result.error_message = "邮箱验证码三轮未收到，已标记无效邮箱" if self._email_otp_exhausted else "获取验证码失败"

            return result

        self._platform_reference_validate_otp(client, device_id, code)

        self._platform_reference_create_account(client, device_id)

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

        result.success = True

        result.email = self.email or ""

        result.password = self.password or ""

        result.account_id = account_id

        result.access_token = access_token

        result.refresh_token = ""

        result.id_token = id_token

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


    def _acquire_platform_tokens(self) -> Optional[dict]:

        """注册完成后，按 chatgpt2api 的 platform.openai.com 协议登录换 refresh_token。"""

        from .constants import OPENAI_AUTH

        if not self.email or not self.password:

            return None

        client = OpenAIHTTPClient(proxy_url=self.proxy_url)

        session = client.session

        device_id = str(uuid.uuid4())

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

                result.error_message = (
                    "邮箱验证码三轮未收到，已标记无效邮箱"
                    if self._email_otp_exhausted
                    else "获取验证码失败"
                )

                return result



            # 11. 验证验证码

            self._log("11. 验证验证码...")

            if not self._validate_verification_code(code):

                result.error_message = (
                    "邮箱验证码三轮未收到，已标记无效邮箱"
                    if self._email_otp_exhausted
                    else "验证验证码失败"
                )

                return result



            # 12. 根据 OTP 响应决定下一步

            if self._otp_page_type == "about_you" and not self._is_existing_account:

                # 正常注册流程: about_you → create_account

                self._log("12. 创建用户账户...")

                if not self._create_user_account():

                    result.error_message = "创建用户账户失败"

                    return result

            elif self._is_existing_account:

                self._log("12. [已注册账号] 跳过创建用户账户")

            else:

                self._log(f"12. OTP page_type={self._otp_page_type}，尝试创建账户...")

                if not self._create_user_account():

                    result.error_message = "创建用户账户失败"

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

            session_token = self.session.cookies.get("__Secure-next-auth.session-token")

            account_cookie = self.session.cookies.get("_account", "")
            session_cookies_header = _cookies_to_header(self.session.cookies)

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

            access_token = session_data.get("accessToken", "")

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

                    OPENAI_AUTH, SENTINEL_FRAME_URL,

                )

                import urllib.parse



                codex_oauth = generate_oauth_url(

                    redirect_uri=CODEX_REDIRECT_URI,

                    scope=CODEX_SCOPE,

                    client_id=CODEX_CLIENT_ID,

                )



                # 用全新 session（Hydra 需要干净 session）

                login_client = OpenAIHTTPClient(proxy_url=self.proxy_url)

                login_session = login_client.session



                # 访问 Codex OAuth URL，跟随重定向到 /log-in

                login_session.get(codex_oauth.auth_url, timeout=15)

                did2 = login_session.cookies.get("oai-did", "")

                self._log(f"Codex login did: {did2[:20]}...")



                # 获取 sentinel（用 login_client）

                sen2 = None

                try:

                    ua2 = login_client.default_headers.get("User-Agent", "")

                    gen2 = _SentinelTokenGenerator(did2, ua2)

                    sp2 = gen2.generate_requirements_token()

                    sr2 = json.dumps({"p": sp2, "id": did2, "flow": "authorize_continue"}, separators=(",", ":"))

                    sr2_resp = login_client.post(

                        OPENAI_API_ENDPOINTS["sentinel"],

                        headers={"origin": "https://sentinel.openai.com", "referer": SENTINEL_FRAME_URL, "content-type": "text/plain;charset=UTF-8"},

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

