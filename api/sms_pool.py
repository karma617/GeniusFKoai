"""SMS 号码池黑名单 HTTP 路由。

提供给前端 "账户 > 短信号码池" 子页使用：
- ``GET /sms-pool/blacklist``           列出全部
- ``POST /sms-pool/blacklist``          手动新增 / 累计 fail_count
- ``DELETE /sms-pool/blacklist/{phone}`` 把号码移出黑名单（恢复可用）
- ``DELETE /sms-pool/blacklist``        清空所有
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from infrastructure.sms_pool_repository import SmsPoolBlacklistRepository
from platforms.gopay import sms_channel

router = APIRouter(prefix="/sms-pool", tags=["sms-pool"])
_repo = SmsPoolBlacklistRepository()


class BlacklistAddRequest(BaseModel):
    phone: str
    relay_url: str = ""
    reason: str = "manual"
    error_code: str = ""
    task_id: str = ""
    error_message: str = ""


@router.get("/blacklist")
def list_blacklist():
    items = [item.to_dict() for item in _repo.list()]
    return {"items": items, "total": len(items)}


@router.post("/blacklist")
def add_blacklist(body: BlacklistAddRequest):
    record = _repo.add(
        phone=body.phone,
        relay_url=body.relay_url,
        reason=body.reason or "manual",
        error_code=body.error_code,
        task_id=body.task_id,
        error_message=body.error_message,
    )
    if not record:
        raise HTTPException(400, "phone 不可为空 / 格式无效")
    return record.to_dict()


@router.delete("/blacklist/{phone}")
def remove_blacklist(phone: str):
    ok = _repo.remove(phone)
    if not ok:
        raise HTTPException(404, "号码不在黑名单中")
    return {"ok": True, "phone": phone}


@router.delete("/blacklist")
def clear_blacklist():
    removed = _repo.clear()
    return {"ok": True, "removed": removed}


def _release_queue_payload() -> dict:
    snapshot = sms_channel.get_sms_release_queue_snapshot()
    logs = sms_channel.get_sms_release_logs(limit=250)
    snapshot["logs"] = logs
    snapshot["succeeded_recent"] = sum(1 for item in logs if item.get("status") == "success")
    snapshot["failed_recent"] = sum(1 for item in logs if item.get("status") == "failed")
    return snapshot


@router.get("/release-queue")
def get_release_queue():
    return _release_queue_payload()


@router.post("/release-queue/process")
def process_release_queue():
    attempted, released = sms_channel.force_process_smspool_release_queue()
    payload = _release_queue_payload()
    payload["ok"] = True
    payload["attempted"] = attempted
    payload["released"] = released
    return payload


@router.post("/release-queue/{order_id}/process")
def process_release_queue_item(order_id: str):
    attempted, released = sms_channel.force_process_smspool_release_queue(order_id=order_id)
    payload = _release_queue_payload()
    payload["ok"] = True
    payload["order_id"] = order_id
    payload["attempted"] = attempted
    payload["released"] = released
    if attempted <= 0:
        raise HTTPException(404, "release queue item not found")
    return payload


@router.delete("/release-queue/{order_id}")
def remove_release_queue_item(order_id: str):
    before = sms_channel.get_sms_release_queue_snapshot()
    sms_channel.remove_smspool_release(order_id)
    after = _release_queue_payload()
    if int(after.get("total") or 0) == int(before.get("total") or 0):
        raise HTTPException(404, "release queue item not found")
    after["ok"] = True
    after["removed"] = 1
    after["order_id"] = order_id
    return after


@router.delete("/release-logs")
def clear_release_logs():
    removed = sms_channel.clear_sms_release_logs()
    payload = _release_queue_payload()
    payload["ok"] = True
    payload["removed"] = removed
    return payload
