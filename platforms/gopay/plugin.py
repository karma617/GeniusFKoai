"""GoPay 平台插件。

目标：在主项目 UI（账号菜单 > GoPay）里跑"协议注册 + 设 PIN" 流程，
复用 ``platforms/gopay-deploy`` 里的现成 SDK，不再起浏览器/模拟器。

跟其它平台不同点：
- GoPay 用**手机号**注册（Hero-SMS 接码），不走邮箱 identity。这里直接
  覆写 ``register()`` 跳过 ``BasePlatform.register`` 的 identity 解析逻辑。
- 注册参数（PIN、Hero-SMS API key、代理）从 ``RegisterConfig.extra`` /
  环境变量取，UI 弹窗里走 ``extra.gopay_pin`` / ``OPAI_HEROSMS_API_KEY``。
- 注册成功后把 phone 当作 ``email`` / ``user_id``（账号唯一标识），PIN
  和接码 activation_id 等元数据存进 ``Account.extra``。
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from core.base_platform import (
    Account,
    AccountStatus,
    BasePlatform,
    RegisterConfig,
)
from core.registry import register

from platforms.gopay._opai_loader import ensure_opai_on_path


def _resolve_api_key(extra: dict) -> str:
    """优先级：注册任务 extra > 环境变量 ``OPAI_HEROSMS_API_KEY``。"""
    raw = str((extra or {}).get("herosms_api_key") or "").strip()
    if raw:
        return raw
    return str(os.environ.get("OPAI_HEROSMS_API_KEY", "") or "").strip()


def _resolve_pin(extra: dict) -> str:
    """优先级：注册任务 extra > 环境变量 > 默认 ``147258``。"""
    raw = str((extra or {}).get("gopay_pin") or "").strip()
    if raw:
        return raw
    env = str(os.environ.get("OPAI_GOPAY_DEFAULT_PIN", "") or "").strip()
    return env or "147258"


def _resolve_proxy(extra: dict, config_proxy: Optional[str]) -> str:
    """优先级：RegisterConfig.proxy > extra > 环境变量 > 空（直连）。"""
    if config_proxy:
        return str(config_proxy).strip()
    raw = str((extra or {}).get("gopay_proxy") or "").strip()
    if raw:
        return raw
    return str(os.environ.get("OPAI_GOPAY_REGISTER_PROXY", "") or "").strip()


def _resolve_proxy_country_code(proxy: str) -> str:
    """从代理用户名/参数中的地区标记提取两位国家代码。"""
    value = str(proxy or "").strip()
    for pattern in (
        r"(?:^|[^A-Za-z])area[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
        r"(?:^|[^A-Za-z])region[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
        r"(?:^|[^A-Za-z])country[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
        r"(?:^|[^A-Za-z])loc[-_=]?([A-Za-z]{2})(?:[^A-Za-z]|$)",
    ):
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return ""


def _resolve_max_price_usd(extra: dict) -> float:
    """Hero-SMS 拿号成本上限（美元）。优先级：extra > env > 默认 0.11。

    Hero-SMS API 文档：``maxPrice`` 单位为 USD，小数格式（如 ``"0.11"``）。
    上游 ``sms_helpers.sms_get_number`` 没传该参数，高峰期号价上涨时会无脑
    走默认价；这里给一个上限（默认 0.11 USD），按 GoPay（service=ni）的
    号价足够拿到号，又能挡住异常高价。
    """
    raw = str((extra or {}).get("herosms_max_price_usd") or "").strip()
    if not raw:
        raw = str(os.environ.get("OPAI_HEROSMS_MAX_PRICE_USD", "") or "").strip()
    if not raw:
        return 0.11
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.11
    return max(0.0, v)


def _format_max_price(usd: float) -> str:
    """Hero-SMS maxPrice 字符串格式，最多 4 位小数，去尾零。"""
    s = f"{usd:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _mask_proxy(value: str) -> str:
    proxy = str(value or "").strip()
    if not proxy:
        return "直连"
    if "@" in proxy:
        prefix, host = proxy.rsplit("@", 1)
        scheme = prefix.split("://", 1)[0] if "://" in prefix else ""
        return f"{scheme}://***@{host}" if scheme else f"***@{host}"
    return proxy


def _sms_lifetime_metadata(provider: str, extra: dict) -> dict:
    acquired = datetime.now(timezone.utc)
    provider = str(provider or "").strip().lower()
    metadata = {"sms_acquired_at": acquired.isoformat(), "sms_expires_at": ""}
    if provider not in {"smsapi", "api_sms"}:
        ttl = max(int((extra or {}).get("sms_ttl_seconds") or 1200), 60)
        metadata["sms_expires_at"] = (acquired + timedelta(seconds=ttl)).isoformat()
    return metadata


def _patch_sms_get_number_with_max_price(max_price_usd: float) -> None:
    """覆盖 ``opai.core.gopay_protocol_worker`` 命名空间里的 ``sms_get_number``，
    给 Hero-SMS ``getNumber`` 调用注入 ``maxPrice`` 参数。

    为什么不直接改 ``opai.core.sms_helpers``：``_register_one`` 用的是
    ``from .sms_helpers import sms_get_number`` 形式 import，名字已经绑到
    worker 模块本地 namespace，所以 patch 必须打在 worker 模块上。
    幂等：第二次调用时只更新闭包里的 ``max_price_usd``，不会重复封装。
    """
    from opai.core import sms_helpers
    from platforms.gopay.sms_channel import bind_worker_sms_callbacks
    import logging

    _log = logging.getLogger("opai.core.sms_helpers")

    def patched_sms_get_number(api_key: str):
        params = {"service": "ni", "country": "6"}
        if max_price_usd > 0:
            params["maxPrice"] = _format_max_price(max_price_usd)
        resp = sms_helpers.sms_api(api_key, "getNumber", params)
        _log.info("Hero-SMS getNumber completed (maxPrice=%s USD)", params.get("maxPrice", "-"))
        if resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            return f"+{parts[2]}", parts[1]
        _log.warning("getNumber failed: %s", resp)
        return None, None

    bind_worker_sms_callbacks(
        get_number=patched_sms_get_number,
        wait_code=sms_helpers.sms_wait_code,
        request_another=sms_helpers.sms_request_another,
        cancel=sms_helpers.sms_cancel,
        done=sms_helpers.sms_done,
    )


class _WorkerLogBridge(logging.Handler):
    """把 ``gopay-deploy`` worker 的 Python logging 实时转发到 UI 任务日志。

    worker（``opai.core.gopay_protocol_worker`` / ``sms_helpers`` 等）用标准
    ``logging`` 记录注册每一步的真实结果（``Already registered`` / ``WAF 403`` /
    ``Signup OTP failed`` 等）。这些信息原本只进 Python log，UI 任务日志看不到，
    导致注册失败时只能看到一条笼统的"没号/WAF/OTP超时"。

    这个 handler 在注册期间挂到 ``opai`` logger 上，把 INFO 及以上的记录转给
    ``log_fn``，同时把最近一条 WARNING/ERROR 存到 ``last_error`` 供失败时引用。
    另外用 ``already_registered`` 标志捕获 INFO 级别的"号已注册"信号，
    供调用方决定是否换号重试。
    """

    def __init__(self, log_fn: Callable[[str], None]):
        super().__init__(level=logging.INFO)
        self._log_fn = log_fn or print
        self.last_error: str = ""
        self.already_registered: bool = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            from opai.core.log_redaction import redact_sensitive_log

            msg = redact_sensitive_log(msg)
        except Exception:
            return
        if record.levelno >= logging.WARNING:
            self.last_error = msg
        # worker 用 INFO 记录"已注册跳过"：log.info("[%s] Already registered, skipping", phone)
        if "Already registered" in msg:
            self.already_registered = True
        try:
            self._log_fn(f"[worker] {msg}")
        except Exception:
            pass


@register
class GoPayPlatform(BasePlatform):
    """印尼 GoPay 协议注册平台。"""

    name = "gopay"
    display_name = "GoPay"
    version = "0.2.0"

    # GoPay 流程**不走**项目的浏览器执行器，只支持纯协议。``protocol``
    # 一种就够；这样 UI 上只显示协议执行器，避免用户误选。
    supported_executors = ["protocol"]
    # 不走邮箱 / OAuth 身份（Hero-SMS 拿手机号即 identity），这里给一个
    # 占位 mode，避免 UI 注册流程因为 supported_identity_modes 为空而隐藏。
    supported_identity_modes = ["phone"]
    supported_oauth_providers: list = []
    capabilities = ["query_state"]

    def __init__(self, config: RegisterConfig = None, **_ignore_kwargs):
        # mailbox 等其它平台用的关键字参数 GoPay 用不到，统一吞掉避免框架
        # 在 ``_build_platform_instance`` 里多塞参数时把构造函数搞炸。
        super().__init__(config)

    # ------------------------------------------------------------------
    # SMS 接码渠道配置（register / 换绑获号 共用）
    # ------------------------------------------------------------------
    def _setup_sms_channel(self, extra: dict, sms_provider: str, proxy: str,
                           pin: str, *, action: str = "注册") -> str:
        """按 ``sms_provider`` patch worker 的 sms 函数，返回 api_key。

        ``register()``（注册新号）和 ``acquire_via_rebind()``（成熟号换绑到新号）
        都要从接码渠道拿号 / 接 OTP，所以共用这套渠道配置。``action`` 只用于
        日志区分（"注册" / "换绑获号"）。
        """
        if sms_provider == "smspool":
            from platforms.gopay.sms_channel import (
                patch_worker_with_smspool,
                SMSPOOL_DEFAULT_API_KEY,
            )

            smspool_key = (
                str(extra.get("smspool_api_key") or "").strip()
                or os.environ.get("OPAI_SMSPOOL_API_KEY", "").strip()
                or SMSPOOL_DEFAULT_API_KEY
            )
            patch_worker_with_smspool(
                api_key=smspool_key,
                country=str(extra.get("smspool_country") or ""),
                service=str(extra.get("smspool_service") or ""),
                pool=str(extra.get("smspool_pool") or ""),
                max_price=str(extra.get("smspool_max_price") or ""),
                pricing_option=str(extra.get("smspool_pricing_option") or ""),
            )
            api_key = smspool_key
            self.log(
                f"GoPay 协议{action}启动（接码=SMSPool, PIN=******, proxy={_mask_proxy(proxy)}）"
            )
        elif sms_provider == "smsbower":
            from platforms.gopay.sms_channel import (
                patch_worker_with_smsbower,
                SMSBOWER_DEFAULT_API_KEY,
            )

            smsbower_key = (
                str(extra.get("smsbower_api_key") or "").strip()
                or os.environ.get("OPAI_SMSBOWER_API_KEY", "").strip()
                or SMSBOWER_DEFAULT_API_KEY
            )
            patch_worker_with_smsbower(
                api_key=smsbower_key,
                country=str(extra.get("smsbower_country") or ""),
                service=str(extra.get("smsbower_service") or ""),
            )
            api_key = smsbower_key
            self.log(
                f"GoPay 协议{action}启动（接码=SMSBower, PIN=******, proxy={_mask_proxy(proxy)}）"
            )
        elif sms_provider == "five_sim":
            from platforms.gopay.sms_channel import patch_worker_with_fivesim

            five_sim_key = (
                str(extra.get("five_sim_api_key") or "").strip()
                or os.environ.get("OPAI_5SIM_API_KEY", "").strip()
            )
            if not five_sim_key:
                raise RuntimeError(
                    "GoPay 注册需要 5sim API key —— 请在注册任务 extra 里填 five_sim_api_key，"
                    "或设置环境变量 OPAI_5SIM_API_KEY"
                )
            five_sim_country = str(extra.get("five_sim_country") or "indonesia").strip()
            five_sim_product = str(extra.get("five_sim_product") or "gojek").strip()
            five_sim_operator = str(extra.get("five_sim_operator") or "any").strip()
            five_sim_max_price = str(extra.get("five_sim_max_price") or "").strip()
            five_sim_base_url = str(extra.get("five_sim_base_url") or "").strip()
            five_sim_reuse = bool(extra.get("five_sim_reuse", True))
            patch_worker_with_fivesim(
                api_key=five_sim_key,
                country=five_sim_country,
                product=five_sim_product,
                operator=five_sim_operator,
                max_price=five_sim_max_price,
                base_url=five_sim_base_url,
                proxy=proxy,
                reuse=five_sim_reuse,
            )
            api_key = five_sim_key
            self.log(
                f"GoPay 协议{action}启动（接码=5sim {five_sim_country}/{five_sim_product}, "
                f"PIN=******, proxy={_mask_proxy(proxy)}）"
            )
        elif sms_provider in {"smsapi", "api_sms"}:
            from platforms.gopay.sms_channel import (
                patch_worker_with_smsapi,
                SMSAPI_DEFAULT_URL,
                SMSAPI_DEFAULT_PHONE,
            )

            smsapi_url = (
                str(extra.get("smsapi_url") or "").strip()
                or os.environ.get("OPAI_SMSAPI_URL", "").strip()
                or SMSAPI_DEFAULT_URL
            )
            smsapi_phone = (
                str(extra.get("smsapi_phone") or "").strip()
                or os.environ.get("OPAI_SMSAPI_PHONE", "").strip()
                or SMSAPI_DEFAULT_PHONE
            )
            if not smsapi_url or not smsapi_phone:
                raise RuntimeError(
                    f"GoPay {action}接码={sms_provider} 需要 smsapi_phone（固定手机号）和 "
                    "smsapi_url（查最新短信的 API URL）—— 在注册任务 extra 里填，"
                    "或设环境变量 OPAI_SMSAPI_PHONE / OPAI_SMSAPI_URL"
                )
            patch_worker_with_smsapi(url=smsapi_url, phone=smsapi_phone)
            api_key = sms_provider
            provider_label = "API接码" if sms_provider == "api_sms" else "SmsApi 固定号"
            self.log(
                f"GoPay 协议{action}启动（接码={provider_label} ***{smsapi_phone[-4:]}, "
                f"PIN=******, proxy={_mask_proxy(proxy)}）"
            )
        else:
            api_key = _resolve_api_key(extra)
            if not api_key:
                raise RuntimeError(
                    "GoPay 注册需要 Hero-SMS API key —— 请在注册任务 extra 里"
                    "填 herosms_api_key，或设置环境变量 OPAI_HEROSMS_API_KEY"
                )
            # 给 Hero-SMS getNumber 加 maxPrice 上限。必须在拿号前 patch。
            max_price_usd = _resolve_max_price_usd(extra)
            _patch_sms_get_number_with_max_price(max_price_usd)
            self.log(
                f"GoPay 协议{action}启动（接码=Hero-SMS, PIN=******, "
                f"proxy={_mask_proxy(proxy)}, maxPrice={_format_max_price(max_price_usd)} USD）"
            )
        return api_key

    # ------------------------------------------------------------------
    # 换绑获号：成熟老账号改绑到新号（绕开新号秒付被风控拒）
    # ------------------------------------------------------------------
    def acquire_via_rebind(self) -> Account:
        """用一个成熟老账号改绑到新拿的未注册号，返回绑定了老账号身份的新号。

        用户验证过的正确方向：服务端风控判账号不判手机号，新注册号秒付会被
        FDS 拒。所以拿成熟老账号（json 池里 refresh_token 活的）改绑到干净
        新号，用新号 + 老账号身份进支付流程。

        和 ``register()`` 的区别：register 是从零注册一个全新账号；这里是
        复用既有成熟账号、只把它的手机号换成新号。
        """
        ensure_opai_on_path()
        from opai.core.gopay_protocol_worker import _acquire_via_mature_rebind

        extra = dict(self.config.extra or {})
        sms_provider = str(extra.get("sms_provider") or "herosms").strip().lower()
        pin = _resolve_pin(extra)
        if not (pin.isdigit() and len(pin) == 6):
            raise RuntimeError(f"GoPay PIN 必须是 6 位数字（当前: {pin!r}）")
        proxy = _resolve_proxy(extra, self.config.proxy)

        api_key = self._setup_sms_channel(extra, sms_provider, proxy, pin, action="换绑获号")
        self.raise_if_cancelled()

        opai_logger = logging.getLogger("opai")
        prev_level = opai_logger.level
        bridge = _WorkerLogBridge(self.log)
        opai_logger.addHandler(bridge)
        if prev_level > logging.INFO or prev_level == logging.NOTSET:
            opai_logger.setLevel(logging.INFO)
        try:
            result = _acquire_via_mature_rebind(api_key, pin, proxy)
        finally:
            opai_logger.removeHandler(bridge)
            opai_logger.setLevel(prev_level)

        if not result:
            reason = (bridge.last_error.strip() if bridge else "")
            raise RuntimeError(
                f"换绑获号失败：{reason}" if reason
                else "换绑获号失败：没有可用成熟号 / 新号拿号失败 / 换绑被拒，详见日志"
            )

        phone = str(result.get("phone") or "").strip()
        local = str(result.get("local") or "").strip()
        aid = str(result.get("aid") or "").strip()
        acct_pin = str(result.get("pin") or pin).strip()
        if not phone:
            raise RuntimeError("换绑获号返回了空手机号，状态异常")

        balance_info = self._safe_initial_balance(result.get("client"))
        balance_rp = int(balance_info.get("balance_rp") or 0)
        registration_ip_country_code = _resolve_proxy_country_code(proxy)
        sms_lifetime = _sms_lifetime_metadata(sms_provider, extra)
        self.log(
            f"换绑获号成功: ***{phone[-4:]}（activation=***{aid[-4:]} 保留给付款 OTP, balance={balance_rp} IDR, "
            f"balance_status={balance_info.get('balance_query_status')}）"
        )
        return Account(
            platform=self.name,
            email=phone,
            password=acct_pin,
            user_id=phone,
            region="ID",
            token=local,
            status=AccountStatus.REGISTERED,
            extra={
                "phone": phone,
                "phone_local": local,
                "country_code": "+62",
                "pin": acct_pin,
                "pin_set": True,
                "herosms_activation_id": aid,
                "sms_provider": sms_provider,
                "register_proxy": proxy,
                "balance_rp": balance_rp,
                "balance_query_status": balance_info.get("balance_query_status"),
                "balance_check_error": balance_info.get("balance_check_error"),
                "registration_ip_country_code": registration_ip_country_code,
                "acquired_via": "mature_rebind",
                **sms_lifetime,
                "account_overview": {
                    "balance_rp": balance_rp,
                    "balance_query_status": balance_info.get("balance_query_status"),
                    "balance_check_error": balance_info.get("balance_check_error"),
                    "registration_ip_country_code": registration_ip_country_code,
                    "phone": phone,
                    "phone_local": local,
                    "pin_set": True,
                    "herosms_activation_id": aid,
                    **sms_lifetime,
                    "sms_provider": sms_provider,
                    "acquired_via": "mature_rebind",
                },
            },
        )

    # ------------------------------------------------------------------
    # 关键：覆写 register() 跳过 identity 解析
    # ------------------------------------------------------------------
    def register(self, email: str = None, password: str = None) -> Account:
        ensure_opai_on_path()
        from opai.core.gopay_protocol_worker import _register_one

        extra = dict(self.config.extra or {})
        # 接码渠道：herosms（默认）/ smspool / smsbower
        sms_provider = str(extra.get("sms_provider") or "herosms").strip().lower()
        pin = _resolve_pin(extra)
        if not (pin.isdigit() and len(pin) == 6):
            raise RuntimeError(f"GoPay PIN 必须是 6 位数字（当前: {pin!r}）")

        proxy = _resolve_proxy(extra, self.config.proxy)
        # ``envelope_did`` 来自 ``opai-team`` 自家的 Payment Inbox 服务，
        # 我们这边只做注册，不依赖那套服务，传空串即可（注册流程不强需要它）。
        envelope_did = ""

        api_key = self._setup_sms_channel(extra, sms_provider, proxy, pin, action="注册")

        self.raise_if_cancelled()

        # auto_rebind：拿到的号若已被注册，登录该账号 -> 换绑到换绑渠道临时号
        # -> 释放本号 -> 重新注册。换绑渠道**独立于注册渠道**（注册用 smsapi
        # 固定号时换绑仍要买一次性外国号）。
        auto_rebind = bool(extra.get("auto_rebind"))
        rebind_acquire = None
        if auto_rebind:
            rebind_provider = str(extra.get("rebind_provider") or "herosms").strip().lower()
            rebind_sms_key = str(extra.get("rebind_sms_key") or "").strip()
            rebind_country = str(extra.get("rebind_country") or "").strip()
            rebind_service = str(extra.get("rebind_service") or "").strip()

            def rebind_acquire():
                from application.gopay_pay_chatgpt import _build_rebind_otp_callback
                return _build_rebind_otp_callback(
                    rebind_provider=rebind_provider,
                    rebind_sms_key=rebind_sms_key,
                    country=rebind_country,
                    service=rebind_service,
                    log=self.log,
                )

        # 注册期间把 worker 的 Python logging 转发到 UI 任务日志，这样
        # 注册失败时能看到真实原因（号已注册 / WAF 403 / OTP 超时 等），
        # 而不是只有一条笼统的兜底错误。
        opai_logger = logging.getLogger("opai")
        prev_level = opai_logger.level

        # 拿到的号在 GoPay 已被注册时，worker 会跳过并返回 None；这种情况
        # 自动换新号重试（SMSPool/Hero-SMS 出号库经常有前人用过的号）。
        # 其它失败原因（WAF / OTP 超时 / 风控）不重试避免烧钱。
        max_retries_on_existing = 1 if sms_provider in {"smsapi", "api_sms"} else 5
        result = None
        last_bridge: _WorkerLogBridge | None = None
        for attempt in range(1, max_retries_on_existing + 1):
            self.raise_if_cancelled()
            bridge = _WorkerLogBridge(self.log)
            last_bridge = bridge
            opai_logger.addHandler(bridge)
            if prev_level > logging.INFO or prev_level == logging.NOTSET:
                opai_logger.setLevel(logging.INFO)
            try:
                result = _register_one(
                    api_key, pin, proxy, envelope_did,
                    auto_rebind=auto_rebind, rebind_acquire=rebind_acquire,
                )
            finally:
                opai_logger.removeHandler(bridge)
                opai_logger.setLevel(prev_level)
            if result:
                break
            if bridge.already_registered and attempt < max_retries_on_existing:
                self.log(
                    f"该号已被 GoPay 注册过，自动换新号重试 "
                    f"({attempt}/{max_retries_on_existing - 1})"
                )
                continue
            # 其它失败原因（或重试次数耗尽）不再重试
            break

        if not result:
            reason = (last_bridge.last_error.strip() if last_bridge else "")
            if last_bridge and last_bridge.already_registered and not reason:
                reason = (
                    f"连续 {max_retries_on_existing} 次拿到的号都已被 GoPay 注册过，放弃"
                )
            if reason:
                raise RuntimeError(f"GoPay 注册失败：{reason}")
            raise RuntimeError(
                "GoPay 注册失败：可能是接码没号 / 代理被 WAF 403 / "
                "OTP 超时，详见 worker 日志"
            )

        phone = str(result.get("phone") or "").strip()
        local = str(result.get("local") or "").strip()
        aid = str(result.get("aid") or "").strip()
        country_code = str(result.get("country_code") or "+62").strip()
        if not phone:
            raise RuntimeError("GoPay 注册返回了空手机号，状态异常")

        # 号已注册→登录换绑到新印尼号的场景：付款 OTP 要从**换绑渠道的新号**接，
        # 不是注册渠道。worker 在 result 里透传了 rebind_provider/rebind_sms_key，
        # 这里据此把账号的 sms_provider 覆写成换绑渠道。
        eff_sms_provider = sms_provider
        rebind_provider_used = str(result.get("rebind_provider") or "").strip().lower()
        rebind_sms_key_used = str(result.get("rebind_sms_key") or "").strip()
        if rebind_provider_used:
            eff_sms_provider = rebind_provider_used
            self.log(
                f"该号已注册→已登录换绑到新印尼号 ***{phone[-4:]}；付款将用换绑渠道"
                f"（{eff_sms_provider}）接新号 OTP（activation=***{aid[-4:]}）"
            )
        sms_lifetime = _sms_lifetime_metadata(eff_sms_provider, extra)
        smsapi_phone = (
            str(extra.get("smsapi_phone") or "").strip()
            if eff_sms_provider in {"smsapi", "api_sms"} else ""
        )
        smsapi_url = (
            str(extra.get("smsapi_url") or "").strip()
            if eff_sms_provider in {"smsapi", "api_sms"} else ""
        )

        # **重要**：不要调 sms_done(aid)！
        # GoPay 整个生命周期需要 3 次 OTP（注册 / PIN / 付款），全部
        # 复用同一个 Hero-SMS aid（通过 setStatus=3 让平台等下一条 SMS）。
        # 注册 + PIN 已经在 ``_register_one`` 里复用了同 aid；付款阶段
        # 后续由 ``gopay-deploy`` 的 worker_loop 或外部 payment 流程
        # 继续用同 aid 接收第 3 条 OTP。如果在这里调 sms_done(status=6)
        # 会把 aid 关闭，付款 OTP 拿不到。
        # （Hero-SMS 默认 20 分钟号租期，付款必须在窗口内完成。）

        # 注册成功立即查一次余额（赠送余额可能尚未到账）。余额值与查询状态
        # 分开保存，避免把网络/token/响应异常误显示成真实 Rp 0；后续可通过
        # 列表的“刷新额度”任务重新查询。
        balance_info = self._safe_initial_balance(result.get("client"))
        balance_rp = int(balance_info.get("balance_rp") or 0)
        registration_ip_country_code = _resolve_proxy_country_code(proxy)

        self.log(
            f"GoPay 注册成功: ***{phone[-4:]}（activation=***{aid[-4:]} 保留给付款 OTP, balance={balance_rp} IDR, "
            f"balance_status={balance_info.get('balance_query_status')}）"
        )
        return Account(
            platform=self.name,
            email=phone,        # GoPay 用手机号当账号唯一标识
            password=pin,        # 把 PIN 当密码字段存（也存进 extra.pin）
            user_id=phone,
            region="ID",
            token=local,
            status=AccountStatus.REGISTERED,
            extra={
                "phone": phone,
                "phone_local": local,
                "country_code": country_code,
                "pin": pin,
                "pin_set": True,
                "smsapi_phone": smsapi_phone,
                "smsapi_url": smsapi_url,
                "herosms_activation_id": aid,
                # 注册用的接码渠道。付款阶段（步骤 ③）必须用**同一个渠道**
                # 接 OTP——aid 对 SMSPool 来说是 order_id，拿去 Hero-SMS 查
                # 永远等不到 OTP。所以这里记下渠道，付款时据此选 API。
                "sms_provider": eff_sms_provider,
                "register_proxy": proxy,
                "balance_rp": balance_rp,
                "balance_query_status": balance_info.get("balance_query_status"),
                "balance_check_error": balance_info.get("balance_check_error"),
                "registration_ip_country_code": registration_ip_country_code,
                # 换绑获号场景：付款接码用的 key（换绑渠道独立 key，可空回退 env）。
                # 不放 overview（避免前端泄漏全局 key）。
                "rebind_sms_key": rebind_sms_key_used,
                **sms_lifetime,
                # ``save_account`` -> ``sync_platform_account_graph`` 只把
                # ``account_overview`` 这层映射进 AccountOverviewModel.summary，
                # 顶层字段不会同步。所以再把和"号本身状态"相关的字段
                # （余额、手机号、PIN 是否已设、aid）也镜像放一份，让下游
                # ``pick_available_gopay_account`` 通过 ``build_platform_extra``
                # 能读到。**敏感凭证**（herosms_api_key / register_proxy）
                # **不放 overview**——前端 /accounts API 会把 overview 整段
                # 返回，不能泄漏接码平台的全局 API key。付款步骤 ③ 那边
                # 改成从 task payload 或环境变量读。
                "account_overview": {
                    "balance_rp": balance_rp,
                    "balance_query_status": balance_info.get("balance_query_status"),
                    "balance_check_error": balance_info.get("balance_check_error"),
                    "registration_ip_country_code": registration_ip_country_code,
                    "phone": phone,
                    "phone_local": local,
                    "country_code": country_code,
                    "pin_set": True,
                    "smsapi_phone": smsapi_phone,
                    "herosms_activation_id": aid,
                    "sms_provider": eff_sms_provider,
                    **sms_lifetime,
                },
            },
        )

    @staticmethod
    def _query_balance_info(client, attempts: int = 3) -> dict:
        """查询钱包余额，并区分真实 0、瞬时网络错误与业务失败。"""
        if client is None:
            return {
                "balance_rp": 0,
                "balance_query_status": "error",
                "balance_check_error": "注册结果未返回 GoPay client",
                "balance_query_attempts": 0,
            }
        total = max(int(attempts or 0), 1)
        for attempt in range(1, total + 1):
            try:
                response = client.get_balance()
                if not isinstance(response, dict):
                    raise RuntimeError("余额接口返回格式无效")
                status = int(response.get("status") or 0)
                body = response.get("body") if isinstance(response.get("body"), dict) else {}
                if status != 200:
                    detail = str(body.get("message") or body.get("error") or body)[:240]
                    raise RuntimeError(f"余额接口 HTTP {status}: {detail}")
                rows = body.get("data")
                if not isinstance(rows, list) or not rows:
                    raise RuntimeError("余额接口未返回 data 列表")
                balance_data = rows[0].get("balance") if isinstance(rows[0], dict) else {}
                raw_value = balance_data.get("value") if isinstance(balance_data, dict) else None
                if raw_value is None:
                    raise RuntimeError("余额接口缺少 balance.value")
                return {
                    "balance_rp": max(int(raw_value), 0),
                    "balance_query_status": "ok",
                    "balance_check_error": "",
                    "balance_query_attempts": attempt,
                }
            except Exception as exc:
                error = str(exc)
                normalized = f"{type(exc).__name__}: {error}".lower()
                transient = any(
                    marker in normalized
                    for marker in (
                        "server disconnected",
                        "remote disconnected",
                        "connection reset",
                        "connection aborted",
                        "unexpected_eof",
                        "ssl",
                        "timed out",
                        "timeout",
                        "temporarily unavailable",
                        "http 429",
                        "http 502",
                        "http 503",
                        "http 504",
                    )
                )
                if transient and attempt < total:
                    time.sleep(attempt)
                    continue
                return {
                    "balance_rp": 0,
                    "balance_query_status": "error",
                    "balance_check_error": error,
                    "balance_query_attempts": attempt,
                }
        raise RuntimeError("余额查询重试状态异常")  # pragma: no cover

    def _safe_initial_balance(self, client) -> dict:
        info = self._query_balance_info(client)
        if info.get("balance_query_status") == "error":
            self.log(f"GoPay 注册后余额查询失败: {info.get('balance_check_error') or 'unknown'}")
        return info

    # ------------------------------------------------------------------
    # 状态查询：拉余额（懒加载 client，避免每次 import 都 ensure 路径）
    # ------------------------------------------------------------------
    def check_valid(self, account: Account) -> bool:
        try:
            ensure_opai_on_path()
            from opai.core.gopay_protocol_worker import _resume_account
        except Exception as exc:
            self._last_check_overview = {
                "balance_query_status": "error",
                "balance_check_error": f"加载 GoPay 查询模块失败: {exc}",
            }
            return False
        try:
            phone = str(account.user_id or account.email or "").strip()
            if not phone:
                self._last_check_overview = {
                    "balance_query_status": "error",
                    "balance_check_error": "账号缺少手机号",
                }
                return False
            account_extra = account.extra or {}
            account_overview = account_extra.get("account_overview") if isinstance(account_extra.get("account_overview"), dict) else {}
            legacy_extra = account_overview.get("legacy_extra") if isinstance(account_overview.get("legacy_extra"), dict) else {}
            register_proxy = str(
                account_extra.get("register_proxy")
                or account_overview.get("register_proxy")
                or legacy_extra.get("register_proxy")
                or ""
            )
            registration_ip_country_code = str(
                account_overview.get("registration_ip_country_code")
                or legacy_extra.get("registration_ip_country_code")
                or _resolve_proxy_country_code(register_proxy)
                or ""
            ).strip().upper()
            resumed = _resume_account(phone, proxy=register_proxy)
            if not resumed:
                self._last_check_overview = {
                    "balance_query_status": "error",
                    "balance_check_error": "无法恢复 GoPay 登录会话",
                }
                return False
            balance_info = self._query_balance_info(resumed.get("client"))
            if balance_info.get("balance_query_status") != "ok":
                self._last_check_overview = balance_info
                self.log(f"GoPay 余额查询失败: {balance_info.get('balance_check_error') or 'unknown'}")
                return False
            balance = int(balance_info.get("balance_rp") or 0)
            self._last_check_overview = {
                "plan": "free" if balance < 1 else "active",
                "plan_name": "GoPay",
                "plan_state": "active" if balance > 0 else "registered",
                "registration_ip_country_code": registration_ip_country_code,
                **balance_info,
            }
            return True
        except Exception as exc:
            self._last_check_overview = {
                "balance_query_status": "error",
                "balance_check_error": str(exc),
            }
            self.log(f"GoPay check_valid 失败: {exc}")
            return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def get_platform_actions(self) -> list:
        return [
            {"id": "query_balance", "label": "查询 GoPay 余额", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "query_balance":
            ok = self.check_valid(account)
            overview = self.get_last_check_overview()
            return {
                "ok": ok,
                "data": overview,
                "error": "" if ok else str(overview.get("balance_check_error") or "GoPay 余额查询失败"),
                "error_type": "" if ok else "balance_query_failed",
                "summary_updates": {} if ok else overview,
            }
        return super().execute_action(action_id, account, params)
