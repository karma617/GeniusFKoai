from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlmodel import Session

from core.base_platform import RegisterConfig
from core.account_graph import patch_account_graph
from core.db import AccountModel, engine
from core.platform_accounts import build_platform_account
from core.registry import get, list_platforms, load_all
from domain.actions import (
    ActionExecutionCommand,
    ActionExecutionResult,
    ActionParameter,
    PlatformAction,
)
from domain.platforms import PlatformCapabilities, PlatformDescriptor


PERSISTED_ACTION_DATA_KEYS = {
    "access_token",
    "refresh_token",
    "session_token",
    "id_token",
    "api_key",
    "client_id",
    "client_secret",
    "workspace_id",
    "accessToken",
    "refreshToken",
    "sessionToken",
    "idToken",
    "clientId",
    "clientSecret",
    "workspaceId",
    "account_id",
    "accountId",
    "org_id",
    "orgId",
    "auth_token",
    "authToken",
    "cookies",
    "login_state_cookie",
    "cookie_header",
}

STATEFUL_ACTION_IDS = {"get_account_state", "switch_account", "query_state", "query_balance", "switch_desktop"}
OAUTH_RESULT_ACTION_IDS = {"get_rt"}
SESSION_RESULT_ACTION_IDS = {"refresh_session"}
UPLOAD_RESULT_ACTION_IDS = {"upload_sub2api", "k12_join_upload"}
FAILED_ACTION_PERSISTED_ERROR_TYPES = {"account_banned", "session_stale_refreshed", "balance_query_failed"}
CASHIER_URL_ACTION_IDS = {
    "payment_link",
    "payment_link_browser",
    "generate_trial_link",
    "generate_trial_link_browser",
    "get_cashier_url",
    "generate_checkout_link",
    "generate_link",
    "generate_link_browser",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_action_url(data: dict[str, Any]) -> str:
    for key in ("cashier_url", "url", "checkout_url"):
        value = str(data.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _build_account_overview(platform: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    overview: dict[str, Any] = {
        "platform": platform,
        "checked_at": _utcnow_iso(),
        "chips": [],
    }
    if "valid" in data:
        overview["valid"] = bool(data.get("valid"))
        overview["chips"].append("有效" if data.get("valid") else "失效")

    remote_email = ""
    if isinstance(data.get("remote_user"), dict):
        remote_email = str(data["remote_user"].get("email", "") or "")
    elif isinstance(data.get("portal_user"), dict):
        remote_email = str(data["portal_user"].get("email", "") or "")
    if remote_email:
        overview["remote_email"] = remote_email

    plan = (
        data.get("membership_type")
        or (data.get("billing_info") or {}).get("membershipType")
        or (data.get("usage_summary") or {}).get("plan_title")
        or (data.get("subscription") or {}).get("plan")
        or ""
    )
    if plan:
        overview["plan"] = plan
        overview["plan_name"] = str(plan)
        overview["chips"].append(str(plan))
        plan_lower = str(plan).strip().lower()
        if any(token in plan_lower for token in ("pro", "plus", "premium", "business", "team", "enterprise", "student")):
            overview["plan_state"] = "subscribed"
        elif "trial" in plan_lower:
            overview["plan_state"] = "trial"
        elif plan_lower in {"free", "basic", "starter", "hobby"}:
            overview["plan_state"] = "free"

    if "trial_eligible" in data:
        overview["trial_eligible"] = data.get("trial_eligible")
        overview["chips"].append("可试用" if data.get("trial_eligible") else "不可试用")
    if data.get("trial_length_days"):
        overview["trial_length_days"] = data.get("trial_length_days")
        overview["chips"].append(f"{data['trial_length_days']}天试用")
    if "has_valid_payment_method" in data:
        overview["has_valid_payment_method"] = data.get("has_valid_payment_method")
        overview["chips"].append("已绑卡" if data.get("has_valid_payment_method") else "未绑卡")

    for key in (
        "remaining_credits",
        "usage_total",
        "plan_credits",
        "balance_rp",
        "balance_query_status",
        "registration_ip_country_code",
        "next_reset_at",
        "days_until_reset",
        "prompt_credits_limit",
        "flow_action_credits_limit",
        "prompt_remaining_percent",
        "flow_action_remaining_percent",
    ):
        if data.get(key) not in (None, ""):
            overview[key] = data.get(key)
    if "balance_check_error" in data:
        overview["balance_check_error"] = str(data.get("balance_check_error") or "")
    if isinstance(data.get("usage_breakdowns"), list):
        overview["usage_breakdowns"] = data.get("usage_breakdowns")
        for item in data.get("usage_breakdowns") or []:
            if not isinstance(item, dict):
                continue
            label = item.get("display_name") or item.get("resource_type") or "usage"
            remaining = item.get("remaining_usage")
            limit = item.get("usage_limit")
            chip = f"{label}"
            if remaining not in (None, ""):
                chip += f" 剩{remaining}"
            if limit not in (None, ""):
                chip += f" / {limit}"
            overview["chips"].append(chip)
    if isinstance(data.get("codex_usage"), dict):
        overview["codex_usage"] = data.get("codex_usage")
    if isinstance(data.get("chatgpt_usage"), dict):
        overview["chatgpt_usage"] = data.get("chatgpt_usage")
    if data.get("codex_usage_error"):
        overview["codex_usage_error"] = data.get("codex_usage_error")

    usage_summary = data.get("usage_summary") or {}
    if platform == "cursor" and isinstance(usage_summary.get("models"), dict):
        usage_models = []
        for model_name, info in usage_summary["models"].items():
            if not isinstance(info, dict):
                continue
            usage_models.append({
                "model": model_name,
                "num_requests": info.get("num_requests"),
                "num_requests_total": info.get("num_requests_total"),
                "num_tokens": info.get("num_tokens"),
                "remaining_requests": info.get("remaining_requests"),
                "remaining_tokens": info.get("remaining_tokens"),
            })
            chip = f"{model_name} {info.get('num_requests', 0)}次"
            if info.get("remaining_requests") is not None:
                chip += f" / 剩{info['remaining_requests']}"
            overview["chips"].append(chip)
        if usage_models:
            overview["usage_models"] = usage_models

    if platform == "kiro" and isinstance(usage_summary, dict):
        if usage_summary.get("next_reset_at"):
            overview["next_reset_at"] = usage_summary.get("next_reset_at")
        if usage_summary.get("days_until_reset") is not None:
            overview["days_until_reset"] = usage_summary.get("days_until_reset")
            overview["chips"].append(f"重置 {usage_summary.get('days_until_reset')} 天")
        breakdowns = []
        for item in usage_summary.get("breakdowns") or []:
            if not isinstance(item, dict):
                continue
            breakdowns.append({
                "display_name": item.get("display_name"),
                "current_usage": item.get("current_usage"),
                "usage_limit": item.get("usage_limit"),
                "remaining_usage": item.get("remaining_usage"),
                "trial_status": item.get("trial_status"),
                "trial_expiry": item.get("trial_expiry"),
                "trial_remaining_usage": item.get("trial_remaining_usage"),
            })
            label = item.get("display_name") or item.get("resource_type") or "usage"
            chip = f"{label} {item.get('current_usage', 0)}/{item.get('usage_limit', '-')}"
            if item.get("trial_status"):
                chip += f" · {item['trial_status']}"
            overview["chips"].append(chip)
        if breakdowns:
            overview["usage_breakdowns"] = breakdowns

    if isinstance(data.get("local_app_account"), dict):
        overview["local_matches_target"] = bool(data["local_app_account"].get("matches_target"))
        if data["local_app_account"].get("matches_target"):
            overview["chips"].append("当前")

    if isinstance(data.get("desktop_app_state"), dict):
        desktop_state = data["desktop_app_state"]
        overview["desktop_app_state"] = {
            "app_name": desktop_state.get("app_name"),
            "running": bool(desktop_state.get("running")),
            "ready": bool(desktop_state.get("ready")),
            "configured": bool(desktop_state.get("configured")),
            "installed": bool(desktop_state.get("installed")),
            "status_label": desktop_state.get("status_label", ""),
            "ready_label": desktop_state.get("ready_label", ""),
        }

    if data.get("quota_note"):
        overview["quota_note"] = data.get("quota_note")

    overview["chips"] = [chip for chip in overview["chips"] if chip]
    return overview if len(overview) > 2 else None


def _build_oauth_result_overview(platform: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    remote_user = data.get("remote_user") if isinstance(data.get("remote_user"), dict) else profile
    claims = data.get("id_token_claims") if isinstance(data.get("id_token_claims"), dict) else {}
    remote_email = str(
        data.get("email")
        or profile.get("email")
        or remote_user.get("email")
        or claims.get("email")
        or ""
    ).strip()
    oauth_summary = {
        "type": str(data.get("type") or "codex"),
        "email": remote_email,
        "account_id": str(data.get("account_id") or ""),
        "expired": str(data.get("expired") or ""),
        "last_refresh": str(data.get("last_refresh") or ""),
        "profile": profile,
        "id_token_claims": claims,
    }
    overview: dict[str, Any] = {
        "platform": platform,
        "checked_at": _utcnow_iso(),
        "oauth": oauth_summary,
        "codex_oauth": oauth_summary,
    }
    if str(data.get("refresh_token") or "").strip():
        overview.update(
            {
                "lifecycle_status": "rt_pending_upload",
                "display_status": "rt_pending_upload",
                "valid": True,
                "authorized_at": _utcnow_iso(),
                "rt_acquired_at": _utcnow_iso(),
                "rt_upload_status": "pending_upload",
            }
        )
        if data.get("token_backup_path"):
            overview["token_backup_path"] = str(data.get("token_backup_path") or "")
            oauth_summary["token_backup_path"] = overview["token_backup_path"]
    if remote_email:
        overview["remote_email"] = remote_email
    if remote_user:
        overview["remote_user"] = remote_user
    return overview


def _build_session_refresh_overview(platform: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if platform != "chatgpt" or not isinstance(data, dict):
        return None
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    if not session:
        return None
    now = str(data.get("session_refreshed_at") or "").strip() or _utcnow_iso()
    return {
        "platform": platform,
        "checked_at": now,
        "lifecycle_status": "registered",
        "display_status": "registered",
        "valid": True,
        "validity_status": "valid",
        "session": session,
        "chatgpt_session": session,
        "session_refresh_status": "refreshed",
        "last_session_refresh_at": now,
        "remote_email": str(data.get("email") or ""),
    }


def _build_upload_result_overview(platform: str, action_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if platform != "chatgpt" or not isinstance(data, dict):
        return None
    if action_id == "k12_join_upload":
        k12_session = data.get("k12_session")
        if not isinstance(k12_session, dict) or not k12_session:
            return None
        now = _utcnow_iso()
        return {
            "platform": platform,
            "checked_at": now,
            "lifecycle_status": "registered",
            "display_status": "registered",
            "valid": True,
            "plan_state": "free",
            "k12_session": k12_session,
            "k12_workspace_id": str(data.get("k12_workspace_id") or ""),
            "k12_upload_status": str(data.get("upload_status") or ""),
            "k12_upload_message": str(data.get("message") or ""),
            "k12_uploaded_at": now if str(data.get("upload_status") or "") == "uploaded" else "",
        }
    if action_id != "upload_sub2api":
        return None
    if str(data.get("upload_status") or "").strip() != "uploaded":
        return None
    now = _utcnow_iso()
    if data.get("uploaded_without_rt"):
        return {
            "platform": platform,
            "checked_at": now,
            "valid": True,
            "sub2api_upload_status": "uploaded",
            "sub2api_upload_message": str(data.get("message") or ""),
            "sub2api_uploaded_at": now,
            "sub2api_force_upload_without_rt": True,
        }
    return {
        "platform": platform,
        "checked_at": now,
        "lifecycle_status": "rt_uploaded",
        "display_status": "rt_uploaded",
        "valid": True,
        "rt_upload_status": "uploaded",
        "rt_upload_message": str(data.get("message") or ""),
        "rt_uploaded_at": now,
        "rt_upload_checked_at": now,
    }


class PlatformRuntime:
    def list_platforms(self) -> list[PlatformDescriptor]:
        load_all()
        descriptors: list[PlatformDescriptor] = []
        for item in list_platforms():
            descriptors.append(
                PlatformDescriptor(
                    name=item["name"],
                    display_name=item["display_name"],
                    version=item["version"],
                    capabilities=PlatformCapabilities(
                        supported_executors=list(item.get("supported_executors", [])),
                        supported_identity_modes=list(item.get("supported_identity_modes", [])),
                        supported_oauth_providers=list(item.get("supported_oauth_providers", [])),
                    ),
                )
            )
        return descriptors

    def list_actions(self, platform: str) -> list[PlatformAction]:
        load_all()
        platform_cls = get(platform)
        instance = platform_cls(config=RegisterConfig())
        actions = []
        for item in instance.get_platform_actions():
            params = [
                ActionParameter(
                    key=str(param.get("key", "")),
                    label=str(param.get("label", "")),
                    type=str(param.get("type", "text")),
                    options=list(param.get("options", []) or []),
                )
                for param in item.get("params", [])
            ]
            actions.append(
                PlatformAction(
                    id=str(item.get("id", "")),
                    label=str(item.get("label", "")),
                    params=params,
                    sync=bool(item.get("sync", False)),
                )
            )
        return actions
    
    def list_capabilities(self, platform: str) -> list[str]:
        """List platform's declared capabilities."""
        load_all()
        platform_cls = get(platform)
        instance = platform_cls(config=RegisterConfig())
        return instance.get_platform_capabilities()

    def get_desktop_state(self, platform: str) -> dict[str, Any]:
        load_all()
        platform_cls = get(platform)
        instance = platform_cls(config=RegisterConfig())
        return instance.get_desktop_state() or {"available": False}

    def execute_action(
        self,
        command: ActionExecutionCommand,
        *,
        log_fn=None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> ActionExecutionResult:
        load_all()
        if callable(cancel_check) and cancel_check():
            return ActionExecutionResult(ok=False, error="任务已取消")
        with Session(engine) as session:
            model = session.get(AccountModel, command.account_id)
            if not model or model.platform != command.platform:
                return ActionExecutionResult(ok=False, error="账号不存在")

            platform_cls = get(command.platform)
            instance = platform_cls(config=RegisterConfig())
            if log_fn:
                instance.set_logger(log_fn)
            if callable(cancel_check):
                if hasattr(instance, "set_cancel_checker"):
                    instance.set_cancel_checker(cancel_check)
                else:
                    instance._cancel_check_fn = cancel_check
            account = build_platform_account(session, model)
            # 给 action 透传数据库账号 ID，便于写回 overview（如 BA 链）
            if isinstance(command.params, dict) and "account_id" not in command.params:
                command.params = {**command.params, "account_id": int(model.id or 0)}
            try:
                if callable(cancel_check) and cancel_check():
                    return ActionExecutionResult(ok=False, error="任务已取消")
                result: dict[str, Any] = instance.execute_action(command.action_id, account, command.params)
            except NotImplementedError as exc:
                return ActionExecutionResult(ok=False, data={"error_type": "not_supported"}, error=str(exc))
            except Exception as exc:
                return ActionExecutionResult(ok=False, error=str(exc))

            if isinstance(result.get("data"), dict):
                data = result["data"]
                needs_save = False
                action_ok = bool(result.get("ok"))
                error_type = str(result.get("error_type") or "").strip()
                can_persist_failure = (not action_ok) and error_type in FAILED_ACTION_PERSISTED_ERROR_TYPES
                credential_updates = {}
                if action_ok or can_persist_failure:
                    credential_updates = {
                        key: value
                        for key, value in data.items()
                        if key in PERSISTED_ACTION_DATA_KEYS and value not in (None, "")
                    }
                summary_updates: dict[str, Any] = {}
                if can_persist_failure:
                    raw_summary = result.get("summary_updates")
                    if isinstance(raw_summary, dict):
                        summary_updates.update(raw_summary)
                    lifecycle_status = str(result.get("lifecycle_status") or "").strip()
                    if lifecycle_status:
                        summary_updates["lifecycle_status"] = lifecycle_status
                        summary_updates.setdefault("display_status", lifecycle_status)
                    if error_type == "account_banned":
                        summary_updates.setdefault("valid", False)
                        summary_updates.setdefault("validity_status", "invalid")
                    if error_type == "session_stale_refreshed":
                        summary_updates.setdefault("session_refresh_status", "refreshed")
                        summary_updates.setdefault("last_session_refresh_at", _utcnow_iso())
                    if summary_updates:
                        needs_save = True
                if action_ok and command.action_id in STATEFUL_ACTION_IDS:
                    overview = _build_account_overview(command.platform, data)
                    if overview:
                        summary_updates.update(overview)
                        needs_save = True
                if action_ok and command.action_id in OAUTH_RESULT_ACTION_IDS:
                    overview = _build_oauth_result_overview(command.platform, data)
                    if overview:
                        summary_updates.update(overview)
                        needs_save = True
                if action_ok and command.action_id in SESSION_RESULT_ACTION_IDS:
                    overview = _build_session_refresh_overview(command.platform, data)
                    if overview:
                        summary_updates.update(overview)
                        needs_save = True
                if action_ok and command.action_id in UPLOAD_RESULT_ACTION_IDS:
                    overview = _build_upload_result_overview(command.platform, command.action_id, data)
                    if overview:
                        summary_updates.update(overview)
                        needs_save = True
                action_url = _extract_action_url(data)
                if action_url and command.action_id in CASHIER_URL_ACTION_IDS:
                    summary_updates["cashier_url"] = action_url
                    needs_save = True
                if credential_updates:
                    needs_save = True
                if needs_save:
                    model.updated_at = datetime.now(timezone.utc)
                    lifecycle_update = (
                        str(summary_updates.get("lifecycle_status") or "").strip()
                        if summary_updates
                        else ""
                    )
                    patch_account_graph(
                        session,
                        model,
                        lifecycle_status=lifecycle_update or None,
                        summary_updates=summary_updates or None,
                        cashier_url=summary_updates.get("cashier_url") if "cashier_url" in summary_updates else None,
                        credential_updates=credential_updates or None,
                    )
                    session.add(model)
                    session.commit()
            return ActionExecutionResult(
                ok=bool(result.get("ok")),
                data=result.get("data"),
                error=str(result.get("error", "")),
            )
