"""Gmail OAuth mailbox provider with Gmail alias fission."""

from __future__ import annotations

import base64
import html
import json
import random
import re
import secrets
import string
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link
from core.db import AccountModel, ProviderResourceModel, engine


GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_OAUTH_REDIRECT_URI = "http://127.0.0.1:53682/"
GMAIL_OAUTH_MOTHER_USAGE_LIMIT = 6


def _bool_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _loads_json(value: str, label: str) -> dict:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"Gmail OAuth 缺少{label}")
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"Gmail OAuth {label}不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Gmail OAuth {label}必须是 JSON 对象")
    return payload


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return ""


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def gmail_oauth_authorization_url(credentials_json: str) -> tuple[str, str]:
    from google_auth_oauthlib.flow import Flow

    client_config = _loads_json(credentials_json, "credentials_json")
    flow = Flow.from_client_config(
        client_config,
        scopes=[GMAIL_MODIFY_SCOPE],
        redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return auth_url, flow.code_verifier


def gmail_oauth_exchange_code(credentials_json: str, code: str, code_verifier: str = "") -> str:
    from google_auth_oauthlib.flow import Flow

    client_config = _loads_json(credentials_json, "credentials_json")
    auth_code = str(code or "").strip()
    if not auth_code:
        raise RuntimeError("Gmail OAuth 授权码不能为空")
    flow = Flow.from_client_config(
        client_config,
        scopes=[GMAIL_MODIFY_SCOPE],
        redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
    )
    flow.fetch_token(code=auth_code, code_verifier=str(code_verifier or "").strip() or None)
    return flow.credentials.to_json()


class GmailOAuthMailbox(BaseMailbox):
    """Use one authorized Gmail mailbox and allocate Gmail alias addresses."""

    _MYSTIC_NAMES = ("leo", "nova", "kai", "luna", "milo", "iris", "axel", "zara")
    _MYSTIC_NOUNS = ("fox", "river", "comet", "lotus", "cloud", "ember", "aurora", "tiger")
    _CLAIM_LOCK = threading.Lock()
    _ACTIVE_CLAIMS: dict[str, tuple[str, float]] = {}
    _CLAIM_TTL_SECONDS = 60 * 60

    def __init__(
        self,
        *,
        master_email: str = "",
        fission_enable: str = "",
        fission_mode: str = "suffix",
        suffix_mode: str = "mystic",
        suffix_len_min: str = "8",
        suffix_len_max: str = "12",
        credentials_json: str = "",
        token_json: str = "",
        pool_json: str = "",
        proxy: str | None = None,
    ):
        self.master_email = _normalize_email(master_email)
        self.fission_enable = _bool_value(fission_enable)
        self.fission_mode = str(fission_mode or "suffix").strip().lower()
        self.suffix_mode = str(suffix_mode or "mystic").strip().lower()
        self.suffix_len_min = self._safe_int(suffix_len_min, 8)
        self.suffix_len_max = self._safe_int(suffix_len_max, self.suffix_len_min)
        self.credentials_json = str(credentials_json or "").strip()
        self.token_json = str(token_json or "").strip()
        self.pool_json = str(pool_json or "").strip()
        self.proxy = str(proxy or "").strip() or None
        self._services: dict[str, Any] = {}
        self._mothers = self._load_mothers()

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _account_exists(email: str) -> bool:
        target = _normalize_email(email)
        if not target:
            return False
        with Session(engine) as session:
            return session.exec(select(AccountModel).where(AccountModel.email == target)).first() is not None

    @staticmethod
    def _is_registration_success_metadata(metadata: dict[str, Any]) -> bool:
        status = str(
            metadata.get("registration_status")
            or metadata.get("gmail_oauth_registration_status")
            or ""
        ).strip().lower()
        return (
            status in {"registered", "registration_success", "success"}
            or bool(metadata.get("gmail_oauth_registered"))
            or bool(metadata.get("registration_success"))
        )

    @classmethod
    def _registered_resource_exists(cls, email: str, master_email: str = "") -> bool:
        target = _normalize_email(email)
        master = _normalize_email(master_email)
        if not target:
            return False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_oauth_fission")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                row_email = _normalize_email(row.handle or metadata.get("email"))
                row_master = _normalize_email(metadata.get("master_email") or row.resource_identifier)
                if row_email != target:
                    continue
                if master and row_master and row_master != master:
                    continue
                if cls._is_registration_success_metadata(metadata):
                    return True
        return False

    @staticmethod
    def _same_gmail_alias_family(email: str, master_email: str) -> bool:
        target = _normalize_email(email)
        master = _normalize_email(master_email)
        if not target or not master or "@" not in target or "@" not in master:
            return False
        target_user, target_domain = target.split("@", 1)
        master_user, master_domain = master.split("@", 1)
        if target_domain != master_domain:
            return False
        target_base = target_user.split("+", 1)[0].replace(".", "")
        master_base = master_user.split("+", 1)[0].replace(".", "")
        return target_base == master_base

    @classmethod
    def _usage_count(cls, master_email: str) -> int:
        master = _normalize_email(master_email)
        if not master:
            return 0
        used_account_ids: set[int] = set()
        with Session(engine) as session:
            for row in session.exec(select(ProviderResourceModel).where(ProviderResourceModel.provider_type == "mailbox")).all():
                metadata = row.get_metadata()
                if _normalize_email(metadata.get("master_email")) == master:
                    used_account_ids.add(int(row.account_id or 0))
            for account in session.exec(select(AccountModel)).all():
                if cls._same_gmail_alias_family(account.email, master):
                    used_account_ids.add(int(account.id or 0))
        used_account_ids.discard(0)
        return len(used_account_ids)

    @classmethod
    def _prune_active_claims_locked(cls) -> None:
        now = time.time()
        expired = [
            email
            for email, (_master, claimed_at) in cls._ACTIVE_CLAIMS.items()
            if now - float(claimed_at or 0) > cls._CLAIM_TTL_SECONDS
        ]
        for email in expired:
            cls._ACTIVE_CLAIMS.pop(email, None)

    @classmethod
    def _is_active_claimed_locked(cls, email: str, master_email: str = "") -> bool:
        cls._prune_active_claims_locked()
        target = _normalize_email(email)
        if not target:
            return False
        claimed = cls._ACTIVE_CLAIMS.get(target)
        if not claimed:
            return False
        claimed_master = _normalize_email(claimed[0])
        master = _normalize_email(master_email)
        return not master or not claimed_master or claimed_master == master

    @classmethod
    def _active_claim_count_locked(cls, master_email: str) -> int:
        cls._prune_active_claims_locked()
        master = _normalize_email(master_email)
        return sum(1 for claimed_master, _claimed_at in cls._ACTIVE_CLAIMS.values() if _normalize_email(claimed_master) == master)

    @classmethod
    def _claim_email_locked(cls, email: str, master_email: str) -> None:
        cls._prune_active_claims_locked()
        cls._ACTIVE_CLAIMS[_normalize_email(email)] = (_normalize_email(master_email), time.time())

    @classmethod
    def _release_active_claim(cls, email: str) -> None:
        target = _normalize_email(email)
        if not target:
            return
        with cls._CLAIM_LOCK:
            cls._ACTIVE_CLAIMS.pop(target, None)

    @staticmethod
    def _manual_aliases(value: Any) -> list[str]:
        if isinstance(value, str):
            raw_items = re.split(r"[\n,，\s]+", value)
        elif isinstance(value, (list, tuple)):
            raw_items = list(value)
        else:
            raw_items = []
        aliases: list[str] = []
        for item in raw_items:
            email = _normalize_email(item)
            if email and email not in aliases:
                aliases.append(email)
        if len(aliases) > 5:
            raise RuntimeError("Gmail OAuth 每个母号最多只能配置 5 个手动子号")
        return aliases

    def _normalize_mother(self, item: dict[str, Any]) -> dict[str, Any]:
        mother = {
            "master_email": _normalize_email(item.get("master_email") or item.get("email")),
            "credentials_json": _json_text(item.get("credentials_json") or item.get("credentials")),
            "token_json": _json_text(item.get("token_json") or item.get("token")),
            "aliases": self._manual_aliases(item.get("aliases") or item.get("children") or item.get("child_emails")),
        }
        if not mother["master_email"] or "@" not in mother["master_email"]:
            raise RuntimeError("Gmail OAuth 母号池存在缺失或非法 master_email")
        _loads_json(mother["credentials_json"], f"{mother['master_email']} credentials_json")
        _loads_json(mother["token_json"], f"{mother['master_email']} token_json")
        for alias in mother["aliases"]:
            if not self._same_gmail_alias_family(alias, mother["master_email"]):
                raise RuntimeError(f"Gmail OAuth 手动子号不属于母号 {mother['master_email']}: {alias}")
        return mother

    def _load_mothers(self) -> list[dict[str, Any]]:
        if self.pool_json:
            try:
                payload = json.loads(self.pool_json)
            except Exception as exc:
                raise RuntimeError(f"Gmail OAuth 母号池 JSON 非法: {exc}") from exc
            if isinstance(payload, dict):
                items = payload.get("accounts") or payload.get("mothers") or []
            else:
                items = payload
            if not isinstance(items, list) or not items:
                raise RuntimeError("Gmail OAuth 母号池 JSON 必须是数组，或包含 accounts 数组")
            return [self._normalize_mother(dict(item or {})) for item in items]

        return [
            self._normalize_mother({
                "master_email": self.master_email,
                "credentials_json": self.credentials_json,
                "token_json": self.token_json,
                "aliases": [],
            })
        ]

    def _assert_ready(self) -> None:
        if not self._mothers:
            raise RuntimeError("Gmail OAuth 未配置可用母号")

    def _suffix_bounds(self, user_part: str) -> tuple[int, int]:
        min_len = max(1, min(32, self.suffix_len_min))
        max_len = max(1, min(32, self.suffix_len_max))
        if max_len < min_len:
            max_len = min_len
        available = 64 - len(user_part) - 1
        if available <= 0:
            return 0, 0
        min_len = min(min_len, available)
        max_len = min(max_len, available)
        return min_len, max(min_len, max_len)

    def _mystic_suffix(self, target_len: int) -> str:
        name = random.choice(self._MYSTIC_NAMES)
        noun = random.choice(self._MYSTIC_NOUNS)
        date_part = f"{random.randint(1, 12):02d}{random.randint(1, 28):02d}"
        year = str(random.randint(1990, 2012))
        seed = random.choice((f"{name}{noun}{date_part}", f"{noun}{name}{date_part}", f"{name}{date_part}{noun}", f"{name}{noun}{year}"))
        suffix = "".join(ch for ch in seed.lower() if ch.isalnum())
        if len(suffix) < target_len:
            suffix += "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(target_len - len(suffix)))
        return suffix[:target_len]

    def _random_suffix(self, user_part: str) -> str:
        min_len, max_len = self._suffix_bounds(user_part)
        if max_len <= 0:
            return ""
        target_len = min_len if self.suffix_mode == "fixed" else random.randint(min_len, max_len)
        if self.suffix_mode == "mystic":
            return self._mystic_suffix(target_len)
        alphabet = "0123456789abcdef"
        return "".join(secrets.choice(alphabet) for _ in range(target_len))

    @staticmethod
    def _dot_variant(user_part: str) -> str:
        if len(user_part) <= 1:
            return user_part
        chars = list(user_part)
        slots = len(chars) - 1
        count = random.randint(1, slots)
        positions = sorted(random.sample(range(1, slots + 1), count), reverse=True)
        for pos in positions:
            chars.insert(pos, ".")
        return "".join(chars)

    def _candidate_email(self, mother: dict[str, Any]) -> str:
        master_email = mother["master_email"]
        user_part, domain_part = master_email.split("@", 1)
        if not self.fission_enable:
            return master_email
        if self.fission_mode == "dot":
            return f"{self._dot_variant(user_part)}@{domain_part}".lower()
        suffix = self._random_suffix(user_part)
        return f"{user_part}+{suffix}@{domain_part}".lower() if suffix else master_email

    def _select_mother(self) -> tuple[dict[str, Any], int]:
        candidates: list[tuple[dict[str, Any], int]] = []
        for mother in self._mothers:
            used = self._usage_count(mother["master_email"])
            active = self._active_claim_count_locked(mother["master_email"])
            if used + active < GMAIL_OAUTH_MOTHER_USAGE_LIMIT:
                candidates.append((mother, used))
        if not candidates:
            raise RuntimeError(f"Gmail OAuth 母号池已耗尽：所有母号使用总数均达到 {GMAIL_OAUTH_MOTHER_USAGE_LIMIT}")
        return random.choice(candidates)

    def _select_email_for_mother(self, mother: dict[str, Any]) -> str:
        manual_aliases = [
            alias
            for alias in mother.get("aliases", [])
            if not self._account_exists(alias)
            and not self._registered_resource_exists(alias, mother["master_email"])
            and not self._is_active_claimed_locked(alias, mother["master_email"])
        ]
        if manual_aliases:
            return random.choice(manual_aliases)
        attempts = 40 if self.fission_enable else 1
        for _ in range(attempts):
            email = self._candidate_email(mother)
            if (
                not self._account_exists(email)
                and not self._registered_resource_exists(email, mother["master_email"])
                and not self._is_active_claimed_locked(email, mother["master_email"])
            ):
                return email
        raise RuntimeError(f"Gmail OAuth 母号 {mother['master_email']} 未找到可用子号")

    def mark_registration_success(self, account: MailboxAccount) -> list[str]:
        email = _normalize_email(getattr(account, "email", ""))
        if not email:
            return []
        self._release_active_claim(email)
        master = _normalize_email(getattr(account, "account_id", ""))
        if not master:
            metadata = ((getattr(account, "extra", {}) or {}).get("provider_resource") or {}).get("metadata", {})
            master = _normalize_email(metadata.get("master_email"))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        updated = False
        with Session(engine) as session:
            rows = session.exec(
                select(ProviderResourceModel)
                .where(ProviderResourceModel.provider_type == "mailbox")
                .where(ProviderResourceModel.provider_name == "gmail_oauth_fission")
            ).all()
            for row in rows:
                metadata = row.get_metadata()
                row_email = _normalize_email(row.handle or metadata.get("email"))
                row_master = _normalize_email(metadata.get("master_email") or row.resource_identifier)
                if row_email != email:
                    continue
                if master and row_master and row_master != master:
                    continue
                metadata.update(
                    {
                        "email": email,
                        "master_email": master or row_master,
                        "registration_status": "registered",
                        "gmail_oauth_registered": True,
                        "registration_success": True,
                        "registration_success_at": now,
                    }
                )
                row.set_metadata(metadata)
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                updated = True
            if updated:
                session.commit()
        return ["Gmail子号已注册"] if updated or self._account_exists(email) else []

    def get_email(self) -> MailboxAccount:
        self._assert_ready()
        skipped: list[str] = []
        with self._CLAIM_LOCK:
            for _ in range(max(1, len(self._mothers) * 2)):
                mother, used_count = self._select_mother()
                try:
                    email = self._select_email_for_mother(mother)
                except RuntimeError as exc:
                    skipped.append(str(exc))
                    continue
                active_count = self._active_claim_count_locked(mother["master_email"])
                if used_count + active_count >= GMAIL_OAUTH_MOTHER_USAGE_LIMIT:
                    continue
                self._claim_email_locked(email, mother["master_email"])
                return MailboxAccount(
                    email=email,
                    account_id=mother["master_email"],
                    extra={
                        "provider_account": {
                            "provider_type": "mailbox",
                            "provider_name": "gmail_oauth_fission",
                            "login_identifier": mother["master_email"],
                            "display_name": mother["master_email"],
                            "metadata": {"master_email": mother["master_email"]},
                        },
                        "provider_resource": {
                            "provider_type": "mailbox",
                            "provider_name": "gmail_oauth_fission",
                            "resource_type": "mailbox",
                            "resource_identifier": mother["master_email"],
                            "handle": email,
                            "display_name": email,
                            "metadata": {
                                "email": email,
                                "master_email": mother["master_email"],
                                "is_gmail_alias": email != mother["master_email"],
                                "fission_mode": self.fission_mode if self.fission_enable else "off",
                                "used_count_at_claim": used_count,
                                "gmail_oauth_claimed": True,
                                "gmail_oauth_claimed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            },
                        },
                    },
                )
        detail = f": {' | '.join(skipped[-3:])}" if skipped else ""
        raise RuntimeError(f"Gmail OAuth 母号池暂未找到可用邮箱{detail}")

    def _mother_for_account(self, account: MailboxAccount) -> dict[str, Any]:
        master = _normalize_email(account.account_id)
        if not master:
            metadata = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {})
            master = _normalize_email(metadata.get("master_email"))
        for mother in self._mothers:
            if mother["master_email"] == master:
                return mother
        if len(self._mothers) == 1:
            return self._mothers[0]
        raise RuntimeError(f"Gmail OAuth 未找到账号对应母号: {master}")

    def _build_service(self, mother: dict[str, Any]):
        master = mother["master_email"]
        if master in self._services:
            return self._services[master]

        import httplib2
        import socks
        import google_auth_httplib2
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_info = _loads_json(mother["token_json"], f"{master} token_json")
        creds = Credentials.from_authorized_user_info(token_info, [GMAIL_MODIFY_SCOPE])
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            mother["token_json"] = creds.to_json()
        if self.proxy:
            parsed = urllib.parse.urlparse(self.proxy)
            proxy_type = socks.PROXY_TYPE_HTTP if parsed.scheme.lower().startswith("http") else socks.PROXY_TYPE_SOCKS5
            proxy_info = httplib2.ProxyInfo(
                proxy_type=proxy_type,
                proxy_host=parsed.hostname,
                proxy_port=parsed.port,
                proxy_user=parsed.username,
                proxy_pass=parsed.password,
            )
            http = httplib2.Http(proxy_info=proxy_info, timeout=60)
            authorized_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
            self._services[master] = build("gmail", "v1", http=authorized_http, static_discovery=False)
            return self._services[master]
        self._services[master] = build("gmail", "v1", credentials=creds, static_discovery=False)
        return self._services[master]

    @staticmethod
    def _message_id(mail: dict) -> str:
        return str(mail.get("id") or "")

    @staticmethod
    def _body_from_payload(payload: dict) -> str:
        def find_body(parts: list[dict], mime_type: str) -> str:
            for part in parts:
                if part.get("mimeType") == mime_type:
                    return str(part.get("body", {}).get("data") or "")
                nested = part.get("parts")
                if nested:
                    found = find_body(nested, mime_type)
                    if found:
                        return found
            return ""

        parts = payload.get("parts", [])
        raw = ""
        if parts:
            raw = find_body(parts, "text/plain") or find_body(parts, "text/html")
        else:
            raw = str(payload.get("body", {}).get("data") or "")
        if not raw:
            return ""
        return base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")

    def _fetch_messages(self, account: MailboxAccount, *, mark_read: bool) -> list[dict]:
        mother = self._mother_for_account(account)
        service = self._build_service(mother)
        query = "label:unread newer_than:1d in:anywhere"
        results = service.users().messages().list(userId="me", q=query, includeSpamTrash=True).execute(num_retries=2)
        messages = results.get("messages", []) or []
        output: list[dict] = []
        target = _normalize_email(account.email)
        for item in messages:
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            headers = msg.get("payload", {}).get("headers", []) or []
            header_map = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}
            recipients = " ".join((header_map.get("to", ""), header_map.get("delivered-to", ""))).lower()
            if target and target not in recipients:
                continue
            body = self._body_from_payload(msg.get("payload", {}) or {})
            output.append({
                "id": message_id,
                "subject": header_map.get("subject", ""),
                "body": body,
                "snippet": msg.get("snippet", ""),
            })
            if mark_read:
                service.users().messages().batchModify(
                    userId="me",
                    body={"ids": [message_id], "removeLabelIds": ["UNREAD"]},
                ).execute()
        return output

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {self._message_id(mail) for mail in self._fetch_messages(account, mark_read=False)}
        except Exception:
            return set()

    @staticmethod
    def _message_text(mail: dict) -> str:
        return " ".join(str(mail.get(key, "") or "") for key in ("subject", "body", "snippet"))

    @staticmethod
    def _clean_message_text(text: str) -> str:
        value = str(text or "")
        value = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", value)
        value = re.sub(r"(?is)<[^>]+>", " ", value)
        value = html.unescape(value)
        value = re.sub(r"https?://\S+", " ", value)
        value = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", value)
        value = re.sub(r"#[0-9a-fA-F]{6}\b", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _extract_code_from_text(cls, text: str, code_pattern: str | None = None) -> str:
        clean_text = cls._clean_message_text(text)
        candidates = (
            re.search(r"verification code to continue:\s*(\d{6})", clean_text, re.I)
            or re.search(r"temporary verification code to continue:\s*(\d{6})", clean_text, re.I)
            or re.search(r"enter this code:\s*(\d{6})", clean_text, re.I)
            or re.search(r"your chatgpt code is\s*(\d{6})", clean_text, re.I)
        )
        if candidates:
            return candidates.group(1)
        pattern = re.compile(code_pattern) if code_pattern else re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        match = pattern.search(clean_text)
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                for mail in self._fetch_messages(account, mark_read=True):
                    mid = self._message_id(mail)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    text = self._message_text(mail)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._extract_code_from_text(text, code_pattern=code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待 Gmail OAuth 验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                for mail in self._fetch_messages(account, mark_read=True):
                    mid = self._message_id(mail)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    link = _extract_verification_link(self._message_text(mail), keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待 Gmail OAuth 验证链接超时 ({timeout}s)")
