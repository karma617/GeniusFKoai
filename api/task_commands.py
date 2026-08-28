from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.task_commands import TaskCommandsService
from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["task-commands"])
command_service = TaskCommandsService()
query_service = TasksQueryService()


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "auto"
    extra: dict = Field(default_factory=dict)


class PhoneBindTaskRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    fallback_ids: list[int] = Field(default_factory=list)
    phone_lines: str
    browser_mode: str = "camoufox_headed"
    bit_profile_id: str = ""
    concurrency: int = 1


class CodexOAuthTaskRequest(BaseModel):
    platform: str = "chatgpt"
    account_id: int = 0
    ids: list[int] = Field(default_factory=list)
    browser_mode: str = "camoufox_headed"
    bit_profile_id: str = ""
    concurrency: int = 1


class MomoTrialProbeTaskRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    platform: str = "chatgpt"
    concurrency: int = 3


class TrialEligibilityProbeTaskRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    platform: str = "chatgpt"
    concurrency: int = 3
    proxies: dict[str, str] = Field(default_factory=dict)

class GoPayPayChatGptTaskRequest(BaseModel):
    """GoPay 协议付款 ChatGPT Plus。

    chatgpt_account_ids: 必填，要付款的 ChatGPT 账号 id 列表（串行处理）
    gopay_account_id: 可选，指定 GoPay 号；为空则自动从池里挑余额 ≥ 1 的
    cashier_url_override: 可选，跳过 generate_plus_link 协议步骤
    midtrans_url_override: 可选，跳过浏览器抓 URL 步骤（直接用这个）
    country/currency: 默认 ID/IDR
    headless: 浏览器无头（建议 false 让用户看见进度）
    grab_timeout: 浏览器等用户跳到 Midtrans 的最大秒数
    herosms_api_key: Hero-SMS 接码平台 API key，付款 OTP 用；不传则回退环境变量 OPAI_HEROSMS_API_KEY
    """

    chatgpt_account_ids: list[int] = Field(default_factory=list)
    gopay_account_id: int = 0
    cashier_url_override: str = ""
    midtrans_url_override: str = ""
    country: str = "ID"
    currency: str = "IDR"
    headless: bool = False
    checkout_mode: str = "camoufox_headed"
    bit_profile_id: str = ""
    envelope_url: str = ""
    concurrency: int = 1
    register_count: int = 0
    register_extra: dict = Field(default_factory=dict)
    proxy: Optional[str] = None
    grab_timeout: int = 300
    phone_ttl_seconds: int = 1200
    auto_register_gopay: bool = True
    gopay_pin: str = "147258"
    sms_provider: str = "herosms"
    smspool_api_key: str = ""
    smsbower_api_key: str = ""
    five_sim_api_key: str = ""
    api_sms_pool: str = ""
    proxy_pool: str = ""
    # smsapi / api_sms（固定手机号或号码池 + 查最新短信 API）渠道
    smsapi_url: str = ""
    smsapi_phone: str = ""
    herosms_api_key: str = ""
    # 拿号价格上限（USD），herosms 与 smspool 共用。空串走插件默认（0.11）。
    max_price: str = ""
    # 付款安全上限（IDR）。默认 0 允许免费订单和元数据完整的 GoPay 1 IDR 绑定验证。
    max_payment_amount_rp: int = 0
    # GoPay 号来源开关：auto（先池后注册）/ pool（只用号池，没号失败）/
    # register（强制现注册新号，忽略号池/指定号）。
    gopay_source: str = "auto"
    # #2：付款成功后自动换绑（买临时外国号绑上去，释放当前印尼号）。
    auto_rebind: bool = False
    # 换绑专用接码渠道（独立于注册渠道）：herosms / smsbower。
    rebind_provider: str = "herosms"
    rebind_sms_key: str = ""
    rebind_country: str = ""
    rebind_service: str = ""
    # 调试抓包开关：开启后抓到 midtrans_url 不关浏览器，停在付款页让人工手动
    # 走完 GoPay 网页付款，全程录 HAR + dump 每页 HTML，不跑协议付款。
    capture_payment: bool = False
    # 抓包产物目录（可选）；留空则用工作目录下 _gopay_capture/<时间戳>/。
    capture_dir: str = ""
    # Hosted 长链：优先采用 checkout 响应 url，缺失时再用 Stripe
    # payment_pages/init 生成 cashier_url；不开浏览器拿 cashier 链。
    use_stripe_init: bool = False
    # 短链：checkout_ui_mode=custom + Plus promo + taxes 同步，使用响应中的
    # processor_entity 返回 chatgpt.com/checkout/<entity>/<cs_id>。
    use_short_link: bool = False
    # protocol=直接纯协议拿 Midtrans；browser=先生成选定的长/短 cashier 链，
    # 再由浏览器选择 GoPay。空值由后端根据两个链接开关自动选择。
    link_mode: str = ""


class GoPayRegisterAccountTaskRequest(BaseModel):
    """协议注册 GoPay 账户并设置 PIN。"""

    gopay_pin: str = "147258"
    proxy: Optional[str] = None
    envelope_url: str = ""
    sms_provider: str = "herosms"
    smspool_api_key: str = ""
    smsbower_api_key: str = ""
    five_sim_api_key: str = ""
    api_sms_pool: str = ""
    proxy_pool: str = ""
    smsapi_url: str = ""
    smsapi_phone: str = ""
    herosms_api_key: str = ""
    max_price: str = ""
    auto_rebind: bool = False
    rebind_provider: str = "herosms"
    rebind_sms_key: str = ""
    rebind_country: str = ""
    rebind_service: str = ""


@router.post("/register")
def create_register_task(body: RegisterTaskRequest):
    return command_service.create_register_task(body.model_dump())


@router.post("/phone-bind")
def create_phone_bind_task(body: PhoneBindTaskRequest):
    return command_service.create_phone_bind_task(body.model_dump())


@router.post("/codex-oauth")
def create_codex_oauth_task(body: CodexOAuthTaskRequest):
    return command_service.create_codex_oauth_task(body.model_dump())


@router.post("/momo-trial-probe")
def create_momo_trial_probe_task(body: MomoTrialProbeTaskRequest):
    return command_service.create_momo_trial_probe_task(body.model_dump())


@router.post("/trial-eligibility-probe")
def create_trial_eligibility_probe_task(body: TrialEligibilityProbeTaskRequest):
    try:
        return command_service.create_trial_eligibility_probe_task(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class GetRtTaskRequest(BaseModel):
    platform: str = "chatgpt"
    account_id: int = 0
    ids: list[int] = Field(default_factory=list)
    task_mode: str = "single"
    executor_type: str = "browser"
    browser_mode: str = "camoufox_headed"
    concurrency: int = 1
    record_har: str = ""
    sms_provider: str = ""
    smspool_api_key: str = ""
    smspool_max_price: str = ""
    smsapi_phone: str = ""
    smsapi_url: str = ""
    phone_reuse_count: int = 3
    phone_change_limit: int = 10
    sms_balance_action: str = "auto_switch"


@router.post("/get-rt")
def create_get_rt_task(body: GetRtTaskRequest):
    return command_service.create_get_rt_task(body.model_dump())


class GetRtBypassTaskRequest(BaseModel):
    platform: str = "chatgpt"
    account_id: int = 0
    ids: list[int] = Field(default_factory=list)
    browser_mode: str = "camoufox_headed"
    concurrency: int = 1


@router.post("/get-rt-bypass")
def create_get_rt_bypass_task(body: GetRtBypassTaskRequest):
    return command_service.create_get_rt_bypass_task(body.model_dump())


class RefreshSessionTaskRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    concurrency: int = 1
    default_status: str = ""


@router.post("/refresh-session")
def create_refresh_session_task(body: RefreshSessionTaskRequest):
    return command_service.create_refresh_session_task(body.model_dump())


class BatchSecuritySetupTaskRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    concurrency: int = 1
    proxy: Optional[str] = None


@router.post("/batch-security-setup")
def create_batch_security_setup_task(body: BatchSecuritySetupTaskRequest):
    return command_service.create_batch_security_setup_task(body.model_dump())


class AgentsUploadSub2ApiTaskRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    batch_size: int = 10
    verify_task: bool = False
    timeout: int = 30


@router.post("/agents-upload-sub2api")
def create_agents_upload_sub2api_task(body: AgentsUploadSub2ApiTaskRequest):
    return command_service.create_agents_upload_sub2api_task(body.model_dump())


@router.post("/gopay-pay-chatgpt")
def create_gopay_pay_chatgpt_task(body: GoPayPayChatGptTaskRequest):
    return command_service.create_gopay_pay_chatgpt_task(body.model_dump())


@router.post("/gopay-register-account")
def create_gopay_register_account_task(body: GoPayRegisterAccountTaskRequest):
    return command_service.create_gopay_register_account_task(body.model_dump())


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    task = command_service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.post("/{task_id}/manual-post-register-capture/finish")
def finish_manual_post_register_capture(task_id: str):
    result = command_service.complete_manual_post_register_capture(task_id)
    if not result:
        raise HTTPException(404, "任务不存在")
    return result


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        command_service.stream_task_events(task_id, since=since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
