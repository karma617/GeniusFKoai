from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from sqlmodel import Session

from application.ba_link_extract import extract_ba_link, infer_region_from_proxy_text
from application.pp_plus_ba import get_pp_plus_worker
from core.account_graph import load_account_graphs, patch_account_graph
from core.db import AccountModel, engine
from core.platform_accounts import build_platform_account


ACTIVE_BA_EXTRACT_STATUSES = {"queued", "running", "started", "cancelling"}


def _now() -> float:
    return time.time()


def _text(value: Any, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    return text or default


def _format_log_time(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(float(ts or _now()))
    return dt.strftime("[%Y年%m月%d日 %H时%M分%S秒]")


@dataclass
class BaExtractTaskView:
    task_id: str
    account_id: int
    email: str = ""
    status: str = "idle"
    stage: str = ""
    step: int = 0
    total: int = 7
    attempt: int = 0
    max_attempts: int = 20
    ba_token: str = ""
    ba_url: str = ""
    region_combo: str = ""
    error: str = ""
    logs: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=_now)
    thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    seq: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "account_id": self.account_id,
            "email": self.email,
            "status": self.status,
            "stage": self.stage,
            "step": self.step,
            "total": self.total,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "ba_token": self.ba_token,
            "ba_url": self.ba_url,
            "region_combo": self.region_combo,
            "error": self.error,
            "logs": list(self.logs),
            "updated_at": self.updated_at,
        }


class BaExtractTaskManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._tasks: dict[int, BaExtractTaskView] = {}

    def get_task(self, account_id: int) -> dict[str, Any]:
        account_id = int(account_id)
        with self._condition:
            task = self._tasks.get(account_id)
            if task:
                return task.public_dict()
            return self._load_persisted_task(account_id, settle_orphaned=True)

    def start_task(self, account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """启动（或强制重启）账号的 BA 提取后台任务。

        - 默认：若已有 queued/running/started 任务，直接返回现有任务（幂等）。
        - force=True：取消旧 worker、清空日志/事件，创建全新 task_id 的任务。
        - cancelling 卡住时：也允许 force 重启；非 force 时若线程已死则自动收口并重启。
        """
        account_id = int(account_id)
        account_ctx = self._load_account_context(account_id)
        max_attempts = max(1, min(int(payload.get("max_attempts") or 20), 50))
        force = bool(payload.get("force") or payload.get("restart") or False)
        billing_country = infer_region_from_proxy_text(
            _text(payload.get("billing_proxy")), default=(_text(payload.get("billing_country"), "US"))
        )
        promo_country = infer_region_from_proxy_text(
            _text(payload.get("promo_proxy")), default=(_text(payload.get("promo_country"), billing_country))
        )
        with self._condition:
            existing = self._tasks.get(account_id)
            if existing and existing.status in ACTIVE_BA_EXTRACT_STATUSES:
                alive = bool(existing.thread and existing.thread.is_alive())
                if not force:
                    # 进程里已经没有活 worker 时，把旧的 running/cancelling 收口，允许重新启动。
                    if not alive:
                        if existing.status == "cancelling":
                            existing.status = "cancelled"
                            existing.stage = "任务已终止"
                            existing.error = "任务已终止"
                        else:
                            existing.status = "error"
                            existing.stage = "任务已结束，可重新提取"
                            existing.error = existing.stage
                        existing.updated_at = _now()
                        self._persist_task_async(existing)
                        self._condition.notify_all()
                    elif existing.status in {"queued", "running", "started"}:
                        return existing.public_dict()
                    elif existing.status == "cancelling" and alive:
                        return existing.public_dict()
                # force：通知旧 worker 退出；更换 task_id 后旧 emit 会被忽略
                if existing:
                    existing.cancel_event.set()

            task = BaExtractTaskView(
                task_id=str(uuid.uuid4()),
                account_id=account_id,
                email=account_ctx["email"],
                status="queued",
                stage="任务已排队",
                step=0,
                total=7,
                attempt=0,
                max_attempts=max_attempts,
                ba_token="",
                ba_url="",
                error="",
                logs=[],
                events=[],
            )
            self._tasks[account_id] = task
            # 清空持久化日志，避免刷新页面仍看到旧日志
            self._append_event_locked(
                task,
                {
                    "type": "started",
                    "status": "queued",
                    "desc": "任务已排队" if not force else "任务已重新开始",
                    "max_attempts": max_attempts,
                    "reset": True,
                },
            )
            thread = threading.Thread(
                target=self._run_task,
                args=(task.task_id, account_id, account_ctx, dict(payload), billing_country, promo_country),
                name=f"ba-extract-task-{account_id}",
                daemon=True,
            )
            task.thread = thread
            thread.start()
            self._condition.notify_all()
            return task.public_dict()

    def cancel_task(self, account_id: int) -> dict[str, Any]:
        """请求终止账号当前 BA 提取任务。

        通过 cancel_event 协作取消；worker 在检查点退出后发出 done/cancelled。
        """
        account_id = int(account_id)
        with self._condition:
            task = self._tasks.get(account_id)
            if not task:
                persisted = self._load_persisted_task(account_id, settle_orphaned=False)
                status = _text(persisted.get("status"), "idle").lower()
                if status in ACTIVE_BA_EXTRACT_STATUSES:
                    fake = self._task_from_persisted(account_id, persisted)
                    self._tasks[account_id] = fake
                    fake.cancel_event.set()
                    self._append_event_locked(
                        fake,
                        {
                            "type": "done",
                            "ok": False,
                            "cancelled": True,
                            "error": "任务已终止",
                        },
                    )
                    self._condition.notify_all()
                    return fake.public_dict()
                if persisted:
                    return persisted
                raise ValueError("当前没有可终止的 BA 提取任务")
            if task.status in {"success", "error", "cancelled"}:
                return task.public_dict()
            if task.cancel_event.is_set() and task.status in {"cancelling", "cancelled"}:
                return task.public_dict()
            task.cancel_event.set()
            task.status = "cancelling"
            task.stage = "正在终止任务..."
            self._append_event_locked(
                task,
                {
                    "type": "done",
                    "ok": False,
                    "cancelled": True,
                    "error": "任务已终止",
                },
            )
            self._condition.notify_all()
            return task.public_dict()

    def stream_events(self, account_id: int, *, after_seq: int = 0) -> Iterator[dict[str, Any]]:
        account_id = int(account_id)
        sent = int(after_seq or 0)
        while True:
            with self._condition:
                task = self._tasks.get(account_id)
                if not task:
                    persisted = self._load_persisted_task(account_id, settle_orphaned=True)
                    events_to_send: list[dict[str, Any]] = []
                    snapshot = {"type": "snapshot", **persisted}
                    terminal = True
                else:
                    pending = [event for event in task.events if int(event.get("seq") or 0) > sent]
                    if not pending and task.status not in {"success", "error", "cancelled"}:
                        self._condition.wait(timeout=15)
                        pending = [event for event in task.events if int(event.get("seq") or 0) > sent]
                    if not pending:
                        snapshot = {"type": "snapshot", **task.public_dict()}
                        terminal = task.status in {"success", "error", "cancelled"}
                        events_to_send = []
                    else:
                        events_to_send = [dict(event) for event in pending]
                        for event in events_to_send:
                            sent = int(event.get("seq") or sent)
                        snapshot = None
                        terminal = any(event.get("type") == "done" for event in events_to_send)

            # SSE iterator may resume on a different Starlette worker thread.
            # Never suspend a generator while the condition lock is held.
            if snapshot is not None:
                yield snapshot
            else:
                yield from events_to_send
            if terminal:
                return

    def _load_account_context(self, account_id: int) -> dict[str, Any]:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model or _text(model.platform).lower() != "chatgpt":
                raise ValueError("account not found or not chatgpt")
            account = build_platform_account(session, model)
            extra = account.extra or {}
            access_token = _text(extra.get("access_token") or account.token)
            if not access_token:
                raise ValueError("missing access_token")
            return {
                "access_token": access_token,
                "cookies": _text(extra.get("cookies")),
                "email": _text(account.email or model.email),
            }

    def _load_persisted_task(self, account_id: int, *, settle_orphaned: bool = False) -> dict[str, Any]:
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            if not model:
                return {}
            graph = load_account_graphs(session, [int(account_id)]).get(int(account_id)) or {}
            overview = graph.get("overview") if isinstance(graph.get("overview"), dict) else {}
            task = {
                "task_id": _text(overview.get("ba_extract_task_id")),
                "account_id": int(account_id),
                "email": _text(model.email),
                "status": _text(overview.get("ba_extract_status"), "idle"),
                "stage": _text(overview.get("ba_extract_stage")),
                "step": int(overview.get("ba_extract_step") or 0),
                "total": int(overview.get("ba_extract_total") or 7),
                "attempt": int(overview.get("ba_extract_attempt") or 0),
                "max_attempts": int(overview.get("ba_extract_max_attempts") or 20),
                "ba_token": _text(overview.get("pp_ba_token") or overview.get("ba_token")),
                "ba_url": _text(overview.get("ba_extract_ba_url")),
                "region_combo": _text(overview.get("ba_extract_region_combo")),
                "error": _text(overview.get("ba_extract_error")),
                "logs": overview.get("ba_extract_logs") if isinstance(overview.get("ba_extract_logs"), list) else [],
                "updated_at": float(overview.get("ba_extract_updated_at") or 0),
            }
        if settle_orphaned and _text(task.get("status")).lower() in ACTIVE_BA_EXTRACT_STATUSES:
            view = self._task_from_persisted(int(account_id), task)
            self._tasks[int(account_id)] = view
            self._append_event_locked(
                view,
                {
                    "type": "error",
                    "error": "服务重启后任务已结束，可重新提取",
                },
            )
            self._condition.notify_all()
            return view.public_dict()
        return task

    @staticmethod
    def _task_from_persisted(account_id: int, persisted: dict[str, Any]) -> BaExtractTaskView:
        return BaExtractTaskView(
            task_id=_text(persisted.get("task_id"), str(uuid.uuid4())),
            account_id=int(account_id),
            email=_text(persisted.get("email")),
            status=_text(persisted.get("status"), "idle"),
            stage=_text(persisted.get("stage")),
            step=int(persisted.get("step") or 0),
            total=int(persisted.get("total") or 7),
            attempt=int(persisted.get("attempt") or 0),
            max_attempts=int(persisted.get("max_attempts") or 20),
            ba_token=_text(persisted.get("ba_token")),
            ba_url=_text(persisted.get("ba_url")),
            region_combo=_text(persisted.get("region_combo")),
            error=_text(persisted.get("error")),
            logs=list(persisted.get("logs") or []) if isinstance(persisted.get("logs"), list) else [],
            updated_at=float(persisted.get("updated_at") or _now()),
        )

    def _run_task(
        self,
        task_id: str,
        account_id: int,
        account_ctx: dict[str, Any],
        payload: dict[str, Any],
        billing_country: str,
        promo_country: str,
    ) -> None:
        task = self._task_if_current(account_id, task_id)
        if not task:
            return
        self._emit(account_id, task_id, {"type": "started", "status": "running", "desc": "任务已开始", "max_attempts": task.max_attempts})

        def progress_cb(event: dict[str, Any]) -> None:
            if isinstance(event, dict):
                self._emit(account_id, task_id, event)

        try:
            if task.cancel_event.is_set():
                self._emit(
                    account_id,
                    task_id,
                    {"type": "done", "ok": False, "cancelled": True, "error": "任务已终止"},
                )
                return
            result = extract_ba_link(
                access_token=account_ctx["access_token"],
                cookies=account_ctx["cookies"],
                email=account_ctx["email"],
                billing_proxy=_text(payload.get("billing_proxy")),
                promo_proxy=_text(payload.get("promo_proxy")),
                billing_country=billing_country,
                promo_country=promo_country,
                billing_currency=_text(payload.get("billing_currency")),
                confirm_mode=_text(payload.get("confirm_mode"), "pm"),
                promo_create_mode=_text(payload.get("promo_create_mode"), "update_after_checkout"),
                max_attempts=task.max_attempts,
                progress_cb=progress_cb,
                cancel_check=task.cancel_event.is_set,
            )
            # 提取返回后若已请求取消，优先按终止收口（避免刚完成却被误标）
            current = self._task_if_current(account_id, task_id)
            if current and current.cancel_event.is_set() and not result.get("ok"):
                self._emit(
                    account_id,
                    task_id,
                    {"type": "done", "ok": False, "cancelled": True, "error": "任务已终止"},
                )
                return
            if result.get("ok"):
                ba_token = _text(result.get("ba_token"))
                result_billing_country = _text(result.get("billing_country"), billing_country).upper()
                result_promo_country = _text(result.get("promo_country"), promo_country).upper()
                region_combo = f"{result_billing_country}+{result_promo_country}" if result_billing_country or result_promo_country else ""
                if ba_token:
                    get_pp_plus_worker().save_ba_token(account_id, ba_token)
                    self._emit(account_id, task_id, {"type": "saved", "ba_token": ba_token})
                self._emit(account_id, task_id, {
                    "type": "done",
                    "ok": True,
                    "ba_token": ba_token,
                    "ba_url": _text(result.get("ba_url")),
                    "data": result.get("data") or {},
                    "billing_country": result_billing_country,
                    "promo_country": result_promo_country,
                    "region_combo": region_combo,
                })
            else:
                err = _text(result.get("error"), "提取 BA 链失败")
                cancelled = bool(task.cancel_event.is_set() or "取消" in err or "终止" in err)
                self._emit(account_id, task_id, {
                    "type": "done",
                    "ok": False,
                    "cancelled": cancelled,
                    "error": "任务已终止" if cancelled else err,
                    "data": result.get("data") or {},
                })
        except Exception as exc:
            msg = str(exc)
            cancelled = bool(task.cancel_event.is_set() or "取消" in msg or "终止" in msg)
            self._emit(
                account_id,
                task_id,
                {
                    "type": "done",
                    "ok": False,
                    "cancelled": cancelled,
                    "error": "任务已终止" if cancelled else msg,
                },
            )

    def _task_if_current(self, account_id: int, task_id: str) -> BaExtractTaskView | None:
        with self._lock:
            task = self._tasks.get(int(account_id))
            if task and task.task_id == task_id:
                return task
        return None

    def _emit(self, account_id: int, task_id: str, event: dict[str, Any]) -> None:
        with self._condition:
            task = self._tasks.get(int(account_id))
            if not task or task.task_id != task_id:
                return
            self._append_event_locked(task, event)
            self._condition.notify_all()

    def _append_event_locked(self, task: BaExtractTaskView, event: dict[str, Any]) -> None:
        event = dict(event or {})
        event.setdefault("type", "progress")
        event["account_id"] = task.account_id
        event["task_id"] = task.task_id
        event["time"] = _now()
        task.seq += 1
        event["seq"] = task.seq

        kind = _text(event.get("type"))
        if task.status == "cancelled" and kind in {"started", "progress", "saved"}:
            return
        if kind == "started":
            if event.get("reset"):
                task.logs = []
                # 保留本事件之前不要叠旧 events 展示；events 在下面仍 append 本条
                task.step = 0
                task.attempt = 0
                task.error = ""
                task.ba_url = ""
                # ba_token 不在 started 清空账号已有值；task 视图可空，由 public 读 overview
            task.status = _text(event.get("status"), "running")
            task.stage = _text(event.get("desc"), task.stage or "任务已开始")
            task.max_attempts = int(event.get("max_attempts") or task.max_attempts or 20)
        elif kind == "progress":
            task.status = "cancelling" if task.cancel_event.is_set() else "running"
            task.step = int(event.get("step") or task.step or 0)
            task.total = int(event.get("total") or task.total or 7)
            task.attempt = int(event.get("attempt") or task.attempt or 0)
            task.stage = _text(event.get("desc"), task.stage)
        elif kind == "saved":
            task.ba_token = _text(event.get("ba_token"), task.ba_token)
            task.stage = "已写回 BA 链"
        elif kind == "done":
            if event.get("ok"):
                task.status = "success"
                task.stage = _text(event.get("ba_token"), "提取成功")
                task.ba_token = _text(event.get("ba_token"), task.ba_token)
                task.ba_url = _text(event.get("ba_url"), task.ba_url)
                task.region_combo = _text(
                    event.get("region_combo"),
                    "+".join(part for part in (
                        _text(event.get("billing_country")).upper(),
                        _text(event.get("promo_country")).upper(),
                    ) if part),
                )
                task.error = ""
            elif event.get("cancelled") or "终止" in _text(event.get("error")) or "取消" in _text(event.get("error")):
                task.status = "cancelled"
                task.stage = _text(event.get("error"), "任务已终止")
                task.error = task.stage
            else:
                task.status = "error"
                task.stage = _text(event.get("error"), "提取失败")
                task.error = task.stage
        elif kind == "error":
            err = _text(event.get("error"), "错误")
            if task.cancel_event.is_set() or "终止" in err or "取消" in err:
                task.status = "cancelled"
                task.stage = "任务已终止"
                task.error = task.stage
            else:
                task.status = "error"
                task.stage = err
                task.error = task.stage

        log_line = self._timestamp_log_line(event, self._event_log_line(event))
        if log_line:
            task.logs.append(log_line)
            if len(task.logs) > 300:
                task.logs = task.logs[-300:]
        task.updated_at = float(event["time"])
        task.events.append(event)
        if len(task.events) > 500:
            task.events = task.events[-500:]
        self._persist_task_async(task)

    @staticmethod
    def _event_log_line(event: dict[str, Any]) -> str:
        kind = _text(event.get("type"))
        if kind == "progress":
            return f"步骤 {int(event.get('step') or 0)}/{int(event.get('total') or 7)}: {_text(event.get('desc'))}"
        if kind == "started":
            return _text(event.get("desc"), "任务已开始")
        if kind == "saved":
            return f"已写回 BA: {_text(event.get('ba_token'))}"
        if kind == "done":
            if event.get("ok"):
                combo = _text(event.get("region_combo"))
                if not combo:
                    combo = "+".join(part for part in (
                        _text(event.get("billing_country")).upper(),
                        _text(event.get("promo_country")).upper(),
                    ) if part)
                suffix = f" · {combo}" if combo else ""
                return f"成功: {_text(event.get('ba_token'))}{suffix}"
            if event.get("cancelled") or "终止" in _text(event.get("error")) or "取消" in _text(event.get("error")):
                return f"已终止: {_text(event.get('error'), '任务已终止')}"
            return f"失败: {_text(event.get('error'), '提取失败')}"
        if kind == "error":
            return f"错误: {_text(event.get('error'))}"
        return ""

    @staticmethod
    def _timestamp_log_line(event: dict[str, Any], line: str) -> str:
        line = _text(line)
        if not line:
            return ""
        if line.startswith("[") and "年" in line[:16] and "秒]" in line[:24]:
            return line
        return f"{_format_log_time(float(event.get('time') or _now()))} {line}"

    @staticmethod
    def _task_persist_snapshot(task: BaExtractTaskView) -> dict[str, Any]:
        updates = {
            "ba_extract_task_id": task.task_id,
            "ba_extract_status": task.status,
            "ba_extract_stage": task.stage,
            "ba_extract_step": task.step,
            "ba_extract_total": task.total,
            "ba_extract_attempt": task.attempt,
            "ba_extract_max_attempts": task.max_attempts,
            "ba_extract_error": task.error,
            "ba_extract_logs": list(task.logs),
            "ba_extract_updated_at": task.updated_at,
        }
        if task.region_combo:
            updates["ba_extract_region_combo"] = task.region_combo
        if task.ba_token:
            updates["pp_ba_token"] = task.ba_token
            updates["ba_token"] = task.ba_token
        if task.ba_url:
            updates["ba_extract_ba_url"] = task.ba_url
        return {
            "account_id": task.account_id,
            "updates": updates,
        }

    def _persist_task_async(self, task: BaExtractTaskView) -> None:
        snapshot = self._task_persist_snapshot(task)
        thread = threading.Thread(
            target=self._persist_task_snapshot,
            args=(snapshot,),
            name=f"ba-extract-persist-{task.account_id}",
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _persist_task_snapshot(snapshot: dict[str, Any]) -> None:
        try:
            with Session(engine) as session:
                model = session.get(AccountModel, int(snapshot.get("account_id") or 0))
                if model:
                    patch_account_graph(session, model, summary_updates=snapshot.get("updates") or {})
                    session.commit()
        except Exception:
            pass


_BA_EXTRACT_TASK_MANAGER = BaExtractTaskManager()


def get_ba_extract_task_manager() -> BaExtractTaskManager:
    return _BA_EXTRACT_TASK_MANAGER
