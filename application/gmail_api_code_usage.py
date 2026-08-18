from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from core.db import AccountModel, ProviderResourceModel, TaskEventModel, TaskModel, engine
from core.gmail_api_code_mailbox import GmailApiCodeMailbox, parse_gmail_api_code_pool_rows
from infrastructure.provider_settings_repository import ProviderSettingsRepository


ICLOUD_ALIAS_LIMIT = 1
DIRECT_MAILBOX_LIMIT = 1
_ALLOCATED_RE = re.compile(r"Email alias allocated:\s+([^\s]+)\s+parent=([^\s]+)")
_SUPPORTED_DOMAINS = {"gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com"}
_ICLOUD_DOMAINS = {"icloud.com", "me.com", "mac.com"}


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _safe_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _mailbox_kind(email: str) -> str:
    target = _normalize_email(email)
    if "@" not in target:
        return ""
    domain = target.rsplit("@", 1)[1]
    if domain in {"gmail.com", "googlemail.com"}:
        return "gmail"
    if domain in _ICLOUD_DOMAINS:
        return "icloud"
    return ""


def _pool_parent(email: str) -> str:
    target = _normalize_email(email)
    if "@" not in target:
        return ""
    local, domain = target.split("@", 1)
    if domain not in _SUPPORTED_DOMAINS:
        return ""
    if domain == "googlemail.com":
        domain = "gmail.com"
    return f"{local.split('+', 1)[0]}@{domain}"


def _resolve_pool_parent(*values: Any) -> str:
    for value in values:
        parent = _pool_parent(_normalize_email(value))
        if parent:
            return parent
    return ""


def _mailbox_limit(parent: str) -> int:
    return DIRECT_MAILBOX_LIMIT + ICLOUD_ALIAS_LIMIT if _mailbox_kind(parent) == "icloud" else DIRECT_MAILBOX_LIMIT


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
    parents = {
        _resolve_pool_parent(item.email)
        for item in parse_gmail_api_code_pool_rows(pool_text)
        if item.status != "deleted"
    }
    return {item for item in parents if item}, bool(pool_text.strip())


def _configured_parent_statuses() -> dict[str, tuple[str, str]]:
    settings = ProviderSettingsRepository().resolve_runtime_settings("mailbox", "gmail_api_code", {})
    pool_text = str(settings.get("gmail_api_code_pool_text") or "")
    statuses: dict[str, tuple[str, str]] = {}
    for row in parse_gmail_api_code_pool_rows(pool_text):
        parent = _resolve_pool_parent(row.email)
        if not parent or row.status in {"active", "deleted"}:
            continue
        if row.status == "invalid":
            statuses[parent] = ("unusable", "pool_invalid")
        elif row.status == "registered_exhausted":
            statuses[parent] = ("registered_exhausted", "pool_registered_exhausted")
        elif row.status == "registered":
            statuses[parent] = ("registered", "pool_registered")
    return statuses


def _empty_parent(parent: str, configured: bool) -> dict[str, Any]:
    return {
        "parent_email": parent,
        "mailbox_type": _mailbox_kind(parent),
        "configured": configured,
        "alias_limit": _mailbox_limit(parent),
        "successful_alias_count": 0,
        "allocated_only_count": 0,
        "confirmed_remaining": _mailbox_limit(parent),
        "conservative_remaining": _mailbox_limit(parent),
        "main_registered": False,
        "alias_exhausted": False,
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
    if current == "unusable" and status != "unusable":
        return
    if status == "unusable" or (
        status == "registered_exhausted" and current != "unusable"
    ) or (
        status == "registered" and current == "usable"
    ):
        item["email_status"] = status
        item["email_status_reason"] = reason or item.get("email_status_reason") or ""


def gmail_api_code_alias_usage() -> dict[str, Any]:
    configured_parents, config_pool_recorded = _configured_parent_emails()
    configured_statuses = _configured_parent_statuses()
    parents: dict[str, dict[str, Any]] = {
        parent: _empty_parent(parent, True)
        for parent in configured_parents
    }
    for parent, (status, reason) in configured_statuses.items():
        if parent not in parents:
            continue
        if status in {"registered", "registered_exhausted"}:
            parents[parent]["main_registered"] = True
        if status == "registered_exhausted":
            parents[parent]["alias_exhausted"] = True
        _mark_email_status(parents[parent], status, reason)
    for parent in _runtime_invalid_emails():
        parent = _resolve_pool_parent(parent)
        if not parent or parent not in configured_parents:
            continue
        item = parents[parent]
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
            parent = _resolve_pool_parent(raw_parent)
            if not parent or parent not in configured_parents:
                continue
            item = parents[parent]
            _add_seen(item, resource.created_at)
            _add_seen(item, resource.updated_at)
            registration_status = str(metadata.get("registration_status") or "").strip().lower()
            invalid_reason = str(metadata.get("registration_invalid_reason") or "").strip().lower()
            if (
                registration_status == "invalid"
                and invalid_reason in {"user_already_exists", "registration_disallowed"}
            ):
                item["main_registered"] = True
                item["alias_exhausted"] = True
                _mark_email_status(item, "registered_exhausted", invalid_reason)
            elif (
                registration_status == "invalid"
                or metadata.get("registration_invalid")
            ):
                _mark_email_status(
                    item,
                    "unusable",
                    str(metadata.get("registration_invalid_reason") or registration_status or "invalid"),
                )
            elif (
                registration_status == "registered_exhausted"
                or metadata.get("registration_alias_exhausted")
            ):
                item["main_registered"] = True
                item["alias_exhausted"] = True
                _mark_email_status(
                    item,
                    "registered_exhausted",
                    str(
                        metadata.get("registration_alias_exhausted_reason")
                        or registration_status
                        or "registered_exhausted"
                    ),
                )
            elif (
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
            parent = _resolve_pool_parent(raw_parent)
            if not parent or parent not in configured_parents:
                continue
            item = parents[parent]
            _add_seen(item, resource.created_at)
            _add_seen(item, account.created_at)
            alias_parent = _pool_parent(alias_email)
            if alias_parent != parent:
                continue
            if alias_email == parent:
                item["main_registered"] = True
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
            parent = _resolve_pool_parent(match.group(2))
            if not parent or parent not in configured_parents:
                continue
            if _pool_parent(alias_email) != parent:
                continue
            item = parents[parent]
            allocated_aliases_by_parent[parent].add(alias_email)
            _add_seen(item, event.created_at)

    for parent, item in parents.items():
        successful_aliases = sorted(successful_aliases_by_parent.get(parent, set()))
        allocated_only_aliases = sorted(allocated_aliases_by_parent.get(parent, set()) - set(successful_aliases))
        if item.get("main_registered") and parent not in successful_aliases:
            successful_aliases.append(parent)
            successful_aliases.sort()
        success_count = len(successful_aliases)
        allocated_only_count = len(allocated_only_aliases)
        limit = int(item.get("alias_limit") or DIRECT_MAILBOX_LIMIT)
        confirmed_remaining = max(0, limit - success_count)
        conservative_remaining = max(0, limit - success_count - allocated_only_count)
        if item.get("email_status") in {"unusable", "registered_exhausted"}:
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
        if item.get("email_status") in {"unusable", "registered_exhausted"} or confirmed_remaining <= 0:
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
    registered_parent_count = sum(
        1 for item in items if item["email_status"] in {"registered", "registered_exhausted"}
    )
    return {
        "alias_limit": DIRECT_MAILBOX_LIMIT,
        "config_pool_recorded": config_pool_recorded,
        "summary": {
            "parent_count": len(items),
            "configured_parent_count": len(configured_parents),
            "usable_parent_count": sum(1 for item in items if int(item["conservative_remaining"]) > 0),
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
