#!/usr/bin/env python3
"""Wire SMSBower mail mailbox into factory + provider definitions."""
from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        if new.strip() in text:
            print(f"skip already applied: {label}")
            return
        raise SystemExit(f"missing block for {label} in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"patched {label} -> {path}")


def main() -> None:
    base = Path("core/base_mailbox.py")
    replace_once(
        base,
        'DEFAULT_TEMPMAIL_WEB_BASE_URL = "https://web2.temp-mail.org"\n',
        (
            'DEFAULT_TEMPMAIL_WEB_BASE_URL = "https://web2.temp-mail.org"\n'
            'DEFAULT_SMSBOWER_MAIL_API_URL = "https://smsbower.page"\n'
        ),
        "default url constant",
    )
    replace_once(
        base,
        """def _create_testmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TestmailMailbox(
        api_url=extra.get("testmail_api_url", ""),
        api_key=extra.get("testmail_api_key", ""),
        namespace=extra.get("testmail_namespace", ""),
        tag_prefix=extra.get("testmail_tag_prefix", ""),
        proxy=proxy,
    )
""",
        """def _create_testmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TestmailMailbox(
        api_url=extra.get("testmail_api_url", ""),
        api_key=extra.get("testmail_api_key", ""),
        namespace=extra.get("testmail_namespace", ""),
        tag_prefix=extra.get("testmail_tag_prefix", ""),
        proxy=proxy,
    )


def _create_smsbower_mail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    from core.smsbower_mail_mailbox import SmsBowerMailMailbox

    mailbox_proxy = str(extra.get("smsbower_mail_proxy") or extra.get("mailbox_proxy") or "").strip()
    return SmsBowerMailMailbox(
        api_url=extra.get("smsbower_mail_api_url", ""),
        api_key=extra.get("smsbower_mail_api_key", ""),
        service=extra.get("smsbower_mail_service", ""),
        domain=extra.get("smsbower_mail_domain", ""),
        alias=extra.get("smsbower_mail_alias", "0"),
        max_price=extra.get("smsbower_mail_max_price", ""),
        ref=extra.get("smsbower_mail_ref", ""),
        poll_interval=extra.get("smsbower_mail_poll_interval", "3"),
        proxy=mailbox_proxy or proxy,
    )
""",
        "factory creator",
    )
    replace_once(
        base,
        '    "testmail_api": _create_testmail,\n',
        '    "testmail_api": _create_testmail,\n    "smsbower_mail_api": _create_smsbower_mail,\n',
        "registry primary key",
    )
    replace_once(
        base,
        '    "testmail": _create_testmail,\n',
        '    "testmail": _create_testmail,\n    "smsbower_mail": _create_smsbower_mail,\n',
        "registry compat key",
    )

    defs = Path("infrastructure/provider_definitions_repository.py")
    replace_once(
        defs,
        """    {
        "provider_type": "mailbox",
        "provider_key": "testmail_api",
        "label": "Testmail（namespace 邮箱）",
        "description": "Testmail.app 第三方服务，通过 API Key 和 Namespace 自动拼接邮箱",
        "driver_type": "testmail_api",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "testmail_api_url", "label": "API 地址（可选）", "placeholder": "https://api.testmail.app", "category": "connection"},
            {"key": "testmail_api_key", "label": "API Key", "secret": True, "category": "auth"},
            {"key": "testmail_namespace", "label": "Namespace", "category": "identity"},
            {"key": "testmail_tag_prefix", "label": "Tag 前缀（可选）", "placeholder": "", "category": "identity"},
        ],
    },
""",
        """    {
        "provider_type": "mailbox",
        "provider_key": "testmail_api",
        "label": "Testmail（namespace 邮箱）",
        "description": "Testmail.app 第三方服务，通过 API Key 和 Namespace 自动拼接邮箱",
        "driver_type": "testmail_api",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "testmail_api_url", "label": "API 地址（可选）", "placeholder": "https://api.testmail.app", "category": "connection"},
            {"key": "testmail_api_key", "label": "API Key", "secret": True, "category": "auth"},
            {"key": "testmail_namespace", "label": "Namespace", "category": "identity"},
            {"key": "testmail_tag_prefix", "label": "Tag 前缀（可选）", "placeholder": "", "category": "identity"},
        ],
    },
    {
        "provider_type": "mailbox",
        "provider_key": "smsbower_mail_api",
        "label": "SMSBROWER谷歌邮箱",
        "description": "SMSBower 第三方谷歌邮箱接码；配置 API Key 与服务码后，注册任务自动 getActivation 取号并 getCode 收码",
        "driver_type": "smsbower_mail_api",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {
                "key": "smsbower_mail_api_url",
                "label": "API 地址（可选）",
                "placeholder": "https://smsbower.page",
                "default_value": "https://smsbower.page",
                "category": "connection",
            },
            {
                "key": "smsbower_mail_api_key",
                "label": "API Key",
                "secret": True,
                "category": "auth",
                "hint": "SMSBower 后台 API Key；测试成功会查询库存，不会锁定邮箱。",
            },
            {
                "key": "smsbower_mail_service",
                "label": "服务码 service",
                "placeholder": "dr",
                "default_value": "dr",
                "category": "identity",
                "hint": "OpenAI (ChatGPT) 默认 dr；可通过 getMailServicesList 查看其它服务码。",
            },
            {
                "key": "smsbower_mail_domain",
                "label": "域名 domain",
                "type": "select",
                "default_value": "gmail.com",
                "category": "identity",
                "options": [
                    {"value": "gmail.com", "label": "gmail.com"},
                    {"value": "mailnestpro.com", "label": "mailnestpro.com"},
                    {"value": "hihinail.com", "label": "hihinail.com"},
                    {"value": "flytempbox.com", "label": "flytempbox.com"},
                    {"value": "mailburstx.com", "label": "mailburstx.com"},
                ],
            },
            {
                "key": "smsbower_mail_alias",
                "label": "使用别名 alias",
                "type": "select",
                "default_value": "0",
                "category": "identity",
                "options": [
                    {"value": "0", "label": "否 (0)"},
                    {"value": "1", "label": "是 (1)"},
                ],
            },
            {
                "key": "smsbower_mail_max_price",
                "label": "最高价格 maxPrice（可选）",
                "placeholder": "0.05",
                "category": "connection",
            },
            {
                "key": "smsbower_mail_ref",
                "label": "推荐 ID ref（可选）",
                "placeholder": "",
                "category": "connection",
            },
            {
                "key": "smsbower_mail_poll_interval",
                "label": "收码轮询间隔秒",
                "placeholder": "3",
                "default_value": "3",
                "category": "connection",
            },
            {
                "key": "smsbower_mail_proxy",
                "label": "邮箱代理（可选）",
                "placeholder": "http://127.0.0.1:7890",
                "category": "connection",
            },
        ],
    },
""",
        "provider definition",
    )


if __name__ == "__main__":
    main()
