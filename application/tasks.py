"""Task orchestration and persistence helpers."""

from __future__ import annotations



import json
import re
import queue

import threading

import time

import uuid

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional



from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select, func


from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)

from core.base_platform import AccountStatus, RegisterConfig

from core.datetime_utils import format_local_clock, serialize_datetime

from core.db import AccountModel, TaskEventModel, TaskLog, TaskModel, engine, save_account

from core.platform_accounts import build_platform_account

from core.registry import get

from infrastructure.accounts_repository import AccountsRepository
from infrastructure.platform_runtime import PlatformRuntime
from application.ctf_plus import CtfPlusAccountsService

from application.phone_binding import PhoneBindingService



TASK_TYPE_REGISTER = "register"

TASK_TYPE_ACCOUNT_CHECK = "account_check"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_ACCOUNT_HEALTH_CHECK = "account_health_check"
TASK_TYPE_PLATFORM_ACTION = "platform_action"
TASK_TYPE_PHONE_BIND = "phone_bind"

TASK_TYPE_CODEX_OAUTH = "codex_oauth"
TASK_TYPE_MOMO_TRIAL_PROBE = "momo_trial_probe"
TASK_TYPE_GET_RT = "get_rt"
TASK_TYPE_GET_RT_BYPASS = "get_rt_bypass"
TASK_TYPE_REFRESH_SESSION = "refresh_session"
TASK_TYPE_AGENTS_UPLOAD_SUB2API = "agents_upload_sub2api"
TASK_TYPE_GOPAY_PAY_CHATGPT = "gopay_pay_chatgpt"
TASK_TYPE_GOPAY_REGISTER_ACCOUNT = "gopay_register_account"

TASK_DB_WRITE_ATTEMPTS = 5

BUGFREE_LABEL = "BUGFREE"
CHATGPT_TRIAL_LABEL = "试用"
MOMO_TRIAL_LABEL = "MOMO试用"
CHATGPT_RELOGIN_REQUIRED_STATUS = "relogin_required"
CHATGPT_FREE_PLUS_CAMPAIGN_ID = "plus-1-month-free"
CHATGPT_ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480"
CHATGPT_TRIAL_CHECK_MAX_ATTEMPTS = 3
CHATGPT_TRIAL_CHECK_BACKGROUND_CONCURRENCY = 2
_CHATGPT_TRIAL_CHECK_EXECUTOR = ThreadPoolExecutor(
    max_workers=CHATGPT_TRIAL_CHECK_BACKGROUND_CONCURRENCY,
    thread_name_prefix="chatgpt-trial-check",
)
CHATGPT_HEALTH_CHECK_NETWORK_RETRIES = 3
CHATGPT_HEALTH_CHECK_GLOBAL_CONCURRENCY = 1
CHATGPT_HEALTH_CHECK_MIN_INTERVAL_SECONDS = 1.2
CHATGPT_HEALTH_NODE_SWITCH_FAILURE_THRESHOLD = 3
CHATGPT_HEALTH_NODE_SWITCH_COOLDOWN_SECONDS = 30.0
_CHATGPT_HEALTH_CHECK_GATE = threading.BoundedSemaphore(CHATGPT_HEALTH_CHECK_GLOBAL_CONCURRENCY)
_CHATGPT_HEALTH_CHECK_SPACING_LOCK = threading.Lock()
_CHATGPT_HEALTH_CHECK_LAST_REQUEST_AT = 0.0
BUGFREE_SKIP_RESULT = "__bugfree_skip__"
SMSBOWER_MAIL_OTP_RETRY_RESULT = "__smsbower_mail_otp_retry__"
SMSBOWER_MAIL_OTP_RETRY_LIMIT_PER_ACCOUNT = 5
BUGFREE_TARGET_SECONDS = 7 * 24 * 60 * 60
BUGFREE_TARGET_TOLERANCE_SECONDS = 24 * 60 * 60
BUGFREE_MONTH_SKIP_SECONDS = 25 * 24 * 60 * 60

GET_RT_SMS_BALANCE_ACTION_AUTO_SWITCH = "auto_switch"
GET_RT_SMS_BALANCE_ACTION_WAIT_RELEASE = "wait_release"
GET_RT_SMS_BALANCE_ACTION_TERMINATE = "terminate"
GET_RT_SMS_BALANCE_ACTIONS = {
    GET_RT_SMS_BALANCE_ACTION_AUTO_SWITCH,
    GET_RT_SMS_BALANCE_ACTION_WAIT_RELEASE,
    GET_RT_SMS_BALANCE_ACTION_TERMINATE,
}


TASK_STATUS_PENDING = "pending"

TASK_STATUS_CLAIMED = "claimed"

TASK_STATUS_RUNNING = "running"

TASK_STATUS_SUCCEEDED = "succeeded"

TASK_STATUS_FAILED = "failed"

TASK_STATUS_INTERRUPTED = "interrupted"

TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"

TASK_STATUS_CANCELLED = "cancelled"



TERMINAL_TASK_STATUSES = {

    TASK_STATUS_SUCCEEDED,

    TASK_STATUS_FAILED,

    TASK_STATUS_INTERRUPTED,

    TASK_STATUS_CANCELLED,

}

MANUAL_POST_REGISTER_CAPTURE_DIR = Path("tools/captures")


def _manual_post_register_capture_signal_path(task_id: str) -> Path:
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id or "").strip()) or "task"
    return MANUAL_POST_REGISTER_CAPTURE_DIR / f"manual-post-register-finish-{safe_task_id}.signal"


def _clear_manual_post_register_capture_signal(task_id: str) -> None:
    try:
        path = _manual_post_register_capture_signal_path(task_id)
        if path.exists():
            path.unlink()
    except Exception:
        pass


def complete_manual_post_register_capture(task_id: str) -> Optional[dict[str, Any]]:
    task = get_task(task_id)
    if not task:
        return None
    path = _manual_post_register_capture_signal_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": str(task_id),
                "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    append_task_event(
        task_id,
        f"已收到手动后置抓包完成信号，准备停止 HAR 录制: {path}",
        event_type="state",
    )
    return {"ok": True, "task_id": str(task_id), "finish_signal_path": str(path)}

ACTIVE_TASK_STATUSES = {

    TASK_STATUS_CLAIMED,

    TASK_STATUS_RUNNING,

    TASK_STATUS_CANCEL_REQUESTED,

}





def _normalize_task_ids(values: Any) -> list[int]:

    ids: list[int] = []

    for item in values or []:

        try:

            account_id = int(item or 0)

        except Exception:

            continue

        if account_id > 0 and account_id not in ids:

            ids.append(account_id)

    return ids


def _normalize_get_rt_sms_balance_action(value: Any) -> str:
    action = str(value or "").strip().lower()
    aliases = {
        "switch": GET_RT_SMS_BALANCE_ACTION_AUTO_SWITCH,
        "next": GET_RT_SMS_BALANCE_ACTION_AUTO_SWITCH,
        "auto": GET_RT_SMS_BALANCE_ACTION_AUTO_SWITCH,
        "retry": GET_RT_SMS_BALANCE_ACTION_WAIT_RELEASE,
        "wait": GET_RT_SMS_BALANCE_ACTION_WAIT_RELEASE,
        "wait_current": GET_RT_SMS_BALANCE_ACTION_WAIT_RELEASE,
        "stop": GET_RT_SMS_BALANCE_ACTION_TERMINATE,
        "abort": GET_RT_SMS_BALANCE_ACTION_TERMINATE,
        "end": GET_RT_SMS_BALANCE_ACTION_TERMINATE,
    }
    action = aliases.get(action, action)
    if action not in GET_RT_SMS_BALANCE_ACTIONS:
        return GET_RT_SMS_BALANCE_ACTION_AUTO_SWITCH
    return action




def _filter_registered_get_rt_ids(

    ids: list[int],

    *,

    platform: str = "chatgpt",

) -> tuple[list[int], list[int]]:

    """Keep only accounts whose display status is exactly registered for get_rt."""

    normalized_ids = _normalize_task_ids(ids)

    if not normalized_ids:

        return [], []

    with Session(engine) as session:

        statement = select(AccountModel).where(AccountModel.id.in_(normalized_ids))

        if platform:

            statement = statement.where(AccountModel.platform == platform)

        models = session.exec(statement).all()

        model_map = {int(model.id or 0): model for model in models if model.id}

        graphs = load_account_graphs(session, list(model_map.keys()))

    allowed: list[int] = []

    skipped: list[int] = []

    for account_id in normalized_ids:

        model = model_map.get(account_id)

        if not model:

            skipped.append(account_id)

            continue

        graph = graphs.get(account_id, {}) or {}

        status = str(

            graph.get("display_status")

            or graph.get("lifecycle_status")

            or getattr(model, "display_status", "")

            or getattr(model, "lifecycle_status", "")

            or AccountStatus.REGISTERED.value

        ).strip().lower()

        if status == AccountStatus.REGISTERED.value:

            allowed.append(account_id)

        else:

            skipped.append(account_id)

    return allowed, skipped





def _filter_get_rt_target_ids(
    ids: list[int],
    *,
    platform: str = "chatgpt",
) -> tuple[list[int], list[int]]:
    """Keep accounts that target mode can finish: registered or RT pending upload."""
    normalized_ids = _normalize_task_ids(ids)

    if not normalized_ids:

        return [], []

    with Session(engine) as session:

        statement = select(AccountModel).where(AccountModel.id.in_(normalized_ids))

        if platform:

            statement = statement.where(AccountModel.platform == platform)

        models = session.exec(statement).all()

        model_map = {int(model.id or 0): model for model in models if model.id}

        graphs = load_account_graphs(session, list(model_map.keys()))

    allowed: list[int] = []

    skipped: list[int] = []

    allowed_statuses = {AccountStatus.REGISTERED.value, "rt_pending_upload"}

    for account_id in normalized_ids:

        model = model_map.get(account_id)

        if not model:

            skipped.append(account_id)

            continue

        graph = graphs.get(account_id, {}) or {}

        status = str(

            graph.get("display_status")

            or graph.get("lifecycle_status")

            or getattr(model, "display_status", "")

            or getattr(model, "lifecycle_status", "")

            or AccountStatus.REGISTERED.value

        ).strip().lower()

        if status in allowed_statuses:

            allowed.append(account_id)

        else:

            skipped.append(account_id)

    return allowed, skipped


def _list_account_ids_by_status(*, platform: str, status: str) -> list[int]:
    platform = str(platform or "chatgpt").strip() or "chatgpt"
    status = str(status or "").strip()
    if not status:
        return []
    with Session(engine) as session:
        statement = select(AccountModel)
        if platform:
            statement = statement.where(AccountModel.platform == platform)
        models = session.exec(statement.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())).all()
        model_map = {int(model.id or 0): model for model in models if model.id}
        graphs = load_account_graphs(session, list(model_map.keys()))
    return [
        account_id
        for account_id in model_map
        if str(
            (graphs.get(account_id, {}) or {}).get("display_status")
            or (graphs.get(account_id, {}) or {}).get("lifecycle_status")
            or AccountStatus.REGISTERED.value
        ).strip().lower() == status
    ]


AGENTS_UPLOAD_EXCLUDED_STATUSES = {"invalid", "banned", "expired", "relogin_required"}


def _is_normal_chatgpt_account_for_agents_upload(model: AccountModel, graph: dict[str, Any]) -> bool:
    statuses = {
        str(graph.get("display_status") or AccountStatus.REGISTERED.value).strip().lower(),
        str(graph.get("lifecycle_status") or AccountStatus.REGISTERED.value).strip().lower(),
        str(graph.get("validity_status") or "unknown").strip().lower(),
    }
    return not (statuses & AGENTS_UPLOAD_EXCLUDED_STATUSES)


def _list_agents_upload_account_ids(platform: str = "chatgpt") -> list[int]:
    platform = str(platform or "chatgpt").strip() or "chatgpt"
    with Session(engine) as session:
        models = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        ).all()
        model_map = {int(model.id or 0): model for model in models if model.id}
        graphs = load_account_graphs(session, list(model_map.keys()))
    return [
        account_id
        for account_id, model in model_map.items()
        if _is_normal_chatgpt_account_for_agents_upload(model, graphs.get(account_id, {}) or {})
    ]

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()





def _utcnow() -> datetime:

    return datetime.now(timezone.utc)





def _utcnow_iso() -> str:

    return _utcnow().isoformat().replace("+00:00", "Z")





def _serialize_datetime(value: datetime | None) -> str | None:

    return serialize_datetime(value)





def _json_default(value: Any) -> Any:

    if isinstance(value, datetime):

        return _serialize_datetime(value)

    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")





def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _is_database_locked_error(exc: Exception) -> bool:
    return "database is locked" in str(exc).lower()


def _sleep_db_write_retry(attempt: int) -> None:
    time.sleep(min(0.25 * (attempt + 1), 1.5))


def _safe_json_stem(value: Any) -> str:
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "_", str(value or "").strip())
    stem = stem.strip("._-")
    return stem or "account"


def _write_local_upload_json(target_dir: str, email: Any, payload: Any) -> str:
    directory = Path("data") / target_dir
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{_safe_json_stem(email)}_{timestamp}_{uuid.uuid4().hex[:8]}.json"
    path = directory / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return str(path)


def _is_global_sms_pool_exhausted_error(error: object) -> bool:
    return "SMS_POOL_EXHAUSTED" in str(error or "")





def _is_current_sms_phone_exhausted_error(error: object) -> bool:

    return "SMS_PHONE_EXHAUSTED" in str(error or "")





EMAIL_ALIAS_PARENT_RETRY_RESULT = "__email_alias_parent_retry__"
EMAIL_ALIAS_PARENT_RETRY_LIMIT_PER_ACCOUNT = 20


def _is_email_alias_parent_exhausted_error(error: object) -> bool:
    text = str(error or "")
    return (
        "EMAIL_ALIAS_PARENT_EXHAUSTED" in text
        or "Email alias quota exhausted for parent mailbox" in text
    )


def _is_email_alias_temporary_pool_error(error: object) -> bool:
    text = str(error or "")
    return "Gmail API接码邮箱池暂未找到可用邮箱" in text


def _is_email_alias_unavailable_parent_error(error: object) -> bool:
    text = str(error or "")
    return "Gmail API接码邮箱不可用或已下架" in text or "api_status=502" in text

_SMSPOOL_RELEASE_DRAIN_TASK_TYPES = {
    TASK_TYPE_REGISTER,

    TASK_TYPE_GET_RT,

    TASK_TYPE_GOPAY_PAY_CHATGPT,

    TASK_TYPE_GOPAY_REGISTER_ACCOUNT,

    TASK_TYPE_PHONE_BIND,

}





def _task_should_drain_smspool_release_queue(task: TaskModel | None) -> bool:

    if not task:

        return False

    task_type = str(getattr(task, "type", "") or "")

    if task_type in _SMSPOOL_RELEASE_DRAIN_TASK_TYPES:

        return True

    if task_type == TASK_TYPE_PLATFORM_ACTION:

        payload = task.get_payload()

        action_id = str(payload.get("action_id") or "").strip().lower()

        if action_id == "get_rt":

            return True

        params = dict(payload.get("params") or {})

        provider = str(params.get("sms_provider") or params.get("phone_provider") or "").strip().lower()

        return provider in {"smspool", "smspool_api", "sms_pool", "sms_pool_api"}

    return False





def _drain_smspool_release_queue_before_task_finish(task_id: str, log_fn: Callable[..., None] | None = None) -> None:

    try:

        with Session(engine) as session:

            task = session.get(TaskModel, task_id)

            should_drain = _task_should_drain_smspool_release_queue(task)

        if not should_drain:

            return



        from platforms.gopay.sms_channel import (

            get_smspool_release_queue_size,

            wait_for_smspool_release_queue_drain,

        )



        pending = get_smspool_release_queue_size()

        if pending <= 0:

            return



        if callable(log_fn):

            log_fn(

                "SMSPool\u91ca\u653e\u961f\u5217\u4ecd\u6709"

                f" {pending} "

                "\u4e2a\u5f85\u91ca\u653e\u53f7\u7801\uff0c\u4efb\u52a1\u7ed3\u675f\u524d\u7ee7\u7eed\u91ca\u653e..."

            )

        wait_for_smspool_release_queue_drain(

            log_fn=log_fn,

            max_wait_seconds=0,

        )

        if callable(log_fn):

            log_fn("SMSPool\u91ca\u653e\u961f\u5217\u5df2\u6e05\u7a7a\uff0c\u5141\u8bb8\u4efb\u52a1\u7ed3\u675f")

    except Exception as exc:

        if callable(log_fn):

            log_fn(f"SMSPool\u91ca\u653e\u961f\u5217\u6536\u5c3e\u68c0\u67e5\u5931\u8d25: {exc}", level="warning")





def _task_lock(task_id: str) -> threading.Lock:

    with _task_locks_guard:

        lock = _task_locks.get(task_id)

        if lock is None:

            lock = threading.Lock()

            _task_locks[task_id] = lock

        return lock





def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:

    with _task_lock(task_id):

        with Session(engine) as session:

            task = session.get(TaskModel, task_id)

            if not task:

                return None

            fn(task)

            task.updated_at = _utcnow()

            session.add(task)

            session.commit()

            session.refresh(task)

            return task





def _save_task_log(platform: str, email: str, status: str, error: str = "", detail: dict | None = None) -> None:

    with Session(engine) as session:

        log = TaskLog(

            platform=platform,

            email=email,

            status=status,

            error=error,

            detail_json=_dump_json(detail or {}),

        )

        session.add(log)

        session.commit()





def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:

    base = {"errors": [], "cashier_urls": [], "data": None}

    if result:

        base.update(result)

    return base





def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type in {TASK_TYPE_ACCOUNT_CHECK, TASK_TYPE_PLATFORM_ACTION}:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    if task_type == TASK_TYPE_ACCOUNT_HEALTH_CHECK:
        ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
        keys = [f"account:{account_id}" for account_id in ids]
        platform = str(payload.get("platform", "") or "").strip().lower()
        if not platform or platform == "chatgpt":
            # 防止多个批量测活任务同时打 ChatGPT，触发 WAF/IP 403。
            keys.append("chatgpt-health-check")
        return keys
    if task_type in {TASK_TYPE_PHONE_BIND, TASK_TYPE_CODEX_OAUTH, TASK_TYPE_MOMO_TRIAL_PROBE}:
        ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
        if not ids and int(payload.get("account_id") or 0) > 0:
            ids = [int(payload.get("account_id") or 0)]
        return [f"account:{account_id}" for account_id in ids]
    return []


def _task_concurrency_key(task_type: str, platform: str = "") -> str:
    """同平台同类型任务共用一条执行通道，不同类型互不阻塞。"""
    type_part = str(task_type or "").strip() or "unknown"
    platform_part = str(platform or "").strip() or "global"
    return f"{platform_part}:{type_part}"






def serialize_task(task: TaskModel) -> dict[str, Any]:

    result = task.get_result()

    progress_total = int(task.progress_total or 0)

    progress_current = int(task.progress_current or 0)

    status = task.status

    if status == TASK_STATUS_CANCEL_REQUESTED and progress_total > 0 and progress_current >= progress_total:

        status = TASK_STATUS_CANCELLED

    return {

        "id": task.id,

        "task_id": task.id,

        "type": task.type,

        "platform": task.platform,

        "status": status,

        "terminal": status in TERMINAL_TASK_STATUSES,

        "cancellable": status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING},

        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",

        "progress_detail": {

            "current": progress_current,

            "total": progress_total,

            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",

        },

        "success": int(task.success_count or 0),

        "error_count": int(task.error_count or 0),

        "errors": list(result.get("errors", [])),

        "cashier_urls": list(result.get("cashier_urls", [])),

        "data": result.get("data"),

        "result": result,

        "error": task.error,

        "created_at": _serialize_datetime(task.created_at),

        "started_at": _serialize_datetime(task.started_at),

        "finished_at": _serialize_datetime(task.finished_at),

        "updated_at": _serialize_datetime(task.updated_at),

    }





def serialize_event(event: TaskEventModel) -> dict[str, Any]:

    return {

        "id": event.id,

        "task_id": event.task_id,

        "type": event.type,

        "level": event.level,

        "message": event.message,

        "line": f"[{format_local_clock(event.created_at)}] {event.message}",

        "detail": event.get_detail(),

        "created_at": _serialize_datetime(event.created_at),

    }





def _create_task_without_db_lock_retry(
    *,

    task_type: str,

    platform: str,

    payload: dict[str, Any],

    progress_total: int = 1,

    result_seed: dict[str, Any] | None = None,

) -> dict[str, Any]:

    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    task = TaskModel(

        id=task_id,

        type=task_type,

        platform=platform,

        status=TASK_STATUS_PENDING,

        payload_json=_dump_json(payload),

        result_json=_dump_json(_task_result_seed(result_seed)),

        progress_current=0,

        progress_total=max(int(progress_total or 0), 0),

    )

    with Session(engine) as session:

        session.add(task)

        session.commit()

        session.refresh(task)

    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")

    return serialize_task(task)


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    last_error: OperationalError | None = None
    for attempt in range(TASK_DB_WRITE_ATTEMPTS):
        task = TaskModel(
            id=task_id,
            type=task_type,
            platform=platform,
            status=TASK_STATUS_PENDING,
            payload_json=_dump_json(payload),
            result_json=_dump_json(_task_result_seed(result_seed)),
            progress_current=0,
            progress_total=max(int(progress_total or 0), 0),
        )
        try:
            with Session(engine) as session:
                session.add(task)
                session.commit()
                session.refresh(task)
                serialized = serialize_task(task)
            append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
            return serialized
        except OperationalError as exc:
            if not _is_database_locked_error(exc) or attempt >= TASK_DB_WRITE_ATTEMPTS - 1:
                raise
            last_error = exc
            _sleep_db_write_retry(attempt)
    if last_error:
        raise last_error
    raise RuntimeError("创建任务失败")




def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:

    count = max(int(payload.get("count", 1) or 1), 1)

    return create_task(

        task_type=TASK_TYPE_REGISTER,

        platform=str(payload.get("platform", "")),

        payload=payload,

        progress_total=count,

    )





def create_account_check_task(account_id: int) -> dict[str, Any]:

    platform = ""

    with Session(engine) as session:

        model = session.get(AccountModel, account_id)

        if model:

            platform = model.platform

    return create_task(

        task_type=TASK_TYPE_ACCOUNT_CHECK,

        platform=platform,

        payload={"account_id": int(account_id)},

        progress_total=1,

    )





def create_account_check_all_task(platform: str = "", limit: int = 50) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload={"platform": platform, "limit": int(limit or 50)},
        progress_total=max(int(limit or 50), 1),
    )


def create_account_health_check_task(platform: str = "", ids: list[int] | None = None) -> dict[str, Any]:
    normalized_ids = [int(item) for item in ids or [] if int(item or 0) > 0]
    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        if normalized_ids:
            q = q.where(AccountModel.id.in_(normalized_ids))  # type: ignore[attr-defined]
        total = len(session.exec(q).all())
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_HEALTH_CHECK,
        platform=platform,
        payload={"platform": platform, "ids": normalized_ids},
        progress_total=max(total, 1),
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,

    )





def create_phone_bind_task(payload: dict[str, Any]) -> dict[str, Any]:

    selected = [item for item in payload.get("ids") or [] if int(item or 0) > 0]

    fallback = [item for item in payload.get("fallback_ids") or [] if int(item or 0) > 0]

    total = len(selected) if selected else max(len(fallback), 1)

    return create_task(

        task_type=TASK_TYPE_PHONE_BIND,

        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),

        payload=payload,

        progress_total=total,

    )





def create_codex_oauth_task(payload: dict[str, Any]) -> dict[str, Any]:

    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]

    account_id = int(payload.get("account_id") or 0)

    total = len(ids) if ids else (1 if account_id > 0 else 0)

    return create_task(

        task_type=TASK_TYPE_CODEX_OAUTH,

        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),

        payload=payload,

        progress_total=max(total, 1),

    )






def create_momo_trial_probe_task(payload: dict[str, Any]) -> dict[str, Any]:
    """创建后台任务：批量检测账号是否具备越南 MoMo 试用资格。

    payload:
      - ids: 账号 ID 列表；为空表示当前 platform 下全部账号
      - platform: 默认 chatgpt
      - concurrency: 并发线程数，默认 3，上限 10
    """
    raw_ids = payload.get("ids") or []
    ids: list[int] = []
    for item in raw_ids:
        try:
            value = int(item)
        except Exception:
            continue
        if value > 0:
            ids.append(value)
    # 去重保序
    seen: set[int] = set()
    uniq_ids: list[int] = []
    for value in ids:
        if value in seen:
            continue
        seen.add(value)
        uniq_ids.append(value)
    ids = uniq_ids

    platform = str(payload.get("platform") or "chatgpt").strip().lower() or "chatgpt"
    try:
        concurrency = int(payload.get("concurrency") or 3)
    except Exception:
        concurrency = 3
    concurrency = max(1, min(concurrency, 10))

    # ids 为空时按平台全量探测：先统计数量作为 progress_total
    if ids:
        total = len(ids)
    else:
        with Session(engine) as session:
            q = select(AccountModel).where(AccountModel.platform == platform)
            total = len(session.exec(q).all())

    task_payload = {
        "ids": ids,
        "platform": platform,
        "concurrency": concurrency,
    }
    return create_task(
        task_type=TASK_TYPE_MOMO_TRIAL_PROBE,
        platform=platform,
        payload=task_payload,
        progress_total=max(int(total or 0), 1),
    )



def create_get_rt_task(payload: dict[str, Any]) -> dict[str, Any]:

    """批量获取 refresh_token 任务创建。



    payload 包含 ids（账号 ID 列表）、browser_mode、concurrency。

    """

    payload = dict(payload or {})

    task_mode = str(payload.get("task_mode") or "single").strip().lower()

    if task_mode not in {"single", "target"}:

        task_mode = "single"

    payload["task_mode"] = task_mode
    payload["sms_balance_action"] = _normalize_get_rt_sms_balance_action(
        payload.get("sms_balance_action")
    )
    ids = _normalize_task_ids(payload.get("ids"))

    if not ids:

        account_id = int(payload.get("account_id") or 0)

        if account_id > 0:

            ids = [account_id]

    if task_mode == "target":

        ids, skipped_ids = _filter_get_rt_target_ids(

            ids,

            platform=str(payload.get("platform", "chatgpt") or "chatgpt"),

        )

    else:

        ids, skipped_ids = _filter_registered_get_rt_ids(

            ids,

            platform=str(payload.get("platform", "chatgpt") or "chatgpt"),

        )

    payload["ids"] = ids

    payload["account_id"] = 0

    payload["executor_type"] = str(payload.get("executor_type") or "browser")

    if skipped_ids:

        payload["skipped_non_registered_ids"] = skipped_ids

    total = len(ids) if ids else 1

    return create_task(

        task_type=TASK_TYPE_GET_RT,

        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),

        payload=payload,

        progress_total=max(total, 1),

    )





def create_get_rt_bypass_task(payload: dict[str, Any]) -> dict[str, Any]:

    """批量获取 refresh_token（绕过手机号）任务创建。"""

    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]

    total = len(ids) if ids else 1

    return create_task(

        task_type=TASK_TYPE_GET_RT_BYPASS,

        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),

        payload=payload,

        progress_total=max(total, 1),

    )





def create_refresh_session_task(payload: dict[str, Any]) -> dict[str, Any]:
    """批量重新登录并刷新 ChatGPT session/at。"""

    payload = dict(payload or {})
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    if not ids and str(payload.get("default_status") or "") == CHATGPT_RELOGIN_REQUIRED_STATUS:
        ids = _list_account_ids_by_status(
            platform=str(payload.get("platform", "chatgpt") or "chatgpt"),
            status=CHATGPT_RELOGIN_REQUIRED_STATUS,
        )
    payload["ids"] = ids
    if not payload.get("default_status"):
        payload["default_status"] = CHATGPT_RELOGIN_REQUIRED_STATUS
    return create_task(
        task_type=TASK_TYPE_REFRESH_SESSION,
        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),
        payload=payload,
        progress_total=max(len(ids), 1),
    )


def create_agents_upload_sub2api_task(payload: dict[str, Any]) -> dict[str, Any]:
    """为状态正常的 ChatGPT 账号生成 Agent Identity 并上传 SUB2API。"""

    payload = dict(payload or {})
    platform = str(payload.get("platform", "chatgpt") or "chatgpt")
    ids = _normalize_task_ids(payload.get("ids"))
    if not ids:
        ids = _list_agents_upload_account_ids(platform)
    payload["ids"] = ids
    payload["batch_size"] = max(int(payload.get("batch_size") or 10), 1)
    return create_task(
        task_type=TASK_TYPE_AGENTS_UPLOAD_SUB2API,
        platform=platform,
        payload=payload,
        progress_total=max(len(ids), 1),
    )


def create_gopay_pay_chatgpt_task(payload: dict[str, Any]) -> dict[str, Any]:
    """GoPay 协议付款 ChatGPT Plus 任务创建。



    payload 至少包含 ``chatgpt_account_ids: [int, ...]`` 或 ``register_count``；

    可选 ``gopay_account_id`` / ``cashier_url_override`` / ``midtrans_url_override``

    / ``country`` / ``currency`` / ``checkout_mode`` / ``bit_profile_id`` /

    ``envelope_url`` / ``concurrency`` / ``grab_timeout`` / ``phone_ttl_seconds``。

    progress_total = 选中账号数；若没选账号则用 register_count。

    """

    ids = [int(item) for item in payload.get("chatgpt_account_ids") or [] if int(item or 0) > 0]

    register_count = max(int(payload.get("register_count") or 0), 0)

    total = max(len(ids) or register_count, 1)

    return create_task(

        task_type=TASK_TYPE_GOPAY_PAY_CHATGPT,

        platform="chatgpt",

        payload=payload,

        progress_total=total,

    )





def create_gopay_register_account_task(payload: dict[str, Any]) -> dict[str, Any]:

    """GoPay 协议注册账号 + 设置 PIN 的单步任务。"""

    return create_task(

        task_type=TASK_TYPE_GOPAY_REGISTER_ACCOUNT,

        platform="gopay",

        payload=payload,

        progress_total=1,

    )





def get_task(task_id: str) -> Optional[dict[str, Any]]:

    with Session(engine) as session:

        task = session.get(TaskModel, task_id)

        return serialize_task(task) if task else None





def list_tasks(*, platform: str = "", status: str = "", task_type: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:

    page = max(page, 1)

    page_size = min(max(page_size, 1), 200)

    with Session(engine) as session:

        q = select(TaskModel)

        total_q = select(func.count()).select_from(TaskModel)

        if platform:

            q = q.where(TaskModel.platform == platform)

            total_q = total_q.where(TaskModel.platform == platform)

        if status:

            q = q.where(TaskModel.status == status)

            total_q = total_q.where(TaskModel.status == status)

        if task_type:

            q = q.where(TaskModel.type == task_type)

            total_q = total_q.where(TaskModel.type == task_type)

        q = q.order_by(TaskModel.created_at.desc())

        total = int(session.exec(total_q).one() or 0)

        items = session.exec(q.offset((page - 1) * page_size).limit(page_size)).all()

    return {"total": total, "page": page, "items": [serialize_task(item) for item in items]}





def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:

    limit = min(max(limit, 1), 500)

    with Session(engine) as session:

        q = (

            select(TaskEventModel)

            .where(TaskEventModel.task_id == task_id)

            .where(TaskEventModel.id > since)

            .order_by(TaskEventModel.id)

            .limit(limit)

        )

        items = session.exec(q).all()

    return [serialize_event(item) for item in items]





def _append_task_event_without_db_lock_retry(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:

        event = TaskEventModel(

            task_id=task_id,

            type=event_type,

            level=level,

            message=message,

            detail_json=_dump_json(detail or {}),

        )

        session.add(event)

        session.commit()

        session.refresh(event)

    return serialize_event(event)





def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    last_error: OperationalError | None = None
    for attempt in range(TASK_DB_WRITE_ATTEMPTS):
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        try:
            with Session(engine) as session:
                session.add(event)
                session.commit()
                session.refresh(event)
                return serialize_event(event)
        except OperationalError as exc:
            if not _is_database_locked_error(exc) or attempt >= TASK_DB_WRITE_ATTEMPTS - 1:
                raise
            last_error = exc
            _sleep_db_write_retry(attempt)
    if last_error:
        raise last_error
    raise RuntimeError("写入任务事件失败")


def mark_incomplete_tasks_interrupted() -> None:
    interrupted_ids: list[str] = []

    with Session(engine) as session:

        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)

        tasks = session.exec(

            select(TaskModel).where(TaskModel.status.in_(non_terminal))

        ).all()

        for task in tasks:

            task.status = TASK_STATUS_INTERRUPTED

            task.error = task.error or "任务在服务重启后被中断"

            task.finished_at = _utcnow()

            task.updated_at = _utcnow()

            session.add(task)

            interrupted_ids.append(task.id)

        session.commit()

    for task_id in interrupted_ids:

        append_task_event(

            task_id,

            "任务在服务重启后被标记为中断",

            event_type="state",

            level="warning",

        )





def request_cancel(task_id: str) -> Optional[dict[str, Any]]:

    task = _mutate_task(

        task_id,

        lambda model: _request_cancel_mutation(model),

    )

    if not task:

        return None

    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")

    return serialize_task(task)





def _request_cancel_mutation(task: TaskModel) -> None:

    if task.status in TERMINAL_TASK_STATUSES:

        return

    if task.status == TASK_STATUS_PENDING:

        task.status = TASK_STATUS_CANCELLED

        task.finished_at = _utcnow()

        task.error = task.error or "任务在开始前被取消"

    elif task.status == TASK_STATUS_CANCEL_REQUESTED:

        progress_total = int(task.progress_total or 0)

        progress_current = int(task.progress_current or 0)

        if progress_total > 0 and progress_current >= progress_total:

            task.status = TASK_STATUS_CANCELLED

            task.finished_at = task.finished_at or _utcnow()

            task.error = task.error or "任务已取消"

    else:

        task.status = TASK_STATUS_CANCEL_REQUESTED





def claim_next_runnable_task(

    *,

    running_task_group_counts: dict[str, int] | None = None,

    busy_account_keys: set[str] | None = None,

    max_parallel_per_task_group: int = 1,

) -> Optional[dict[str, Any]]:

    running_task_group_counts = dict(running_task_group_counts or {})

    busy_account_keys = set(busy_account_keys or set())

    with Session(engine) as session:

        # 读取数据库里全局运行中任务，避免多个后端进程/重载实例同时

        # 启动 ChatGPT 测活任务，触发 WAF/IP 403。

        active_tasks = session.exec(

            select(TaskModel).where(TaskModel.status.in_(ACTIVE_TASK_STATUSES))  # type: ignore[attr-defined]

        ).all()

        for active in active_tasks:

            active_payload = active.get_payload()

            active_platform = active.platform or str(active_payload.get("platform", "") or "")

            active_group = _task_concurrency_key(active.type, active_platform)

            running_task_group_counts[active_group] = running_task_group_counts.get(active_group, 0) + 1

            busy_account_keys.update(_task_account_keys(active.type, active_payload))



        tasks = session.exec(

            select(TaskModel)

            .where(TaskModel.status == TASK_STATUS_PENDING)

            .order_by(TaskModel.created_at)

        ).all()

        for task in tasks:

            payload = task.get_payload()

            platform = task.platform or str(payload.get("platform", "") or "")

            account_keys = _task_account_keys(task.type, payload)

            task_group = _task_concurrency_key(task.type, platform)

            if running_task_group_counts.get(task_group, 0) >= max_parallel_per_task_group:

                continue

            if account_keys and busy_account_keys.intersection(account_keys):

                continue

            task.status = TASK_STATUS_CLAIMED

            task.started_at = task.started_at or _utcnow()

            task.updated_at = _utcnow()

            session.add(task)

            session.commit()

            return {"id": task.id, "platform": platform, "type": task.type, "task_group": task_group, "account_keys": account_keys}

    return None





class TaskLogger:

    def __init__(self, task_id: str):

        self.task_id = task_id

        # 并发任务里每个 worker 通过 ``set_subtask`` 把自己的 subtask_id

        # 绑到 thread-local，之后 ``log()`` 自动把 ``subtask_id`` 注入

        # 事件 detail，前端按这个分组折叠展示。

        self._tlocal = threading.local()



    def set_subtask(self, subtask_id: str, label: str = "") -> None:

        """绑定当前线程的子任务标签。子任务结束后调 ``clear_subtask`` 解绑。



        ``subtask_id`` 是稳定标识（如 ``worker_1``）；``label`` 是给前端

        展示的人类可读标题（如"账号 #1"）。

        """

        self._tlocal.subtask_id = str(subtask_id or "")

        self._tlocal.subtask_label = str(label or "")



    def clear_subtask(self) -> None:

        try:

            del self._tlocal.subtask_id

        except AttributeError:

            pass

        try:

            del self._tlocal.subtask_label

        except AttributeError:

            pass



    def _current_subtask(self) -> tuple[str, str]:

        sid = getattr(self._tlocal, "subtask_id", "") or ""

        label = getattr(self._tlocal, "subtask_label", "") or ""

        return sid, label



    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:

        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠

        merged_detail = dict(detail or {})

        sid, slabel = self._current_subtask()

        if sid and "subtask_id" not in merged_detail:

            merged_detail["subtask_id"] = sid

        if slabel and "subtask_label" not in merged_detail:

            merged_detail["subtask_label"] = slabel

        append_task_event(

            self.task_id,

            message,

            event_type=event_type,

            level=level,

            detail=merged_detail or None,

        )

        prefix = f"[task:{self.task_id}]"

        if sid:

            prefix += f"[{sid}]"

        print(f"{prefix} {message}")



    def mark_running(self) -> None:

        def _update(task: TaskModel) -> None:

            task.status = TASK_STATUS_RUNNING

            task.started_at = task.started_at or _utcnow()



        _mutate_task(self.task_id, _update)

        self.log("任务已开始执行", event_type="state")



    def is_cancel_requested(self) -> bool:

        with Session(engine) as session:

            task = session.get(TaskModel, self.task_id)

            return bool(task and task.status == TASK_STATUS_CANCEL_REQUESTED)



    def set_progress(self, current: int, total: Optional[int] = None) -> None:

        current = max(int(current), 0)



        def _update(task: TaskModel) -> None:

            task.progress_current = current

            if total is not None:

                task.progress_total = max(int(total), 0)



        _mutate_task(self.task_id, _update)



    def record_success(self) -> None:

        def _update(task: TaskModel) -> None:

            task.success_count += 1



        _mutate_task(self.task_id, _update)



    def record_error(self, error: str) -> None:

        def _update(task: TaskModel) -> None:

            task.error_count += 1

            result = task.get_result()

            errors = list(result.get("errors", []))

            errors.append(error)

            result["errors"] = errors

            task.set_result(result)



        _mutate_task(self.task_id, _update)



    def add_cashier_url(self, url: str) -> None:

        def _update(task: TaskModel) -> None:

            result = task.get_result()

            urls = list(result.get("cashier_urls", []))

            urls.append(url)

            result["cashier_urls"] = urls

            task.set_result(result)



        _mutate_task(self.task_id, _update)



    def set_result_data(self, data: Any) -> None:

        def _update(task: TaskModel) -> None:

            result = task.get_result()

            result["data"] = data

            task.set_result(result)



        _mutate_task(self.task_id, _update)



    def finish(self, status: str, *, error: str = "") -> None:

        if status in TERMINAL_TASK_STATUSES:

            _drain_smspool_release_queue_before_task_finish(self.task_id, self.log)



        def _update(task: TaskModel) -> None:

            task.status = status

            task.finished_at = _utcnow()

            if error:

                task.error = error



        _mutate_task(self.task_id, _update)

        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")

        self.log(

            f"任务结束: {status}",

            level=event_level,

            event_type="state",

            detail={"status": status, "error": error},

        )





def _auto_push_any2api(task_logger: TaskLogger, account) -> None:

    """注册成功后自动推送账号到 Any2API（如果已配置）。"""

    try:

        from core.any2api_sync import push_account_to_any2api

        push_account_to_any2api(account, log_fn=task_logger.log)

    except Exception as exc:

        task_logger.log(f"  [Any2API] 自动推送异常: {exc}", level="warning")





def _build_chatgpt_upload_account(account):

    """构造上传用轻量账号对象，统一给 CPA / SUB2API 等外部面板使用。"""

    class _AccountProxy:

        pass



    target = _AccountProxy()

    extra = account.extra or {}

    target.email = account.email

    target.access_token = extra.get("access_token") or account.token

    target.refresh_token = extra.get("refresh_token", "")

    target.id_token = extra.get("id_token", "")

    target.session_token = extra.get("session_token", "")

    target.workspace_id = extra.get("workspace_id", "")

    target.expires_at = extra.get("expires_at", "")

    target.session = extra.get("session", {})

    target.user_id = account.user_id or ""

    target.account_id = account.user_id or extra.get("account_id", "")

    target.cookies = extra.get("cookies", "")

    target.extra = extra

    return target


def _format_bugfree_reset_at(reset_at: int) -> str:
    if reset_at <= 0:
        return ""
    try:
        return datetime.fromtimestamp(reset_at, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return ""


def _extract_chatgpt_account_id_for_usage(account) -> str:
    extra = dict(getattr(account, "extra", {}) or {})
    overview = extra.get("account_overview") if isinstance(extra.get("account_overview"), dict) else {}
    return str(
        extra.get("chatgpt_account_id")
        or extra.get("chatgptAccountId")
        or extra.get("account_id")
        or overview.get("chatgpt_account_id")
        or overview.get("chatgptAccountId")
        or overview.get("account_id")
        or getattr(account, "user_id", "")
        or ""
    ).strip()


def _extract_chatgpt_access_token(account) -> str:
    extra = dict(getattr(account, "extra", {}) or {})
    session = extra.get("session") if isinstance(extra.get("session"), dict) else {}
    return str(
        extra.get("access_token")
        or extra.get("accessToken")
        or session.get("accessToken")
        or session.get("access_token")
        or getattr(account, "token", "")
        or ""
    ).strip()


def _extract_chatgpt_session_token(account) -> str:
    extra = dict(getattr(account, "extra", {}) or {})
    session = extra.get("session") if isinstance(extra.get("session"), dict) else {}
    return str(
        extra.get("session_token")
        or extra.get("sessionToken")
        or session.get("sessionToken")
        or session.get("session_token")
        or ""
    ).strip()


def _extract_chatgpt_cookies(account) -> str:
    extra = dict(getattr(account, "extra", {}) or {})
    return str(extra.get("cookies") or extra.get("cookie") or extra.get("cookie_header") or "").strip()


def _auto_enable_chatgpt_2fa_after_register(
    account,
    logger: TaskLogger,
    *,
    proxy: str | None = None,
    enable: bool | None = None,
    require_password_set: bool = False,
) -> None:
    extra = dict(getattr(account, "extra", {}) or {})
    enabled = _bool_config(extra.get("enable_2fa_after_register", False), False) if enable is None else bool(enable)
    if not enabled:
        logger.log("2FA: 未勾选设置2FA，跳过")
        return
    if _bool_config(extra.get("mfa_enabled"), False) or str(extra.get("totp_secret") or "").strip():
        logger.log("2FA: 已在注册后置浏览器任务中设置完成，跳过重复设置")
        return
    if _bool_config(extra.get("manual_post_register_capture"), False):
        logger.log("2FA: 手动后置抓包模式已完成，跳过自动设置2FA")
        return
    if require_password_set and not _bool_config(extra.get("password_set_after_register"), False):
        account_overview = dict(extra.get("account_overview") or {})
        account_overview.update({"mfa_enabled": False, "mfa_error": "password_not_set"})
        extra.update({"mfa_enabled": False, "mfa_error": "password_not_set", "account_overview": account_overview})
        account.extra = extra
        logger.log("2FA: 设置帐号密码未成功，跳过自动设置2FA", level="warning")
        return
    cookies = _extract_chatgpt_cookies(account)
    session_token = _extract_chatgpt_session_token(account)
    access_token = _extract_chatgpt_access_token(account)
    if not (cookies or session_token or access_token):
        logger.log("2FA: 缺少 ChatGPT cookies/session_token/access_token，跳过自动设置", level="warning")
        return
    try:
        from platforms.chatgpt.mfa import enable_totp_mfa

        result = enable_totp_mfa(
            cookies=cookies,
            session_token=session_token,
            access_token=access_token,
            proxy=proxy,
            log_fn=logger.log,
            user_agent=str(extra.get("chatgpt_user_agent") or ""),
            accept_language=str(extra.get("chatgpt_accept_language") or ""),
            client_version=str(extra.get("chatgpt_oai_client_version") or ""),
            client_build_number=str(extra.get("chatgpt_oai_client_build_number") or ""),
            device_id=str(extra.get("chatgpt_oai_device_id") or ""),
            oai_session_id=str(extra.get("chatgpt_oai_session_id") or ""),
        )
        account_overview = dict(extra.get("account_overview") or {})
        chips = list(account_overview.get("chips") or [])
        if "2FA已绑" not in [str(item) for item in chips]:
            chips.append("2FA已绑")
        account_overview.update(
            {
                "mfa_enabled": True,
                "mfa_type": "totp",
                "mfa_factor_id": str(result.get("mfa_factor_id") or ""),
                "chips": chips,
            }
        )
        extra.update(
            {
                "totp_secret": str(result.get("totp_secret") or ""),
                "mfa_factor_id": str(result.get("mfa_factor_id") or ""),
                "mfa_session_id": str(result.get("mfa_session_id") or ""),
                "mfa_enabled": True,
                "mfa_type": "totp",
                "account_overview": account_overview,
            }
        )
        account.extra = extra
        logger.log("2FA: TOTP 已设置并保存密钥")
    except Exception as exc:
        account_overview = dict(extra.get("account_overview") or {})
        account_overview.update({"mfa_enabled": False, "mfa_error": str(exc)[:240]})
        extra.update({"mfa_enabled": False, "mfa_error": str(exc)[:240], "account_overview": account_overview})
        account.extra = extra
        logger.log(f"2FA: 自动设置失败，账号已保留: {exc}", level="warning")


def _inspect_chatgpt_bugfree_usage(account, *, proxy: str | None = None, timeout: int = 20) -> dict[str, Any]:
    from curl_cffi import requests as cffi_requests
    from platforms.chatgpt.payment import WHAM_USAGE_URL, _build_proxy_request_kwargs

    access_token = _extract_chatgpt_access_token(account)
    if not access_token:
        return {
            "ok": False,
            "url": WHAM_USAGE_URL,
            "error": "缺少 access_token，无法查询 BUGFREE 额度周期",
        }

    headers = {
        "authorization": f"Bearer {access_token}",
        "accept": "application/json",
        "openai-beta": "codex-1",
        "oai-language": "zh-CN",
        "originator": "Codex Desktop",
        "sec-fetch-site": "none",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-dest": "empty",
    }
    chatgpt_account_id = _extract_chatgpt_account_id_for_usage(account)
    if chatgpt_account_id:
        headers["Chatgpt-Account-Id"] = chatgpt_account_id

    try:
        response = cffi_requests.get(
            WHAM_USAGE_URL,
            headers=headers,
            timeout=timeout,
            impersonate="chrome124",
            **_build_proxy_request_kwargs(proxy),
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if not (200 <= status_code < 300):
            body = str(getattr(response, "text", "") or "")[:240]
            return {
                "ok": False,
                "url": WHAM_USAGE_URL,
                "status_code": status_code,
                "error": f"wham/usage HTTP {status_code}: {body}".strip(),
            }
        data = response.json()
        if not isinstance(data, dict):
            return {
                "ok": False,
                "url": WHAM_USAGE_URL,
                "status_code": status_code,
                "error": "wham/usage 响应格式异常",
            }
    except Exception as exc:
        return {
            "ok": False,
            "url": WHAM_USAGE_URL,
            "status_code": 0,
            "error": f"{exc.__class__.__name__}: {exc}",
        }

    window = data.get("rate_limit", {}).get("primary_window", {}) if isinstance(data.get("rate_limit"), dict) else {}
    try:
        reset_at = int(window.get("reset_at") or 0) if isinstance(window, dict) else 0
    except Exception:
        reset_at = 0
    now_ts = int(time.time())
    reset_delta_seconds = reset_at - now_ts if reset_at > 0 else 0
    reset_days = reset_delta_seconds / 86400 if reset_delta_seconds > 0 else 0
    reset_at_text = _format_bugfree_reset_at(reset_at)
    is_bugfree = (
        reset_delta_seconds > 0
        and abs(reset_delta_seconds - BUGFREE_TARGET_SECONDS) <= BUGFREE_TARGET_TOLERANCE_SECONDS
    )
    is_month_window = reset_delta_seconds >= BUGFREE_MONTH_SKIP_SECONDS
    if is_bugfree:
        decision = "bugfree"
        reason = "额度刷新时间在 7 天左右"
    elif is_month_window:
        decision = "skip_month"
        reason = "额度刷新时间约 30 天，跳过当前账号"
    else:
        decision = "skip"
        reason = "额度刷新时间不在 7 天左右，跳过当前账号"
    return {
        "ok": True,
        "url": WHAM_USAGE_URL,
        "status_code": status_code,
        "usage": data,
        "reset_at": reset_at,
        "reset_at_text": reset_at_text,
        "reset_delta_seconds": reset_delta_seconds,
        "reset_days": reset_days,
        "is_bugfree": is_bugfree,
        "decision": decision,
        "reason": reason,
    }


def _mark_bugfree_account(saved_account_id: int, usage_info: dict[str, Any]) -> None:
    if saved_account_id <= 0:
        return
    with Session(engine) as session:
        model = session.get(AccountModel, saved_account_id)
        if not model:
            return
        graph = load_account_graphs(session, [saved_account_id]).get(saved_account_id, {})
        overview = dict(graph.get("overview") or {})
        chips = [
            str(item).strip()
            for item in (overview.get("chips") or [])
            if str(item or "").strip()
        ]
        if BUGFREE_LABEL not in chips:
            chips.append(BUGFREE_LABEL)
        usage = usage_info.get("usage") if isinstance(usage_info.get("usage"), dict) else {}
        patch_account_graph(
            session,
            model,
            summary_updates={
                "chips": chips,
                "bugfree": True,
                "bugfree_reset_at": int(usage_info.get("reset_at") or 0),
                "bugfree_reset_at_text": str(usage_info.get("reset_at_text") or ""),
                "bugfree_reset_days": round(float(usage_info.get("reset_days") or 0), 3),
                "wham_usage": usage,
                "chatgpt_usage": usage,
            },
        )
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()


def _find_chatgpt_free_plus_trial_campaign(data: dict[str, Any], account_id: str = "") -> dict[str, Any] | None:
    accounts = data.get("accounts") if isinstance(data, dict) else {}
    if not isinstance(accounts, dict):
        return None
    candidates: list[dict[str, Any]] = []
    if account_id and isinstance(accounts.get(account_id), dict):
        candidates.append(accounts[account_id])
    if isinstance(accounts.get("default"), dict):
        candidates.append(accounts["default"])
    candidates.extend(
        item
        for key, item in accounts.items()
        if key not in {account_id, "default"} and isinstance(item, dict)
    )
    for item in candidates:
        campaigns = item.get("eligible_promo_campaigns")
        if not isinstance(campaigns, dict):
            continue
        plus = campaigns.get("plus")
        if not isinstance(plus, dict):
            continue
        metadata = plus.get("metadata") if isinstance(plus.get("metadata"), dict) else {}
        discount = metadata.get("discount") if isinstance(metadata.get("discount"), dict) else {}
        try:
            percentage = int(float(discount.get("percentage") or 0))
        except (TypeError, ValueError):
            percentage = 0
        if plus.get("id") == CHATGPT_FREE_PLUS_CAMPAIGN_ID and percentage == 100:
            return plus
    return None


def _is_chatgpt_trial_check_retryable_error(error: Any) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "curl:",
            "tls",
            "ssl",
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "connection closed",
            "remote end closed",
            "name resolution",
            "temporary failure",
            "failed to perform",
            "failed to establish a new connection",
        )
    )


def _is_chatgpt_health_check_retryable_error(status_code: int, error: Any) -> bool:
    code = int(status_code or 0)
    if code in {403, 408, 409, 425, 429} or 500 <= code <= 599:
        return True
    if code != 0:
        return False
    return _is_chatgpt_trial_check_retryable_error(error)


def _call_chatgpt_health_check(fetch_fn: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Serialize ChatGPT health probes across concurrent tasks in this process.

    Python threads share process/network resources; they are not isolated browser
    environments. If several batch-health tasks probe ChatGPT at once, the same
    local proxy/upstream IP can trigger WAF 403. This gate keeps health probes
    globally low-rate even when multiple tasks are running.
    """
    global _CHATGPT_HEALTH_CHECK_LAST_REQUEST_AT
    with _CHATGPT_HEALTH_CHECK_GATE:
        with _CHATGPT_HEALTH_CHECK_SPACING_LOCK:
            elapsed = time.monotonic() - _CHATGPT_HEALTH_CHECK_LAST_REQUEST_AT
            delay = max(CHATGPT_HEALTH_CHECK_MIN_INTERVAL_SECONDS - elapsed, 0.0)
            if delay > 0:
                time.sleep(delay)
            _CHATGPT_HEALTH_CHECK_LAST_REQUEST_AT = time.monotonic()
        try:
            return fetch_fn(**kwargs)
        finally:
            with _CHATGPT_HEALTH_CHECK_SPACING_LOCK:
                _CHATGPT_HEALTH_CHECK_LAST_REQUEST_AT = time.monotonic()


class _ChatGPTHealthNetworkCoordinator:
    """Coordinate global Clash node changes across one concurrent health-check task."""

    def __init__(
        self,
        *,
        failure_threshold: int = CHATGPT_HEALTH_NODE_SWITCH_FAILURE_THRESHOLD,
        cooldown_seconds: float = CHATGPT_HEALTH_NODE_SWITCH_COOLDOWN_SECONDS,
    ) -> None:
        self.failure_threshold = max(int(failure_threshold or 1), 1)
        self.cooldown_seconds = max(float(cooldown_seconds or 0), 0.0)
        self._lock = threading.Lock()
        self._failed_accounts: set[int] = set()
        self._last_switch_at = 0.0
        self._refresh_unavailable = False

    def report_failure(
        self,
        *,
        account_id: int,
        reason: str,
        logger: "TaskLogger" | None,
    ) -> bool:
        with self._lock:
            if self._refresh_unavailable:
                return False
            self._failed_accounts.add(int(account_id))
            if len(self._failed_accounts) < self.failure_threshold:
                return False
            now = time.monotonic()
            if self._last_switch_at and now - self._last_switch_at < self.cooldown_seconds:
                return False
            self._failed_accounts.clear()
            refreshed = _refresh_chatgpt_local_proxy_node(reason=reason, logger=logger)
            if refreshed:
                self._last_switch_at = time.monotonic()
            else:
                self._refresh_unavailable = True
            return refreshed


def _is_local_proxy_url(value: Any) -> bool:
    proxy = str(value or "").strip().lower()
    return any(marker in proxy for marker in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0"))


def _chatgpt_trial_check_request_kwargs(proxy: str | None) -> dict[str, Any]:
    from core.http_client import _normalize_runtime_proxy_url, _proxy_curl_options
    from core.proxy_pool import get_proxy_runtime_config, normalize_proxy_url

    request_kwargs: dict[str, Any] = {}
    proxy_url = _normalize_runtime_proxy_url(normalize_proxy_url(proxy) or proxy)
    if proxy_url:
        request_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        if not _is_local_proxy_url(proxy_url):
            curl_options = _proxy_curl_options(get_proxy_runtime_config().get("upstream_url", ""))
            if curl_options:
                request_kwargs["curl_options"] = curl_options
    return request_kwargs


def _inspect_chatgpt_free_plus_trial(
    account,
    *,
    proxy: str | None = None,
    timeout: int = 20,
    max_attempts: int = CHATGPT_TRIAL_CHECK_MAX_ATTEMPTS,
    retry_log: Callable[[int, int, float, str], None] | None = None,
) -> dict[str, Any]:
    from curl_cffi import requests as cffi_requests

    access_token = _extract_chatgpt_access_token(account)
    if not access_token:
        return {
            "ok": False,
            "url": CHATGPT_ACCOUNTS_CHECK_URL,
            "eligible": False,
            "error": "缺少 access_token，无法查询免费 Plus 试用权益",
        }

    headers = {
        "authorization": f"Bearer {access_token}",
        "accept": "application/json",
        "oai-language": "zh-CN",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    chatgpt_account_id = _extract_chatgpt_account_id_for_usage(account)
    if chatgpt_account_id:
        headers["Chatgpt-Account-Id"] = chatgpt_account_id

    request_kwargs = _chatgpt_trial_check_request_kwargs(proxy)

    attempts = max(int(max_attempts or 1), 1)
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = cffi_requests.get(
                CHATGPT_ACCOUNTS_CHECK_URL,
                headers=headers,
                timeout=timeout,
                impersonate="chrome124",
                **request_kwargs,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if not (200 <= status_code < 300):
                body = str(getattr(response, "text", "") or "")[:240]
                return {
                    "ok": False,
                    "url": CHATGPT_ACCOUNTS_CHECK_URL,
                    "status_code": status_code,
                    "eligible": False,
                    "attempts": attempt,
                    "error": f"accounts/check HTTP {status_code}: {body}".strip(),
                }
            data = response.json()
            if not isinstance(data, dict):
                return {
                    "ok": False,
                    "url": CHATGPT_ACCOUNTS_CHECK_URL,
                    "status_code": status_code,
                    "eligible": False,
                    "attempts": attempt,
                    "error": "accounts/check 响应格式异常",
                }
            break
        except Exception as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            if attempt < attempts and _is_chatgpt_trial_check_retryable_error(last_error):
                if proxy:
                    try:
                        proxy_pool.report_fail(proxy)
                    except Exception:
                        pass
                delay = min(1.5 * attempt, 5.0)
                if retry_log:
                    retry_log(attempt, attempts, delay, last_error)
                time.sleep(delay)
                continue
            return {
                "ok": False,
                "url": CHATGPT_ACCOUNTS_CHECK_URL,
                "status_code": 0,
                "eligible": False,
                "attempts": attempt,
                "error": last_error,
            }
    else:
        return {
            "ok": False,
            "url": CHATGPT_ACCOUNTS_CHECK_URL,
            "status_code": 0,
            "eligible": False,
            "attempts": attempts,
            "error": last_error or "accounts/check 请求失败",
        }

    campaign = _find_chatgpt_free_plus_trial_campaign(data, chatgpt_account_id)
    metadata = campaign.get("metadata") if isinstance(campaign, dict) and isinstance(campaign.get("metadata"), dict) else {}
    discount = metadata.get("discount") if isinstance(metadata.get("discount"), dict) else {}
    duration = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
    return {
        "ok": True,
        "url": CHATGPT_ACCOUNTS_CHECK_URL,
        "status_code": status_code,
        "eligible": bool(campaign),
        "attempts": attempt,
        "campaign": campaign or {},
        "campaign_id": str((campaign or {}).get("id") or ""),
        "plan_name": str(metadata.get("plan_name") or ""),
        "title": str(metadata.get("title") or ""),
        "discount_percentage": discount.get("percentage"),
        "duration": duration,
    }


def _mark_chatgpt_trial_account(saved_account_id: int, trial_info: dict[str, Any]) -> None:
    if saved_account_id <= 0:
        return
    with Session(engine) as session:
        model = session.get(AccountModel, saved_account_id)
        if not model:
            return
        graph = load_account_graphs(session, [saved_account_id]).get(saved_account_id, {})
        overview = dict(graph.get("overview") or {})
        chips = [
            str(item).strip()
            for item in (overview.get("chips") or [])
            if str(item or "").strip()
        ]
        if CHATGPT_TRIAL_LABEL not in chips:
            chips.append(CHATGPT_TRIAL_LABEL)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "chips": chips,
                "chatgpt_free_plus_trial": True,
                "chatgpt_free_plus_trial_campaign_id": str(trial_info.get("campaign_id") or ""),
                "chatgpt_free_plus_trial_plan_name": str(trial_info.get("plan_name") or ""),
                "chatgpt_free_plus_trial_title": str(trial_info.get("title") or ""),
                "chatgpt_free_plus_trial_discount_percentage": trial_info.get("discount_percentage"),
                "chatgpt_free_plus_trial_duration": trial_info.get("duration") or {},
            },
        )
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()


def _saved_account_id(saved_model: Any, account: Any) -> int:
    try:
        saved_account_id = int(getattr(saved_model, "id", 0) or 0)
        if saved_account_id > 0:
            return saved_account_id
    except Exception:
        pass

    platform = str(getattr(account, "platform", "") or "").strip()
    email = str(getattr(account, "email", "") or "").strip()
    if not platform or not email:
        return 0
    with Session(engine) as session:
        model = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .where(AccountModel.email == email)
        ).first()
        return int(getattr(model, "id", 0) or 0) if model else 0


def _run_bugfree_post_register_check(
    *,
    account,
    saved_account_id: int,
    logger: TaskLogger,
    proxy: str | None,
) -> bool:
    usage_info = _inspect_chatgpt_bugfree_usage(account, proxy=proxy)
    logger.log(f"  [BUGFREE] 请求额度接口: {usage_info.get('url') or ''}")
    if not usage_info.get("ok"):
        logger.log(f"  [BUGFREE] 查询失败: {usage_info.get('error') or 'unknown'}", level="error")
        return False

    reset_at = int(usage_info.get("reset_at") or 0)
    reset_at_text = str(usage_info.get("reset_at_text") or "")
    reset_days = float(usage_info.get("reset_days") or 0)
    logger.log(
        "  [BUGFREE] rate_limit.primary_window.reset_at="
        f"{reset_at}（{reset_at_text or '无法转换'}），距离当前约 {reset_days:.2f} 天"
    )
    if not usage_info.get("is_bugfree"):
        logger.log(f"  [BUGFREE] {usage_info.get('reason') or '非目标账号'}", level="warning")
        return False

    _mark_bugfree_account(saved_account_id, usage_info)
    logger.log(f"  [BUGFREE] 已确认并打标签 {BUGFREE_LABEL}")
    return True


def _run_chatgpt_trial_post_register_check(
    *,
    account,
    saved_account_id: int,
    logger: TaskLogger,
    proxy: str | None,
) -> bool:
    if str(getattr(account, "platform", "") or "").strip().lower() != "chatgpt":
        return False
    logger.log(f"  [试用] 请求权益接口: {CHATGPT_ACCOUNTS_CHECK_URL}")

    def _log_trial_check_retry(attempt: int, total: int, delay: float, error: str) -> None:
        logger.log(
            f"  [试用] 第 {attempt}/{total} 次查询失败: {error}，{delay:g}s 后重试",
            level="warning",
        )

    trial_info = _inspect_chatgpt_free_plus_trial(
        account,
        proxy=proxy,
        retry_log=_log_trial_check_retry,
    )
    if not trial_info.get("ok"):
        logger.log(f"  [试用] 查询失败，保留已注册账号: {trial_info.get('error') or 'unknown'}", level="warning")
        return False
    if not trial_info.get("eligible"):
        logger.log("  [试用] 当前账号没有免费领取 Plus 权益")
        return False

    _mark_chatgpt_trial_account(saved_account_id, trial_info)
    logger.log(f"  [试用] 已确认免费领取 Plus 权益并打标签 {CHATGPT_TRIAL_LABEL}")
    return True


def _schedule_chatgpt_trial_post_register_check(
    *,
    account,
    saved_account_id: int,
    logger: TaskLogger,
    proxy: str | None,
) -> None:
    if str(getattr(account, "platform", "") or "").strip().lower() != "chatgpt":
        return

    subtask_id, subtask_label = logger._current_subtask()

    def _run_in_background() -> None:
        if subtask_id:
            logger.set_subtask(subtask_id, subtask_label)
        try:
            _run_chatgpt_trial_post_register_check(
                account=account,
                saved_account_id=saved_account_id,
                logger=logger,
                proxy=proxy,
            )
        finally:
            if subtask_id:
                logger.clear_subtask()

    _CHATGPT_TRIAL_CHECK_EXECUTOR.submit(_run_in_background)
    logger.log("  [试用] 权益检查已转入低优先级后台")




def _auto_upload_cpa(task_logger: TaskLogger, account) -> None:

    if getattr(account, "platform", "") != "chatgpt":

        return

    try:

        from core.config_store import config_store



        cpa_url = config_store.get("cpa_api_url", "")

        cpa_key = config_store.get("cpa_api_key", "")

        if cpa_url and cpa_key:

            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa



            target = _build_chatgpt_upload_account(account)

            token_data = generate_token_json(target)

            ok, msg = upload_to_cpa(token_data)

            task_logger.log(f"  [CPA] {'✓ ' + msg if ok else '✗ ' + msg}")

    except Exception as exc:

        task_logger.log(f"  [CPA] 自动上传异常: {exc}", level="warning")





def _auto_upload_sub2api(task_logger: TaskLogger, account) -> None:

    """获取 refresh_token 后自动导入 SUB2API；仅注册号不得上传。"""

    if getattr(account, "platform", "") != "chatgpt":

        return None

    try:

        from core.config_store import config_store



        sub2api_url = config_store.get("sub2api_url", "")

        if not sub2api_url:

            return None

        target = _build_chatgpt_upload_account(account)

        if not str(getattr(target, "refresh_token", "") or "").strip():

            task_logger.log("  [SUB2API] 跳过：账号尚未获取 rt，仅注册状态不上传")

            return False

        from platforms.chatgpt.sub2api_upload import upload_to_sub2api



        max_attempts = 6

        last_msg = ""

        for attempt in range(1, max_attempts + 1):

            ok, msg = upload_to_sub2api(target)

            last_msg = str(msg or "")

            if ok:

                if attempt > 1:

                    task_logger.log(f"  [SUB2API] 第 {attempt}/{max_attempts} 次重试成功")

                task_logger.log(f"  [SUB2API] ✓ {last_msg}")

                return True

            task_logger.log(f"  [SUB2API] ✗ {last_msg}")

            if "请求异常" not in last_msg and "request" not in last_msg.lower() and "curl:" not in last_msg.lower():

                return False

            if attempt < max_attempts:

                delay = min(5 * attempt, 30)

                task_logger.log(f"  [SUB2API] 请求异常，{delay}s 后重试 ({attempt + 1}/{max_attempts})")

                time.sleep(delay)

        task_logger.log(f"  [SUB2API] 请求异常重试 {max_attempts} 次仍失败，保留为未上传: {last_msg}", level="warning")

        return False

    except Exception as exc:
        task_logger.log(f"  [SUB2API] 自动上传异常: {exc}", level="warning")
        return False


def _save_local_upload_jsons(task_logger: TaskLogger, account) -> tuple[str, str]:
    if getattr(account, "platform", "") != "chatgpt":
        return "", ""
    cpa_path = ""
    sub2api_path = ""
    target = _build_chatgpt_upload_account(account)
    try:
        from platforms.chatgpt.cpa_upload import generate_token_json

        cpa_payload = generate_token_json(target)
        cpa_path = _write_local_upload_json("cpa", getattr(account, "email", ""), cpa_payload)
        task_logger.log(f"  [本地CPA] 已保存: {cpa_path}")
    except Exception as exc:
        task_logger.log(f"  [本地CPA] 保存失败: {exc}", level="warning")
    try:
        sub2api_payload = _build_local_sub2api_payload(target)
        sub2api_path = _write_local_upload_json("sub2api", getattr(account, "email", ""), sub2api_payload)
        task_logger.log(f"  [本地SUB2API] 已保存: {sub2api_path}")
    except Exception as exc:
        task_logger.log(f"  [本地SUB2API] 保存失败: {exc}", level="warning")
    return cpa_path, sub2api_path


def _build_local_sub2api_payload(account) -> dict:
    from platforms.chatgpt.sub2api_upload import (
        DEFAULT_SUB2API_CONCURRENCY,
        DEFAULT_SUB2API_RATE_MULTIPLIER,
        _account_expires,
        _account_name,
        _account_plan_type,
        _account_tokens,
        _decode_jwt_payload,
        _extract_credential,
        _normalize_string,
        _seconds_until,
        _strip_empty,
    )

    tokens = _account_tokens(account)
    access_token = tokens["access_token"]
    if not access_token:
        raise ValueError("账号缺少 access_token，无法生成 SUB2API JSON。")
    claims = _decode_jwt_payload(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}
    expires_at, expires_epoch = _account_expires(account, access_token)
    account_id = (
        _normalize_string(auth_info.get("chatgpt_account_id") if isinstance(auth_info, dict) else "")
        or _extract_credential(account, "chatgpt_account_id")
        or _extract_credential(account, "account_id")
        or _normalize_string(getattr(account, "account_id", ""))
    )
    user_id = (
        _normalize_string(auth_info.get("chatgpt_user_id") if isinstance(auth_info, dict) else "")
        or _normalize_string(auth_info.get("user_id") if isinstance(auth_info, dict) else "")
        or _normalize_string(getattr(account, "user_id", ""))
    )
    workspace_id = (
        _normalize_string(getattr(account, "workspace_id", ""))
        or _extract_credential(account, "workspace_id")
        or _normalize_string(auth_info.get("organization_id") if isinstance(auth_info, dict) else "")
    )
    email = _normalize_string(getattr(account, "email", "")) or _normalize_string(claims.get("email") if isinstance(claims, dict) else "")
    payload = {
        "name": email or _account_name(account, access_token),
        "platform": "openai",
        "type": "oauth",
        "expires_at": expires_epoch,
        "auto_pause_on_expired": True,
        "concurrency": DEFAULT_SUB2API_CONCURRENCY,
        "priority": 1,
        "rate_multiplier": DEFAULT_SUB2API_RATE_MULTIPLIER,
        "group_ids": [],
        "credentials": _strip_empty({
            "access_token": access_token,
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
            "session_token": tokens["session_token"],
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "organization_id": workspace_id,
            "email": email,
            "expires_at": expires_at,
            "expires_in": _seconds_until(expires_at),
            "plan_type": _account_plan_type(account),
            "client_id": _extract_credential(account, "client_id") or _extract_credential(account, "clientId"),
        }),
        "extra": _strip_empty({
            "email": email,
            "name": email or _account_name(account, access_token),
            "source": "geniusfkoai",
            "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }),
    }
    return _strip_empty(payload)


def _collect_k12_deferred_sub2api_paths(account) -> list[str]:
    extra = dict(getattr(account, "extra", {}) or {})
    if not _bool_config(extra.get("k12_deferred_sub2api_upload_enabled"), False):
        return []

    paths: list[str] = []
    for path in extra.get("k12_sub2api_paths") or []:
        path_text = str(path or "").strip()
        if path_text:
            paths.append(path_text)

    for item in extra.get("k12_workspace_sessions") or []:
        if not isinstance(item, dict):
            continue
        path_text = str(item.get("sub2api_path") or "").strip()
        if path_text:
            paths.append(path_text)

    seen: set[str] = set()
    unique_paths: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique_paths.append(path)
    return unique_paths


def _finalize_k12_deferred_sub2api_uploads(paths: list[str], task_logger: TaskLogger) -> None:
    seen: set[str] = set()
    unique_paths: list[str] = []
    for path in paths:
        path_text = str(path or "").strip()
        if not path_text or path_text in seen:
            continue
        seen.add(path_text)
        unique_paths.append(path_text)
    if not unique_paths:
        return

    try:
        from platforms.chatgpt.k12_join import (
            merge_sub2api_export_files,
            upload_sub2api_export_accounts,
        )

        task_logger.log(f"[K12] 注册子任务已结束，开始合并 {len(unique_paths)} 个 SUB2API JSON")
        merged_path, payload = merge_sub2api_export_files(unique_paths)
        accounts = payload.get("accounts") if isinstance(payload, dict) else []
        task_logger.log(f"[K12] SUB2API 总 JSON 已保存: {Path(merged_path).resolve()}")
        ok, message = upload_sub2api_export_accounts(
            accounts if isinstance(accounts, list) else [],
            log=task_logger.log,
        )
        task_logger.log(f"[K12] {message}", level=None if ok else "warning")
    except Exception as exc:
        task_logger.log(f"[K12] SUB2API 统一上传异常: {exc}", level="warning")


def _run_agent_identity_auth_json_post_register_upload(account, logger: TaskLogger) -> tuple[bool, str]:
    target = _build_chatgpt_upload_account(account)
    access_token = str(getattr(target, "access_token", "") or "").strip()
    if not access_token:
        return False, "账号缺少 access_token，无法生成 Agent Identity auth.json"

    from platforms.chatgpt.codex_agent_identity import create_codex_agent_identity
    from platforms.chatgpt.sub2api_upload import upload_agent_identity_auths_to_sub2api

    logger.log(f"  [Agent Identity] {getattr(account, 'email', '')}: 生成 auth.json")
    auth_json = create_codex_agent_identity(
        access_token,
        verify_task=False,
        timeout=30,
    )
    ok, message, _result = upload_agent_identity_auths_to_sub2api(
        [auth_json],
        timeout=30,
    )
    if ok:
        logger.log(f"  [Agent Identity] 上传到 Sub2Api 成功：{message}")
        return True, message
    return False, message or "上传到 Sub2Api 失败"


def _mark_agent_identity_auth_json_upload_status(
    account_id: int,
    *,
    uploaded: bool,
    upload_message: str = "",
) -> None:
    lifecycle_status = "agent_identity_uploaded" if uploaded else AccountStatus.REGISTERED.value
    with Session(engine) as session:
        model = session.get(AccountModel, int(account_id or 0))
        if not model:
            return
        now_text = _utcnow_iso()
        summary_updates = {
            "lifecycle_status": lifecycle_status,
            "display_status": lifecycle_status,
            "valid": True,
            "agent_identity_upload_status": "uploaded" if uploaded else "failed",
            "agent_identity_upload_message": str(upload_message or ""),
            "agent_identity_upload_checked_at": now_text,
        }
        if uploaded:
            summary_updates["agent_identity_uploaded_at"] = now_text
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates=summary_updates,
        )
        model.updated_at = datetime.now(timezone.utc)
        session.add(model)
        session.commit()


def _mark_get_rt_upload_status(
    account_id: int,

    *,

    uploaded: bool,

    upload_message: str = "",

) -> None:

    from core.db import AccountModel



    lifecycle_status = "rt_uploaded" if uploaded else "rt_pending_upload"

    with Session(engine) as session:

        model = session.get(AccountModel, int(account_id or 0))

        if not model:

            return

        now_text = _utcnow_iso()

        summary_updates = {

            "lifecycle_status": lifecycle_status,

            "display_status": lifecycle_status,

            "valid": True,

            "rt_upload_status": "uploaded" if uploaded else "pending_upload",

            "rt_upload_message": str(upload_message or ""),

            "rt_upload_checked_at": now_text,

        }

        if uploaded:

            summary_updates["rt_uploaded_at"] = now_text

        else:

            summary_updates["rt_acquired_at"] = now_text

        patch_account_graph(

            session,

            model,

            lifecycle_status=lifecycle_status,

            summary_updates=summary_updates,

        )

        model.updated_at = datetime.now(timezone.utc)

        session.add(model)

        session.commit()





def _mailbox_account_from_platform_account(
    account,
    provider_names: set[str] | None = None,
) -> Any | None:
    extra = dict(getattr(account, "extra", {}) or {})
    resources = list(extra.get("provider_resources") or [])
    identity = dict(extra.get("identity") or {})
    if isinstance(identity.get("provider_resource"), dict):
        resources.append(identity["provider_resource"])
    for item in resources:

        if not isinstance(item, dict):
            continue
        provider_name = str(item.get("provider_name") or item.get("provider") or "").strip().lower()
        if provider_names is not None and provider_name not in provider_names:
            continue
        resource_type = str(item.get("resource_type") or "mailbox").strip().lower()
        if resource_type != "mailbox":
            continue
        handle = str(item.get("handle") or item.get("email") or getattr(account, "email", "") or "").strip()
        resource_id = str(item.get("resource_identifier") or item.get("account_id") or "").strip()
        if not handle:
            continue
        from core.base_mailbox import MailboxAccount


        return MailboxAccount(

            email=handle,

            account_id=resource_id,

            extra={"provider_resource": item},

        )

    return None


def _outlook_mailbox_account_from_platform_account(account) -> Any | None:
    return _mailbox_account_from_platform_account(
        account,
        provider_names={"outlook_email", "outlook_email_api"},
    )


def _resolve_outlook_mailbox_for_tagging(shared_mailbox, mailbox_account):
    if shared_mailbox is not None:

        if hasattr(shared_mailbox, "mark_registration_success") or hasattr(shared_mailbox, "mark_plus_success"):

            return shared_mailbox

        resolver = getattr(shared_mailbox, "_resolve_mailbox", None)

        if callable(resolver):

            try:

                resolved = resolver(mailbox_account)

                if hasattr(resolved, "mark_registration_success") or hasattr(resolved, "mark_plus_success"):

                    return resolved

            except Exception:

                pass



    try:

        from core.outlook_email_mailbox import OutlookEmailMailbox

        from infrastructure.provider_settings_repository import ProviderSettingsRepository



        settings = ProviderSettingsRepository().resolve_runtime_settings("mailbox", "outlook_email_api", {})

        if settings.get("outlook_email_api_url") and settings.get("outlook_email_api_key"):

            return OutlookEmailMailbox.from_config(settings)

    except Exception:

        return None

    return None





def _mark_outlook_mailbox_event(shared_mailbox, account, event: str, logger: TaskLogger) -> None:
    prefer_shared = False
    if shared_mailbox is not None:
        if event == "registration_success" and hasattr(shared_mailbox, "mark_registration_success"):
            prefer_shared = True
        elif event == "plus_success" and hasattr(shared_mailbox, "mark_plus_success"):
            prefer_shared = True
    if prefer_shared:
        mailbox_account = _mailbox_account_from_platform_account(account)
        mailbox = shared_mailbox
    else:
        mailbox_account = _outlook_mailbox_account_from_platform_account(account)
    if mailbox_account is None:
        return
    if not prefer_shared:
        mailbox = _resolve_outlook_mailbox_for_tagging(shared_mailbox, mailbox_account)
    if mailbox is None:
        return
    try:

        if event == "registration_success":

            applied = mailbox.mark_registration_success(mailbox_account)

            label = "注册成功"

        elif event == "plus_success":

            applied = mailbox.mark_plus_success(mailbox_account)

            label = "Plus 开通成功"

        else:

            return

        if applied:
            logger.log(f"邮箱 {label}后已打标签: {', '.join(applied)}")
    except Exception as exc:
        logger.log(f"邮箱自动打标签失败（忽略）: {exc}", level="warning")


def _is_smsbower_mail_otp_timeout_error(error: str) -> bool:
    text = str(error or "")
    lowered = text.lower()
    return (
        "等待 smsbower 验证码超时" in lowered
        or "等待 smsbower 验证链接超时" in lowered
        or (
            "smsbower" in lowered
            and (
                "code has not been received" in lowered
                or "验证码超时" in lowered
                or "verification code not received" in lowered
            )
        )
    )


def _is_smsbower_mailbox_account(mailbox_account) -> bool:
    extra = dict(getattr(mailbox_account, "extra", {}) or {})
    provider_resource = extra.get("provider_resource") if isinstance(extra.get("provider_resource"), dict) else {}
    provider_account = extra.get("provider_account") if isinstance(extra.get("provider_account"), dict) else {}
    markers = [
        extra.get("mailbox_provider_key"),
        provider_resource.get("provider_name"),
        provider_account.get("provider_name"),
    ]
    return any("smsbower" in str(item or "").strip().lower() for item in markers)


def _release_smsbower_mailbox_after_otp_timeout(platform, shared_mailbox, logger: TaskLogger, reason: str) -> bool:
    identity = getattr(platform, "_last_identity", None)
    mailbox_account = getattr(identity, "mailbox_account", None)
    if mailbox_account is None or not _is_smsbower_mailbox_account(mailbox_account):
        return False

    mailbox = shared_mailbox or getattr(platform, "mailbox", None)
    marker = getattr(mailbox, "mark_invalid_email", None)
    if not callable(marker):
        return False

    try:
        applied = marker(mailbox_account, reason=reason)
        email_text = str(getattr(mailbox_account, "email", "") or "").strip()
        suffix = f": {', '.join(applied)}" if applied else ""
        logger.log(f"SMSBower 邮箱验证码超时，已释放当前邮箱 {email_text}{suffix}")
        return True
    except Exception as exc:
        logger.log(f"SMSBower 邮箱释放失败（忽略）: {exc}", level="warning")
        return False


def _maybe_wrap_email_alias_mailbox(mailbox, *, platform_name: str, extra: dict[str, Any], logger: TaskLogger):
    if mailbox is None:
        return None
    _attach_mailbox_logger(mailbox, logger.log)
    if getattr(mailbox, "email_alias_enabled", False):
        return mailbox
    if not _bool_config(
        extra.get("enable_email_alias", extra.get("email_alias_enabled")),
        False,
    ):
        return mailbox
    mail_provider = str(extra.get("mail_provider") or "").strip().lower()
    if mail_provider in {"icloud_hme", "icloud"} or type(mailbox).__name__ == "ICloudHMEMailbox":
        return mailbox

    from core.email_alias_mailbox import EmailAliasMailbox, normalize_email_alias_limit

    return EmailAliasMailbox(
        mailbox,
        alias_limit=normalize_email_alias_limit(extra.get("email_alias_limit")),
        platform=platform_name,
        log_fn=logger.log,
    )


def _attach_mailbox_logger(mailbox, log_fn) -> None:
    if mailbox is None:
        return
    if hasattr(mailbox, "_log_fn"):
        try:
            setattr(mailbox, "_log_fn", log_fn)
        except Exception:
            pass


def _build_platform_instance(platform_name: str, payload: dict[str, Any], logger: TaskLogger, resolved_proxy: str | None = None, shared_mailbox=None):
    from core.base_identity import normalize_identity_provider

    from core.base_mailbox import create_mailbox



    executor_type = str(payload.get("executor_type", "protocol") or "protocol")

    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")

    extra = dict(payload.get("extra") or {})
    if platform_name == "chatgpt":
        extra.setdefault("_task_id", logger.task_id)
        extra.setdefault(
            "_manual_post_register_capture_finish_path",
            str(_manual_post_register_capture_signal_path(logger.task_id)),
        )
        extra.setdefault("_cancel_check", logger.is_cancel_requested)

    config = RegisterConfig(

        executor_type=executor_type,

        captcha_solver=captcha_solver,

        proxy=resolved_proxy,

        extra=extra,

    )

    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))

    mailbox = shared_mailbox

    if mailbox is None and identity_provider in {"mailbox", "sms_oauth"}:

        if not extra.get("mail_provider"):

            from infrastructure.provider_settings_repository import ProviderSettingsRepository



            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")

        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=None,
        )
    if identity_provider in {"mailbox", "sms_oauth"}:
        mailbox = _maybe_wrap_email_alias_mailbox(
            mailbox,
            platform_name=platform_name,
            extra=extra,
            logger=logger,
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)

    if hasattr(platform, "set_logger"):

        platform.set_logger(logger.log)

    else:

        platform._log_fn = logger.log

    return platform





def _run_single_account_check(account_id: int, logger: TaskLogger | None = None) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(config=RegisterConfig())

        account = build_platform_account(session, model)



    valid = plugin.check_valid(account)
    refreshed_graph: dict[str, Any] = {}
    with Session(engine) as session:

        model = session.get(AccountModel, account_id)

        if model:

            model.updated_at = _utcnow()

            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})

            summary_updates = {"checked_at": _utcnow_iso(), "valid": bool(valid)}

            if hasattr(plugin, "get_last_check_overview"):

                summary_updates.update(plugin.get_last_check_overview() or {})

            lifecycle_status = None

            if valid:

                # **bug 修复**：原实现 ``recover_lifecycle_status_for_valid_account``

                # 直接读 ``current_graph`` 老快照——但 plugin 刚拉到的新

                # ``plan_state`` 在 ``summary_updates`` 里、还没写回 graph，

                # 导致 free → 重新刷新仍然被认成 subscribed。这里把

                # ``summary_updates`` merge 到 graph 里再算 lifecycle。

                merged_graph = dict(current_graph)

                merged_overview = dict(merged_graph.get("overview") or {})

                merged_overview.update(summary_updates)

                merged_graph["overview"] = merged_overview

                lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)

            patch_account_graph(

                session,

                model,

                lifecycle_status=lifecycle_status,

                summary_updates=summary_updates,

            )

            session.add(model)

            session.commit()
            refreshed_graph = load_account_graphs(session, [account_id]).get(account_id, {})
    overview = refreshed_graph.get("overview") or {}
    usage = overview.get("chatgpt_usage") if isinstance(overview.get("chatgpt_usage"), dict) else {}
    result = {
        "account_id": account_id,
        "valid": bool(valid),
        "platform": account.platform,
        "email": account.email,
        "plan_state": refreshed_graph.get("plan_state") or overview.get("plan_state") or "",
        "plan_name": refreshed_graph.get("plan_name") or overview.get("plan_name") or "",
        "display_status": refreshed_graph.get("display_status") or overview.get("display_status") or "",
        "subscription_status": overview.get("subscription_status") or "",
        "usage_plan_type": usage.get("plan_type") or usage.get("planType") or "",
    }
    if logger:

        logger.log(f"{account.email}: {'有效' if valid else '失效'}")
    return valid, result


def _chatgpt_health_error_text(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, default=str).strip()
    except Exception:
        return str(value).strip()


def _chatgpt_health_status_code(value: Any) -> int:
    if isinstance(value, dict):
        for key in ("status_code", "status", "http_status", "code"):
            try:
                status_code = int(value.get(key) or 0)
            except (TypeError, ValueError):
                status_code = 0
            if 100 <= status_code <= 599:
                return status_code
    text = _chatgpt_health_error_text(value)
    match = re.search(r"\b(?:HTTP|status(?:_code)?|status)\D*(\d{3})\b", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\b(400|401|403|404|429|5\d\d)\b", text)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return 0
    return 0


def _chatgpt_health_state_error(state: dict[str, Any]) -> Any:
    for key in ("profile_error", "token_refresh_error", "subscription_error", "codex_usage_error"):
        value = state.get(key)
        if value not in (None, "", {}, []):
            return value
    return ""


def _is_chatgpt_relogin_required_health_error(status_code: int, error_text: str) -> bool:
    text = str(error_text or "").lower()
    return status_code == 401 or (
        "token_expired" in text
        or "authentication token is expired" in text
        or "try signing in again" in text
    )


def _is_chatgpt_banned_health_error(status_code: int, error_text: str) -> bool:
    """Return True only for explicit account-ban signals.

    ChatGPT/Cloudflare can return HTTP 403 for IP/WAF/proxy pressure,
    especially during concurrent probes. Health-check probes cannot reliably
    distinguish real bans from WAF 403s, so this path never mutates lifecycle
    status to `banned`; generic 403 stays transient.
    """
    # Health-check probes cannot reliably distinguish account bans from WAF/IP
    # 403s. Do not mutate lifecycle_status to `banned` from this path; users can
    # still set banned manually or through stronger login/action errors.
    return False


def _sanitize_chatgpt_state_for_health_result(state: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key in (
        "platform",
        "desktop_app",
        "session_token_present",
        "quota_note",
        "valid",
        "account_id",
        "subscription_status",
        "profile_error",
        "token_refresh_error",
        "subscription_error",
        "codex_usage_error",
        "codex_usage",
        "usage_breakdowns",
        "prompt_remaining_percent",
        "next_reset_at",
    ):
        if key in state:
            sanitized[key] = state[key]
    remote_user = state.get("remote_user") if isinstance(state.get("remote_user"), dict) else state.get("profile")
    if isinstance(remote_user, dict):
        remote_email = str(remote_user.get("email") or "").strip()
        if remote_email:
            sanitized["remote_email"] = remote_email
    probe = state.get("codex_usage_probe")
    if isinstance(probe, dict):
        sanitized["codex_usage_probe"] = {
            key: probe.get(key)
            for key in ("source", "force", "url", "model", "status_code", "duration_ms", "account_id_present")
            if key in probe
        }
    extra = state.get("codex_usage_extra")
    if isinstance(extra, dict):
        sanitized["codex_usage_extra"] = dict(extra)
    return sanitized


def _normalize_chatgpt_plan_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if any(token in text for token in ("team", "enterprise", "business")):
        return "team"
    if any(token in text for token in ("plus", "pro", "premium", "paid")):
        return "plus"
    if text in {"trial", "trialing", "free_trial", "trial-active", "trial_active"}:
        return "trial"
    if text in {"free", "basic", "starter", "hobby"}:
        return "free"
    return text


def _resolve_chatgpt_subscription_status(
    result: dict[str, Any],
    account_state: dict[str, Any],
    usage: dict[str, Any],
) -> str:
    usage_plan = _normalize_chatgpt_plan_type(usage.get("plan_type") or usage.get("planType"))
    if usage_plan in {"plus", "team", "pro", "enterprise"}:
        return usage_plan
    for candidate in (
        result.get("plan_type"),
        account_state.get("subscription_status"),
        usage_plan,
    ):
        normalized = _normalize_chatgpt_plan_type(candidate)
        if normalized:
            return normalized
    return ""


def _run_single_chatgpt_health_check(
    account_id: int,
    logger: TaskLogger | None = None,
    network_coordinator: _ChatGPTHealthNetworkCoordinator | None = None,
) -> dict[str, Any]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        if model.platform != "chatgpt":
            raise ValueError("批量测活仅支持 ChatGPT 账号")
        account = build_platform_account(session, model)

    extra = dict(account.extra or {})
    access_token = _extract_chatgpt_access_token(account)
    session_token = _extract_chatgpt_session_token(account)
    cookies = _extract_chatgpt_cookies(account)
    chatgpt_account_id = _extract_chatgpt_account_id_for_usage(account)
    if not (access_token or session_token or cookies):
        result = {
            "account_id": account_id,
            "email": account.email,
            "valid": False,
            "status_code": 0,
            "error": "缺少 access_token/session_token/cookies，无法查询账号状态/订阅",
        }
        _persist_chatgpt_health_result(account_id, result)
        if logger:
            logger.log(f"{account.email}: 测活失效（缺少状态查询凭据）", level="warning")
        return result

    proxy = None
    proxy_pool = None
    region = str(account.region or extra.get("region") or "")

    def _resolve_probe_proxy(*, retry: bool = False) -> str | None:
        nonlocal proxy_pool
        try:
            from platforms.chatgpt.plugin import _resolve_action_proxy, proxy_pool
            return _resolve_action_proxy(
                None,
                region=region,
                log_fn=logger.log if logger else None,
                action_label="批量测活重试换代理" if retry else "批量测活",
            )
        except Exception as exc:
            if logger:
                logger.log(f"{account.email}: 代理解析失败，改用直连: {exc}", level="warning")
            return None

    try:
        from platforms.chatgpt.switch import fetch_chatgpt_account_state

        attempts = max(int(CHATGPT_HEALTH_CHECK_NETWORK_RETRIES or 0), 0) + 1
        state: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            proxy = _resolve_probe_proxy(retry=attempt > 1)
            try:
                state = _call_chatgpt_health_check(
                    fetch_chatgpt_account_state,
                    access_token=access_token,
                    session_token=session_token,
                    cookies=cookies,
                    proxy=proxy,
                    chatgpt_account_id=chatgpt_account_id,
                    existing_extra=extra,
                    force_usage=True,
                )
            except Exception as exc:
                error_text = f"{exc.__class__.__name__}: {exc}"
                if attempt < attempts and _is_chatgpt_health_check_retryable_error(0, error_text):
                    if network_coordinator:
                        network_coordinator.report_failure(
                            account_id=account_id,
                            reason=f"批量测活连续网络错误: {error_text[:160]}",
                            logger=logger,
                        )
                    delay = min(1.5 * attempt, 5.0)
                    if logger:
                        logger.log(
                            f"{account.email}: 测活网络错误第 {attempt}/{attempts} 次: {error_text[:240]}，{delay:g}s 后重试",
                            level="warning",
                        )
                    time.sleep(delay)
                    continue
                raise

            if not isinstance(state, dict):
                raise ValueError("账号状态/订阅响应格式异常")
            if bool(state.get("valid")):
                break
            retry_error = _chatgpt_health_state_error(state)
            retry_status_code = _chatgpt_health_status_code(retry_error)
            retry_error_text = _chatgpt_health_error_text(retry_error)
            if (
                attempt < attempts
                and _is_chatgpt_health_check_retryable_error(retry_status_code, retry_error_text)
            ):
                if network_coordinator:
                    network_coordinator.report_failure(
                        account_id=account_id,
                        reason=f"批量测活连续网络错误: {retry_error_text[:160]}",
                        logger=logger,
                    )
                if proxy:
                    try:
                        proxy_pool.report_fail(proxy)
                    except Exception:
                        pass
                delay = min(1.5 * attempt, 5.0)
                if logger:
                    logger.log(
                        f"{account.email}: 测活网络错误第 {attempt}/{attempts} 次: {retry_error_text[:240]}，{delay:g}s 后重试",
                        level="warning",
                    )
                time.sleep(delay)
                continue
            break
        if not isinstance(state, dict):
            raise ValueError("账号状态/订阅响应格式异常")

        sanitized_state = _sanitize_chatgpt_state_for_health_result(state)
        if bool(state.get("valid")):
            result = {
                "account_id": account_id,
                "email": account.email,
                "valid": True,
                "status_code": 200,
                "plan_type": str(state.get("subscription_status") or ""),
                "account_state": sanitized_state,
            }
            if proxy:
                try:
                    proxy_pool.report_success(proxy)
                except Exception:
                    pass
            _persist_chatgpt_health_result(account_id, result)
            if logger:
                plan_text = str(state.get("subscription_status") or "unknown")
                logger.log(f"{account.email}: 测活存活（账号状态/订阅: {plan_text}）")
            return result

        state_error = _chatgpt_health_state_error(state)
        status_code = _chatgpt_health_status_code(state_error)
        error_text = _chatgpt_health_error_text(state_error) or "账号状态/订阅查询失败"
        relogin_required = _is_chatgpt_relogin_required_health_error(status_code, error_text)
        banned = _is_chatgpt_banned_health_error(status_code, error_text)
        invalid = status_code in {400, 404} or banned or "缺少 access_token" in error_text
        result = {
            "account_id": account_id,
            "email": account.email,
            "valid": False,
            "status_code": status_code,
            "error": error_text[:500],
            "transient": not invalid,
            "account_state": sanitized_state,
        }
        if relogin_required:
            result["relogin_required"] = True
        if banned:
            result["banned"] = True
        if proxy:
            try:
                proxy_pool.report_fail(proxy)
            except Exception:
                pass
        if invalid:
            _persist_chatgpt_health_result(account_id, result)
            if logger:
                suffix = f" HTTP {status_code}" if status_code else ""
                logger.log(f"{account.email}: 测活失效（账号状态/订阅{suffix}）", level="warning")
        elif relogin_required:
            _persist_chatgpt_health_result(account_id, result)
            if logger:
                logger.log(f"{account.email}: 测活需要重登验证（账号状态/订阅 HTTP {status_code}）", level="warning")
        elif logger:
            suffix = f" HTTP {status_code}" if status_code else ""
            logger.log(f"{account.email}: 测活错误（账号状态/订阅{suffix}），未改为失效", level="error")
        return result
    except Exception as exc:
        if proxy:
            try:
                proxy_pool.report_fail(proxy)
            except Exception:
                pass
        result = {
            "account_id": account_id,
            "email": account.email,
            "valid": False,
            "status_code": 0,
            "error": str(exc),
            "transient": True,
        }
        if logger:
            logger.log(f"{account.email}: 测活异常 {exc}", level="error")
        return result


def _persist_chatgpt_health_result(account_id: int, result: dict[str, Any]) -> None:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            return
        checked_at = _utcnow_iso()
        valid = bool(result.get("valid"))
        summary_updates: dict[str, Any] = {
            "checked_at": checked_at,
            "health_checked_at": checked_at,
            "health_status_code": int(result.get("status_code") or 0),
            "valid": valid,
        }
        lifecycle_status = None
        if valid:
            account_state = result.get("account_state") if isinstance(result.get("account_state"), dict) else {}
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            if not usage and isinstance(account_state.get("codex_usage"), dict):
                usage = account_state["codex_usage"]
            subscription_status = _resolve_chatgpt_subscription_status(result, account_state, usage)
            summary_updates.update(
                {
                    "validity_status": "valid",
                    "health_error": "",
                }
            )
            if account_state:
                summary_updates["chatgpt_account_state"] = account_state
                for key in (
                    "codex_usage",
                    "usage_breakdowns",
                    "prompt_remaining_percent",
                    "next_reset_at",
                    "codex_usage_error",
                    "codex_usage_probe",
                    "codex_usage_extra",
                    "remote_email",
                ):
                    if key in account_state:
                        summary_updates[key] = account_state[key]
            if usage:
                summary_updates["wham_usage"] = usage
                summary_updates["chatgpt_usage"] = usage
            elif isinstance(account_state.get("codex_usage"), dict):
                summary_updates["chatgpt_usage"] = account_state["codex_usage"]
            if subscription_status:
                summary_updates["subscription_status"] = subscription_status
                summary_updates["plan"] = subscription_status
                summary_updates["plan_name"] = subscription_status
                if subscription_status in {"plus", "team", "pro", "enterprise"}:
                    summary_updates["plan_state"] = "subscribed"
                elif subscription_status == "free":
                    summary_updates["plan_state"] = "free"
                elif subscription_status == "trial":
                    summary_updates["plan_state"] = "trial"
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            merged_graph = dict(current_graph)
            merged_overview = dict(merged_graph.get("overview") or {})
            merged_overview.update(summary_updates)
            merged_graph["overview"] = merged_overview
            lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
        else:
            account_state = result.get("account_state") if isinstance(result.get("account_state"), dict) else {}
            if result.get("relogin_required"):
                summary_updates.update(
                    {
                        "validity_status": "unknown",
                        "display_status": CHATGPT_RELOGIN_REQUIRED_STATUS,
                        "health_error": str(result.get("error") or ""),
                    }
                )
                lifecycle_status = CHATGPT_RELOGIN_REQUIRED_STATUS
            elif _is_chatgpt_banned_health_error(
                int(result.get("status_code") or 0),
                str(result.get("error") or ""),
            ):
                summary_updates.update(
                    {
                        "validity_status": "invalid",
                        "display_status": "banned",
                        "health_error": str(result.get("error") or ""),
                    }
                )
                lifecycle_status = "banned"
            else:
                summary_updates.update(
                    {
                        "validity_status": "invalid",
                        "display_status": AccountStatus.INVALID.value,
                        "health_error": str(result.get("error") or ""),
                    }
                )
                lifecycle_status = AccountStatus.INVALID.value
            if account_state:
                summary_updates["chatgpt_account_state"] = account_state
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates=summary_updates,
        )
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()


def execute_task(task_id: str) -> None:
    with Session(engine) as session:

        task = session.get(TaskModel, task_id)

        if not task:

            return

        task_type = task.type

        payload = task.get_payload()



    logger = TaskLogger(task_id)

    logger.mark_running()



    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")

        return



    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {

        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK: _execute_account_check_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        TASK_TYPE_ACCOUNT_HEALTH_CHECK: _execute_account_health_check_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
        TASK_TYPE_PHONE_BIND: _execute_phone_bind_task,

        TASK_TYPE_CODEX_OAUTH: _execute_codex_oauth_task,
        TASK_TYPE_MOMO_TRIAL_PROBE: _execute_momo_trial_probe_task,
        TASK_TYPE_GET_RT: _execute_get_rt_task,
        TASK_TYPE_GET_RT_BYPASS: _execute_get_rt_bypass_task,
        TASK_TYPE_REFRESH_SESSION: _execute_refresh_session_task,
        TASK_TYPE_AGENTS_UPLOAD_SUB2API: _execute_agents_upload_sub2api_task,
        TASK_TYPE_GOPAY_PAY_CHATGPT: _execute_gopay_pay_chatgpt_task,
        TASK_TYPE_GOPAY_REGISTER_ACCOUNT: _execute_gopay_register_account_task,

    }

    handler = handlers.get(task_type)

    if not handler:

        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")

        return

    handler(payload, logger)





def _resolve_sms_provider_for_task(extra: dict[str, Any]) -> tuple[str, dict[str, Any]]:

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    from infrastructure.provider_settings_repository import ProviderSettingsRepository



    settings_repo = ProviderSettingsRepository()

    definitions_repo = ProviderDefinitionsRepository()

    provider_key = str(

        extra.get("sms_provider")

        or extra.get("phone_provider")

        or settings_repo.get_default_provider_key("sms")

        or ""

    ).strip()

    if not provider_key:

        provider_key = "sms_activate" if extra.get("sms_activate_api_key") else ""

    definition = definitions_repo.get_by_key("sms", provider_key) if provider_key else None

    settings = settings_repo.resolve_runtime_settings("sms", provider_key, extra) if definition else dict(extra)

    return provider_key, settings





def _is_register_sms_provider_fallback_enabled(payload: dict[str, Any]) -> bool:
    if str(payload.get("platform") or "").strip().lower() != "chatgpt":
        return False
    extra = dict(payload.get("extra") or {})
    identity_provider = str(extra.get("identity_provider") or "").strip().lower()
    if identity_provider != "sms_oauth":
        return False
    # 当用户手动选择了 sms_provider 时，将其视为首选，
    # 仍然启用 fallback——首选失败后自动切换其他已启用的平台。
    return True


def _list_register_sms_provider_candidates(payload: dict[str, Any]) -> list[dict[str, str]]:
    if not _is_register_sms_provider_fallback_enabled(payload):
        return []
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository

        extra = dict(payload.get("extra") or {})
        preferred = str(extra.get("sms_provider") or extra.get("phone_provider") or "").strip()
        settings_repo = ProviderSettingsRepository()
        enabled_keys: list[str] = []
        seen: set[str] = set()
        for item in settings_repo.list_enabled("sms"):
            provider_key = str(getattr(item, "provider_key", "") or "").strip()
            if not provider_key or provider_key in seen:
                continue
            seen.add(provider_key)
            enabled_keys.append(provider_key)
        def _aliases(key: str) -> set[str]:
            normalized = str(key or "").strip().lower()
            if normalized in {"smspool", "smspool_api", "sms_pool", "sms_pool_api"}:
                return {"smspool", "smspool_api", "sms_pool", "sms_pool_api"}
            if normalized in {"smsbower", "smsbower_api", "smsbower.page", "smsbower_page"}:
                return {"smsbower", "smsbower_api", "smsbower.page", "smsbower_page"}
            return {normalized} if normalized else set()

        preferred_key = ""
        preferred_aliases = _aliases(preferred)
        if preferred_aliases:
            for key in enabled_keys:
                if _aliases(key).intersection(preferred_aliases):
                    preferred_key = key
                    break

        # Preferred provider goes first only when its enabled setting exists.
        ordered: list[str] = []
        if preferred_key and preferred_key not in ordered:
            ordered.append(preferred_key)
        for key in enabled_keys:
            if key not in ordered:
                ordered.append(key)
        return [{"provider": key} for key in ordered]
    except Exception:
        return []




def _is_register_sms_provider_switch_error(message: Any) -> bool:
    text = str(message or "").strip().lower()

    if not text:

        return False

    markers = (

        "voip_phone_disallowed",

        "virtual phone",

        "voip",

        "invalid phone number",

        "invalid_phone",

        "account_creation_failed",

        "failed to create account",

        "error creating your account",

        "unable to create account",

        "could not create account",

        "no_numbers",

        "no numbers",

        "no_balance",
        "no balance",
        "insufficient balance",
        "not enough balance",
        "could not find a suitable pool",
        "suitable pool below the price",
        "below the price of",
        "phone_first_provider_switch",
        "phone identity submit did not advance",
        "too many failed purchases",
        "try again in 6 hours",
        "purchase rate limit",
        "improve your success rate",
    )
    return any(marker in text for marker in markers)


def _summarize_register_sms_provider_exhausted_error(
    errors: list[str],
    candidates: list[dict[str, str]],
) -> str:
    if not errors:
        return "All enabled SMS providers failed during ChatGPT sms_oauth registration"
    providers = [
        str(item.get("provider") or "").strip()
        for item in candidates
        if str(item.get("provider") or "").strip()
    ]
    provider_text = ", ".join(providers) if providers else "enabled providers"
    first_error = str(errors[0] or "").strip()
    last_error = errors[-1]
    if first_error and first_error != last_error:
        return (
            "All enabled SMS providers failed during ChatGPT sms_oauth registration "
            f"({provider_text}); first error: {first_error}; last error: {last_error}"
        )
    return (
        "All enabled SMS providers failed during ChatGPT sms_oauth registration "
        f"({provider_text}); last error: {last_error}"
    )




def _normalize_get_rt_sms_provider(value: Any) -> str:

    """规范化 get_rt 的手机接码参数。



    旧前端可能传空字符串；为避免用户已设置默认 SMS 却未生效，空值按 default 处理。

    若要显式禁用手机接码，前端传 ``none``。

    """

    provider = str(value or "").strip().lower()

    if provider in {"none", "off", "disabled", "disable", "false", "0", "不启用"}:

        return ""

    if provider in {"smspool_api", "sms_pool", "sms_pool_api"}:

        return "smspool"

    if provider == "sms_api":

        return "smsapi"

    return provider or "default"





def _get_rt_sms_setting_candidates(provider: str) -> list[str]:

    normalized = _normalize_get_rt_sms_provider(provider)

    if normalized == "smspool":

        return ["smspool_api", "smspool", "sms_pool_api", "sms_pool"]

    if normalized == "smsapi":

        return ["smsapi", "sms_api"]

    return [normalized] if normalized else []





def _is_get_rt_balance_error(message: Any) -> bool:

    try:

        from platforms.gopay.sms_channel import is_smspool_insufficient_balance_response



        if is_smspool_insufficient_balance_response(message):

            return True

    except Exception:

        pass

    text = str(message or "").strip().lower()

    if not text:

        return False

    balance_markers = (

        "余额不足",

        "no_balance",

        "no balance",

        "insufficient balance",

        "insufficient funds",

        "not enough balance",

        "not enough funds",

        "low balance",

        "balance too low",

        "not enough credit",

        "not enough credits",

        "credit too low",

        "no funds",

        "not have enough",

        "balance error",

    )

    return any(marker in text for marker in balance_markers)





def _is_get_rt_sms_provider_switch_error(message: Any) -> bool:
    if _is_get_rt_balance_error(message):
        return True
    text = str(message or "").strip().lower()
    if not text:
        return False
    if "codex_sms_pool_exhausted" in text or "codex_sms_pool_blocked" in text:
        return True
    if (
        "could not find a suitable pool" in text
        or "suitable pool below the price" in text
        or "below the price of" in text
    ):
        return True
    if "too many phone verification requests" in text:
        return True
    if "fraud" in text or "suspicious behavior" in text:

        return True

    phone_submit_markers = (

        "add_phone",

        "add-phone",

        "\u624b\u673a\u53f7\u63d0\u4ea4",

        "\u624b\u673a\u53f7\u7801\u63d0\u4ea4",

    )

    phone_rate_markers = (

        "http 429",

        "status=429",

        "status 429",

        "-> 429",

        "rate_limit_exceeded",

        "rate limit",

        "rate-limit",

        "too many",

        "maximum",

        "exceeded",

    )

    if any(marker in text for marker in phone_submit_markers) and any(marker in text for marker in phone_rate_markers):

        return True

    switch_markers = (
        "too many failed purchases",
        "improve your success rate",
        "try again in 6 hours",
        "increased ratelimit",

        "increased rate limit",

        "purchase rate limit",

        "purchase ratelimit",

    )

    if any(marker in text for marker in switch_markers):

        return True

    provider_markers = (

        "smsbower",

        "smsbower.page",

        "hero-sms",

        "herosms",

        "sms-activate",

        "smspool",

        "5sim",

        "nexsms",

        "grizzlysms",

        "sms-verification-number",

        "handler_api.php",

        "stubs/handler_api",

        "action=getbalance",

        "action=getnumber",

        "action=getnumberv2",

    )

    network_markers = (

        "httpsconnectionpool",

        "httpconnectionpool",

        "max retries exceeded",

        "ssleoferror",

        "unexpected_eof",

        "eof occurred in violation of protocol",

        "ssl:",

        "connection aborted",

        "connection reset",

        "remote end closed connection",

        "failed to establish a new connection",

        "name resolution",

        "temporary failure",

        "tls",

    )

    return any(marker in text for marker in provider_markers) and any(marker in text for marker in network_markers)





def _is_get_rt_hard_retry_error(message: Any) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    markers = (

        "请求超时",

        "网络请求超时",

        "proxy connect aborted",

        "request timeout",

        "request timed out",

        "read timeout",

        "read timed out",

        "connect timeout",

        "connect timed out",

        "connection timeout",

        "net::err_timed_out",

        "network error",

    )
    return any(marker in text for marker in markers)


def _is_get_rt_email_login_cooldown_error(message: Any) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    markers = (
        "get_rt_email_login_cooldown",
        "too many tries",
        "please wait a few minutes",
        "too many attempts",
    )
    return any(marker in text for marker in markers)


def _is_get_rt_login_restart_error(message: Any) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    markers = (
        "get_rt_login_restart_required",
        "invalid_state",
        "sign-in session is no longer valid",
        "session is no longer valid",
        "please start over",
        "start over to continue",
    )
    return any(marker in text for marker in markers)


def _is_get_rt_target_recoverable_error(message: Any) -> bool:
    if _is_get_rt_sms_provider_switch_error(message):
        return False
    if _is_get_rt_email_login_cooldown_error(message):
        return False
    text = str(message or "").strip().lower()
    if not text:
        return False
    markers = (

        "http 429",

        "status=429",

        "status 429",

        "-> 429",

        "rate_limit_exceeded",
        "too many phone verification requests",
        "invalid_state",
        "get_rt_login_restart_required",
        "sign-in session is no longer valid",
        "session is no longer valid",
        "phone_rejected_retryable",
        "phone_rejected_retryable:",
        "\u5df2\u8fbe\u6362\u53f7\u4e0a\u9650",
        "phone_change_limit",
        "phone change limit",
        "\u83b7\u53d6\u624b\u673a\u53f7\u5931\u8d25",
        "phone otp \u83b7\u53d6\u5931\u8d25",
        "we couldn't send a text message",
        "we could not send a text message",
        "couldn't send a text",
        "could not send a text",
        "can't send a text",
        "cannot send a text",
        "unable to send a text",
        "switched to whatsapp",
        "continue to send a verification code on whatsapp",
        "token exchange failed: network error",
        "curl: (28)",
        "operation timed out",
        "request timeout",

        "request timed out",

        "read timeout",

        "read timed out",

        "connect timeout",

        "connect timed out",

        "connection timeout",

        "net::err_timed_out",

        "network error",

    )

    return any(marker in text for marker in markers)





def _is_get_rt_upload_success(upload_ok: Any) -> bool:

    return upload_ok is True





def _is_sub2api_configured() -> bool:

    try:

        from core.config_store import config_store



        return bool(str(config_store.get("sub2api_url", "") or "").strip())

    except Exception:

        return False





def _list_get_rt_sms_provider_candidates(payload: dict[str, Any], sms_runtime: dict[str, str]) -> list[dict[str, Any]]:

    from infrastructure.provider_settings_repository import ProviderSettingsRepository



    raw_requested = str(payload.get("sms_provider") or "").strip().lower()

    if raw_requested in {"none", "off", "disabled", "disable", "false", "0", "\u4e0d\u542f\u7528"}:

        return []



    settings_repo = ProviderSettingsRepository()

    candidates: list[dict[str, Any]] = []

    seen: set[str] = set()



    def add(provider_key: str, *, source: str) -> None:
        normalized = _normalize_get_rt_sms_provider(provider_key)
        if not normalized or normalized in seen:
            return
        settings_key = ""
        runtime_settings: dict[str, Any] = {}
        if normalized == "smspool":
            settings_key = "smspool_api"
            item = settings_repo.get_by_key("sms", settings_key)
            if not item or not bool(getattr(item, "enabled", True)):
                return
            runtime_settings = settings_repo.resolve_runtime_settings("sms", settings_key, {})
        elif normalized == "smsapi":
            settings_key = "smsapi"
            item = settings_repo.get_by_key("sms", settings_key)
            if not item or not bool(getattr(item, "enabled", True)):
                return
            runtime_settings = settings_repo.resolve_runtime_settings("sms", settings_key, {})
        else:

            runtime_setting = settings_repo.get_by_key("sms", provider_key)

            if not runtime_setting or not bool(getattr(runtime_setting, "enabled", True)):

                return

            settings_key = str(runtime_setting.provider_key or "").strip()

            runtime_settings = settings_repo.resolve_runtime_settings("sms", settings_key, {})

        seen.add(normalized)

        candidates.append(

            {

                "provider": normalized,

                "settings_provider_key": settings_key,

                "source": source,

                "sms_runtime": {

                    "sms_provider": normalized,

                    "smspool_api_key": _first_nonempty_text(

                        payload.get("smspool_api_key"),

                        runtime_settings.get("smspool_api_key"),

                        runtime_settings.get("api_key"),

                        runtime_settings.get("smsPoolApiKey"),

                    ),

                    "smspool_max_price": _first_nonempty_text(

                        payload.get("smspool_max_price"),

                        runtime_settings.get("smspool_max_price"),

                        "0.13",

                    ),

                    "smspool_country": _first_nonempty_text(

                        payload.get("smspool_country"),

                        payload.get("smspool_default_country"),

                        runtime_settings.get("smspool_country"),

                        runtime_settings.get("smspool_default_country"),

                        runtime_settings.get("smsPoolCountry"),

                    ),

                    "smspool_service": _first_nonempty_text(

                        payload.get("smspool_service"),

                        payload.get("smspool_default_service"),

                        runtime_settings.get("smspool_service"),

                        runtime_settings.get("smspool_default_service"),

                        runtime_settings.get("smsPoolServiceCode"),

                    ),

                    "smspool_base_url": _first_nonempty_text(

                        payload.get("smspool_base_url"),

                        runtime_settings.get("smspool_base_url"),

                    ),

                    "smspool_compat_base_url": _first_nonempty_text(

                        payload.get("smspool_compat_base_url"),

                        runtime_settings.get("smspool_compat_base_url"),

                    ),

                    "smspool_pricing_option": _first_nonempty_text(

                        payload.get("smspool_pricing_option"),

                        runtime_settings.get("smspool_pricing_option"),

                    ),

                    "smspool_poll_interval": _first_nonempty_text(

                        payload.get("smspool_poll_interval"),

                        runtime_settings.get("sms_poll_interval"),

                        runtime_settings.get("poll_interval"),

                    ),

                    "smsapi_phone": _first_nonempty_text(

                        payload.get("smsapi_phone"),

                        runtime_settings.get("smsapi_phone"),

                    ),

                    "smsapi_url": _first_nonempty_text(

                        payload.get("smsapi_url"),

                        runtime_settings.get("smsapi_url"),

                    ),

                    "settings_provider_key": settings_key,

                },

            }

        )



    requested = _normalize_get_rt_sms_provider(payload.get("sms_provider"))

    if requested and requested not in {"default", "default_sms", "__default__"}:

        add(requested, source="payload")



    default_provider = _normalize_get_rt_sms_provider(sms_runtime.get("settings_provider_key") or "")

    if default_provider:

        add(default_provider, source="saved-default")



    for item in settings_repo.list_enabled("sms"):

        add(str(item.provider_key or ""), source="enabled")



    return candidates





def _first_nonempty_text(*values: Any) -> str:

    for value in values:

        text = str(value or "").strip()

        if text:

            return text

    return ""





def _resolve_get_rt_sms_runtime_config(payload: dict[str, Any]) -> dict[str, str]:

    """Resolve get_rt SMS config from task payload plus saved provider settings."""

    provider = _normalize_get_rt_sms_provider(payload.get("sms_provider"))

    settings: dict[str, Any] = {}

    settings_provider_key = ""



    if provider:

        try:

            from infrastructure.provider_settings_repository import ProviderSettingsRepository



            settings_repo = ProviderSettingsRepository()

            if provider in {"default", "default_sms", "__default__"}:

                settings_provider_key = str(settings_repo.get_default_provider_key("sms") or "").strip()

                provider = _normalize_get_rt_sms_provider(settings_provider_key)

            else:
                for candidate in _get_rt_sms_setting_candidates(provider):
                    item = settings_repo.get_by_key("sms", candidate)
                    if item and bool(getattr(item, "enabled", True)):
                        settings_provider_key = candidate
                        break
                if not settings_provider_key:
                    provider = ""
            if settings_provider_key:
                settings = settings_repo.resolve_runtime_settings("sms", settings_provider_key, {})
        except Exception:
            settings = {}


    smspool_key = _first_nonempty_text(

        payload.get("smspool_api_key"),

        settings.get("smspool_api_key"),

        settings.get("api_key"),

        settings.get("smsPoolApiKey"),

    )

    smspool_max_price = _first_nonempty_text(

        payload.get("smspool_max_price"),

        settings.get("smspool_max_price"),

        "0.13",

    )

    smspool_country = _first_nonempty_text(

        payload.get("smspool_country"),

        payload.get("smspool_default_country"),

        settings.get("smspool_country"),

        settings.get("smspool_default_country"),

        settings.get("smsPoolCountry"),

    )

    smspool_service = _first_nonempty_text(

        payload.get("smspool_service"),

        payload.get("smspool_default_service"),

        settings.get("smspool_service"),

        settings.get("smspool_default_service"),

        settings.get("smsPoolServiceCode"),

    )

    smspool_base_url = _first_nonempty_text(

        payload.get("smspool_base_url"),

        settings.get("smspool_base_url"),

    )

    smspool_compat_base_url = _first_nonempty_text(

        payload.get("smspool_compat_base_url"),

        settings.get("smspool_compat_base_url"),

    )

    smspool_pricing_option = _first_nonempty_text(

        payload.get("smspool_pricing_option"),

        settings.get("smspool_pricing_option"),

    )

    smspool_poll_interval = _first_nonempty_text(

        payload.get("smspool_poll_interval"),

        settings.get("sms_poll_interval"),

        settings.get("poll_interval"),

    )

    return {

        "sms_provider": provider,

        "smspool_api_key": smspool_key,

        "smspool_max_price": smspool_max_price,

        "smspool_country": smspool_country,

        "smspool_service": smspool_service,

        "smspool_base_url": smspool_base_url,

        "smspool_compat_base_url": smspool_compat_base_url,

        "smspool_pricing_option": smspool_pricing_option,

        "smspool_poll_interval": smspool_poll_interval,

        "smsapi_phone": str(payload.get("smsapi_phone") or "").strip(),

        "smsapi_url": str(payload.get("smsapi_url") or "").strip(),

        "settings_provider_key": settings_provider_key,

    }





def _bool_config(value: Any, default: bool) -> bool:

    if value in (None, ""):

        return default

    if isinstance(value, bool):

        return value

    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}





def _int_config(value: Any, default: int) -> int:

    try:

        return int(value)

    except (TypeError, ValueError):

        return default





def _resolve_registration_proxy_for_platform(

    platform_name: str,

    *,

    explicit_proxy: str | None,

    proxy_getter: Callable[[], str | None],

) -> str | None:

    from core.proxy_pool import resolve_runtime_proxy



    # 平台注册统一走全局代理策略：代理池空时可回退到用户配置的本地代理。

    return resolve_runtime_proxy(explicit_proxy=explicit_proxy, proxy_getter=proxy_getter)





def _mask_proxy_for_log(proxy: str | None) -> str:

    value = str(proxy or "").strip()

    if not value:

        return "direct"

    try:

        from urllib.parse import urlsplit



        parsed = urlsplit(value)

        if parsed.hostname:

            port = f":{parsed.port}" if parsed.port else ""

            scheme = f"{parsed.scheme}://" if parsed.scheme else ""

            return f"{scheme}***@{parsed.hostname}{port}"

    except Exception:

        pass

    if "@" in value:

        prefix, host = value.rsplit("@", 1)

        scheme = prefix.split("://", 1)[0] + "://" if "://" in prefix else ""

        return f"{scheme}***@{host}"

    return value



_CHATGPT_PROXY_PREFLIGHT_DETAIL_CACHE: dict[str, str] = {}


def _chatgpt_proxy_cache_key(proxy: str | None) -> str:
    return str(proxy or "").strip()


def _parse_cloudflare_trace_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in str(text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip().lower()] = value.strip()
    return fields


def _format_chatgpt_proxy_preflight_detail(status_code: int, route: str, trace_text: str = "") -> str:
    fields = _parse_cloudflare_trace_fields(trace_text)
    parts = [route]
    loc = fields.get("loc", "")
    ip = fields.get("ip", "")
    if loc:
        parts.append(f"地区={loc}")
    if ip:
        parts.append(f"出口IP={ip}")
    return f"HTTP {status_code} ({', '.join(parts)})"


def _chatgpt_proxy_detail_for_log(proxy: str | None) -> str:
    detail = _CHATGPT_PROXY_PREFLIGHT_DETAIL_CACHE.get(_chatgpt_proxy_cache_key(proxy), "")
    if not detail:
        return ""
    inside = ""
    if "(" in detail and detail.endswith(")"):
        inside = detail.rsplit("(", 1)[1][:-1]
    if not inside:
        return detail
    return inside




def _chatgpt_proxy_preflight(proxy: str | None, *, timeout: int = 12) -> tuple[bool, str]:
    if not proxy:
        return True, "direct"
    try:
        from core.http_client import RequestConfig
        from platforms.chatgpt.http_client import OpenAIHTTPClient

        client = OpenAIHTTPClient(
            proxy_url=proxy,
            config=RequestConfig(
                timeout=timeout,
                max_retries=2,
                impersonate="chrome120",
            ),
        )
        try:
            response = client.get("https://chatgpt.com/cdn-cgi/trace", timeout=timeout)
            route = "本地中转" if client.config.proxy_upstream_url else "目标代理直连"
            detail = _format_chatgpt_proxy_preflight_detail(
                int(getattr(response, "status_code", 0) or 0),
                route,
                str(getattr(response, "text", "") or ""),
            )
            _CHATGPT_PROXY_PREFLIGHT_DETAIL_CACHE[_chatgpt_proxy_cache_key(proxy)] = detail
            if response.status_code >= 500:
                return False, detail
            return True, detail
        finally:
            client.close()
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {str(exc)[:180]}"

def _is_chatgpt_proxy_preflight_transient_error(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "failed to perform",
            "curl:",
            "tls connect error",
            "openssl_internal",
            "invalid library",
            "sslerror",
            "ssl error",
            "proxy connect",
            "connection reset",
            "connection refused",
            "connection closed",
            "couldn't connect",
            "timed out",
            "timeout",
            "name resolution",
            "dns",
            "empty reply",
        )
    )


def _refresh_chatgpt_local_proxy_node(
    *,
    reason: str,
    logger: "TaskLogger" | None,
    extra: dict[str, Any] | None = None,
) -> bool:
    try:
        from core.proxy_providers import refresh_local_proxy_node
        from core.proxy_pool import get_proxy_runtime_config

        result = refresh_local_proxy_node(
            reason=reason,
            proxy_url=get_proxy_runtime_config().get("upstream_url", ""),
            extra=extra,
        )
    except Exception as exc:
        if logger:
            logger.log(f"本地 Clash 节点自动切换失败: {exc}", level="warning")
        return False

    if not result.get("ok"):
        if logger:
            logger.log(
                f"本地 Clash 节点自动切换失败: {result.get('error') or 'unknown'}",
                level="warning",
            )
        return False

    if logger and not result.get("reused_recent"):
        selected = str(result.get("selected_node") or "").strip()
        previous = str(result.get("previous_node") or "").strip()
        if selected and previous and selected != previous:
            logger.log(f"已自动切换本地 Clash 节点: {previous} -> {selected}")
        elif selected:
            logger.log(f"已自动切换本地 Clash 节点: {selected}")
        else:
            logger.log("已自动刷新本地 Clash 代理节点")
    return True




def _resolve_chatgpt_reachable_proxy(
    *,

    platform_name: str,

    explicit_proxy: str | None,

    proxy_getter: Callable[[], str | None],

    logger: "TaskLogger",

    max_attempts: int = 6,

    continue_on_transient_failure: bool = True,

) -> str | None:

    resolved = _resolve_registration_proxy_for_platform(

        platform_name,

        explicit_proxy=explicit_proxy,

        proxy_getter=proxy_getter,

    )

    if platform_name != "chatgpt" or not resolved:

        return resolved



    explicit = bool(str(explicit_proxy or "").strip())

    attempts = max_attempts
    seen: set[str] = set()

    last_detail = ""
    retry_same_resolved = False
    transient_refresh_counts: dict[str, int] = {}
    for attempt in range(1, attempts + 1):

        if not resolved:

            break

        if not explicit and resolved in seen and not retry_same_resolved:
            resolved = _resolve_registration_proxy_for_platform(

                platform_name,

                explicit_proxy=None,

                proxy_getter=proxy_getter,

            )

            continue

        retry_same_resolved = False
        seen.add(resolved)
        ok, detail = _chatgpt_proxy_preflight(resolved)

        if ok:

            if attempt > 1:

                logger.log(

                    f"\u5df2\u5207\u6362\u5230\u53ef\u8bbf\u95ee ChatGPT \u7684\u4ee3\u7406: {_mask_proxy_for_log(resolved)}"

                )

            return resolved



        last_detail = detail

        if _is_chatgpt_proxy_preflight_transient_error(detail):
            refresh_limit = 3 if (explicit or continue_on_transient_failure) else 1
            refreshed_count = transient_refresh_counts.get(resolved, 0)
            if (
                attempt < attempts
                and refreshed_count < refresh_limit
                and _refresh_chatgpt_local_proxy_node(
                reason=f"ChatGPT 代理预检网络异常: {detail[:160]}",
                logger=logger,
                )
            ):
                transient_refresh_counts[resolved] = refreshed_count + 1
                logger.log(
                    "ChatGPT 代理预检遇到传输/TLS异常，已切换本地 Clash 节点后重试: "
                    f"{_mask_proxy_for_log(resolved)}: {detail}",
                    level="warning",
                )
                retry_same_resolved = True
                continue
            if not explicit and not continue_on_transient_failure:
                logger.log(
                    "ChatGPT 协议注册代理预检遇到传输/TLS异常，"
                    f"将跳过该代理并换下一条: {_mask_proxy_for_log(resolved)}: {detail}",
                    level="warning",
                )
                try:
                    from core.proxy_pool import proxy_pool as _proxy_pool

                    _proxy_pool.report_fail(resolved)
                except Exception:
                    pass
                if attempt >= attempts:
                    resolved = None
                    continue
                resolved = _resolve_registration_proxy_for_platform(

                    platform_name,

                    explicit_proxy=None,

                    proxy_getter=proxy_getter,

                )
                retry_same_resolved = False
                continue
            logger.log(
                "ChatGPT 代理预检遇到传输/TLS异常，无法确认代理不可用，"
                f"将继续使用浏览器真实流程: {_mask_proxy_for_log(resolved)}: {detail}",
                level="warning",
            )
            return resolved
        logger.log(

            f"ChatGPT \u4ee3\u7406\u9884\u68c0\u5931\u8d25\uff0c\u5c06\u8df3\u8fc7 {_mask_proxy_for_log(resolved)}: {detail}",

            level="warning",

        )

        try:

            from core.proxy_pool import proxy_pool as _proxy_pool



            _proxy_pool.report_fail(resolved)

        except Exception:

            pass

        if str(explicit_proxy or "").strip():

            break

        resolved = _resolve_registration_proxy_for_platform(

            platform_name,

            explicit_proxy=None,

            proxy_getter=proxy_getter,

        )



    if resolved:
        logger.log(
            "ChatGPT \u4ee3\u7406\u9884\u68c0\u672a\u80fd\u786e\u8ba4\u53ef\u8bbf\u95ee\uff0c"
            f"\u5c06\u4f7f\u7528\u6d4f\u89c8\u5668\u771f\u5b9e\u6d41\u7a0b\u7ee7\u7eed: {_mask_proxy_for_log(resolved)}"
            + (f" ({last_detail})" if last_detail else ""),
            level="warning",
        )
        return resolved
    raise RuntimeError(
        "\u6ca1\u6709\u627e\u5230\u53ef\u8bbf\u95ee ChatGPT \u7684\u4ee3\u7406"
        + (f": {last_detail}" if last_detail else "")
    )




def _auto_followup_windsurf_payment(

    *,

    platform_name: str,

    payload: dict[str, Any],

    platform,

    account,

    logger: "TaskLogger",

) -> None:

    if platform_name != "windsurf":

        return

    executor_type = str(payload.get("executor_type", "") or "").strip()

    use_browser = executor_type in {"headless", "headed"}

    if not use_browser:

        extra_cfg = dict(payload.get("extra") or {})

        if not _bool_config(extra_cfg.get("auto_payment_link"), True):

            return

    if not str(getattr(account, "password", "") or "").strip() and use_browser:

        logger.log("Windsurf 注册后自动升级已跳过: 账号缺少密码", level="error")

        return

    extra = dict(payload.get("extra") or {})

    turnstile_token = str(extra.get("turnstile_token") or "").strip()

    if use_browser:

        action_id = "payment_link_browser"

        params = {

            "timeout": _int_config(extra.get("windsurf_payment_timeout"), 240),

            "headless": "true" if _bool_config(extra.get("windsurf_payment_headless"), False) else "false",

            "payment_channel": "checkout",

        }

        if turnstile_token:

            params["turnstile_token"] = turnstile_token

    else:

        action_id = "payment_link"

        params = {}

        if turnstile_token:

            params["turnstile_token"] = turnstile_token

    logger.log("注册成功，开始自动生成 Windsurf Pro Trial Stripe 链接")

    try:

        result = platform.execute_action(action_id, account, params)

    except Exception as exc:

        message = f"Windsurf 注册后自动升级失败: {exc}"

        logger.record_error(message)

        logger.log(message, level="error")

        return

    if not result.get("ok"):

        message = f"Windsurf 注册后自动升级失败: {result.get('error') or 'unknown error'}"

        logger.record_error(message)

        logger.log(message, level="error")

        return

    data = dict(result.get("data") or {})

    if data:

        merged_extra = dict(getattr(account, "extra", {}) or {})

        merged_extra.update(data)

        account.extra = merged_extra

        save_account(account)

    cashier_url = str(data.get("cashier_url") or data.get("url") or "").strip()

    if cashier_url:

        logger.log(f"Windsurf 自动升级链接已生成: {cashier_url}")

        logger.add_cashier_url(cashier_url)





def _shortlink_payment_enabled(payload: dict[str, Any]) -> bool:

    """CtfGptPlus 注册任务是否开了短链物理复用付款。"""

    extra = dict(payload.get("extra") or {})

    if not _bool_config(extra.get("auto_chatgpt_plus_payment"), False):

        return False

    payment_cfg = dict(extra.get("chatgpt_payment") or {})

    raw = payment_cfg.get("use_short_link")

    return raw is True or str(raw or "").strip().lower() in ("1", "true", "yes", "on")





def _build_inbrowser_shortlink_checkout(

    *,

    payload: dict[str, Any],

    logger: "TaskLogger",

    proxy: str | None,

    sms_pool_override: str = "",

):

    """构造短链物理复用回调：注册完、浏览器还开着时，在**同一 page** 上生成

    短链 → 打开 → 跑 PayPal checkout。返回 ``post_register_in_browser(page,

    session_info) -> dict`` 给 ChatGPTBrowserRegister。



    付款参数从 ``extra.chatgpt_payment`` 取（country/currency/超时/captcha/

    sms_pool 等），跟 ``_auto_followup_chatgpt_plus_payment`` 一套来源，只是

    改成在已存在 page 上跑（不另开浏览器）。

    """

    from platforms.chatgpt import payment as payment_module



    extra = dict(payload.get("extra") or {})

    payment_cfg = dict(extra.get("chatgpt_payment") or {})

    country = str(payment_cfg.get("country") or "ID").strip() or "ID"

    currency = str(payment_cfg.get("currency") or "IDR").strip() or "IDR"

    checkout_timeout = _int_config(payment_cfg.get("checkout_timeout"), 180)

    address_region = str(payment_cfg.get("address_region") or "US").strip().upper() or "US"

    use_captcha = _bool_config(payment_cfg.get("use_captcha_service"), True)

    sms_pool_raw = sms_pool_override or str(payment_cfg.get("sms_pool") or "")

    sms_pool = []

    try:

        if sms_pool_raw.strip():

            sms_pool = payment_module.parse_sms_pool(sms_pool_raw)

    except Exception:

        sms_pool = []



    def _post_register(page, session_info: dict) -> dict:

        class _A:

            pass

        a = _A()

        a.access_token = str(session_info.get("access_token") or "")

        a.cookies = str(session_info.get("cookies") or "")

        if not a.access_token:

            logger.log("短链复用(PayPal)：注册结果没有 access_token，无法生成短链")

            return {"_shortlink_checkout": {"ok": False, "error": "no access_token"}}

        short_url = payment_module.generate_plus_link(

            a, proxy=None, country=country, currency=currency, use_short_link=True,

        )

        logger.log(f"短链已生成（PayPal 同浏览器复用）: {short_url[:70]}…")

        # turnstile solver：默认按设置走（这里简化为 None，captcha 用页面点击 +

        # 等待；要接 YesCaptcha 可在此按 use_captcha 注入 solver）。

        turnstile_solver = None

        res = payment_module.complete_paypal_checkout(

            checkout_url=short_url,

            cookies_str=a.cookies,

            proxy=None,

            email=str(session_info.get("email") or ""),

            payment_method="paypal",

            timeout=checkout_timeout,

            log_fn=logger.log,

            cancel_check=logger.is_cancel_requested,

            turnstile_solver=turnstile_solver,

            sms_pool=sms_pool or None,

            address_region=address_region,

            existing_page=page,  # 物理复用：在注册浏览器同一 page 上跑

        )

        return {"_shortlink_checkout": res}



    return _post_register





def _auto_followup_chatgpt_plus_payment(

    *,

    platform_name: str,

    payload: dict[str, Any],

    platform,

    account,

    logger: "TaskLogger",

    sms_pool_override: str = "",

    phone_swap_callback: Optional[Callable[[str], Optional[dict]]] = None,

) -> str:

    if platform_name != "chatgpt":

        return ""

    extra = dict(payload.get("extra") or {})

    if not _bool_config(extra.get("auto_chatgpt_plus_payment"), False):

        return ""



    payment_cfg = dict(extra.get("chatgpt_payment") or {})

    params: dict[str, Any] = {

        "plan": "plus",

        "country": str(payment_cfg.get("country") or "ID").strip() or "ID",

        "currency": str(payment_cfg.get("currency") or "IDR").strip() or "IDR",

        "auto_checkout": str(payment_cfg.get("auto_checkout", "true")).lower(),

        "payment_method": str(payment_cfg.get("payment_method") or "paypal").strip().lower() or "paypal",

        "headless": str(payment_cfg.get("headless", "false")).lower(),

        "checkout_timeout": _int_config(payment_cfg.get("checkout_timeout"), 180),

    }

    # 账单地址来源（meiguodizhi 接口分路）："US" / "JP"。空 / 非法值 plugin 层会

    # fallback 到 US，这里只做格式化透传。

    if payment_cfg.get("address_region") not in (None, ""):

        params["address_region"] = str(payment_cfg.get("address_region") or "").strip().upper()

    if payment_cfg.get("checkout_hold_seconds") not in (None, ""):

        params["checkout_hold_seconds"] = _int_config(payment_cfg.get("checkout_hold_seconds"), 10)

    if payment_cfg.get("proxy_region") not in (None, ""):

        params["proxy_region"] = str(payment_cfg.get("proxy_region") or "").strip().upper()

    if payment_cfg.get("checkout_mode") not in (None, ""):

        params["checkout_mode"] = str(payment_cfg.get("checkout_mode") or "").strip().lower()

    # Stripe 协议长链开关（accessToken → pay.openai.com，纯协议生成 cashier_url）

    if payment_cfg.get("use_stripe_init") not in (None, ""):

        params["use_stripe_init"] = str(payment_cfg.get("use_stripe_init")).strip().lower()

    # 短链开关（checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc 短链）

    if payment_cfg.get("use_short_link") not in (None, ""):

        params["use_short_link"] = str(payment_cfg.get("use_short_link")).strip().lower()

    # bitbrowser_* 模式下需要 BitBrowser 客户端里手工建好的 profile ID

    # （见 platforms/_browser_backend.py BrowserBackendConfig.bitbrowser）。

    # 留空时插件层会回退到 BIT_PROFILE_ID 环境变量。

    if payment_cfg.get("bit_profile_id") not in (None, ""):

        params["bit_profile_id"] = str(payment_cfg.get("bit_profile_id") or "").strip()

    if payment_cfg.get("record_har") not in (None, ""):

        params["record_har"] = str(payment_cfg.get("record_har")).strip().lower()

    # 是否启用 YesCaptcha 求解；缺省 / 空 视为 true。"false" 时插件层会把

    # turnstile_solver 强制置 None，captcha 路径退化为"鼠标点击 + 10s 等待"。

    if payment_cfg.get("use_captcha_service") not in (None, ""):

        params["use_captcha_service"] = str(

            payment_cfg.get("use_captcha_service")

        ).strip().lower()

    # SMS 号码池：调用方（``_execute_register_task._do_one``）在并发槽里

    # acquire 了一条号字符串后通过 ``sms_pool_override`` 传进来，这里直接当

    # ``sms_pool`` 透传给 plugin。下游 ``parse_sms_pool`` 仍按原 textarea

    # 路径解析，但只看到一条号，不会跨线程偷其它槽的号。

    # 没传 override 时退化到原行为（把 textarea 全量传下去），保持兼容

    # 单测 / 老调用路径。

    if sms_pool_override:

        params["sms_pool"] = sms_pool_override

    elif payment_cfg.get("sms_pool") not in (None, ""):

        params["sms_pool"] = str(payment_cfg.get("sms_pool") or "")

    for key, value in payment_cfg.items():

        key_text = str(key)

        if key_text.startswith("ppboom_") or key_text in {"use_ppboom", "ppboom_enabled"}:

            params[key_text] = value

    # 透传 phone swap callback —— Camoufox checkout 在 PayPal 拒号时会

    # 回调换一条全局空闲号继续。callback 由 ``_execute_register_task``

    # 持有 slot_queue 的闭包构造。

    if callable(phone_swap_callback):

        params["phone_swap_callback"] = phone_swap_callback



    logger.log("注册成功，开始自动生成 ChatGPT Plus 测试支付链接")

    try:

        result = platform.execute_action("payment_link", account, params)

    except Exception as exc:

        return f"ChatGPT Plus 支付链接生成失败: {exc}"



    data = dict(result.get("data") or {})

    cashier_url = str(data.get("cashier_url") or data.get("checkout_url") or data.get("url") or "").strip()

    open_url = str(

        data.get("paypal_authorize_url")

        or data.get("checkout_url")

        or data.get("url")

        or cashier_url

        or ""

    ).strip()

    protocol_extract = data.get("paypal_protocol_extract")

    action_ok = bool(result.get("ok"))

    subscription_submitted = _bool_config(data.get("subscription_submitted"), True)

    if data or action_ok:

        merged_extra = dict(getattr(account, "extra", {}) or {})

        merged_extra.update(data)

        if cashier_url:

            merged_extra["cashier_url"] = cashier_url

        if action_ok and subscription_submitted:

            overview = dict(merged_extra.get("account_overview") or {})

            chips = [

                str(item)

                for item in (overview.get("chips") or [])

                if str(item or "").strip()

            ]

            if "Plus" not in chips:

                chips.append("Plus")

            overview.update(

                {

                    "plan_state": "subscribed",

                    "plan_name": "Plus",

                    "plan": "plus",

                    "membership_type": "plus",

                    "lifecycle_status": AccountStatus.SUBSCRIBED.value,

                    "chips": chips,

                }

            )

            if cashier_url:

                overview["cashier_url"] = cashier_url

            merged_extra["account_overview"] = overview

            account.status = AccountStatus.SUBSCRIBED

        account.extra = merged_extra

        save_account(account)

        logger.set_result_data({

            "account_email": getattr(account, "email", ""),

            "payment": data,

        })

    if open_url and (action_ok or not protocol_extract):

        logger.log(f"ChatGPT Plus 测试支付链接已生成: {open_url}")

        if cashier_url and cashier_url != open_url:

            logger.log(f"原始 cashier_url: {cashier_url}")

        logger.add_cashier_url(open_url)



    if not result.get("ok"):

        return f"ChatGPT Plus 支付链接生成失败: {result.get('error') or 'unknown error'}"

    return ""





def _format_register_task_average_duration(elapsed_seconds: float, completed_count: int) -> str:
    elapsed = max(float(elapsed_seconds or 0), 0.0)
    completed = max(int(completed_count or 0), 0)
    if completed <= 0:
        return ""
    return f"平均耗时: {elapsed / completed:.1f} 秒/任务（总耗时 {elapsed:.1f} 秒，已处理 {completed} 个）"


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    register_started_at = time.monotonic()

    from core.proxy_pool import get_proxy_runtime_config, proxy_pool



    count = max(int(payload.get("count", 1) or 1), 1)

    concurrency = min(max(int(payload.get("concurrency", 1) or 1), 1), count, 5)

    platform_name = str(payload.get("platform", ""))

    email = payload.get("email") or None

    password = payload.get("password") or None

    proxy = payload.get("proxy") or None

    extra = dict(payload.get("extra") or {})

    manual_post_register_capture = (
        platform_name == "chatgpt"
        and str(payload.get("executor_type", "") or "").strip().lower() == "headed"
        and (
            _bool_config(extra.get("set_password_after_register"), False)
            or _bool_config(extra.get("enable_2fa_after_register"), False)
        )
    )
    if manual_post_register_capture:
        _clear_manual_post_register_capture_signal(logger.task_id)
        extra["record_har"] = True
        extra["_manual_post_register_security_capture"] = True
        extra["_task_id"] = logger.task_id
        extra["_manual_post_register_capture_finish_path"] = str(
            _manual_post_register_capture_signal_path(logger.task_id)
        )
        payload = dict(payload)
        payload["extra"] = extra
        if concurrency > 1:
            logger.log("手动后置安全抓包模式需要逐个操作，注册并发已调整为 1", level="warning")
            concurrency = 1
        logger.log("有头浏览器后置安全抓包模式已启用：注册成功后会保持浏览器和 HAR 录制")



    if platform_name == "chatgpt" and _bool_config(

        extra.get("auto_chatgpt_plus_payment"), False

    ):

        payment_cfg_for_start = dict(extra.get("chatgpt_payment") or {})

        if _bool_config(payment_cfg_for_start.get("use_ppboom"), False) or _bool_config(

            payment_cfg_for_start.get("ppboom_enabled"), False

        ):

            try:

                from application.ppboom import (

                    DEFAULT_PPBOOM_BASE_URL,

                    ensure_ppboom_service,

                )



                ppboom_base_url = (

                    str(payment_cfg_for_start.get("ppboom_base_url") or "").strip()

                    or DEFAULT_PPBOOM_BASE_URL

                ).rstrip("/")

                logger.log(f"PPBoom: ensuring helper service at {ppboom_base_url}")

                ensure_ppboom_service(ppboom_base_url, log_fn=logger.log)

            except Exception as exc:

                msg = f"PPBoom helper startup failed: {exc}"

                logger.log(msg, level="error")

                logger.finish(TASK_STATUS_FAILED, error=msg)

                return



    # 强校验：ChatGPT Plus 自动支付链接 + sms_pool 模式下，**每个并发线程

    # 独占一条 SMS 号**——所以数量约束是 ``len(pool) >= concurrency``，**不是**

    # ``>= count``（注册数量）。多个 batch 跑下来，每个并发槽会被复用，但同

    # 一时刻同一条号只在一个线程里跑，不会错乱。

    sms_pool_slots: list[str] = []  # 启动后每个 slot 一条号字符串（"+phone----url"）

    sms_pool_extras: list[dict] = []  # 备份池：当某线程被 PayPal 拒号时换号用

    sms_pool_lock = threading.Lock()  # 保护 extras 的并发读取

    # 当某线程触发 swap 但 extras 为空时置 set —— 整个任务级别立刻停止投新任务，

    # 让正在跑的任务自然失败结束，避免下一批又抢同一条死号继续被拒。

    sms_pool_exhausted = threading.Event()

    if platform_name == "chatgpt" and _bool_config(

        extra.get("auto_chatgpt_plus_payment"), False

    ):

        payment_cfg = dict(extra.get("chatgpt_payment") or {})

        sms_pool_raw = str(payment_cfg.get("sms_pool") or "")

        if sms_pool_raw.strip():

            from platforms.chatgpt import payment as _chatgpt_payment_module

            try:

                parsed_pool = _chatgpt_payment_module.parse_sms_pool(sms_pool_raw)

            except Exception as exc:

                msg = f"SMS 号码池解析失败: {exc}"

                logger.log(msg, level="error")

                logger.finish(TASK_STATUS_FAILED, error=msg)

                return

            if len(parsed_pool) < concurrency:

                msg = (

                    f"SMS 号码池数量不足：并发数 {concurrency}，号码池仅 "

                    f"{len(parsed_pool)} 条。每个并发线程必须独占一条号，"

                    f"请在 SMS 号码池里至少填 {concurrency} 条 +phone----relay_url。"

                )

                logger.log(msg, level="error")

                logger.finish(TASK_STATUS_FAILED, error=msg)

                return

            # 前 concurrency 条作为初始并发槽；其余作为 extras 备份池——

            # 当某线程的号被 PayPal 拒后从 extras 换一条继续，extras 用完了

            # 就让该线程结束失败（前端会显示"号码不可用"）。

            sms_pool_slots = [

                (

                    f"{entry.get('phone_e164') or '+' + str(entry.get('phone', ''))}"

                    f"----{entry.get('relay_url', '')}"

                )

                for entry in parsed_pool[:concurrency]

            ]

            sms_pool_extras = list(parsed_pool[concurrency:])

            logger.log(

                f"SMS 号码池校验通过：{len(parsed_pool)} 条 ≥ 并发数 {concurrency}，"

                f"前 {concurrency} 条作并发槽，剩余 {len(sms_pool_extras)} 条作"

                "拒号换号备份池"

            )

    # 并发槽 → SMS 号映射：用 queue 让每个并发任务 acquire/release 一个槽位，

    # 同一时刻一个槽位只被一个线程占用，跑完归还供下一批复用。

    sms_slot_queue: "queue.Queue[int]" = queue.Queue()

    for slot_index in range(len(sms_pool_slots)):

        sms_slot_queue.put(slot_index)

    sms_provider_key, sms_settings = _resolve_sms_provider_for_task(extra)

    register_sms_candidates = _list_register_sms_provider_candidates(payload)
    register_sms_lock = threading.Lock()
    register_sms_exhausted: set[str] = set()
    if register_sms_candidates:

        logger.log(
            "ChatGPT register SMS provider fallback enabled: "
            + " -> ".join(str(item.get("provider") or "") for item in register_sms_candidates)
        )
    herosms_enabled = sms_provider_key == "herosms" and bool(str(sms_settings.get("herosms_api_key") or "").strip())

    hero_extra_max = max(_int_config(sms_settings.get("register_phone_extra_max"), 3), 0) if herosms_enabled else 0

    hero_reuse_to_max = _bool_config(sms_settings.get("register_reuse_phone_to_max"), True) if herosms_enabled else False

    target_success = count

    max_success = count + hero_extra_max if herosms_enabled and hero_reuse_to_max else count

    progress_total = max_success if herosms_enabled else count

    registration_base_proxy = _resolve_registration_proxy_for_platform(
        platform_name,
        explicit_proxy=proxy,
        proxy_getter=lambda: None,
    )

    prepared_registration_proxies: list[str] = []
    prepared_registration_proxy_index = 0
    prepared_registration_proxy_lock = threading.Lock()
    prepared_registration_proxy_slots: "queue.Queue[int]" = queue.Queue()
    if not str(proxy or "").strip():
        try:
            from core.proxy_providers import prepare_dynamic_proxy_for_task

            prepared_registration_proxies = prepare_dynamic_proxy_for_task(concurrency, extra=extra)
            if prepared_registration_proxies:
                for proxy_slot_index in range(len(prepared_registration_proxies)):
                    prepared_registration_proxy_slots.put(proxy_slot_index)
                logger.log(
                    "动态代理已按并发预热："
                    f"{len(prepared_registration_proxies)} 个独立入口"
                )
        except Exception as exc:
            logger.log(f"动态代理任务级准备失败: {exc}", level="error")
            logger.finish(TASK_STATUS_FAILED, error=f"动态代理任务级准备失败: {exc}")
            return

    registration_proxy_runtime_config = get_proxy_runtime_config()
    registration_proxy_lease_enabled = (
        concurrency > 1
        and not str(proxy or "").strip()
        and not prepared_registration_proxies
        and str(registration_proxy_runtime_config.get("strategy") or "") in {
            "pool_then_default",
            "pool_only",
        }
    )
    if registration_proxy_lease_enabled:
        logger.log("注册并发已启用代理池任务级租约：同一时间一个代理只分配给一个注册 worker")
    elif (
        concurrency > 1
        and not str(proxy or "").strip()
        and not prepared_registration_proxies
        and _is_local_proxy_url(registration_base_proxy)
    ):
        logger.log(
            "注册并发检测到当前只会使用同一个本地/默认代理入口，"
            f"为避免多个注册 worker 共享同一出口，并发从 {concurrency} 降为 1",
            level="warning",
        )
        concurrency = 1

    def _registration_proxy_getter() -> str | None:
        nonlocal prepared_registration_proxy_index
        if prepared_registration_proxies:
            with prepared_registration_proxy_lock:
                proxy_value = prepared_registration_proxies[
                    prepared_registration_proxy_index % len(prepared_registration_proxies)
                ]
                prepared_registration_proxy_index += 1
                return proxy_value
        return proxy_pool.get_next()

    logger.set_progress(0, progress_total)
    if herosms_enabled:

        logger.log(

            f"HeroSMS 模式: 成功目标 {target_success}，失败自动补尝试，"

            f"号码仍可复用时最多额外成功 {hero_extra_max} 个"

        )



    try:

        get(platform_name)

    except Exception as exc:

        logger.log(f"致命错误: {exc}", level="error")

        logger.finish(TASK_STATUS_FAILED, error=str(exc))

        return



    success = 0
    errors: list[str] = []
    k12_deferred_sub2api_paths: list[str] = []
    k12_deferred_sub2api_lock = threading.Lock()


    # Pre-create a shared mailbox instance for the entire task to avoid

    # concurrent initialization issues (e.g. MoeMail auto-registering

    # multiple provider accounts simultaneously).

    shared_mailbox = None

    try:

        from core.base_identity import normalize_identity_provider

        from core.base_mailbox import create_mailbox



        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))

        if identity_provider in {"mailbox", "sms_oauth"}:

            if not extra.get("mail_provider"):

                from infrastructure.provider_settings_repository import ProviderSettingsRepository

                extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")

            shared_mailbox = create_mailbox(
                provider=extra.get("mail_provider", ""),
                extra=extra,
                proxy=None,
            )
            shared_mailbox = _maybe_wrap_email_alias_mailbox(
                shared_mailbox,
                platform_name=platform_name,
                extra=extra,
                logger=logger,
            )
    except Exception as exc:

        logger.log(f"邮箱初始化失败: {exc}", level="error")

        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")

        return



    def _do_one(index: int) -> bool | str:
        if logger.is_cancel_requested():
            return "__cancel_requested__"
        proxy_slot_id: int | None = None
        proxy_slot_value = ""
        leased_registration_proxy = ""
        leased_registration_proxy_seen: set[str] = set()
        if prepared_registration_proxies:
            proxy_slot_id = prepared_registration_proxy_slots.get()
            proxy_slot_value = prepared_registration_proxies[proxy_slot_id]

        def _current_registration_proxy_getter() -> str | None:
            nonlocal leased_registration_proxy
            if proxy_slot_id is not None:
                return proxy_slot_value
            if registration_proxy_lease_enabled:
                if leased_registration_proxy:
                    leased_registration_proxy_seen.add(leased_registration_proxy)
                    proxy_pool.release_lease(leased_registration_proxy)
                    leased_registration_proxy = ""
                leased = proxy_pool.lease_next(exclude=leased_registration_proxy_seen)
                if not leased:
                    raise RuntimeError(
                        "代理池可租用代理不足，已停止当前账号任务以避免并发共享同一代理"
                    )
                leased_registration_proxy = leased
                return leased
            return _registration_proxy_getter()

        # 占用一个 SMS 槽位（如果配了 sms_pool_slots）。每个并发线程独占
        # 一条号；跑完归还供下一批任务复用。slot_queue 大小 = concurrency，

        # 启动前已校验过；这里只在配了池时阻塞 acquire。

        sms_slot_id: int | None = None

        sms_slot_value: str = ""

        if sms_pool_slots:

            sms_slot_id = sms_slot_queue.get()

            sms_slot_value = sms_pool_slots[sms_slot_id]

            logger.log(

                f"任务 #{index + 1} 占用 SMS 槽 {sms_slot_id + 1}/{len(sms_pool_slots)}: "

                f"{sms_slot_value.split('----', 1)[0]}"

            )

        # 给当前线程绑定 subtask 标签——后续所有 ``logger.log`` 都自动带上

        # ``subtask_id``，前端按这个分组折叠展示。优先用 SMS 槽 ID 做稳定

        # subtask（同一号一直在同一组）；没号池就退化到注册序号。

        if sms_slot_id is not None:

            subtask_id = f"worker_{sms_slot_id + 1}"

            subtask_label = (

                f"Worker {sms_slot_id + 1} ({sms_slot_value.split('----', 1)[0]})"

            )

        else:

            subtask_id = f"task_{index + 1}"

            subtask_label = f"账号 #{index + 1}"

        logger.set_subtask(subtask_id, subtask_label)



        # 构造 swap callback：当 checkout 中途 PayPal 拒号时，从 extras 备份池里

        # 取一条新号继续；同时把当前线程的当前号"标坏"（即不再放回 slot_queue

        # 让下个任务用），并把新号作为当前线程后续可能再次被拒时的回退基础。

        # callback 返回 None 表示备份池空 → 当前线程任务失败、前端可识别为

        # "号码不可用"。

        slot_state = {

            "slot_value": sms_slot_value,

            "swapped_or_dead": False,  # 标记当前 slot 是死号，finally 不归还

        }



        def _swap_phone(rejected_e164: str) -> Optional[dict]:

            with sms_pool_lock:

                if not sms_pool_extras:

                    # 备份池空：把当前 slot 标为死号 + 全局通知"池耗尽"，

                    # 防止 finally 误把这条死号归还、调度层再投新任务又抢

                    # 到这条号继续被拒。

                    slot_state["swapped_or_dead"] = True

                    sms_pool_exhausted.set()

                    return None

                next_entry = sms_pool_extras.pop(0)

            phone_e164 = str(next_entry.get("phone_e164") or "").strip()

            relay_url = str(next_entry.get("relay_url") or "").strip()

            if not (phone_e164 and relay_url):

                slot_state["swapped_or_dead"] = True

                sms_pool_exhausted.set()

                return None

            new_value = f"{phone_e164}----{relay_url}"

            slot_state["slot_value"] = new_value

            slot_state["swapped_or_dead"] = True

            # 更新 subtask label，让前端分组里"号码"信息也跟着换

            label_idx = sms_slot_id + 1 if sms_slot_id is not None else index + 1

            logger.set_subtask(subtask_id, f"Worker {label_idx} ({phone_e164})")

            logger.log(

                f"任务 #{index + 1} 切换 SMS 号到备份池：{phone_e164}（剩余备份 {len(sms_pool_extras)} 条）"

            )

            return next_entry



        try:
            resolved_proxy = _resolve_chatgpt_reachable_proxy(
                platform_name=platform_name,
                explicit_proxy=proxy,
                proxy_getter=_current_registration_proxy_getter,
                logger=logger,
                continue_on_transient_failure=(
                    str(payload.get("executor_type", "protocol") or "protocol").strip().lower()
                    in {"headless", "headed"}
                    or _shortlink_payment_enabled(payload)
                ),
            )
        except Exception as exc:
            if leased_registration_proxy:
                proxy_pool.release_lease(leased_registration_proxy)
                leased_registration_proxy = ""
            if proxy_slot_id is not None:
                prepared_registration_proxy_slots.put(proxy_slot_id)
            if sms_slot_id is not None:
                sms_slot_queue.put(sms_slot_id)
            logger.clear_subtask()
            error = str(exc)
            logger.record_error(error)
            logger.log(f"✗ 注册失败: {error}", level="error")
            _save_task_log(platform_name, email or "", "failed", error=error)
            return error
        # 短链物理复用（CtfGptPlus / PayPal）：注册和打开短链必须同一浏览器。

        # 把 post_register 回调 + backend_config 注入 config.extra，让注册器在

        # 注册完、浏览器还开着时，在同一 page 上生成短链 → 跑 PayPal checkout。

        _shortlink_reuse = (

            platform_name == "chatgpt" and _shortlink_payment_enabled(payload)

        )

        _build_payload = payload

        _sl_acquired_profile = ""

        if _shortlink_reuse:

            from platforms._browser_backend import parse_checkout_mode

            _pcfg = dict((payload.get("extra") or {}).get("chatgpt_payment") or {})

            _ckmode = str(_pcfg.get("checkout_mode") or "camoufox_headed").strip().lower()

            if _ckmode == "protocol":

                _ckmode = "camoufox_headed"  # 短链复用必须用浏览器

            # BitBrowser 模式：从池里 acquire 一个 profile（跟正常 PayPal 流程

            # 一致），否则 backend_config 缺 profile_id 会直接报错。

            _sl_bit_profile = str(_pcfg.get("bit_profile_id") or "")

            if _ckmode.startswith("bitbrowser"):

                from application.bitbrowser_profiles import acquire_profile_for_browser_mode

                _sl_bit_profile, _sl_acquired_profile = acquire_profile_for_browser_mode(

                    _ckmode, fallback=_sl_bit_profile, log_fn=logger.log,

                )

                if not _sl_bit_profile:

                    logger.log(

                        "短链复用：BitBrowser 池为空且未配 bit_profile_id，"

                        "回退 Camoufox 前台",

                        level="error",

                    )

                    _ckmode = "camoufox_headed"

            _cb = _build_inbrowser_shortlink_checkout(

                payload=payload, logger=logger, proxy=resolved_proxy,

                sms_pool_override=slot_state["slot_value"] or sms_slot_value,

            )

            _reuse_extra = dict(payload.get("extra") or {})

            _reuse_extra["_reuse_backend_config"] = parse_checkout_mode(

                _ckmode, bit_profile_id=_sl_bit_profile,

            )

            _reuse_extra["_post_register_in_browser"] = _cb

            _build_payload = dict(payload)

            _build_payload["extra"] = _reuse_extra

            # **关键**：短链物理复用必须走浏览器注册（headed/headless），

            # 否则 base_platform.register 会走 ProtocolMailboxFlow（协议邮箱

            # 注册，根本不开浏览器，post_register_in_browser 回调永远不触发）。

            # 从付款 checkout_mode 推导注册 executor：headless 模式→headless，

            # 其余（含 bitbrowser_*/camoufox_headed）→headed。

            _reuse_executor = "headless" if _ckmode.endswith("_headless") else "headed"

            _build_payload["executor_type"] = _reuse_executor

            logger.log(

                f"短链物理复用：注册+打开短链+PayPal 付款将在同一浏览器"

                f"（注册执行器={_reuse_executor}, 浏览器={_ckmode}）里完成"

            )

        current_register_sms_provider = ""
        current_register_sms_index = -1
        if register_sms_candidates:
            with register_sms_lock:
                available_candidates = [
                    (candidate_index, candidate)
                    for candidate_index, candidate in enumerate(register_sms_candidates)
                    if str(candidate.get("provider") or "").strip() not in register_sms_exhausted
                ]
            if not available_candidates:
                raise RuntimeError(
                    _summarize_register_sms_provider_exhausted_error(
                        errors,
                        register_sms_candidates,
                    )
                )
            available_position = min(index // max(count, 1), len(available_candidates) - 1)
            current_register_sms_index, candidate = available_candidates[available_position]
            candidate_provider = str(candidate.get("provider") or "").strip()
            if candidate_provider:
                current_register_sms_provider = candidate_provider
                candidate_payload = dict(_build_payload)
                candidate_extra = dict(candidate_payload.get("extra") or {})
                candidate_extra["sms_provider"] = candidate_provider
                candidate_payload["extra"] = candidate_extra
                _build_payload = candidate_payload
                logger.log(
                    f"ChatGPT register: using SMS provider={candidate_provider} "
                    f"({current_register_sms_index + 1}/{len(register_sms_candidates)})"
                )
        try:

            platform = _build_platform_instance(platform_name, _build_payload, logger, resolved_proxy=resolved_proxy, shared_mailbox=shared_mailbox)

            # 失败不计进度的模式（chatgpt_plus_must_succeed）下 index 可能 > count，

            # 显示成"已成功 X/N，本次为第 M 次尝试"更直观。

            if chatgpt_plus_must_succeed or bugfree_mode_enabled:
                logger.log(

                    f"开始注册账号（已成功 {success}/{count}，本次第 {index + 1} 次尝试）"

                )

            else:

                logger.log(f"开始注册第 {index + 1}/{count} 个账号")

            if resolved_proxy:

                proxy_detail = _chatgpt_proxy_detail_for_log(resolved_proxy)
                if proxy_detail:
                    logger.log(f"使用代理: {resolved_proxy}（{proxy_detail}）")
                else:
                    logger.log(f"使用代理: {resolved_proxy}")

            account = platform.register(email=email, password=password)
            if platform_name == "chatgpt":
                _auto_enable_chatgpt_2fa_after_register(
                    account,
                    logger,
                    proxy=resolved_proxy,
                    enable=_bool_config((dict(_build_payload.get("extra") or {})).get("enable_2fa_after_register"), False),
                    require_password_set=_bool_config((dict(_build_payload.get("extra") or {})).get("set_password_after_register"), False),
                )

            saved_model = save_account(account)
            _mark_outlook_mailbox_event(shared_mailbox, account, "registration_success", logger)
            saved_account_id = _saved_account_id(saved_model, account)
            _schedule_chatgpt_trial_post_register_check(
                account=account,
                saved_account_id=saved_account_id,
                logger=logger,
                proxy=resolved_proxy,
            )

            if bugfree_mode_enabled:
                if not _run_bugfree_post_register_check(
                    account=account,
                    saved_account_id=saved_account_id,
                    logger=logger,
                    proxy=resolved_proxy,
                ):
                    return BUGFREE_SKIP_RESULT
            _auto_followup_windsurf_payment(

                platform_name=platform_name,

                payload=payload,

                platform=platform,

                account=account,

                logger=logger,

            )

            if _shortlink_reuse:

                # 短链复用：PayPal checkout 已在注册浏览器里跑完，结果挂在

                # account.extra["_shortlink_checkout"]（由注册器回调合并进

                # registration_state/result，再由 _map_chatgpt_result 透传）。

                # 这里直接判定，不再调 _auto_followup（那会另开浏览器）。

                _sl_res = {}

                try:

                    _sl_res = dict((getattr(account, "extra", {}) or {}).get("_shortlink_checkout") or {})

                except Exception:

                    _sl_res = {}

                if _sl_res and not _sl_res.get("ok"):

                    chatgpt_plus_error = f"短链复用 PayPal 付款失败: {_sl_res.get('error') or _sl_res.get('status') or 'unknown'}"

                    logger.record_error(chatgpt_plus_error)

                    logger.log(chatgpt_plus_error, level="error")

                    _save_task_log(platform_name, account.email, "failed", error=chatgpt_plus_error)

                    return chatgpt_plus_error

                logger.log("短链复用 PayPal 付款完成（同一浏览器）")

                return True

            chatgpt_plus_error = _auto_followup_chatgpt_plus_payment(

                platform_name=platform_name,

                payload=payload,

                platform=platform,

                account=account,

                logger=logger,

                sms_pool_override=slot_state["slot_value"] or sms_slot_value,

                phone_swap_callback=_swap_phone if sms_pool_slots else None,

            )

            if chatgpt_plus_error:

                logger.record_error(chatgpt_plus_error)

                logger.log(chatgpt_plus_error, level="error")

                _save_task_log(platform_name, account.email, "failed", error=chatgpt_plus_error)

                # SMS 号池耗尽错误（payment.py 抛 SMS_POOL_EXHAUSTED:）→

                # 整个任务级别停止投新任务（兜底，正常路径已经在 _swap_phone

                # 里 set 过；这里覆盖那种 payment 内部直接 raise 没经 callback

                # 的边角情况）。

                if _is_global_sms_pool_exhausted_error(chatgpt_plus_error):

                    sms_pool_exhausted.set()

                elif _is_current_sms_phone_exhausted_error(chatgpt_plus_error):

                    slot_state["swapped_or_dead"] = True

                return chatgpt_plus_error

            chatgpt_plus_enabled = (

                platform_name == "chatgpt"

                and _bool_config(extra.get("auto_chatgpt_plus_payment"), False)

            )

            if chatgpt_plus_enabled:

                _mark_outlook_mailbox_event(shared_mailbox, account, "plus_success", logger)

            agent_identity_auth_json_mode_enabled = (
                platform_name == "chatgpt"
                and _bool_config(extra.get("agent_identity_auth_json_mode"), False)
            )
            if agent_identity_auth_json_mode_enabled:
                upload_ok, upload_message = _run_agent_identity_auth_json_post_register_upload(account, logger)
                if not upload_ok:
                    _mark_agent_identity_auth_json_upload_status(
                        saved_account_id,
                        uploaded=False,
                        upload_message=upload_message,
                    )
                    error_message = f"Agent Identity auth.json 上传到 Sub2Api 失败: {upload_message}"
                    logger.record_error(error_message)
                    logger.log(error_message, level="error")
                    _save_task_log(platform_name, account.email, "failed", error=error_message)
                    return error_message
                _mark_agent_identity_auth_json_upload_status(
                    saved_account_id,
                    uploaded=True,
                    upload_message=upload_message,
                )
            if resolved_proxy:

                proxy_pool.report_success(resolved_proxy)

            logger.record_success()
            logger.log(f"✓ 注册成功: {account.email}")
            _save_task_log(platform_name, account.email, "success")
            account_extra = dict(account.extra or {})
            collected_k12_paths = _collect_k12_deferred_sub2api_paths(account)
            if collected_k12_paths:
                with k12_deferred_sub2api_lock:
                    k12_deferred_sub2api_paths.extend(collected_k12_paths)
            if agent_identity_auth_json_mode_enabled:
                logger.log("  [Agent Identity] 已按 auth.json 模式上传，跳过普通 session/CPA 上传")
            elif _bool_config(extra.get("remote_upload_enabled"), False):
                if collected_k12_paths:
                    logger.log("  [K12] 已保存待统一上传的 SUB2API JSON，跳过当前账号即时远端上传")
                elif _bool_config(account_extra.get("k12_remote_upload_handled"), False):
                    logger.log("  [K12] SUB2API 远端上传已在 K12 流程中处理，跳过通用远端上传")
                else:
                    _auto_upload_cpa(logger, account)
            else:
                _save_local_upload_jsons(logger, account)
            _auto_push_any2api(logger, account)
            overview = dict(account_extra.get("account_overview") or {})
            cashier_url = str(account_extra.get("cashier_url") or overview.get("cashier_url") or "")

            if cashier_url:

                logger.log(f"  [升级链接] {cashier_url}")

                logger.add_cashier_url(cashier_url)

            return True

        except Exception as exc:

            if resolved_proxy:
                proxy_pool.report_fail(resolved_proxy)
            error = str(exc)
            if email_alias_retry_enabled and _is_email_alias_parent_exhausted_error(error):
                logger.log("父邮箱别名配额已耗尽，正在切换新父邮箱继续当前注册")
                return EMAIL_ALIAS_PARENT_RETRY_RESULT
            if email_alias_retry_enabled and _is_email_alias_unavailable_parent_error(error):
                logger.log("邮箱别名父邮箱不可用或已下架，正在切换新父邮箱继续当前注册")
                return EMAIL_ALIAS_PARENT_RETRY_RESULT
            if email_alias_retry_enabled and _is_email_alias_temporary_pool_error(error):
                logger.log("邮箱别名父邮箱池当前都在使用中，等待释放后继续当前注册")
                time.sleep(3)
                return EMAIL_ALIAS_PARENT_RETRY_RESULT

            if _is_smsbower_mail_otp_timeout_error(error):
                _release_smsbower_mailbox_after_otp_timeout(platform, shared_mailbox, logger, error)
                logger.log(
                    "SMSBower 邮箱验证码超时，准备重新获取新邮箱并重走注册流程",
                    level="warning",
                )
                return SMSBOWER_MAIL_OTP_RETRY_RESULT

            logger.record_error(error)
            logger.log(f"✗ 注册失败: {error}", level="error")

            if current_register_sms_provider and _is_register_sms_provider_switch_error(error):
                with register_sms_lock:
                    register_sms_exhausted.add(current_register_sms_provider)
                    remaining_sms_providers = [
                        str(item.get("provider") or "").strip()
                        for item in register_sms_candidates
                        if str(item.get("provider") or "").strip()
                        and str(item.get("provider") or "").strip() not in register_sms_exhausted
                    ]
                if remaining_sms_providers:
                    logger.log(
                        "ChatGPT register: SMS provider exhausted, "
                        f"provider={current_register_sms_provider}, "
                        f"next={' -> '.join(remaining_sms_providers)}"
                    )
                else:
                    logger.log(
                        "ChatGPT register: all enabled SMS providers exhausted "
                        f"after provider={current_register_sms_provider}",
                        level="error",
                    )

            _save_task_log(platform_name, email or "", "failed", error=error)
            return error

        finally:

            # 归还 SMS 槽位：``swapped_or_dead`` 为 True 表示原号在跑过程中被

            # PayPal 拒了（不论备份池有没有补到新号），原号永久标坏，**不能**

            # 再放回 slot_queue 让下一个任务复用——否则下一个任务又抢到死号

            # 继续被拒。备份池还有就用备份号补位 slot；备份池也空就丢弃 slot。

            # 没换号（``swapped_or_dead`` False）→ 原号没被拒，正常归还。

            if sms_slot_id is not None:

                with sms_pool_lock:

                    if not slot_state["swapped_or_dead"]:

                        sms_slot_queue.put(sms_slot_id)

                    elif sms_pool_extras:

                        next_entry = sms_pool_extras.pop(0)

                        phone_e164 = str(next_entry.get("phone_e164") or "").strip()

                        relay_url = str(next_entry.get("relay_url") or "").strip()

                        if phone_e164 and relay_url:

                            sms_pool_slots[sms_slot_id] = (

                                f"{phone_e164}----{relay_url}"

                            )

                            sms_slot_queue.put(sms_slot_id)

                            logger.log(

                                f"SMS 槽 {sms_slot_id + 1} 用过备份号补位为 "

                                f"{phone_e164}（剩余备份 {len(sms_pool_extras)} 条）"

                            )

                        else:

                            sms_pool_exhausted.set()

                    else:

                        sms_pool_exhausted.set()

            if proxy_slot_id is not None:
                try:
                    from core.proxy_providers import refresh_dynamic_proxy

                    refreshed_proxy = refresh_dynamic_proxy(proxy_slot_value, extra=extra)
                    if refreshed_proxy:
                        prepared_registration_proxies[proxy_slot_id] = refreshed_proxy
                except Exception as exc:
                    logger.log(f"动态代理槽 {proxy_slot_id + 1} 刷新失败，继续复用原入口: {exc}", level="warning")
                finally:
                    prepared_registration_proxy_slots.put(proxy_slot_id)

            if leased_registration_proxy:
                proxy_pool.release_lease(leased_registration_proxy)

            # 解除 thread-local subtask 绑定，避免 ThreadPool 复用线程时
            # 把上一个任务的标签泄露到下一个任务。

            logger.clear_subtask()

            # 短链复用从 BitBrowser 池 acquire 的 profile，跑完归还计数。

            if _sl_acquired_profile:

                try:

                    from application.bitbrowser_profiles import release_acquired_profile

                    release_acquired_profile(_sl_acquired_profile, log_fn=logger.log)

                except Exception:

                    pass



    try:

        submitted = 0

        completed = 0
        email_alias_retry_count = 0
        smsbower_mail_otp_retry_count = 0
        futures: dict[Any, int] = {}

        # ChatGPT Plus 自动支付链接场景：用户诉求"设置生成 N 个必须生成 N 个

        # 成功"——失败的账号进入 gpt 账户池但**不增加进度**，调度继续投新任务

        # 直到 success 达到 count。最多投 ``count * 5`` 次防止号池烂掉时无限

        # 循环。其它平台 / 不开自动支付 → 退化为原"投 count 次就停"语义。

        chatgpt_plus_must_succeed = (

            platform_name == "chatgpt"

            and _bool_config(extra.get("auto_chatgpt_plus_payment"), False)

        )

        bugfree_mode_enabled = (
            platform_name == "chatgpt"
            and _bool_config(extra.get("bugfree_mode"), False)
        )

        email_alias_retry_enabled = _bool_config(
            extra.get("enable_email_alias", extra.get("email_alias_enabled")),
            False,
        )

        if bugfree_mode_enabled:
            max_attempts = max(
                count * 10,
                count * (EMAIL_ALIAS_PARENT_RETRY_LIMIT_PER_ACCOUNT + 1)
                if email_alias_retry_enabled
                else count,
                1,
            )
        elif chatgpt_plus_must_succeed:
            max_attempts = max(
                count * 5,
                count * (EMAIL_ALIAS_PARENT_RETRY_LIMIT_PER_ACCOUNT + 1)
                if email_alias_retry_enabled
                else count,
                1,
            )
        elif register_sms_candidates:

            max_attempts = max(
                count * len(register_sms_candidates),
                count * (EMAIL_ALIAS_PARENT_RETRY_LIMIT_PER_ACCOUNT + 1)
                if email_alias_retry_enabled
                else count,
                1,
            )
        else:

            max_attempts = max(

                count if not herosms_enabled else max_success * 3, 1
            )



        if email_alias_retry_enabled and not (bugfree_mode_enabled or chatgpt_plus_must_succeed or register_sms_candidates):
            max_attempts += count * EMAIL_ALIAS_PARENT_RETRY_LIMIT_PER_ACCOUNT
        if platform_name == "chatgpt":
            max_attempts = max(
                max_attempts,
                count * (SMSBOWER_MAIL_OTP_RETRY_LIMIT_PER_ACCOUNT + 1),
            )

        def _hero_phone_alive() -> bool:
            if not (herosms_enabled and hero_reuse_to_max):

                return False

            try:

                from core.base_sms import is_herosms_phone_cache_alive

                alive, info = is_herosms_phone_cache_alive(sms_settings)

                if alive:

                    logger.log(

                        "HeroSMS 号码仍可复用: "

                        f"{str(info.get('phone_number') or '')[:5]}**** "

                        f"剩余 {int(info.get('remaining_seconds') or 0)} 秒，"

                        f"已成功 {int(info.get('use_count') or 0)} 次"

                    )

                return bool(alive)

            except Exception:

                return False



        def _should_submit_more() -> bool:

            if submitted >= max_attempts or logger.is_cancel_requested():

                return False

            # SMS 号池被耗尽（某条号被拒 + 备份池空）→ 整个任务级别停止

            # 投新任务，让正在跑的任务跑完后退出。否则下一批又抢同一条死号

            # 继续被拒（用户实战日志 "开始注册第 2/1 个账号" 即此场景）。

            if sms_pool_exhausted.is_set():

                return False

            # 如果配了 sms_pool_slots，slot_queue 实际可用 + 在跑数 < 待补的

            # success 缺口才能再投。slot 全死光了（chatgpt_plus_must_succeed

            # 模式下号码池+备份池全被 PayPal 拒）就不再投，避免 _do_one 的

            # ``sms_slot_queue.get()`` 永久阻塞。

            if sms_pool_slots:

                # qsize 是近似的（多线程下不严格），但作为"全死光"判定够用

                if sms_slot_queue.qsize() == 0 and len(futures) >= concurrency:

                    return False

            if chatgpt_plus_must_succeed or bugfree_mode_enabled:
                # 必须达到 count 个 success；失败不计 progress，继续投。
                # 已成功 + 在跑的 ≥ count 时不再投（避免超额）。
                return success + len(futures) < count
            if register_sms_candidates:
                with register_sms_lock:
                    available_sms_provider_count = sum(
                        1
                        for item in register_sms_candidates
                        if str(item.get("provider") or "").strip()
                        and str(item.get("provider") or "").strip() not in register_sms_exhausted
                    )
                if available_sms_provider_count <= 0:
                    return False
                if success + len(futures) >= count:
                    return False
                return submitted < max_attempts
            if not herosms_enabled:

                if email_alias_retry_enabled or smsbower_mail_otp_retry_count:
                    allowed_attempts = min(
                        max_attempts,
                        count + email_alias_retry_count + smsbower_mail_otp_retry_count,
                    )
                    return success + len(futures) < count and submitted < allowed_attempts
                return submitted < count
            if success + len(futures) >= max_success:

                return False

            if success < target_success:

                return True

            if success >= max_success:

                return False

            return _hero_phone_alive()



        with ThreadPoolExecutor(max_workers=concurrency) as pool:

            while _should_submit_more() and len(futures) < concurrency:

                futures[pool.submit(_do_one, submitted)] = submitted

                submitted += 1



            while futures:

                done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)

                for future in done:

                    futures.pop(future, None)

                    result = future.result()

                    if result == EMAIL_ALIAS_PARENT_RETRY_RESULT:
                        email_alias_retry_count += 1
                        continue

                    if result == SMSBOWER_MAIL_OTP_RETRY_RESULT:
                        retry_limit = count * SMSBOWER_MAIL_OTP_RETRY_LIMIT_PER_ACCOUNT
                        if smsbower_mail_otp_retry_count < retry_limit:
                            smsbower_mail_otp_retry_count += 1
                            logger.log(
                                "SMSBower 邮箱验证码超时换邮箱重试 "
                                f"({smsbower_mail_otp_retry_count}/{retry_limit})"
                            )
                            continue
                        errors.append(
                            "SMSBower 邮箱验证码超时，已释放当前邮箱并达到换邮箱重试上限"
                        )
                        completed += 1
                        continue

                    completed += 1
                    if result is True:

                        success += 1

                    elif result == BUGFREE_SKIP_RESULT:

                        pass

                    elif result != "__cancel_requested__":
                        errors.append(str(result))

                    logger.set_progress(

                        min(

                            success

                            if (herosms_enabled or chatgpt_plus_must_succeed or bugfree_mode_enabled)
                            else completed,

                            progress_total,

                        ),

                        progress_total,

                    )

                while _should_submit_more() and len(futures) < concurrency:

                    futures[pool.submit(_do_one, submitted)] = submitted

                    submitted += 1

                if logger.is_cancel_requested() and not futures:

                    break

    except Exception as exc:

        logger.log(f"致命错误: {exc}", level="error")

        logger.finish(TASK_STATUS_FAILED, error=str(exc))

        return



    if herosms_enabled:

        logger.set_result_data({

            "target_count": target_success,

            "attempts": submitted,

            "success": success,

            "fail": len(errors),

            "extra_success": max(0, success - target_success),

            "hero_sms_reuse": True,

        })

    if (
        email_alias_retry_enabled
        and email_alias_retry_count > 0
        and success < target_success
        and not errors
        and submitted >= max_attempts
        and not logger.is_cancel_requested()
    ):
        errors.append("Email alias quota exhausted: no available parent mailbox after retry")

    if (
        bugfree_mode_enabled
        and success < count
        and not errors
        and submitted >= max_attempts
        and not logger.is_cancel_requested()
    ):
        errors.append(f"BUGFREE模式未找到足够账号：目标 {count} 个，已确认 {success} 个，已尝试 {submitted} 个")

    if success < target_success and not errors and not logger.is_cancel_requested():
        errors.append(
            f"注册未达到目标：目标 {target_success} 个，成功 {success} 个，已尝试 {submitted} 个"
        )

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    _finalize_k12_deferred_sub2api_uploads(k12_deferred_sub2api_paths, logger)

    summary = f"完成: 成功 {success} 个, 失败 {len(errors)} 个"
    logger.log(summary, event_type="summary")
    average_duration = _format_register_task_average_duration(
        time.monotonic() - register_started_at,
        success + len(errors),
    )
    if average_duration:
        logger.log(average_duration, event_type="summary")
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    if final_status == TASK_STATUS_SUCCEEDED:
        final_error = ""
    elif register_sms_candidates:
        final_error = _summarize_register_sms_provider_exhausted_error(
            errors,
            register_sms_candidates,
        )
    else:
        final_error = errors[0]
    logger.finish(final_status, error=final_error)




def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    command_platform = str(payload.get("platform", ""))

    account_id = int(payload.get("account_id", 0) or 0)

    action_id = str(payload.get("action_id", ""))

    params = dict(payload.get("params") or {})

    runtime = PlatformRuntime()

    result = runtime.execute_action(

        type("Command", (), {

            "platform": command_platform,

            "account_id": account_id,

            "action_id": action_id,

            "params": params,

        })(),

        log_fn=logger.log,

        cancel_check=logger.is_cancel_requested,

    )

    if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    if not result.ok:

        logger.record_error(result.error)

        logger.finish(TASK_STATUS_FAILED, error=result.error)

        return

    logger.set_result_data(result.data)

    message = ""

    if isinstance(result.data, dict):

        message = str(result.data.get("message", "") or "")

        open_url = str(

            result.data.get("paypal_authorize_url")

            or result.data.get("checkout_url")

            or result.data.get("url")

            or result.data.get("cashier_url")

            or ""

        ).strip()

        if open_url:

            logger.add_cashier_url(open_url)

    if message:

        logger.log(message, event_type="summary")

    logger.set_progress(1, 1)

    logger.finish(TASK_STATUS_SUCCEEDED)





def _execute_phone_bind_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]

    fallback_ids = [int(item) for item in payload.get("fallback_ids") or [] if int(item or 0) > 0]

    total = len(ids) if ids else max(len(fallback_ids), 1)

    logger.set_progress(0, total)

    logger.log(

        f"开始绑定手机号：目标账号 {total} 个，浏览器模式 {payload.get('browser_mode') or 'camoufox_headed'}"

    )

    try:

        result = PhoneBindingService().bind(

            platform=str(payload.get("platform") or "chatgpt"),

            ids=ids,

            fallback_ids=fallback_ids,

            phone_lines=str(payload.get("phone_lines") or ""),

            browser_mode=str(payload.get("browser_mode") or "camoufox_headed"),

            bit_profile_id=str(payload.get("bit_profile_id") or ""),

            concurrency=max(int(payload.get("concurrency") or 1), 1),

            log_fn=logger.log,

        )

    except ValueError as exc:

        logger.record_error(str(exc))

        logger.finish(TASK_STATUS_FAILED, error=str(exc))

        return

    except Exception as exc:

        logger.record_error(str(exc))

        logger.finish(TASK_STATUS_FAILED, error=str(exc))

        return



    for _ in range(int(result.get("success_count") or 0)):

        logger.record_success()

    for item in result.get("results") or []:

        if item.get("ok"):

            logger.log(f"✓ 绑定成功: {item.get('email')} -> {item.get('phone')}")

        else:

            error = str(item.get("error") or "unknown error")

            logger.record_error(error)

            logger.log(f"✗ 绑定失败: {item.get('email')} -> {error}", level="error")

    logger.set_result_data(result)

    done = int(result.get("total") or total)

    logger.set_progress(done, done)

    final_status = TASK_STATUS_SUCCEEDED if int(result.get("failure_count") or 0) == 0 else TASK_STATUS_FAILED

    logger.finish(final_status)





def _execute_codex_oauth_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]

    if not ids:

        account_id = int(payload.get("account_id") or 0)

        if account_id > 0:

            ids = [account_id]

    if not ids:

        logger.finish(TASK_STATUS_FAILED, error="\u7f3a\u5c11 account_id")

        return

    total = len(ids)

    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)

    browser_mode = str(payload.get("browser_mode") or "camoufox_headed")

    bit_profile_id = str(payload.get("bit_profile_id") or "")

    logger.set_progress(0, total)

    logger.log(f"开始 Codex OAuth：账号 {total} 个，并发 {concurrency}，浏览器模式 {browser_mode}")



    results: list[dict[str, Any] | None] = [None] * total

    completed = 0



    def run_one(index: int, account_id: int) -> dict[str, Any]:

        logger.set_subtask(f"worker_{index + 1}", f"账号 {account_id}")

        try:

            if logger.is_cancel_requested():

                return {"ok": False, "account_id": account_id, "error": "任务已取消"}

            logger.log(f"[{index + 1}/{total}] 开始 Codex OAuth: {account_id}")

            result = CtfPlusAccountsService().run_codex_oauth_browser(

                account_id=account_id,

                browser_mode=browser_mode,

                bit_profile_id=bit_profile_id,

                log_fn=logger.log,

            )

            logger.log(f"[{index + 1}/{total}] Codex OAuth 成功: {result.get('email') or account_id}")

            return {"ok": True, **(result or {}), "account_id": account_id}

        except Exception as exc:

            error = str(exc)

            logger.log(f"[{index + 1}/{total}] Codex OAuth 失败 {account_id}: {error}", level="error")

            return {"ok": False, "account_id": account_id, "error": error}

        finally:

            logger.clear_subtask()



    with ThreadPoolExecutor(max_workers=concurrency) as pool:

        future_map = {}

        next_index = 0

        while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():

            future = pool.submit(run_one, next_index, ids[next_index])

            future_map[future] = next_index

            next_index += 1



        while future_map:

            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)

            for future in done:

                index = future_map.pop(future)

                try:

                    item = future.result()

                except Exception as exc:

                    item = {"ok": False, "account_id": ids[index], "error": str(exc)}

                results[index] = item

                if item.get("ok"):

                    logger.record_success()

                else:

                    logger.record_error(str(item.get("error") or "unknown error"))

                completed += 1

                logger.set_progress(completed, total)



            while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():

                future = pool.submit(run_one, next_index, ids[next_index])

                future_map[future] = next_index

                next_index += 1



    final_results = [item for item in results if item is not None]

    success_count = sum(1 for item in final_results if item.get("ok"))

    failure_count = len(final_results) - success_count

    result_data = {

        "total": total,

        "success_count": success_count,

        "failure_count": failure_count,

        "results": final_results,

        "concurrency": concurrency,

    }

    logger.set_result_data(result_data)

    if logger.is_cancel_requested() and len(final_results) < total:

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    final_status = TASK_STATUS_SUCCEEDED if failure_count == 0 else TASK_STATUS_FAILED

    logger.finish(final_status)





def _execute_get_rt_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    """批量获取 refresh_token（跳过手机验证）。"""

    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    task_mode = str(payload.get("task_mode") or "single").strip().lower()

    if task_mode not in {"single", "target"}:

        task_mode = "single"

    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]

    if not ids:

        account_id = int(payload.get("account_id") or 0)

        if account_id > 0:

            ids = [account_id]

    filter_fn = _filter_get_rt_target_ids if task_mode == "target" else _filter_registered_get_rt_ids

    ids, skipped_now = filter_fn(

        ids,

        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),

    )

    skipped_before = _normalize_task_ids(payload.get("skipped_non_registered_ids"))

    skipped_ids = skipped_before + [item for item in skipped_now if item not in skipped_before]

    if skipped_ids:

        logger.log(f"获取rt: 已过滤非仅注册账号 {len(skipped_ids)} 个: {skipped_ids}")

    if not ids:

        error = (

            "\u76ee\u6807\u6a21\u5f0f\u53ea\u80fd\u5bf9\u201c\u4ec5\u6ce8\u518c\u201d\u6216\u201c\u5df2\u83b7\u53d6rt\uff0c\u672a\u4e0a\u4f20\u201d\u72b6\u6001\u8d26\u53f7\u8d77\u4efb\u52a1"

            if task_mode == "target"

            else "\u83b7\u53d6rt\u53ea\u80fd\u5bf9\u4ec5\u6ce8\u518c\u72b6\u6001\u8d26\u53f7\u8d77\u4efb\u52a1"

        )

        logger.finish(TASK_STATUS_FAILED, error=error)

        return

    total = len(ids)

    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)

    browser_mode = str(payload.get("browser_mode") or "camoufox_headed")

    executor_type = str(payload.get("executor_type") or "browser").strip().lower() or "browser"
    sms_balance_action = _normalize_get_rt_sms_balance_action(
        payload.get("sms_balance_action")
    )
    logger.set_progress(0, total)

    mode_label = "\u76ee\u6807\u6a21\u5f0f" if task_mode == "target" else "\u5355\u8f6e\u6a21\u5f0f"

    if executor_type == "protocol":

        logger.log(f"开始获取rt：账号 {total} 个，并发 {concurrency}，任务模式 {mode_label}，执行方式 protocol")

    else:

        logger.log(f"开始获取rt：账号 {total} 个，并发 {concurrency}，任务模式 {mode_label}，执行方式 {executor_type}，浏览器模式 {browser_mode}")



    from infrastructure.platform_runtime import PlatformRuntime

    from core.db import engine, AccountModel

    from core.platform_accounts import build_platform_account

    from sqlmodel import Session



    sms_runtime = _resolve_get_rt_sms_runtime_config(payload)

    sms_provider = sms_runtime["sms_provider"]

    if sms_provider == "smspool":

        logger.log(

            "获取rt: SMSPool配置解析 "

            f"source={sms_runtime.get('settings_provider_key') or 'payload/default'} "

            f"country={sms_runtime.get('smspool_country') or '(default)'} "

            f"service={sms_runtime.get('smspool_service') or '(default)'} "

            f"max_price={sms_runtime.get('smspool_max_price') or '(default)'} "

            f"key={'set' if sms_runtime.get('smspool_api_key') else 'empty'}"

        )

    try:

        phone_reuse_count = max(int(payload.get("phone_reuse_count") or 3), 3)

    except Exception:

        phone_reuse_count = 3

    try:

        phone_change_limit = max(int(payload.get("phone_change_limit") or 10), 1)

    except Exception:

        phone_change_limit = 10



    sms_candidates = _list_get_rt_sms_provider_candidates(payload, sms_runtime) if task_mode == "target" else []

    active_sms_index = 0

    exhausted_sms_providers: set[str] = set()

    if task_mode == "target" and sms_candidates:

        order = " -> ".join(str(item.get("provider") or "") for item in sms_candidates)

        logger.log(f"获取rt: 目标模式接码平台轮换顺序: {order}")



    def create_phone_reuse_pool(runtime_config: dict[str, str]):

        provider = runtime_config.get("sms_provider") or ""

        if provider not in {"smspool", "smsapi"}:

            return None, ""

        try:

            from platforms.chatgpt.browser_get_rt import build_get_rt_phone_reuse_pool



            pool, pool_error = build_get_rt_phone_reuse_pool(

                sms_provider=provider,

                smspool_api_key=runtime_config["smspool_api_key"],

                smspool_max_price=runtime_config["smspool_max_price"],

                smspool_country=runtime_config["smspool_country"],

                smspool_service=runtime_config["smspool_service"],

                smspool_base_url=runtime_config["smspool_base_url"],

                smspool_compat_base_url=runtime_config["smspool_compat_base_url"],

                smspool_pricing_option=runtime_config["smspool_pricing_option"],

                smspool_poll_interval=runtime_config["smspool_poll_interval"],

                smsapi_phone=runtime_config["smsapi_phone"],

                smsapi_url=runtime_config["smsapi_url"],

                reuse_count=phone_reuse_count,

                log_fn=logger.log,

            )

            if pool:

                logger.log(

                    f"获取rt: 启用任务级手机号复用 provider={provider}, "

                    f"每号成功 {phone_reuse_count} 次后换号"

                )

            else:

                logger.log(f"获取rt: 手机号复用池创建失败: {pool_error}", level="error")

            return pool, pool_error

        except Exception as exc:

            logger.log(f"获取rt: 手机号复用池初始化异常: {exc}", level="error")

            return None, str(exc)



    phone_reuse_pool = None

    phone_pool_error = ""

    if sms_provider in {"smspool", "smsapi"}:

        phone_reuse_pool, phone_pool_error = create_phone_reuse_pool(sms_runtime)

    elif sms_provider:

        logger.log(f"获取rt: 使用通用手机号 provider={sms_provider}，每个账号单独创建接码 callback")



    def close_phone_pool(pool) -> None:

        if pool:

            try:

                pool.cleanup()

            except Exception as exc:

                logger.log(f"获取rt: 手机号复用池清理异常: {exc}", level="error")

        # 清理完后，驱动一次 SMSPool 释放重试队列，让冷却窗口外的号能尽快被取消；

        # 仍未到期的号会保留在 data/smspool_release_queue.json 里由后台 worker 重试。

        try:

            from platforms.gopay.sms_channel import (

                _process_release_queue_once,

                get_smspool_release_queue_size,

            )

            attempted, released = _process_release_queue_once(force=False, log_fn=logger.log)

            pending = get_smspool_release_queue_size()

            if attempted or pending:

                logger.log(

                    f"获取rt: SMSPool 释放队列进度 attempted={attempted} "

                    f"released={released} pending={pending}"

                )

        except Exception as exc:

            logger.log(f"获取rt: SMSPool 释放队列驱动异常: {exc}", level="warning")



    results: list[dict[str, Any] | None] = [None] * total

    target_success_ids: set[int] = set()



    def account_has_refresh_token(account_id: int) -> bool:

        try:

            with Session(engine) as session:

                model = session.get(AccountModel, account_id)

                if not model:

                    return False

                account = build_platform_account(session, model)

                extra = getattr(account, "extra", {}) or {}

                return bool(str(extra.get("refresh_token") or "").strip())

        except Exception:

            return False



    def upload_existing_rt(index: int, account_id: int) -> dict[str, Any] | None:

        if not account_has_refresh_token(account_id):

            return None

        logger.log(f"[{index + 1}/{total}] 目标模式: 账号 #{account_id} 已有 refresh_token，优先重试上传")

        if not _is_sub2api_configured():

            _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 未配置")

            return {

                "ok": True,

                "final_ok": False,

                "uploaded": False,

                "account_id": account_id,

                "error": "SUB2API 未配置，无法完成上传",

                "hard_error": True,

                "sms_provider": sms_provider,

            }

        upload_ok = False

        try:

            with Session(engine) as upload_session:

                fresh_model = upload_session.get(AccountModel, account_id)

                if fresh_model:

                    upload_ok = _auto_upload_sub2api(logger, build_platform_account(upload_session, fresh_model))

        except Exception as exc:

            logger.log(f"  [SUB2API] 目标模式重试上传异常: {exc}", level="warning")

            upload_ok = False

        if upload_ok is True:

            _mark_get_rt_upload_status(account_id, uploaded=True, upload_message="SUB2API 上传成功")

            return {

                "ok": True,

                "final_ok": True,

                "uploaded": True,

                "account_id": account_id,

                "data": {"upload_only": True},

                "sms_provider": sms_provider,

            }

        _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 上传失败")

        return {

            "ok": True,

            "final_ok": False,

            "uploaded": False,

            "account_id": account_id,

            "error": "SUB2API 上传失败",

            "sms_provider": sms_provider,

        }



    def run_one(index: int, account_id: int) -> dict[str, Any]:

        logger.set_subtask(f"get_rt_{index + 1}", f"账号 {account_id}")

        try:

            if logger.is_cancel_requested():

                return {"ok": False, "account_id": account_id, "error": "任务已取消"}

            if task_mode == "target":

                upload_result = upload_existing_rt(index, account_id)

                if upload_result is not None:

                    return upload_result

            logger.log(f"[{index + 1}/{total}] 获取rt: 账号 #{account_id}")

            runtime = PlatformRuntime()

            command_params = {

                "executor_type": executor_type,

                "browser_mode": browser_mode,

                "record_har": str(payload.get("record_har") or "").strip().lower(),

                "sms_provider": sms_provider,

                "smspool_api_key": sms_runtime["smspool_api_key"],

                "smspool_max_price": sms_runtime["smspool_max_price"],

                "smspool_country": sms_runtime["smspool_country"],

                "smspool_service": sms_runtime["smspool_service"],

                "smspool_base_url": sms_runtime["smspool_base_url"],

                "smspool_compat_base_url": sms_runtime["smspool_compat_base_url"],

                "smspool_pricing_option": sms_runtime["smspool_pricing_option"],

                "smspool_poll_interval": sms_runtime["smspool_poll_interval"],

                "smsapi_phone": sms_runtime["smsapi_phone"],

                "smsapi_url": sms_runtime["smsapi_url"],

                "phone_reuse_count": str(phone_reuse_count),

                "phone_change_limit": str(phone_change_limit),

            }

            if phone_reuse_pool:

                command_params["phone_callback"] = phone_reuse_pool.make_callback(

                    label=f"{index + 1}/{total}"

                )

            result = runtime.execute_action(

                type("Command", (), {

                    "platform": "chatgpt",

                    "account_id": account_id,

                    "action_id": "get_rt",

                    "params": command_params,

                })(),

                log_fn=logger.log,

                cancel_check=logger.is_cancel_requested,

            )

            if result.ok:

                logger.log(f"[{index + 1}/{total}] 获取rt成功: 账号 #{account_id}")

                _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 未上传")

                upload_ok = None

                try:

                    with Session(engine) as upload_session:

                        fresh_model = upload_session.get(AccountModel, account_id)

                        if fresh_model:

                            upload_ok = _auto_upload_sub2api(logger, build_platform_account(upload_session, fresh_model))

                except Exception as exc:

                    logger.log(f"  [SUB2API] 获取rt后自动上传异常: {exc}", level="warning")

                    upload_ok = False

                if upload_ok is True:

                    _mark_get_rt_upload_status(account_id, uploaded=True, upload_message="SUB2API 上传成功")

                elif upload_ok is False:

                    _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 上传失败")

                elif task_mode == "target" and not _is_sub2api_configured():

                    _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 未配置")

                return {

                    "ok": True,

                    "final_ok": _is_get_rt_upload_success(upload_ok) if task_mode == "target" else True,

                    "uploaded": upload_ok is True,

                    "account_id": account_id,

                    "data": result.data,

                    "sms_provider": sms_provider,

                    "error": "SUB2API 未配置，无法完成上传" if task_mode == "target" and upload_ok is None else "",

                    "hard_error": bool(task_mode == "target" and upload_ok is None and not _is_sub2api_configured()),

                }

            else:

                error = str(result.error or "unknown error")

                logger.log(f"[{index + 1}/{total}] 获取rt失败 #{account_id}: {error}", level="error")

                return {

                    "ok": False,

                    "final_ok": False,

                    "account_id": account_id,

                    "error": error,

                    "sms_provider": sms_provider,

                    "sms_balance_error": _is_get_rt_balance_error(error),

                    "sms_provider_switch_error": _is_get_rt_sms_provider_switch_error(error),

                    "recoverable_error": _is_get_rt_target_recoverable_error(error),

                    "hard_error": _is_get_rt_hard_retry_error(error),

                }

        except Exception as exc:

            error = str(exc)

            logger.log(f"[{index + 1}/{total}] 获取rt异常 #{account_id}: {error}", level="error")

            return {

                "ok": False,

                "final_ok": False,

                "account_id": account_id,

                "error": error,

                "sms_provider": sms_provider,

                "sms_balance_error": _is_get_rt_balance_error(error),

                "sms_provider_switch_error": _is_get_rt_sms_provider_switch_error(error),

                "recoverable_error": _is_get_rt_target_recoverable_error(error),

                "hard_error": _is_get_rt_hard_retry_error(error),

            }

        finally:

            logger.clear_subtask()



    def run_single_pass(pending_indices: list[int], *, progress_on_finished: bool) -> tuple[list[dict[str, Any]], bool]:
        completed_in_pass = 0
        pass_results: list[dict[str, Any]] = []
        stop_pass_for_login_restart = False
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            future_map = {}
            next_index = 0
            while (
                next_index < len(pending_indices)
                and len(future_map) < concurrency
                and not logger.is_cancel_requested()
                and not stop_pass_for_login_restart
            ):
                account_index = pending_indices[next_index]
                future = pool.submit(run_one, account_index, ids[account_index])
                future_map[future] = account_index
                next_index += 1


            while future_map:

                done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)

                for future in done:

                    index = future_map.pop(future)

                    try:

                        item = future.result()

                    except Exception as exc:

                        item = {"ok": False, "account_id": ids[index], "error": str(exc)}

                    results[index] = item

                    if item.get("ok") and (task_mode != "target" or item.get("final_ok")):

                        logger.record_success()

                    else:

                        logger.record_error(str(item.get("error") or "unknown error"))

                    pass_results.append(item)

                    if task_mode == "target":

                        if item.get("final_ok"):

                            target_success_ids.add(int(item.get("account_id") or 0))

                        logger.set_progress(len(target_success_ids), total)

                    elif progress_on_finished:
                        completed_in_pass += 1
                        done_count = sum(1 for result_item in results if result_item is not None)
                        logger.set_progress(done_count, total)
                    if task_mode == "target" and _is_get_rt_login_restart_error(item.get("error")):
                        stop_pass_for_login_restart = True
                        logger.log(
                            "\u83b7\u53d6rt: \u76ee\u6807\u6a21\u5f0f\u68c0\u6d4b\u5230\u767b\u5f55 session \u5df2\u5931\u6548\uff0c"
                            "\u6682\u505c\u672c\u8f6e\u540e\u7eed\u8d26\u53f7\uff0c\u5c06\u5148\u7b49\u5f85 10s \u540e\u4ece\u5934\u91cd\u8bd5",
                            level="warning",
                        )

                while (
                    next_index < len(pending_indices)
                    and len(future_map) < concurrency
                    and not logger.is_cancel_requested()
                    and not stop_pass_for_login_restart
                ):
                    account_index = pending_indices[next_index]
                    future = pool.submit(run_one, account_index, ids[account_index])
                    future_map[future] = account_index
                    next_index += 1
        return pass_results, logger.is_cancel_requested()



    def switch_to_next_sms_provider(current_provider: str) -> bool:

        nonlocal sms_provider, sms_runtime, phone_reuse_pool, phone_pool_error, active_sms_index

        if not sms_candidates:

            return False

        exhausted_sms_providers.add(current_provider)

        while active_sms_index < len(sms_candidates) and str(sms_candidates[active_sms_index].get("provider") or "") in exhausted_sms_providers:

            active_sms_index += 1

        if active_sms_index >= len(sms_candidates):

            return False

        candidate = sms_candidates[active_sms_index]

        next_runtime = dict(candidate.get("sms_runtime") or {})

        next_provider = str(next_runtime.get("sms_provider") or candidate.get("provider") or "").strip()

        if not next_provider:

            return False

        close_phone_pool(phone_reuse_pool)

        phone_reuse_pool = None

        sms_runtime = next_runtime

        sms_provider = next_provider

        phone_pool_error = ""

        logger.log(

            f"获取rt: 接码平台当前不可用，切换到下一个已启用平台 provider={sms_provider} "

            f"({active_sms_index + 1}/{len(sms_candidates)})"

        )

        if sms_provider in {"smspool", "smsapi"}:

            phone_reuse_pool, phone_pool_error = create_phone_reuse_pool(sms_runtime)

        elif sms_provider:

            logger.log(f"获取rt: 使用通用手机号 provider={sms_provider}，每个账号单独创建接码 callback")

        return True



    try:

        if task_mode != "target":

            run_single_pass(list(range(total)), progress_on_finished=True)

        else:

            attempt_round = 1

            pending_indices = list(range(total))

            while pending_indices and not logger.is_cancel_requested():

                logger.log(

                    f"获取rt: 目标模式第 {attempt_round} 轮，待完成 {len(pending_indices)}/{total}，"

                    f"当前接码 provider={sms_provider or '(无)'}"

                )

                pass_results, _cancelled = run_single_pass(pending_indices, progress_on_finished=False)

                pending_indices = [

                    index

                    for index, account_id in enumerate(ids)

                    if account_id not in target_success_ids

                ]

                if not pending_indices:

                    break

                balance_errors = [

                    item for item in pass_results

                    if item.get("sms_provider_switch_error") or _is_get_rt_sms_provider_switch_error(item.get("error"))

                ]

                if balance_errors:

                    current_provider = str(balance_errors[-1].get("sms_provider") or sms_provider or "").strip()
                    if sms_balance_action == GET_RT_SMS_BALANCE_ACTION_TERMINATE:

                        logger.log(
                            "\u83b7\u53d6rt: \u63a5\u7801\u5e73\u53f0\u4f59\u989d\u4e0d\u8db3\uff0c\u5df2\u6309\u914d\u7f6e\u76f4\u63a5\u7ec8\u6b62\u76ee\u6807\u6a21\u5f0f\u4efb\u52a1",
                            level="error",
                        )

                        break

                    if sms_balance_action == GET_RT_SMS_BALANCE_ACTION_WAIT_RELEASE:

                        logger.log(
                            f"\u83b7\u53d6rt: \u63a5\u7801\u5e73\u53f0\u4f59\u989d\u4e0d\u8db3\uff0c\u5df2\u6309\u914d\u7f6e\u7b49\u5f85\u5f53\u524d\u5e73\u53f0\u624b\u673a\u53f7\u91ca\u653e\u540e\u91cd\u8bd5 provider={current_provider or sms_provider or '(none)'}",
                            level="warning",
                        )

                        close_phone_pool(phone_reuse_pool)

                        phone_reuse_pool = None

                        phone_pool_error = ""

                        if sms_provider == "smspool":

                            try:

                                from platforms.gopay.sms_channel import wait_for_smspool_release_queue_drain

                                wait_for_smspool_release_queue_drain(
                                    api_key=str(sms_runtime.get("smspool_api_key") or ""),
                                    base_url=str(sms_runtime.get("smspool_base_url") or ""),
                                    log_fn=logger.log,
                                )

                            except Exception as exc:

                                logger.log(
                                    f"\u83b7\u53d6rt: SMSPool \u91ca\u653e\u7b49\u5f85\u5f02\u5e38: {exc}",
                                    level="warning",
                                )

                        if sms_provider in {"smspool", "smsapi"}:

                            phone_reuse_pool, phone_pool_error = create_phone_reuse_pool(sms_runtime)

                        elif sms_provider:

                            logger.log(
                                f"\u83b7\u53d6rt: \u7ee7\u7eed\u4f7f\u7528\u5f53\u524d\u63a5\u7801 provider={sms_provider}"
                            )

                        logger.log(
                            "\u83b7\u53d6rt: \u7a77\u4e3e\u6a21\u5f0f\u7b49\u5f85 10s \u540e\u91cd\u65b0\u767b\u5f55\u5e76\u7ee7\u7eed\u5c1d\u8bd5\u5f53\u524d\u63a5\u7801\u56fd\u5bb6",
                            level="warning",
                        )

                        for _ in range(10):

                            if logger.is_cancel_requested():

                                break

                            time.sleep(1)

                        attempt_round += 1

                        continue
                    if not switch_to_next_sms_provider(current_provider):

                        logger.log("获取rt: 所有已启用接码平台均不可用，目标模式停止", level="error")

                        break

                    attempt_round += 1

                    continue

                hard_errors = [

                    item for item in pass_results

                    if (

                        item.get("hard_error")

                        or _is_get_rt_hard_retry_error(item.get("error"))

                    )

                    and not _is_get_rt_target_recoverable_error(item.get("error"))

                ]

                if hard_errors:
                    logger.log(
                        f"获取rt: 目标模式遇到硬性失败，停止重试: {hard_errors[-1].get('error')}",
                        level="error",
                    )
                    break
                email_cooldown_errors = [
                    item for item in pass_results
                    if _is_get_rt_email_login_cooldown_error(item.get("error"))
                ]
                if email_cooldown_errors:
                    logger.log(
                        "\u83b7\u53d6rt: \u76ee\u6807\u6a21\u5f0f\u9047\u5230\u90ae\u7bb1\u767b\u5f55\u9650\u6d41\uff0c"
                        f"\u505c\u6b62\u672c\u8f6e\u4efb\u52a1\u4ee5\u907f\u514d\u7ee7\u7eed\u89e6\u53d1 Too many tries: {email_cooldown_errors[-1].get('error')}",
                        level="error",
                    )
                    break
                recoverable_errors = [
                    item for item in pass_results
                    if item.get("recoverable_error") or _is_get_rt_target_recoverable_error(item.get("error"))
                ]
                if recoverable_errors:

                    logger.log(

                        f"获取rt: 目标模式遇到可恢复失败，将重新执行完整授权流程: {recoverable_errors[-1].get('error')}",

                        level="warning",

                    )

                logger.log(f"获取rt: 目标模式还有 {len(pending_indices)} 个账号未达成上传成功，10s 后重试")

                for _ in range(10):

                    if logger.is_cancel_requested():

                        break

                    time.sleep(1)

                attempt_round += 1

    finally:

        close_phone_pool(phone_reuse_pool)



    final_results = [item for item in results if item is not None]

    success_count = sum(

        1

        for item in final_results

        if item.get("ok") and (task_mode != "target" or item.get("final_ok"))

    )

    failure_count = len(final_results) - success_count

    result_data = {

        "total": total,

        "success_count": success_count,

        "failure_count": failure_count,

        "results": final_results,

        "task_mode": task_mode,
        "sms_balance_action": sms_balance_action,
    }

    logger.set_result_data(result_data)

    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    if task_mode == "target":

        final_status = TASK_STATUS_SUCCEEDED if len(target_success_ids) == total else TASK_STATUS_FAILED

    else:

        final_status = TASK_STATUS_SUCCEEDED if failure_count == 0 else TASK_STATUS_FAILED

    if final_status == TASK_STATUS_SUCCEEDED:

        logger.set_progress(total, total)

    elif task_mode == "target":

        logger.set_progress(len(target_success_ids), total)

    else:

        logger.set_progress(len(final_results), total)

    logger.finish(final_status)





def _execute_refresh_session_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """批量重新登录 ChatGPT 账号，刷新 Web session/at。"""

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    platform = str(payload.get("platform") or "chatgpt").strip() or "chatgpt"
    if platform != "chatgpt":
        logger.finish(TASK_STATUS_FAILED, error="重新登录获取session/at 仅支持 ChatGPT")
        return

    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    if not ids:
        ids = _list_account_ids_by_status(platform=platform, status=CHATGPT_RELOGIN_REQUIRED_STATUS)
        if ids:
            logger.log(f"未选择账号，自动处理状态为重登验证的账号 {len(ids)} 个")
        else:
            logger.finish(TASK_STATUS_FAILED, error="没有可重新登录的重登验证账号")
            return

    total = len(ids)
    try:
        concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)
    except Exception:
        concurrency = 1

    logger.set_progress(0, total)
    logger.log(f"开始重新登录获取session/at：账号 {total} 个，并发 {concurrency}")

    results: list[dict[str, Any] | None] = [None] * total
    completed = 0
    consecutive_cloudflare_challenges = 0
    stop_for_cloudflare_challenge = False

    def mark_account_banned(account_id: int, error: str) -> None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return
            patch_account_graph(
                session,
                model,
                lifecycle_status="banned",
                summary_updates={
                    "valid": False,
                    "validity_status": "invalid",
                    "display_status": "banned",
                    "health_error": error,
                    "deactivated_reason": error,
                },
            )
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()

    def run_one(index: int, account_id: int) -> dict[str, Any]:
        logger.set_subtask(f"refresh_session_{index + 1}", f"账号 {account_id}")
        try:
            if logger.is_cancel_requested():
                return {"ok": False, "account_id": account_id, "error": "任务已取消"}
            logger.log(f"[{index + 1}/{total}] 重新登录获取session/at: 账号 #{account_id}")
            runtime = PlatformRuntime()
            result = runtime.execute_action(
                type("Command", (), {
                    "platform": "chatgpt",
                    "account_id": account_id,
                    "action_id": "refresh_session",
                    "params": {},
                })(),
                log_fn=logger.log,
                cancel_check=logger.is_cancel_requested,
            )
            if result.ok:
                logger.log(f"[{index + 1}/{total}] 重新登录成功: 账号 #{account_id}")
                return {"ok": True, "account_id": account_id, "data": result.data}

            error = str(result.error or "重新登录失败")
            data = result.data if isinstance(result.data, dict) else {}
            error_type = str(data.get("error_type") or "")
            should_delete = bool(data.get("delete_local_account"))
            should_mark_banned = error_type == "account_banned" and not should_delete
            deleted = False
            if should_delete:
                deleted = AccountsRepository().delete(account_id)
                delete_note = "已删除本地账号" if deleted else "本地账号不存在或已删除"
                logger.log(
                    f"[{index + 1}/{total}] 重新登录失败，账号已封禁/注销，{delete_note} #{account_id}: {error}",
                    level="error",
                )
            elif should_mark_banned:
                mark_account_banned(account_id, error)
                logger.log(
                    f"[{index + 1}/{total}] 重新登录失败，账号已标记为封禁 #{account_id}: {error}",
                    level="error",
                )
            else:
                logger.log(f"[{index + 1}/{total}] 重新登录失败 #{account_id}: {error}", level="error")
            return {
                "ok": False,
                "account_id": account_id,
                "error": error,
                "deleted": deleted,
                "marked_banned": should_mark_banned,
                "error_type": error_type,
            }
        except Exception as exc:
            logger.log(f"[{index + 1}/{total}] 重新登录异常 #{account_id}: {exc}", level="error")
            return {"ok": False, "account_id": account_id, "error": str(exc)}
        finally:
            logger.clear_subtask()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_map = {}
        next_index = 0
        while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
            future = pool.submit(run_one, next_index, ids[next_index])
            future_map[future] = next_index
            next_index += 1
        while future_map:
            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                index = future_map.pop(future)
                try:
                    item = future.result()
                except Exception as exc:
                    item = {"ok": False, "account_id": ids[index], "error": str(exc)}
                results[index] = item
                if item.get("ok"):
                    logger.record_success()
                    consecutive_cloudflare_challenges = 0
                else:
                    logger.record_error(str(item.get("error") or "重新登录失败"))
                    if str(item.get("error_type") or "") == "cloudflare_managed_challenge":
                        consecutive_cloudflare_challenges += 1
                        if consecutive_cloudflare_challenges >= 2:
                            stop_for_cloudflare_challenge = True
                            logger.log(
                                "连续遇到 Cloudflare Managed Challenge，停止提交后续账号；"
                                "请更换低风险代理/IP，或改用可执行 JS 的浏览器/Camoufox/本地 solver 路径后重试。",
                                level="warning",
                            )
                    else:
                        consecutive_cloudflare_challenges = 0
                completed += 1
                logger.set_progress(completed, total)
            while (
                next_index < total
                and len(future_map) < concurrency
                and not logger.is_cancel_requested()
                and not stop_for_cloudflare_challenge
            ):
                future = pool.submit(run_one, next_index, ids[next_index])
                future_map[future] = next_index
                next_index += 1

    final_results = [item for item in results if item is not None]
    success_count = sum(1 for item in final_results if item.get("ok"))
    failure_count = len(final_results) - success_count
    logger.set_result_data({
        "total": total,
        "success_count": success_count,
        "failure_count": failure_count,
        "results": final_results,
    })
    if stop_for_cloudflare_challenge and len(final_results) < total:
        logger.finish(TASK_STATUS_FAILED, error="Cloudflare Managed Challenge 连续拦截，已停止后续账号")
    elif logger.is_cancel_requested() and len(final_results) < total:
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
    elif success_count > 0:
        logger.finish(TASK_STATUS_SUCCEEDED)
    else:
        logger.finish(TASK_STATUS_FAILED, error="全部账号重新登录失败")


def _execute_agents_upload_sub2api_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform") or "chatgpt").strip() or "chatgpt"
    if platform != "chatgpt":
        logger.finish(TASK_STATUS_FAILED, error="Agents上传到Sub2Api 仅支持 ChatGPT")
        return

    account_ids = _normalize_task_ids(payload.get("ids"))
    if not account_ids:
        account_ids = _list_agents_upload_account_ids(platform)
    total = len(account_ids)
    if total <= 0:
        logger.finish(TASK_STATUS_FAILED, error="没有可上传的状态正常 ChatGPT 账号")
        return

    batch_size = max(int(payload.get("batch_size") or 10), 1)
    verify_task = _bool_config(payload.get("verify_task"), False)
    timeout = max(int(payload.get("timeout") or 30), 1)

    from platforms.chatgpt.codex_agent_identity import create_codex_agent_identity
    from platforms.chatgpt.sub2api_upload import upload_agent_identity_auths_to_sub2api

    logger.log(f"开始 Agents上传到Sub2Api：账号 {total} 个，批大小 {batch_size}")
    logger.set_progress(0, total)
    success_count = 0
    failed_count = 0
    processed_count = 0
    pending_batch: list[dict[str, Any]] = []

    def _finish_one(email: str, ok: bool, message: str) -> None:
        nonlocal success_count, failed_count, processed_count
        processed_count += 1
        if ok:
            success_count += 1
            logger.record_success()
            _save_task_log(platform, email, "success", detail={"action": "agents_upload_sub2api", "message": message})
            logger.log(f"{email}: Agent Identity 已上传到 Sub2Api（{message}）")
        else:
            failed_count += 1
            logger.record_error(f"{email}: {message}")
            _save_task_log(platform, email, "failed", error=message, detail={"action": "agents_upload_sub2api"})
            if message.startswith("Agent Identity "):
                logger.log(f"{email}: {message}", level="error")
            else:
                logger.log(f"{email}: Agent Identity 上传失败：{message}", level="error")
        logger.set_progress(processed_count, total)

    def _flush_batch() -> None:
        nonlocal pending_batch
        if not pending_batch:
            return
        batch = pending_batch
        pending_batch = []
        logger.log(f"上传 Agent Identity 批次：{len(batch)} 个账号")
        ok, message, result = upload_agent_identity_auths_to_sub2api(
            [item["auth_json"] for item in batch],
            timeout=timeout,
        )
        item_map = {
            int(item.get("index") or 0): item
            for item in result.get("items", [])
            if isinstance(item, dict)
        }
        if item_map:
            for index, entry in enumerate(batch, start=1):
                item = item_map.get(index) or {}
                action = str(item.get("action") or "").strip().lower()
                item_message = str(item.get("message") or message or action or "unknown")
                if action in {"created", "updated"}:
                    _finish_one(entry["email"], True, action)
                else:
                    _finish_one(entry["email"], False, item_message)
            return
        for entry in batch:
            _finish_one(entry["email"], ok, message)

    for index, account_id in enumerate(account_ids, start=1):
        if logger.is_cancel_requested():
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        logger.set_subtask(f"agents_upload_{index}", f"账号 {account_id}")
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            account = build_platform_account(session, model) if model and model.platform == platform else None
        if not account:
            _finish_one(str(account_id), False, "账号不存在或平台不是 ChatGPT")
            continue
        email = str(getattr(account, "email", "") or account_id)
        try:
            target = _build_chatgpt_upload_account(account)
            access_token = str(getattr(target, "access_token", "") or "").strip()
            if not access_token:
                raise ValueError("账号缺少 access_token")
            logger.log(f"[{index}/{total}] {email}: 生成 Codex Agent Identity auth.json")
            auth_json = create_codex_agent_identity(
                access_token,
                verify_task=verify_task,
                timeout=timeout,
            )
            pending_batch.append({"email": email, "auth_json": auth_json})
            if len(pending_batch) >= batch_size:
                _flush_batch()
        except Exception as exc:
            _finish_one(email, False, f"Agent Identity 生成失败：{exc}")

    _flush_batch()
    logger.clear_subtask()
    logger.set_result_data({
        "total": total,
        "success": success_count,
        "failed": failed_count,
        "batch_size": batch_size,
    })
    if success_count > 0:
        logger.finish(TASK_STATUS_SUCCEEDED)
    else:
        logger.finish(TASK_STATUS_FAILED, error="Agents上传到Sub2Api 全部失败")


def _execute_get_rt_bypass_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """批量获取 refresh_token（绕过手机号，session/select 拦截）。"""

    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]

    if not ids:

        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")

        return

    total = len(ids)

    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)

    browser_mode = str(payload.get("browser_mode") or "camoufox_headed")

    logger.set_progress(0, total)

    logger.log(f"开始获取rt(绕过)：账号 {total} 个，并发 {concurrency}，{browser_mode}")



    from infrastructure.platform_runtime import PlatformRuntime

    from core.db import AccountModel, engine

    from core.platform_accounts import build_platform_account

    from sqlmodel import Session

    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED



    results: list[dict[str, Any] | None] = [None] * total

    completed = 0



    def run_one(index: int, account_id: int) -> dict[str, Any]:

        logger.set_subtask(f"get_rt_bypass_{index + 1}", f"账号 {account_id}")

        try:

            if logger.is_cancel_requested():

                return {"ok": False, "account_id": account_id, "error": "任务已取消"}

            logger.log(f"[{index + 1}/{total}] 获取rt(绕过): 账号 #{account_id}")

            runtime = PlatformRuntime()

            result = runtime.execute_action(

                type("Command", (), {

                    "platform": "chatgpt",

                    "account_id": account_id,

                    "action_id": "get_rt_bypass",

                    "params": {"browser_mode": browser_mode},

                })(),

                log_fn=logger.log,

                cancel_check=logger.is_cancel_requested,

            )

            if result.ok:

                logger.log(f"[{index + 1}/{total}] 获取rt(绕过)成功: 账号 #{account_id}")

                _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 未上传")

                upload_ok = None

                try:

                    with Session(engine) as upload_session:

                        fresh_model = upload_session.get(AccountModel, account_id)

                        if fresh_model:

                            upload_ok = _auto_upload_sub2api(logger, build_platform_account(upload_session, fresh_model))

                except Exception as exc:

                    logger.log(f"  [SUB2API] 获取rt(绕过)后自动上传异常: {exc}", level="warning")

                    upload_ok = False

                if upload_ok is True:

                    _mark_get_rt_upload_status(account_id, uploaded=True, upload_message="SUB2API 上传成功")

                elif upload_ok is False:

                    _mark_get_rt_upload_status(account_id, uploaded=False, upload_message="SUB2API 上传失败")

                return {"ok": True, "account_id": account_id, "data": result.data}

            else:

                error = str(result.error or "unknown error")

                logger.log(f"[{index + 1}/{total}] 获取rt(绕过)失败 #{account_id}: {error}", level="error")

                return {"ok": False, "account_id": account_id, "error": error}

        except Exception as exc:

            logger.log(f"[{index + 1}/{total}] 获取rt(绕过)异常 #{account_id}: {exc}", level="error")

            return {"ok": False, "account_id": account_id, "error": str(exc)}

        finally:

            logger.clear_subtask()



    with ThreadPoolExecutor(max_workers=concurrency) as pool:

        future_map = {}

        next_index = 0

        while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():

            future = pool.submit(run_one, next_index, ids[next_index])

            future_map[future] = next_index

            next_index += 1

        while future_map:

            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)

            for future in done:

                index = future_map.pop(future)

                try:

                    item = future.result()

                except Exception as exc:

                    item = {"ok": False, "account_id": ids[index], "error": str(exc)}

                results[index] = item

                if item.get("ok"):

                    logger.record_success()

                else:

                    logger.record_error(str(item.get("error") or "unknown error"))

                completed += 1

                logger.set_progress(completed, total)

            while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():

                future = pool.submit(run_one, next_index, ids[next_index])

                future_map[future] = next_index

                next_index += 1



    final_results = [item for item in results if item is not None]

    success_count = sum(1 for item in final_results if item.get("ok"))

    failure_count = len(final_results) - success_count

    logger.set_result_data({"total": total, "success_count": success_count, "failure_count": failure_count, "results": final_results})

    if logger.is_cancel_requested() and len(final_results) < total:

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    logger.finish(TASK_STATUS_SUCCEEDED if failure_count == 0 else TASK_STATUS_FAILED)





def _execute_gopay_register_account_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    """只注册 GoPay 账号并设置 PIN，不进入 ChatGPT Plus 付款流程。"""

    from application.gopay_pay_chatgpt import register_gopay_account



    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return



    pin = str(payload.get("gopay_pin") or payload.get("pin") or "147258").strip() or "147258"

    sms_provider = str(payload.get("sms_provider") or "herosms").strip().lower() or "herosms"

    max_price = str(payload.get("max_price") or "").strip()

    auto_rebind = _bool_config(payload.get("auto_rebind"), False)



    logger.set_progress(0, 1)

    logger.log(

        f"开始协议注册 GoPay 账户并设置 PIN：sms_provider={sms_provider}, "

        f"pin={'*' * len(pin)}"

    )



    try:

        model = register_gopay_account(

            herosms_api_key=str(payload.get("herosms_api_key") or "").strip(),

            pin=pin,

            proxy=str(payload.get("proxy") or "").strip(),

            envelope_url=str(payload.get("envelope_url") or "").strip(),

            sms_provider=sms_provider,

            smspool_api_key=str(payload.get("smspool_api_key") or "").strip(),

            smsbower_api_key=str(payload.get("smsbower_api_key") or "").strip(),

            smsapi_url=str(payload.get("smsapi_url") or "").strip(),

            smsapi_phone=str(payload.get("smsapi_phone") or "").strip(),

            herosms_max_price_usd=max_price,

            smspool_max_price=max_price,

            auto_rebind=auto_rebind,

            rebind_provider=str(payload.get("rebind_provider") or "herosms").strip().lower(),

            rebind_sms_key=str(payload.get("rebind_sms_key") or "").strip(),

            rebind_country=str(payload.get("rebind_country") or "").strip(),

            rebind_service=str(payload.get("rebind_service") or "").strip(),

            log=logger.log,

        )

    except Exception as exc:

        error = f"GoPay 协议注册任务异常: {exc}"

        logger.log(error, level="error")

        logger.record_error(error)

        logger.set_progress(1, 1)

        logger.finish(TASK_STATUS_FAILED, error=error)

        return



    if not model:

        error = "GoPay 协议注册失败：未产出可用账号"

        logger.log(error, level="error")

        logger.record_error(error)

        logger.set_progress(1, 1)

        logger.finish(TASK_STATUS_FAILED, error=error)

        return



    extra = dict(getattr(model, "extra", {}) or {})

    account_id = int(getattr(model, "id", 0) or 0)

    phone = str(extra.get("phone") or getattr(model, "user_id", "") or getattr(model, "email", "") or "")

    balance_raw = extra.get("balance_rp", 0)

    try:

        balance_rp = int(balance_raw or 0)

    except (TypeError, ValueError):

        balance_rp = 0



    logger.record_success()

    logger.set_progress(1, 1)

    logger.set_result_data({

        "account_id": account_id,

        "email": getattr(model, "email", ""),

        "phone": phone,

        "balance_rp": balance_rp,

        "sms_provider": sms_provider,

    })

    logger.log(f"GoPay 协议注册完成: #{account_id} {phone}")

    logger.finish(TASK_STATUS_SUCCEEDED)





def _execute_gopay_pay_chatgpt_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    """GoPay 协议付款 ChatGPT Plus 任务执行入口。



    并发处理 ``payload['chatgpt_account_ids']`` 里的每个 ChatGPT 账号：

    每条账号按"协议拿 cashier_url → 浏览器抓 midtrans_url → 协议付款"三步

    流水线跑一遍，失败不阻塞其它账号。



    若未选 ChatGPT 账号（``chatgpt_account_ids`` 为空）但给了

    ``register_count``，则先注册 N 个 ChatGPT 账号再跑付款。

    """

    from application.gopay_pay_chatgpt import execute_gopay_pay_chatgpt



    if logger.is_cancel_requested():

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return



    chatgpt_ids = [int(v) for v in payload.get("chatgpt_account_ids") or [] if int(v or 0) > 0]

    midtrans_url_override_early = str(payload.get("midtrans_url_override") or "").strip()



    # 需求 2：填了 midtrans_url 就不再注册 ChatGPT，直接拿这个 url 付款。

    # 用占位 chatgpt_account_id=0（execute 会跳过 ChatGPT 相关的标记逻辑）。

    if not chatgpt_ids and midtrans_url_override_early:

        logger.log("已提供 midtrans_url，跳过 ChatGPT 注册，直接付款")

        chatgpt_ids = [0]

    elif not chatgpt_ids:

        # 需求 5：没选 ChatGPT 账号也没 midtrans_url，则先从注册开始。

        register_count = max(int(payload.get("register_count") or 0), 0)

        if register_count <= 0:

            logger.finish(

                TASK_STATUS_FAILED,

                error="未选择 ChatGPT 账号，且未设置 register_count（无法从注册开始）",

            )

            return

        register_extra = dict(payload.get("register_extra") or {})

        # 注册阶段也按任务并发数并行（之前是串行 for 循环，导致 10 个号一个

        # 一个排队注册）。并发上限 = min(payload.concurrency, register_count)。

        register_concurrency = min(

            max(int(payload.get("concurrency") or 1), 1), register_count

        )

        # 短链模式：注册浏览器物理复用——在同一浏览器里注册→拿短链→抓 midtrans。

        _short_link_early = payload.get("use_short_link")

        _use_short_link_early = (

            _short_link_early is True

            or str(_short_link_early or "").strip().lower() in ("1", "true", "yes", "on")

        )

        if _use_short_link_early:

            try:

                logger.log(

                    f"短链复用模式：注册 {register_count} 个 ChatGPT，每个在同一浏览器里"

                    f"注册→拿短链→抓 midtrans（并发 {register_concurrency}）"

                )

                _sl_results = _register_chatgpt_shortlink_grab_for_gopay(

                    register_count, register_extra, logger,

                    concurrency=register_concurrency,

                    checkout_mode=str(payload.get("checkout_mode") or "camoufox_headed"),

                    bit_profile_id=str(payload.get("bit_profile_id") or ""),

                    country=str(payload.get("country") or "ID").upper(),

                    currency=str(payload.get("currency") or "IDR").upper(),

                    grab_timeout=max(int(payload.get("grab_timeout") or 300), 60),

                    proxy=payload.get("proxy") or None,

                )

            except Exception as exc:

                logger.finish(TASK_STATUS_FAILED, error=f"短链复用注册失败: {exc}")

                return

            if not _sl_results:

                logger.finish(TASK_STATUS_FAILED, error="短链复用：没产出任何 (账号+midtrans)")

                return

            chatgpt_ids = [int(r["account_id"]) for r in _sl_results]

            # 把每账号抓到的 midtrans_url 存进 payload，供付款循环按账号取用。

            payload["_shortlink_midtrans_map"] = {

                int(r["account_id"]): str(r["midtrans_url"]) for r in _sl_results

            }

        else:

            try:

                logger.log(

                    f"未选 ChatGPT 账号，先注册 {register_count} 个（并发 {register_concurrency}）"

                )

                chatgpt_ids = _register_chatgpt_accounts_for_gopay(

                    register_count, register_extra, logger,

                    concurrency=register_concurrency,

                )

            except Exception as exc:

                logger.finish(TASK_STATUS_FAILED, error=f"ChatGPT 注册失败: {exc}")

                return

            if not chatgpt_ids:

                logger.finish(TASK_STATUS_FAILED, error="ChatGPT 注册没产出任何账号")

                return



    gopay_account_id = int(payload.get("gopay_account_id") or 0) or None

    cashier_url_override = str(payload.get("cashier_url_override") or "")

    midtrans_url_override = str(payload.get("midtrans_url_override") or "")

    herosms_api_key_override = str(payload.get("herosms_api_key") or "")

    # **设计选择**：override 是手动调试用的（已经手动拿到一个 cashier 或

    # midtrans URL，只想试 GoPay 协议付款这一段）。它绑定在某一个具体的

    # ChatGPT 账号上，在多账号循环里**没法广播**复用——所以只允许单账号

    # 任务用 override，多账号时静默忽略让流水线全自动跑（每个账号都重新

    # 协议拿 cashier，浏览器抓 midtrans）。

    use_override = len(chatgpt_ids) == 1

    country = str(payload.get("country") or "ID").upper()

    currency = str(payload.get("currency") or "IDR").upper()

    headless = bool(payload.get("headless", False))

    checkout_mode = str(payload.get("checkout_mode") or "camoufox_headed")

    bit_profile_id = str(payload.get("bit_profile_id") or "")

    envelope_url = str(payload.get("envelope_url") or "")

    proxy = payload.get("proxy") or None

    grab_timeout = max(int(payload.get("grab_timeout") or 300), 60)

    phone_ttl_seconds = max(int(payload.get("phone_ttl_seconds") or 1200), 60)

    # 没有可用 GoPay 号时是否自动注册新号（默认开启——这是用户要的行为：

    # 抓到 midtrans 后没号就现注册，而不是直接失败）。

    auto_register_gopay = bool(payload.get("auto_register_gopay", True))

    gopay_pin = str(payload.get("gopay_pin") or "147258")

    sms_provider = str(payload.get("sms_provider") or "herosms").strip().lower()

    smspool_api_key = str(payload.get("smspool_api_key") or "")

    smsbower_api_key = str(payload.get("smsbower_api_key") or "")

    # smsapi（固定号 + 查最新短信 API）渠道参数

    smsapi_url = str(payload.get("smsapi_url") or "")

    smsapi_phone = str(payload.get("smsapi_phone") or "")

    # 拿号价格上限（USD）。herosms 与 smspool 都用 USD 计价，默认 0.11；

    # 空串交给插件用默认值。

    max_price = str(payload.get("max_price") or "").strip()

    # GoPay 号来源：auto（默认，先池后注册）/ pool（只用池）/ register（强制注册）。

    gopay_source = str(payload.get("gopay_source") or "auto").strip().lower()

    # #2：付款成功后自动换绑，把 GoPay 号占用的印尼号释放出来。

    _rebind_raw = payload.get("auto_rebind")

    auto_rebind = (

        _rebind_raw is True

        or str(_rebind_raw or "").strip().lower() in ("1", "true", "yes", "on")

    )

    # 换绑专用接码渠道（独立于注册渠道——注册用 smsapi 固定号时换绑仍要买

    # 一次性外国号）。默认 herosms。

    rebind_provider = str(payload.get("rebind_provider") or "herosms").strip().lower()

    rebind_sms_key = str(payload.get("rebind_sms_key") or "")

    rebind_country = str(payload.get("rebind_country") or "")

    rebind_service = str(payload.get("rebind_service") or "")

    # 调试抓包开关（前端）：开启后抓到 midtrans_url 不关浏览器，停在付款页让

    # 人工手动走完 GoPay 网页付款，全程录 HAR + dump 每页 HTML，不跑协议付款。

    _capture_raw = payload.get("capture_payment")

    capture_payment = (

        _capture_raw is True

        or str(_capture_raw or "").strip().lower() in ("1", "true", "yes", "on")

    )

    capture_dir = str(payload.get("capture_dir") or "")

    # 用 Stripe payment_pages/init 协议生成 cashier_url（accessToken →

    # pay.openai.com 长链，纯协议）。

    _stripe_init_raw = payload.get("use_stripe_init")

    use_stripe_init = (

        _stripe_init_raw is True

        or str(_stripe_init_raw or "").strip().lower() in ("1", "true", "yes", "on")

    )

    # 短链模式：checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc 短链。

    _short_link_raw = payload.get("use_short_link")

    use_short_link = (

        _short_link_raw is True

        or str(_short_link_raw or "").strip().lower() in ("1", "true", "yes", "on")

    )



    total = len(chatgpt_ids)

    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)

    logger.set_progress(0, total)

    logger.log(

        f"开始 GoPay 付款 ChatGPT Plus：账号 {total} 个，并发 {concurrency}，"

        f"checkout_mode={checkout_mode}, country={country}, currency={currency}, "

        f"grab_timeout={grab_timeout}s, phone_ttl={phone_ttl_seconds}s"

    )

    logger.log(

        f"GoPay 号选择：gopay_source={gopay_source}, "

        f"gopay_account_id={gopay_account_id}, sms_provider={sms_provider}"

    )



    results: list[dict[str, Any] | None] = [None] * total

    completed = 0



    def run_one(index: int, chatgpt_account_id: int) -> dict[str, Any]:

        logger.set_subtask(

            f"chatgpt_{chatgpt_account_id}", f"ChatGPT 账号 {chatgpt_account_id}"

        )

        acquired_profile = ""

        try:

            if logger.is_cancel_requested():

                return {"ok": False, "chatgpt_account_id": chatgpt_account_id, "error": "任务已取消"}

            # BitBrowser 模式：从「设置 → BitBrowser」的 profile 池里取一个，

            # 每个 worker 独占一个 profile，跑完归还。前端不再让用户手填

            # profile id。acquire 放进 try 里——池空/读取异常都算该账号失败，

            # 不连累其它并发账号。

            effective_bit_profile = bit_profile_id

            if checkout_mode.startswith("bitbrowser"):

                from application.bitbrowser_profiles import (

                    acquire_profile_for_browser_mode,

                )

                effective_bit_profile, acquired_profile = acquire_profile_for_browser_mode(

                    checkout_mode,

                    fallback=bit_profile_id,

                    log_fn=logger.log,

                )

            logger.log(f"[{index + 1}/{total}] 处理账号 #{chatgpt_account_id}")

            # 短链复用模式：midtrans_url 已经在注册同浏览器里抓好了，按账号取

            # 出来当 override 传进去，execute 内部会跳过自己的拿 cashier + 抓

            # midtrans（不会再开新浏览器）。

            _sl_map = payload.get("_shortlink_midtrans_map") or {}

            _sl_midtrans = str(_sl_map.get(int(chatgpt_account_id)) or "")

            _eff_midtrans_override = _sl_midtrans or (midtrans_url_override if use_override else "")

            out = execute_gopay_pay_chatgpt(

                chatgpt_account_id=chatgpt_account_id,

                gopay_account_id=gopay_account_id,

                cashier_url_override=cashier_url_override if use_override else "",

                midtrans_url_override=_eff_midtrans_override,

                country=country,

                currency=currency,

                headless=headless,

                checkout_mode=checkout_mode,

                bit_profile_id=effective_bit_profile,

                envelope_url=envelope_url,

                proxy=proxy,

                grab_timeout=grab_timeout,

                herosms_api_key_override=herosms_api_key_override,

                phone_ttl_seconds=phone_ttl_seconds,

                auto_register_gopay=auto_register_gopay,

                gopay_pin=gopay_pin,

                sms_provider=sms_provider,

                smspool_api_key=smspool_api_key,

                smsbower_api_key=smsbower_api_key,

                smsapi_url=smsapi_url,

                smsapi_phone=smsapi_phone,

                max_price=max_price,

                gopay_source=gopay_source,

                auto_rebind=auto_rebind,

                rebind_provider=rebind_provider,

                rebind_sms_key=rebind_sms_key,

                rebind_country=rebind_country,

                rebind_service=rebind_service,

                capture_payment=capture_payment,

                capture_dir=capture_dir,

                use_stripe_init=use_stripe_init,

                use_short_link=use_short_link,

                log=logger.log,

                cancel_check=logger.is_cancel_requested,

            )

            logger.log(f"[{index + 1}/{total}] 成功: #{chatgpt_account_id}")

            if int(chatgpt_account_id or 0) > 0:

                try:

                    with Session(engine) as session:

                        model = session.get(AccountModel, int(chatgpt_account_id))

                        if model:

                            marked_account = build_platform_account(session, model)

                            _mark_outlook_mailbox_event(None, marked_account, "plus_success", logger)

                except Exception as exc:

                    logger.log(f"outlookEmail Plus 自动打标签检查失败（忽略）: {exc}", level="warning")

            return {"ok": True, **out}

        except Exception as exc:

            error = str(exc)

            logger.log(f"[{index + 1}/{total}] 失败: {error}", level="error")

            return {"ok": False, "chatgpt_account_id": chatgpt_account_id, "error": error}

        finally:

            if acquired_profile:

                from application.bitbrowser_profiles import release_acquired_profile

                release_acquired_profile(acquired_profile, log_fn=logger.log)

            logger.clear_subtask()



    with ThreadPoolExecutor(max_workers=concurrency) as pool:

        future_map = {}

        next_index = 0

        while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():

            fut = pool.submit(run_one, next_index, chatgpt_ids[next_index])

            future_map[fut] = next_index

            next_index += 1



        while future_map:

            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)

            for fut in done:

                idx = future_map.pop(fut)

                try:

                    item = fut.result()

                except Exception as exc:

                    item = {"ok": False, "chatgpt_account_id": chatgpt_ids[idx], "error": str(exc)}

                results[idx] = item

                if item.get("ok"):

                    logger.record_success()

                else:

                    logger.record_error(str(item.get("error") or "unknown error"))

                completed += 1

                logger.set_progress(completed, total)



            while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():

                fut = pool.submit(run_one, next_index, chatgpt_ids[next_index])

                future_map[fut] = next_index

                next_index += 1



    final_results = [item for item in results if item is not None]

    success_count = sum(1 for item in final_results if item.get("ok"))

    logger.set_result_data({

        "total": total,

        "success_count": success_count,

        "failure_count": len(final_results) - success_count,

        "results": final_results,

    })

    if logger.is_cancel_requested() and success_count < total:

        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

        return

    final_status = (

        TASK_STATUS_SUCCEEDED if success_count == total else TASK_STATUS_FAILED

    )

    logger.finish(final_status)





def _register_chatgpt_shortlink_grab_for_gopay(

    register_count: int,

    register_extra: dict[str, Any],

    logger: "TaskLogger",

    *,

    concurrency: int = 1,

    checkout_mode: str = "camoufox_headed",

    bit_profile_id: str = "",

    country: str = "ID",

    currency: str = "IDR",

    grab_timeout: int = 300,

    proxy: str | None = None,

) -> list[dict[str, Any]]:

    """短链复用流程：每个账号在**同一个浏览器**里注册 → 拿短链 → 打开 → 抓

    midtrans_url，返回 ``[{"account_id", "midtrans_url"}, ...]``。



    物理复用注册浏览器（不关、不换）：短链是 ChatGPT 托管页、URL 无 token，

    必须用注册时那个已登录的浏览器打开。通过给 ChatGPTBrowserRegister 注入

    ``post_register_in_browser`` 回调，在注册拿到 session 后、浏览器还开着时，

    在同一 page 上 ``generate_plus_link(use_short_link)`` 拿短链 → goto → 抓

    midtrans。支持 Camoufox / BitBrowser，N 个并发各占一个浏览器。

    """

    from platforms._browser_backend import parse_checkout_mode

    from platforms.chatgpt import payment as chatgpt_payment



    payload = {

        "platform": "chatgpt",

        "executor_type": str(register_extra.get("executor_type") or "headless"),

        "captcha_solver": str(register_extra.get("captcha_solver") or "auto"),

        "extra": dict(register_extra or {}),

    }

    concurrency = min(max(int(concurrency or 1), 1), max(int(register_count), 1))



    results: list[dict[str, Any]] = []

    results_lock = threading.Lock()



    def _one(seq: int) -> None:

        if logger.is_cancel_requested():

            return

        logger.set_subtask(f"reg_pay_{seq + 1}", f"注册+短链 #{seq + 1}")

        acquired_profile = ""

        midtrans_holder: dict[str, str] = {}

        try:

            resolved_proxy = _resolve_registration_proxy_for_platform(

                "chatgpt", explicit_proxy=None, proxy_getter=lambda: None,

            )

            # 每个并发槽独占一个 BitBrowser profile（同 profile 不能并发开）。

            effective_bit_profile = bit_profile_id

            if checkout_mode.startswith("bitbrowser"):

                from application.bitbrowser_profiles import acquire_profile_for_browser_mode

                effective_bit_profile, acquired_profile = acquire_profile_for_browser_mode(

                    checkout_mode, fallback=bit_profile_id, log_fn=logger.log,

                )

            backend_config = parse_checkout_mode(checkout_mode, bit_profile_id=effective_bit_profile)



            # post_register_in_browser：注册完、浏览器还开着时，在同一 page 上

            # 拿短链 + 抓 midtrans。

            def _post_register(page, session_info: dict) -> dict:

                class _A:

                    pass

                a = _A()

                a.access_token = str(session_info.get("access_token") or "")

                a.cookies = str(session_info.get("cookies") or "")

                if not a.access_token:

                    logger.log("短链复用：注册结果没有 access_token，无法生成短链")

                    return {}

                short_url = chatgpt_payment.generate_plus_link(

                    a, proxy=None, country=country, currency=currency,

                    use_short_link=True,

                )

                logger.log(f"短链已生成（同浏览器复用）: {short_url[:70]}…")

                midtrans = chatgpt_payment.grab_midtrans_on_existing_page(

                    page, short_url, timeout_seconds=grab_timeout,

                    cancel_check=logger.is_cancel_requested, log=logger.log,

                )

                midtrans_holder["midtrans_url"] = midtrans

                return {"midtrans_url": midtrans}



            reg_extra = dict(payload["extra"])

            reg_extra["_reuse_backend_config"] = backend_config

            reg_extra["_post_register_in_browser"] = _post_register

            slot_payload = dict(payload)

            slot_payload["extra"] = reg_extra



            platform = _build_platform_instance(

                "chatgpt", slot_payload, logger, resolved_proxy=resolved_proxy,

            )
            account = platform.register()
            _auto_enable_chatgpt_2fa_after_register(
                account,
                logger,
                proxy=resolved_proxy,
                enable=_bool_config((dict(slot_payload.get("extra") or {})).get("enable_2fa_after_register"), False),
                require_password_set=_bool_config((dict(slot_payload.get("extra") or {})).get("set_password_after_register"), False),
            )
            saved_model = save_account(account)
            _mark_outlook_mailbox_event(getattr(platform, "mailbox", None), account, "registration_success", logger)
            _schedule_chatgpt_trial_post_register_check(
                account=account,
                saved_account_id=_saved_account_id(saved_model, account),
                logger=logger,
                proxy=resolved_proxy,
            )
            with Session(engine) as session:
                fresh = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")

                    .where(AccountModel.email == account.email)

                ).first()

                acc_id = int(fresh.id) if fresh else 0

            midtrans_url = midtrans_holder.get("midtrans_url", "")

            if acc_id and midtrans_url:

                with results_lock:

                    results.append({"account_id": acc_id, "midtrans_url": midtrans_url})

                logger.log(f"注册+短链+抓 midtrans 成功 #{seq + 1}: {account.email} -> ...{midtrans_url[-32:]}")

            elif acc_id:

                logger.log(f"注册成功但没抓到 midtrans #{seq + 1}: {account.email}（短链复用失败）", level="error")

            else:

                logger.log(f"注册后查不到账号 #{seq + 1}", level="error")

        except Exception as exc:

            logger.log(f"注册+短链失败 #{seq + 1}: {exc}", level="error")

        finally:

            if acquired_profile:

                try:

                    from application.bitbrowser_profiles import release_acquired_profile

                    release_acquired_profile(acquired_profile, log_fn=logger.log)

                except Exception:

                    pass

            logger.clear_subtask()



    with ThreadPoolExecutor(max_workers=concurrency) as pool:

        futures = {}

        next_seq = 0

        while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():

            futures[pool.submit(_one, next_seq)] = next_seq

            next_seq += 1

        while futures:

            done, _pending = wait(futures.keys(), return_when=FIRST_COMPLETED)

            for fut in done:

                futures.pop(fut, None)

            while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():

                futures[pool.submit(_one, next_seq)] = next_seq

                next_seq += 1



    return results





def _register_chatgpt_accounts_for_gopay(

    register_count: int,

    register_extra: dict[str, Any],

    logger: "TaskLogger",

    *,

    concurrency: int = 1,

) -> list[int]:

    """为 GoPay 付款流水线先注册 N 个 ChatGPT 账号，返回新账号 id 列表。



    复用现有 ``_build_platform_instance`` + ``platform.register`` + ``save_account``

    **并发**注册（``concurrency`` 由外层任务的并发数决定）。之前是串行 for

    循环，10 个号只能一个一个排队注册；现在用 ThreadPoolExecutor 同时跑，

    跟后续付款阶段一样的并发模型。



    **默认走浏览器后台模式（headless）**：协议注册当前过不去 ChatGPT 风控，

    浏览器后台更稳。调用方可以用 ``register_extra.executor_type`` 覆盖。

    """

    payload = {

        "platform": "chatgpt",

        "executor_type": str(register_extra.get("executor_type") or "headless"),

        "captcha_solver": str(register_extra.get("captcha_solver") or "auto"),

        "extra": dict(register_extra or {}),

    }

    concurrency = min(max(int(concurrency or 1), 1), max(int(register_count), 1))



    new_ids: list[int] = []

    new_ids_lock = threading.Lock()



    def _register_one(seq: int) -> None:

        if logger.is_cancel_requested():

            return

        logger.set_subtask(f"register_{seq + 1}", f"注册 ChatGPT #{seq + 1}")

        try:

            resolved_proxy = _resolve_registration_proxy_for_platform(

                "chatgpt",

                explicit_proxy=None,

                proxy_getter=lambda: None,

            )

            platform = _build_platform_instance(

                "chatgpt", payload, logger, resolved_proxy=resolved_proxy

            )

            account = platform.register()
            _auto_enable_chatgpt_2fa_after_register(
                account,
                logger,
                proxy=resolved_proxy,
                enable=_bool_config((dict(payload.get("extra") or {})).get("enable_2fa_after_register"), False),
                require_password_set=_bool_config((dict(payload.get("extra") or {})).get("set_password_after_register"), False),
            )

            save_account(account)

            _mark_outlook_mailbox_event(getattr(platform, "mailbox", None), account, "registration_success", logger)

            _schedule_chatgpt_trial_post_register_check(
                account=account,
                saved_account_id=_saved_account_id(None, account),
                logger=logger,
                proxy=resolved_proxy,
            )

            # save_account 返回的 model 出 session 即 detached，访问 .id 会抛
            # DetachedInstanceError。用 email 重新查一次拿稳定 id。

            with Session(engine) as session:

                fresh = session.exec(

                    select(AccountModel)

                    .where(AccountModel.platform == "chatgpt")

                    .where(AccountModel.email == account.email)

                ).first()

                if fresh:

                    with new_ids_lock:

                        new_ids.append(int(fresh.id))

            logger.log(f"ChatGPT 注册成功 #{seq + 1}: {account.email}")

        except Exception as exc:

            logger.log(f"ChatGPT 注册失败 #{seq + 1}: {exc}", level="error")

        finally:

            logger.clear_subtask()



    with ThreadPoolExecutor(max_workers=concurrency) as pool:

        futures = {}

        next_seq = 0

        # 先填满并发窗口

        while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():

            futures[pool.submit(_register_one, next_seq)] = next_seq

            next_seq += 1

        # 完成一个补一个，直到投满 register_count

        while futures:

            done, _pending = wait(futures.keys(), return_when=FIRST_COMPLETED)

            for fut in done:

                futures.pop(fut, None)

            while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():

                futures[pool.submit(_register_one, next_seq)] = next_seq

                next_seq += 1



    return new_ids





def _execute_account_check_task(payload: dict[str, Any], logger: TaskLogger) -> None:

    account_id = int(payload.get("account_id", 0) or 0)

    if account_id <= 0:

        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")

        return

    try:

        _, result = _run_single_account_check(account_id, logger)

        logger.set_result_data(result)

        logger.set_progress(1, 1)

        logger.finish(TASK_STATUS_SUCCEEDED)

    except Exception as exc:

        logger.record_error(str(exc))

        logger.finish(TASK_STATUS_FAILED, error=str(exc))





def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "") or "")
    limit = max(int(payload.get("limit", 50) or 50), 1)


    with Session(engine) as session:

        q = select(AccountModel)

        if platform:

            q = q.where(AccountModel.platform == platform)

        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())

        accounts = session.exec(q.limit(limit)).all()



    total = len(accounts)

    logger.set_progress(0, total)

    if total == 0:

        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0})

        logger.finish(TASK_STATUS_SUCCEEDED)

        return



    results = {"valid": 0, "invalid": 0, "error": 0}

    completed = 0

    for model in accounts:

        if logger.is_cancel_requested():

            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")

            return

        try:

            valid, _ = _run_single_account_check(int(model.id or 0), logger)

            if valid:

                results["valid"] += 1

            else:

                results["invalid"] += 1

        except Exception as exc:

            results["error"] += 1

            logger.record_error(str(exc))

            logger.log(f"{model.email}: 检测异常 {exc}", level="error")

        completed += 1

        logger.set_progress(completed, total)

    logger.set_result_data(results)
    logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_account_health_check_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "") or "")
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    # 串行执行：与单个账号「检测存活」走完全相同的验证逻辑，避免并发触发
    # ChatGPT/Cloudflare IP/WAF 限流返回 403，被误判为 banned。
    # （原 max_workers=10 并发时同一 IP 短时间大量请求易触发 WAF 403。）
    max_workers = 1

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        if ids:
            q = q.where(AccountModel.id.in_(ids))  # type: ignore[attr-defined]
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0, "items": []})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    account_ids = [int(model.id or 0) for model in accounts if model.id]
    items: list[dict[str, Any] | None] = [None] * len(account_ids)
    completed = 0
    counts = {"valid": 0, "invalid": 0, "error": 0}
    # 串行模式下不需要 network_coordinator：每个账号独立解析代理，
    # 与单个账号「检测存活」行为一致。
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {}
        next_index = 0
        while next_index < len(account_ids) and len(future_map) < max_workers and not logger.is_cancel_requested():
            future = pool.submit(
                _run_single_chatgpt_health_check,
                account_ids[next_index],
                logger,
                None,
            )
            future_map[future] = next_index
            next_index += 1

        while future_map:
            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                index = future_map.pop(future)
                try:
                    item = future.result()
                except Exception as exc:
                    item = {"account_id": account_ids[index], "valid": False, "error": str(exc), "transient": True}
                    logger.log(f"账号 #{account_ids[index]}: 测活异常 {exc}", level="error")
                items[index] = item
                if item.get("valid"):
                    counts["valid"] += 1
                    logger.record_success()
                elif item.get("transient"):
                    counts["error"] += 1
                    logger.record_error(str(item.get("error") or "unknown error"))
                else:
                    counts["invalid"] += 1
                    identity = str(item.get("email") or f"账号 #{account_ids[index]}")
                    error = str(item.get("error") or "账号状态/订阅查询判定失效")
                    logger.record_error(f"{identity}: {error}")
                completed += 1
                logger.set_progress(completed, total)

            if logger.is_cancel_requested():
                logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                return

            while next_index < len(account_ids) and len(future_map) < max_workers and not logger.is_cancel_requested():
                future = pool.submit(
                    _run_single_chatgpt_health_check,
                    account_ids[next_index],
                    logger,
                    None,
                )
                future_map[future] = next_index
                next_index += 1

    result = {**counts, "total": total, "items": [item for item in items if item is not None]}
    logger.set_result_data(result)
    logger.finish(TASK_STATUS_SUCCEEDED if counts["error"] == 0 else TASK_STATUS_FAILED)


def _is_momo_trial_result_taggable(result: dict[str, Any]) -> bool:
    decision = str(result.get("decision") or "")
    has_momo = bool(result.get("has_momo"))
    return has_momo and (bool(result.get("supported")) or decision == "ready")



def _mark_momo_trial_account(saved_account_id: int, probe: dict[str, Any]) -> None:
    if saved_account_id <= 0:
        return
    with Session(engine) as session:
        model = session.get(AccountModel, saved_account_id)
        if not model:
            return
        graph = load_account_graphs(session, [saved_account_id]).get(saved_account_id, {})
        overview = dict(graph.get("overview") or {})
        chips = [
            str(item).strip()
            for item in (overview.get("chips") or [])
            if str(item or "").strip()
        ]
        if MOMO_TRIAL_LABEL not in chips:
            chips.append(MOMO_TRIAL_LABEL)
        trial = probe.get("trial") if isinstance(probe.get("trial"), dict) else {}
        patch_account_graph(
            session,
            model,
            summary_updates={
                "chips": chips,
                "momo_trial": True,
                "momo_trial_decision": str(probe.get("decision") or "ready"),
                "momo_trial_has_momo": bool(probe.get("has_momo")),
                "momo_trial_one_click": trial.get("one_click_trial_eligible"),
                "momo_trial_period_days": trial.get("trial_period_days"),
                "momo_trial_checked_at": _utcnow_iso(),
            },
        )
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()


def _momo_trial_probe_result_data(
    *,
    total: int,
    ready: int,
    ineligible: int,
    failed: int,
    completed: int,
) -> dict[str, int]:
    total = max(int(total or 0), 0)
    completed = min(max(int(completed or 0), 0), total)
    return {
        "total": total,
        "ready": max(int(ready or 0), 0),
        "ineligible": max(int(ineligible or 0), 0),
        "failed": max(int(failed or 0), 0),
        "completed": completed,
        "remaining": max(total - completed, 0),
    }


def _execute_momo_trial_probe_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """批量检测账号是否具备越南 MoMo + 真实试用资格。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from core.platform_accounts import build_platform_account
    from core.proxy_pool import resolve_runtime_proxy
    from application.momo_trial_probe import probe_momo_trial

    raw_ids = payload.get("ids") or []
    ids: list[int] = []
    for item in raw_ids:
        try:
            value = int(item)
        except Exception:
            continue
        if value > 0:
            ids.append(value)
    platform = str(payload.get("platform") or "chatgpt").strip().lower() or "chatgpt"
    try:
        concurrency = int(payload.get("concurrency") or 3)
    except Exception:
        concurrency = 3
    concurrency = max(1, min(concurrency, 10))

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        if ids:
            q = q.where(AccountModel.id.in_(ids))  # type: ignore[attr-defined]
        models = list(session.exec(q.order_by(AccountModel.id.asc())).all())
        account_rows: list[tuple[int, Any]] = []
        for model in models:
            try:
                account = build_platform_account(session, model)
            except Exception as exc:
                logger.log(f"账号 #{model.id} 构建失败: {type(exc).__name__}", level="warning")
                continue
            account_rows.append((int(model.id), account))

    total = len(account_rows)
    if total <= 0:
        logger.log("没有可检测的账号")
        logger.set_progress(0, 0)
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    logger.set_progress(0, total)
    logger.log(f"开始 MoMo 试用资格检测：共 {total} 个账号，并发 {concurrency}")

    success = 0
    ready = 0
    ineligible = 0
    failed = 0
    progress_lock = threading.Lock()
    done_count = 0

    def _publish_progress() -> None:
        logger.set_result_data(
            _momo_trial_probe_result_data(
                total=total,
                ready=ready,
                ineligible=ineligible,
                failed=failed,
                completed=done_count,
            )
        )

    _publish_progress()

    def _resolve_proxy_for_account(account: Any) -> str:
        explicit = ""
        try:
            extra = getattr(account, "extra", None) or {}
            if isinstance(extra, dict):
                explicit = str(extra.get("proxy") or extra.get("proxy_url") or "").strip()
        except Exception:
            explicit = ""
        try:
            return str(
                resolve_runtime_proxy(
                    explicit_proxy=explicit or None,
                    region="VN",
                )
                or ""
            )
        except Exception:
            return explicit or ""

    def _worker(account_id: int, account: Any) -> dict[str, Any]:
        email = str(getattr(account, "email", "") or "")
        label = email or f"#{account_id}"
        access_token = _extract_chatgpt_access_token(account)
        cookies = _extract_chatgpt_cookies(account)
        if not access_token:
            return {
                "account_id": account_id,
                "label": label,
                "decision": "credential_invalid",
                "supported": False,
                "error": "缺少 access_token",
            }
        proxy = _resolve_proxy_for_account(account)
        try:
            result = probe_momo_trial(
                access_token=access_token,
                proxy=proxy,
                cookies=cookies,
                log_fn=lambda msg: logger.log(f"[{label}] {msg}"),
            )
        except Exception as exc:
            return {
                "account_id": account_id,
                "label": label,
                "decision": "checkout_failed",
                "supported": False,
                "error": f"{type(exc).__name__}",
            }
        result = dict(result or {})
        result["account_id"] = account_id
        result["label"] = label
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_worker, account_id, account): account_id for account_id, account in account_rows}
        for fut in as_completed(futures):
            account_id = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                failed += 1
                logger.log(f"账号 #{account_id} 异常: {type(exc).__name__}", level="error")
                with progress_lock:
                    done_count += 1
                    logger.set_progress(done_count, total)
                    _publish_progress()
                continue

            decision = str(result.get("decision") or "")
            label = str(result.get("label") or f"#{account_id}")
            supported = bool(result.get("supported"))
            trial = result.get("trial") if isinstance(result.get("trial"), dict) else {}
            momo_taggable = _is_momo_trial_result_taggable(result)
            if momo_taggable:
                if supported or decision == "ready":
                    ready += 1
                success += 1
                try:
                    _mark_momo_trial_account(int(result.get("account_id") or account_id), result)
                    logger.log(
                        f"✓ {label} 具备 MoMo 试用资格 decision={decision} "
                        f"momo={result.get('has_momo')}，已打标签 {MOMO_TRIAL_LABEL}"
                    )
                except Exception as exc:
                    logger.log(f"{label} 打标签失败: {type(exc).__name__}", level="warning")
                    logger.log(f"✓ {label} 具备 MoMo 试用资格（标签写入失败） decision={decision}")
            else:
                # 非 MoMo 试用可用；credential/checkout 类计 failed
                if decision in {"credential_invalid", "checkout_failed", "stripe_init_failed", "cloudflare", "rate_limited"}:
                    failed += 1
                    logger.log(
                        f"✗ {label} 决策={decision} error={result.get('error') or result.get('reason') or ''}",
                        level="warning",
                    )
                else:
                    success += 1
                    ineligible += 1
                    prefix = "·"
                    summary = "未达到 MoMo 试用条件"
                    if decision == "momo_not_enabled":
                        summary = "有试用但 MoMo 不可用，未打标签"
                    logger.log(
                        f"{prefix} {label} {summary} decision={decision} "
                        f"trial={trial.get('has_real_trial')} "
                        f"momo={result.get('has_momo')} "
                        f"methods={result.get('payment_method_types') or []}"
                    )

            with progress_lock:
                done_count += 1
                logger.set_progress(done_count, total)
                _publish_progress()

    logger.log(f"MoMo 试用检测完成：ready={ready} 完成={success} 失败={failed} 总计={total}")
    _publish_progress()
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
    else:
        logger.finish(TASK_STATUS_SUCCEEDED if failed == 0 else TASK_STATUS_FAILED)

