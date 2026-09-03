# -*- coding: utf-8 -*-
"""ChatGPT 账号邮箱换绑协议（批处理单账号入口）。

对齐 rebind-server services/account_rebind_service.py 中
chat_passwordless_login / rebind_email_protocol 的真实协议链路，但复用本项目既有能力：

  1. 旧邮箱验证码：账号 graph 的 mailbox resource 解析（provider_resources /
     verification_mailbox / identity.mailbox），复用
     platforms.chatgpt.plugin.ChatGPTPlatform 的 _build_refresh_session_mailbox_email_service；
  2. 旧邮箱 OTP 新鲜登录：复用 platforms.chatgpt.register.RegistrationEngine
     的 run_chatgpt_refresh_session_latest（chatgpt.com NextAuth + Sentinel 链路）；
  3. 新邮箱：core.base_mailbox.CFWorkerMailbox Cloud Mail 模式，依据独立
     mail_config（api_url / api_token / domains）自动生成新邮箱并轮询验证码；
      支持子域名邮箱：任务层把 mail_config.domain 覆写为「子域名.主域名」，
      创建地址的域名部分逐字校验（见 _create_new_cloud_mailbox /
      _verify_new_email_address）。
  4. 二次换绑：account.extra 中 api_url/domain 与当前独立 mail_config 相容的
     cloud_mail/cfworker provider_resources 会在内存安全副本里补注 api_token，
     供 plugin 构建旧邮箱收码服务；secret 不进入返回 mailbox_resource / 数据库。

协议请求（与 chatgpt.com 设置页「修改邮箱」一致）：
  POST https://chatgpt.com/backend-api/accounts/change_email/begin  {"email": 新邮箱}
  POST https://chatgpt.com/backend-api/accounts/change_email/verify {"email": 新邮箱, "code": 验证码}

对外仅暴露 rebind_account_email；任何失败都返回结构化
{"ok": False, "error": ...}，不向批处理外层抛异常。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from curl_cffi import requests as curl_requests

from core.base_mailbox import CFWorkerMailbox
from core.base_platform import Account
from core.http_client import build_cffi_proxy_request_kwargs
from platforms.chatgpt.plugin import ChatGPTPlatform, _account_secret_value
from platforms.chatgpt.register import RegistrationEngine

CHATGPT_BASE_URL = "https://chatgpt.com"
_CHANGE_EMAIL_BEGIN_PATH = "/backend-api/accounts/change_email/begin"
_CHANGE_EMAIL_VERIFY_PATH = "/backend-api/accounts/change_email/verify"
_PROTOCOL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
# CF 转发 + 上游投递可能 1-3 分钟，与 rebind-server 参考实现一致给足 300s 下限。
_NEW_CODE_WAIT_TIMEOUT_FLOOR = 300.0
# plugin._mailbox_provider_key 会把以下 provider/api_mode 归一到 cfworker_admin_api。
_CFWORKER_PROVIDER_KEYS = {"cloud_mail", "cfworker", "cfworker_admin_api"}
_CFWORKER_API_MODES = {"cloud_mail", "cfworker"}

LogFn = Callable[[str], None]


def _make_logger(log_fn: LogFn | None) -> LogFn:
    return log_fn if callable(log_fn) else print


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _resp_snippet(resp: Any, limit: int = 300) -> str:
    text = str(getattr(resp, "text", "") or "")
    return re.sub(r"\s+", " ", text)[:limit]


def _failure(old_email: str, new_email: str, message: str) -> dict:
    return {"ok": False, "old_email": old_email, "new_email": new_email, "error": message}


def _ordered_mail_config_domains(mail_config: dict) -> list[str]:
    """候选主域名：显式 domain 优先，其次 domains（列表 / 逗号分号空白分隔）顺序去重。"""
    ordered: list[str] = []
    seen: set[str] = set()
    explicit = _normalize_domain_value(str(mail_config.get("domain") or "").strip().lstrip("@"))
    if explicit:
        ordered.append(explicit)
        seen.add(explicit)
    raw = mail_config.get("domains")
    if isinstance(raw, str):
        items = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        items = []
    for item in items:
        domain = _normalize_domain_value(item)
        if domain and domain not in seen:
            seen.add(domain)
            ordered.append(domain)
    return ordered


def _select_cloud_mail_domain(
    mail_config: dict,
    *,
    subdomain_allocations: dict | None = None,
    subdomain_limit: int = 10,
) -> str:
    """选择新邮箱主域名；提供 subdomain_allocations 时感知子域名配额。

    无配额数据时行为与历史一致：显式 domain 优先，其次 domains 第一个候选。
    有配额数据时只返回子域名占用数 < subdomain_limit 的主域名（上限由分配方
    传入，语义与 application.chatgpt_rebind.SUBDOMAIN_LIMIT_PER_DOMAIN 一致）；
    全部候选满额时返回空串。
    """
    candidates = _ordered_mail_config_domains(mail_config)
    if subdomain_allocations is None:
        return candidates[0] if candidates else ""
    try:
        limit = max(1, int(subdomain_limit))
    except Exception:
        limit = 10
    for domain in candidates:
        if len(subdomain_allocations.get(domain) or []) < limit:
            return domain
    return ""


def _normalize_api_base_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _normalize_domain_value(value: Any) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _email_domain(value: Any) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return ""
    return _normalize_domain_value(text.split("@", 1)[1])


def _mail_config_domain_set(mail_config: dict) -> set:
    """domain + domains（列表 / 逗号分号空白分隔字符串）归一后的全部候选域名。"""
    domains = set()
    selected = _select_cloud_mail_domain(mail_config)
    if selected:
        domains.add(_normalize_domain_value(selected))
    raw = mail_config.get("domains")
    if isinstance(raw, str):
        items = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        items = []
    for item in items:
        domain = _normalize_domain_value(item)
        if domain:
            domains.add(domain)
    return domains


def _resource_cloud_mail_domain(resource: dict, metadata: dict) -> str:
    domain = _normalize_domain_value(metadata.get("domain"))
    if domain:
        return domain
    for key in ("email", "handle", "display_name"):
        domain = _email_domain(metadata.get(key)) or _email_domain(resource.get(key))
        if domain:
            return domain
    return ""


def _enhance_old_mailbox_account_extra(account: dict, mail_config: dict) -> dict:
    """对 account.extra 的 cloud_mail/cfworker 资源做纯内存安全副本增强。

    成功换绑落库的 mailbox_resource 出于安全不含 api_token，再次换绑同一账号时
    plugin 解析器拿不到 cfworker_admin_token。这里只在内存副本上，为 api_url/domain
    与当前独立 mail_config 相容的 cloud_mail/cfworker provider_resources 注入
    metadata api_url/domain/api_token；普通邮箱资源与不相容资源保持原样。不修改
    调用方传入的 account dict，token 也不会进入返回 mailbox_resource / 数据库。
    """
    extra = dict(account.get("extra") or {})
    raw_resources = extra.get("provider_resources")
    if not isinstance(raw_resources, (list, tuple)) or not raw_resources:
        return extra
    api_url = _normalize_api_base_url(mail_config.get("api_url"))
    token = _first_non_empty(mail_config.get("api_token"), mail_config.get("admin_token"))
    domains = _mail_config_domain_set(mail_config)
    if not api_url or not token or not domains:
        return extra

    enhanced_resources = []
    for item in raw_resources:
        if not isinstance(item, dict):
            enhanced_resources.append(item)
            continue
        resource = dict(item)
        raw_metadata = resource.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        raw_provider = _first_non_empty(resource.get("provider_name"), resource.get("provider")).lower()
        api_mode = str(metadata.get("api_mode") or "").strip().lower()
        is_cfworker_family = raw_provider in _CFWORKER_PROVIDER_KEYS or api_mode in _CFWORKER_API_MODES
        if is_cfworker_family:
            resource_api_url = _normalize_api_base_url(metadata.get("api_url"))
            resource_domain = _resource_cloud_mail_domain(resource, metadata)
            if resource_api_url == api_url and resource_domain and resource_domain in domains:
                metadata["api_url"] = str(mail_config.get("api_url") or "").strip()
                metadata["domain"] = resource_domain
                metadata["api_token"] = token
                resource["metadata"] = metadata
        enhanced_resources.append(resource)
    extra["provider_resources"] = enhanced_resources
    return extra


def _resolve_account_mailbox_email_service(
    account_obj: Account, log: LogFn, proxy: str
):
    """复用 plugin 的账号邮箱资源解析能力，得到旧邮箱收码 email_service。

    该方法体只使用 account/log_fn/proxy 参数（不依赖 self），以 None 直接调用，
    避免实例化平台时触发数据库能力查询。返回 (email_service, error_str)。
    """
    return ChatGPTPlatform._build_refresh_session_mailbox_email_service(
        None, account_obj, log, proxy
    )


def _fresh_login_with_old_email_otp(
    account_obj: Account, email_service: Any, proxy: str, log: LogFn
) -> dict:
    """旧邮箱 OTP 新鲜登录；成功返回 {ok, access_token, refresh_token, id_token}。"""
    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy,
        callback_logger=log,
    )
    engine.email = account_obj.email
    engine.password = account_obj.password
    engine.totp_secret = _account_secret_value(account_obj, "totp_secret")
    engine.prefer_password_totp_login = bool(
        str(account_obj.password or "").strip() and engine.totp_secret
    )
    engine.k12_join_enabled = False
    engine.set_password_after_register = False
    result = engine.run_chatgpt_refresh_session_latest()
    if not result or not getattr(result, "success", False):
        error = str(getattr(result, "error_message", "") or "重新登录失败")
        return {"ok": False, "error": error}

    metadata = getattr(result, "metadata", None) or {}
    session = metadata.get("session") if isinstance(metadata.get("session"), dict) else {}
    access_token = _first_non_empty(
        getattr(result, "access_token", ""),
        session.get("accessToken"),
        session.get("access_token"),
    )
    if not access_token:
        return {"ok": False, "error": "重新登录成功但没有拿到 accessToken"}
    return {
        "ok": True,
        "access_token": access_token,
        "refresh_token": _first_non_empty(
            getattr(result, "refresh_token", ""),
            session.get("refreshToken"),
            session.get("refresh_token"),
        ),
        "id_token": _first_non_empty(
            getattr(result, "id_token", ""),
            session.get("idToken"),
            session.get("id_token"),
        ),
    }


def _create_new_cloud_mailbox(mail_config: dict, proxy: str, log: LogFn | None = None):
    """按独立 mail_config 建立 Cloud Mail 新邮箱，返回 (mailbox, mailbox_account, new_email)。

    新邮箱域名取 mail_config 显式 domain / domains 第一个候选（子域名分配后任务层
    会把 domain 覆写为「子域名.主域名」）。cloud-mail 创建 API（/api/public/addUser）
    按完整地址 {本地部分}@{域名} 注册，地址的域名部分由客户端指定；创建地址的
    逐字校验由 _verify_new_email_address 在调用方完成（新旧邮箱相同检查之后）。
    """
    logger = log if callable(log) else (lambda _message: None)
    full_domain = _select_cloud_mail_domain(mail_config)
    if not full_domain:
        raise RuntimeError("mail_config 缺少新邮箱域名（domains）")
    mailbox = CFWorkerMailbox(
        api_url=str(mail_config.get("api_url") or "").strip(),
        admin_token=_first_non_empty(mail_config.get("api_token"), mail_config.get("admin_token")),
        domain=full_domain,
        proxy=proxy or None,
    )
    try:
        mailbox_account = mailbox.get_email()
    except RuntimeError as exc:
        if "未启用邮箱域名" not in str(exc):
            raise
        # worker domainList 预检未包含该（子）域名：cloud-mail 创建 API 本身按完整
        # 地址注册，跳过该客户端预检重试一次（不改 worker、不改协议语义）。
        logger("[email_rebind] Cloud Mail 域名预检未包含 " + full_domain + "，按完整地址重试 addUser")
        mailbox._api_mode = "cloud_mail"
        mailbox_account = mailbox.get_email()
    new_email = str(getattr(mailbox_account, "email", "") or "").strip()
    if not new_email:
        raise RuntimeError("Cloud Mail 未返回新邮箱地址")
    return mailbox, mailbox_account, new_email


def _verify_new_email_address(new_email: str, full_domain: str) -> None:
    """校验创建地址等于 {随机本地部分}@{目标域名}；域名不符或本地部分不合法立即失败。"""
    local_part, _, actual_domain = str(new_email or "").lower().partition("@")
    if actual_domain != str(full_domain or "").lower():
        raise RuntimeError(
            "Cloud Mail 返回邮箱域名 " + (actual_domain or "空") + " 与目标 " + str(full_domain or "").lower() + " 不符"
        )
    if not re.fullmatch(r"[a-z0-9]+", local_part or ""):
        raise RuntimeError("Cloud Mail 返回邮箱本地部分不合法: " + new_email)


def _open_change_email_session():
    """与 switch.py 一致：curl_cffi chrome124 指纹会话。"""
    return curl_requests.Session(impersonate="chrome124")


def _change_email_headers(access_token: str) -> dict:
    return {
        "authorization": "Bearer " + access_token,
        "content-type": "application/json",
        "accept": "application/json, text/plain, */*",
        "origin": CHATGPT_BASE_URL,
        "referer": CHATGPT_BASE_URL + "/",
        "user-agent": _PROTOCOL_USER_AGENT,
        "connection": "keep-alive",
    }


def _rebind_account_email(
    account: dict, mail_config: dict, *, proxy: str, log: LogFn
) -> dict:
    old_email = str(account.get("email") or "").strip()

    if not old_email:
        return _failure("", "", "账号缺少邮箱")
    api_url = str(mail_config.get("api_url") or "").strip()
    api_token = _first_non_empty(mail_config.get("api_token"), mail_config.get("admin_token"))
    if not api_url or not api_token:
        return _failure(old_email, "", "mail_config 缺少 api_url 或 api_token")
    if not _select_cloud_mail_domain(mail_config):
        return _failure(old_email, "", "mail_config 缺少新邮箱域名（domains）")

    # 1) 旧邮箱 mailbox resource -> 收码 email_service
    account_obj = Account(
        platform="chatgpt",
        email=old_email,
        password=str(account.get("password") or ""),
        extra=_enhance_old_mailbox_account_extra(account, mail_config),
    )
    email_service, mailbox_error = _resolve_account_mailbox_email_service(account_obj, log, proxy)
    if email_service is None:
        return _failure(old_email, "", "旧邮箱收码服务不可用: " + str(mailbox_error))

    # 2) 旧邮箱 OTP 新鲜登录
    log("[email_rebind] " + old_email + " 开始旧邮箱 OTP 新鲜登录 proxy=" + (proxy or "直连"))
    try:
        login = _fresh_login_with_old_email_otp(account_obj, email_service, proxy, log)
    except Exception as exc:
        return _failure(old_email, "", "重新登录异常: " + str(exc)[:300])
    if not login.get("ok"):
        return _failure(old_email, "", "重新登录失败: " + str(login.get("error") or ""))
    access_token = login["access_token"]
    log("[email_rebind] " + old_email + " 重新登录成功，拿到 accessToken")

    # 3) 依据独立 mail_config 建立 Cloud Mail 新邮箱
    try:
        mailbox, mailbox_account, new_email = _create_new_cloud_mailbox(mail_config, proxy, log)
    except Exception as exc:
        return _failure(old_email, "", "新 Cloud Mail 邮箱创建失败: " + str(exc)[:300])
    if new_email.lower() == old_email.lower():
        return _failure(old_email, new_email, "新旧邮箱相同")
    try:
        _verify_new_email_address(new_email, _select_cloud_mail_domain(mail_config))
    except Exception as exc:
        return _failure(old_email, new_email, "新 Cloud Mail 邮箱创建失败: " + str(exc)[:300])
    mailbox_extra = getattr(mailbox_account, "extra", None) or {}
    mailbox_resource = (
        dict(mailbox_extra.get("provider_resource"))
        if isinstance(mailbox_extra, dict) and mailbox_extra.get("provider_resource")
        else {}
    )
    log("[email_rebind] " + old_email + " 新邮箱已生成: " + new_email)

    try:
        before_ids = set(mailbox.get_current_ids(mailbox_account) or set())
    except Exception:
        before_ids = set()

    # 4) change_email/begin + 新邮箱 OTP + change_email/verify
    session = _open_change_email_session()
    proxy_kwargs = build_cffi_proxy_request_kwargs(proxy)
    headers = _change_email_headers(access_token)
    try:
        log("[email_rebind] " + old_email + " change_email/begin 请求 email=" + new_email)
        r_begin = session.post(
            CHATGPT_BASE_URL + _CHANGE_EMAIL_BEGIN_PATH,
            json={"email": new_email},
            headers=headers,
            timeout=30,
            **proxy_kwargs,
        )
        begin_status = int(getattr(r_begin, "status_code", 0) or 0)
        log(
            "[email_rebind] " + old_email + " change_email/begin 响应 status="
            + str(begin_status) + " resp=" + _resp_snippet(r_begin)
        )
        if begin_status != 200:
            return _failure(
                old_email,
                new_email,
                "change_email/begin " + str(begin_status) + ": " + _resp_snippet(r_begin),
            )
        log("[email_rebind] " + old_email + " change_email/begin 成功，等待新邮箱验证码")

        wait_timeout = max(
            _NEW_CODE_WAIT_TIMEOUT_FLOOR,
            float(mail_config.get("wait_timeout") or _NEW_CODE_WAIT_TIMEOUT_FLOOR),
        )
        try:
            new_code = mailbox.wait_for_code(
                mailbox_account,
                keyword="",
                timeout=int(wait_timeout),
                before_ids=before_ids,
            )
        except TimeoutError:
            return _failure(old_email, new_email, "新邮箱验证码超时未收到")
        except Exception as exc:
            return _failure(old_email, new_email, "取新邮箱验证码失败: " + str(exc)[:300])
        if not new_code:
            return _failure(old_email, new_email, "新邮箱验证码超时未收到")

        log("[email_rebind] " + old_email + " change_email/verify 请求 email=" + new_email + " code=" + str(new_code))
        r_verify = session.post(
            CHATGPT_BASE_URL + _CHANGE_EMAIL_VERIFY_PATH,
            json={"email": new_email, "code": new_code},
            headers=headers,
            timeout=30,
            **proxy_kwargs,
        )
        verify_status = int(getattr(r_verify, "status_code", 0) or 0)
        log(
            "[email_rebind] " + old_email + " change_email/verify 响应 status="
            + str(verify_status) + " resp=" + _resp_snippet(r_verify)
        )
        if verify_status != 200:
            return _failure(
                old_email,
                new_email,
                "change_email/verify " + str(verify_status) + ": " + _resp_snippet(r_verify),
            )
        log("[email_rebind] " + old_email + " change_email/verify 成功")
    finally:
        try:
            session.close()
        except Exception:
            pass

    log("[email_rebind] " + old_email + " 换绑成功 -> " + new_email + "，保留新登录 tokens")
    success = {
        "ok": True,
        "old_email": old_email,
        "new_email": new_email,
        "access_token": access_token,
    }
    refresh_token = str(login.get("refresh_token") or "").strip()
    if refresh_token:
        success["refresh_token"] = refresh_token
    id_token = str(login.get("id_token") or "").strip()
    if id_token:
        success["id_token"] = id_token
    if mailbox_resource:
        success["mailbox_resource"] = mailbox_resource
    return success


def rebind_account_email(
    account: dict, mail_config: dict, *, proxy: str = "", log_fn: LogFn | None = None
) -> dict:
    """单账号协议换绑邮箱（批处理安全入口，失败不抛出）。

    参数:
        account: 账号 graph 字典，至少包含 email；可选 password、extra
                 （extra 内为 mailbox resource 信息，用于读取旧邮箱验证码）。
        mail_config: 独立 Cloud Mail 配置，至少包含 api_url、api_token、domains；
                     可选 domain（指定单个域名）、wait_timeout（新邮箱收码超时秒数）。
        proxy: 可选代理 URL，用于登录引擎与 change_email 协议请求。
        log_fn: 可选日志回调；缺省使用 print。

    返回:
        成功: {"ok": True, "old_email", "new_email", "access_token",
               "refresh_token"?, "id_token"?, "mailbox_resource"?}
        失败: {"ok": False, "old_email", "new_email", "error"}
    """
    log = _make_logger(log_fn)
    safe_account = account if isinstance(account, dict) else {}
    safe_mail_config = mail_config if isinstance(mail_config, dict) else {}
    old_email = str(safe_account.get("email") or "").strip()
    try:
        result = _rebind_account_email(
            safe_account,
            safe_mail_config,
            proxy=str(proxy or "").strip(),
            log=log,
        )
    except Exception as exc:  # 批处理外层兜底：任何异常都转结构化 error
        log("[email_rebind] " + old_email + " 换绑异常: " + str(exc))
        return _failure(old_email, "", "换绑异常: " + str(exc)[:300])
    if not result.get("ok"):
        log("[email_rebind] " + old_email + " 换绑失败: " + str(result.get("error") or ""))
    return result
