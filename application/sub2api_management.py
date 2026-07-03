from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

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

    def list_inventory(
        self,
        *,
        group_id: int | None = None,
        status: str = "",
        search: str = "",
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
        if group_id:
            accounts = [item for item in accounts if int(group_id) in item.get("group_ids", [])]
        status_filter = _normalize_string(status).lower()
        if status_filter and status_filter != "all":
            accounts = [item for item in accounts if _normalize_string(item.get("status")).lower() == status_filter]
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

    def test_account(self, ctx: Sub2ApiContext, account_id: str, *, model_id: str = DEFAULT_TEST_MODEL) -> tuple[str, str]:
        from curl_cffi import requests as cffi_requests

        headers = {
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.token}",
        }
        try:
            response = cffi_requests.post(
                f"{ctx.origin}/api/v1/admin/accounts/{account_id}/test",
                headers=headers,
                data=json.dumps({"model_id": model_id}, ensure_ascii=False).encode("utf-8"),
                timeout=60,
                impersonate="chrome110",
            )
        except Exception as exc:
            return "skipped", f"test request failed: {exc}"

        if int(getattr(response, "status_code", 0) or 0) != 200:
            return "skipped", f"HTTP {getattr(response, 'status_code', 0)}"
        text = getattr(response, "text", "") or ""
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
            event_type = str(event.get("type") or "")
            if event_type == "test_complete":
                return ("ok", "test completed") if event.get("success") else ("dead", _short_error(event.get("error") or event.get("text")))
            if event_type == "error":
                return "dead", _short_error(event.get("error") or event.get("text"))
        return "skipped", "no terminal SSE event"

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
        summary = {"ok": 0, "failed": 0, "skipped": 0, "marked_error": 0}
        results: list[dict[str, Any]] = []

        def worker(account_id: str) -> dict[str, Any]:
            result, reason = self.test_account(ctx, account_id, model_id=model_id)
            marked_error = False
            if result == "dead":
                marked_error = self.set_account_error(ctx, account_id)
            with lock:
                if result == "ok":
                    summary["ok"] += 1
                elif result == "dead":
                    summary["failed"] += 1
                else:
                    summary["skipped"] += 1
                if marked_error:
                    summary["marked_error"] += 1
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

        max_workers = max(1, min(int(concurrency or 2), 5, len(selected)))
        summary = {
            "total": len(selected),
            "deleted": 0,
            "replaced": 0,
            "phone_skipped": 0,
            "free_skipped": 0,
            "skipped": 0,
            "failed": 0,
        }
        lock = threading.Lock()

        def worker(remote_account: dict[str, Any]) -> dict[str, Any]:
            result = self._relogin_one(ctx, remote_account, workspace_ids=workspace_ids)
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
            return result

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker, account) for account in selected]
            for future in as_completed(futures):
                results.append(future.result())
        order = {item["id"]: index for index, item in enumerate(selected)}
        return {"ok": True, "summary": summary, "results": sorted(results, key=lambda item: order.get(item["account_id"], 0))}

    def _relogin_one(self, ctx: Sub2ApiContext, remote_account: dict[str, Any], *, workspace_ids: str = "") -> dict[str, Any]:
        account_id = remote_account["id"]
        email = remote_account.get("email") or remote_account.get("name")
        local = self._find_local_account(str(email or ""))
        if not local:
            return {"account_id": account_id, "status": "skipped", "message": f"本地未找到账号 {email}"}
        if not local.password:
            return {"account_id": account_id, "status": "skipped", "message": "本地账号缺少密码"}

        logs: list[str] = []
        login_result = self._run_browser_relogin(local, logs)
        login_text = json.dumps(login_result or {}, ensure_ascii=False) + "\n" + "\n".join(logs)
        if _is_deactivated_error(login_text):
            ok, msg = self.delete_account(ctx, account_id)
            return {
                "account_id": account_id,
                "status": "deleted" if ok else "failed",
                "message": "账号已封禁，已删除远端账号" if ok else f"账号已封禁，但远端删除失败: {msg}",
            }
        if _is_phone_required(login_text) and not (isinstance(login_result, dict) and login_result.get("session")):
            return {"account_id": account_id, "status": "phone_skipped", "message": "登录要求手机接码，已跳过"}
        if not isinstance(login_result, dict) or not login_result.get("session"):
            return {"account_id": account_id, "status": "failed", "message": "重新登录未拿到 ChatGPT session"}

        plan_type = _account_plan_type(remote_account.get("raw") or remote_account) or _account_plan_type(remote_account)
        if plan_type == "free":
            return {"account_id": account_id, "status": "free_skipped", "message": "free 类型账号登录正常，按规则跳过"}
        if plan_type != "k12":
            return {"account_id": account_id, "status": "skipped", "message": f"账号类型 {plan_type or 'unknown'} 不处理"}

        workspace_text = _normalize_string(workspace_ids) or remote_account.get("workspace_id") or _normalize_string(local.overview.get("k12_workspace_id") if isinstance(local.overview, dict) else "")
        if not workspace_text:
            return {"account_id": account_id, "status": "failed", "message": "K12 账号缺少 workspace_id，无法替换"}

        replaced = self._replace_k12_account(ctx, remote_account, login_result, workspace_text)
        if replaced.get("ok"):
            self._persist_k12_session(local.id, replaced)
            return {"account_id": account_id, "status": "replaced", "message": replaced.get("message", "K12 session 已替换")}
        return {"account_id": account_id, "status": "failed", "message": replaced.get("message", "K12 session 替换失败")}

    def _find_local_account(self, email: str):
        normalized = _normalize_string(email).lower()
        if not normalized:
            return None
        _total, items = self.repository.list(AccountQuery(platform="chatgpt", email=normalized, page=1, page_size=5000))
        for item in items:
            if _normalize_string(item.email).lower() == normalized:
                return item
        return None

    def _run_browser_relogin(self, local_account, logs: list[str]) -> dict[str, Any] | None:
        if self.browser_relogin:
            return self.browser_relogin(local_account, logs)

        from application.bitbrowser_profiles import release_acquired_profile
        from core.base_platform import Account as PlatformAccount
        from core.base_platform import RegisterConfig
        from platforms._browser_backend import parse_checkout_mode
        from platforms.chatgpt.browser_register import ChatGPTBrowserRegister
        from platforms.chatgpt.plugin import ChatGPTPlatform

        def log(message: str) -> None:
            logs.append(str(message))

        backend_config = parse_checkout_mode("camoufox_headed", bit_profile_id="")
        acquired_profile_id = ""
        try:
            platform_account = PlatformAccount(
                platform=local_account.platform,
                email=local_account.email,
                password=local_account.password,
                user_id=local_account.user_id,
                token=local_account.primary_token,
                extra={
                    "account_overview": dict(local_account.overview or {}),
                    "provider_accounts": list(local_account.provider_accounts or []),
                    "provider_resources": list(local_account.provider_resources or []),
                },
            )
            otp_callback, otp_error = ChatGPTPlatform(
                RegisterConfig(proxy=None)
            )._build_get_rt_mailbox_otp_callback(platform_account, log, None)
            if not otp_callback:
                log(f"邮箱 OTP callback 不可用: {otp_error}")
            worker = ChatGPTBrowserRegister(
                headless=backend_config.is_headless,
                log_fn=log,
                backend_config=backend_config,
                otp_callback=otp_callback,
            )
            return worker._retry_oauth_fresh_browser(local_account.email, local_account.password)
        finally:
            if acquired_profile_id:
                release_acquired_profile(acquired_profile_id, log_fn=log)

    def _replace_k12_account(
        self,
        ctx: Sub2ApiContext,
        remote_account: dict[str, Any],
        login_result: dict[str, Any],
        workspace_ids: str,
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

        chosen_ws = ""
        k12_session = None
        log_lines: list[str] = []
        log = lambda message: log_lines.append(str(message))
        for workspace_id in workspace_list:
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
                chosen_ws = workspace_id
                k12_session = candidate
                break
        if not chosen_ws or not k12_session:
            return {"ok": False, "message": "K12 join/exchange 未完成，未删除远端旧账号"}

        delete_ok, delete_msg = self.delete_account(ctx, remote_account["id"])
        if not delete_ok:
            return {"ok": False, "message": f"K12 exchange 成功，但删除远端旧账号失败: {delete_msg}"}
        upload_ok, upload_msg = upload_session_to_sub2api(k12_session, log=log)
        if not upload_ok:
            return {"ok": False, "message": f"远端旧账号已删除，但 K12 session 上传失败: {upload_msg}"}
        return {
            "ok": True,
            "message": f"K12 账号已删除旧号并重新上传: {upload_msg}",
            "k12_workspace_id": chosen_ws,
            "k12_session": k12_session,
            "logs": log_lines,
        }

    def _persist_k12_session(self, account_id: int, replaced: dict[str, Any]) -> None:
        k12_session = replaced.get("k12_session") if isinstance(replaced.get("k12_session"), dict) else {}
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
                    "k12_workspace_id": replaced.get("k12_workspace_id") or "",
                    "k12_replaced_at": _utcnow_iso(),
                },
            ),
        )
