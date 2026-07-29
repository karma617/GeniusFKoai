from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from core.datetime_utils import serialize_datetime
from core.account_display import build_account_display_summary
from core.db import AccountModel, engine
from core.account_graph import (
    RT_RUNTIME_SUMMARY_KEYS,
    compute_account_stats,
    load_account_graphs,
    matches_status_filter,
    patch_account_graph,
    purge_account_graph,
    sync_account_graph,
)
from core.platform_accounts import resolve_primary_token
from domain.accounts import (
    AccountBatchStatusUpdateCommand,
    AccountCreateCommand,
    AccountDeleteInvalidBannedCommand,
    AccountExportSelection,
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountStats,
    AccountUpdateCommand,
)

CHATGPT_BATCH_STATUS_UPDATE_STATUSES = {
    "registered",
    "rt_pending_upload",
    "rt_uploaded",
    "trial",
    "subscribed",
    "expired",
    "relogin_required",
    "invalid",
    "banned",
}

PLAN_DERIVED_SUMMARY_KEYS = {
    "membership_type",
    "individual_membership_type",
    "plan",
    "plan_name",
    "plan_state",
    "trial_eligible",
}


def _tag_text(value: object) -> str:
    return str(value or "").strip().lower()


def _record_tag_values(item: AccountRecord) -> set[str]:
    overview = item.overview if isinstance(item.overview, dict) else {}
    legacy_extra = overview.get("legacy_extra") if isinstance(overview.get("legacy_extra"), dict) else {}
    display_summary = item.display_summary if isinstance(item.display_summary, dict) else {}
    values: set[str] = set()

    for chip in overview.get("chips") or []:
        text = _tag_text(chip)
        if text:
            values.add(text)
    for badge in display_summary.get("badges") or []:
        if isinstance(badge, dict):
            text = _tag_text(badge.get("label"))
            if text:
                values.add(text)

    for value in (
        item.lifecycle_status,
        item.display_status,
        item.plan_state,
        item.plan_name,
        overview.get("plan"),
        overview.get("plan_name"),
        overview.get("membership_type"),
        overview.get("individual_membership_type"),
        overview.get("registration_mode_label"),
        legacy_extra.get("registration_mode_label"),
    ):
        text = _tag_text(value)
        if text and text != "unknown":
            values.add(text)

    if overview.get("k12_workspace_id") or overview.get("k12_session") or (isinstance(overview.get("k12"), dict) and overview["k12"].get("session")):
        values.add("k12")
    if bool(overview.get("bugfree")):
        values.add("bugfree")
    if bool(overview.get("chatgpt_free_plus_trial")):
        values.add("试用")
    if bool(overview.get("mfa_enabled")):
        values.add("2fa已绑")
    for credential in item.credentials or []:
        if _tag_text(credential.get("key")) == "plan_type" and _tag_text(credential.get("value")) == "k12":
            values.add("k12")
        if _tag_text(credential.get("key")) == "totp_secret" and _tag_text(credential.get("value")):
            values.add("2fa已绑")

    if "plus" in values or "subscribed" in values:
        values.add("plus")
    if "free" in values or "registered" in values:
        values.add("free")
    if "bugfree" in values:
        values.add("bugfree")
    return values


def _matches_tag_filter(item: AccountRecord, tag: str) -> bool:
    expected = _tag_text(tag)
    if not expected:
        return True
    return expected in _record_tag_values(item)


def _build_summary_updates(
    overview: dict | None,
    *,
    cashier_url: str | None = None,
    region: str | None = None,
    trial_end_time: int | None = None,
) -> dict | None:
    summary = dict(overview or {})
    if cashier_url is not None:
        summary["cashier_url"] = cashier_url
    if region is not None:
        summary["region"] = region
    if trial_end_time is not None:
        summary["trial_end_time"] = int(trial_end_time or 0)
    return summary or None


def _build_credential_updates(
    credentials: dict | None,
) -> dict | None:
    return dict(credentials or {}) or None


def _build_batch_status_patch(lifecycle_status: str) -> tuple[dict, set[str]]:
    now_text = serialize_datetime(datetime.now(timezone.utc)) or ""
    summary_updates: dict = {
        "manual_status_updated_at": now_text,
        "manual_status_source": "accounts.batch_status",
    }
    remove_keys = set(RT_RUNTIME_SUMMARY_KEYS)

    if lifecycle_status == "registered":
        summary_updates["valid"] = True
        remove_keys.update(PLAN_DERIVED_SUMMARY_KEYS)
    elif lifecycle_status == "rt_pending_upload":
        summary_updates.update({
            "valid": True,
            "rt_upload_status": "pending_upload",
            "rt_upload_checked_at": now_text,
            "rt_acquired_at": now_text,
        })
    elif lifecycle_status == "rt_uploaded":
        summary_updates.update({
            "valid": True,
            "rt_upload_status": "uploaded",
            "rt_upload_checked_at": now_text,
            "rt_acquired_at": now_text,
            "rt_uploaded_at": now_text,
        })
    elif lifecycle_status in {"trial", "subscribed", "expired"}:
        summary_updates.update({
            "valid": True,
            "plan_state": lifecycle_status,
        })
    elif lifecycle_status in {"invalid", "banned"}:
        summary_updates["valid"] = False
    elif lifecycle_status == "relogin_required":
        summary_updates.update({
            "valid": False,
            "validity_status": "unknown",
            "display_status": "relogin_required",
        })

    return summary_updates, remove_keys


def _to_record(model: AccountModel, graph: dict | None = None) -> AccountRecord:
    graph = graph or {}
    overview = graph.get("overview") or {}
    lifecycle_status = graph.get("lifecycle_status") or "registered"
    validity_status = graph.get("validity_status") or "unknown"
    plan_state = graph.get("plan_state") or "unknown"
    plan_name = graph.get("plan_name") or ""
    display_status = graph.get("display_status") or "registered"
    provider_resources = list(graph.get("provider_resources") or [])
    return AccountRecord(
        id=int(model.id or 0),
        platform=model.platform,
        email=model.email,
        password=model.password,
        user_id=model.user_id,
        primary_token=resolve_primary_token(model, graph),
        trial_end_time=int(overview.get("trial_end_time") or 0),
        cashier_url=str(overview.get("cashier_url") or ""),
        lifecycle_status=lifecycle_status,
        validity_status=validity_status,
        plan_state=plan_state,
        plan_name=plan_name,
        display_status=display_status,
        overview=overview,
        display_summary=build_account_display_summary(
            platform=model.platform,
            email=model.email,
            lifecycle_status=lifecycle_status,
            validity_status=validity_status,
            plan_state=plan_state,
            plan_name=plan_name,
            display_status=display_status,
            overview=overview,
            provider_resources=provider_resources,
        ),
        credentials=list(graph.get("credentials") or []),
        provider_accounts=list(graph.get("provider_accounts") or []),
        provider_resources=provider_resources,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class AccountsRepository:
    @staticmethod
    def _load_records(session: Session, models: list[AccountModel]) -> list[AccountRecord]:
        account_ids = [int(model.id or 0) for model in models if model.id]
        graphs = load_account_graphs(session, account_ids)
        missing = [model for model in models if int(model.id or 0) not in graphs]
        if missing:
            for model in missing:
                sync_account_graph(session, model)
            session.commit()
            graphs = load_account_graphs(session, account_ids)
        return [_to_record(model, graphs.get(int(model.id or 0), {})) for model in models]

    def list(self, query: AccountQuery) -> tuple[int, list[AccountRecord]]:
        page = max(query.page, 1)
        page_size = max(query.page_size, 1)
        with Session(engine) as session:
            statement = select(AccountModel)
            count_statement = select(func.count()).select_from(AccountModel)
            if query.platform:
                statement = statement.where(AccountModel.platform == query.platform)
                count_statement = count_statement.where(AccountModel.platform == query.platform)
            if query.email:
                statement = statement.where(AccountModel.email.contains(query.email))
                count_statement = count_statement.where(AccountModel.email.contains(query.email))
            statement = statement.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            if not query.status and not query.tag:
                total = int(session.exec(count_statement).one() or 0)
                start = (page - 1) * page_size
                models = session.exec(statement.offset(start).limit(page_size)).all()
                return total, self._load_records(session, models)
            models = session.exec(statement).all()
            records = self._load_records(session, models)
            if query.status:
                records = [item for item in records if matches_status_filter({
                    "display_status": item.display_status,
                    "lifecycle_status": item.lifecycle_status,
                    "plan_state": item.plan_state,
                    "validity_status": item.validity_status,
                }, query.status)]
            if query.tag:
                records = [item for item in records if _matches_tag_filter(item, query.tag)]
        total = len(records)
        start = (page - 1) * page_size
        end = start + page_size
        return total, records[start:end]

    def get(self, account_id: int) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            records = self._load_records(session, [model])
            return records[0] if records else None

    def select_for_export(self, selection: AccountExportSelection) -> list[AccountRecord]:
        with Session(engine) as session:
            statement = select(AccountModel)
            if selection.platform:
                statement = statement.where(AccountModel.platform == selection.platform)
            if selection.search_filter:
                statement = statement.where(AccountModel.email.contains(selection.search_filter))
            if not selection.select_all and selection.ids:
                statement = statement.where(AccountModel.id.in_(selection.ids))
            statement = statement.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            models = session.exec(statement).all()
            records = self._load_records(session, models)
        if selection.status_filter:
            records = [item for item in records if matches_status_filter({
                "display_status": item.display_status,
                "lifecycle_status": item.lifecycle_status,
                "plan_state": item.plan_state,
                "validity_status": item.validity_status,
            }, selection.status_filter)]
        if selection.tag_filter:
            records = [item for item in records if _matches_tag_filter(item, selection.tag_filter)]
        return records

    def create(self, command: AccountCreateCommand) -> AccountRecord:
        with Session(engine) as session:
            model = AccountModel(
                platform=command.platform,
                email=command.email,
                password=command.password,
                user_id=command.user_id,
            )
            session.add(model)
            session.commit()
            session.refresh(model)
            patch_account_graph(
                session,
                model,
                lifecycle_status=command.lifecycle_status,
                primary_token=command.primary_token or None,
                cashier_url=command.cashier_url or None,
                region=command.region or None,
                trial_end_time=command.trial_end_time or None,
                summary_updates=_build_summary_updates(
                    command.overview,
                    cashier_url=command.cashier_url or None,
                    region=command.region or None,
                    trial_end_time=command.trial_end_time or None,
                ),
                credential_updates=_build_credential_updates(command.credentials),
                provider_accounts=command.provider_accounts or None,
                provider_resources=command.provider_resources or None,
                replace_provider_accounts=bool(command.provider_accounts),
                replace_provider_resources=bool(command.provider_resources),
            )
            session.commit()
            return self._load_records(session, [model])[0]

    def update(self, account_id: int, command: AccountUpdateCommand) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            if command.password is not None:
                model.password = command.password
            if command.user_id is not None:
                model.user_id = command.user_id
            model.updated_at = datetime.now(timezone.utc)
            session.add(model)
            session.commit()
            session.refresh(model)
            patch_account_graph(
                session,
                model,
                lifecycle_status=command.lifecycle_status,
                primary_token=command.primary_token,
                cashier_url=command.cashier_url,
                region=command.region,
                trial_end_time=command.trial_end_time,
                summary_updates=_build_summary_updates(
                    command.overview,
                    cashier_url=command.cashier_url,
                    region=command.region,
                    trial_end_time=command.trial_end_time,
                ),
                credential_updates=_build_credential_updates(command.credentials),
                provider_accounts=command.provider_accounts,
                provider_resources=command.provider_resources,
                replace_provider_accounts=command.replace_provider_accounts,
                replace_provider_resources=command.replace_provider_resources,
            )
            session.commit()
            return self._load_records(session, [model])[0]

    def batch_update_status(self, command: AccountBatchStatusUpdateCommand) -> dict:
        platform = str(command.platform or "chatgpt").strip()
        lifecycle_status = str(command.lifecycle_status or "").strip()
        ids = list(dict.fromkeys(int(item) for item in command.ids if int(item or 0) > 0))
        if platform != "chatgpt":
            raise ValueError("batch status update only supports chatgpt accounts")
        if not ids:
            raise ValueError("select at least one account")
        if lifecycle_status not in CHATGPT_BATCH_STATUS_UPDATE_STATUSES:
            raise ValueError("unsupported account status")

        summary_updates, remove_keys = _build_batch_status_patch(lifecycle_status)
        with Session(engine) as session:
            models = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == platform)
                .where(AccountModel.id.in_(ids))
            ).all()
            by_id = {int(model.id or 0): model for model in models}
            updated_ids: list[int] = []
            for account_id in ids:
                model = by_id.get(account_id)
                if not model:
                    continue
                patch_account_graph(
                    session,
                    model,
                    lifecycle_status=lifecycle_status,
                    summary_updates=summary_updates,
                    summary_remove_keys=remove_keys,
                )
                model.updated_at = datetime.now(timezone.utc)
                session.add(model)
                updated_ids.append(account_id)
            if not updated_ids:
                raise ValueError("no accounts matched selected ids")
            session.commit()

        return {
            "updated": len(updated_ids),
            "updated_ids": updated_ids,
            "missing_ids": [account_id for account_id in ids if account_id not in set(updated_ids)],
            "platform": platform,
            "lifecycle_status": lifecycle_status,
        }

    def delete(self, account_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return False
            purge_account_graph(session, account_id)
            session.delete(model)
            session.commit()
            return True

    def delete_invalid_and_banned(self, command: AccountDeleteInvalidBannedCommand) -> dict:
        platform = str(command.platform or "chatgpt").strip() or "chatgpt"
        with Session(engine) as session:
            statement = select(AccountModel)
            if platform:
                statement = statement.where(AccountModel.platform == platform)
            models = session.exec(statement).all()
            records = self._load_records(session, models)
            delete_ids = [
                item.id
                for item in records
                if (
                    item.lifecycle_status in {"invalid", "banned"}
                    or item.display_status in {"invalid", "banned"}
                    or item.validity_status in {"invalid", "banned"}
                )
            ]
            delete_id_set = set(delete_ids)
            for model in models:
                account_id = int(model.id or 0)
                if account_id not in delete_id_set:
                    continue
                purge_account_graph(session, account_id)
                session.delete(model)
            session.commit()
        return {
            "ok": True,
            "platform": platform,
            "deleted": len(delete_ids),
            "deleted_ids": delete_ids,
            "statuses": ["invalid", "banned"],
        }

    def import_lines(self, platform: str, lines: list[AccountImportLine]) -> int:
        created = 0
        with Session(engine) as session:
            for line in lines:
                model = AccountModel(
                    platform=platform,
                    email=line.email,
                    password=line.password,
                )
                session.add(model)
                created += 1
            session.commit()
            models = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == platform)
                .order_by(AccountModel.id.desc())
                .limit(created)
            ).all()
            by_email = {line.email: line for line in lines}
            for model in models:
                line = by_email.get(model.email)
                if not line:
                    sync_account_graph(session, model)
                    continue
                extra = dict(line.extra or {})
                summary_updates = dict(extra.get("overview") or extra.get("summary") or {})
                for key in ("trial_end_time", "cashier_url", "region", "remote_email", "checked_at"):
                    if key in extra and key not in summary_updates:
                        summary_updates[key] = extra[key]
                legacy_extra = {
                    key: value
                    for key, value in extra.items()
                    if key not in {
                        "overview",
                        "summary",
                        "primary_token",
                        "token",
                        "lifecycle_status",
                        "status",
                        "cashier_url",
                        "trial_end_time",
                        "region",
                        "remote_email",
                        "checked_at",
                        "credentials",
                        "provider_accounts",
                        "provider_resources",
                    }
                    and value not in (None, "", [], {})
                }
                if legacy_extra:
                    summary_updates["legacy_extra"] = legacy_extra
                credential_updates = dict(extra.get("credentials") or {})
                for key in (
                    "access_token",
                    "refresh_token",
                    "session_token",
                    "id_token",
                    "accessToken",
                    "refreshToken",
                    "sessionToken",
                    "idToken",
                    "cookies",
                    "cookie",
                    "api_key",
                    "wos_session",
                    "sso",
                    "sso_rw",
                ):
                    if key in extra and key not in credential_updates:
                        credential_updates[key] = extra[key]
                primary_token = extra.get("primary_token")
                if primary_token in (None, ""):
                    primary_token = extra.get("token")
                patch_account_graph(
                    session,
                    model,
                    lifecycle_status=str(extra.get("lifecycle_status") or extra.get("status") or "registered"),
                    primary_token=str(primary_token or "") or None,
                    cashier_url=str(extra.get("cashier_url") or "") or None,
                    summary_updates=summary_updates or None,
                    credential_updates=credential_updates or None,
                    provider_accounts=list(extra.get("provider_accounts") or []) or None,
                    provider_resources=list(extra.get("provider_resources") or []) or None,
                    replace_provider_accounts=bool(extra.get("provider_accounts")),
                    replace_provider_resources=bool(extra.get("provider_resources")),
                )
            session.commit()
        return created

    def stats(self) -> AccountStats:
        with Session(engine) as session:
            accounts = session.exec(select(AccountModel).order_by(AccountModel.created_at.desc(), AccountModel.id.desc())).all()
            records = self._load_records(session, accounts)
        stats = compute_account_stats(
            [
                {
                    "lifecycle_status": item.lifecycle_status,
                    "plan_state": item.plan_state,
                    "validity_status": item.validity_status,
                    "display_status": item.display_status,
                }
                for item in records
            ],
            [item.platform for item in records],
        )
        return AccountStats(
            total=len(records),
            by_platform=stats["by_platform"],
            by_status=stats["by_display_status"],
            by_lifecycle_status=stats["by_lifecycle_status"],
            by_plan_state=stats["by_plan_state"],
            by_validity_status=stats["by_validity_status"],
            by_display_status=stats["by_display_status"],
        )

    def export_csv(self, query: AccountQuery) -> str:
        _, items = self.list(query)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "platform",
                "email",
                "password",
                "user_id",
                "display_status",
                "lifecycle_status",
                "plan_state",
                "validity_status",
                "cashier_url",
                "created_at",
            ]
        )
        for item in items:
            writer.writerow([
                item.platform,
                item.email,
                item.password,
                item.user_id,
                item.display_status,
                item.lifecycle_status,
                item.plan_state,
                item.validity_status,
                item.cashier_url,
                serialize_datetime(item.created_at) or "",
            ])
        return output.getvalue()
