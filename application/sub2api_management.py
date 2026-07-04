from __future__ import annotations

import json
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from sqlmodel import Session, select

from core.db import Sub2ApiAccountTagLinkModel, Sub2ApiAccountTagModel, engine
from domain.accounts import AccountQuery, AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository
from platforms.chatgpt.sub2api_upload import (
    _normalize_proxy_id,
    _normalize_string,
    _request_json,
    login_sub2api,
)


DEFAULT_TEST_MODEL = "gpt-5.4-mini"
ERROR_STATUS = "error"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _short_error(value: Any, limit: int = 200) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _is_deactivated_error(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "account_deactivated",
            "deleted or deactivated",
            "account has been deleted",
            "account disabled",
            "account suspended",
        )
    )


def _is_phone_required(value: Any) -> bool:
    text = str(value or "").lower()
    return "add_phone" in text or "phone" in text or "手机号" in text


def _is_error_status(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"error", "errored", "failed", "invalid", "错误", "异常"}


def _is_usage_limit_error(value: Any) -> bool:
    text = str(value or "").lower()
    return (
        "usage_limit_reached" in text
        or "usage limit has been reached" in text
        or '"type":"usage_limit_reached"' in text
        or "api returned 429" in text
        or "http 429" in text
    )


def _account_plan_type(item: dict[str, Any]) -> str:
    credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for value in (
        item.get("plan_type"),
        credentials.get("plan_type"),
        credentials.get("planType"),
        extra.get("plan_type"),
        extra.get("planType"),
    ):
        text = _normalize_string(value).lower()
        if text:
            return text
    return ""


def _account_email(item: dict[str, Any]) -> str:
    credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for value in (
        item.get("email"),
        item.get("name"),
        credentials.get("email"),
        extra.get("email"),
        extra.get("name"),
    ):
        text = _normalize_string(value)
        if "@" in text:
            return text
    return _normalize_string(item.get("name"))


def _account_workspace_id(item: dict[str, Any]) -> str:
    credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for value in (
        item.get("workspace_id"),
        item.get("organization_id"),
        credentials.get("organization_id"),
        credentials.get("workspace_id"),
        extra.get("workspace_id"),
        extra.get("k12_workspace_id"),
    ):
        text = _normalize_string(value)
        if text:
            return text
    return ""


def _gmail_family_key(value: Any) -> str:
    email = _normalize_string(value).lower()
    if "@" not in email:
        return ""
    local_part, domain = email.split("@", 1)
    if domain not in {"gmail.com", "googlemail.com"}:
        return ""
    base = local_part.split("+", 1)[0].replace(".", "")
    return f"{base}@gmail.com" if base else ""


def _same_gmail_family(left: Any, right: Any) -> bool:
    left_key = _gmail_family_key(left)
    return bool(left_key and left_key == _gmail_family_key(right))


def _normalize_tag_name(value: Any) -> str:
    return _normalize_string(value).strip()[:40]


def _normalize_tag_color(value: Any) -> str:
    text = _normalize_string(value).strip()
    if len(text) > 32:
        text = text[:32]
    return text


def _serialize_tag(tag: Sub2ApiAccountTagModel, account_count: int = 0) -> dict[str, Any]:
    return {
        "id": int(tag.id or 0),
        "name": tag.name,
        "color": tag.color,
        "account_count": int(account_count or 0),
    }


def _extract_group_ids(item: dict[str, Any]) -> list[int]:
    raw_values: list[Any] = []
    for key in ("group_ids", "groups"):
        value = item.get(key)
        if isinstance(value, list):
            raw_values.extend(value)
    result: list[int] = []
    for value in raw_values:
        if isinstance(value, dict):
            value = value.get("id")
        group_id = _normalize_proxy_id(value)
        if group_id and group_id not in result:
            result.append(group_id)
    return result


def _normalize_groups(raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        group_id = _normalize_proxy_id(item.get("id"))
        if not group_id:
            continue
        result.append(
            {
                "id": group_id,
                "name": _normalize_string(item.get("name")) or f"#{group_id}",
                "platform": _normalize_string(item.get("platform") or "openai"),
            }
        )
    return result


def _items_from_accounts_payload(payload: Any) -> tuple[list[dict[str, Any]], int]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], len(payload)
    if not isinstance(payload, dict):
        return [], 0
    container = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items = container.get("items") if isinstance(container, dict) else []
    if not isinstance(items, list):
        items = container.get("accounts") if isinstance(container, dict) else []
    if not isinstance(items, list):
        items = []
    total = container.get("total") if isinstance(container, dict) else len(items)
    try:
        total_int = int(total)
    except Exception:
        total_int = len(items)
    return [item for item in items if isinstance(item, dict)], max(total_int, len(items))


def _normalize_account(item: dict[str, Any], group_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    account_id = _normalize_string(item.get("id"))
    group_ids = _extract_group_ids(item)
    groups = [group_by_id[group_id] for group_id in group_ids if group_id in group_by_id]
    credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
    return {
        "id": account_id,
        "name": _normalize_string(item.get("name")) or _account_email(item) or account_id,
        "email": _account_email(item),
        "status": _normalize_string(item.get("status")) or "unknown",
        "plan_type": _account_plan_type(item),
        "created_at": _normalize_string(item.get("created_at") or item.get("createdAt")),
        "last_used_at": _normalize_string(
            item.get("last_used_at")
            or item.get("lastUsedAt")
            or item.get("last_use_time")
            or item.get("last_used")
        ),
        "group_ids": group_ids,
        "groups": groups,
        "workspace_id": _account_workspace_id(item),
        "has_refresh_token": bool(credentials.get("refresh_token")),
        "raw": item,
    }


@dataclass(slots=True)
class Sub2ApiContext:
    origin: str
    token: str


class Sub2ApiManagementService:
    def __init__(
        self,
        repository: AccountsRepository | None = None,
        *,
        browser_relogin: Callable[..., dict[str, Any] | None] | None = None,
    ):
        self.repository = repository or AccountsRepository()
        self.browser_relogin = browser_relogin

    def _context(self) -> Sub2ApiContext:
        from core.config_store import config_store

        origin, token = login_sub2api(
            config_store.get("sub2api_url", ""),
            config_store.get("sub2api_email", ""),
            config_store.get("sub2api_password", ""),
        )
        return Sub2ApiContext(origin=origin, token=token)

    def _list_tags_for_origin(self, session: Session, origin: str) -> list[dict[str, Any]]:
        tags = session.exec(
            select(Sub2ApiAccountTagModel)
            .where(Sub2ApiAccountTagModel.origin == origin)
            .order_by(Sub2ApiAccountTagModel.name)
        ).all()
        links = session.exec(
            select(Sub2ApiAccountTagLinkModel).where(Sub2ApiAccountTagLinkModel.origin == origin)
        ).all()
        counts: dict[int, int] = {}
        for link in links:
            counts[int(link.tag_id or 0)] = counts.get(int(link.tag_id or 0), 0) + 1
        return [_serialize_tag(tag, counts.get(int(tag.id or 0), 0)) for tag in tags]

    def _load_account_tags(self, origin: str, account_ids: list[str]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        normalized_ids = [str(item) for item in account_ids if _normalize_string(item)]
        with Session(engine) as session:
            tags = self._list_tags_for_origin(session, origin)
            tag_by_id = {int(item["id"]): item for item in tags}
            if not normalized_ids:
                return {}, tags
            links = session.exec(
                select(Sub2ApiAccountTagLinkModel)
                .where(Sub2ApiAccountTagLinkModel.origin == origin)
                .where(Sub2ApiAccountTagLinkModel.account_id.in_(normalized_ids))
            ).all()
        by_account: dict[str, list[dict[str, Any]]] = {account_id: [] for account_id in normalized_ids}
        for link in links:
            tag = tag_by_id.get(int(link.tag_id or 0))
            if tag:
                by_account.setdefault(str(link.account_id), []).append({k: v for k, v in tag.items() if k != "account_count"})
        return by_account, tags

    def list_tags(self) -> dict[str, Any]:
        ctx = self._context()
        with Session(engine) as session:
            tags = self._list_tags_for_origin(session, ctx.origin)
        return {"ok": True, "tags": tags}

    def create_tag(self, *, name: str, color: str = "") -> dict[str, Any]:
        ctx = self._context()
        tag_name = _normalize_tag_name(name)
        if not tag_name:
            raise ValueError("标签名称不能为空")
        tag_color = _normalize_tag_color(color)
        with Session(engine) as session:
            existing = session.exec(
                select(Sub2ApiAccountTagModel)
                .where(Sub2ApiAccountTagModel.origin == ctx.origin)
                .where(Sub2ApiAccountTagModel.name == tag_name)
            ).first()
            if existing:
                raise ValueError(f"标签已存在: {tag_name}")
            tag = Sub2ApiAccountTagModel(origin=ctx.origin, name=tag_name, color=tag_color)
            session.add(tag)
            session.commit()
            session.refresh(tag)
            return {"ok": True, "tag": _serialize_tag(tag)}

    def update_tag(self, tag_id: int, *, name: str, color: str = "") -> dict[str, Any]:
        ctx = self._context()
        tag_name = _normalize_tag_name(name)
        if not tag_name:
            raise ValueError("标签名称不能为空")
        tag_color = _normalize_tag_color(color)
        with Session(engine) as session:
            tag = session.get(Sub2ApiAccountTagModel, int(tag_id))
            if not tag or tag.origin != ctx.origin:
                raise ValueError("标签不存在")
            duplicate = session.exec(
                select(Sub2ApiAccountTagModel)
                .where(Sub2ApiAccountTagModel.origin == ctx.origin)
                .where(Sub2ApiAccountTagModel.name == tag_name)
                .where(Sub2ApiAccountTagModel.id != int(tag_id))
            ).first()
            if duplicate:
                raise ValueError(f"标签已存在: {tag_name}")
            tag.name = tag_name
            tag.color = tag_color
            tag.updated_at = _utcnow()
            session.add(tag)
            session.commit()
            session.refresh(tag)
            return {"ok": True, "tag": _serialize_tag(tag)}

    def delete_tag(self, tag_id: int) -> dict[str, Any]:
        ctx = self._context()
        with Session(engine) as session:
            tag = session.get(Sub2ApiAccountTagModel, int(tag_id))
            if not tag or tag.origin != ctx.origin:
                raise ValueError("标签不存在")
            links = session.exec(
                select(Sub2ApiAccountTagLinkModel)
                .where(Sub2ApiAccountTagLinkModel.origin == ctx.origin)
                .where(Sub2ApiAccountTagLinkModel.tag_id == int(tag_id))
            ).all()
            for link in links:
                session.delete(link)
            session.delete(tag)
            session.commit()
        return {"ok": True}

    def update_account_tags(
        self,
        *,
        account_ids: list[str],
        tag_ids: list[int],
        action: str = "add",
    ) -> dict[str, Any]:
        ctx = self._context()
        normalized_account_ids = [_normalize_string(item) for item in account_ids if _normalize_string(item)]
        normalized_tag_ids: list[int] = []
        for item in tag_ids:
            try:
                tag_id = int(item or 0)
            except Exception:
                continue
            if tag_id > 0 and tag_id not in normalized_tag_ids:
                normalized_tag_ids.append(tag_id)
        normalized_tag_ids.sort()
        if not normalized_account_ids:
            raise ValueError("请选择账号")
        if not normalized_tag_ids:
            raise ValueError("请选择标签")
        action_key = _normalize_string(action).lower() or "add"
        if action_key not in {"add", "remove"}:
            raise ValueError("标签操作必须是 add 或 remove")

        with Session(engine) as session:
            tags = session.exec(
                select(Sub2ApiAccountTagModel)
                .where(Sub2ApiAccountTagModel.origin == ctx.origin)
                .where(Sub2ApiAccountTagModel.id.in_(normalized_tag_ids))
            ).all()
            found_ids = {int(tag.id or 0) for tag in tags}
            missing = [tag_id for tag_id in normalized_tag_ids if tag_id not in found_ids]
            if missing:
                raise ValueError(f"标签不存在: {', '.join(str(item) for item in missing)}")

            existing_links = session.exec(
                select(Sub2ApiAccountTagLinkModel)
                .where(Sub2ApiAccountTagLinkModel.origin == ctx.origin)
                .where(Sub2ApiAccountTagLinkModel.account_id.in_(normalized_account_ids))
                .where(Sub2ApiAccountTagLinkModel.tag_id.in_(normalized_tag_ids))
            ).all()
            existing_pairs = {(str(link.account_id), int(link.tag_id or 0)) for link in existing_links}
            changed = 0
            if action_key == "add":
                for account_id in normalized_account_ids:
                    for tag_id in normalized_tag_ids:
                        pair = (account_id, tag_id)
                        if pair in existing_pairs:
                            continue
                        session.add(Sub2ApiAccountTagLinkModel(origin=ctx.origin, account_id=account_id, tag_id=tag_id))
                        changed += 1
            else:
                for link in existing_links:
                    session.delete(link)
                    changed += 1
            session.commit()
        return {"ok": True, "changed": changed}

    def export_accounts_data(
        self,
        *,
        account_ids: list[str],
        timezone_name: str = "Asia/Shanghai",
        include_proxies: bool = True,
    ) -> dict[str, Any]:
        ctx = self._context()
        normalized_account_ids: list[str] = []
        for item in account_ids:
            account_id = _normalize_string(item)
            if account_id and account_id not in normalized_account_ids:
                normalized_account_ids.append(account_id)
        if not normalized_account_ids:
            raise ValueError("请选择要导出的账号")

        query: dict[str, str] = {
            "ids": ",".join(normalized_account_ids),
            "timezone": _normalize_string(timezone_name) or "Asia/Shanghai",
        }
        if not include_proxies:
            query["include_proxies"] = "false"
        payload = _request_json(
            ctx.origin,
            f"/api/v1/admin/accounts/data?{urlencode(query, safe=',')}",
            token=ctx.token,
            timeout=90,
        )
        if not isinstance(payload, dict):
            raise ValueError("Sub2API 导出接口返回格式无效")
        return payload

    def list_inventory(
        self,
        *,
        group_id: int | None = None,
        status: str = "",
        search: str = "",
        tag_id: int | None = None,
    ) -> dict[str, Any]:
        ctx = self._context()
        groups = _normalize_groups(
            _request_json(ctx.origin, "/api/v1/admin/groups/all", token=ctx.token)
        )
        group_by_id = {int(group["id"]): group for group in groups}
        accounts = [
            _normalize_account(item, group_by_id)
            for item in self._fetch_all_accounts(ctx)
        ]
        tags_by_account, tags = self._load_account_tags(ctx.origin, [item["id"] for item in accounts])
        for item in accounts:
            item["tags"] = tags_by_account.get(item["id"], [])
        if group_id:
            accounts = [item for item in accounts if int(group_id) in item.get("group_ids", [])]
        status_filter = _normalize_string(status).lower()
        if status_filter and status_filter != "all":
            accounts = [item for item in accounts if _normalize_string(item.get("status")).lower() == status_filter]
        if tag_id:
            accounts = [
                item
                for item in accounts
                if any(int(tag.get("id") or 0) == int(tag_id) for tag in item.get("tags", []))
            ]
        search_filter = _normalize_string(search).lower()
        if search_filter:
            accounts = [
                item
                for item in accounts
                if search_filter in _normalize_string(item.get("name")).lower()
                or search_filter in _normalize_string(item.get("email")).lower()
            ]
        return {
            "ok": True,
            "groups": groups,
            "tags": tags,
            "accounts": accounts,
            "total": len(accounts),
        }

    def _fetch_all_accounts(self, ctx: Sub2ApiContext, *, page_size: int = 100) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = _request_json(
                ctx.origin,
                f"/api/v1/admin/accounts?page={page}&page_size={page_size}",
                token=ctx.token,
                timeout=45,
            )
            items, total = _items_from_accounts_payload(payload)
            if not items:
                break
            all_items.extend(items)
            if len(all_items) >= total or len(items) < page_size:
                break
            page += 1
        return all_items

    def set_account_error(self, ctx: Sub2ApiContext, account_id: str) -> bool:
        return self._update_account_status(ctx, account_id, ERROR_STATUS)

    def _update_account_status(self, ctx: Sub2ApiContext, account_id: str, status: str) -> bool:
        path = f"/api/v1/admin/accounts/{account_id}"
        payload = {"status": status}
        try:
            _request_json(ctx.origin, path, method="PATCH", token=ctx.token, body=payload)
            return True
        except Exception:
            try:
                _request_json(ctx.origin, path, method="PUT", token=ctx.token, body=payload)
                return True
            except Exception:
                return False

    def delete_account(self, ctx: Sub2ApiContext, account_id: str) -> tuple[bool, str]:
        try:
            _request_json(ctx.origin, f"/api/v1/admin/accounts/{account_id}", method="DELETE", token=ctx.token)
            return True, "deleted"
        except Exception as exc:
            return False, str(exc)

    def test_account(
        self,
        ctx: Sub2ApiContext,
        account_id: str,
        *,
        model_id: str = DEFAULT_TEST_MODEL,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[str, str]:
        from curl_cffi import requests as cffi_requests

        def emit(event: dict[str, Any]) -> None:
            if callable(event_callback):
                event_callback({"account_id": account_id, **event})

        headers = {
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.token}",
        }
        emit({"event": "request_started", "message": f"发起模型测活请求 model={model_id}"})
        try:
            response = cffi_requests.post(
                f"{ctx.origin}/api/v1/admin/accounts/{account_id}/test",
                headers=headers,
                data=json.dumps({"model_id": model_id}, ensure_ascii=False).encode("utf-8"),
                timeout=60,
                impersonate="chrome110",
                stream=True,
            )
        except Exception as exc:
            reason = f"test request failed: {exc}"
            emit({"event": "request_failed", "result": "skipped", "message": reason})
            return "skipped", reason

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code != 200:
            text = getattr(response, "text", "") or ""
            reason = _short_error(f"HTTP {status_code}: {text}")
            if status_code == 429 or _is_usage_limit_error(reason):
                emit({"event": "rate_limited", "result": "rate_limited", "message": reason})
                return "rate_limited", reason
            emit({"event": "request_failed", "result": "skipped", "message": reason})
            return "skipped", reason

        saw_terminal = False
        saw_line = False
        try:
            line_iter = response.iter_lines()
        except Exception:
            line_iter = []
        for raw_line in line_iter:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line or "")
            line = line.strip()
            if not line:
                continue
            saw_line = True
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue
            event_type = str(event.get("type") or "")
            text = _normalize_string(
                event.get("content")
                or event.get("text")
                or event.get("message")
                or event.get("delta")
                or event.get("response")
            )
            if text:
                emit({"event": "model_message", "message": _short_error(text, 500), "raw": event})
            if event_type == "test_complete":
                saw_terminal = True
                message = _short_error(event.get("error") or event.get("text") or text or "test completed", 500)
                if event.get("success"):
                    emit({"event": "completed", "result": "ok", "message": message})
                    return "ok", message
                if _is_usage_limit_error(message):
                    emit({"event": "completed", "result": "rate_limited", "message": message})
                    return "rate_limited", message
                emit({"event": "completed", "result": "dead", "message": message})
                return "dead", message
            if event_type == "error":
                saw_terminal = True
                message = _short_error(event.get("error") or event.get("text") or text, 500)
                if _is_usage_limit_error(message):
                    emit({"event": "completed", "result": "rate_limited", "message": message})
                    return "rate_limited", message
                emit({"event": "completed", "result": "dead", "message": message})
                return "dead", message

        if not saw_line:
            text = getattr(response, "text", "") or ""
            if text:
                for line in text.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except Exception:
                        continue
                    message = _short_error(event.get("error") or event.get("text") or event.get("message"), 500)
                    if message:
                        emit({"event": "model_message", "message": message, "raw": event})
                    if str(event.get("type") or "") == "test_complete":
                        if event.get("success"):
                            emit({"event": "completed", "result": "ok", "message": message or "test completed"})
                            return "ok", message or "test completed"
                        if _is_usage_limit_error(message):
                            emit({"event": "completed", "result": "rate_limited", "message": message})
                            return "rate_limited", message
                        emit({"event": "completed", "result": "dead", "message": message})
                        return "dead", message
                    if str(event.get("type") or "") == "error":
                        if _is_usage_limit_error(message):
                            emit({"event": "completed", "result": "rate_limited", "message": message})
                            return "rate_limited", message
                        emit({"event": "completed", "result": "dead", "message": message})
                        return "dead", message
        reason = "no terminal SSE event" if not saw_terminal else "missing terminal result"
        emit({"event": "request_finished", "result": "skipped", "message": reason})
        return "skipped", reason

    @staticmethod
    def _empty_summary() -> dict[str, int]:
        return {"ok": 0, "failed": 0, "rate_limited": 0, "skipped": 0, "marked_error": 0}

    @staticmethod
    def _apply_check_result_to_summary(summary: dict[str, int], result: str, marked_error: bool) -> None:
        if result == "ok":
            summary["ok"] += 1
        elif result == "dead":
            summary["failed"] += 1
        elif result == "rate_limited":
            summary["rate_limited"] += 1
            summary["skipped"] += 1
        else:
            summary["skipped"] += 1
        if marked_error:
            summary["marked_error"] += 1

    def bulk_check(
        self,
        *,
        account_ids: list[str],
        model_id: str = DEFAULT_TEST_MODEL,
        concurrency: int = 10,
    ) -> dict[str, Any]:
        ctx = self._context()
        unique_ids = [item for item in dict.fromkeys(_normalize_string(x) for x in account_ids) if item]
        if not unique_ids:
            raise ValueError("请选择至少一个 Sub2API 账号")
        max_workers = max(1, min(int(concurrency or 10), 50, len(unique_ids)))
        lock = threading.Lock()
        summary = self._empty_summary()
        results: list[dict[str, Any]] = []

        def worker(account_id: str) -> dict[str, Any]:
            result, reason = self.test_account(ctx, account_id, model_id=model_id)
            marked_error = False
            if result == "dead":
                marked_error = self.set_account_error(ctx, account_id)
            with lock:
                self._apply_check_result_to_summary(summary, result, marked_error)
            return {
                "account_id": account_id,
                "result": result,
                "reason": reason,
                "marked_error": marked_error,
            }

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker, account_id) for account_id in unique_ids]
            for future in as_completed(futures):
                results.append(future.result())
        return {"ok": True, "summary": summary, "results": sorted(results, key=lambda item: unique_ids.index(item["account_id"]))}

    def bulk_check_events(
        self,
        *,
        account_ids: list[str],
        model_id: str = DEFAULT_TEST_MODEL,
        concurrency: int = 10,
    ):
        ctx = self._context()
        unique_ids = [item for item in dict.fromkeys(_normalize_string(x) for x in account_ids) if item]
        if not unique_ids:
            raise ValueError("请选择至少一个 Sub2API 账号")
        max_workers = max(1, min(int(concurrency or 10), 50, len(unique_ids)))
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        lock = threading.Lock()
        summary = self._empty_summary()
        results: list[dict[str, Any]] = []
        order = {account_id: index for index, account_id in enumerate(unique_ids)}

        yield {
            "event": "bulk_started",
            "total": len(unique_ids),
            "concurrency": max_workers,
            "model_id": model_id,
            "summary": dict(summary),
        }

        def emit(event: dict[str, Any]) -> None:
            events.put(event)

        def worker(account_id: str) -> None:
            result = "skipped"
            reason = ""
            marked_error = False
            try:
                result, reason = self.test_account(
                    ctx,
                    account_id,
                    model_id=model_id,
                )
                if result == "dead":
                    marked_error = self.set_account_error(ctx, account_id)
            except Exception as exc:
                result = "skipped"
                reason = f"worker failed: {exc}"
            item = {
                "account_id": account_id,
                "result": result,
                "reason": reason,
                "marked_error": marked_error,
            }
            with lock:
                self._apply_check_result_to_summary(summary, result, marked_error)
                results.append(item)
                current_summary = dict(summary)
            emit({
                "event": "account_finished",
                "account_id": account_id,
                "result": result,
                "reason": reason,
                "marked_error": marked_error,
                "summary": current_summary,
            })

        def run_all() -> None:
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(worker, account_id) for account_id in unique_ids]
                    for future in as_completed(futures):
                        future.result()
                ordered = sorted(results, key=lambda item: order.get(item["account_id"], 0))
                events.put({"event": "bulk_finished", "ok": True, "summary": dict(summary), "results": ordered})
            except Exception as exc:
                events.put({"event": "bulk_failed", "ok": False, "message": str(exc), "summary": dict(summary)})
            finally:
                events.put(None)

        thread = threading.Thread(target=run_all, name="sub2api-bulk-check-stream", daemon=True)
        thread.start()
        while True:
            event = events.get()
            if event is None:
                break
            yield event

    def relogin_error_accounts(
        self,
        *,
        account_ids: list[str] | None = None,
        group_id: int | None = None,
        workspace_ids: str = "",
        concurrency: int = 2,
    ) -> dict[str, Any]:
        ctx = self._context()
        groups = _normalize_groups(_request_json(ctx.origin, "/api/v1/admin/groups/all", token=ctx.token))
        group_by_id = {int(group["id"]): group for group in groups}
        selected = [
            _normalize_account(item, group_by_id)
            for item in self._fetch_all_accounts(ctx)
        ]
        wanted_ids = {item for item in (account_ids or []) if _normalize_string(item)}
        if wanted_ids:
            selected = [item for item in selected if item["id"] in wanted_ids]
        else:
            selected = [item for item in selected if _is_error_status(item.get("status"))]
            if group_id:
                selected = [item for item in selected if int(group_id) in item.get("group_ids", [])]
        if not selected:
            return {"ok": True, "summary": {"total": 0}, "results": []}

        summary = {
            "total": len(selected),
            "deleted": 0,
            "replaced": 0,
            "phone_skipped": 0,
            "free_skipped": 0,
            "skipped": 0,
            "failed": 0,
        }

        def worker(remote_account: dict[str, Any]) -> dict[str, Any]:
            try:
                result = self._relogin_one(ctx, remote_account, workspace_ids=workspace_ids)
            except Exception as exc:
                result = self._delete_relogin_failed_account(ctx, remote_account["id"], f"重新登录任务异常: {exc}")
            status = str(result.get("status") or "")
            if status == "deleted":
                summary["deleted"] += 1
            elif status == "replaced":
                summary["replaced"] += 1
            elif status == "phone_skipped":
                summary["phone_skipped"] += 1
            elif status == "free_skipped":
                summary["free_skipped"] += 1
            elif status == "failed":
                summary["failed"] += 1
            else:
                summary["skipped"] += 1
            return result

        results: list[dict[str, Any]] = []
        for account in selected:
            results.append(worker(account))
        order = {item["id"]: index for index, item in enumerate(selected)}
        return {"ok": True, "summary": summary, "results": sorted(results, key=lambda item: order.get(item["account_id"], 0))}

    def relogin_error_account_events(
        self,
        *,
        account_ids: list[str] | None = None,
        group_id: int | None = None,
        workspace_ids: str = "",
        concurrency: int = 2,
    ):
        ctx = self._context()
        groups = _normalize_groups(_request_json(ctx.origin, "/api/v1/admin/groups/all", token=ctx.token))
        group_by_id = {int(group["id"]): group for group in groups}
        selected = [
            _normalize_account(item, group_by_id)
            for item in self._fetch_all_accounts(ctx)
        ]
        wanted_ids = {item for item in (account_ids or []) if _normalize_string(item)}
        if wanted_ids:
            selected = [item for item in selected if item["id"] in wanted_ids]
        else:
            selected = [item for item in selected if _is_error_status(item.get("status"))]
            if group_id:
                selected = [item for item in selected if int(group_id) in item.get("group_ids", [])]

        summary = {
            "total": len(selected),
            "deleted": 0,
            "replaced": 0,
            "phone_skipped": 0,
            "free_skipped": 0,
            "skipped": 0,
            "failed": 0,
        }
        if not selected:
            yield {"event": "relogin_finished", "ok": True, "summary": summary, "results": []}
            return

        max_workers = 1
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        lock = threading.Lock()
        results: list[dict[str, Any]] = []
        order = {item["id"]: index for index, item in enumerate(selected)}

        yield {
            "event": "relogin_started",
            "total": len(selected),
            "concurrency": max_workers,
            "summary": dict(summary),
        }

        def emit(event: dict[str, Any]) -> None:
            events.put(event)

        def worker(remote_account: dict[str, Any]) -> None:
            account_id = remote_account["id"]

            def log(message: str) -> None:
                emit({
                    "event": "relogin_log",
                    "account_id": account_id,
                    "message": str(message),
                })

            try:
                result = self._relogin_one(ctx, remote_account, workspace_ids=workspace_ids, log_fn=log)
            except Exception as exc:
                result = self._delete_relogin_failed_account(ctx, account_id, f"重新登录任务异常: {exc}", log_fn=log)
            with lock:
                status = str(result.get("status") or "")
                if status == "deleted":
                    summary["deleted"] += 1
                elif status == "replaced":
                    summary["replaced"] += 1
                elif status == "phone_skipped":
                    summary["phone_skipped"] += 1
                elif status == "free_skipped":
                    summary["free_skipped"] += 1
                elif status == "failed":
                    summary["failed"] += 1
                else:
                    summary["skipped"] += 1
                results.append(result)
                current_summary = dict(summary)
            emit({
                "event": "relogin_account_finished",
                "account_id": account_id,
                "status": status,
                "message": result.get("message") or "",
                "summary": current_summary,
            })

        def run_all() -> None:
            try:
                for account in selected:
                    worker(account)
                ordered = sorted(results, key=lambda item: order.get(item["account_id"], 0))
                events.put({"event": "relogin_finished", "ok": True, "summary": dict(summary), "results": ordered})
            except Exception as exc:
                events.put({"event": "relogin_failed", "ok": False, "message": str(exc), "summary": dict(summary)})
            finally:
                events.put(None)

        thread = threading.Thread(target=run_all, name="sub2api-relogin-errors-stream", daemon=True)
        thread.start()
        while True:
            event = events.get()
            if event is None:
                break
            yield event

    def _relogin_one(
        self,
        ctx: Sub2ApiContext,
        remote_account: dict[str, Any],
        *,
        workspace_ids: str = "",
        log_fn: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        log = log_fn or (lambda _message: None)
        account_id = remote_account["id"]
        email = remote_account.get("email") or remote_account.get("name")
        log(f"开始处理错误账号 {email or account_id}")
        local = self._find_local_account(str(email or ""), log_fn=log)
        if not local:
            return self._delete_relogin_failed_account(ctx, account_id, f"本地未找到账号 {email}", log_fn=log)
        local = self._prepare_gmail_alias_relogin_account(local, str(email or ""), log)
        if not local.password:
            return self._delete_relogin_failed_account(ctx, account_id, "本地账号缺少密码", log_fn=log)

        logs: list[str] = []
        log(
            f"本地账号匹配成功: local_id={local.id} plan={_account_plan_type(remote_account.get('raw') or remote_account) or _account_plan_type(remote_account) or 'unknown'}"
        )
        login_result = self._run_protocol_relogin(local, logs, log_fn=log)
        login_text = json.dumps(login_result or {}, ensure_ascii=False) + "\n" + "\n".join(logs)
        if _is_deactivated_error(login_text):
            ok, msg = self.delete_account(ctx, account_id)
            return {
                "account_id": account_id,
                "status": "deleted" if ok else "failed",
                "message": "账号已封禁，已删除远端账号" if ok else f"账号已封禁，但远端删除失败: {msg}",
            }
        if _is_phone_required(login_text):
            return self._delete_relogin_failed_account(ctx, account_id, "登录要求手机接码", log_fn=log)
        if not isinstance(login_result, dict) or not login_result.get("session"):
            return self._delete_relogin_failed_account(ctx, account_id, "重新登录未拿到 ChatGPT session", log_fn=log)

        plan_type = _account_plan_type(remote_account.get("raw") or remote_account) or _account_plan_type(remote_account)
        if plan_type == "free":
            return {"account_id": account_id, "status": "free_skipped", "message": "free 类型账号登录正常，按规则跳过"}
        if plan_type != "k12":
            return {"account_id": account_id, "status": "skipped", "message": f"账号类型 {plan_type or 'unknown'} 不处理"}

        workspace_text = _normalize_string(workspace_ids) or remote_account.get("workspace_id") or _normalize_string(local.overview.get("k12_workspace_id") if isinstance(local.overview, dict) else "")
        if not workspace_text:
            return self._delete_relogin_failed_account(ctx, account_id, "K12 账号缺少 workspace_id，无法替换", log_fn=log)

        replaced = self._replace_k12_account(ctx, remote_account, login_result, workspace_text, log_fn=log)
        if replaced.get("ok"):
            self._persist_k12_session(local.id, replaced)
            return {"account_id": account_id, "status": "replaced", "message": replaced.get("message", "K12 session 已替换")}
        message = replaced.get("message", "K12 session 替换失败")
        if replaced.get("remote_deleted"):
            return {"account_id": account_id, "status": "deleted", "message": message}
        return self._delete_relogin_failed_account(ctx, account_id, message, log_fn=log)

    def _delete_relogin_failed_account(
        self,
        ctx: Sub2ApiContext,
        account_id: str,
        reason: str,
        *,
        log_fn: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        ok, msg = self.delete_account(ctx, account_id)
        if ok:
            message = f"{reason}，已删除远端账号"
            if callable(log_fn):
                log_fn(message)
            return {"account_id": account_id, "status": "deleted", "message": message}
        message = f"{reason}，远端删除失败: {msg}"
        if callable(log_fn):
            log_fn(message)
        return {"account_id": account_id, "status": "failed", "message": message}

    def _find_local_account(self, email: str, log_fn: Callable[[str], None] | None = None):
        normalized = _normalize_string(email).lower()
        if not normalized:
            return None
        _total, items = self.repository.list(AccountQuery(platform="chatgpt", email=normalized, page=1, page_size=5000))
        for item in items:
            if _normalize_string(item.email).lower() == normalized:
                return item
        if not _gmail_family_key(normalized):
            return None

        _total, items = self.repository.list(AccountQuery(platform="chatgpt", page=1, page_size=5000))
        for item in items:
            provider_name, mailbox_email = self._match_gmail_mailbox_resource(item, normalized)
            if provider_name:
                if callable(log_fn):
                    log_fn(
                        "通过 Gmail 邮箱服务匹配本地账号: "
                        f"remote={normalized} local={item.email} provider={provider_name} mailbox={mailbox_email}"
                    )
                return item
        return None

    @staticmethod
    def _match_gmail_mailbox_resource(local_account, remote_email: str) -> tuple[str, str]:
        def text(value: Any) -> str:
            return str(value or "").strip()

        def safe_dict(value: Any) -> dict[str, Any]:
            return dict(value) if isinstance(value, dict) else {}

        def add_email(candidates: list[str], value: Any) -> None:
            email = text(value).lower()
            if "@" in email:
                candidates.append(email)

        gmail_providers = {"gmail_oauth_fission", "gmail_api_code", "gmail_oauth", "gmail_api"}
        resources = [
            dict(item)
            for item in list(getattr(local_account, "provider_resources", None) or [])
            if isinstance(item, dict)
        ]
        mailbox = safe_dict((getattr(local_account, "overview", None) or {}).get("verification_mailbox"))
        if mailbox:
            resources.append(
                {
                    "provider_name": mailbox.get("provider"),
                    "resource_type": "mailbox",
                    "resource_identifier": mailbox.get("account_id"),
                    "handle": mailbox.get("email"),
                    "display_name": mailbox.get("email"),
                    "metadata": {
                        "email": mailbox.get("email"),
                        "account_id": mailbox.get("account_id"),
                    },
                }
            )

        for resource in resources:
            if text(resource.get("resource_type") or "mailbox").lower() != "mailbox":
                continue
            provider_name = text(resource.get("provider_name") or resource.get("provider")).lower()
            if provider_name not in gmail_providers:
                continue
            metadata = safe_dict(resource.get("metadata"))
            alias_meta = safe_dict(metadata.get("email_alias"))
            candidates: list[str] = []
            for value in (
                resource.get("handle"),
                resource.get("display_name"),
                resource.get("resource_identifier"),
                metadata.get("email"),
                metadata.get("master_email"),
                metadata.get("alias_parent_email"),
                metadata.get("email_alias_parent"),
                metadata.get("parent_email"),
                alias_meta.get("alias_email"),
                alias_meta.get("parent_email"),
            ):
                add_email(candidates, value)
            for candidate in candidates:
                if candidate == remote_email or _same_gmail_family(candidate, remote_email):
                    return provider_name, candidate
        return "", ""

    @staticmethod
    def _prepare_gmail_alias_relogin_account(local_account, remote_email: str, log: Callable[[str], None] | None = None):
        remote = _normalize_string(remote_email).lower()
        if not remote or remote == _normalize_string(getattr(local_account, "email", "")).lower():
            return local_account
        if not _gmail_family_key(remote):
            return local_account

        def text(value: Any) -> str:
            return str(value or "").strip()

        def safe_dict(value: Any) -> dict[str, Any]:
            return dict(value) if isinstance(value, dict) else {}

        def parent_candidate(resource: dict[str, Any]) -> str:
            metadata = safe_dict(resource.get("metadata"))
            alias_meta = safe_dict(metadata.get("email_alias"))
            for value in (
                metadata.get("master_email"),
                metadata.get("alias_parent_email"),
                metadata.get("email_alias_parent"),
                metadata.get("parent_email"),
                alias_meta.get("parent_email"),
                metadata.get("email"),
                resource.get("handle"),
                resource.get("display_name"),
                resource.get("resource_identifier"),
            ):
                email = text(value).lower()
                if "@" in email and _same_gmail_family(email, remote):
                    if "+" not in email.split("@", 1)[0]:
                        return email
            if "+" in remote.split("@", 1)[0]:
                local_part, domain = remote.split("@", 1)
                return f"{local_part.split('+', 1)[0]}@{domain}"
            return ""

        resources = [
            dict(item)
            for item in list(getattr(local_account, "provider_resources", None) or [])
            if isinstance(item, dict)
        ]
        mailbox = safe_dict((getattr(local_account, "overview", None) or {}).get("verification_mailbox"))
        if mailbox:
            resources.append(
                {
                    "provider_name": mailbox.get("provider"),
                    "resource_type": "mailbox",
                    "resource_identifier": mailbox.get("account_id"),
                    "handle": mailbox.get("email"),
                    "display_name": mailbox.get("email"),
                    "metadata": {
                        "email": mailbox.get("email"),
                        "account_id": mailbox.get("account_id"),
                    },
                }
            )
        gmail_providers = {"gmail_oauth_fission", "gmail_api_code", "gmail_oauth", "gmail_api"}
        for index, resource in enumerate(resources):
            if text(resource.get("resource_type") or "mailbox").lower() != "mailbox":
                continue
            provider_name = text(resource.get("provider_name") or resource.get("provider")).lower()
            if provider_name not in gmail_providers:
                continue
            parent_email = parent_candidate(resource)
            if not parent_email:
                continue

            metadata = safe_dict(resource.get("metadata"))
            alias_meta = safe_dict(metadata.get("email_alias"))
            parent_account_id = text(
                metadata.get("alias_parent_account_id")
                or alias_meta.get("parent_account_id")
                or resource.get("resource_identifier")
                or parent_email
            )
            metadata.update(
                {
                    "email": parent_email,
                    "alias_email": remote,
                    "alias_parent_email": parent_email,
                    "alias_parent_account_id": parent_account_id,
                    "email_alias": {
                        **alias_meta,
                        "enabled": True,
                        "alias_email": remote,
                        "parent_email": parent_email,
                        "parent_account_id": parent_account_id,
                    },
                }
            )
            resource["metadata"] = metadata
            resource["handle"] = remote
            resource["display_name"] = remote
            resource["resource_identifier"] = parent_account_id
            resources[index] = resource
            if callable(log):
                log(f"Gmail 别名重登映射: login={remote} parent_mailbox={parent_email} provider={provider_name}")
            return replace(local_account, email=remote, provider_resources=resources)

        return local_account

    def _run_protocol_relogin(
        self,
        local_account,
        logs: list[str],
        *,
        log_fn: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        if self.browser_relogin:
            before = len(logs)
            result = self.browser_relogin(local_account, logs)
            if callable(log_fn):
                for line in logs[before:]:
                    log_fn(str(line))
            return result

        from platforms.chatgpt.register import RegistrationEngine, RegistrationResult

        def log(message: str) -> None:
            text = str(message)
            logs.append(text)
            if callable(log_fn):
                log_fn(text)

        log("初始化批量注册同款 mailbox 服务，用于协议登录验证码读取")
        email_service, mailbox_error = self._build_relogin_mailbox_email_service(local_account, log)
        if email_service is None:
            log(f"邮箱服务不可用: {mailbox_error}")
            return {"error": f"mailbox_otp_unavailable: {mailbox_error}"}

        log("使用批量注册同款 Platform 协议链路重新登录，不启动浏览器")
        log("创建 RegistrationEngine，并固定使用当前本地邮箱与密码")
        engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=None,
            callback_logger=log,
        )
        engine.email = local_account.email
        engine.password = local_account.password
        engine.k12_join_enabled = True
        engine.k12_workspace_ids = _normalize_string(
            (local_account.overview or {}).get("k12_workspace_id")
            or (local_account.overview or {}).get("workspace_id")
        )
        log("开始执行协议登录流程")
        result = engine.run()
        if not result or not result.success:
            return {"error": getattr(result, "error_message", "") or "protocol relogin failed"}
        metadata = getattr(result, "metadata", None) or {}
        session = metadata.get("session") if isinstance(metadata.get("session"), dict) else {}
        log(
            "协议登录完成: "
            f"session={'有' if session else '无'} "
            f"accessToken={'有' if session.get('accessToken') or session.get('access_token') else '无'} "
            f"sessionToken={'有' if getattr(result, 'session_token', '') else '无'}"
        )
        return {
            "session": session,
            "cookies": metadata.get("cookies", ""),
            "session_token": getattr(result, "session_token", ""),
            "access_token": getattr(result, "access_token", ""),
            "account_id": getattr(result, "account_id", ""),
            "raw_result": result.to_dict() if isinstance(result, RegistrationResult) else {},
        }

    def _build_relogin_mailbox_email_service(
        self,
        local_account,
        log: Callable[[str], None],
    ) -> tuple[Any | None, str]:
        from core.base_mailbox import MailboxAccount, create_mailbox
        from core.email_alias_mailbox import EmailAliasMailbox, normalize_email_address
        from platforms.chatgpt.protocol_mailbox import _MailboxEmailService

        def text(value: Any) -> str:
            return str(value or "").strip()

        def safe_dict(value: Any) -> dict[str, Any]:
            return dict(value) if isinstance(value, dict) else {}

        def safe_list(value: Any) -> list[Any]:
            return list(value) if isinstance(value, (list, tuple)) else []

        def mailbox_provider_key(value: str, metadata: dict[str, Any] | None = None) -> str:
            raw = text(value)
            api_mode = text((metadata or {}).get("api_mode")).lower()
            if raw in {"cloud_mail", "cfworker"} or api_mode in {"cloud_mail", "cfworker"}:
                return "cfworker_admin_api"
            if raw == "outlook_email":
                return "outlook_email_api"
            return raw

        def apply_provider_compat_settings(provider_key: str, runtime_extra: dict[str, Any], metadata: dict[str, Any]) -> None:
            if provider_key == "cfworker_admin_api":
                if metadata.get("api_url") and not runtime_extra.get("cfworker_api_url"):
                    runtime_extra["cfworker_api_url"] = metadata.get("api_url")
                if metadata.get("domain") and not runtime_extra.get("cfworker_domain"):
                    runtime_extra["cfworker_domain"] = metadata.get("domain")
                token = (
                    metadata.get("admin_token")
                    or metadata.get("public_token")
                    or metadata.get("api_token")
                    or metadata.get("token")
                )
                if token and not runtime_extra.get("cfworker_admin_token"):
                    runtime_extra["cfworker_admin_token"] = token

        def alias_parent_from(metadata: dict[str, Any], mailbox_email: str) -> tuple[str, str, bool]:
            alias_meta = safe_dict(metadata.get("email_alias"))
            metadata_email = text(metadata.get("email"))
            metadata_email_lc = metadata_email.lower()
            mailbox_email_lc = mailbox_email.lower()
            metadata_email_is_parent = (
                "@" in metadata_email
                and metadata_email_lc != mailbox_email_lc
                and "+" not in metadata_email.split("@", 1)[0]
            )
            parent_email = text(
                (metadata_email if metadata_email_is_parent else "")
                or metadata.get("alias_parent_email")
                or metadata.get("email_alias_parent")
                or metadata.get("parent_email")
                or alias_meta.get("parent_email")
                or alias_meta.get("alias_parent_email")
            )
            parent_account_id = text(
                metadata.get("alias_parent_account_id")
                or alias_meta.get("parent_account_id")
                or alias_meta.get("alias_parent_account_id")
            )
            is_alias = bool(parent_email or parent_account_id or alias_meta.get("enabled"))
            if not parent_email and "+" in mailbox_email and "@" in mailbox_email:
                local_part, domain = mailbox_email.split("@", 1)
                parent_email = f"{local_part.split('+', 1)[0]}@{domain}"
                is_alias = True
                log(f"检测到 plus 别名邮箱但缺少父邮箱元数据，按邮箱地址推断父邮箱={parent_email}")
            return parent_email, parent_account_id, is_alias

        resources = [dict(item) for item in safe_list(local_account.provider_resources) if isinstance(item, dict)]
        mailbox_resources = [
            item
            for item in resources
            if text(item.get("resource_type") or "mailbox").lower() == "mailbox"
        ]
        def mailbox_resource_score(item: dict[str, Any]) -> tuple[int, int]:
            metadata = safe_dict(item.get("metadata"))
            alias_meta = safe_dict(metadata.get("email_alias"))
            has_alias_parent = bool(
                metadata.get("alias_parent_email")
                or metadata.get("email_alias_parent")
                or metadata.get("parent_email")
                or alias_meta.get("parent_email")
            )
            return (1 if has_alias_parent else 0, len(metadata))

        mailbox_resources.sort(key=mailbox_resource_score, reverse=True)
        if not mailbox_resources:
            mailbox = safe_dict((local_account.overview or {}).get("verification_mailbox"))
            if mailbox:
                mailbox_resources.append(
                    {
                        "provider_type": "mailbox",
                        "provider_name": mailbox.get("provider"),
                        "resource_type": "mailbox",
                        "resource_identifier": mailbox.get("account_id"),
                        "handle": mailbox.get("email"),
                        "display_name": mailbox.get("email"),
                        "metadata": {
                            "account_id": mailbox.get("account_id"),
                            "email": mailbox.get("email"),
                        },
                    }
                )

        if not mailbox_resources:
            return None, "本地账号没有绑定邮箱 provider 资源，无法自动读取邮箱 OTP"

        provider_accounts = [
            dict(item) for item in safe_list(local_account.provider_accounts) if isinstance(item, dict)
        ]
        last_error = ""
        for mailbox_resource in mailbox_resources:
            metadata = safe_dict(mailbox_resource.get("metadata"))
            raw_provider_name = text(mailbox_resource.get("provider_name") or mailbox_resource.get("provider"))
            provider_name = mailbox_provider_key(raw_provider_name, metadata)
            mailbox_email = text(
                mailbox_resource.get("handle")
                or mailbox_resource.get("display_name")
                or metadata.get("email")
                or local_account.email
            )
            account_id = text(
                mailbox_resource.get("resource_identifier")
                or metadata.get("account_id")
                or metadata.get("id")
                or mailbox_email
            )
            if not provider_name:
                last_error = "账号邮箱资源缺少 provider_name"
                continue
            if not mailbox_email:
                last_error = "账号邮箱资源缺少 email"
                continue

            accepted_providers = {provider_name, raw_provider_name}
            if provider_name == "cfworker_admin_api":
                accepted_providers.update({"cloud_mail", "cfworker"})
            if provider_name == "outlook_email_api":
                accepted_providers.add("outlook_email")
            accepted_providers = {item for item in accepted_providers if item}

            provider_account = None
            email_lc = mailbox_email.lower()
            account_id_lc = account_id.lower()
            for item in provider_accounts:
                item_metadata = safe_dict(item.get("metadata"))
                item_credentials = safe_dict(item.get("credentials"))
                item_provider = mailbox_provider_key(
                    text(item.get("provider_name") or item.get("provider")),
                    item_metadata,
                )
                raw_item_provider = text(item.get("provider_name") or item.get("provider"))
                if (item_provider or raw_item_provider) and not ({item_provider, raw_item_provider} & accepted_providers):
                    continue
                candidates = {
                    text(item.get("login_identifier")).lower(),
                    text(item.get("display_name")).lower(),
                    text(item_metadata.get("email")).lower(),
                    text(item_metadata.get("account_id")).lower(),
                    text(item_credentials.get("email")).lower(),
                    text(item_credentials.get("login_account")).lower(),
                    text(item.get("id")).lower(),
                }
                if email_lc in candidates or (account_id_lc and account_id_lc in candidates):
                    provider_account = item
                    break
                if provider_account is None:
                    provider_account = item

            parent_email, parent_account_id, is_alias = alias_parent_from(metadata, mailbox_email)
            runtime_extra = dict(metadata)
            apply_provider_compat_settings(provider_name, runtime_extra, metadata)
            runtime_resource = dict(mailbox_resource)
            runtime_metadata = dict(metadata)
            if is_alias:
                parent_account_id = parent_account_id or account_id or parent_email
                runtime_metadata["alias_parent_email"] = parent_email
                runtime_metadata["alias_parent_account_id"] = parent_account_id
                runtime_metadata["email_alias"] = {
                    **safe_dict(runtime_metadata.get("email_alias")),
                    "enabled": True,
                    "alias_email": mailbox_email,
                    "parent_email": parent_email,
                    "parent_account_id": parent_account_id,
                }
                runtime_resource["metadata"] = runtime_metadata
                runtime_resource["handle"] = mailbox_email
                runtime_resource["display_name"] = mailbox_email
                runtime_resource["resource_identifier"] = parent_account_id
            runtime_extra["provider_resource"] = runtime_resource
            if provider_account:
                runtime_extra["provider_account"] = provider_account

            mailbox_account_extra = dict(runtime_extra)
            mailbox_account_extra["mailbox_provider_key"] = provider_name
            if is_alias:
                mailbox_account_extra["email_alias"] = {
                    "enabled": True,
                    "alias_email": mailbox_email,
                    "parent_email": parent_email,
                    "parent_account_id": parent_account_id or account_id or parent_email,
                }
            mailbox_account = MailboxAccount(
                email=mailbox_email,
                account_id=(parent_account_id or account_id or mailbox_email) if is_alias else (account_id or mailbox_email),
                extra=mailbox_account_extra,
            )
            try:
                mailbox = create_mailbox(provider_name, extra=runtime_extra, proxy=None)
            except Exception as exc:
                last_error = f"{raw_provider_name or provider_name} -> {provider_name}: {exc}"
                log(f"邮箱资源不可用，跳过: {last_error}")
                continue

            if raw_provider_name and raw_provider_name != provider_name:
                log(f"邮箱 provider 兼容映射: {raw_provider_name} -> {provider_name}")
            if is_alias:
                normalized_parent = normalize_email_address(parent_email)
                if not normalized_parent:
                    return None, f"别名邮箱 {mailbox_email} 缺少父邮箱，无法读取 OTP"
                log(f"检测到别名邮箱: alias={mailbox_email} parent={normalized_parent}")
                alias_mailbox = EmailAliasMailbox(mailbox, platform="chatgpt", log_fn=log)
                parent_extra = dict(mailbox_account_extra)
                parent_resource = dict(parent_extra.get("provider_resource") or {})
                if parent_resource:
                    parent_resource["handle"] = parent_email
                    parent_resource["display_name"] = parent_email
                    parent_resource["resource_identifier"] = parent_account_id or account_id or parent_email
                    parent_extra["provider_resource"] = parent_resource
                alias_mailbox._parents_by_alias[normalize_email_address(mailbox_email)] = MailboxAccount(
                    email=parent_email,
                    account_id=parent_account_id or account_id or parent_email,
                    extra=parent_extra,
                )
                mailbox = alias_mailbox
            log(
                "使用本地邮箱资源读取 OTP: "
                f"provider={provider_name} email={mailbox_email} account_id={mailbox_account.account_id}"
            )
            return (
                _MailboxEmailService(
                    mailbox=mailbox,
                    mailbox_account=mailbox_account,
                    provider=provider_name,
                    log_fn=log,
                ),
                "",
            )

        return None, f"无法初始化账号邮箱 provider: {last_error or '没有可用邮箱资源'}"

    def _replace_k12_account(
        self,
        ctx: Sub2ApiContext,
        remote_account: dict[str, Any],
        login_result: dict[str, Any],
        workspace_ids: str,
        *,
        log_fn: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        from platforms.chatgpt.k12_join import (
            ensure_chatgpt_session_cookie,
            exchange_workspace_session,
            parse_workspace_ids,
            send_workspace_join_requests,
            upload_session_to_sub2api,
        )

        session = login_result.get("session") if isinstance(login_result.get("session"), dict) else {}
        access_token = _normalize_string(session.get("accessToken") or session.get("access_token") or login_result.get("access_token"))
        session_token = _normalize_string(session.get("sessionToken") or session.get("session_token") or login_result.get("session_token"))
        cookies = ensure_chatgpt_session_cookie(_normalize_string(login_result.get("cookies")), session_token)
        workspace_list = parse_workspace_ids(workspace_ids)
        if not access_token or "__Secure-next-auth.session-token=" not in cookies:
            return {"ok": False, "message": "新登录态缺少 ChatGPT Web session token/cookie"}
        if not workspace_list:
            return {"ok": False, "message": "K12 workspace_id 为空"}

        successful_sessions: list[dict[str, Any]] = []
        log_lines: list[str] = []
        def log(message: str) -> None:
            text = str(message)
            log_lines.append(text)
            if callable(log_fn):
                log_fn(text)

        for workspace_id in workspace_list:
            log(f"  [K12] 先检查账号是否仍在空间 {workspace_id[:8]}，尝试直接切换 session")
            candidate = exchange_workspace_session(
                cookies=cookies,
                workspace_id=workspace_id,
                access_token=access_token,
                log=log,
            )
            if candidate:
                successful_sessions.append({"workspace_id": workspace_id, "session": candidate})
                log(f"  [K12] 账号仍可切换到空间 {workspace_id[:8]}，跳过强入 join")
                continue

            log(f"  [K12] 直接切换失败，开始重新强入空间 {workspace_id[:8]}")
            join_results = send_workspace_join_requests(
                access_token=access_token,
                cookies=cookies,
                workspace_ids=workspace_id,
                log=log,
            )
            if not any(isinstance(item, dict) and item.get("ok") for item in join_results):
                continue
            candidate = exchange_workspace_session(
                cookies=cookies,
                workspace_id=workspace_id,
                access_token=access_token,
                log=log,
            )
            if candidate:
                successful_sessions.append({"workspace_id": workspace_id, "session": candidate})

        if not successful_sessions:
            return {"ok": False, "message": "K12 join/exchange 未完成，未删除远端旧账号"}

        delete_ok, delete_msg = self.delete_account(ctx, remote_account["id"])
        if not delete_ok:
            return {"ok": False, "message": f"K12 exchange 成功，但删除远端旧账号失败: {delete_msg}"}

        upload_messages: list[str] = []
        upload_failures: list[str] = []
        for item in successful_sessions:
            workspace_id = str(item.get("workspace_id") or "")
            k12_session = item.get("session") if isinstance(item.get("session"), dict) else {}
            upload_ok, upload_msg = upload_session_to_sub2api(k12_session, workspace_id=workspace_id, log=log)
            upload_messages.append(f"{workspace_id[:8]}: {upload_msg}")
            if not upload_ok:
                upload_failures.append(f"{workspace_id[:8]}: {upload_msg}")

        first_success = successful_sessions[0]
        if upload_failures:
            return {
                "ok": False,
                "remote_deleted": True,
                "message": f"远端旧账号已删除，但 {len(upload_failures)}/{len(successful_sessions)} 个 K12 session 上传失败: {'; '.join(upload_failures)}",
                "k12_workspace_id": first_success.get("workspace_id") or "",
                "k12_session": first_success.get("session") or {},
                "k12_sessions": successful_sessions,
                "logs": log_lines,
            }
        return {
            "ok": True,
            "message": f"K12 账号已删除旧号并重新上传 {len(successful_sessions)} 个空间: {'; '.join(upload_messages)}",
            "k12_workspace_id": first_success.get("workspace_id") or "",
            "k12_session": first_success.get("session") or {},
            "k12_sessions": successful_sessions,
            "logs": log_lines,
        }

    def _persist_k12_session(self, account_id: int, replaced: dict[str, Any]) -> None:
        k12_sessions = replaced.get("k12_sessions") if isinstance(replaced.get("k12_sessions"), list) else []
        first_entry = k12_sessions[0] if k12_sessions and isinstance(k12_sessions[0], dict) else {}
        k12_session = first_entry.get("session") if isinstance(first_entry.get("session"), dict) else {}
        if not k12_session:
            k12_session = replaced.get("k12_session") if isinstance(replaced.get("k12_session"), dict) else {}
        workspace_ids = [
            str(item.get("workspace_id") or "")
            for item in k12_sessions
            if isinstance(item, dict) and item.get("workspace_id")
        ]
        if not workspace_ids and replaced.get("k12_workspace_id"):
            workspace_ids = [str(replaced.get("k12_workspace_id") or "")]
        credentials = {
            "access_token": k12_session.get("accessToken") or k12_session.get("access_token") or "",
            "session_token": k12_session.get("sessionToken") or k12_session.get("session_token") or "",
            "plan_type": "k12",
        }
        credentials = {key: value for key, value in credentials.items() if value}
        self.repository.update(
            int(account_id),
            AccountUpdateCommand(
                credentials=credentials,
                primary_token=credentials.get("access_token") or None,
                overview={
                    "k12_workspace_id": workspace_ids[0] if workspace_ids else "",
                    "k12_workspace_ids": "\n".join(workspace_ids),
                    "k12_replaced_at": _utcnow_iso(),
                },
            ),
        )
