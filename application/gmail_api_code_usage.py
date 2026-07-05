from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from core.db import AccountModel, ProviderResourceModel, TaskEventModel, TaskModel, engine
from core.gmail_api_code_mailbox import GmailApiCodeMailbox, parse_gmail_api_code_entries
from infrastructure.provider_settings_repository import ProviderSettingsRepository


ALIAS_LIMIT = 5
_ALLOCATED_RE = re.compile(r"Email alias allocated:\s+([^\s]+)\s+parent=([^\s]+)")


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _safe_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _gmail_parent(email: str) -> str:
    target = _normalize_email(email)
    if "@" not in target:
        return ""
    local, domain = target.split("@", 1)
    if domain not in {"gmail.com", "googlemail.com"}:
        return ""
    return f"{local.split('+', 1)[0]}@gmail.com"


def _resolve_gmail_parent(*values: Any) -> str:
    for value in values:
        parent = _gmail_parent(_normalize_email(value))
        if parent:
            return parent
    return ""


def _metadata_text(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
            return _normalize_email(value)
    nested = metadata.get("email_alias")
    if isinstance(nested, dict):
        for key in keys:
            value = nested.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
                return _normalize_email(value)
    return ""


def _configured_parent_emails() -> tuple[set[str], bool]:
    settings = ProviderSettingsRepository().resolve_runtime_settings("mailbox", "gmail_api_code", {})
    pool_text = str(settings.get("gmail_api_code_pool_text") or "")
    parents = {_resolve_gmail_parent(item.email) for item in parse_gmail_api_code_entries(pool_text)}
    return {item for item in parents if item}, bool(pool_text.strip())


def _empty_parent(parent: str, configured: bool) -> dict[str, Any]:
    return {
        "parent_email": parent,
        "configured": configured,
        "alias_limit": ALIAS_LIMIT,
        "successful_alias_count": 0,
        "allocated_only_count": 0,
        "confirmed_remaining": ALIAS_LIMIT,
        "conservative_remaining": ALIAS_LIMIT,
        "main_registered": False,
        "email_status": "usable",
        "email_status_reason": "",
        "status": "available",
        "successful_aliases": [],
        "allocated_only_aliases": [],
        "first_seen_at": "",
        "last_seen_at": "",
    }


def _add_seen(item: dict[str, Any], value: datetime | None) -> None:
    if value is None:
        return
    text = value.isoformat()
    if not item["first_seen_at"] or text < item["first_seen_at"]:
        item["first_seen_at"] = text
    if not item["last_seen_at"] or text > item["last_seen_at"]:
        item["last_seen_at"] = text


def _runtime_invalid_emails() -> set[str]:
    try:
        with GmailApiCodeMailbox._CLAIM_LOCK:
            return set(GmailApiCodeMailbox._INVALID_EMAILS)
    except Exception:
        return set()


def _mark_email_status(item: dict[str, Any], status: str, reason: str = "") -> None:
    current = str(item.get("email_status") or "usable")
    if current == "unusable":
        return
    if status == "unusable" or (status == "registered" and current == "usable"):
        item["email_status"] = status
        item["email_status_reason"] = reason or item.get("email_status_reason") or ""


def gmail_api_code_alias_usage() -> dict[str, Any]:
    configured_parents, config_pool_recorded = _configured_parent_emails()
    parents: dict[str, dict[str, Any]] = {
        parent: _empty_parent(parent, True)
        for parent in configured_parents
    }
    for parent in _runtime_invalid_emails():
        parent = _resolve_gmail_parent(parent)
        if not parent:
            continue
        item = parents.setdefault(parent, _empty_parent(parent, parent in configured_parents))
        _mark_email_status(item, "unusable", "runtime_invalid")
    successful_aliases_by_parent: dict[str, set[str]] = defaultdict(set)
    allocated_aliases_by_parent: dict[str, set[str]] = defaultdict(set)

    with Session(engine) as session:
        mailbox_resources = session.exec(
            select(ProviderResourceModel)
            .where(ProviderResourceModel.provider_type == "mailbox")
            .where(ProviderResourceModel.provider_name == "gmail_api_code")
        ).all()
        for resource in mailbox_resources:
            metadata = _safe_json(resource.metadata_json)
            resource_email = _normalize_email(resource.handle or metadata.get("email"))
            raw_parent = (
                _metadata_text(metadata, "alias_parent_email", "email_alias_parent", "parent_email", "main_email")
                or _metadata_text(metadata, "account_id", "parent_account_id")
                or resource_email
            )
            parent = _resolve_gmail_parent(raw_parent)
            if not parent:
                continue
            item = parents.setdefault(parent, _empty_parent(parent, parent in configured_parents))
            _add_seen(item, resource.created_at)
            _add_seen(item, resource.updated_at)
            registration_status = str(metadata.get("registration_status") or "").strip().lower()
            if resource_email == parent and (
                registration_status == "invalid"
                or metadata.get("registration_invalid")
            ):
                _mark_email_status(
                    item,
                    "unusable",
                    str(metadata.get("registration_invalid_reason") or registration_status or "invalid"),
                )
            elif resource_email == parent and (
                registration_status == "registered"
                or metadata.get("registration_success")
            ):
                item["main_registered"] = True
                _mark_email_status(item, "registered", "registration_success")

        resources = session.exec(
            select(ProviderResourceModel, AccountModel)
            .join(AccountModel, AccountModel.id == ProviderResourceModel.account_id)
            .where(ProviderResourceModel.provider_type == "mailbox")
            .where(ProviderResourceModel.provider_name == "gmail_api_code")
        ).all()

        for resource, account in resources:
            metadata = _safe_json(resource.metadata_json)
            alias_email = (
                _metadata_text(metadata, "alias_email", "email")
                or _normalize_email(resource.handle)
                or _normalize_email(account.email)
            )
            raw_parent = (
                _metadata_text(metadata, "alias_parent_email", "email_alias_parent", "parent_email", "main_email")
                or _metadata_text(metadata, "account_id", "parent_account_id")
                or alias_email
            )
            parent = _resolve_gmail_parent(raw_parent)
            if not parent:
                continue
            item = parents.setdefault(parent, _empty_parent(parent, parent in configured_parents))
            _add_seen(item, resource.created_at)
            _add_seen(item, account.created_at)
            if alias_email == parent:
                item["main_registered"] = True
                _mark_email_status(item, "registered", "main_registered")
                continue
            if _gmail_parent(alias_email) == parent:
                successful_aliases_by_parent[parent].add(alias_email)

        events = session.exec(
            select(TaskEventModel, TaskModel)
            .join(TaskModel, TaskModel.id == TaskEventModel.task_id)
            .where(TaskModel.payload_json.like("%gmail_api_code%"))
            .where(TaskEventModel.message.like("%Email alias allocated:%"))
            .order_by(TaskEventModel.id)
        ).all()

        for event, task in events:
            payload = _safe_json(task.payload_json)
            extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
            if str(extra.get("mail_provider") or "").strip() != "gmail_api_code":
                continue
            match = _ALLOCATED_RE.search(event.message or "")
            if not match:
                continue
            alias_email = _normalize_email(match.group(1))
            parent = _resolve_gmail_parent(match.group(2))
            if not parent:
                continue
            if _gmail_parent(alias_email) != parent:
                continue
            item = parents.setdefault(parent, _empty_parent(parent, parent in configured_parents))
            allocated_aliases_by_parent[parent].add(alias_email)
            _add_seen(item, event.created_at)

    for parent, item in parents.items():
        successful_aliases = sorted(successful_aliases_by_parent.get(parent, set()))
        allocated_only_aliases = sorted(allocated_aliases_by_parent.get(parent, set()) - set(successful_aliases))
        success_count = len(successful_aliases)
        allocated_only_count = len(allocated_only_aliases)
        confirmed_remaining = max(0, ALIAS_LIMIT - success_count)
        conservative_remaining = max(0, ALIAS_LIMIT - success_count - allocated_only_count)
        if item.get("email_status") != "usable":
            confirmed_remaining = 0
            conservative_remaining = 0
        item.update(
            {
                "configured": parent in configured_parents,
                "successful_alias_count": success_count,
                "allocated_only_count": allocated_only_count,
                "confirmed_remaining": confirmed_remaining,
                "conservative_remaining": conservative_remaining,
                "successful_aliases": successful_aliases,
                "allocated_only_aliases": allocated_only_aliases,
            }
        )
        if success_count >= ALIAS_LIMIT:
            item["status"] = "full"
        elif allocated_only_count > 0:
            item["status"] = "has_unconfirmed"
        else:
            item["status"] = "available"

    items = sorted(
        parents.values(),
        key=lambda item: (
            not bool(item["configured"]),
            item["conservative_remaining"],
            item["parent_email"],
        ),
    )
    total_success = sum(int(item["successful_alias_count"]) for item in items)
    total_allocated_only = sum(int(item["allocated_only_count"]) for item in items)
    total_confirmed_remaining = sum(int(item["confirmed_remaining"]) for item in items)
    total_conservative_remaining = sum(int(item["conservative_remaining"]) for item in items)
    unusable_parent_count = sum(1 for item in items if item["email_status"] == "unusable")
    registered_parent_count = sum(1 for item in items if item["email_status"] == "registered")
    return {
        "alias_limit": ALIAS_LIMIT,
        "config_pool_recorded": config_pool_recorded,
        "summary": {
            "parent_count": len(items),
            "configured_parent_count": len(configured_parents),
            "usable_parent_count": sum(1 for item in items if item["email_status"] == "usable"),
            "unusable_parent_count": unusable_parent_count,
            "registered_parent_count": registered_parent_count,
            "successful_alias_count": total_success,
            "allocated_only_count": total_allocated_only,
            "confirmed_remaining": total_confirmed_remaining,
            "conservative_remaining": total_conservative_remaining,
            "full_parent_count": sum(1 for item in items if item["status"] == "full"),
            "unconfirmed_parent_count": sum(1 for item in items if item["status"] == "has_unconfirmed"),
        },
        "items": items,
    }
