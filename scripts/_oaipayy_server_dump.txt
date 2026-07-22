#!/usr/bin/env python3
"""本地支付长链生成器 — FastAPI 后端。

3 步全在服务端跑，浏览器只负责显示表单和收 long_url：
  1. POST chatgpt.com/backend-api/payments/checkout  → cs_id
  2. POST api.stripe.com/v1/payment_pages/{cs}/init  → stripe_hosted_url
  3. host 重写 checkout.stripe.com → pay.openai.com → long_url

启动：
  pip install -r requirements.txt
  python server.py            # http://localhost:8000
  PORT=8765 python server.py  # 换端口
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Cloudflare 拦 plain requests 的 TLS 指纹 → 用 curl_cffi 模拟 Chrome
try:
    from curl_cffi.requests import Session as CffiSession
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False
    CffiSession = None

import requests


def new_session(proxy: Optional[str] = None):
    """Build a session with Chrome TLS fingerprint when available."""
    if HAS_CFFI and CffiSession is not None:
        s = CffiSession(impersonate="chrome136")
    else:
        s = requests.Session()
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        )
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


# OpenAI 的 Stripe live publishable key（公开、嵌在 checkout JS 里）
DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
DEFAULT_STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30

COUNTRIES = {
    "US": "USD", "GB": "GBP", "CA": "CAD", "AU": "AUD", "JP": "JPY",
    "SG": "SGD", "HK": "HKD", "TW": "TWD", "KR": "KRW", "ID": "IDR",
    "MY": "MYR", "TH": "THB", "VN": "VND", "PH": "PHP", "IN": "INR",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "IE": "EUR", "PT": "EUR", "BE": "EUR", "FI": "EUR", "AT": "EUR",
    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN",
    "CZ": "CZK", "MX": "MXN", "BR": "BRL", "NZ": "NZD",
}

LOCKED_COUNTRY_BY_TYPE = {"hosted": "US", "paypal": "JP", "gopay": "ID"}


# ───────── Schemas ─────────

class LongLinkRequest(BaseModel):
    accessToken: str
    link_type: str = "hosted"
    billing_country: str = "US"
    checkout_ui_mode: str = "hosted"
    payment_locale: str = "en"
    stripe_publishable_key: str = ""
    user_agent: str = ""
    proxy: str = ""


class LongLinkResponse(BaseModel):
    ok: bool = True
    cs_id: str
    processor_entity: str = ""
    billing_country: str
    currency: str
    payment_locale: str
    link_type: str
    payment_method_type: str = "hosted"
    stripe_hosted_url: str
    long_url: str


# ───────── App ─────────

app = FastAPI(title="本地支付长链生成器", version="1.0.0")


# 把所有没接住的异常转成 JSON 而不是 FastAPI 默认的 HTML "Internal Server Error"
@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    from fastapi.responses import JSONResponse
    import traceback
    tb = traceback.format_exc()
    print(f"[oaipc] UNCAUGHT: {tb}", flush=True)
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务端异常: {type(exc).__name__}: {str(exc)[:400]}"},
    )


def step1_openai(token: str, payload: dict, proxy: str = "") -> dict:
    sess = new_session(proxy=proxy)
    sess.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Authorization": f"Bearer {token}",
    })
    r = sess.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code != 200:
        raise HTTPException(
            status_code=r.status_code,
            detail=f"OpenAI 失败: {r.text[:500]}",
        )
    return r.json()


def step2_stripe(cs_id: str, pk: str, locale: str, user_agent: str, proxy: str = "") -> dict:
    sess = new_session(proxy=proxy)
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Shanghai",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": pk,
        "_stripe_version": DEFAULT_STRIPE_VERSION,
    }
    headers = {
        "Authorization": f"Bearer {pk}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": user_agent or DEFAULT_UA,
    }
    r = sess.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        data=body,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code != 200:
        raise HTTPException(
            status_code=r.status_code,
            detail=f"Stripe init 失败: {r.text[:500]}",
        )
    return r.json()


@app.get("/")
async def index():
    return FileResponse("index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "cffi": HAS_CFFI}


@app.post("/api/long-link")
async def long_link(req: LongLinkRequest):
    if not req.accessToken.strip():
        raise HTTPException(status_code=400, detail="accessToken 不能为空")

    # 链类型 → 锁定地区（paypal=JP, gopay=ID, hosted=US or 用户选）
    if req.link_type != "hosted":
        country = LOCKED_COUNTRY_BY_TYPE.get(req.link_type, "US")
    else:
        country = req.billing_country
    currency = COUNTRIES.get(country, "USD")

    # OpenAI payload
    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": req.checkout_ui_mode or "hosted",
    }
    if req.link_type == "gopay":
        payload["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }

    # Step 1
    print(f"[oaipc] Step 1 OpenAI country={country} currency={currency} proxy={'yes' if req.proxy else 'no'}", flush=True)
    openai_data = step1_openai(req.accessToken, payload, proxy=req.proxy)
    cs_id = openai_data.get("checkout_session_id") or openai_data.get("cs_id") or ""
    if not cs_id:
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI 响应没 checkout_session_id: {openai_data}",
        )
    print(f"[oaipc]   → cs_id={cs_id}", flush=True)

    # PK: OpenAI 响应里的 > 请求里的 > 默认
    pk = (
        (openai_data.get("publishable_key") or "").strip()
        or req.stripe_publishable_key.strip()
        or DEFAULT_STRIPE_PK
    )
    print(f"[oaipc]   → pk={pk[:24]}…", flush=True)

    # Step 2
    print(f"[oaipc] Step 2 Stripe init", flush=True)
    stripe_data = step2_stripe(cs_id, pk, req.payment_locale, req.user_agent, proxy=req.proxy)
    stripe_hosted_url = (
        stripe_data.get("stripe_hosted_url")
        or stripe_data.get("hosted_url")
        or stripe_data.get("url")
        or ""
    )
    if not stripe_hosted_url:
        raise HTTPException(
            status_code=502,
            detail=f"Stripe 响应没 hosted URL: {stripe_data}",
        )
    print(f"[oaipc]   → stripe_hosted_url={stripe_hosted_url[:60]}…", flush=True)

    # Step 3
    if "checkout.stripe.com" not in stripe_hosted_url:
        raise HTTPException(
            status_code=500,
            detail=f"Stripe URL host 不是 checkout.stripe.com, 不敢重写: {stripe_hosted_url}",
        )
    long_url = stripe_hosted_url.replace("checkout.stripe.com", "pay.openai.com")
    print(f"[oaipc] Step 3 long_url={long_url[:60]}…", flush=True)

    return LongLinkResponse(
        ok=True,
        cs_id=cs_id,
        processor_entity=openai_data.get("processor_entity", ""),
        billing_country=country,
        currency=currency,
        payment_locale=req.payment_locale,
        link_type=req.link_type,
        payment_method_type="hosted",
        stripe_hosted_url=stripe_hosted_url,
        long_url=long_url,
    )


# ───────── Entrypoint ─────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print("=" * 60)
    print(f"  本地支付长链生成器  →  http://localhost:{port}")
    print(f"  curl_cffi: {HAS_CFFI}  (没装的话 chatgpt.com 会被 Cloudflare 拦)")
    if not HAS_CFFI:
        print("  ⚠️  建议先: pip install curl_cffi")
    print("=" * 60, flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    except OSError as e:
        if "address already in use" in str(e).lower() or "10048" in str(e):
            print(f"\n❌ 端口 {port} 被占。两种修法：")
            print(f"  1) 关掉占 {port} 的进程")
            print(f"  2) 换个端口: PORT=8765 python server.py\n")
            sys.exit(1)
        raise
