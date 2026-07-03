from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.sub2api_management import DEFAULT_TEST_MODEL, Sub2ApiManagementService


router = APIRouter(prefix="/sub2api-management", tags=["sub2api-management"])
service = Sub2ApiManagementService()


class BulkCheckRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    model_id: str = DEFAULT_TEST_MODEL
    concurrency: int = 10


class ReloginRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    group_id: int | None = None
    workspace_ids: str = ""
    concurrency: int = 2


@router.get("/inventory")
def list_sub2api_inventory(group_id: int | None = None, status: str = "", search: str = ""):
    try:
        return service.list_inventory(group_id=group_id, status=status, search=search)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"读取 Sub2API 远端数据失败: {exc}") from exc


@router.post("/bulk-check")
def bulk_check_sub2api_accounts(body: BulkCheckRequest):
    try:
        return service.bulk_check(
            account_ids=body.account_ids,
            model_id=body.model_id or DEFAULT_TEST_MODEL,
            concurrency=body.concurrency,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Sub2API 批量测活失败: {exc}") from exc


@router.post("/bulk-check/stream")
def stream_bulk_check_sub2api_accounts(body: BulkCheckRequest):
    try:
        events = service.bulk_check_events(
            account_ids=body.account_ids,
            model_id=body.model_id or DEFAULT_TEST_MODEL,
            concurrency=body.concurrency,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Sub2API 批量测活失败: {exc}") from exc

    def generate():
        try:
            for event in events:
                yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
        except Exception as exc:
            payload = {"event": "bulk_failed", "ok": False, "message": str(exc)}
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/relogin-errors")
def relogin_sub2api_error_accounts(body: ReloginRequest):
    try:
        return service.relogin_error_accounts(
            account_ids=body.account_ids,
            group_id=body.group_id,
            workspace_ids=body.workspace_ids,
            concurrency=body.concurrency,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Sub2API 错误账号重新登录失败: {exc}") from exc


@router.post("/relogin-errors/stream")
def stream_relogin_sub2api_error_accounts(body: ReloginRequest):
    try:
        events = service.relogin_error_account_events(
            account_ids=body.account_ids,
            group_id=body.group_id,
            workspace_ids=body.workspace_ids,
            concurrency=body.concurrency,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Sub2API 错误账号重新登录失败: {exc}") from exc

    def generate():
        try:
            for event in events:
                yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
        except Exception as exc:
            payload = {"event": "relogin_failed", "ok": False, "message": str(exc)}
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
