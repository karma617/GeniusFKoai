"""iCloud Hide My Email provider support.

This module ports the cookie-based HME management path from the local
``icloud-hme`` Go service into the current provider system.
"""
from __future__ import annotations

import email
import imaplib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from core.base_mailbox import BaseMailbox, MailboxAccount


CLIENT_BUILD_NUMBER = "2624Build22"
DEFAULT_BUILD_NUMBER = "2624Build13"
MAX_RETRIES = 3


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _strip_default_https_port(raw_url: str) -> str:
    parsed = urlparse(str(raw_url or "").strip())
    if parsed.hostname and parsed.port == 443:
        return urlunparse(parsed._replace(netloc=parsed.hostname))
    return str(raw_url or "").strip()


def normalize_icloud_host(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "icloud.com"
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or raw
    return "icloud.com.cn" if host.endswith(".icloud.com.cn") or host == "icloud.com.cn" else "icloud.com"


def _strip_cookie_outer_quotes(value: str) -> str:
    text = str(value)
    if len(text) >= 4 and text[:2] == '\\"' and text[-2:] == '\\"':
        return text[2:-2]
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _quote_cookie_value(value: str) -> str:
    text = str(value).strip()
    if len(text) >= 4 and text[:2] == '\\"' and text[-2:] == '\\"':
        text = text[2:-2]
    if text.startswith('"'):
        return text
    return f'"{text}"'


def _is_missing_icloud_user_cookie_error(value: Any) -> bool:
    return "missing x-apple-webauth-user cookie" in str(value or "").lower()


def parse_icloud_cookie_input(raw: str | dict[str, Any] | None) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if str(k or "").strip() and str(v or "").strip()}
    text = str(raw or "").strip()
    if not text:
        return {}
    first_sep = min((idx for idx in (text.find(":"), text.find("：")) if idx >= 0), default=-1)
    if first_sep >= 0:
        prefix = text[:first_sep].strip().lower()
        if prefix in {"cookie", "cookies"}:
            text = text[first_sep + 1:].strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return {
                    str(k): str(v)
                    for k, v in payload.items()
                    if str(k or "").strip() and str(v or "").strip()
                }
        except Exception:
            pass
    cookies: dict[str, str] = {}
    for part in text.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            cookies[name.strip()] = value.strip()
    return cookies


def _derive_icloud_email(apple_id: str = "", primary_email: str = "") -> str:
    for candidate in (primary_email, apple_id):
        value = str(candidate or "").strip()
        lower = value.lower()
        if lower.endswith(("@icloud.com", "@me.com", "@mac.com")):
            return value
    if "@" in apple_id:
        return apple_id.split("@", 1)[0] + "@icloud.com"
    return _first_non_empty(primary_email, apple_id)


@dataclass
class ICloudHMEAccount:
    id: str
    name: str = ""
    real_email: str = ""
    icloud_email: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    host: str = "icloud.com"
    proxy: str = ""
    app_password: str = ""
    status: str = "pending"
    alias_total: int = 0
    alias_active: int = 0
    last_validated: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=_utcnow_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ICloudHMEAccount":
        cookies = data.get("cookies")
        if not isinstance(cookies, dict):
            cookies = parse_icloud_cookie_input(data.get("cookie") or data.get("cookie_header") or "")
        return cls(
            id=str(data.get("id") or "acc_" + uuid.uuid4().hex[:8]),
            name=str(data.get("name") or ""),
            real_email=str(data.get("real_email") or data.get("realEmail") or ""),
            icloud_email=str(data.get("icloud_email") or data.get("icloudEmail") or ""),
            cookies={str(k): str(v) for k, v in dict(cookies or {}).items() if str(k or "").strip()},
            host=normalize_icloud_host(str(data.get("host") or "")),
            proxy=str(data.get("proxy") or ""),
            app_password=str(data.get("app_password") or data.get("appPassword") or ""),
            status=str(data.get("status") or "pending"),
            alias_total=int(data.get("alias_total") or data.get("aliasTotal") or 0),
            alias_active=int(data.get("alias_active") or data.get("aliasActive") or 0),
            last_validated=str(data.get("last_validated") or data.get("lastValidated") or ""),
            last_error=str(data.get("last_error") or data.get("lastError") or ""),
            created_at=str(data.get("created_at") or data.get("createdAt") or _utcnow_iso()),
        )

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("cookies", None)
        data["cookies_count"] = len(self.cookies)
        data["has_app_password"] = bool(self.app_password)
        return data

    def full_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_icloud_accounts_json(value: str | dict[str, Any] | list[Any] | None) -> list[ICloudHMEAccount]:
    if not value:
        return []
    payload: Any = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except Exception:
            return []
    if isinstance(payload, dict):
        raw_accounts = payload.get("accounts", [])
        if isinstance(raw_accounts, dict):
            items = list(raw_accounts.values())
        else:
            items = raw_accounts
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    accounts: list[ICloudHMEAccount] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            accounts.append(ICloudHMEAccount.from_dict(item))
    return accounts


def serialize_icloud_accounts_json(accounts: list[ICloudHMEAccount]) -> str:
    return json.dumps(
        {"accounts": [account.full_dict() for account in accounts], "updated_at": _utcnow_iso()},
        ensure_ascii=False,
        indent=2,
    )


class ICloudHMEClient:
    def __init__(self, cookies: dict[str, str], *, host: str = "icloud.com", proxy: str = ""):
        self.cookies = dict(cookies or {})
        self.host = normalize_icloud_host(host)
        self.proxy = str(proxy or "").strip()
        self.client_id = str(uuid.uuid4())
        self.dsid = ""
        self.service_url = ""
        self.mcc_gateway_url = ""
        self.account_info: dict[str, Any] = {}
        self._session_kind = "requests"
        try:
            from curl_cffi import requests as cffi_requests

            self._requests = cffi_requests
            self._session_kind = "curl_cffi"
            self.session = cffi_requests.Session(impersonate="chrome124", proxies=self._proxies())
        except Exception:
            import requests

            self._requests = requests
            self.session = requests.Session()

    def _proxies(self) -> dict[str, str] | None:
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    def _reset_session(self) -> None:
        if self._session_kind == "curl_cffi":
            self.session = self._requests.Session(impersonate="chrome124", proxies=self._proxies())
        else:
            self.session = self._requests.Session()

    def setup_url(self) -> str:
        return f"https://setup.{self.host}/setup/ws/1"

    def origin(self) -> str:
        return f"https://www.{self.host}"

    def _build_url(self, raw_url: str) -> str:
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        build = CLIENT_BUILD_NUMBER if "maildomainws" in (parsed.hostname or "") else DEFAULT_BUILD_NUMBER
        query["clientBuildNumber"] = [build]
        query["clientMasteringNumber"] = [build]
        query["clientId"] = [self.client_id]
        if self.dsid:
            query["dsid"] = [self.dsid]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _cookie_header(self) -> str:
        parts: list[str] = []
        for name, value in self.cookies.items():
            if not name:
                continue
            parts.append(f"{name}={_quote_cookie_value(str(value))}")
        return "; ".join(parts)

    def _request(self, method: str, url: str, body: Any = None, *, attempts: int = MAX_RETRIES) -> dict[str, Any]:
        full_url = self._build_url(url)
        host = urlparse(url).hostname or ""
        content_type = "text/plain" if "maildomainws" in host else "application/json"
        accept = "*/*" if "maildomainws" in host else "application/json, text/plain, */*"
        headers = {
            "Origin": self.origin(),
            "Referer": self.origin() + "/",
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Content-Type": content_type,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        }
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
        last_error = ""
        for attempt in range(max(1, attempts)):
            cookie_header = self._cookie_header()
            if cookie_header:
                headers["Cookie"] = cookie_header
            response = self.session.request(
                method.upper(),
                full_url,
                data=payload,
                headers=headers,
                timeout=30,
            )
            try:
                for name, value in response.cookies.items():
                    if name and value:
                        self.cookies[str(name)] = str(value)
            except Exception:
                pass
            text = str(getattr(response, "text", "") or "")
            status_code = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status_code < 300:
                try:
                    return json.loads(text or "{}")
                except Exception as exc:
                    raise RuntimeError(f"iCloud 响应不是 JSON: {exc}") from exc
            last_error = f"HTTP {status_code}: {text[:240]}"
            if status_code in {401, 403}:
                break
            if attempt < attempts - 1:
                time.sleep([1, 2.5, 5][min(attempt, 2)])
        raise RuntimeError(last_error or "iCloud 请求失败")

    def _validate_session_once(self) -> dict[str, Any]:
        data = self._request("POST", self.setup_url() + "/validate", attempts=MAX_RETRIES)
        service_url = str(((data.get("webservices") or {}).get("premiummailsettings") or {}).get("url") or "").rstrip("/")
        if not service_url:
            raise RuntimeError("iCloud 会话校验失败：Cookie 过期、未开通 iCloud+ 或网络异常")
        self.service_url = _strip_default_https_port(service_url).rstrip("/")
        mcc_gateway_url = str(((data.get("webservices") or {}).get("mccgateway") or {}).get("url") or "").strip()
        if mcc_gateway_url:
            if not mcc_gateway_url.startswith("http"):
                mcc_gateway_url = "https://" + mcc_gateway_url
            self.mcc_gateway_url = _strip_default_https_port(mcc_gateway_url).rstrip("/")
        ds_info = data.get("dsInfo") if isinstance(data.get("dsInfo"), dict) else {}
        self.dsid = str(ds_info.get("dsid") or "")
        self.account_info = {
            "dsid": self.dsid,
            "apple_id": _first_non_empty(ds_info.get("appleId"), ds_info.get("primaryEmail"), ds_info.get("appleIdEmail")),
            "primary_email": _first_non_empty(ds_info.get("primaryEmail"), ds_info.get("appleId")),
            "full_name": _first_non_empty(ds_info.get("fullName"), ds_info.get("name")),
        }
        self._reset_session()
        return dict(self.account_info)

    def validate_session(self) -> dict[str, Any]:
        original_host = self.host
        try:
            return self._validate_session_once()
        except RuntimeError as exc:
            if not _is_missing_icloud_user_cookie_error(exc) or "X-APPLE-WEBAUTH-USER" not in self.cookies:
                raise
            alternate_host = "icloud.com" if self.host == "icloud.com.cn" else "icloud.com.cn"
            self.host = alternate_host
            try:
                return self._validate_session_once()
            except Exception:
                self.host = original_host
                raise

    def _ensure_service(self) -> None:
        if not self.service_url:
            self.validate_session()

    def _ensure_mcc_gateway(self) -> None:
        if not self.mcc_gateway_url:
            self.validate_session()
        if not self.mcc_gateway_url:
            raise RuntimeError("iCloud Web Mail 服务不可用：未找到 mccgateway URL")

    def list_aliases(self) -> list[dict[str, Any]]:
        self._ensure_service()
        data = self._request("GET", self.service_url + "/v2/hme/list")
        return parse_alias_list(data)

    def generate(self) -> str:
        self._ensure_service()
        data = self._request("POST", self.service_url + "/v1/hme/generate", {"langCode": "en-us"}, attempts=2)
        if not data.get("success"):
            raise RuntimeError(str(((data.get("error") or {}).get("errorMessage")) or "生成失败"))
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        hme = result.get("hme")
        if isinstance(hme, dict):
            hme = _first_non_empty(hme.get("hme"), hme.get("email"))
        return str(hme or "")

    def reserve(self, hme: str, label: str = "") -> str:
        self._ensure_service()
        if not label:
            label = "Created " + datetime.now().strftime("%Y-%m-%d %H:%M")
        data = self._request(
            "POST",
            self.service_url + "/v1/hme/reserve",
            {"hme": hme, "label": label, "note": "Created by GeniusFKoai"},
            attempts=2,
        )
        if not data.get("success"):
            raise RuntimeError(str(((data.get("error") or {}).get("errorMessage")) or "保留失败"))
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        result_hme = result.get("hme")
        if isinstance(result_hme, dict):
            return str(result_hme.get("hme") or hme)
        return str(result_hme or hme)

    def create_alias(self, label: str = "", *, max_retries: int = 5) -> dict[str, Any]:
        last_error = ""
        for attempt in range(max(1, max_retries)):
            if attempt:
                self.service_url = ""
            try:
                hme = self.generate()
                email_address = self.reserve(hme, label)
                return {"email": email_address, "label": label, "created_at": _utcnow_iso()}
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1)
        raise RuntimeError(f"创建别名失败: {last_error}")

    def deactivate_alias(self, anonymous_id: str) -> bool:
        self._ensure_service()
        data = self._request("POST", self.service_url + "/v1/hme/deactivate", {"anonymousId": anonymous_id}, attempts=2)
        return bool(data.get("success"))

    def reactivate_alias(self, anonymous_id: str) -> bool:
        self._ensure_service()
        data = self._request("POST", self.service_url + "/v1/hme/reactivate", {"anonymousId": anonymous_id}, attempts=2)
        return bool(data.get("success"))

    def delete_alias(self, anonymous_id: str) -> bool:
        self._ensure_service()
        payload = {"anonymousId": anonymous_id}
        try:
            data = self._request("POST", self.service_url + "/v1/hme/delete", payload, attempts=2)
            if data.get("success"):
                return True
        except Exception:
            pass
        try:
            self._request("POST", self.service_url + "/v1/hme/deactivate", payload, attempts=2)
        except Exception:
            pass
        data = self._request("POST", self.service_url + "/v1/hme/delete", payload, attempts=2)
        return bool(data.get("success"))

    def web_search_messages(self, query: str = "", limit: int = 30) -> list[dict[str, str]]:
        self._ensure_mcc_gateway()
        safe_limit = max(1, int(limit or 30))
        payload: dict[str, Any] = {
            "responseType": "THREAD_DIGEST",
            "includeFolderStatus": False,
            "maxResults": safe_limit,
            "sessionHeaders": {
                "folder": "INBOX",
                "condstore": 1,
                "qresync": 1,
                "threadmode": 1,
            },
        }
        if query:
            payload["query"] = query
        else:
            payload["includeFolderStatus"] = True
            payload["sessionHeaders"].update({
                "modseq": None,
                "threadmodseq": None,
            })
        data = self._request("POST", self.mcc_gateway_url + "/mailws2/v1/thread/search", payload)
        return parse_web_mail_threads(data)

    def web_find_by_alias(self, alias: str, limit: int = 30) -> list[dict[str, str]]:
        target = str(alias or "").strip().lower()
        if not target:
            return self.web_search_messages("", limit)
        messages: list[dict[str, str]] = []
        for item in self.web_search_messages("", max(limit * 2, 50)):
            if target not in _message_combined_text(item).lower():
                continue
            item["_alias_search"] = "1"
            messages.append(item)
            if len(messages) >= limit:
                break
        return messages


def parse_alias_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    items = result.get("hmeEmails")
    if not isinstance(items, list):
        items = _find_first_dict_array(data)
    aliases: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metaData") if isinstance(item.get("metaData"), dict) else {}
        email_address = _first_non_empty(
            item.get("hme"),
            item.get("email"),
            item.get("alias"),
            item.get("address"),
            meta.get("hme"),
        ).lower()
        if "@" not in email_address:
            continue
        state = _first_non_empty(item.get("state"), item.get("status")).lower()
        active = state not in {"inactive", "deleted"}
        if "active" in item:
            active = bool(item.get("active")) and active
        if "isActive" in item:
            active = bool(item.get("isActive")) and active
        aliases.append({
            "email": email_address,
            "anonymous_id": _first_non_empty(item.get("anonymousId"), item.get("id")),
            "anonymousId": _first_non_empty(item.get("anonymousId"), item.get("id")),
            "label": _first_non_empty(item.get("label"), meta.get("label")),
            "forward_to_email": _first_non_empty(item.get("forwardToEmail"), item.get("forward_to_email"), meta.get("forwardToEmail")),
            "forwardToEmail": _first_non_empty(item.get("forwardToEmail"), item.get("forward_to_email"), meta.get("forwardToEmail")),
            "active": active,
            "created_at": _first_non_empty(item.get("createTimestamp"), item.get("createdAt")),
            "createdAt": _first_non_empty(item.get("createTimestamp"), item.get("createdAt")),
        })
    return sorted(aliases, key=lambda item: (not bool(item.get("active")), str(item.get("email") or "")))


def parse_web_mail_threads(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, dict):
        return []
    items = data.get("threadList")
    if not isinstance(items, list):
        items = _find_first_dict_array(data)
    messages: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        senders = item.get("senders") if isinstance(item.get("senders"), list) else []
        recipients = item.get("recipients") if isinstance(item.get("recipients"), list) else []
        messages.append({
            "id": _first_non_empty(item.get("threadId"), item.get("id"), item.get("messageId")),
            "from": _join_text_values(senders) or _first_non_empty(item.get("from"), item.get("sender")),
            "to": _join_text_values(recipients) or _join_text_values(item.get("to")),
            "subject": _first_non_empty(item.get("subject")),
            "body": _first_non_empty(item.get("preview"), item.get("snippet"), item.get("body")),
            "date": _first_non_empty(item.get("date"), item.get("timestamp")),
            "_source": "icloud_web",
        })
    return messages


def _join_text_values(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(item for item in (_join_text_values(part) for part in value) if item)
    if isinstance(value, dict):
        preferred = _first_non_empty(value.get("email"), value.get("address"), value.get("name"), value.get("displayName"))
        if preferred:
            return preferred
        return ", ".join(item for item in (_join_text_values(part) for part in value.values()) if item)
    return str(value)


def _message_combined_text(message: dict[str, Any]) -> str:
    return "\n".join(str(message.get(key) or "") for key in ("from", "to", "subject", "body", "preview"))


def _find_first_dict_array(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return value
        for item in value:
            found = _find_first_dict_array(item)
            if found:
                return found
    if isinstance(value, dict):
        for item in value.values():
            found = _find_first_dict_array(item)
            if found:
                return found
    return []


class ICloudHMEMailbox(BaseMailbox):
    def __init__(self, accounts_json: str = "", label_prefix: str = "", poll_interval: str = "", proxy: str | None = None):
        self.accounts = parse_icloud_accounts_json(accounts_json)
        self.label_prefix = str(label_prefix or "GeniusFKoai").strip() or "GeniusFKoai"
        try:
            self.poll_interval = max(1, int(float(poll_interval or 5)))
        except Exception:
            self.poll_interval = 5
        self.proxy = proxy or ""

    def _active_accounts(self) -> list[ICloudHMEAccount]:
        return [item for item in self.accounts if item.cookies and item.status != "deleted"]

    def _find_account(self, account_id: str) -> ICloudHMEAccount:
        for item in self.accounts:
            if item.id == account_id:
                return item
        raise RuntimeError(f"iCloud 账号不存在: {account_id}")

    def _client(self, account: ICloudHMEAccount) -> ICloudHMEClient:
        return ICloudHMEClient(account.cookies, host=account.host, proxy=account.proxy or self.proxy)

    @staticmethod
    def _ensure_forward_target_matches(account: ICloudHMEAccount, forward_to_email: str) -> None:
        forward_to = str(forward_to_email or "").strip().lower()
        if not forward_to:
            return
        receiver = str(account.icloud_email or "").strip().lower()
        if receiver and forward_to == receiver:
            return
        if not receiver:
            raise RuntimeError(
                f"iCloud HME 转发目标是 {forward_to}，但当前账号未配置 iCloud 收信邮箱。"
                f"请配置与转发目标一致的 iCloud 收信邮箱，或改用 {forward_to} 对应邮箱 provider 收信。"
            )
        raise RuntimeError(
            f"iCloud HME 转发目标是 {forward_to}，但当前配置收信邮箱是 {receiver}。"
            f"请在 Apple 隐私邮箱设置中把转发目标改为 {receiver}，或改用 {forward_to} 对应邮箱 provider 收信。"
        )

    def get_email(self) -> MailboxAccount:
        accounts = self._active_accounts()
        if not accounts:
            raise RuntimeError("iCloud 隐私邮箱未配置可用账号")
        target = sorted(accounts, key=lambda item: (item.alias_active, item.alias_total, item.created_at))[0]
        label = f"{self.label_prefix} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        client = self._client(target)
        aliases = client.list_aliases()
        existing_forward_to = _first_non_empty(*(item.get("forward_to_email") for item in aliases))
        self._ensure_forward_target_matches(target, existing_forward_to)
        result = client.create_alias(label)
        aliases = client.list_aliases()
        created = next((item for item in aliases if item.get("email") == result["email"]), {})
        self._ensure_forward_target_matches(target, str(created.get("forward_to_email") or ""))
        return MailboxAccount(
            email=result["email"],
            account_id=result["email"],
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "icloud_hme",
                    "resource_type": "mailbox",
                    "resource_identifier": result["email"],
                    "handle": result["email"],
                    "display_name": result["email"],
                    "metadata": {
                        "account_id": target.id,
                        "alias_email": result["email"],
                        "anonymous_id": created.get("anonymous_id") or created.get("anonymousId") or "",
                        "forward_to_email": created.get("forward_to_email") or created.get("forwardToEmail") or "",
                        "label": label,
                    },
                },
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "icloud_hme",
                    "login_identifier": target.icloud_email or target.real_email or target.name,
                    "display_name": target.name or target.real_email or target.id,
                    "metadata": {"account_id": target.id},
                },
            },
        )

    def _recent_imap_messages(self, account: ICloudHMEAccount, limit: int = 30) -> list[dict[str, str]]:
        user = account.icloud_email or account.real_email
        if not user or not account.app_password:
            return []
        conn = imaplib.IMAP4_SSL("imap.mail.me.com", 993)
        try:
            conn.login(user, account.app_password)
            conn.select("INBOX")
            _typ, data = conn.search(None, "ALL")
            ids = (data[0].decode() if data and data[0] else "").split()[-limit:]
            messages: list[dict[str, str]] = []
            for msg_id in reversed(ids):
                _typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                raw = b"".join(part[1] for part in msg_data if isinstance(part, tuple))
                parsed = email.message_from_bytes(raw)
                body = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                            break
                else:
                    body = parsed.get_payload(decode=True).decode(parsed.get_content_charset() or "utf-8", errors="ignore")
                messages.append({
                    "id": msg_id,
                    "from": parsed.get("From", ""),
                    "to": parsed.get("To", ""),
                    "subject": parsed.get("Subject", ""),
                    "body": body,
                })
            return messages
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _recent_messages(self, mailbox_account: MailboxAccount) -> list[dict[str, str]]:
        metadata = ((mailbox_account.extra or {}).get("provider_resource") or {}).get("metadata") or {}
        provider_account_id = str(metadata.get("account_id") or "")
        account = self._find_account(provider_account_id)
        alias = str(metadata.get("alias_email") or mailbox_account.email or "").strip()
        errors: list[str] = []
        imap_messages: list[dict[str, str]] = []
        try:
            imap_messages = self._recent_imap_messages(account)
        except Exception as exc:
            errors.append(f"IMAP: {exc}")
        try:
            web_messages = self._recent_web_messages(account, alias)
            if web_messages:
                return web_messages
        except Exception as exc:
            errors.append(f"Web Mail: {exc}")
            if not account.app_password and not imap_messages:
                raise RuntimeError("iCloud 隐私邮箱读取失败：" + "; ".join(errors)) from exc
        return imap_messages

    def _recent_web_messages(self, account: ICloudHMEAccount, alias: str, limit: int = 30) -> list[dict[str, str]]:
        client = self._client(account)
        messages = client.web_find_by_alias(alias, limit)
        if messages:
            return messages
        messages = client.web_search_messages("", limit)
        for item in messages:
            item["_hme_unscoped"] = "1"
        return messages

    def get_current_ids(self, account: MailboxAccount) -> set:
        return {str(item.get("id") or "") for item in self._recent_messages(account) if item.get("id")}

    def wait_for_code(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        before = {str(item) for item in (before_ids or set())}
        pattern = re.compile(code_pattern or r"(?<!\d)(\d{6})(?!\d)")
        deadline = time.time() + timeout
        target = str(account.email or "").lower()
        while time.time() < deadline:
            for message in self._recent_messages(account):
                msg_id = str(message.get("id") or "")
                if msg_id and msg_id in before:
                    continue
                combined = _message_combined_text(message)
                alias_matched = target in combined.lower() or message.get("_alias_search") == "1"
                unscoped_fresh = message.get("_hme_unscoped") == "1" and bool(before)
                if target and not alias_matched and not unscoped_fresh:
                    continue
                if keyword and keyword.lower() not in combined.lower():
                    continue
                match = pattern.search(combined)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待 iCloud 隐私邮箱验证码超时 ({timeout}s)")

    def delete_account(self, account: MailboxAccount, reason: str = "") -> bool:
        metadata = ((account.extra or {}).get("provider_resource") or {}).get("metadata") or {}
        provider_account_id = str(metadata.get("account_id") or "")
        anonymous_id = str(metadata.get("anonymous_id") or "")
        if not provider_account_id or not anonymous_id:
            return False
        client = self._client(self._find_account(provider_account_id))
        return client.delete_alias(anonymous_id)
