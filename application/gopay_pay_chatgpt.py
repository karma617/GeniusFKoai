"""GoPay 协议付款 ChatGPT Plus 编排器。

整条流水线：

  ① **准备**：租用/注册 GoPay 号，校验接码 TTL，并刷新真实余额

  ② **协议**：调 ``platforms.chatgpt.payment.generate_plus_link(country=ID, currency=IDR)``
      拿到 ChatGPT 的 cashier_url（Stripe hosted checkout）

  ③ **浏览器**：打开 cashier_url 并抓取唯一的 ``midtrans_url``

  ④ **协议**：校验金额和币种后执行 GoPay 付款，再查询 OpenAI 订阅状态

设计原则：
- **不依赖** ``platforms/gopay-deploy`` 的 Payment Inbox 服务（Inbox 只是 worker 的 job 队列源）
- 复用 ``GoPayPayment`` 协议类（已经被 ``ensure_opai_on_path`` 加到 sys.path）
- 四阶段串行，整段失败任意一步就标 FAILED；中间产物（cashier_url / midtrans_url）写进 task result 方便排查
- 单条 ChatGPT × 单条 GoPay 号一一配对（concurrency=1 时）
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from core.db import AccountModel, engine, save_account
from core.platform_accounts import build_platform_account


_MIDTRANS_URL_RE = re.compile(
    r"https?://app\.midtrans\.com/snap/v[34]/redirection/[0-9a-f-]{36}",
    re.IGNORECASE,
)


def _mask_proxy(proxy: str | None) -> str:
    """脱敏代理 URL 用于日志：只保留 host:port，把 user:pass 替换成 ***。"""
    value = str(proxy or "").strip()
    if not value or "@" not in value:
        return value
    scheme, _, rest = value.partition("://")
    if not rest:
        return value
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def _normalize_proxy_url(proxy: str | None) -> str:
    """规范化代理 URL：缺 scheme 时自动补 ``http://``。

    数据库里存的代理常是裸 ``user:pass@host:port``（没有协议前缀），
    ``tls_client`` 等严格 URL 解析器会报 ``first path segment in URL
    cannot contain colon``。这里统一补前缀避免下游崩溃。
    """
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    return f"http://{value}"


_CHECKOUT_URL_SESSION_RE = re.compile(r"^(?:oaics_|cs_)[A-Za-z0-9_]+$")


def _parse_short_link_id(cashier_url: str) -> tuple[str, str]:
    """解析短链 https://chatgpt.com/checkout/{entity}/{session_id}。

    返回 (processor_entity, checkout_session_id)；URL 非法时给清晰报错。
    """
    from urllib.parse import urlsplit

    url = str(cashier_url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "chatgpt.com":
        raise RuntimeError(
            f"cashier URL 非法（需要 https://chatgpt.com/checkout/<entity>/<session_id>）：{url[:100]}"
        )
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2 or parts[0] != "checkout":
        raise RuntimeError(
            f"cashier URL 路径非法（需要 /checkout/<entity>/<session_id>）：{url[:100]}"
        )
    entity, session_id = parts[1], parts[2]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", entity):
        raise RuntimeError(f"cashier URL 中 processor_entity 非法：{entity}")
    if not _CHECKOUT_URL_SESSION_RE.fullmatch(session_id):
        raise RuntimeError(f"cashier URL 中 checkout_session_id 非法：{session_id}")
    return entity, session_id


class PhoneTTLGuard:
    """Hero-SMS 号码 20 分钟自动回收的护栏。

    流水线从开始（注册拿号）起算，每跨一步调一次 ``check()``；超过
    ``ttl_seconds`` 即抛 ``RuntimeError``，调用方据此判失败重开任务。
    用 ``time.monotonic`` 避免系统时钟回拨干扰。
    """

    def __init__(self, ttl_seconds: int = 1200):
        self.ttl_seconds = max(int(ttl_seconds or 0), 0)
        self._start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def reset_remaining(self, remaining_seconds: float) -> None:
        self.ttl_seconds = max(int(remaining_seconds), 0)
        self._start = time.monotonic()

    def check(self) -> None:
        if self.ttl_seconds <= 0:
            return
        if self.elapsed() > self.ttl_seconds:
            raise RuntimeError(
                f"Hero-SMS 号码有效期({self.ttl_seconds // 60}min)已过，"
                f"本次任务判失败（已耗时 {int(self.elapsed())}s）"
            )


def claim_envelope_for_account(client, envelope_url: str, *, log: Callable[[str], None] = print) -> bool:
    """给已登录的 GoPay client 领一个红包。

    ``envelope_url`` 形如 ``https://app.gopay.co.id/NF8p/qps2s1y0``。空 URL
    直接返回 False。任何异常都吞掉返回 False（领红包失败不该让整条流水线崩）。
    """
    url = str(envelope_url or "").strip()
    if not url:
        return False
    try:
        from platforms.gopay._opai_loader import ensure_opai_on_path

        ensure_opai_on_path()
        from opai.core.envelope_manager import EnvelopeManager

        mgr = EnvelopeManager()
        mgr.add_url(url)
        result = mgr.claim_one(client)
        ok = bool(result)
        log(f"红包领取{'成功' if ok else '失败/无可用红包'}: ...{url[-8:]}")
        return ok
    except Exception as exc:
        log(f"红包领取异常（忽略）: {exc}")
        return False


def _resolve_gopay_client(
    phone: str,
    proxy: str,
    *,
    log: Callable[[str], None] = print,
):
    """Resume a persisted GoPay protocol account and return its authenticated client."""
    try:
        from platforms.gopay._opai_loader import ensure_opai_on_path

        ensure_opai_on_path()
        from opai.core.gopay_protocol_worker import _resume_account

        resumed = _resume_account(
            str(phone or "").strip(),
            proxy=_normalize_proxy_url(proxy),
        )
        client = resumed.get("client") if isinstance(resumed, dict) else None
        if client is None:
            log(f"GoPay 账号 ***{str(phone or '')[-4:]} 恢复登录失败")
        return client
    except Exception as exc:
        log(
            f"GoPay 账号 ***{str(phone or '')[-4:]} 恢复登录异常: "
            f"{type(exc).__name__}"
        )
        return None


def register_gopay_account(
    *,
    herosms_api_key: str,
    pin: str = "147258",
    proxy: str = "",
    envelope_url: str = "",
    sms_provider: str = "herosms",
    smspool_api_key: str = "",
    smsbower_api_key: str = "",
    five_sim_api_key: str = "",
    smsapi_url: str = "",
    smsapi_phone: str = "",
    herosms_max_price_usd: str = "",
    smspool_max_price: str = "",
    auto_rebind: bool = False,
    rebind_provider: str = "herosms",
    rebind_sms_key: str = "",
    rebind_country: str = "",
    rebind_service: str = "",
    log: Callable[[str], None] = print,
) -> AccountModel | None:
    """自动注册一个新 GoPay 号并入库，返回 AccountModel。

    流程：调 GoPay plugin 的 ``register()``（内含拿号 + 注册 OTP + PIN OTP，
    见 platforms/gopay/plugin.py）→ ``save_account`` 入库 → 若余额 0 且给了
    红包链接则 resume client 领红包补余额 → 返回最新 AccountModel。

    ``sms_provider``: herosms（默认，herosms_api_key 为空时回退
    OPAI_HEROSMS_API_KEY）或 smspool（用 smspool_api_key，缺省走内置默认 key）。
    ``herosms_max_price_usd`` / ``smspool_max_price``: 拿号价格上限，空则
    走插件默认（0.11）。

    失败返回 None（不抛，让调用方决定是否继续）。
    """
    from core.base_platform import RegisterConfig
    from core.registry import get as get_platform

    provider = str(sms_provider or "herosms").strip().lower()
    api_key = str(herosms_api_key or "").strip()
    if provider == "herosms" and not api_key:
        api_key = str(os.environ.get("OPAI_HEROSMS_API_KEY", "") or "").strip()
    if provider == "herosms" and not api_key:
        log("自动注册 GoPay 失败：缺少 Hero-SMS API key")
        return None

    # 没显式传代理时，从主项目代理池取一个 ID 区域的代理（动态代理优先，
    # 失败回退静态池）。GoPay 的注册接口对印尼区出口 IP 敏感，直连容易被
    # WAF 403 / 风控；用代理池能稳得多。代理池也没号时回退直连。
    effective_proxy = _normalize_proxy_url(proxy)
    if not effective_proxy:
        try:
            from core.proxy_pool import proxy_pool

            picked = proxy_pool.get_next(region="ID") or ""
            if picked:
                effective_proxy = _normalize_proxy_url(picked)
                log(f"代理池分配：{_mask_proxy(effective_proxy)}（GoPay 注册用）")
            else:
                log("代理池为空，GoPay 注册回退直连")
        except Exception as exc:
            log(f"代理池调用异常，GoPay 注册回退直连：{exc}")

    cfg = RegisterConfig(
        executor_type="protocol",
        captcha_solver="auto",
        proxy=effective_proxy or None,
        extra={
            "identity_provider": "phone",
            "herosms_api_key": api_key,
            "gopay_pin": str(pin or "147258"),
            "gopay_proxy": effective_proxy or "",
            "sms_provider": provider,
            "smspool_api_key": str(smspool_api_key or ""),
            "smsbower_api_key": str(smsbower_api_key or ""),
            "five_sim_api_key": str(five_sim_api_key or ""),
            "five_sim_country": "indonesia",
            "five_sim_product": "gojek",
            # 5sim 不复用 Hero/SMSPool 默认 0.11 美元上限，避免误判无号。
            "five_sim_max_price": "",
            "smsapi_url": str(smsapi_url or ""),
            "smsapi_phone": str(smsapi_phone or ""),
            "herosms_max_price_usd": str(herosms_max_price_usd or ""),
            "smspool_max_price": str(smspool_max_price or ""),
            # auto_rebind：号已注册时登录+换绑释放再重注册（换绑渠道独立）
            "auto_rebind": bool(auto_rebind),
            "rebind_provider": str(rebind_provider or "herosms"),
            "rebind_sms_key": str(rebind_sms_key or ""),
            "rebind_country": str(rebind_country or ""),
            "rebind_service": str(rebind_service or ""),
        },
    )
    try:
        platform_cls = get_platform("gopay")
        platform = platform_cls(config=cfg)
        if hasattr(platform, "set_logger"):
            platform.set_logger(log)
        log("没有可用 GoPay 号，开始自动注册新号…")
        account = platform.register()
    except Exception as exc:
        log(f"自动注册 GoPay 失败: {exc}")
        return None

    save_account(account)
    # ``save_account`` 返回的 model 出了它内部的 session 就 detached，
    # 访问 ``.id`` 会触发懒加载报 DetachedInstanceError。用 email 重新查一次
    # 拿稳定的 id。
    with Session(engine) as session:
        fresh = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "gopay")
            .where(AccountModel.email == account.email)
        ).first()
        if not fresh:
            log("GoPay 自动注册入库后查不到记录，异常")
            return None
        model_id = int(fresh.id)
    log(f"GoPay 自动注册成功并入库: #{model_id} ***{str(account.email)[-4:]}")

    # 余额 0 + 有红包链接 → 领红包补余额
    extra = dict(getattr(account, "extra", {}) or {})
    balance_rp = int(extra.get("balance_rp") or 0)
    env_url = str(envelope_url or "").strip()
    if balance_rp < 1 and env_url:
        phone = str(extra.get("phone") or account.email or "").strip()
        try:
            from platforms.gopay._opai_loader import ensure_opai_on_path

            ensure_opai_on_path()
            from opai.core.gopay_protocol_worker import _resume_account, _check_balance

            resumed = _resume_account(phone, proxy=_normalize_proxy_url(proxy))
            if resumed and claim_envelope_for_account(resumed["client"], env_url, log=log):
                new_balance = max(int(_check_balance(resumed["client"]) or 0), 0)
                log(f"自动注册号领红包后余额 = {new_balance} IDR")
                from core.account_graph import patch_account_graph

                with Session(engine) as session:
                    m = session.get(AccountModel, model_id)
                    if m:
                        patch_account_graph(session, m, summary_updates={"balance_rp": new_balance})
                        session.commit()
        except Exception as exc:
            log(f"自动注册号领红包异常（忽略）: {exc}")

    with Session(engine) as session:
        return session.get(AccountModel, model_id)


def acquire_gopay_via_rebind(
    *,
    herosms_api_key: str = "",
    pin: str = "147258",
    proxy: str = "",
    sms_provider: str = "herosms",
    smspool_api_key: str = "",
    smsbower_api_key: str = "",
    smsapi_url: str = "",
    smsapi_phone: str = "",
    herosms_max_price_usd: str = "",
    smspool_max_price: str = "",
    log: Callable[[str], None] = print,
) -> AccountModel | None:
    """换绑获号：成熟老账号改绑到新拿的未注册号，入库后返回 AccountModel。

    用户验证过的正确方向：风控判账号不判手机号。新注册号秒付会被 FDS 拒，
    所以取一个成熟老账号（本地 ``gopay_worker_accounts.json`` 里 refresh_token
    还活的）改绑到一个干净的新号，用新号 + 老账号身份进支付流程。

    接码渠道用于「拿新号 + 接换绑 OTP + 后续付款 OTP」，新号必须能接码
    （所以这里用注册同款渠道：herosms / smspool / smsbower；smsapi 固定号
    没法当"新号"用，会在 plugin 里报错）。失败返回 None。
    """
    from core.base_platform import RegisterConfig
    from core.registry import get as get_platform

    provider = str(sms_provider or "herosms").strip().lower()
    if provider in {"smsapi", "api_sms"}:
        log("换绑获号不支持 smsapi 固定号渠道（新号必须能独立接码），请改用 herosms/smspool/smsbower")
        return None

    effective_proxy = _normalize_proxy_url(proxy)
    if not effective_proxy:
        try:
            from core.proxy_pool import proxy_pool

            picked = proxy_pool.get_next(region="ID") or ""
            if picked:
                effective_proxy = _normalize_proxy_url(picked)
                log(f"代理池分配：{_mask_proxy(effective_proxy)}（GoPay 换绑获号用）")
            else:
                log("代理池为空，GoPay 换绑获号回退直连")
        except Exception as exc:
            log(f"代理池调用异常，GoPay 换绑获号回退直连：{exc}")

    cfg = RegisterConfig(
        executor_type="protocol",
        captcha_solver="auto",
        proxy=effective_proxy or None,
        extra={
            "identity_provider": "phone",
            "herosms_api_key": str(herosms_api_key or ""),
            "gopay_pin": str(pin or "147258"),
            "gopay_proxy": effective_proxy or "",
            "sms_provider": provider,
            "smspool_api_key": str(smspool_api_key or ""),
            "smsbower_api_key": str(smsbower_api_key or ""),
            "smsapi_url": str(smsapi_url or ""),
            "smsapi_phone": str(smsapi_phone or ""),
            "herosms_max_price_usd": str(herosms_max_price_usd or ""),
            "smspool_max_price": str(smspool_max_price or ""),
        },
    )
    try:
        platform_cls = get_platform("gopay")
        platform = platform_cls(config=cfg)
        if hasattr(platform, "set_logger"):
            platform.set_logger(log)
        log("开始换绑获号：成熟老账号改绑到新号…")
        account = platform.acquire_via_rebind()
    except Exception as exc:
        log(f"换绑获号失败: {exc}")
        return None

    save_account(account)
    with Session(engine) as session:
        fresh = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "gopay")
            .where(AccountModel.email == account.email)
        ).first()
        if not fresh:
            log("换绑获号入库后查不到记录，异常")
            return None
        model_id = int(fresh.id)
    log(f"换绑获号成功并入库: #{model_id} ***{str(account.email)[-4:]}")

    with Session(engine) as session:
        return session.get(AccountModel, model_id)


# ===========================================================================
# 换绑（改绑新号 + 释放旧号）编排
# ===========================================================================

def _build_rebind_otp_callback(
    *,
    rebind_provider: str = "herosms",
    rebind_sms_key: str = "",
    country: str = "",
    service: str = "",
    log: Callable[[str], None] = print,
):
    """买一个换绑用的新印尼号，返回 ``(new_phone, wait_otp, finish, cancel, meta)``。

    **换绑渠道独立于注册渠道**：注册可能用 smsapi（固定号，没法买一次性号），
    换绑必须走能买一次性号的渠道（herosms / smsbower，SMS-Activate 风格）。
    换绑后的新号要继续用于下一轮 GoPay 付款，所以买的是**印尼号**（country=6，
    见 sms_channel 默认）。

    返回：
      new_phone: 新印尼号（+62...）
      wait_otp(phone, timeout)->code: 接新号的换绑/付款 OTP
      finish(): 用完归还（付款全部结束后才调）
      cancel(): 失败取消
      meta: ``{"provider","aid","sms_key"}``——付款阶段要用同渠道+同 aid 接
            新号的 midtrans OTP，所以把这些透传出去。
    买号失败返回 ``(None, None, None, None, None)``。
    """
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()

    provider = str(rebind_provider or "herosms").strip().lower()
    key = str(rebind_sms_key or "").strip()

    if provider == "smsbower":
        from platforms.gopay.sms_channel import make_smsbower_channel
        key = key or os.environ.get("OPAI_SMSBOWER_API_KEY", "").strip()
        if not key:
            log("换绑失败：缺少 SMSBower API key（买换绑新号用）")
            return None, None, None, None, None
        channel = make_smsbower_channel(api_key=key, country=country, service=service)
    else:
        # 默认 Hero-SMS
        from platforms.gopay.sms_channel import make_herosms_rebind_channel
        key = key or os.environ.get("OPAI_HEROSMS_API_KEY", "").strip()
        if not key:
            log("换绑失败：缺少 Hero-SMS API key（买换绑新号用）")
            return None, None, None, None, None
        channel = make_herosms_rebind_channel(api_key=key, country=country, service=service)

    new_phone, aid = channel.get_number()
    if not new_phone or not aid:
        log(f"换绑失败：{provider} 没买到换绑新号")
        return None, None, None, None, None
    log(f"换绑新印尼号已购（{provider}）：***{new_phone[-4:]}（activation=***{aid[-4:]}）")

    def _wait_otp(_phone_arg: str = "", timeout: int = 180) -> Optional[str]:
        try:
            channel.request_another(aid)
        except Exception:
            pass
        time.sleep(2)
        return channel.wait_code(aid, timeout=timeout)

    def _finish() -> None:
        try:
            channel.done(aid)
        except Exception:
            pass

    def _cancel() -> None:
        try:
            channel.cancel(aid)
        except Exception:
            pass

    meta = {"provider": provider, "aid": str(aid), "sms_key": key}
    return new_phone, _wait_otp, _finish, _cancel, meta


def rebind_release_phone(
    client,
    *,
    pin: str,
    rebind_provider: str = "herosms",
    rebind_sms_key: str = "",
    rebind_country: str = "",
    rebind_service: str = "",
    log: Callable[[str], None] = print,
) -> dict:
    """把已登录账号换绑到一个新临时号，从而释放它当前占用的（印尼）号。

    返回 ``{"success": bool, "detail": str, "new_phone": str}``。
    """
    new_phone, wait_otp, finish, cancel, _meta = _build_rebind_otp_callback(
        rebind_provider=rebind_provider,
        rebind_sms_key=rebind_sms_key,
        country=rebind_country,
        service=rebind_service,
        log=log,
    )
    if not new_phone:
        return {"success": False, "detail": "换绑临时号获取失败", "new_phone": ""}
    try:
        res = client.rebind_phone(
            new_phone=new_phone, pin=pin, wait_otp=wait_otp,
            otp_timeout=180, log=log,
        )
        if res.get("success"):
            finish()
        else:
            cancel()
        return res
    except Exception as exc:
        cancel()
        return {"success": False, "detail": f"换绑异常: {exc}", "new_phone": new_phone}


def login_and_rebind_release(
    *,
    phone: str,
    pin: str,
    proxy: str = "",
    login_sms_key: str = "",
    use_pin: bool = True,
    rebind_provider: str = "herosms",
    rebind_sms_key: str = "",
    rebind_country: str = "",
    rebind_service: str = "",
    log: Callable[[str], None] = print,
) -> dict:
    """#1：登录一个**已注册**的号 → 换绑到新临时号 → 释放原号 ``phone``。

    释放后原号 ``phone`` 可以拿去重新注册新账号。返回换绑结果（含 released_phone）。
    ``login_sms_key``：登录走 OTP 时接码用（PIN 强登则用不到）。
    """
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core.gopay_protocol_worker import _login_one

    eff_proxy = _normalize_proxy_url(proxy)
    if not eff_proxy:
        try:
            from core.proxy_pool import proxy_pool

            picked = proxy_pool.get_next(region="ID") or ""
            if picked:
                eff_proxy = _normalize_proxy_url(picked)
                log(f"代理池分配：{_mask_proxy(eff_proxy)}（换绑登录用）")
        except Exception:
            pass

    log(f"换绑流程：登录已注册号 ***{phone[-4:]}…")
    logged = _login_one(phone, pin, eff_proxy, use_pin=use_pin, api_key=login_sms_key)
    if not logged or not logged.get("client"):
        return {"success": False, "detail": f"登录 {phone} 失败，无法换绑", "released_phone": ""}

    res = rebind_release_phone(
        logged["client"], pin=pin,
        rebind_provider=rebind_provider, rebind_sms_key=rebind_sms_key,
        rebind_country=rebind_country, rebind_service=rebind_service, log=log,
    )
    res["released_phone"] = phone if res.get("success") else ""
    return res


def wait_for_balance(
    *,
    client,
    envelope_url: str,
    ttl_guard: "PhoneTTLGuard",
    poll_interval: float = 15.0,
    cancel_check: Optional[Callable[[], bool]] = None,
    log: Callable[[str], None] = print,
) -> int:
    """轮询 GoPay 余额直到 ≥ 1 IDR，否则一直等到 ``ttl_guard`` 超时抛错。

    每轮：若给了 ``envelope_url`` 先尝试领红包补余额，再查余额。余额 ≥ 1
    立即返回。不再因"某次查到 0"就判失败——红包/充值到账有延迟，必须等。

    Args:
        client: 已登录的 GoPay client（``_resume_account`` 返回的 client）
        envelope_url: 红包链接，空则只查余额不领红包
        ttl_guard: 20 分钟号码有效期护栏；超时由它抛 RuntimeError
        poll_interval: 两次查询间隔秒数
    """
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core.gopay_protocol_worker import _check_balance

    env_url = str(envelope_url or "").strip()
    round_no = 0
    while True:
        if cancel_check and cancel_check():
            raise RuntimeError("任务已取消")
        # 先检查 TTL——超时抛错（任务判失败重开）
        ttl_guard.check()
        round_no += 1
        if env_url:
            try:
                claim_envelope_for_account(client, env_url, log=log)
            except Exception as exc:
                log(f"轮询领红包异常（忽略）: {exc}")
        try:
            balance = max(int(_check_balance(client) or 0), 0)
        except Exception:
            balance = 0
        log(f"余额轮询第 {round_no} 轮：{balance} IDR")
        if balance >= 1:
            return balance
        sleep_left = max(float(poll_interval or 0), 0)
        while sleep_left > 0:
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")
            chunk = min(sleep_left, 0.5)
            time.sleep(chunk)
            sleep_left -= chunk


def _account_extra(account_model: AccountModel) -> dict:
    """从 ``AccountModel`` 通过 ``build_platform_account`` 读出统一 extra。

    主项目里 ``AccountModel`` 自身只有 platform/email/password/user_id 这
    几列，``extra`` 实际上是从 ``AccountOverviewModel.summary_json`` +
    credentials + provider_accounts/resources 等多张表拼出来的，必须走
    ``build_platform_account`` 才能读到 plugin 写进去的 ``phone_local``、
    ``pin``、``herosms_activation_id`` 等字段。

    这些字段实际是写在 overview 的 ``summary_json`` 里，``build_platform_extra``
    会把它们整体放在 ``extra["account_overview"]``——所以这里把 overview
    字段也合并提到顶层，方便调用方按 ``extra["balance_rp"]`` / ``extra["pin"]``
    这种习惯写法直接读取。
    """
    if not account_model:
        return {}
    with Session(engine) as session:
        merged = session.merge(account_model, load=False)
        platform_account = build_platform_account(session, merged)
    extra = getattr(platform_account, "extra", {}) or {}
    if not isinstance(extra, dict):
        return {}
    merged_extra: dict[str, Any] = dict(extra)
    overview = extra.get("account_overview")
    if isinstance(overview, dict):
        # overview 里的字段优先级**低于**已存在的顶层字段（避免覆盖
        # plugin 主动写到 credentials 里的同名 key）
        for k, v in overview.items():
            merged_extra.setdefault(k, v)
    return merged_extra


def _remaining_sms_lifetime_seconds(
    account: AccountModel,
    extra: dict[str, Any],
    default_ttl_seconds: int,
) -> float | None:
    """Return remaining activation lifetime; fixed API numbers have no expiry."""
    provider = str(extra.get("sms_provider") or "herosms").strip().lower()
    if provider in {"smsapi", "api_sms"}:
        return None
    expires_raw = str(extra.get("sms_expires_at") or "").strip()
    try:
        if expires_raw:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            created_at = account.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            expires_at = created_at + timedelta(seconds=max(int(default_ttl_seconds), 0))
        return (expires_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    except Exception as exc:
        raise RuntimeError(f"GoPay 账号短信激活有效期无效: {exc}") from exc


def find_chatgpt_account(account_id: int) -> AccountModel | None:
    with Session(engine) as session:
        m = session.get(AccountModel, int(account_id))
        if not m or m.platform != "chatgpt":
            return None
        return m


def pick_available_gopay_account(
    min_balance_rp: int = 1,
    *,
    owner_key: str = "",
    task_id: str = "",
) -> AccountModel | None:
    """Lease the newest resumable GoPay account without trusting cached balance.

    The selected account is checked against the live balance endpoint before checkout.
    """
    with Session(engine) as session:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "gopay")
            .order_by(AccountModel.created_at.desc())
            .limit(50)
        ).all()
        for m in rows:
            extra = _account_extra(m)
            phone = str(extra.get("phone") or m.email or "").strip()
            pin = str(extra.get("pin") or m.password or "").strip()
            if not phone or not pin:
                continue
            if owner_key:
                from application.gopay_payment_state import acquire_gopay_lease

                if not acquire_gopay_lease(
                    account_id=int(m.id), owner_key=owner_key, task_id=task_id
                ):
                    continue
            return m
    return None


def _refresh_chatgpt_checkout_auth(account, *, proxy: str | None, log: Callable[[str], None]):
    """用已保存的 session/refresh token 刷新失效 AT，并同步账号图凭证。"""
    from platforms.chatgpt.token_refresh import TokenRefreshManager

    extra = dict(getattr(account, "extra", {}) or {})

    class _RefreshTarget:
        pass

    target = _RefreshTarget()
    target.email = account.email
    target.access_token = str(extra.get("access_token") or getattr(account, "token", "") or "")
    target.refresh_token = str(extra.get("refresh_token") or "")
    target.session_token = str(extra.get("session_token") or "")
    target.cookies = str(extra.get("cookies") or "")
    target.client_id = str(extra.get("client_id") or extra.get("clientId") or "")

    if not target.session_token and not target.refresh_token:
        raise RuntimeError("ChatGPT 鉴权已失效，账号没有可用于自动刷新的 session_token/refresh_token，请重新登录")

    result = TokenRefreshManager(proxy_url=proxy).refresh_account(target)
    if not result.success or not result.access_token:
        detail = str(result.error_message or "刷新服务未返回 access_token").strip()
        raise RuntimeError(f"ChatGPT 鉴权已失效且自动刷新失败：{detail}；请重新登录该账号")

    account.token = result.access_token
    extra["access_token"] = result.access_token
    if result.refresh_token:
        extra["refresh_token"] = result.refresh_token
    account.extra = extra
    save_account(account)
    log("ChatGPT access_token 已自动刷新并保存，重新生成 cashier_url")
    return account


def _checkout_error_status(exc: Exception) -> int:
    response = getattr(exc, "response", None)
    try:
        return int(
            getattr(exc, "status_code", 0)
            or getattr(exc, "status", 0)
            or getattr(response, "status_code", 0)
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _is_checkout_unusual_activity(exc: Exception) -> bool:
    return (
        _checkout_error_status(exc) == 400
        and "unusual activity" in str(exc).lower()
    )


def step_generate_cashier_url(
    chatgpt_account_model: AccountModel,
    *,
    country: str = "ID",
    currency: str = "IDR",
    proxy: Optional[str] = None,
    use_stripe_init: bool = False,
    use_short_link: bool = False,
    expected_exit_country: str = "ID",
    checkout_context: Optional[dict] = None,
    log: Callable[[str], None] = print,
) -> str:
    """步骤 ①：协议拿 ChatGPT Plus cashier URL。"""
    expected_exit_country = str(expected_exit_country or "").strip().upper()
    if expected_exit_country and not str(proxy or "").strip():
        raise RuntimeError("GoPay GPTPlus 提链必须使用任务代理池中的固定代理")

    from platforms.chatgpt import payment as chatgpt_payment

    with Session(engine) as session:
        account = build_platform_account(session, chatgpt_account_model)

    # ``generate_plus_link`` 期望 ``account.access_token`` / ``account.cookies``，
    # 但 ``build_platform_account`` 返回的 ``Account`` 把 token 放在 ``token``
    # 字段、cookies 在 ``extra`` 里——参考 chatgpt/plugin.py::check_valid 的做法
    # 用一个 SimpleNamespace 适配过去。
    extra = dict(getattr(account, "extra", {}) or {})

    class _AccountAdapter:
        pass

    a = _AccountAdapter()
    a.access_token = str(extra.get("access_token") or getattr(account, "token", "") or "")
    a.cookies = str(extra.get("cookies", "") or "")
    a.chatgpt_account_id = str(extra.get("account_id") or "")
    a.extra = extra
    if not a.access_token:
        raise RuntimeError(
            f"ChatGPT 账号 {account.email} 缺少 access_token，无法生成支付链接"
        )

    log(
        f"协议生成 cashier_url（country={country}, currency={currency}, "
        f"proxy={_mask_proxy(proxy) or '直连'}）"
    )
    if use_short_link:
        log("cashier_url 走短链模式（custom + Plus 优惠 + taxes，同步 processor_entity）")
    elif use_stripe_init:
        log("cashier_url 走 Hosted 长链模式（优先响应 URL，缺失时 Stripe init 补链）")
    # 同一任务账号的 checkout、浏览器和 GoPay 流程必须使用同一个代理，
    # 避免出口 IP 在流水线中切换。
    # 并发场景下 curl_cffi 首次在多线程里初始化 SSL 库会偶发
    # ``curl: (35) TLS connect error ... invalid library`` 竞态——10 个 worker
    # 同时打 cashier API 时极易命中。这里加轻量重试（指数退避）兜底，区分
    # 瞬时 TLS/连接错误（重试）和业务错误（直接抛）。
    last_exc: Exception | None = None
    auth_refresh_attempted = False
    url = ""
    for attempt in range(1, 4):
        try:
            url = chatgpt_payment.generate_plus_link(
                a,
                proxy=proxy,
                country=country,
                currency=currency,
                use_stripe_init=use_stripe_init,
                use_short_link=use_short_link,
                response_log=log,
                expected_exit_country=expected_exit_country,
                checkout_context=checkout_context,
            )
            break
        except Exception as exc:  # noqa: BLE001 - 需按错误内容判断是否重试
            last_exc = exc
            msg = str(exc).lower()
            unauthorized = _checkout_error_status(exc) == 401 or "http 401" in msg
            unusual_activity = _is_checkout_unusual_activity(exc)
            if (unauthorized or unusual_activity) and not auth_refresh_attempted:
                auth_refresh_attempted = True
                try:
                    account = _refresh_chatgpt_checkout_auth(account, proxy=proxy, log=log)
                except Exception as refresh_exc:
                    if unusual_activity:
                        raise RuntimeError(
                            "OpenAI checkout 检测到异常活动，当前账号或固定代理被风控；"
                            "自动刷新会话失败，请更换可用固定代理或账号后重试"
                        ) from refresh_exc
                    raise
                extra = dict(getattr(account, "extra", {}) or {})
                a.access_token = str(extra.get("access_token") or getattr(account, "token", "") or "")
                a.cookies = str(extra.get("cookies") or "")
                continue
            if unauthorized:
                raise RuntimeError("ChatGPT 鉴权仍返回 401，请重新登录该账号后重试") from exc
            if unusual_activity:
                raise RuntimeError(
                    "OpenAI checkout 检测到异常活动，当前账号或固定代理仍被风控；"
                    "未创建付款交易，请更换可用固定代理或账号后重试"
                ) from exc
            transient = (
                "tls connect error" in msg
                or "invalid library" in msg
                or "curl: (35)" in msg
                or "curl: (56)" in msg
                or "connection reset" in msg
                or "failed to perform" in msg
            )
            if attempt >= 3 or not transient:
                raise
            backoff = 0.5 * (2 ** (attempt - 1))
            log(f"cashier_url 生成瞬时失败（第 {attempt}/3 次，{backoff}s 后重试）: {exc}")
            time.sleep(backoff)
    if not url:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ChatGPT API 未返回 cashier URL")
    log(f"cashier_url 已生成: ...{url[-12:]}")
    return url


def _verify_chatgpt_subscription(
    chatgpt_account_model: AccountModel,
    *,
    proxy: str | None,
    cancel_check: Callable[[], bool] | None,
    timeout_seconds: int = 90,
    log: Callable[[str], None] = print,
) -> str:
    """Poll OpenAI until the paid plan is visible; never infer it from Midtrans alone."""
    from platforms.chatgpt import payment as chatgpt_payment

    with Session(engine) as session:
        account = build_platform_account(session, chatgpt_account_model)
    extra = dict(getattr(account, "extra", {}) or {})

    class _AccountAdapter:
        pass

    adapted = _AccountAdapter()
    adapted.access_token = str(extra.get("access_token") or getattr(account, "token", "") or "")
    adapted.cookies = str(extra.get("cookies") or "")
    adapted.email = str(getattr(account, "email", "") or "")
    if not adapted.access_token:
        raise RuntimeError("付款已结算，但 ChatGPT 账号缺少 access_token，无法确认订阅状态")

    deadline = time.monotonic() + max(int(timeout_seconds or 0), 1)
    last_status = "unknown"
    while time.monotonic() < deadline:
        if cancel_check and cancel_check():
            raise RuntimeError("任务已取消")
        try:
            last_status = str(
                chatgpt_payment.check_subscription_status(adapted, proxy=proxy) or "unknown"
            ).strip().lower()
            if last_status in {"plus", "team", "enterprise", "business", "pro"}:
                log(f"OpenAI 订阅状态已确认: {last_status}")
                return last_status
        except Exception as exc:
            log(f"OpenAI 订阅状态暂未确认: {str(exc)[:160]}")
        for _ in range(5):
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")
            time.sleep(1)
    raise RuntimeError(f"付款已结算，但 OpenAI 订阅在确认窗口内仍为 {last_status}；请稍后执行状态恢复，禁止重复付款")


def _build_checkout_protocol_context(
    chatgpt_account_model: AccountModel,
    *,
    country: str = "ID",
    currency: str = "IDR",
    proxy: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> tuple[Any, dict, dict]:
    """共享的账号适配 + 账单地址 + Transport 构造参数（供协议提链两个入口复用）。"""
    from platforms.chatgpt import payment as chatgpt_payment

    with Session(engine) as session:
        account = build_platform_account(session, chatgpt_account_model)

    extra = dict(getattr(account, "extra", {}) or {})

    class _AccountAdapter:
        pass

    a = _AccountAdapter()
    a.access_token = str(extra.get("access_token") or getattr(account, "token", "") or "")
    a.cookies = str(extra.get("cookies") or "")
    a.chatgpt_account_id = str(extra.get("account_id") or "")
    a.extra = extra
    a.email = str(getattr(account, "email", "") or "")
    if not a.access_token:
        raise RuntimeError(
            f"ChatGPT 账号 {getattr(account, 'email', '')} 缺少 access_token，"
            "无法提取 GoPay 链接"
        )

    device_id, client_version, build_number = chatgpt_payment._checkout_account_metadata(a)
    chatgpt_account_id = chatgpt_payment._extract_chatgpt_account_id(a)

    address = chatgpt_payment.fetch_billing_address("ID")
    # meiguodizhi.com 的 /id-address 数据源可能返回非印尼占位地址（实测返回过
    # 英国 Newry / CH3O 3OF 之类），Stripe 会对非 5 位数字邮编报
    # invalid_postal_code。印尼邮编必须是 5 位数字；不满足则用项目内置的印尼
    # 地址 seed 覆盖地理字段（DKI Jakarta + 10310 已由浏览器流程验证可通过
    # Stripe 校验），email/name/phone 仍保留外部地址服务的值。
    if not re.fullmatch(r"\d{5}", str(address.get("postal_code") or "").strip()):
        _seed = dict(
            chatgpt_payment._LOCAL_BILLING_ADDRESS_SEEDS.get("ID", ({},))[0] or {}
        )
        address["line1"] = str(_seed.get("line1") or "Jalan M.H. Thamrin No. 1")
        address["city"] = str(_seed.get("city") or "Jakarta")
        address["state"] = str(_seed.get("state") or "DKI Jakarta")
        address["postal_code"] = str(_seed.get("postal_code") or "10310")
        address["country"] = "ID"
    billing = {
        "name": str(address.get("name") or ""),
        "email": str(address.get("email") or a.email or "buyer@example.com"),
        "line1": str(address.get("line1") or ""),
        "line2": str(address.get("line2") or ""),
        "city": str(address.get("city") or ""),
        "state": str(address.get("state") or ""),
        "postal_code": str(address.get("postal_code") or ""),
        "phone": str(address.get("phone") or ""),
    }

    transport_kwargs = {
        "access_token": a.access_token,
        "cookies": a.cookies,
        "device_id": device_id,
        "client_version": client_version,
        "build_number": build_number,
        "chatgpt_account_id": chatgpt_account_id,
        "proxy": proxy,
        "country": country,
    }
    return a, billing, transport_kwargs



def step_extract_gopay_link_protocol(
    chatgpt_account_model: AccountModel,
    *,
    country: str = "ID",
    currency: str = "IDR",
    proxy: Optional[str] = None,
    plan_name: str = "chatgptplusplan",
    coupon_id: str = "none",
    expected_amount: str = "",
    log: Callable[[str], None] = print,
) -> dict:
    """步骤 ①② 合并（纯协议提链）：create → fetch → Stripe Elements 映射
    GoPay → taxes → confirm → custom payment method start，直接拿到 Midtrans
    支付链接，全程不开浏览器。

    返回 {"cashier_url": ..., "midtrans_url": ...}。
    """
    if not str(proxy or "").strip():
        raise RuntimeError("GoPay GPTPlus 提链必须使用任务代理池中的固定代理")

    from platforms.chatgpt.gopay_link_protocol import (
        checkout_url as _protocol_checkout_url,
        extract_gopay_payment_link as _protocol_extract,
    )
    from platforms.chatgpt.gopay_link_transport import CurlCffiTransport

    _a, billing, transport_kwargs = _build_checkout_protocol_context(
        chatgpt_account_model, country=country, currency=currency, proxy=proxy, log=log
    )

    log(
        f"纯协议提取 GoPay 链接（country={country}, currency={currency}, "
        f"proxy={_mask_proxy(proxy) or '直连'}）"
    )
    transport = CurlCffiTransport(**transport_kwargs)
    try:
        exit_info = transport.probe_checkout_exit(str(country or "ID"))
        log(
            "纯协议提链出口校验通过："
            f"country={exit_info['country']}, ip={exit_info['ip']}，"
            f"proxy={_mask_proxy(proxy) or '直连'}"
        )
        result = _protocol_extract(
            transport,
            plan_name=plan_name,
            billing=billing,
            coupon_id=coupon_id,
            expected_amount=expected_amount,
            stripe_js_id=str(uuid.uuid4()),
            trace=log,
        )
    finally:
        transport.close()

    midtrans_url = str(result.get("provider_redirect_url") or "").strip()
    if not _MIDTRANS_URL_RE.fullmatch(midtrans_url):
        # 失败时透出候选 URL 与链路信息，便于下一轮诊断真实支付链接字段。
        _candidates = result.get("candidate_urls") or []
        raise RuntimeError(
            "纯协议提链未得到有效的 Midtrans Snap URL: "
            f"{midtrans_url[:120] or 'missing'} | "
            f"payment_link_type={result.get('payment_link_type') or 'unknown'} | "
            f"session={result.get('checkout_session_id') or ''} | "
            f"candidate_urls={[str(u)[:80] for u in _candidates][:5]}"
        )

    try:
        cashier_url = _protocol_checkout_url(result)
    except Exception:
        entity = str(result.get("processor_entity") or "openai_llc")
        sid = str(result.get("checkout_session_id") or "")
        cashier_url = f"https://chatgpt.com/checkout/{entity}/{sid}" if sid else ""

    log(f"纯协议提链完成: midtrans=...{midtrans_url[-40:]}")
    return {"cashier_url": cashier_url, "midtrans_url": midtrans_url}



def step_extract_gopay_link_from_cashier(
    cashier_url: str,
    chatgpt_account_model: AccountModel,
    *,
    country: str = "ID",
    currency: str = "IDR",
    proxy: Optional[str] = None,
    plan_name: str = "chatgptplusplan",
    coupon_id: str = "none",
    expected_amount: str = "",
    log: Callable[[str], None] = print,
) -> dict:
    """步骤 ②（纯协议）：解析既有 cashier_url → fetch 权威 checkout → 路由
    oaics_/cs_ 尾部直接拿到 Midtrans 链接，全程不开浏览器。

    返回 {"cashier_url": ..., "midtrans_url": ...}。
    """
    if not str(proxy or "").strip():
        raise RuntimeError("GoPay GPTPlus 提链必须使用任务代理池中的固定代理")

    entity, session_id = _parse_short_link_id(cashier_url)

    from platforms.chatgpt.gopay_link_protocol import (
        extract_gopay_payment_link_from_checkout as _protocol_extract_from_checkout,
    )
    from platforms.chatgpt.gopay_link_transport import CurlCffiTransport

    _a, billing, transport_kwargs = _build_checkout_protocol_context(
        chatgpt_account_model, country=country, currency=currency, proxy=proxy, log=log
    )

    log(
        f"纯协议从既有 cashier 提取 GoPay 链接（country={country}, currency={currency}, "
        f"proxy={_mask_proxy(proxy) or '直连'}）"
    )
    transport = CurlCffiTransport(**transport_kwargs)
    try:
        exit_info = transport.probe_checkout_exit(str(country or "ID"))
        log(
            "纯协议提链出口校验通过："
            f"country={exit_info['country']}, ip={exit_info['ip']}，"
            f"proxy={_mask_proxy(proxy) or '直连'}"
        )
        result = _protocol_extract_from_checkout(
            transport,
            checkout_session_id=session_id,
            processor_entity=entity,
            plan_name=plan_name,
            billing=billing,
            coupon_id=coupon_id,
            expected_amount=expected_amount,
            stripe_js_id=str(uuid.uuid4()),
            trace=log,
        )
    finally:
        transport.close()

    midtrans_url = str(result.get("provider_redirect_url") or "").strip()
    if not _MIDTRANS_URL_RE.fullmatch(midtrans_url):
        _candidates = result.get("candidate_urls") or []
        raise RuntimeError(
            "纯协议从既有 cashier 提取未得到有效的 Midtrans Snap URL: "
            f"{midtrans_url[:120] or 'missing'} | "
            f"payment_link_type={result.get('payment_link_type') or 'unknown'} | "
            f"session={result.get('checkout_session_id') or ''} | "
            f"candidate_urls={[str(u)[:80] for u in _candidates][:5]}"
        )

    log(f"纯协议从既有 cashier 提取完成: midtrans=...{midtrans_url[-40:]}")
    return {"cashier_url": cashier_url, "midtrans_url": midtrans_url}


def step_grab_midtrans_url(
    cashier_url: str,
    *,
    checkout_mode: str = "camoufox_headed",
    bit_profile_id: str = "",
    bit_api_url: str = "",
    bit_api_token: str = "",
    proxy: Optional[str] = None,
    timeout_seconds: int = 300,
    capture_dir: str = "",
    after_grab: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    chatgpt_cookies: str = "",
    expected_exit_country: str = "ID",
    expected_exit_ip: str = "",
    log: Callable[[str], None] = print,
) -> str:
    """步骤 ②：浏览器打开 cashier_url，自动选 GoPay 渠道、点订阅，抓跳转后的
    Midtrans URL 后关闭浏览器返回。

    ``checkout_mode`` 解析成 camoufox/bitbrowser backend（同 CtfGptPlus 那套）：
      camoufox_headed / camoufox_headless / bitbrowser_headed /
      bitbrowser_hidden / bitbrowser_headless。
    bitbrowser_* 必须提供 ``bit_profile_id``。

    ``chatgpt_cookies``：短链模式（chatgpt.com/checkout/openai_llc/<id>）必传。
    短链是 ChatGPT 托管页、URL 里没有 token，打开时必须带登录态 cookie，否则
    会跳登录页。长链（pay.openai.com）不需要。

    ``capture_dir`` 非空时开启调试抓包：抓到 midtrans_url 不关浏览器，停在
    付款页让人工手动付款，录 HAR + dump 每页 HTML。``after_grab`` 在抓到 url
    后、进入人工付款等待前调用（用于浏览器开着时准备 GoPay 账号）。
    """
    expected_exit_country = str(expected_exit_country or "").strip().upper()
    if expected_exit_country and not str(proxy or "").strip():
        raise RuntimeError("GoPay GPTPlus 支付页必须使用任务代理池中的固定代理")

    from platforms.chatgpt import payment as chatgpt_payment
    from platforms._browser_backend import parse_checkout_mode, DEFAULT_BIT_API_URL

    backend_config = parse_checkout_mode(
        checkout_mode,
        bit_profile_id=bit_profile_id,
        bit_api_url=bit_api_url or DEFAULT_BIT_API_URL,
        bit_api_token=bit_api_token,
    )
    log(
        f"浏览器抓 midtrans（mode={checkout_mode} -> backend={backend_config.backend}/"
        f"{backend_config.window_mode}）"
    )
    return chatgpt_payment.select_gopay_and_grab_midtrans(
        cashier_url,
        backend_config=backend_config,
        proxy=proxy,
        timeout_seconds=timeout_seconds,
        capture_dir=capture_dir,
        after_grab=after_grab,
        cancel_check=cancel_check,
        chatgpt_cookies=chatgpt_cookies,
        expected_exit_country=expected_exit_country,
        expected_exit_ip=expected_exit_ip,
        log=log,
    )


def step_pay_with_gopay(
    midtrans_url: str,
    gopay_account_model: AccountModel,
    *,
    proxy_override: str = "",
    herosms_api_key_override: str = "",
    smspool_api_key_override: str = "",
    smsbower_api_key_override: str = "",
    five_sim_api_key_override: str = "",
    smsapi_url_override: str = "",
    sms_provider_override: str = "",
    cancel_check: Optional[Callable[[], bool]] = None,
    expected_currency: str = "IDR",
    max_payment_amount_rp: int = 0,
    raise_on_failure: bool = True,
    log: Callable[[str], None] = print,
) -> dict:
    """步骤 ③：用 GoPay 号协议完成 Midtrans 付款（14 步）。

    需要 ``Account.extra`` 里有 ``phone_local`` / ``pin`` / ``herosms_activation_id``，
    这些都是注册阶段写进去的（见 ``platforms/gopay/plugin.py::register``）。

    **接码渠道必须和注册时一致**：``herosms_activation_id`` 这个字段对
    SMSPool 注册的号来说存的其实是 SMSPool 的 ``order_id``，拿去 Hero-SMS
    查 OTP 永远等不到（必现 OTP timeout）。所以这里先从账号 extra 读
    注册渠道（``sms_provider``），据此选对应平台的接码 API 接付款 OTP。
    没记录渠道的老号回退到 ``sms_provider_override`` / 默认 herosms。

    接码平台 API key 来源（**不存账号 extra 里**，避免 /accounts API 把
    overview 返回给前端时泄漏全局密钥）：
      1. 显式传参（task payload 走这条）
      2. 环境变量（Hero-SMS: ``OPAI_HEROSMS_API_KEY``）
    """
    from platforms.gopay._opai_loader import ensure_opai_on_path

    ensure_opai_on_path()
    from opai.core.gopay_payment_protocol import (
        GoPayPayment,
        GoPayFraudDenyError,
        GoPayPaymentError,
    )

    extra = _account_extra(gopay_account_model)
    phone_local = str(extra.get("phone_local") or "").strip()
    pin = str(extra.get("pin") or gopay_account_model.password or "").strip()
    aid = str(extra.get("herosms_activation_id") or "").strip()
    register_proxy = _normalize_proxy_url(proxy_override or extra.get("register_proxy"))
    if not register_proxy:
        raise RuntimeError("GoPay 付款缺少该账号固定代理，禁止回退直连或临时换代理")
    provider = (
        str(extra.get("sms_provider") or "").strip().lower()
        or str(sms_provider_override or "").strip().lower()
        or "herosms"
    )
    # 换绑获号场景：账号是登录旧号后换绑到的新印尼号，付款 OTP 要从**换绑渠道
    # 的新号**接。worker 把换绑渠道独立 key 存进了 extra.rebind_sms_key，这里
    # 取出来覆盖对应渠道的 key（herosms/smsbower）。普通注册号该字段为空，
    # 走原有 *_override / 环境变量逻辑。
    rebind_sms_key = str(extra.get("rebind_sms_key") or "").strip()
    if rebind_sms_key:
        if provider == "smsbower":
            smsbower_api_key_override = smsbower_api_key_override or rebind_sms_key
        else:
            herosms_api_key_override = herosms_api_key_override or rebind_sms_key
        log(f"换绑获号号付款：用换绑渠道独立 key 接新号 OTP（provider={provider}）")

    if not (phone_local and pin and aid):
        raise RuntimeError(
            "GoPay 账号缺少 phone_local / pin / herosms_activation_id，无法付款"
        )

    log(
        f"开始 GoPay 协议付款（phone={phone_local}, aid={aid}, "
        f"接码={provider}, midtrans=...{midtrans_url[-40:]}）"
    )

    # 按注册渠道构造「等付款 OTP」回调 + 「付款成功后归还号」回调。
    wait_otp, sms_done = _build_payment_sms_callbacks(
        provider=provider,
        aid=aid,
        herosms_api_key=herosms_api_key_override,
        smspool_api_key=smspool_api_key_override,
        smsbower_api_key=smsbower_api_key_override,
        five_sim_api_key=five_sim_api_key_override,
        proxy=register_proxy,
        smsapi_url=(smsapi_url_override or str(extra.get("smsapi_url") or "").strip()),
        smsapi_phone=str(extra.get("smsapi_phone") or extra.get("phone") or phone_local or ""),
        log=log,
    )

    payment = GoPayPayment(proxy=register_proxy)
    try:
        result = payment.pay(
            midtrans_url=midtrans_url,
            phone=phone_local,
            country_code=str(extra.get("country_code") or "+62").strip().lstrip("+"),
            pin=pin,
            wait_otp=wait_otp,
            otp_total_timeout=120,
            otp_resend_after=60,
            cancel_check=cancel_check,
            expected_currency=expected_currency,
            max_amount=max_payment_amount_rp,
            require_zero_amount=max_payment_amount_rp == 0,
            # Midtrans 的 GoPay 强制绑定会创建 1 IDR 验证交易；仅在 0 元
            # 安全模式且交易元数据完整命中 tokenization 证据时放行。
            allow_one_idr_tokenization_verification=max_payment_amount_rp == 0,
        )
    except GoPayFraudDenyError as exc:
        raise RuntimeError(f"GoPay 风控拒付（号被烧）: {exc}")
    except GoPayPaymentError as exc:
        if "cancelled" not in str(exc).lower():
            raise
        charge_attempted = bool(getattr(payment, "_charge_attempted", False))
        result = {
            "success": False,
            "cancelled": True,
            "uncertain": charge_attempted,
            "charge_attempted": charge_attempted,
            "detail": "payment cancelled",
        }
    finally:
        try:
            sms_done()
        except Exception as exc:
            log(f"接码订单收尾失败（忽略）: {exc}")

    if not result.get("success") and raise_on_failure:
        detail = str(result.get("detail") or "unknown")
        raise RuntimeError(f"GoPay 付款失败: {detail}")

    log(
        f"GoPay 付款结果: status={result.get('transaction_status') or 'unknown'} "
        f"success={bool(result.get('success'))}"
    )
    return result


def _build_payment_sms_callbacks(
    *,
    provider: str,
    aid: str,
    herosms_api_key: str = "",
    smspool_api_key: str = "",
    smsbower_api_key: str = "",
    five_sim_api_key: str = "",
    proxy: str = "",
    smsapi_url: str = "",
    smsapi_phone: str = "",
    log: Callable[[str], None] = print,
) -> tuple[Callable[..., Optional[str]], Callable[[], None]]:
    """按接码渠道返回 ``(wait_otp, sms_done)`` 两个回调。

    - herosms / smsbower：SMS-Activate 风格，同 aid 内 ``setStatus=3``
      让平台准备下一条 SMS，再阻塞 ``getStatus`` 拿码；成功后 ``setStatus=6``
      归还余额。一个 aid 跨注册/PIN/付款 3 次 OTP 都能续接。
    - smspool：先 ``/sms/resend`` 重发，再轮询 ``/sms/check`` 拿码；SMSPool
      一次性号付款阶段拿不到新码（号会停在 Completed 不再接），只能尽力
      ignore 注册旧码避免把它当付款 OTP；想稳就用 herosms / smsbower。
    """
    provider = (provider or "herosms").strip().lower()

    if provider in {"smsapi", "api_sms"}:
        from platforms.gopay.sms_channel import SmsApiChannel
        import os as _os

        url = (
            str(smsapi_url or "").strip()
            or _os.environ.get("OPAI_SMSAPI_URL", "").strip()
        )
        phone = (
            str(smsapi_phone or "").strip()
            or _os.environ.get("OPAI_SMSAPI_PHONE", "").strip()
        )
        channel = SmsApiChannel(url=url, phone=phone)
        # 付款前快照基线短信时间：付款 OTP 必须比这条更新才认（避免把注册/PIN
        # 阶段的旧码当付款 OTP）。
        try:
            channel.prime()
        except Exception:
            pass

        def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
            # 重置基线 -> 等比基线更新的短信（GoPay 这次付款新发的 SMS OTP）。
            try:
                channel.request_another(phone)
            except Exception:
                pass
            return channel.wait_code(phone, timeout=timeout)

        def _sms_done() -> None:
            return None

        return _wait_otp, _sms_done

    if provider == "five_sim":
        from core.base_sms import FiveSimProvider

        key = str(five_sim_api_key or "").strip() or os.environ.get("OPAI_5SIM_API_KEY", "").strip()
        if not key:
            raise RuntimeError("缺少 5sim API key（task payload 和 OPAI_5SIM_API_KEY 都为空）")
        channel = FiveSimProvider(api_key=key, country="indonesia", product="gojek", proxy=proxy or None)

        def _current_codes() -> list[str]:
            payload = channel._request(f"/v1/user/check/{aid}")
            rows = payload.get("sms") if isinstance(payload, dict) else []
            return [
                str(row.get("code") or "").strip()
                for row in rows or []
                if isinstance(row, dict) and str(row.get("code") or "").strip()
            ]

        try:
            seen_codes = set(_current_codes())
        except Exception:
            seen_codes = set()

        def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
            deadline = time.time() + max(int(timeout or 120), 1)
            while time.time() < deadline:
                try:
                    for code in _current_codes():
                        if code not in seen_codes:
                            seen_codes.add(code)
                            return code
                except Exception:
                    pass
                time.sleep(3)
            return None

        def _sms_done() -> None:
            channel.report_success(aid)

        return _wait_otp, _sms_done

    if provider == "smspool":
        from platforms.gopay.sms_channel import SmsPoolChannel, SMSPOOL_DEFAULT_API_KEY
        import os as _os

        key = (
            str(smspool_api_key or "").strip()
            or _os.environ.get("OPAI_SMSPOOL_API_KEY", "").strip()
            or SMSPOOL_DEFAULT_API_KEY
        )
        channel = SmsPoolChannel(api_key=key)
        # 付款前快照"旧码"：SMSPool 的 order 在注册阶段收过 OTP 后会一直停在
        # status=3 并缓存最后一条码，付款复用同一 order 时 /sms/check 会立刻
        # 吐回那条旧码。先记下它，等新码时排除，避免把注册旧码当付款 OTP
        # 提交（会被 GoPay 判 GoPay-900 / validate-otp 500）。
        old_code = None
        try:
            old_code = channel.peek_code(aid)
            if old_code:
                log(f"SMSPool 旧码快照={old_code}（付款时将忽略，只认 GoPay 新发的 OTP）")
        except Exception:
            old_code = None

        def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
            # 触发 SMSPool resend（让它准备接收下一条短信），再等**新**码。
            try:
                channel.request_another(aid)
            except Exception:
                pass
            time.sleep(2)
            return channel.wait_code(aid, timeout=timeout, ignore_code=old_code)

        def _sms_done() -> None:
            # SMSPool 号用完即止，无需显式关闭。
            return None

        return _wait_otp, _sms_done

    if provider == "smsbower":
        from platforms.gopay.sms_channel import (
            make_smsbower_channel,
            SMSBOWER_DEFAULT_API_KEY,
        )
        import os as _os

        key = (
            str(smsbower_api_key or "").strip()
            or _os.environ.get("OPAI_SMSBOWER_API_KEY", "").strip()
            or SMSBOWER_DEFAULT_API_KEY
        )
        channel = make_smsbower_channel(api_key=key)

        def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
            # SMSBower（SMS-Activate 风格）：先 setStatus=3 通知平台准备下一条
            # SMS，再阻塞 getStatus 拿码。同 aid 内能续接 3 次 OTP。
            try:
                channel.request_another(aid)
            except Exception:
                pass
            time.sleep(2)
            return channel.wait_code(aid, timeout=timeout)

        def _sms_done() -> None:
            channel.done(aid)

        return _wait_otp, _sms_done

    # 默认 Hero-SMS
    from opai.core.sms_helpers import sms_wait_code, sms_request_another, sms_api
    import os as _os

    api_key = (
        str(herosms_api_key or "").strip()
        or _os.environ.get("OPAI_HEROSMS_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            "缺少 Hero-SMS API key（task payload 没传，"
            "环境变量 OPAI_HEROSMS_API_KEY 也没设）"
        )

    def _wait_otp(_phone_arg: str = "", timeout: int = 120) -> Optional[str]:
        try:
            sms_request_another(api_key, aid)
        except Exception:
            pass
        time.sleep(2)
        return sms_wait_code(api_key, aid, timeout=timeout)

    def _sms_done() -> None:
        sms_api(api_key, "setStatus", {"id": aid, "status": "6"})

    return _wait_otp, _sms_done


def execute_gopay_pay_chatgpt(
    *,
    chatgpt_account_id: int,
    gopay_account_id: Optional[int] = None,
    cashier_url_override: str = "",
    midtrans_url_override: str = "",
    country: str = "ID",
    currency: str = "IDR",
    headless: bool = False,
    checkout_mode: str = "camoufox_headed",
    bit_profile_id: str = "",
    envelope_url: str = "",
    proxy: Optional[str] = None,
    grab_timeout: int = 300,
    herosms_api_key_override: str = "",
    phone_ttl_seconds: int = 1200,
    auto_register_gopay: bool = False,
    gopay_pin: str = "147258",
    sms_provider: str = "herosms",
    smspool_api_key: str = "",
    smsbower_api_key: str = "",
    five_sim_api_key: str = "",
    smsapi_url: str = "",
    smsapi_phone: str = "",
    max_price: str = "",
    gopay_source: str = "auto",
    auto_rebind: bool = False,
    rebind_provider: str = "herosms",
    rebind_sms_key: str = "",
    rebind_country: str = "",
    rebind_service: str = "",
    capture_payment: bool = False,
    capture_dir: str = "",
    use_stripe_init: bool = False,
    use_short_link: bool = False,
    link_mode: str = "protocol",
    log: Callable[[str], None] = print,
    cancel_check: Optional[Callable[[], bool]] = None,
    payment_attempt_key: str = "",
    payment_attempt_action: str = "start",
    task_id: str = "",
    max_payment_amount_rp: int = 0,
) -> dict:
    """整条流水线（同步）。

    Args:
        chatgpt_account_id: 主项目 ``accounts`` 表中 platform=chatgpt 的行 id
        gopay_account_id: 指定的 GoPay 号 id；为空则从池里挑一条余额 ≥ 1 的
        cashier_url_override: 跳过步骤 ①，直接用这个 cashier_url
        midtrans_url_override: 跳过步骤 ① + ②，直接用这个 midtrans_url
        country/currency: 默认印尼盾，跟 ChatGPT 印尼区订阅匹配
        checkout_mode: 浏览器模式 camoufox_headed/camoufox_headless/
            bitbrowser_headed/bitbrowser_hidden/bitbrowser_headless
        bit_profile_id: bitbrowser_* 模式必填
        envelope_url: GoPay 红包链接，选号后余额不足时领取补余额
        grab_timeout: 步骤 ② 等跳到 Midtrans 的最长秒数
        link_mode: 提链方式——``protocol``（默认）走纯协议 create→Elements→
            taxes→confirm→start 直接拿 Midtrans 链接，不开浏览器；``browser``
            保留旧的 ① 协议拿 cashier_url + ② 浏览器抓 midtrans 流程
        phone_ttl_seconds: Hero-SMS 号码有效期（默认 1200=20min），整条
            流水线超时即判失败
        gopay_source: GoPay 号来源开关——
            ``pool``=只用号池里已有的号（池空直接失败，不注册）；
            ``register``=强制现注册新号（忽略号池）；
            ``auto``（默认）=先查号池，池空再按 ``auto_register_gopay`` 决定
            是否注册（保持原有行为）。``gopay_account_id`` 显式指定时此开关无效。

    Returns:
        ``{"chatgpt_account_id", "gopay_account_id", "cashier_url",
           "midtrans_url", "payment": <pay 返回>}``
    """
    country = str(country or "ID").strip().upper()
    currency = str(currency or "IDR").strip().upper()
    proxy = str(proxy or "").strip() or None
    if not midtrans_url_override:
        if country != "ID" or currency != "IDR":
            raise RuntimeError("GoPay GPTPlus 付款仅允许 country=ID、currency=IDR")
        if not proxy:
            raise RuntimeError("GoPay GPTPlus 提链和支付页必须使用任务代理池中的固定代理")

    ttl_guard = PhoneTTLGuard(ttl_seconds=phone_ttl_seconds)
    out: dict[str, Any] = {
        "chatgpt_account_id": int(chatgpt_account_id),
        "gopay_account_id": None,
        "cashier_url": cashier_url_override or "",
        "midtrans_url": midtrans_url_override or "",
        "payment": {},
    }

    chatgpt = None
    if int(chatgpt_account_id) > 0:
        chatgpt = find_chatgpt_account(int(chatgpt_account_id))
        if not chatgpt:
            raise RuntimeError(f"ChatGPT 账号 #{chatgpt_account_id} 不存在或不是 chatgpt 平台")
    elif not midtrans_url_override:
        # chatgpt_account_id=0 占位仅在已提供 midtrans_url 时合法（需求 2：
        # 直接拿 url 付款，不关联具体 ChatGPT 账号）。
        raise RuntimeError("chatgpt_account_id 为 0 时必须提供 midtrans_url_override")

    # ① 拿 cashier_url（除非已 override）
    # 抓包模式：算一个本次抓包目录（前端开关 capture_payment 打开时）。
    effective_capture_dir = ""
    if capture_payment:
        base = str(capture_dir or "").strip()
        if not base:
            base = os.path.join(os.getcwd(), "_gopay_capture")
        effective_capture_dir = os.path.join(base, time.strftime("%Y%m%d_%H%M%S"))
        log(f"[capture] 抓包模式已开启，HAR/HTML 将保存到: {effective_capture_dir}")

    # ③ 的逻辑（选/注册 GoPay 号 + 查余额）抽成闭包：
    #   - 普通模式：抓到 midtrans 后直接调，再跑协议付款；
    #   - 抓包模式：作为 after_grab 回调，在浏览器开着时跑（注册/设PIN/查余额），
    #     把账号信息打印出来给人工手动付款，最后不跑协议付款。
    source = str(gopay_source or "auto").strip().lower()

    def _do_register():
        ttl_guard.check()
        api_key_for_register = (
            herosms_api_key_override
            or os.environ.get("OPAI_HEROSMS_API_KEY", "")
        )
        return register_gopay_account(
            herosms_api_key=api_key_for_register,
            pin=gopay_pin,
            proxy=proxy or "",
            envelope_url=envelope_url,
            sms_provider=sms_provider,
            smspool_api_key=smspool_api_key,
            smsbower_api_key=smsbower_api_key,
            five_sim_api_key=five_sim_api_key,
            smsapi_url=smsapi_url,
            smsapi_phone=smsapi_phone,
            herosms_max_price_usd=max_price,
            smspool_max_price=max_price,
            auto_rebind=auto_rebind,
            rebind_provider=rebind_provider,
            rebind_sms_key=rebind_sms_key,
            rebind_country=rebind_country,
            rebind_service=rebind_service,
            log=log,
        )

    def _prepare_gopay_account(
        _midtrans_url: str = "",
        _page=None,
        *,
        _prepared_acc: AccountModel | None = None,
        _prepare_only: bool = False,
    ):
        """Atomically lease and prepare one GoPay account before checkout creation."""
        ttl_guard.check()
        owner_key = payment_attempt_key or f"adhoc:{chatgpt_account_id}:{threading.get_ident()}"
        acc = _prepared_acc
        if acc is None:
            if source == "register":
                log("GoPay 号来源=强制注册：现注册一个新号（忽略号池/指定号）")
                acc = _do_register()
                if not acc:
                    raise RuntimeError("强制注册 GoPay 号失败，详见上方日志")
            elif gopay_account_id:
                with Session(engine) as session:
                    acc = session.get(AccountModel, int(gopay_account_id))
                    if not acc or acc.platform != "gopay":
                        raise RuntimeError(
                            f"GoPay 账号 #{gopay_account_id} 不存在或不是 gopay 平台"
                        )
            else:
                acc = pick_available_gopay_account(
                    min_balance_rp=1,
                    owner_key=owner_key,
                    task_id=task_id,
                )
                if not acc and source != "pool" and auto_register_gopay:
                    acc = _do_register()
                if not acc:
                    detail = "号池里没有未占用且有效的账号" if source == "pool" else "没有可用的 GoPay 账号，且无法自动注册"
                    raise RuntimeError(detail)

            from application.gopay_payment_state import (
                acquire_gopay_lease,
                update_payment_attempt,
            )

            if not acquire_gopay_lease(
                account_id=int(acc.id), owner_key=owner_key, task_id=task_id
            ):
                raise RuntimeError(f"GoPay 账号 #{acc.id} 正被其它付款任务使用")
            if payment_attempt_key:
                update_payment_attempt(
                    payment_attempt_key,
                    task_id=task_id,
                    gopay_account_id=int(acc.id),
                    status="preparing",
                )

        out["gopay_account_id"] = int(acc.id)
        log(f"使用 GoPay 账号 #{acc.id}（{str(acc.email)[:4]}***）")

        gopay_extra = _account_extra(acc)
        remaining_lifetime = _remaining_sms_lifetime_seconds(
            acc, gopay_extra, phone_ttl_seconds
        )
        if remaining_lifetime is not None:
            if remaining_lifetime <= 180:
                raise RuntimeError(
                    f"GoPay 号 #{acc.id} 的接码激活只剩 {max(int(remaining_lifetime), 0)} 秒，禁止开始付款"
                )
            ttl_guard.reset_remaining(remaining_lifetime)
            log(f"GoPay 接码激活剩余约 {int(remaining_lifetime)} 秒")
        else:
            ttl_guard.reset_remaining(0)

        phone = str(gopay_extra.get("phone") or acc.email or "").strip()
        register_proxy = _normalize_proxy_url(
            str(proxy or gopay_extra.get("register_proxy") or "").strip()
        )
        client = _resolve_gopay_client(phone, register_proxy, log=log)
        if client is None:
            raise RuntimeError(f"GoPay 号 #{acc.id} 无法恢复登录，禁止使用缓存余额付款")

        from platforms.gopay._opai_loader import ensure_opai_on_path

        ensure_opai_on_path()
        from opai.core.gopay_protocol_worker import _check_balance

        try:
            current_balance = max(int(_check_balance(client) or 0), 0)
        except Exception as exc:
            raise RuntimeError(f"GoPay 号 #{acc.id} 实时余额查询失败: {exc}") from exc
        if current_balance < 1:
            log(f"GoPay 号 #{acc.id} 实时余额 {current_balance} IDR，开始轮询等红包/充值到账")
            current_balance = wait_for_balance(
                client=client,
                envelope_url=envelope_url,
                ttl_guard=ttl_guard,
                cancel_check=cancel_check,
                log=log,
            )

        from core.account_graph import patch_account_graph

        with Session(engine) as session:
            m = session.get(AccountModel, int(acc.id))
            if m:
                patch_account_graph(session, m, summary_updates={"balance_rp": current_balance})
                session.commit()

        # 抓包模式只在 Midtrans 页面已经打开后执行浏览器付款。
        if capture_payment and not _prepare_only:
            _ex = _account_extra(acc)
            _phone = str(_ex.get("phone") or acc.email or "")
            _pin = str(_ex.get("pin") or acc.password or "")
            _bal = int(_ex.get("balance_rp") or 0)
            _aid = str(_ex.get("herosms_activation_id") or "")
            _provider = str(_ex.get("sms_provider") or sms_provider or "smspool")
            log(
                "==================== 浏览器付款用的 GoPay 账号 ====================\n"
                f"[capture]   GoPay 手机号 : ***{_phone[-4:]}\n"
                "[capture]   GoPay PIN    : ******\n"
                f"[capture]   当前余额     : {_bal} IDR\n"
                f"[capture]   账号 #{acc.id}（已注册+设PIN+查余额完成）\n"
                "==============================================================="
            )
            # 浏览器脚本驱动付款（page 由 after_grab 传入）
            if _page is not None:
                try:
                    from platforms.gopay.browser_pay import gopay_browser_pay

                    wait_otp, _sms_done = _build_payment_sms_callbacks(
                        provider=_provider,
                        aid=_aid,
                        herosms_api_key=herosms_api_key_override,
                        smspool_api_key=smspool_api_key,
                        smsbower_api_key=smsbower_api_key,
                        five_sim_api_key=five_sim_api_key,
                        proxy=proxy or "",
                        smsapi_url=smsapi_url,
                        smsapi_phone=smsapi_phone,
                        log=log,
                    )
                    log("[capture] 开始浏览器脚本付款（输手机号→同意→OTP→PIN→Pay now）…")
                    pay_res = gopay_browser_pay(
                        _page,
                        phone=_phone,
                        pin=_pin,
                        wait_otp=wait_otp,
                        timeout_seconds=240,
                        log=log,
                    )
                    out["payment"] = pay_res
                    log(
                        f"[capture] 浏览器付款结果: success={bool(pay_res.get('success'))}"
                        if isinstance(pay_res, dict)
                        else "[capture] 浏览器付款未返回结构化结果"
                    )
                except Exception as exc:
                    log(f"[capture] 浏览器付款异常: {exc}")
                    raise
                finally:
                    try:
                        _sms_done()
                    except Exception:
                        pass
            else:
                log("[capture] 未拿到浏览器 page，跳过自动付款（可手动操作）")
        return acc

    # GoPay 必须先准备完成，再创建短时有效的 cashier/Midtrans 会话。
    gopay = None
    if payment_attempt_action != "reconcile":
        gopay = _prepare_gopay_account(_prepare_only=True)

    checkout_context: dict[str, str] = {}
    if not midtrans_url_override:
        ttl_guard.check()
        if use_short_link or cashier_url_override:
            # 短链 / 既有 cashier：协议生成短链（或直接用 override），再协议抓
            # midtrans，全程不开浏览器。
            if not cashier_url_override:
                out["cashier_url"] = step_generate_cashier_url(
                    chatgpt,
                    country=country,
                    currency=currency,
                    proxy=proxy,
                    use_stripe_init=False,
                    use_short_link=True,
                    expected_exit_country="ID",
                    checkout_context=checkout_context,
                    log=log,
                )
            cashier_url = out["cashier_url"]
            # ② 纯协议从既有 cashier 抓 midtrans（fetch 权威 checkout → 路由
            # oaics_/cs_ 尾部），不再打开浏览器。
            ttl_guard.check()
            extracted = step_extract_gopay_link_from_cashier(
                cashier_url,
                chatgpt,
                country=country,
                currency=currency,
                proxy=proxy,
                log=log,
            )
            out["midtrans_url"] = extracted.get("midtrans_url") or ""
        elif link_mode == "protocol" and not cashier_url_override:
            # 纯协议提链：create → Elements → taxes → confirm → start，一次拿到
            # cashier_url + midtrans_url，不开浏览器。
            extracted = step_extract_gopay_link_protocol(
                chatgpt,
                country=country,
                currency=currency,
                proxy=proxy,
                log=log,
            )
            out["cashier_url"] = extracted.get("cashier_url") or ""
            out["midtrans_url"] = extracted.get("midtrans_url") or ""
        else:
            # use_stripe_init（hosted 长链）或显式 browser：保持浏览器抓取。
            if not cashier_url_override:
                out["cashier_url"] = step_generate_cashier_url(
                    chatgpt,
                    country=country,
                    currency=currency,
                    proxy=proxy,
                    use_stripe_init=use_stripe_init,
                    use_short_link=use_short_link,
                    expected_exit_country="ID",
                    checkout_context=checkout_context,
                    log=log,
                )
            cashier_url = out["cashier_url"]

            # ② 浏览器抓 midtrans_url（自动选 GoPay + 填表 + 点订阅）
            ttl_guard.check()
            # 提链和支付页必须复用任务层分配给该账号的同一条固定代理；不在此处
            # 重新从全局池取代理，避免两阶段出口发生漂移。
            browser_proxy = proxy
            # 短链模式：抓 midtrans 的浏览器要带 ChatGPT 登录 cookie（短链是
            # ChatGPT 托管页，URL 无 token）。从 chatgpt 账号读 cookies 透传。
            chatgpt_cookies = ""
            if use_short_link and chatgpt is not None:
                try:
                    with Session(engine) as session:
                        _acc = build_platform_account(session, chatgpt)
                    _ex = dict(getattr(_acc, "extra", {}) or {})
                    chatgpt_cookies = str(_ex.get("cookies", "") or "")
                    if chatgpt_cookies:
                        log("短链模式：已取到 ChatGPT 登录 cookie，将注入抓 midtrans 浏览器")
                    else:
                        log("短链模式警告：ChatGPT 账号没存 cookie，短链可能打不开（会跳登录页）")
                except Exception as exc:
                    log(f"短链模式：读取 ChatGPT cookie 失败（继续）：{exc}")

            out["midtrans_url"] = step_grab_midtrans_url(
                cashier_url,
                checkout_mode=checkout_mode,
                bit_profile_id=bit_profile_id,
                proxy=browser_proxy,
                timeout_seconds=grab_timeout,
                capture_dir=effective_capture_dir,
                # 抓包模式只在页面打开后用已经准备好的账号执行浏览器付款。
                after_grab=(
                    (lambda url, page: _prepare_gopay_account(
                        url, page, _prepared_acc=gopay
                    ))
                    if capture_payment else None
                ),
                cancel_check=cancel_check,
                chatgpt_cookies=chatgpt_cookies,
                expected_exit_country="ID",
                expected_exit_ip=str(checkout_context.get("exit_ip") or ""),
                log=log,
            )
    midtrans_url = out["midtrans_url"]
    if not _MIDTRANS_URL_RE.fullmatch(str(midtrans_url or "").strip()):
        raise RuntimeError("未获得有效的 Midtrans Snap URL")

    from application.gopay_payment_state import (
        extract_snap_id,
        get_payment_attempt,
        update_payment_attempt,
    )

    if payment_attempt_key and payment_attempt_action != "reconcile":
        update_payment_attempt(
            payment_attempt_key,
            task_id=task_id,
            status="checkout_ready",
            midtrans_url=midtrans_url,
            snap_id=extract_snap_id(midtrans_url),
        )

    if payment_attempt_action == "reconcile":
        from platforms.gopay._opai_loader import ensure_opai_on_path

        ensure_opai_on_path()
        from opai.core.gopay_payment_protocol import GoPayPayment

        attempt_state = get_payment_attempt(payment_attempt_key) or {}
        out["gopay_account_id"] = int(attempt_state.get("gopay_account_id") or 0) or None
        if str(attempt_state.get("status") or "") == "settled":
            inspected = {
                "success": True,
                "uncertain": False,
                "transaction_status": str(
                    attempt_state.get("transaction_status") or "settlement"
                ),
                "amount": attempt_state.get("amount"),
                "currency": str(attempt_state.get("currency") or currency),
            }
        else:
            inspected = GoPayPayment(proxy=_normalize_proxy_url(proxy)).inspect_transaction(
                midtrans_url,
                cancel_check=cancel_check,
            )
        out["payment"] = inspected
        if inspected.get("success"):
            update_payment_attempt(
                payment_attempt_key,
                task_id=task_id,
                status="settled",
                uncertain=False,
                transaction_status=str(inspected.get("transaction_status") or "settlement"),
                amount=inspected.get("amount"),
                currency=str(inspected.get("currency") or currency),
                error="",
            )
        elif inspected.get("uncertain"):
            update_payment_attempt(
                payment_attempt_key,
                task_id=task_id,
                status="payment_pending",
                uncertain=True,
                transaction_status=str(inspected.get("transaction_status") or "unknown"),
                error=str(inspected.get("detail") or "payment pending"),
            )
            raise RuntimeError("原付款交易仍处于待确认状态，已禁止创建或扣取第二笔交易")
        else:
            update_payment_attempt(
                payment_attempt_key,
                task_id=task_id,
                status="failed_terminal",
                uncertain=False,
                transaction_status=str(inspected.get("transaction_status") or "failed"),
                error=str(inspected.get("detail") or "payment failed"),
            )
            raise RuntimeError(f"原付款交易已终止: {inspected.get('transaction_status') or 'failed'}")

    if capture_payment:
        out["captured"] = True
        out["capture_dir"] = effective_capture_dir
        payment_result = out.get("payment") if isinstance(out.get("payment"), dict) else {}
        if not payment_result.get("success"):
            if payment_attempt_key:
                update_payment_attempt(
                    payment_attempt_key,
                    task_id=task_id,
                    status="uncertain",
                    uncertain=True,
                    error=str(payment_result.get("detail") or "抓包浏览器未确认付款成功"),
                )
            raise RuntimeError("抓包完成，但浏览器付款未确认成功；任务不会标记 Plus")
    elif payment_attempt_action != "reconcile":
        ttl_guard.check()
        if gopay is None:
            raise RuntimeError("GoPay 账号准备状态丢失")
        if payment_attempt_key:
            update_payment_attempt(
                payment_attempt_key,
                task_id=task_id,
                status="charging",
                uncertain=True,
            )
        payment_result = step_pay_with_gopay(
            midtrans_url,
            gopay,
            proxy_override=proxy or "",
            herosms_api_key_override=herosms_api_key_override,
            smspool_api_key_override=smspool_api_key,
            smsbower_api_key_override=smsbower_api_key,
            five_sim_api_key_override=five_sim_api_key,
            smsapi_url_override=smsapi_url,
            sms_provider_override=sms_provider,
            cancel_check=cancel_check,
            expected_currency=currency,
            max_payment_amount_rp=max_payment_amount_rp,
            raise_on_failure=False,
            log=log,
        )
        out["payment"] = payment_result
        if not payment_result.get("success"):
            uncertain = bool(
                payment_result.get("uncertain") or payment_result.get("charge_attempted")
            )
            if payment_attempt_key:
                update_payment_attempt(
                    payment_attempt_key,
                    task_id=task_id,
                    status="payment_pending" if uncertain else "failed_precharge",
                    uncertain=uncertain,
                    transaction_status=str(payment_result.get("transaction_status") or "unknown"),
                    amount=payment_result.get("amount"),
                    currency=str(payment_result.get("currency") or currency),
                    error=str(payment_result.get("detail") or "payment failed"),
                )
            if uncertain:
                raise RuntimeError("付款结果不确定，已保存原交易用于状态恢复，禁止重新扣款")
            raise RuntimeError(f"GoPay 付款失败: {payment_result.get('detail') or 'unknown'}")

    if payment_attempt_key:
        settled_result = out.get("payment") if isinstance(out.get("payment"), dict) else {}
        update_payment_attempt(
            payment_attempt_key,
            task_id=task_id,
            status="settled",
            uncertain=False,
            transaction_status=str(settled_result.get("transaction_status") or "settlement"),
            amount=settled_result.get("amount"),
            currency=str(settled_result.get("currency") or currency),
            error="",
        )

    # #2：付款成功后自动换绑，把当前 GoPay 号占用的（印尼）号释放出来。
    if (
        auto_rebind
        and isinstance(out.get("payment"), dict)
        and out["payment"].get("success")
    ):
        try:
            g_extra = _account_extra(gopay)
            g_phone = str(g_extra.get("phone") or gopay.email or "").strip()
            g_pin = str(g_extra.get("pin") or gopay.password or "").strip()
            g_proxy = _normalize_proxy_url(str(proxy or g_extra.get("register_proxy") or ""))
            log(f"付款成功，开始自动换绑释放号 ***{g_phone[-4:]}…")
            client = _resolve_gopay_client(g_phone, g_proxy, log=log)
            if client is None:
                log("自动换绑跳过：无法 resume GoPay client")
            else:
                rb = rebind_release_phone(
                    client, pin=g_pin,
                    rebind_provider=rebind_provider,
                    rebind_sms_key=rebind_sms_key,
                    rebind_country=rebind_country,
                    rebind_service=rebind_service,
                    log=log,
                )
                out["rebind"] = rb
                log(f"自动换绑结果: success={bool(rb.get('success'))}")
        except Exception as exc:
            log(f"自动换绑异常（忽略，不影响付款结果）: {exc}")
            out["rebind"] = {"success": False, "detail": str(exc)}

    # 付款结算只代表资金侧完成；必须等 OpenAI 套餐状态可见后才能标 subscribed。
    if int(chatgpt_account_id) > 0:
        confirmed_plan = _verify_chatgpt_subscription(
            chatgpt,
            proxy=proxy,
            cancel_check=cancel_check,
            log=log,
        )
        if payment_attempt_key:
            update_payment_attempt(
                payment_attempt_key,
                task_id=task_id,
                status="subscribed",
                uncertain=False,
                error="",
            )
        from core.account_graph import patch_account_graph

        with Session(engine) as session:
            m = session.get(AccountModel, int(chatgpt_account_id))
            if m:
                patch_account_graph(
                    session,
                    m,
                    lifecycle_status="subscribed",
                    cashier_url=out["cashier_url"] or None,
                    summary_updates={
                        "midtrans_url": out["midtrans_url"],
                        "paid_via": "gopay",
                        "paid_via_gopay_account_id": out["gopay_account_id"],
                        "plan_state": "subscribed",
                        "plan_name": confirmed_plan.title(),
                    },
                )
                session.commit()
        log("ChatGPT 账号已标记 subscribed")

    return out
