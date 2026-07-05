from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from application.sub2api_management import DEFAULT_TEST_MODEL, Sub2ApiManagementService


router = APIRouter(prefix="/sub2api-management", tags=["sub2api-management"])
service = Sub2ApiManagementService()


class BulkCheckRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    model_id: str = DEFAULT_TEST_MODEL
    concurrency: int = 10
    group_id: int | None = None
    status: str = ""
    search: str = ""
    tag_id: int | None = None
    untagged: bool = False


class ReloginRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    group_id: int | None = None
    workspace_ids: str = ""
    concurrency: int = 2


class TagRequest(BaseModel):
    name: str
    color: str = ""


class AccountTagRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    action: str = "add"


class ExportDataRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    timezone: str = "Asia/Shanghai"
    include_proxies: bool = True


def _export_account_count(account_ids: list[str]) -> int:
    return len([item for item in dict.fromkeys(str(value or "").strip() for value in account_ids) if item])


@router.get("/inventory")
def list_sub2api_inventory(
    group_id: int | None = None,
    status: str = "",
    search: str = "",
    tag_id: int | None = None,
    untagged: bool = False,
    page: int = 1,
    page_size: int = 10,
):
    try:
        return service.list_inventory(
            group_id=group_id,
            status=status,
            search=search,
            tag_id=tag_id,
            untagged=untagged,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"读取 Sub2API 远端数据失败: {exc}") from exc


@router.get("/tags")
def list_sub2api_tags():
    try:
        return service.list_tags()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"读取 Sub2API 标签失败: {exc}") from exc


@router.post("/tags")
def create_sub2api_tag(body: TagRequest):
    try:
        return service.create_tag(name=body.name, color=body.color)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"创建 Sub2API 标签失败: {exc}") from exc


@router.put("/tags/{tag_id}")
def update_sub2api_tag(tag_id: int, body: TagRequest):
    try:
        return service.update_tag(tag_id, name=body.name, color=body.color)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"更新 Sub2API 标签失败: {exc}") from exc


@router.delete("/tags/{tag_id}")
def delete_sub2api_tag(tag_id: int):
    try:
        return service.delete_tag(tag_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"删除 Sub2API 标签失败: {exc}") from exc


@router.post("/account-tags")
def update_sub2api_account_tags(body: AccountTagRequest):
    try:
        return service.update_account_tags(
            account_ids=body.account_ids,
            tag_ids=body.tag_ids,
            action=body.action,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"更新 Sub2API 账号标签失败: {exc}") from exc


@router.post("/export-data")
def export_sub2api_account_data(body: ExportDataRequest):
    try:
        if not body.tag_ids:
            raise ValueError("请选择导出标签")
        service.update_account_tags(
            account_ids=body.account_ids,
            tag_ids=body.tag_ids,
            action="add",
        )
        payload = service.export_accounts_data(
            account_ids=body.account_ids,
            timezone_name=body.timezone,
            include_proxies=body.include_proxies,
        )
        content = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        account_count = _export_account_count(body.account_ids)
        filename = f"sub2api-account-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{account_count}.json"
        return Response(
            content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"导出 Sub2API 账号数据失败: {exc}") from exc


@router.post("/bulk-check")
def bulk_check_sub2api_accounts(body: BulkCheckRequest):
    try:
        return service.bulk_check(
            account_ids=body.account_ids,
            model_id=body.model_id or DEFAULT_TEST_MODEL,
            concurrency=body.concurrency,
            group_id=body.group_id,
            status=body.status,
            search=body.search,
            tag_id=body.tag_id,
            untagged=body.untagged,
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
            group_id=body.group_id,
            status=body.status,
            search=body.search,
            tag_id=body.tag_id,
            untagged=body.untagged,
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
