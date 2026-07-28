from __future__ import annotations

import json
import threading
import time
from queue import Queue
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.pp_plus_ba import get_pp_plus_worker

router = APIRouter(prefix="/pp-plus", tags=["pp-plus"])


class SettingsUpdateRequest(BaseModel):
    sms_provider: str | None = None
    sms_country: str | None = None
    sms_service_code: str | None = None
    flow_country: str | None = None
    max_card_attempts: int | None = None
    max_phone_changes: int | None = None
    proxy_enabled: bool | None = None
    proxy_mode: str | None = None
    proxy_api_url: str | None = None
    proxy_pool_text: str | None = None
    debug: bool | None = None


class BaTokenRequest(BaseModel):
    ba_token: str = Field(default="")


class StartRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)


@router.get("/status")
def pp_plus_status() -> dict[str, Any]:
    return get_pp_plus_worker().get_status()


@router.get("/settings")
def pp_plus_get_settings() -> dict[str, Any]:
    worker = get_pp_plus_worker()
    status = worker.get_status()
    return {
        "settings": status.get("settings") or {},
        "runtime": status.get("runtime") or {},
        "sms_service_code": status.get("sms_service_code") or "pp",
        "sms_provider_options": status.get("sms_provider_options") or worker.list_sms_provider_options(),
        "running": bool(status.get("running")),
        "stopping": bool(status.get("stopping")),
    }


@router.post("/settings")
def pp_plus_save_settings(body: SettingsUpdateRequest) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    settings = get_pp_plus_worker().save_settings(payload)
    status = get_pp_plus_worker().get_status()
    return {
        "ok": True,
        "settings": settings,
        "runtime": status.get("runtime") or {},
        "sms_service_code": status.get("sms_service_code") or "pp",
    }


@router.post("/start")
def pp_plus_start(body: StartRequest) -> dict[str, Any]:
    try:
        return get_pp_plus_worker().start(body.account_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stop")
def pp_plus_stop() -> dict[str, Any]:
    return get_pp_plus_worker().stop()


@router.get("/accounts/{account_id}")
def pp_plus_account_status(account_id: int) -> dict[str, Any]:
    data = get_pp_plus_worker().get_account_view(account_id)
    if not data:
        raise HTTPException(status_code=404, detail="account not found")
    return data


@router.post("/accounts/{account_id}/ba-token")
def pp_plus_save_ba_token(account_id: int, body: BaTokenRequest) -> dict[str, Any]:
    try:
        return get_pp_plus_worker().save_ba_token(account_id, body.ba_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/accounts/{account_id}/ba-token")
def pp_plus_delete_ba_token(account_id: int) -> dict[str, Any]:
    try:
        return get_pp_plus_worker().remove_ba_token(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class BaExtractStreamRequest(BaseModel):
    billing_proxy: str = ""
    promo_proxy: str = ""
    billing_country: str = ""
    promo_country: str = ""
    billing_currency: str = ""
    confirm_mode: str = "pm"
    promo_create_mode: str = "update_after_checkout"
    max_attempts: int = 20
    force: bool = False


def _sse_pack(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@router.post("/accounts/{account_id}/extract-ba-task")
def pp_plus_start_ba_extract_task(account_id: int, body: BaExtractStreamRequest) -> dict[str, Any]:
    from application.ba_extract_tasks import get_ba_extract_task_manager

    try:
        task = get_ba_extract_task_manager().start_task(int(account_id), body.model_dump())
        return {"ok": True, "task": task}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/extract-ba-task")
def pp_plus_get_ba_extract_task(account_id: int) -> dict[str, Any]:
    from application.ba_extract_tasks import get_ba_extract_task_manager

    task = get_ba_extract_task_manager().get_task(int(account_id))
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task": task}


@router.post("/accounts/{account_id}/extract-ba-task/cancel")
def pp_plus_cancel_ba_extract_task(account_id: int) -> dict[str, Any]:
    from application.ba_extract_tasks import get_ba_extract_task_manager

    try:
        task = get_ba_extract_task_manager().cancel_task(int(account_id))
        return {"ok": True, "task": task}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/accounts/{account_id}/extract-ba-events")
def pp_plus_ba_extract_events(account_id: int, after_seq: int = 0):
    from application.ba_extract_tasks import get_ba_extract_task_manager

    def generate():
        yield ": keep-alive" + "\n\n"
        for event in get_ba_extract_task_manager().stream_events(int(account_id), after_seq=int(after_seq or 0)):
            yield _sse_pack(event)
        yield ": done" + "\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/accounts/{account_id}/extract-ba-stream")
def pp_plus_extract_ba_stream(account_id: int, body: BaExtractStreamRequest):
    """SSE dual-IP BA extract with progress events."""
    from application.ba_link_extract import extract_ba_link, infer_region_from_proxy_text
    from application.pp_plus_ba import get_pp_plus_worker
    from core.db import AccountModel, engine
    from core.platform_accounts import build_platform_account
    from sqlmodel import Session

    with Session(engine) as session:
        model = session.get(AccountModel, int(account_id))
        if not model or str(model.platform or "").lower() != "chatgpt":
            raise HTTPException(status_code=404, detail="account not found or not chatgpt")
        account = build_platform_account(session, model)
        extra = account.extra or {}
        access_token = str(extra.get("access_token") or account.token or "").strip()
        cookies = str(extra.get("cookies") or "").strip()
        email = str(account.email or model.email or "").strip()

    if not access_token:
        raise HTTPException(status_code=400, detail="missing access_token")

    billing_country = infer_region_from_proxy_text(
        body.billing_proxy, default=(body.billing_country or "US")
    )
    promo_country = infer_region_from_proxy_text(
        body.promo_proxy, default=(body.promo_country or billing_country)
    )

    q: Queue = Queue()
    stop = {"done": False}
    terminal_emitted = {"done": False}
    cancel_event = threading.Event()

    def progress_cb(event: dict):
        if not isinstance(event, dict):
            return
        if event.get("type") == "done":
            terminal_emitted["done"] = True
        q.put(event)

    def worker():
        try:
            result = extract_ba_link(
                access_token=access_token,
                cookies=cookies,
                email=email,
                billing_proxy=body.billing_proxy or "",
                promo_proxy=body.promo_proxy or "",
                billing_country=billing_country,
                promo_country=promo_country,
                billing_currency=body.billing_currency or "",
                confirm_mode=body.confirm_mode or "pm",
                promo_create_mode=body.promo_create_mode or "update_after_checkout",
                max_attempts=int(body.max_attempts or 20),
                progress_cb=progress_cb,
                cancel_check=cancel_event.is_set,
            )
            if result.get("ok"):
                ba_token = str(result.get("ba_token") or "").strip()
                if ba_token:
                    try:
                        get_pp_plus_worker().save_ba_token(int(account_id), ba_token)
                        q.put({"type": "saved", "ba_token": ba_token, "account_id": int(account_id)})
                    except Exception as exc:
                        q.put({"type": "error", "error": f"save failed: {exc}", "ba_token": ba_token})
            if not terminal_emitted["done"]:
                if result.get("ok"):
                    q.put({
                        "type": "done",
                        "ok": True,
                        "ba_token": result.get("ba_token"),
                        "ba_url": result.get("ba_url"),
                        "data": result.get("data") or {},
                        "billing_country": result.get("billing_country") or billing_country,
                        "promo_country": result.get("promo_country") or promo_country,
                    })
                else:
                    q.put({
                        "type": "done",
                        "ok": False,
                        "error": result.get("error") or "extract failed",
                        "data": result.get("data") or {},
                    })
        except Exception as exc:
            q.put({"type": "done", "ok": False, "error": str(exc)})
        finally:
            stop["done"] = True
            q.put(None)

    thread = threading.Thread(target=worker, name=f"ba-extract-{account_id}", daemon=True)
    thread.start()

    def generate():
        try:
            yield ": keep-alive" + "\n\n"
            last_ping = time.time()
            while True:
                try:
                    item = q.get(timeout=1.0)
                except Exception:
                    item = "__timeout__"
                now = time.time()
                if item is None:
                    break
                if item == "__timeout__":
                    if now - last_ping >= 1.0:
                        yield ": keep-alive" + "\n\n"
                        last_ping = now
                    if stop["done"] and q.empty():
                        break
                    continue
                if isinstance(item, dict):
                    yield _sse_pack(item)
                    last_ping = now
            yield ": done" + "\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

