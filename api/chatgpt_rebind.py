from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter
from pydantic import BaseModel, Field

from application.chatgpt_rebind import (
    get_mail_config,
    list_registered_accounts,
    update_mail_config,
)

router = APIRouter(prefix="/chatgpt-rebind", tags=["chatgpt-rebind"])


class MailConfigUpdateRequest(BaseModel):
    """domains 支持多行文本或列表，后端归一为去重域名列表。

    PUT 为部分更新：仅 body.model_dump(exclude_unset=True) 中实际提交的
    字段参与覆盖；显式空值会清除普通字段（api_url/cloudflare_account_id/
    forward_to），secret 字段传空或遮蔽值（******开头）表示保留原值。
    """

    domains: Optional[Union[str, list[str]]] = Field(default="")
    api_url: Optional[str] = ""
    api_token: Optional[str] = ""
    cloudflare_api_token: Optional[str] = ""
    cloudflare_account_id: Optional[str] = ""
    forward_to: Optional[str] = ""


@router.get("/mail-config")
def read_mail_config():
    return get_mail_config()


@router.put("/mail-config")
def save_mail_config(body: MailConfigUpdateRequest):
    return update_mail_config(body.model_dump(exclude_unset=True))


@router.get("/accounts")
def list_accounts(page: int = 1, page_size: int = 20, email: str = ""):
    return list_registered_accounts(page=page, page_size=page_size, email=email)
