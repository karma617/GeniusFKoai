"""ChatGPT 邮箱换绑独立配置与账号视图。

- 配置使用独立键 chatgpt_rebind_mail_config 持久化（core.config_store），
  不复用设置-邮箱服务（provider_settings mailbox）。
- Cloudflare MX / Email Routing 自动配置为未来扩展预留 status/capabilities
  字段，当前未实现，不伪装已完成。
"""
from __future__ import annotations

import json
import re
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
TEXT_FIELDS = ("api_url", "cloudflare_account_id", "forward_to")

# Cloudflare 自动配置占位：明确标注未实现。
PROVISION_STATUS = "not_implemented"
PROVISION_CAPABILITIES = {
    "cloudflare_mx_provision": {"implemented": False},
    "cloudflare_email_routing_provision": {"implemented": False},
}


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
        "capabilities": PROVISION_CAPABILITIES,
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
