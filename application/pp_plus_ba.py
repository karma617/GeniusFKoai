from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from loguru import logger
except Exception:  # pragma: no cover
    import logging
    _std = logging.getLogger("pp_plus")
    class _Logger:
        def info(self, msg, *args, **kwargs):
            _std.info(msg if not args else msg.format(*args))
        def warning(self, msg, *args, **kwargs):
            _std.warning(msg if not args else msg.format(*args))
        def exception(self, msg, *args, **kwargs):
            _std.exception(msg if not args else msg.format(*args))
        def success(self, msg, *args, **kwargs):
            _std.info(msg if not args else msg.format(*args))
    logger = _Logger()
from sqlmodel import Session

from application.account_checks import AccountChecksService
from core.account_graph import load_account_graphs, patch_account_graph
from core.db import AccountModel, engine
from infrastructure.accounts_repository import AccountsRepository
from infrastructure.provider_settings_repository import ProviderSettingsRepository
from paypal.flow import PayPalFlow
from paypal.flow_factory import flow_class_for_country, normalize_flow_country
from paypal.models import generate_address, generate_card, generate_country_materials, generate_user
from paypal.proxy import build_dynamic_proxy_config, build_proxy_config, parse_proxy_pool_text

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "pp_plus_settings.json"
RUNTIME_PATH = Path(__file__).resolve().parents[1] / "data" / "pp_plus_runtime.json"
BA_TOKEN_RE = re.compile(r"BA-[A-Za-z0-9]{8,80}", re.I)
PHONE_RE = re.compile(r"^\+?\d{8,20}$")
NO_NUMBERS_RETRY_INTERVAL_SECONDS = 3
NO_NUMBERS_TIMEOUT_SECONDS = 180
PAYPAL_SMS_SERVICE_CODES = {
    "herosms_api": "ts",
    "herosms": "ts",
    "smsbower_api": "pp",
    "smsbower": "pp",
    "sms_activate": "pp",
    "sms_activate_api": "pp",
    "smsactivate": "pp",
    "grizzlysms_api": "pp",
    "grizzlysms": "pp",
    "sms_verification_number_api": "pp",
    "sms_verification_number": "pp",
    "smspool": "paypal",
    "smspool_api": "paypal",
    "five_sim_api": "paypal",
    "five_sim": "paypal",
    "5sim": "paypal",
    "nexsms_api": "pp",
    "nexsms": "pp",
}
DEFAULT_SETTINGS = {
    "sms_provider": "herosms_api",
    "sms_country": "73",
    "sms_service_code": "pp",
    "flow_country": "BR",
    "max_card_attempts": 5,
    "max_phone_changes": 5,
    "proxy_enabled": True,
    "proxy_mode": "api",
    "proxy_api_url": "",
    "proxy_pool_text": "",
    "debug": False,
}

def _now():
    return time.time()

def _text(value, default=""):
    text = str(value if value is not None else "").strip()
    return text or default

def _bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

def _int(value, default, minimum=None, maximum=None):
    try:
        number = int(value)
    except Exception:
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number

def _read_json(path, default):
    try:
        if not path.exists():
            return dict(default)
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return dict(default)

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def extract_ba_token(raw):
    text = _text(raw)
    if not text:
        return ""
    match = BA_TOKEN_RE.search(text)
    if not match:
        return ""
    token = match.group(0)
    return token if token.startswith("BA-") else f"BA-{token[3:]}"

def normalize_phone(raw):
    phone = re.sub(r"[\s().-]+", "", _text(raw))
    if phone and not phone.startswith("+") and phone.isdigit():
        phone = f"+{phone}"
    return phone

def _overview_of(account):
    if isinstance(account, dict):
        overview = account.get("overview")
        if isinstance(overview, dict):
            return overview
        return account
    overview = getattr(account, "overview", None)
    return overview if isinstance(overview, dict) else {}

def _is_free_plan(account):
    overview = _overview_of(account)
    if isinstance(account, dict):
        plan_state = _text(account.get("plan_state") or overview.get("plan_state")).lower()
        plan_name = _text(account.get("plan_name") or overview.get("plan_name")).lower()
    else:
        plan_state = _text(getattr(account, "plan_state", "") or overview.get("plan_state")).lower()
        plan_name = _text(getattr(account, "plan_name", "") or overview.get("plan_name")).lower()
    payment_status = _text(overview.get("pp_payment_status") or overview.get("payment_status")).lower()
    if payment_status in {"success", "succeeded", "paid", "ok"}:
        return False
    if plan_state in {"subscribed", "trial", "plus", "team", "enterprise"}:
        return False
    if any(token in plan_name for token in ("plus", "team", "enterprise", "pro")) and "free" not in plan_name:
        return False
    # free / unknown / empty 都视为可开通
    return True

def _account_ba_token(account):
    overview = _overview_of(account)
    return extract_ba_token(overview.get("pp_ba_token") or overview.get("ba_token") or overview.get("ba_chain") or "")


class NoAvailableSmsNumberError(RuntimeError):
    pass


class PpPlusStopped(RuntimeError):
    pass


def _raise_if_stopped(stop_check: Callable[[], bool] | None) -> None:
    if callable(stop_check) and stop_check():
        raise PpPlusStopped("任务已停止")


def _is_no_numbers_error(exc: Exception) -> bool:
    text = str(exc or "").upper()
    return (
        "NO_NUMBERS" in text
        or "NO NUMBER" in text
        or "NO FREE PHONES" in text
        or "NO FREE PHONE" in text
        or "无可用号码" in text
        or "暂无可用手机号" in text
    )


def _rent_sms_number_with_retry(
    sms_provider,
    *,
    service: str,
    country: str,
    on_wait: Callable[[int], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    retry_interval: int = NO_NUMBERS_RETRY_INTERVAL_SECONDS,
    timeout_seconds: int = NO_NUMBERS_TIMEOUT_SECONDS,
):
    deadline = time.monotonic() + max(int(timeout_seconds or 0), 0)
    attempt = 0
    while True:
        _raise_if_stopped(stop_check)
        attempt += 1
        try:
            return sms_provider.get_number(service=service, country=country)
        except Exception as exc:
            if not _is_no_numbers_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise NoAvailableSmsNumberError("暂无可用手机号") from exc
            if callable(on_wait):
                try:
                    on_wait(attempt)
                except Exception:
                    pass
            wait_seconds = max(float(retry_interval or 0), 0)
            if not callable(stop_check):
                time.sleep(wait_seconds)
            else:
                wait_until = time.monotonic() + wait_seconds
                while time.monotonic() < wait_until:
                    _raise_if_stopped(stop_check)
                    time.sleep(min(0.2, max(wait_until - time.monotonic(), 0)))


def _should_clear_ba_token_after_failure(error: object) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    preserve_markers = (
        "no_numbers",
        "暂无可用手机号",
        "无可用号码",
        "invalid phone",
        "phone number",
        "手机号",
        "otp",
        "验证码",
        "sms",
        "接码",
        "proxy",
        "timeout",
        "timed out",
        "tls",
        "connection",
        "network",
        "datadome",
        "captcha",
        "challenge",
        "buyer_not_set",
        "anonymous",
        "session",
    )
    if any(marker in text for marker in preserve_markers):
        return False

    ba_context_markers = (
        "ba_token",
        "billing agreement",
        "billingagreement",
        "billing agreement id",
        "agreements/approve",
        "/pay/billing",
        "authorize mutation",
    )
    clear_markers = (
        "invalid",
        "expired",
        "already used",
        "canceled",
        "cancelled",
        "not found",
        "not eligible",
        "form error",
        "form submission",
        "submit blocked",
        "submission blocked",
        "merchant rejected",
        "returnurl invalid",
    )
    return any(marker in text for marker in ba_context_markers) and any(marker in text for marker in clear_markers)


@dataclass
class AccountTaskView:
    account_id: int
    email: str = ""
    status: str = "idle"
    stage: str = ""
    ba_token: str = ""
    phone: str = ""
    error: str = ""
    logs: list = field(default_factory=list)
    updated_at: float = field(default_factory=_now)
    def to_dict(self):
        return asdict(self)


class PpPlusWorker:
    """串行扫描 free + 有 BA 链账号，并自动跑 PayPal BA 协议。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._settings = self.load_settings()
        self._runtime = self._load_runtime()
        self._current = None
        self._account_views = {}
        self._last_error = ""
        self._pending_account_ids = []

    def load_settings(self):
        data = _read_json(SETTINGS_PATH, DEFAULT_SETTINGS)
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: data.get(k, v) for k, v in DEFAULT_SETTINGS.items()})
        for key in ("sms_provider", "sms_country", "sms_service_code", "flow_country", "proxy_mode", "proxy_api_url", "proxy_pool_text"):
            if key in data:
                merged[key] = _text(data.get(key), DEFAULT_SETTINGS[key])
        if not _text(data.get("sms_service_code")):
            merged["sms_service_code"] = PAYPAL_SMS_SERVICE_CODES.get(_text(merged.get("sms_provider")), "pp")
        merged["max_card_attempts"] = _int(data.get("max_card_attempts"), 5, minimum=1, maximum=20)
        merged["max_phone_changes"] = _int(data.get("max_phone_changes"), 5, minimum=0, maximum=20)
        merged["proxy_enabled"] = _bool(data.get("proxy_enabled"), True)
        merged["debug"] = _bool(data.get("debug"), False)
        self._settings = merged
        return dict(merged)

    def save_settings(self, payload):
        current = self.load_settings()
        next_settings = dict(current)
        if "sms_provider" in payload:
            next_settings["sms_provider"] = _text(payload.get("sms_provider"), current["sms_provider"])
        if "sms_country" in payload:
            next_settings["sms_country"] = _text(payload.get("sms_country"), current["sms_country"])
        if "sms_service_code" in payload:
            next_settings["sms_service_code"] = _text(
                payload.get("sms_service_code"),
                PAYPAL_SMS_SERVICE_CODES.get(_text(next_settings.get("sms_provider")), "pp"),
            )
        if "flow_country" in payload:
            try:
                next_settings["flow_country"] = normalize_flow_country(payload.get("flow_country"))
            except Exception:
                next_settings["flow_country"] = current["flow_country"]
        if "max_card_attempts" in payload:
            next_settings["max_card_attempts"] = _int(payload.get("max_card_attempts"), 5, minimum=1, maximum=20)
        if "max_phone_changes" in payload:
            next_settings["max_phone_changes"] = _int(payload.get("max_phone_changes"), 5, minimum=0, maximum=20)
        if "proxy_enabled" in payload:
            next_settings["proxy_enabled"] = _bool(payload.get("proxy_enabled"), current["proxy_enabled"])
        if "proxy_mode" in payload:
            mode = _text(payload.get("proxy_mode"), current["proxy_mode"]).lower()
            next_settings["proxy_mode"] = mode if mode in {"api", "pool"} else current["proxy_mode"]
        if "proxy_api_url" in payload:
            next_settings["proxy_api_url"] = _text(payload.get("proxy_api_url"))
        if "proxy_pool_text" in payload:
            next_settings["proxy_pool_text"] = str(payload.get("proxy_pool_text") or "")
        if "debug" in payload:
            next_settings["debug"] = _bool(payload.get("debug"), False)
        _write_json(SETTINGS_PATH, next_settings)
        self._settings = next_settings
        return dict(next_settings)

    def _load_runtime(self):
        data = _read_json(RUNTIME_PATH, {
            "last_phone": "",
            "last_phone_success_count": 0,
            "last_sms_provider": "",
            "last_activation_id": "",
            "updated_at": 0,
        })
        return {
            "last_phone": _text(data.get("last_phone")),
            "last_phone_success_count": _int(data.get("last_phone_success_count"), 0, minimum=0),
            "last_sms_provider": _text(data.get("last_sms_provider")),
            "last_activation_id": _text(data.get("last_activation_id")),
            "updated_at": float(data.get("updated_at") or 0),
        }

    def _save_runtime(self):
        self._runtime["updated_at"] = _now()
        _write_json(RUNTIME_PATH, self._runtime)

    def _mark_phone_success(self, phone, provider, activation_id=""):
        with self._lock:
            phone_n = normalize_phone(phone)
            if phone_n and phone_n == normalize_phone(self._runtime.get("last_phone")):
                self._runtime["last_phone_success_count"] = _int(self._runtime.get("last_phone_success_count"), 0) + 1
            else:
                self._runtime["last_phone"] = phone_n
                self._runtime["last_phone_success_count"] = 1
            self._runtime["last_sms_provider"] = provider
            self._runtime["last_activation_id"] = activation_id
            self._save_runtime()

    def get_status(self):
        with self._lock:
            settings = dict(self._settings)
            runtime = dict(self._runtime)
            current = self._current.to_dict() if self._current else None
            accounts = {str(account_id): view.to_dict() for account_id, view in self._account_views.items()}
            running = bool(self._thread and self._thread.is_alive())
            sms_provider_options = self.list_sms_provider_options()
            return {
                "running": running,
                "stopping": bool(self._stop_event.is_set() and running),
                "settings": settings,
                "runtime": runtime,
                "current": current,
                "accounts": accounts,
                "pending_account_ids": list(self._pending_account_ids),
                "last_error": self._last_error,
                "sms_service_code": _text(
                    settings.get("sms_service_code"),
                    PAYPAL_SMS_SERVICE_CODES.get(_text(settings.get("sms_provider")), "pp"),
                ),
                "sms_provider_options": sms_provider_options,
            }

    def get_account_view(self, account_id):
        with self._lock:
            view = self._account_views.get(int(account_id))
            if view:
                return view.to_dict()
        repo = AccountsRepository()
        item = repo.get(int(account_id))
        if not item:
            return {}
        overview = item.overview if isinstance(item.overview, dict) else {}
        logs = overview.get("pp_task_logs") if isinstance(overview.get("pp_task_logs"), list) else []
        return {
            "account_id": int(account_id),
            "email": item.email,
            "status": _text(overview.get("pp_task_status"), "idle"),
            "stage": _text(overview.get("pp_task_stage")),
            "ba_token": extract_ba_token(overview.get("pp_ba_token") or ""),
            "phone": _text(overview.get("pp_last_phone")),
            "error": _text(overview.get("pp_task_error")),
            "logs": logs,
            "updated_at": float(overview.get("pp_task_updated_at") or 0),
        }

    def start(self, account_ids=None):
        selected_ids = []
        for raw_id in account_ids or []:
            try:
                account_id = int(raw_id)
            except Exception:
                continue
            if account_id > 0 and account_id not in selected_ids:
                selected_ids.append(account_id)
        if not selected_ids:
            raise ValueError("请先勾选要开通 PLUS 的账号")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.get_status()
            self._stop_event.clear()
            self._last_error = ""
            self._pending_account_ids = selected_ids
            self._thread = threading.Thread(target=self._run_loop, name="pp-plus-worker", daemon=True)
            self._thread.start()
        return self.get_status()

    def stop(self):
        self._stop_event.set()
        with self._lock:
            self._pending_account_ids = []
            current = self._current
        if current:
            self._set_account_stage(
                int(current.account_id),
                email=current.email,
                status="cancelled",
                stage="任务已停止",
                error="任务已停止",
                phone=current.phone,
                ba_token=current.ba_token,
                extra_overview={"pp_payment_status": "stopped"},
            )
        return self.get_status()

    def save_ba_token(self, account_id, raw_token):
        token = extract_ba_token(raw_token)
        if not token:
            raise ValueError("BA 链无效，请填写 BA-xxxxxxxx 或包含 ba_token 的链接")
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            if not model:
                raise ValueError("账号不存在")
            if _text(model.platform).lower() != "chatgpt":
                raise ValueError("仅支持 ChatGPT 账号")
            patch_account_graph(
                session,
                model,
                summary_updates={
                    "pp_ba_token": token,
                    "ba_token": token,
                    "pp_payment_status": "",
                    "pp_task_status": "idle",
                    "pp_task_stage": "已填写BA链",
                    "pp_task_error": "",
                    "pp_task_updated_at": _now(),
                },
                summary_remove_keys=["pp_task_logs"],
            )
            session.commit()
            email = model.email
        with self._lock:
            view = self._account_views.get(int(account_id)) or AccountTaskView(account_id=int(account_id), email=email)
            view.email = email
            view.ba_token = token
            view.status = "idle"
            view.stage = "已填写BA链"
            view.error = ""
            view.logs = []
            view.updated_at = _now()
            self._account_views[int(account_id)] = view
        return {"ok": True, "account_id": int(account_id), "ba_token": token}

    def remove_ba_token(self, account_id):
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            if not model:
                raise ValueError("账号不存在")
            if _text(model.platform).lower() != "chatgpt":
                raise ValueError("仅支持 ChatGPT 账号")
            patch_account_graph(
                session,
                model,
                summary_updates={
                    "pp_payment_status": "",
                    "pp_task_status": "idle",
                    "pp_task_stage": "BA链已清除",
                    "pp_task_error": "",
                    "pp_task_updated_at": _now(),
                },
                summary_remove_keys=["pp_ba_token", "ba_token"],
            )
            session.commit()
            email = model.email
        with self._lock:
            view = self._account_views.get(int(account_id)) or AccountTaskView(account_id=int(account_id), email=email)
            view.email = email
            view.ba_token = ""
            view.status = "idle"
            view.stage = "BA链已清除"
            view.error = ""
            view.updated_at = _now()
            self._account_views[int(account_id)] = view
        return {"ok": True, "account_id": int(account_id)}

    def clear_ba_token(self, account_id, reason=""):
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            if not model:
                return
            patch_account_graph(
                session,
                model,
                summary_updates={
                    "pp_payment_status": "failed" if reason else "",
                    "pp_task_error": reason,
                    "pp_task_updated_at": _now(),
                },
                summary_remove_keys=["pp_ba_token", "ba_token"],
            )
            session.commit()
        with self._lock:
            view = self._account_views.get(int(account_id))
            if view:
                view.ba_token = ""
                view.updated_at = _now()


    def _run_loop(self):
        logger.info("PP Plus worker started")
        try:
            while not self._stop_event.is_set():
                account = self._pick_next_account()
                if not account:
                    break
                if self._stop_event.is_set():
                    break
                self._process_account(account)
            logger.info("PP Plus worker stopped")
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("PP Plus worker crashed: {}", exc)
        finally:
            with self._lock:
                self._current = None

    def _pick_next_account(self):
        while True:
            with self._lock:
                if not self._pending_account_ids:
                    return None
                account_id = int(self._pending_account_ids.pop(0))
            account = self._load_selected_account(account_id)
            if account:
                return account

    def _load_selected_account(self, account_id):
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            if not model or _text(model.platform).lower() != "chatgpt":
                return None
            graph = load_account_graphs(session, [int(account_id)]).get(int(account_id)) or {}
            overview = graph.get("overview") if isinstance(graph.get("overview"), dict) else {}
            return {
                "id": int(account_id),
                "platform": model.platform,
                "email": model.email,
                "password": model.password,
                "user_id": model.user_id,
                "plan_state": graph.get("plan_state") or overview.get("plan_state") or "unknown",
                "plan_name": graph.get("plan_name") or overview.get("plan_name") or "",
                "overview": dict(overview),
            }

    def _set_account_stage(self, account_id, *, email, status, stage, error="", phone="", ba_token="", append_log=True, extra_overview=None):
        with self._lock:
            view = self._account_views.get(account_id) or AccountTaskView(account_id=account_id, email=email)
            if self._stop_event.is_set() and view.status == "cancelled" and status == "running":
                return
            view.email = email or view.email
            view.status = status
            view.stage = stage
            if error:
                view.error = error
            if phone:
                view.phone = phone
            if ba_token:
                view.ba_token = ba_token
            view.updated_at = _now()
            if append_log and stage:
                view.logs.append({"time": view.updated_at, "status": status, "stage": stage, "message": error or stage})
                if len(view.logs) > 300:
                    view.logs = view.logs[-300:]
            self._account_views[account_id] = view
            if status == "running":
                self._current = view
            logs_snapshot = list(view.logs)
            ba_snapshot = view.ba_token
            phone_snapshot = view.phone

        summary_updates = {
            "pp_task_status": status,
            "pp_task_stage": stage,
            "pp_task_error": error,
            "pp_task_logs": logs_snapshot,
            "pp_task_updated_at": _now(),
        }
        if ba_snapshot:
            summary_updates["pp_ba_token"] = ba_snapshot
            summary_updates["ba_token"] = ba_snapshot
        if phone_snapshot:
            summary_updates["pp_last_phone"] = phone_snapshot
        if extra_overview:
            summary_updates.update(extra_overview)
        try:
            with Session(engine) as session:
                model = session.get(AccountModel, account_id)
                if not model:
                    return
                patch_account_graph(session, model, summary_updates=summary_updates)
                session.commit()
        except Exception as exc:
            logger.warning("persist pp stage failed account={} err={}", account_id, exc)

    def list_sms_provider_options(self):
        """返回系统里可用于 PLUS 开通的接码平台（优先已启用配置）。"""
        from application.provider_settings import ProviderSettingsService
        from application.provider_definitions import ProviderDefinitionsService

        options = []
        seen = set()
        try:
            settings_rows = ProviderSettingsService().list_settings("sms")
        except Exception:
            settings_rows = []
        for row in settings_rows or []:
            if isinstance(row, dict):
                key = _text(row.get("provider_key"))
                enabled = _bool(row.get("enabled"), False)
                label = _text(row.get("display_name") or row.get("catalog_label") or key)
                is_default = _bool(row.get("is_default"), False)
            else:
                key = _text(getattr(row, "provider_key", ""))
                enabled = _bool(getattr(row, "enabled", False), False)
                label = _text(getattr(row, "display_name", "") or getattr(row, "catalog_label", "") or key)
                is_default = _bool(getattr(row, "is_default", False), False)
            if not key or key == "codex_sms_pool" or not enabled or key in seen:
                continue
            seen.add(key)
            options.append({
                "value": key,
                "label": label or key,
                "is_default": is_default,
                "service_code": PAYPAL_SMS_SERVICE_CODES.get(key, "pp"),
            })

        if not options:
            try:
                defs = ProviderDefinitionsService().list_definitions("sms", enabled_only=True)
            except Exception:
                defs = []
            for item in defs or []:
                if isinstance(item, dict):
                    key = _text(item.get("value") or item.get("provider_key"))
                    label = _text(item.get("label") or key)
                else:
                    key = _text(getattr(item, "value", "") or getattr(item, "provider_key", ""))
                    label = _text(getattr(item, "label", "") or key)
                if not key or key == "codex_sms_pool" or key in seen:
                    continue
                seen.add(key)
                options.append({
                    "value": key,
                    "label": label or key,
                    "is_default": False,
                    "service_code": PAYPAL_SMS_SERVICE_CODES.get(key, "pp"),
                })
        return options

    def _resolve_sms_saved_config(self, provider_key: str) -> dict:
        repo = ProviderSettingsRepository()
        saved = {}
        candidates = [provider_key]
        if provider_key.endswith("_api"):
            candidates.append(provider_key[: -4])
        else:
            candidates.append(f"{provider_key}_api")
        # 兼容历史 key
        aliases = {
            "five_sim_api": ["five_sim", "5sim"],
            "sms_activate_api": ["sms_activate", "smsactivate"],
            "sms_verification_number_api": ["sms_verification_number"],
        }
        candidates.extend(aliases.get(provider_key, []))
        for key in candidates:
            try:
                candidate = repo.resolve_runtime_settings("sms", key, {})
            except Exception:
                candidate = {}
            if candidate:
                return candidate
        return saved

    def _build_sms_provider(self, settings):
        provider_key = _text(settings.get("sms_provider"), "herosms_api")
        country = _text(settings.get("sms_country"), "73")
        service = _text(settings.get("sms_service_code"), PAYPAL_SMS_SERVICE_CODES.get(provider_key, "pp"))
        saved = self._resolve_sms_saved_config(provider_key)
        key_l = provider_key.lower()

        def _f(name_keys, default=-1.0):
            for k in name_keys:
                if saved.get(k) not in (None, ""):
                    try:
                        return float(saved.get(k))
                    except Exception:
                        pass
            return default

        def _s(name_keys, default=""):
            for k in name_keys:
                val = _text(saved.get(k))
                if val:
                    return val
            return default

        if "herosms" in key_l:
            from core.base_sms import HeroSmsProvider
            api_key = _s(["herosms_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("HeroSMS API Key 未配置，请先到系统设置填写")
            return HeroSmsProvider(
                api_key=api_key,
                default_service=service,
                default_country=country or _s(["herosms_default_country"], "73"),
                max_price=_f(["herosms_max_price"]),
                reuse_phone_to_max=True,
            ), service, provider_key

        if "smsbower" in key_l:
            from core.base_sms import SmsBowerProvider
            api_key = _s(["smsbower_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("SMSBower API Key 未配置，请先到系统设置填写")
            return SmsBowerProvider(
                api_key=api_key,
                default_service=service,
                default_country=country or _s(["smsbower_default_country"], "73"),
                max_price=_f(["smsbower_max_price"]),
                reuse_phone_to_max=True,
            ), service, provider_key

        if "grizzly" in key_l:
            from core.base_sms import GrizzlySmsProvider
            api_key = _s(["grizzlysms_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("GrizzlySMS API Key 未配置，请先到系统设置填写")
            return GrizzlySmsProvider(
                api_key=api_key,
                default_service=service,
                default_country=country or _s(["grizzlysms_default_country"], "73"),
                max_price=_f(["grizzlysms_max_price"]),
                reuse_phone_to_max=True,
            ), service, provider_key

        if "verification_number" in key_l:
            from core.base_sms import SmsVerificationNumberProvider
            api_key = _s(["sms_verification_number_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("SMS Verification Number API Key 未配置，请先到系统设置填写")
            return SmsVerificationNumberProvider(
                api_key=api_key,
                default_service=service,
                default_country=country or _s(["sms_verification_number_default_country"], "73"),
                max_price=_f(["sms_verification_number_max_price"]),
                reuse_phone_to_max=True,
            ), service, provider_key

        if "smspool" in key_l:
            from core.base_sms import SmsPoolProvider
            api_key = _s(["smspool_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("SMSPool API Key 未配置，请先到系统设置填写")
            kwargs = {
                "api_key": api_key,
                "default_service": service,
                "default_country": country or _s(["smspool_default_country"], "1"),
                "max_price": _f(["smspool_max_price"]),
            }
            base_url = _s(["smspool_base_url"])
            compat = _s(["smspool_compat_base_url"])
            if base_url:
                kwargs["base_url"] = base_url
            if compat:
                kwargs["compat_base_url"] = compat
            return SmsPoolProvider(**kwargs), service, provider_key

        if "five_sim" in key_l or key_l in {"5sim", "fivesim"}:
            from core.base_sms import FiveSimProvider
            api_key = _s(["five_sim_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("5sim API Key 未配置，请先到系统设置填写")
            product = service if service else "paypal"
            return FiveSimProvider(
                api_key=api_key,
                country=country or _s(["five_sim_country"], "brazil"),
                operator=_s(["five_sim_operator"], "any") or "any",
                product=product,
                max_price=_f(["five_sim_max_price"]),
                base_url=_s(["five_sim_base_url"]) or "https://5sim.net",
                reuse=_bool(saved.get("five_sim_reuse"), True),
            ), product, provider_key

        if "nexsms" in key_l:
            from core.base_sms import NexSmsProvider
            api_key = _s(["nexsms_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("NexSMS API Key 未配置，请先到系统设置填写")
            return NexSmsProvider(
                api_key=api_key,
                country_order=country or _s(["nexsms_default_country"], "BR"),
                service_code=service,
                max_price=_f(["nexsms_max_price"]),
                base_url=_s(["nexsms_base_url"]) or "https://api.nexsms.net",
            ), service, provider_key

        if "sms_activate" in key_l or "smsactivate" in key_l:
            from core.base_sms import SmsActivateProvider, SMS_ACTIVATE_SERVICES
            api_key = _s(["sms_activate_api_key", "api_key"])
            if not api_key:
                raise RuntimeError("SMS-Activate API Key 未配置，请先到系统设置填写")
            # 让逻辑名/服务码都能落到 PayPal 的 pp
            SMS_ACTIVATE_SERVICES.setdefault("pp", "pp")
            SMS_ACTIVATE_SERVICES.setdefault("paypal", "pp")
            return SmsActivateProvider(
                api_key=api_key,
                default_country=country or _s(["sms_activate_default_country"], "73"),
            ), service, provider_key

        if "codex_sms_pool" in key_l:
            raise RuntimeError("Codex 接码池不适用于 PayPal 自动租号，请选择第三方接码平台")

        raise RuntimeError(f"暂不支持的接码平台: {provider_key}")

    def _build_proxy_config(self, settings, flow_country):
        if not _bool(settings.get("proxy_enabled"), False):
            return build_proxy_config(enabled=False)
        mode = _text(settings.get("proxy_mode"), "api").lower()
        if mode == "pool":
            pool = parse_proxy_pool_text(settings.get("proxy_pool_text") or "")
            if pool:
                return build_proxy_config(enabled=True, pool=pool)
            return build_proxy_config(enabled=True)
        return build_dynamic_proxy_config(flow_country, api_url=_text(settings.get("proxy_api_url")) or None)

    def _process_account(self, account):
        account_id = int(account.get("id") if isinstance(account, dict) else getattr(account, "id", 0) or 0)
        email = _text(account.get("email") if isinstance(account, dict) else getattr(account, "email", ""))
        ba_token = _account_ba_token(account)
        settings = self.load_settings()
        self._set_account_stage(account_id, email=email, status="running", stage="准备支付任务", ba_token=ba_token)
        try:
            if not ba_token:
                raise RuntimeError("未填写BA链")
            if not _is_free_plan(account):
                raise RuntimeError("当前账号不是 Free 套餐，已跳过")
            _raise_if_stopped(self._stop_event.is_set)
            result = self._run_paypal_for_account(account, ba_token, settings)
            _raise_if_stopped(self._stop_event.is_set)
            self._set_account_stage(account_id, email=email, status="running", stage="支付成功，确认到账", phone=_text(result.get("phone")), ba_token=ba_token, extra_overview={"pp_payment_status": "success"})
            for _ in range(15):
                _raise_if_stopped(self._stop_event.is_set)
                time.sleep(0.2)
            plus_ok = self._refresh_and_check_plus(account_id)
            _raise_if_stopped(self._stop_event.is_set)
            stage = "支付成功，已到账" if plus_ok else "支付完成，套餐待确认"
            self._set_account_stage(account_id, email=email, status="success", stage=stage, phone=_text(result.get("phone")), ba_token="", extra_overview={"pp_payment_status": "success", "pp_task_status": "success"})
            self.clear_ba_token(account_id, reason="")
        except PpPlusStopped as exc:
            err = str(exc) or "任务已停止"
            self._set_account_stage(
                account_id,
                email=email,
                status="cancelled",
                stage=err,
                error=err,
                ba_token=ba_token,
                extra_overview={"pp_payment_status": "stopped"},
            )
        except Exception as exc:
            err = str(exc)
            logger.exception("PP Plus account failed id={} email={} err={}", account_id, email, err)
            clear_ba = _should_clear_ba_token_after_failure(err)
            self._set_account_stage(
                account_id,
                email=email,
                status="error",
                stage="任务失败，BA链已清除" if clear_ba else "任务失败",
                error=err,
                ba_token=ba_token,
                extra_overview={"pp_payment_status": "failed"},
            )
            if clear_ba:
                self.clear_ba_token(account_id, reason=err)

    def _refresh_and_check_plus(self, account_id):
        try:
            result = AccountChecksService().refresh_plan_sync("chatgpt", account_ids=[account_id], max_workers=1, timeout_seconds=90)
            items = result.get("items") if isinstance(result, dict) else []
            item = None
            if isinstance(items, list):
                for row in items:
                    if int(row.get("account_id") or 0) == account_id:
                        item = row
                        break
                if item is None and items:
                    item = items[0]
            if not isinstance(item, dict):
                return False
            blob = " ".join([
                _text(item.get("plan_state")).lower(),
                _text(item.get("plan_name")).lower(),
                _text(item.get("usage_plan_type")).lower(),
                _text(item.get("subscription_status")).lower(),
            ])
            return any(token in blob for token in ("plus", "subscribed", "team"))
        except Exception as exc:
            logger.warning("refresh plan after pp pay failed: {}", exc)
            return False

    def _run_paypal_for_account(self, account, ba_token, settings):
        account_id = int(account.get("id") if isinstance(account, dict) else getattr(account, "id", 0) or 0)
        email = _text(account.get("email") if isinstance(account, dict) else getattr(account, "email", ""))
        flow_country = normalize_flow_country(settings.get("flow_country") or "BR")
        max_card_attempts = _int(settings.get("max_card_attempts"), 5, minimum=1, maximum=20)
        max_phone_changes = _int(settings.get("max_phone_changes"), 5, minimum=0, maximum=20)

        self._set_account_stage(account_id, email=email, status="running", stage="初始化接码", ba_token=ba_token)
        sms_provider, service_code, provider_key = self._build_sms_provider(settings)

        self._set_account_stage(
            account_id,
            email=email,
            status="running",
            stage=f"正在获取手机号（{provider_key} / {service_code}）",
            ba_token=ba_token,
        )
        activation = _rent_sms_number_with_retry(
            sms_provider,
            service=service_code,
            country=_text(settings.get("sms_country")),
            stop_check=self._stop_event.is_set,
            on_wait=lambda attempt: self._set_account_stage(
                account_id,
                email=email,
                status="running",
                stage=f"暂无可用手机号，3秒后重试（第{attempt}次）",
                ba_token=ba_token,
            ),
        )
        phone = normalize_phone(activation.phone_number)
        if not phone:
            raise RuntimeError("接码平台未返回有效手机号")

        self._set_account_stage(account_id, email=email, status="running", stage="配置代理", phone=phone, ba_token=ba_token)
        proxy_config = self._build_proxy_config(settings, flow_country)

        if flow_country != "BR":
            user, card, address, _profile = generate_country_materials(phone, flow_country)
        else:
            user = generate_user(phone)
            card = generate_card(proxy_url=getattr(proxy_config, "url", None))
            address = generate_address()

        flow_class = flow_class_for_country(flow_country)
        stage_cb = lambda stage: self._set_account_stage(account_id, email=email, status="running", stage=stage, phone=phone, ba_token=ba_token)

        flow = type("AutoSms" + flow_class.__name__, (AutoSmsPayPalFlow, flow_class), {})(
            ba_token=ba_token,
            user=user,
            card=card,
            address=address,
            proxy_config=proxy_config,
            max_card_attempts=max_card_attempts,
            max_phone_changes=max_phone_changes,
            sms_provider=sms_provider,
            sms_activation=activation,
            sms_service=service_code,
            sms_country=_text(settings.get("sms_country")),
            stage_callback=stage_cb,
            stop_event=self._stop_event,
            on_phone_changed=lambda new_phone, act: self._set_account_stage(account_id, email=email, status="running", stage="更换手机号", phone=new_phone, ba_token=ba_token),
        )

        self._set_account_stage(account_id, email=email, status="running", stage="正在打开支付页面", phone=phone, ba_token=ba_token)
        _raise_if_stopped(self._stop_event.is_set)
        result = flow.run()
        _raise_if_stopped(self._stop_event.is_set)
        if isinstance(result, dict) and _text(result.get("status")).lower() not in {"", "success", "ok"}:
            raise RuntimeError(_text(result.get("error"), "PayPal 最终提交失败"))
        self._mark_phone_success(phone, provider_key, activation.activation_id)
        try:
            sms_provider.report_success(activation.activation_id)
        except Exception:
            pass
        return {"ok": True, "phone": phone, "result": result, "provider": provider_key}



class AutoSmsPayPalFlow(PayPalFlow):
    """自动接码版 PayPalFlow：手机号和验证码全部来自接码平台。"""

    def __init__(
        self,
        *args,
        sms_provider,
        sms_activation,
        sms_service,
        sms_country,
        stage_callback=None,
        stop_event=None,
        on_phone_changed=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.sms_provider = sms_provider
        self.sms_activation = sms_activation
        self.sms_service = sms_service
        self.sms_country = sms_country
        self.stage_callback = stage_callback
        self.stop_event = stop_event
        self.on_phone_changed = on_phone_changed
        self._phone_resend_fails = 0

    def _set_stage(self, stage):
        if callable(self.stage_callback):
            try:
                self.stage_callback(stage)
            except Exception:
                pass

    def _phase0_initial_load(self):
        self._set_stage("正在打开支付页面")
        return super()._phase0_initial_load()

    def _phase1_risk_controls(self):
        self._set_stage("正在挑战风控页")
        return super()._phase1_risk_controls()

    def _phase2_create_account(self):
        self._set_stage("正在填写资料")
        return super()._phase2_create_account()

    def _phase3_signup_and_2fa(self):
        self._set_stage("正在短信验证")
        return super()._phase3_signup_and_2fa()

    def _phase4_authorize(self):
        self._set_stage("正在最终授权")
        return super()._phase4_authorize()

    def _ensure_not_stopped(self):
        _raise_if_stopped(self.stop_event.is_set if self.stop_event else None)

    def _rent_new_phone(self):
        self._set_stage("正在获取手机号")
        try:
            if self.sms_activation is not None:
                self.sms_provider.cancel(self.sms_activation.activation_id)
        except Exception:
            pass
        activation = _rent_sms_number_with_retry(
            self.sms_provider,
            service=self.sms_service,
            country=self.sms_country,
            stop_check=self.stop_event.is_set if self.stop_event else None,
            on_wait=lambda attempt: self._set_stage(f"暂无可用手机号，3秒后重试（第{attempt}次）"),
        )
        self.sms_activation = activation
        phone = normalize_phone(activation.phone_number)
        self._update_user_phone(phone)
        self._phone_resend_fails = 0
        if callable(self.on_phone_changed):
            try:
                self.on_phone_changed(phone, activation)
            except Exception:
                pass
        return phone

    def _wait_sms_code(self, timeout=60):
        self._ensure_not_stopped()
        self._set_stage("正在获取验证码")
        activation_id = self.sms_activation.activation_id
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        last_exc = None
        while time.monotonic() < deadline:
            self._ensure_not_stopped()
            slice_timeout = max(1, min(3, int(deadline - time.monotonic()) or 1))
            if hasattr(self.sms_provider, "wait_for_code"):
                try:
                    payload = self.sms_provider.wait_for_code(activation_id, timeout=slice_timeout, poll_interval=1)
                    if isinstance(payload, dict):
                        code = _text(payload.get("code") or payload.get("otp"))
                        if code:
                            return code
                    elif payload:
                        return _text(payload)
                except Exception as exc:
                    last_exc = exc
                    logger.warning("wait_for_code failed: {}", exc)
            else:
                try:
                    code = _text(self.sms_provider.get_code(activation_id, timeout=slice_timeout))
                    if code:
                        return code
                except Exception as exc:
                    last_exc = exc
            self._ensure_not_stopped()
        raise TimeoutError(str(last_exc) if last_exc else "等待验证码超时")

    def _resend_sms(self):
        activation_id = self.sms_activation.activation_id
        self._set_stage("正在重发验证码")
        if hasattr(self.sms_provider, "request_resend_sms"):
            self.sms_provider.request_resend_sms(activation_id)
        elif hasattr(self.sms_provider, "set_status"):
            self.sms_provider.set_status(activation_id, 3)

    def _confirm_phone_with_retry(self, token, signup_url):
        phone_changes = 0
        max_phone_changes = getattr(self, "max_phone_changes", 5)
        browser_assist_phones = set()
        prefer_browser_initiate = False

        def _assist_url():
            return signup_url or self.state.signup_url or (
                f"https://www.paypal.com/checkoutweb/signup?token={token}&ul=1"
                f"&locale.x={self.state.locale or 'pt_BR'}&country.x={self.address.country}"
            )

        def _initiate_with_optional_browser():
            nonlocal prefer_browser_initiate
            phone_key = (self.user.phone_local or "").strip()
            if prefer_browser_initiate and phone_key not in browser_assist_phones:
                browser_assist_phones.add(phone_key)
                prefer_browser_initiate = False
                logger.warning(
                    "Preferring headed browser OTP initiate for phone {} after prior challenge/phone change",
                    self._masked_phone(),
                )
                assist = self._run_headed_browser_assist(
                    _assist_url(),
                    purpose="otp_authchallenge",
                    otp_phone_local=self.user.phone_local,
                    otp_token=token,
                )
                if assist and getattr(assist, "otp_auth_id", "") and getattr(assist, "otp_challenge_id", ""):
                    logger.success(
                        "OTP initiate via browser page context state={}",
                        getattr(assist, "otp_state", "") or "?",
                    )
                    return assist.otp_auth_id, assist.otp_challenge_id
            try:
                return self._initiate_2fa_phone_confirmation(token, signup_url)
            except Exception as e:
                msg = str(e)
                challenged = any(marker in msg for marker in ("authchallenge", "challenge", "datadome", "blocked", "denied", "403"))
                if challenged and phone_key not in browser_assist_phones:
                    browser_assist_phones.add(phone_key)
                    prefer_browser_initiate = False
                    assist = self._run_headed_browser_assist(
                        _assist_url(),
                        purpose="otp_authchallenge",
                        otp_phone_local=self.user.phone_local,
                        otp_token=token,
                    )
                    if assist and getattr(assist, "otp_auth_id", "") and getattr(assist, "otp_challenge_id", ""):
                        return assist.otp_auth_id, assist.otp_challenge_id
                raise

        while True:
            self._ensure_not_stopped()
            try:
                self._set_stage("正在发送验证码")
                auth_id, challenge_id = _initiate_with_optional_browser()
            except Exception as e:
                if phone_changes >= max_phone_changes:
                    raise RuntimeError(
                        "OTP send failed and phone-change limit reached "
                        f"({max_phone_changes})."
                    ) from e
                self._rent_new_phone()
                phone_changes += 1
                prefer_browser_initiate = True
                continue

            logger.info("SMS verification code sent to phone: {}", self._masked_phone())
            self._phone_resend_fails = 0

            while True:
                self._ensure_not_stopped()
                try:
                    code = self._wait_sms_code(timeout=60)
                except Exception:
                    code = ""

                if code and len(re.sub(r"\D", "", code)) >= 4:
                    compact = re.sub(r"\D", "", code)[:6]
                    self._set_stage("正在填写验证码")
                    if self._confirm_2fa_phone_confirmation(token, signup_url, auth_id, challenge_id, compact):
                        self._set_stage("验证码通过")
                        return
                    logger.warning("验证码验证失败，继续等待/重发")
                    code = ""

                self._phone_resend_fails += 1
                if self._phone_resend_fails > 3:
                    if phone_changes >= max_phone_changes:
                        raise RuntimeError(
                            f"复用手机号重发超过3次仍无验证码，且换号次数已达上限({max_phone_changes})"
                        )
                    self._set_stage("验证码超时，更换手机号")
                    self._rent_new_phone()
                    phone_changes += 1
                    prefer_browser_initiate = True
                    break

                try:
                    self._resend_sms()
                except Exception as exc:
                    logger.warning("resend sms failed: {}", exc)
                self._set_stage(f"等待验证码({self._phone_resend_fails}/3)")
                continue


_WORKER = None
_WORKER_LOCK = threading.Lock()


def get_pp_plus_worker():
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = PpPlusWorker()
        return _WORKER
