"""ChatGPT 邮箱换绑独立配置与账号视图。

- 配置使用独立键 chatgpt_rebind_mail_config 持久化（core.config_store），
  不复用设置-邮箱服务（provider_settings mailbox）。
- Cloudflare MX / Email Routing 自动配置由 application.cloudflare_email_routing
  实现；GET 配置中的 provision.status/capabilities 对外声明 ready。
"""
from __future__ import annotations

import json
import random
import re
import string
import threading
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.account_graph import load_account_graphs
from core.config_store import config_store
from core.datetime_utils import serialize_datetime
from core.db import AccountModel, engine

MAIL_CONFIG_KEY = "chatgpt_rebind_mail_config"

# secret 字段 GET 时遮蔽；PUT 收到遮蔽值/空值时保留原值。
SECRET_FIELDS = ("api_token", "cloudflare_api_token")
SECRET_MASK = "******"
TEXT_FIELDS = ("api_url", "cloudflare_account_id", "cloudflare_worker_name", "forward_to")

# Cloudflare 自动配置能力声明（实现见 application.cloudflare_email_routing）。
PROVISION_STATUS = "ready"
PROVISION_CAPABILITIES = {
    "cloudflare_mx_provision": {"implemented": True},
    "cloudflare_email_routing_provision": {"implemented": True},
}

# 换绑子域名邮箱配额：每个主域名最多分配的子域名数量；一个子域名绑定一个账号
# （账号邮箱 <-> 子域名 1:1），由任务执行层经 allocate_rebind_subdomain 分配。
SUBDOMAIN_LIMIT_PER_DOMAIN = 10


def normalize_domains(raw: Any) -> list[str]:
    """多行/逗号/分号/空白分隔输入归一为去重小写域名列表（去 @ 前缀）。"""
    if isinstance(raw, str):
        items = re.split(r"[\s,;，；]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item or "") for item in raw]
    else:
        items = []
    domains: list[str] = []
    seen: set[str] = set()
    for item in items:
        domain = str(item or "").strip().lstrip("@").strip().lower()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return domains


def mask_secret(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return SECRET_MASK
    return SECRET_MASK + text[-4:]


def _is_masked_value(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.startswith(SECRET_MASK)


def load_rebind_mail_config() -> dict[str, Any]:
    """读取换绑专用配置（协议执行器使用原始值，不遮蔽）。"""
    try:
        data = json.loads(config_store.get(MAIL_CONFIG_KEY, "") or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    config: dict[str, Any] = {}
    for field in (*TEXT_FIELDS, *SECRET_FIELDS):
        config[field] = str(data.get(field) or "").strip()
    config["domains"] = normalize_domains(data.get("domains"))
    config["subdomain_allocations"] = normalize_subdomain_allocations(data.get("subdomain_allocations"))
    return config


def get_mail_config() -> dict[str, Any]:
    """GET 用：遮蔽 secret，附带 Cloudflare 自动配置占位 status/capabilities。

    secret 只以遮蔽形式输出：保留原键（api_token/cloudflare_api_token，
    值为 ****** 形式）便于整体回写安全，另提供前端使用的
    api_token_masked / cloudflare_api_token_masked 别名。
    """
    config = load_rebind_mail_config()
    masked = dict(config)
    for field in SECRET_FIELDS:
        masked[field] = mask_secret(config.get(field, ""))
        masked[field + "_masked"] = masked[field]
    masked["has_api_token"] = bool(str(config.get("api_token") or "").strip())
    masked["has_cloudflare_api_token"] = bool(str(config.get("cloudflare_api_token") or "").strip())
    masked["provision"] = {
        "status": PROVISION_STATUS,
        "capabilities": {
            **PROVISION_CAPABILITIES,
            "subdomain_limit_per_domain": SUBDOMAIN_LIMIT_PER_DOMAIN,
        },
    }
    return masked


def update_mail_config(data: dict[str, Any]) -> dict[str, Any]:
    """PUT 用：普通字段覆盖；secret 为空或遮蔽值时保留原值。"""
    if not isinstance(data, dict):
        data = {}
    next_config = dict(load_rebind_mail_config())
    for field in TEXT_FIELDS:
        if field in data:
            next_config[field] = str(data.get(field) or "").strip()
    for field in SECRET_FIELDS:
        if field not in data:
            continue
        incoming = str(data.get(field) or "").strip()
        if not incoming or _is_masked_value(incoming):
            continue
        next_config[field] = incoming
    if "domains" in data:
        next_config["domains"] = normalize_domains(data.get("domains"))
    config_store.set(MAIL_CONFIG_KEY, json.dumps(next_config, ensure_ascii=False))
    return get_mail_config()


# ---------------------------------------------------------------------------
# 换绑子域名分配：新邮箱形如 {随机本地部分}@{子域名}.{主域名}，每个主域名最多
# SUBDOMAIN_LIMIT_PER_DOMAIN 个子域名，一个子域名绑定一个账号（1:1）。分配记录
# 持久化在 mail config KV 的 subdomain_allocations 字段：
# {主域名: [{subdomain, account_email, created_at}]}。
# ---------------------------------------------------------------------------

_SUBDOMAIN_ALLOCATION_LOCK = threading.Lock()


def normalize_subdomain_allocations(raw: Any) -> dict[str, list[dict[str, str]]]:
    """归一 subdomain_allocations 结构：非法条目丢弃，同主域内子域名去重。"""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[dict[str, str]]] = {}
    for raw_domain, raw_entries in raw.items():
        domain = str(raw_domain or "").strip().lstrip("@").lower()
        if not domain or not isinstance(raw_entries, (list, tuple)):
            continue
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            subdomain = str(raw_entry.get("subdomain") or "").strip().lstrip("@").lower()
            if not subdomain or subdomain in seen:
                continue
            seen.add(subdomain)
            entries.append(
                {
                    "subdomain": subdomain,
                    "account_email": str(raw_entry.get("account_email") or "").strip().lower(),
                    "created_at": str(raw_entry.get("created_at") or ""),
                }
            )
        result[domain] = entries
    return result


def load_subdomain_allocations() -> dict[str, list[dict[str, str]]]:
    """读取当前子域名分配（归一后）。"""
    return load_rebind_mail_config().get("subdomain_allocations") or {}


def save_subdomain_allocations(
    allocations: dict[str, list[dict[str, str]]]
) -> dict[str, list[dict[str, str]]]:
    """整体覆盖式持久化子域名分配；仅在 _SUBDOMAIN_ALLOCATION_LOCK 内调用。

    直接改写 KV 原始 JSON 的 subdomain_allocations 键，不经过
    update_mail_config 的字段白名单，保证其余配置原样保留。
    """
    normalized = normalize_subdomain_allocations(allocations)
    try:
        data = json.loads(config_store.get(MAIL_CONFIG_KEY, "") or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["subdomain_allocations"] = normalized
    config_store.set(MAIL_CONFIG_KEY, json.dumps(data, ensure_ascii=False))
    return normalized


def _generate_rebind_subdomain(used: set[str]) -> str:
    """生成 6-8 位小写字母数字子域名，与 used 中已分配的不冲突。"""
    for _ in range(64):
        length = random.randint(6, 8)
        candidate = "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
        if candidate not in used:
            return candidate
    raise RuntimeError("生成唯一子域名失败：冲突次数过多")


def _allocation_quota_error(mail_config: dict, allocations: dict) -> str:
    """全部主域名配额用尽时的失败信息，逐域名给出占用/上限。"""
    from platforms.chatgpt.email_rebind import _ordered_mail_config_domains

    domains = _ordered_mail_config_domains(mail_config)
    if not domains:
        return "没有可用的主域名（请先在换绑配置填写 domains）"
    details = "；".join(
        "主域名 " + domain + " 子域名配额已用完("
        + str(len(allocations.get(domain) or [])) + "/" + str(SUBDOMAIN_LIMIT_PER_DOMAIN) + ")"
        for domain in domains
    )
    return "所有主域名子域名配额已用尽：" + details


def allocate_rebind_subdomain(account_email: str) -> tuple[str, str]:
    """为账号分配（或复用）换绑子域名，返回 (子域名, 主域名)。

    - 同一 account_email 已有分配：直接复用（任务重试幂等）；
    - 否则选择仍有余量的主域名（配额语义见
      platforms.chatgpt.email_rebind._select_cloud_mail_domain），生成 6-8 位
      小写字母数字子域名（全局唯一）并先持久化再返回；
    - 全部主域名配额用尽：RuntimeError，消息含
      「主域名 X 子域名配额已用完(N/10)」。
    并发安全：读-改-写全程持有 _SUBDOMAIN_ALLOCATION_LOCK。
    """
    from platforms.chatgpt.email_rebind import _select_cloud_mail_domain

    email_key = str(account_email or "").strip().lower()
    if not email_key:
        raise RuntimeError("账号缺少邮箱，无法分配子域名")
    with _SUBDOMAIN_ALLOCATION_LOCK:
        mail_config = load_rebind_mail_config()
        allocations = mail_config.get("subdomain_allocations") or {}
        for main_domain, entries in allocations.items():
            for entry in entries:
                if str(entry.get("account_email") or "") == email_key:
                    return str(entry.get("subdomain") or ""), str(main_domain)
        main_domain = _select_cloud_mail_domain(
            mail_config,
            subdomain_allocations=allocations,
            subdomain_limit=SUBDOMAIN_LIMIT_PER_DOMAIN,
        )
        if not main_domain:
            raise RuntimeError(_allocation_quota_error(mail_config, allocations))
        used = {
            str(entry.get("subdomain") or "")
            for entries in allocations.values()
            for entry in entries
        }
        subdomain = _generate_rebind_subdomain(used)
        allocations.setdefault(main_domain, []).append(
            {
                "subdomain": subdomain,
                "account_email": email_key,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        save_subdomain_allocations(allocations)
        return subdomain, main_domain


def list_registered_accounts(page: int = 1, page_size: int = 20, email: str = "") -> dict[str, Any]:
    """platform=chatgpt 且 lifecycle_status 精确为 registered 的分页账号。

    email 非空时按大小写不敏感子串过滤（前端搜索框），过滤发生在分页前。
    """
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or 20), 1), 200)
    keyword = str(email or "").strip().lower()
    with Session(engine) as session:
        models = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.id.desc())
        ).all()
        model_map = {int(model.id or 0): model for model in models if model.id}
        graphs = load_account_graphs(session, list(model_map.keys()))
    registered_ids = [
        account_id
        for account_id in model_map
        if str((graphs.get(account_id, {}) or {}).get("lifecycle_status") or "") == "registered"
        and (not keyword or keyword in str(model_map[account_id].email or "").lower())
    ]
    total = len(registered_ids)
    start = (page - 1) * page_size
    items = []
    for account_id in registered_ids[start:start + page_size]:
        graph = graphs.get(account_id, {}) or {}
        model = model_map[account_id]
        email_value = str(model.email or "")
        display_status = str(graph.get("display_status") or "")
        items.append(
            {
                "id": account_id,
                "email": email_value,
                "current_email": email_value,
                "status": display_status or str(graph.get("lifecycle_status") or "registered"),
                "registered_at": serialize_datetime(model.created_at),
                "created_at": serialize_datetime(model.created_at),
                "lifecycle_status": str(graph.get("lifecycle_status") or ""),
                "display_status": display_status,
                "updated_at": serialize_datetime(model.updated_at),
            }
        )
    return {"total": total, "page": page, "page_size": page_size, "items": items}
