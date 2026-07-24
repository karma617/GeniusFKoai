"""Proxy helpers for outbound HTTP requests.

Supports 1024proxy lines in the form:
    host:port:username:password
and converts them to httpx-compatible proxy URLs.
"""
from __future__ import annotations

import os
import hashlib
import re
import random
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote

import httpx
from config import PROXY_POOL, PROXY_ENABLED, DYNAMIC_PROXY_API

_TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled", "y"}
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled", "n", ""}


def _is_port(value: str) -> bool:
    try:
        port = int((value or "").strip())
    except ValueError:
        return False
    return 1 <= port <= 65535


def _split_host_port(value: str) -> tuple[str, int]:
    host, sep, port_text = (value or "").strip().rpartition(":")
    if not sep or not host:
        raise ValueError("代理 host:port 格式不正确")
    if not _is_port(port_text):
        raise ValueError("代理 port 必须是 1-65535 的数字")
    return host.strip(), int(port_text)


def _split_user_password(value: str) -> tuple[str, str]:
    username, sep, password = (value or "").strip().partition(":")
    if not sep or not username or not password:
        raise ValueError("代理 username/password 格式不正确")
    return username.strip(), password.strip()


@dataclass(frozen=True)
class ProxyEntry:
    host: str
    port: int
    username: str
    password: str
    scheme: str = "http"

    @classmethod
    def parse(cls, raw: str) -> "ProxyEntry":
        value = (raw or "").strip()
        if not value:
            raise ValueError("代理配置为空")

        # Already a URL.  This path is mainly for env overrides such as
        # PAYPAL_PROXY_URL=http://user:pass@host:port.
        if "://" in value:
            from urllib.parse import urlsplit, unquote

            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
                raise ValueError(f"不支持的代理协议：{parsed.scheme}")
            netloc = parsed.netloc.strip()
            if "@" not in netloc and netloc.count(":") >= 3:
                host, port_text, username, password = [part.strip() for part in netloc.split(":", 3)]
                if not host:
                    raise ValueError("代理 host 不能为空")
                if not _is_port(port_text):
                    raise ValueError("代理 port 必须是 1-65535 的数字")
                if not username or not password:
                    raise ValueError("代理 username/password 不能为空")
                return cls(
                    host=host,
                    port=int(port_text),
                    username=unquote(username),
                    password=unquote(password),
                    scheme=parsed.scheme,
                )
            try:
                parsed_port = parsed.port
            except ValueError as exc:
                raise ValueError("代理 URL port 格式不正确") from exc
            if not parsed.hostname or not parsed_port:
                raise ValueError("代理 URL 必须包含 host 和 port")
            return cls(
                host=parsed.hostname,
                port=int(parsed_port),
                username=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                scheme=parsed.scheme,
            )

        if "##" in value:
            parts = [part.strip() for part in value.split("##")]
            if len(parts) != 3:
                raise ValueError("代理格式应为 host:port##username##password")
            host, port = _split_host_port(parts[0])
            username, password = parts[1], parts[2]
            if not username or not password:
                raise ValueError("代理 username/password 不能为空")
            return cls(host=host, port=port, username=username, password=password)

        if "@" in value:
            left, right = [part.strip() for part in value.split("@", 1)]
            if not left or not right:
                raise ValueError("代理 @ 格式不正确")
            if _is_port(right.rpartition(":")[2]):
                username, password = _split_user_password(left)
                host, port = _split_host_port(right)
            else:
                host, port = _split_host_port(left)
                username, password = _split_user_password(right)
            return cls(host=host, port=port, username=username, password=password)

        if value.count(":") == 1:
            host, port = _split_host_port(value)
            return cls(host=host, port=port, username="", password="")

        parts = value.split(":", 3)
        if len(parts) != 4:
            raise ValueError("代理格式应为 host:port:username:password")
        if _is_port(parts[1]):
            host, port_text, username, password = [part.strip() for part in parts]
            port = int(port_text)
        elif _is_port(parts[3]):
            username, password, host, port_text = [part.strip() for part in parts]
            port = int(port_text)
        else:
            raise ValueError("代理格式应为 host:port:username:password 或 username:password:host:port")
        if not host:
            raise ValueError("代理 host 不能为空")
        if not username or not password:
            raise ValueError("代理 username/password 不能为空")
        return cls(host=host, port=port, username=username, password=password)

    @property
    def url(self) -> str:
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        auth = f"{user}:{password}@" if self.username or self.password else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def masked(self) -> str:
        # Keep credentials hidden, but expose a short fingerprint so sticky
        # sessions that share host:port are distinguishable in logs.
        if self.username or self.password:
            fp = hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:6]
            return f"{self.scheme}://***:***@{self.host}:{self.port}#{fp}"
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool
    entry: ProxyEntry | None = None
    entries: tuple[ProxyEntry, ...] = ()
    current_index: int = 0

    @property
    def url(self) -> str | None:
        return self.entry.url if self.enabled and self.entry else None

    @property
    def label(self) -> str:
        if not self.enabled or not self.entry:
            return "代理关闭"
        return self.entry.masked


def parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def _split_pool(raw: str) -> list[str]:
    lines: list[str] = []
    for item in (raw or "").replace(",", "\n").splitlines():
        item = item.strip()
        if item and not item.startswith("#"):
            lines.append(item)
    return lines


def load_proxy_pool() -> list[str]:
    """Load proxy lines from env first, then config.PROXY_POOL."""
    env_url = os.getenv("PAYPAL_PROXY_URL", "").strip()
    if env_url:
        return [env_url]

    env_pool = _split_pool(os.getenv("PAYPAL_PROXY_POOL", ""))
    if env_pool:
        return env_pool

    return [line.strip() for line in PROXY_POOL if str(line).strip()]


def parse_proxy_pool_text(raw: str) -> list[str]:
    return _split_pool(raw)


def choose_proxy_entry(pool: Iterable[str] | None = None, index: int | None = None) -> ProxyEntry:
    entries = list(pool if pool is not None else load_proxy_pool())
    if not entries:
        raise ValueError("未配置代理池")
    if index is not None:
        if index < 0 or index >= len(entries):
            raise ValueError(f"代理序号超出范围：{index}，可用范围 0-{len(entries) - 1}")
        raw = entries[index]
    else:
        raw = random.choice(entries)
    return ProxyEntry.parse(raw)


def parse_proxy_entries(pool: Iterable[str] | None = None) -> tuple[ProxyEntry, ...]:
    entries = list(pool if pool is not None else load_proxy_pool())
    if not entries:
        raise ValueError("未配置代理池")
    return tuple(ProxyEntry.parse(raw) for raw in entries)


def build_proxy_config(
    enabled: bool | None = None,
    index: int | None = None,
    pool: Iterable[str] | None = None,
    exclude_urls: Iterable[str] | None = None,
) -> ProxyConfig:
    """Return a selected proxy config.

    enabled=None means use config/env default.  If disabled, no proxy is selected.
    exclude_urls skips already-used proxy URLs when picking a new entry.
    """
    if enabled is None:
        # Env can override the default at process startup without code changes.
        should_enable = parse_bool(os.getenv("PAYPAL_PROXY_ENABLED"), PROXY_ENABLED)
    else:
        # Explicit CLI/API choices must win so the proxy can be toggled dynamically.
        should_enable = bool(enabled)
    if not should_enable:
        return ProxyConfig(enabled=False)
    entries = parse_proxy_entries(pool)
    excluded = {str(u).strip() for u in (exclude_urls or ()) if str(u).strip()}
    if index is not None:
        if index < 0 or index >= len(entries):
            raise ValueError(f"代理序号超出范围：{index}，可用范围 0-{len(entries) - 1}")
        current_index = index
    else:
        candidates = [i for i, e in enumerate(entries) if e.url not in excluded]
        if not candidates:
            # All entries already used; return empty-ish disabled-like result via
            # raising? Keep last random for backward compat only when nothing excluded.
            if excluded:
                return ProxyConfig(enabled=False)
            current_index = random.randrange(len(entries))
        else:
            current_index = random.choice(candidates)
    return ProxyConfig(
        enabled=True,
        entry=entries[current_index],
        entries=entries,
        current_index=current_index,
    )


def dynamic_proxy_api_url(country: str = "BR", api_url: str | None = None) -> str:
    """Build dynamic proxy API URL for a country code.

    api_url can be a full template from the web UI. Supports:
    - `{country}` placeholder
    - existing `g=XX` rewrite
    - append `g=` when missing
    """
    code = (country or "BR").strip().upper() or "BR"
    template = (api_url or "").strip()
    if not template:
        template = os.getenv("PAYPAL_DYNAMIC_PROXY_API", "").strip() or DYNAMIC_PROXY_API
    if "{country}" in template:
        return template.format(country=code)
    # Allow fixed URLs that hardcode g=XX by rewriting g=
    if re.search(r"([?&])g=", template):
        return re.sub(r"([?&])g=[^&]*", rf"\1g={code}", template, count=1)
    sep = "&" if "?" in template else "?"
    return f"{template}{sep}g={code}"


def fetch_dynamic_proxy(
    country: str = "BR",
    *,
    api_url: str | None = None,
    timeout: float = 15.0,
) -> ProxyEntry:
    """Fetch one fresh proxy line from the dynamic API for this task."""
    url = dynamic_proxy_api_url(country, api_url=api_url)
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            text = (resp.text or "").strip()
    except Exception as exc:
        raise RuntimeError(f"动态代理 API 请求失败: {exc}") from exc

    lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
    if not lines:
        raise RuntimeError("动态代理 API 返回为空")
    # Prefer first non-error looking line
    raw = lines[0]
    lowered = raw.lower()
    if any(x in lowered for x in ("error", "fail", "invalid", "denied", "no ip")):
        raise RuntimeError(f"动态代理 API 返回异常: {raw[:200]}")
    try:
        return ProxyEntry.parse(raw)
    except Exception as exc:
        raise RuntimeError(f"动态代理格式无法解析: {raw[:120]}") from exc


def build_dynamic_proxy_config(
    country: str = "BR",
    *,
    api_url: str | None = None,
) -> ProxyConfig:
    entry = fetch_dynamic_proxy(country, api_url=api_url)
    return ProxyConfig(enabled=True, entry=entry, entries=(entry,), current_index=0)

