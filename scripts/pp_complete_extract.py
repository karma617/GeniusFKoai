# -*- coding: utf-8 -*-
"""Complete PP BA extractor (oaipay-style + repo stripe_http protocol).

Pools:
  BA_CHECKOUT_PROXY  - OpenAI payments/checkout
  BA_STRIPE_PROXY    - Stripe init/tax/confirm/poll/redirect
  BA_APPROVE_PROXY   - ChatGPT approve (prefer US)
  BA_PROMO_PROXY     - optional second tax update pool

Billing:
  BA_BILLING_COUNTRY / BA_BILLING_CURRENCY / BA_TAX_COUNTRY

Confirm mode:
  BA_CONFIRM_MODE = pm|direct
    pm     : create payment_method then confirm (hammer path)
    direct : stripe_confirm_paypal_direct (payment_protocol path)

BR+JP note:
  Create via BR/JP currently returns amt=0 + payment_method_types=[card,link]
  (no paypal) on trial-eligible geo; BA requires paypal at create time.
  Recovered BA path on this stack: US create (+ optional BR/JP promo tax +
  BA_SKIP_MAIN_TAX=1 + BA_CONFIRM_MODE=direct).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PYTHONIOENCODING"] = "utf-8"

from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt

from platforms.chatgpt import stripe_http

PRE_PROXY = "socks5h://127.0.0.1:7897"
KOOKEEY_API = (
    "https://www.kookeey.com/pickdynamicips?t=2&auth=pwd&format=4&n=1&p=http"
    "&gate=global&g={region}&r=-1&type=txt&sign=874086cfbdb353e32d67a6dbebd498af"
    "&accessid=8239626&upf=1,1&dl=%5Cr%5Cn"
)
PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
STRIPE_PK = stripe_http.STRIPE_PUBLISHABLE_KEY
STRIPE_INIT_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
STRIPE_RUNTIME_VERSION = "6f8494a281"
BA_RE = re.compile(r"ba_token=(BA-[A-Za-z0-9]+)")
PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\"'\s<>]+")

CHECKOUT_PROXY = os.environ.get("BA_CHECKOUT_PROXY", "US").upper()
STRIPE_PROXY = os.environ.get("BA_STRIPE_PROXY", CHECKOUT_PROXY).upper()
APPROVE_PROXY = os.environ.get("BA_APPROVE_PROXY", "US").upper()
PROMO_PROXY = os.environ.get("BA_PROMO_PROXY", "").upper()
BILLING_COUNTRY = os.environ.get("BA_BILLING_COUNTRY", "US").upper()
BILLING_CURRENCY = os.environ.get("BA_BILLING_CURRENCY", "USD").upper()
TAX_COUNTRY = os.environ.get("BA_TAX_COUNTRY", BILLING_COUNTRY).upper()
PROMO_TAX_COUNTRY = os.environ.get("BA_PROMO_TAX_COUNTRY", "").upper()
UI_MODE = os.environ.get("BA_UI_MODE", "custom").lower()
CONFIRM_MODE = os.environ.get("BA_CONFIRM_MODE", "pm").lower()  # pm|direct
# When 1 and promo tax is set, skip the second main tax update so confirm keeps promo amounts.
SKIP_MAIN_TAX = os.environ.get("BA_SKIP_MAIN_TAX", "0") not in {"0", "false", "False"}
MAX_ATTEMPTS = int(os.environ.get("BA_MAX_ATTEMPTS", "4"))
STOP_ON_FIRST = os.environ.get("BA_STOP_ON_FIRST", "1") not in {"0", "false", "False"}
DO_FULL = os.environ.get("BA_DO_FULL", "1") not in {"0", "false", "False"}
OUT_JSON = os.environ.get("BA_OUT_JSON", "scripts/pp_complete_results.json")

ADDRS = {
    "US": {"name": "John Smith", "email": "test@example.com", "country": "US", "state": "NY", "city": "New York", "postal_code": "10001", "line1": "350 5th Ave", "line2": ""},
    "BR": {"name": "Joao Silva", "email": "test@example.com", "country": "BR", "state": "SP", "city": "Sao Paulo", "postal_code": "01310-100", "line1": "Av Paulista 1000", "line2": ""},
    "JP": {"name": "Taro Yamada", "email": "test@example.com", "country": "JP", "state": "Tokyo", "city": "Tokyo", "postal_code": "100-0001", "line1": "1-1 Chiyoda", "line2": ""},
    "TR": {"name": "Ahmet Yilmaz", "email": "test@example.com", "country": "TR", "state": "34", "city": "Istanbul", "postal_code": "34000", "line1": "Istiklal Cad 1", "line2": ""},
    "VN": {"name": "Nguyen Van A", "email": "test@example.com", "country": "VN", "state": "", "city": "Ho Chi Minh", "postal_code": "700000", "line1": "1 Nguyen Hue", "line2": ""},
    "TH": {"name": "Somchai Jaidee", "email": "test@example.com", "country": "TH", "state": "", "city": "Bangkok", "postal_code": "10100", "line1": "1 Silom Road", "line2": ""},
    "IE": {"name": "Sean Murphy", "email": "test@example.com", "country": "IE", "state": "", "city": "Dublin", "postal_code": "D01 F5P2", "line1": "1 Grafton Street", "line2": ""},
}

_ds = cffi.Session(impersonate="chrome110")
with open("scripts/_active_token.json", encoding="utf-8") as f:
    _tok = json.load(f)
TOKEN = _tok["token"]
COOKIE = _tok.get("cookie") or ""
EMAIL = _tok.get("email") or "test@example.com"


def fetch_proxy(region: str) -> str:
    last = ""
    for _ in range(4):
        try:
            r = _ds.get(KOOKEEY_API.format(region=region), timeout=15)
            last = r.text.strip()
            parts = last.split(":")
            if len(parts) == 4:
                return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        except Exception as e:
            last = str(e)
        time.sleep(0.7)
    raise ValueError(f"proxy fail {region}: {last[:120]}")


def make_session(proxy_url: str):
    return cffi.Session(
        impersonate="chrome110",
        proxy=proxy_url,
        curl_options={CurlOpt.PRE_PROXY: PRE_PROXY},
    )


def _jwt_account_id(token: str) -> str:
    try:
        import base64, json as _json
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(part.encode()))
        auth = claims.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or "")
    except Exception:
        return ""


ACCOUNT_ID = _jwt_account_id(TOKEN)


def auth_headers(extra=None):
    h = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if ACCOUNT_ID:
        h["Chatgpt-Account-Id"] = ACCOUNT_ID
    if COOKIE:
        h["cookie"] = COOKIE
    if extra:
        h.update(extra)
    return h


def has_paypal(pmt) -> bool:
    return isinstance(pmt, list) and "paypal" in [str(x).lower() for x in pmt]


def find_redirect(payload):
    url = ""
    try:
        url, _ = stripe_http.extract_paypal_redirect_url(payload)
    except Exception:
        pass
    if url:
        return url
    text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload)
    m = PM_REDIRECT_RE.search(text)
    return m.group(0) if m else ""


def follow_ba(sess, redir: str) -> tuple[bool, str, str]:
    rg = sess.get(redir, allow_redirects=True, timeout=25, headers={"Referer": "https://pay.openai.com/"})
    fin = str(getattr(rg, "url", "") or "")
    bm = BA_RE.search(fin)
    if bm:
        return True, bm.group(1), fin[:240]
    # also scan body
    bm = BA_RE.search(getattr(rg, "text", "") or "")
    if bm:
        return True, bm.group(1), fin[:240]
    return False, "", fin[:240]


def try_once(n: int) -> dict:
    bill_addr = dict(ADDRS.get(BILLING_COUNTRY, ADDRS["US"]))
    tax_addr = dict(ADDRS.get(TAX_COUNTRY, bill_addr))
    promo_addr = dict(ADDRS.get(PROMO_TAX_COUNTRY)) if PROMO_TAX_COUNTRY else None
    # force email from token for better consistency
    bill_addr["email"] = EMAIL
    tax_addr["email"] = EMAIL
    if promo_addr:
        promo_addr["email"] = EMAIL

    res = {
        "attempt": n,
        "checkout_proxy": CHECKOUT_PROXY,
        "stripe_proxy": STRIPE_PROXY,
        "approve_proxy": APPROVE_PROXY,
        "promo_proxy": PROMO_PROXY or None,
        "billing": f"{BILLING_COUNTRY}/{BILLING_CURRENCY}",
        "tax_country": TAX_COUNTRY,
        "promo_tax_country": PROMO_TAX_COUNTRY or None,
        "ui": UI_MODE,
        "confirm_mode": CONFIRM_MODE,
        "approve_result": "",
        "ba_ok": False,
        "ba": "",
        "ba_url": "",
        "amt": "",
        "zero": False,
        "paypal": False,
        "pmt": None,
        "err": "",
        "steps": {},
    }
    sessions = []
    try:
        p_chk = fetch_proxy(CHECKOUT_PROXY)
        s_chk = make_session(p_chk)
        sessions.append(s_chk)
        res["steps"]["proxy_checkout"] = p_chk.split("@")[-1]

        if STRIPE_PROXY == CHECKOUT_PROXY:
            s_stripe = s_chk
        else:
            p_st = fetch_proxy(STRIPE_PROXY)
            s_stripe = make_session(p_st)
            sessions.append(s_stripe)
            res["steps"]["proxy_stripe"] = p_st.split("@")[-1]

        if APPROVE_PROXY == CHECKOUT_PROXY:
            s_appr = s_chk
        elif APPROVE_PROXY == STRIPE_PROXY:
            s_appr = s_stripe
        else:
            p_ap = fetch_proxy(APPROVE_PROXY)
            s_appr = make_session(p_ap)
            sessions.append(s_appr)
            res["steps"]["proxy_approve"] = p_ap.split("@")[-1]

        s_promo = None
        if PROMO_PROXY:
            if PROMO_PROXY == STRIPE_PROXY:
                s_promo = s_stripe
            elif PROMO_PROXY == CHECKOUT_PROXY:
                s_promo = s_chk
            else:
                p_pr = fetch_proxy(PROMO_PROXY)
                s_promo = make_session(p_pr)
                sessions.append(s_promo)
                res["steps"]["proxy_promo"] = p_pr.split("@")[-1]

        pl = {
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": BILLING_COUNTRY, "currency": BILLING_CURRENCY},
            "entry_point": "all_plans_pricing_modal",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
            "checkout_ui_mode": UI_MODE,
        }
        r = s_chk.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=30)
        res["steps"]["checkout"] = r.status_code
        if r.status_code != 200:
            res["err"] = "checkout " + str(r.status_code)
            res["body"] = r.text[:180]
            return res
        data = r.json()
        cs = data.get("checkout_session_id") or ""
        pk = data.get("publishable_key") or STRIPE_PK
        ent = data.get("processor_entity") or "openai_llc"
        res["cs"] = cs
        res["ent"] = ent
        res["trial"] = data.get("one_click_trial_eligible")
        res["promo_campaign"] = data.get("promo_campaign")
        if not cs:
            res["err"] = "no cs"
            return res

        # stripe_http helpers use fixed STRIPE_PUBLISHABLE_KEY internally; ok for OpenAI live
        init = stripe_http.stripe_init(s_stripe, cs_id=cs)
        res["steps"]["init"] = 200
        ck = str(init.get("init_checksum") or "")
        cid = str(init.get("config_id") or "")
        pmt = init.get("payment_method_types") or []
        amt = stripe_http.extract_expected_amount(init)
        res["pmt"] = pmt
        res["amt"] = amt
        res["paypal"] = has_paypal(pmt)
        res["zero"] = str(amt) == "0"
        res["amts"] = stripe_http.extract_display_amounts(init) if hasattr(stripe_http, "extract_display_amounts") else {}
        latest = dict(init)

        if s_promo is not None and promo_addr is not None:
            try:
                pr = stripe_http.stripe_update_tax_region(s_promo, cs_id=cs, address=promo_addr)
                res["steps"]["promo_tax"] = 200
                latest = stripe_http.merge_checkout_payload(latest, pr) if hasattr(stripe_http, "merge_checkout_payload") else dict(pr)
                ck = str(latest.get("init_checksum") or ck)
                pmt = latest.get("payment_method_types") or pmt
                amt = stripe_http.extract_expected_amount(latest)
                res["pmt"] = pmt
                res["amt"] = amt
                res["paypal"] = has_paypal(pmt)
                res["zero"] = str(amt) == "0"
                res["amt_after_promo"] = amt
            except Exception as e:
                res["steps"]["promo_tax_err"] = str(e)[:120]

        if not DO_FULL:
            return res

        confirm_addr = dict(tax_addr)
        # main tax (optional; skip to keep promo tax amounts for recovery tests)
        if SKIP_MAIN_TAX:
            res["steps"]["tax"] = "skipped"
            if promo_addr is not None:
                confirm_addr = dict(promo_addr)
            res["amts_tax"] = stripe_http.extract_display_amounts(latest) if hasattr(stripe_http, "extract_display_amounts") else {}
        else:
            try:
                tax = stripe_http.stripe_update_tax_region(s_stripe, cs_id=cs, address=tax_addr)
                res["steps"]["tax"] = 200
                latest = stripe_http.merge_checkout_payload(latest, tax) if hasattr(stripe_http, "merge_checkout_payload") else dict(tax)
                ck = str(latest.get("init_checksum") or ck)
                pmt = latest.get("payment_method_types") or pmt
                amt = stripe_http.extract_expected_amount(latest)
                res["pmt"] = pmt
                res["amt"] = amt
                res["paypal"] = has_paypal(pmt)
                res["zero"] = str(amt) == "0"
                res["amts_tax"] = stripe_http.extract_display_amounts(latest) if hasattr(stripe_http, "extract_display_amounts") else {}
            except Exception as e:
                res["steps"]["tax_err"] = str(e)[:140]

        if not res["paypal"]:
            res["err"] = "no paypal pmt=" + str(res.get("pmt"))
            return res

        expected_amount, expected_on_bca = stripe_http.extract_confirm_expected_amounts(latest, fallback_amount=str(amt or "0"))
        displayed = stripe_http.extract_display_amounts(latest) if hasattr(stripe_http, "extract_display_amounts") else {}
        res["expected_amount"] = expected_amount
        res["expected_amount_on_bca"] = expected_on_bca

        success_url = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs}&processor_entity={ent}&plan_type=plus"
        return_url = stripe_http.build_confirm_return_url(latest, cs_id=cs, fallback_url=success_url)
        referrer = stripe_http.build_confirm_referrer_url(latest, cs_id=cs, fallback_url=f"https://pay.openai.com/c/pay/{cs}")

        conf_resp = None
        if CONFIRM_MODE == "direct":
            conf_resp = stripe_http.stripe_confirm_paypal_direct(
                s_stripe,
                cs_id=cs,
                init_checksum=ck,
                email=EMAIL,
                address=confirm_addr,
                return_url=return_url,
                expected_amount=expected_amount,
                expected_amount_on_bca=expected_on_bca,
                displayed_amounts=displayed,
                referrer=referrer,
            )
            res["steps"]["confirm"] = 200
        else:
            # payment_method path (compatible with custom checkout betas)
            sh = {
                "Origin": "https://pay.openai.com",
                "Referer": "https://pay.openai.com/",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            ctx = {
                "stripe_js_id": str(uuid.uuid4()),
                "elements_session_id": "es_" + uuid.uuid4().hex[:11],
                "elements_session_config_id": cid or str(uuid.uuid4()),
                "config_id": cid,
            }
            pmb = {
                "billing_details[name]": tax_addr.get("name") or "Test User",
                "billing_details[email]": EMAIL,
                "billing_details[address][country]": tax_addr["country"],
                "billing_details[address][line1]": tax_addr["line1"],
                "billing_details[address][city]": tax_addr["city"],
                "billing_details[address][postal_code]": tax_addr["postal_code"],
                "type": "paypal",
                "payment_user_agent": f"stripe.js/{STRIPE_RUNTIME_VERSION}; stripe-js-v3/{STRIPE_RUNTIME_VERSION}; payment-element; deferred-intent",
                "referrer": "https://chatgpt.com",
                "time_on_page": "35000",
                "client_attribution_metadata[checkout_session_id]": cs,
                "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
                "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
                "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
                "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
                "client_attribution_metadata[merchant_integration_source]": "elements",
                "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
                "client_attribution_metadata[merchant_integration_version]": "2021",
                "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
                "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                "guid": uuid.uuid4().hex,
                "muid": uuid.uuid4().hex,
                "sid": uuid.uuid4().hex,
                "key": pk,
                "_stripe_version": STRIPE_INIT_VERSION,
            }
            if tax_addr.get("state"):
                pmb["billing_details[address][state]"] = tax_addr["state"]
            pmr = s_stripe.post("https://api.stripe.com/v1/payment_methods", data=pmb, headers=sh, timeout=30)
            res["steps"]["pm"] = pmr.status_code
            if pmr.status_code != 200:
                res["err"] = "pm " + str(pmr.status_code)
                res["body"] = pmr.text[:180]
                return res
            pmid = str(pmr.json().get("id") or "")
            if not pmid.startswith("pm_"):
                res["err"] = "bad pm"
                return res
            conf = {
                "guid": uuid.uuid4().hex,
                "muid": uuid.uuid4().hex,
                "sid": uuid.uuid4().hex,
                "payment_method": pmid,
                "init_checksum": ck,
                "version": STRIPE_RUNTIME_VERSION,
                "expected_amount": str(expected_amount),
                "expected_payment_method_type": "paypal",
                "return_url": return_url,
                "elements_session_client[session_id]": ctx["elements_session_id"],
                "elements_session_client[locale]": "en",
                "elements_session_client[referrer_host]": "chatgpt.com",
                "elements_session_client[is_aggregation_expected]": "false",
                "elements_session_client[elements_init_source]": "custom_checkout",
                "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
                "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
                "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
                "elements_options_client[saved_payment_method][enable_save]": "never",
                "elements_options_client[saved_payment_method][enable_redisplay]": "never",
                "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
                "client_attribution_metadata[checkout_session_id]": cs,
                "client_attribution_metadata[checkout_config_id]": ctx.get("config_id", ""),
                "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
                "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
                "client_attribution_metadata[merchant_integration_source]": "checkout",
                "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
                "client_attribution_metadata[merchant_integration_version]": "custom",
                "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
                "client_attribution_metadata[payment_method_selection_flow]": "automatic",
                "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
                "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
                "consent[terms_of_service]": "accepted",
                "key": pk,
                "_stripe_version": STRIPE_INIT_VERSION,
            }
            if expected_on_bca:
                conf["expected_amount_on_bca"] = str(expected_on_bca)
            # include displayed amounts when available
            if isinstance(displayed, dict):
                for k, form_k in [
                    ("subtotal", "last_displayed_line_item_group_details[subtotal]"),
                    ("total_exclusive_tax", "last_displayed_line_item_group_details[total_exclusive_tax]"),
                    ("total_inclusive_tax", "last_displayed_line_item_group_details[total_inclusive_tax]"),
                    ("total_discount_amount", "last_displayed_line_item_group_details[total_discount_amount]"),
                    ("shipping_rate_amount", "last_displayed_line_item_group_details[shipping_rate_amount]"),
                ]:
                    if displayed.get(k) is not None:
                        conf[form_k] = str(displayed.get(k))
            cr = s_stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs}/confirm", data=conf, headers=sh, timeout=30)
            res["steps"]["confirm"] = cr.status_code
            if cr.status_code != 200:
                res["err"] = "confirm " + str(cr.status_code)
                res["body"] = cr.text[:200]
                return res
            conf_resp = cr.json()

        if isinstance(conf_resp, dict):
            res["steps"]["confirm_keys"] = list(conf_resp.keys())[:20]
            psa0 = conf_resp.get("submission_attempt") or {}
            if isinstance(psa0, dict):
                res["steps"]["confirm_sa"] = psa0.get("state")
        redir = find_redirect(conf_resp if isinstance(conf_resp, dict) else {})
        if redir:
            ok, ba, url = follow_ba(s_stripe, redir)
            if ok:
                res["ba_ok"] = True
                res["ba"] = ba
                res["ba_url"] = url
                res["approve_result"] = "confirm_redirect"
                return res
            res["err"] = "confirm redirect no ba: " + url[:80]

        # ChatGPT approve + poll
        try:
            s_appr.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={},
                headers={
                    "x-openai-target-path": "/backend-api/sentinel/ping",
                    "x-openai-target-route": "/backend-api/sentinel/ping",
                },
                timeout=4,
            )
        except Exception:
            pass
        pt = "/backend-api/payments/checkout/approve"
        ar = s_appr.post(
            "https://chatgpt.com" + pt,
            json={"checkout_session_id": cs, "processor_entity": ent},
            headers=auth_headers(
                {
                    "Referer": f"https://chatgpt.com/checkout/{ent}/{cs}",
                    "x-openai-target-path": pt,
                    "x-openai-target-route": pt,
                }
            ),
            timeout=20,
        )
        res["steps"]["approve_http"] = ar.status_code
        try:
            ab = ar.json()
            res["approve_result"] = ab.get("result", "unknown_" + str(ar.status_code))
        except Exception:
            res["approve_result"] = "http_" + str(ar.status_code)
            res["body"] = ar.text[:160]
            return res
        if ar.status_code != 200:
            res["err"] = "approve " + str(ar.status_code)
            return res
        if res["approve_result"] != "approved":
            return res

        # poll using custom-checkout payment_pages GET (same as proven hammer)
        poll_params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": "es_" + uuid.uuid4().hex[:11],
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": pk,
            "_stripe_version": STRIPE_INIT_VERSION,
        }
        ph = {"Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/", "Accept": "application/json"}
        for i in range(12):
            time.sleep(1.5)
            pr = s_stripe.get(
                f"https://api.stripe.com/v1/payment_pages/{cs}",
                params=poll_params,
                headers=ph,
                timeout=10,
            )
            if pr.status_code != 200:
                res["steps"]["poll_http"] = pr.status_code
                continue
            poll = pr.json()
            redir = find_redirect(poll)
            if redir:
                ok, ba, url = follow_ba(s_stripe, redir)
                if ok:
                    res["ba_ok"] = True
                    res["ba"] = ba
                    res["ba_url"] = url
                else:
                    res["err"] = "poll redirect no ba: " + url[:80]
                return res
            psa = poll.get("submission_attempt") or {}
            if isinstance(psa, dict):
                st = psa.get("state")
                res["steps"]["poll_sa"] = st
                if st == "failed":
                    pe = psa.get("error") or {}
                    res["err"] = "failed:" + str(pe.get("code") if isinstance(pe, dict) else pe)
                    return res
        if not res.get("err"):
            res["err"] = "timeout approved but no redirect"
        return res
    except Exception as e:
        res["err"] = str(e)[:200]
        low = res["err"].lower()
        if any(x in low for x in ("tls", "timeout", "proxy", "connect", "ssl", "curl")):
            res["approve_result"] = "network_error"
        return res
    finally:
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass


def main():
    print(
        f"=== complete extract chk={CHECKOUT_PROXY} stripe={STRIPE_PROXY} approve={APPROVE_PROXY} "
        f"promo={PROMO_PROXY or '-'} bill={BILLING_COUNTRY}/{BILLING_CURRENCY} tax={TAX_COUNTRY} "
        f"promo_tax={PROMO_TAX_COUNTRY or '-'} skip_main_tax={int(SKIP_MAIN_TAX)} ui={UI_MODE} confirm={CONFIRM_MODE} full={DO_FULL} max={MAX_ATTEMPTS} ===",
        flush=True,
    )
    results = []
    for i in range(1, MAX_ATTEMPTS + 1):
        r = try_once(i)
        results.append(r)
        print(
            f"  [{i:02d}] approve={str(r.get('approve_result') or ''):16s} ba_ok={str(r.get('ba_ok')):5s} "
            f"amt={str(r.get('amt') or ''):6s} zero={str(r.get('zero')):5s} paypal={str(r.get('paypal')):5s} "
            f"pmt={r.get('pmt')} err={str(r.get('err') or '')[:90]}",
            flush=True,
        )
        if r.get("steps"):
            print(f"       steps={r.get('steps')}", flush=True)
        if r.get("ba_ok") and STOP_ON_FIRST:
            print(f"  BA_URL={r.get('ba_url')}", flush=True)
            break
        time.sleep(0.35)
    print("\n=== SUMMARY ===", flush=True)
    cnt = Counter((r.get("approve_result") or "empty") for r in results)
    for k, v in cnt.most_common():
        ok = sum(1 for r in results if (r.get("approve_result") or "empty") == k and r.get("ba_ok"))
        print(f"  {k}: {v} total, {ok} ba_ok", flush=True)
    print(
        f"  overall_ba_ok={sum(1 for r in results if r.get('ba_ok'))}/{len(results)} "
        f"zero_and_paypal={sum(1 for r in results if r.get('zero') and r.get('paypal'))}",
        flush=True,
    )
    Path(OUT_JSON).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  saved={OUT_JSON}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
