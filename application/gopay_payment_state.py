"""Durable idempotency and resource leases for GoPay Plus payments."""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session

from core.db import (
    GoPayAccountLeaseModel,
    GoPayPaymentAttemptModel,
    engine,
)

_SNAP_RE = re.compile(r"/snap/v[34]/redirection/([0-9a-f-]{36})", re.IGNORECASE)
_TABLE_LOCK = threading.Lock()
_TABLES_READY = False
_FINAL_PAYMENT_STATUSES = {"subscribed"}
_RECONCILE_STATUSES = {"charging", "payment_pending", "uncertain", "settled"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ensure_tables() -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _TABLE_LOCK:
        if _TABLES_READY:
            return
        GoPayPaymentAttemptModel.__table__.create(engine, checkfirst=True)
        GoPayAccountLeaseModel.__table__.create(engine, checkfirst=True)
        with engine.begin() as connection:
            columns = {
                str(row[1])
                for row in connection.exec_driver_sql("PRAGMA table_info(gopay_payment_attempts)").all()
            }
            if "proxy" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE gopay_payment_attempts ADD COLUMN proxy TEXT DEFAULT ''"
                )
        _TABLES_READY = True


def payment_attempt_key(chatgpt_account_id: int, midtrans_url: str = "") -> str:
    account_id = int(chatgpt_account_id or 0)
    if account_id > 0:
        return f"chatgpt:{account_id}"
    value = str(midtrans_url or "").strip()
    if not value:
        raise ValueError("占位付款必须提供 midtrans_url 才能建立幂等键")
    return "midtrans:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_snap_id(midtrans_url: str) -> str:
    match = _SNAP_RE.search(str(midtrans_url or ""))
    return match.group(1) if match else ""


def claim_payment_attempt(
    *,
    key: str,
    chatgpt_account_id: int,
    task_id: str,
    proxy: str = "",
    lease_seconds: int = 3600,
) -> dict[str, Any]:
    """Atomically claim an attempt and tell the caller whether to start or reconcile."""
    _ensure_tables()
    now = _utcnow()
    expires = now + timedelta(seconds=max(int(lease_seconds or 0), 60))
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.exec_driver_sql(
            "SELECT * FROM gopay_payment_attempts WHERE key = ?", (key,)
        ).mappings().first()
        if row is None:
            connection.exec_driver_sql(
                """
                INSERT INTO gopay_payment_attempts
                    (key, chatgpt_account_id, task_id, gopay_account_id, status,
                     snap_id, midtrans_url, proxy, transaction_status, amount, currency,
                     uncertain, error, lease_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, 0, 'claimed', '', '', ?, '', NULL, '', 0, '', ?, ?, ?)
                """,
                (key, int(chatgpt_account_id or 0), task_id, proxy, _iso(expires), _iso(now), _iso(now)),
            )
            connection.commit()
            return {"action": "start", "key": key, "status": "claimed"}

        current = dict(row)
        status = str(current.get("status") or "claimed")
        persisted_proxy = str(current.get("proxy") or "")
        safe_precharge_proxy_change = (
            status in {"failed_precharge", "cancelled"}
            and not str(current.get("midtrans_url") or "")
            and not str(current.get("snap_id") or "")
            and not bool(current.get("uncertain"))
        )
        if persisted_proxy and proxy and persisted_proxy != proxy:
            if not safe_precharge_proxy_change:
                connection.commit()
                return {"action": "proxy_mismatch", **current}
            connection.exec_driver_sql(
                "UPDATE gopay_payment_attempts SET proxy = ? WHERE key = ?", (proxy, key)
            )
            current["proxy"] = proxy
        if not persisted_proxy and proxy:
            connection.exec_driver_sql(
                "UPDATE gopay_payment_attempts SET proxy = ? WHERE key = ?", (proxy, key)
            )
            current["proxy"] = proxy
        if status in _FINAL_PAYMENT_STATUSES:
            connection.commit()
            return {"action": "already_complete", **current}

        lease_active = False
        try:
            lease_active = _as_utc(current.get("lease_expires_at")) > now
        except Exception:
            pass
        current_task = str(current.get("task_id") or "")
        owner_task_terminal = False
        if lease_active and current_task and current_task != task_id:
            try:
                owner_row = connection.exec_driver_sql(
                    "SELECT status FROM tasks WHERE id = ?", (current_task,)
                ).mappings().first()
                owner_status = str((owner_row or {}).get("status") or "")
                owner_task_terminal = owner_status in {
                    "succeeded", "failed", "interrupted", "cancelled"
                }
            except Exception:
                # If the task table is unavailable, preserve the lease fail-closed.
                owner_task_terminal = False
            if not owner_task_terminal and status not in {"failed_precharge", "cancelled"}:
                connection.commit()
                return {"action": "busy", **current}
        if owner_task_terminal and status in {"claimed", "preparing"}:
            status = "failed_precharge"
            current["status"] = status
            connection.exec_driver_sql(
                """
                UPDATE gopay_payment_attempts
                SET status = 'failed_precharge', uncertain = 0,
                    error = '原任务已终止，付款准备租约已自动回收', updated_at = ?
                WHERE key = ?
                """,
                (_iso(now), key),
            )

        action = "start"
        new_status = status
        if status in _RECONCILE_STATUSES:
            action = "reconcile"
        elif status == "checkout_ready" and current.get("midtrans_url"):
            action = "resume_payment"
        elif status not in {"claimed", "preparing", "failed_precharge", "cancelled"}:
            action = "blocked"
        if status in {"failed_precharge", "cancelled"}:
            new_status = "claimed"
            connection.exec_driver_sql(
                """
                UPDATE gopay_payment_attempts
                SET task_id = ?, status = 'claimed', gopay_account_id = 0,
                    snap_id = '', midtrans_url = '', transaction_status = '',
                    amount = NULL, currency = '', uncertain = 0, error = '',
                    lease_expires_at = ?, updated_at = ?
                WHERE key = ?
                """,
                (task_id, _iso(expires), _iso(now), key),
            )
        else:
            connection.exec_driver_sql(
                "UPDATE gopay_payment_attempts SET task_id = ?, lease_expires_at = ?, updated_at = ? WHERE key = ?",
                (task_id, _iso(expires), _iso(now), key),
            )
        connection.commit()
        current.update({"task_id": task_id, "status": new_status, "action": action})
        return current


def get_payment_attempt(key: str) -> dict[str, Any] | None:
    _ensure_tables()
    with Session(engine) as session:
        row = session.get(GoPayPaymentAttemptModel, key)
        if row is None:
            return None
        return {name: getattr(row, name) for name in GoPayPaymentAttemptModel.model_fields}


def update_payment_attempt(key: str, *, task_id: str = "", **updates: Any) -> dict[str, Any]:
    _ensure_tables()
    allowed = {
        "gopay_account_id", "status", "snap_id", "midtrans_url", "proxy",
        "transaction_status", "amount", "currency", "uncertain", "error",
    }
    with Session(engine) as session:
        row = session.get(GoPayPaymentAttemptModel, key)
        if row is None:
            raise RuntimeError(f"支付尝试不存在: {key}")
        if task_id and row.task_id and row.task_id != task_id:
            raise RuntimeError("支付尝试已由其它任务接管")
        for name, value in updates.items():
            if name in allowed:
                setattr(row, name, value)
        if task_id:
            row.task_id = task_id
        row.updated_at = _utcnow()
        row.lease_expires_at = _utcnow() + timedelta(hours=1)
        session.add(row)
        session.commit()
        session.refresh(row)
        return {name: getattr(row, name) for name in GoPayPaymentAttemptModel.model_fields}


def acquire_gopay_lease(
    *, account_id: int, owner_key: str, task_id: str, lease_seconds: int = 1800
) -> bool:
    _ensure_tables()
    now = _utcnow()
    expires = now + timedelta(seconds=max(int(lease_seconds or 0), 60))
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.exec_driver_sql(
            "SELECT owner_key, expires_at FROM gopay_account_leases WHERE account_id = ?",
            (int(account_id),),
        ).mappings().first()
        available = row is None
        if row is not None:
            try:
                available = str(row.get("owner_key") or "") == owner_key or _as_utc(row.get("expires_at")) <= now
            except Exception:
                available = True
        if not available:
            connection.commit()
            return False
        if row is None:
            connection.exec_driver_sql(
                """
                INSERT INTO gopay_account_leases
                    (account_id, owner_key, task_id, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(account_id), owner_key, task_id, _iso(expires), _iso(now), _iso(now)),
            )
        else:
            connection.exec_driver_sql(
                """
                UPDATE gopay_account_leases
                SET owner_key = ?, task_id = ?, expires_at = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (owner_key, task_id, _iso(expires), _iso(now), int(account_id)),
            )
        connection.commit()
        return True


def release_gopay_lease(*, account_id: int, owner_key: str) -> None:
    if not account_id:
        return
    _ensure_tables()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM gopay_account_leases WHERE account_id = ? AND owner_key = ?",
            (int(account_id), owner_key),
        )


def renew_gopay_lease(*, account_id: int, owner_key: str, lease_seconds: int = 1800) -> bool:
    _ensure_tables()
    now = _utcnow()
    expires = now + timedelta(seconds=max(int(lease_seconds or 0), 60))
    with engine.begin() as connection:
        result = connection.exec_driver_sql(
            """
            UPDATE gopay_account_leases SET expires_at = ?, updated_at = ?
            WHERE account_id = ? AND owner_key = ?
            """,
            (_iso(expires), _iso(now), int(account_id), owner_key),
        )
        return bool(result.rowcount)
