# -*- coding: utf-8 -*-
"""Triple-pool PP BA extractor (checkout/stripe/approve + optional promo).

Mode2 contract (frontend):
  billing_country=US, checkout_ui_mode=hosted, link_type=paypal
  proxyPools.checkout = US
  proxyPools.promotion = TR  # promotion update only

This script implements:
  1) checkout pool: OpenAI checkout + Stripe init (+ optional full custom BA path)
  2) promotion pool: payment_pages update on same cs (tax/billing/address)
  3) approve/poll on checkout pool (US preferred for approve)

Env:
  BA_CHECKOUT_PROXY / BA_PROMO_PROXY
  BA_BILLING_COUNTRY / BA_BILLING_CURRENCY
  BA_UI_MODE=custom|hosted
  BA_PROMO_WHEN=after_init|after_tax|none
  BA_MAX_ATTEMPTS / BA_STOP_ON_FIRST / BA_DO_FULL
  BA_PROMO_ADDR_COUNTRY  # address country used in promotion update (default=billing)
"""
import json
import os
import re
import sys
import time
import uuid
from collections import Counter

sys.path.insert(0, ".")
os.environ["PYTHONIOENCODING"] = "utf-8"

from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt

PRE_PROXY = "socks5h://127.0.0.1:7897"
KOOKEEY_API = (
    "https://www.kookeey.com/pickdynamicips?t=2&auth=pwd&format=4&n=1&p=http"
    "&gate=global&g={region}&r=-1&type=txt&sign=874086cfbdb353e32d67a6dbebd498af"
    "&accessid=8239626&upf=1,1&dl=%5Cr%5Cn"
)
PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
STRIPE_API_BASE = "https://api.stripe.com/v1"
STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_INIT_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
STRIPE_RUNTIME_VERSION = "6f8494a281"
PM_REDIRECT_RE = re.compile(r"https://pm-redirects\.stripe\.com/authorize/[^\"\s<>]+")
BA_RE = re.compile(r"ba_token=(BA-[A-Za-z0-9]+)")

CHECKOUT_PROXY = os.environ.get("BA_CHECKOUT_PROXY", "US").upper()
STRIPE_PROXY = os.environ.get("BA_STRIPE_PROXY", CHECKOUT_PROXY).upper()
APPROVE_PROXY = os.environ.get("BA_APPROVE_PROXY", "US").upper()
PROMO_PROXY = os.environ.get("BA_PROMO_PROXY", "").upper()
BILLING_COUNTRY = os.environ.get("BA_BILLING_COUNTRY", "US").upper()
BILLING_CURRENCY = os.environ.get("BA_BILLING_CURRENCY", "USD").upper()
UI_MODE = os.environ.get("BA_UI_MODE", "custom").lower()  # custom|hosted
PROMO_WHEN = os.environ.get("BA_PROMO_WHEN", "none").lower()  # after_init|after_tax|none
PROMO_ADDR_COUNTRY = os.environ.get("BA_PROMO_ADDR_COUNTRY", BILLING_COUNTRY).upper()
TAX_COUNTRY = os.environ.get("BA_TAX_COUNTRY", BILLING_COUNTRY).upper()
MAX_ATTEMPTS = int(os.environ.get("BA_MAX_ATTEMPTS", "5"))
STOP_ON_FIRST = os.environ.get("BA_STOP_ON_FIRST", "1") not in {"0", "false", "False"}
DO_FULL = os.environ.get("BA_DO_FULL", "1") not in {"0", "false", "False"}
OUT_JSON = os.environ.get("BA_OUT_JSON", "scripts/pp_triple_proxy_results.json")

ADDRS = {
    "US": {"name": "John Smith", "email": "test@example.com", "country": "US", "state": "NY", "city": "New York", "postal_code": "10001", "line1": "350 5th Ave"},
    "IE": {"name": "Sean Murphy", "email": "test@example.com", "country": "IE", "state": "", "city": "Dublin", "postal_code": "D01 F5P2", "line1": "1 Grafton Street"},
    "DE": {"name": "Max Mustermann", "email": "test@example.com", "country": "DE", "state": "", "city": "Berlin", "postal_code": "10115", "line1": "Friedrichstrasse 1"},
    "FR": {"name": "Jean Dupont", "email": "test@example.com", "country": "FR", "state": "", "city": "Paris", "postal_code": "75001", "line1": "1 Rue de Rivoli"},
    "NL": {"name": "Jan de Vries", "email": "test@example.com", "country": "NL", "state": "", "city": "Amsterdam", "postal_code": "1011 AA", "line1": "Damrak 1"},
    "BE": {"name": "Pierre Dubois", "email": "test@example.com", "country": "BE", "state": "", "city": "Brussels", "postal_code": "1000", "line1": "Rue Neuve 1"},
    "BR": {"name": "Joao Silva", "email": "test@example.com", "country": "BR", "state": "SP", "city": "Sao Paulo", "postal_code": "01310-100", "line1": "Av Paulista 1000"},
    "JP": {"name": "Taro Yamada", "email": "test@example.com", "country": "JP", "state": "Tokyo", "city": "Tokyo", "postal_code": "100-0001", "line1": "1-1 Chiyoda"},
    "TR": {"name": "Ahmet Yilmaz", "email": "test@example.com", "country": "TR", "state": "34", "city": "Istanbul", "postal_code": "34000", "line1": "Istiklal Cad 1"},
    "VN": {"name": "Nguyen Van A", "email": "test@example.com", "country": "VN", "state": "", "city": "Ho Chi Minh", "postal_code": "700000", "line1": "1 Nguyen Hue"},
    "TH": {"name": "Somchai Jaidee", "email": "test@example.com", "country": "TH", "state": "", "city": "Bangkok", "postal_code": "10100", "line1": "1 Silom Road"},
    "MY": {"name": "Ahmad Rahman", "email": "test@example.com", "country": "MY", "state": "", "city": "Kuala Lumpur", "postal_code": "50000", "line1": "1 Jalan Ampang"},
    "PH": {"name": "Juan Dela Cruz", "email": "test@example.com", "country": "PH", "state": "", "city": "Manila", "postal_code": "1000", "line1": "1 Rizal Avenue"},
    "IN": {"name": "Rahul Sharma", "email": "test@example.com", "country": "IN", "state": "MH", "city": "Mumbai", "postal_code": "400001", "line1": "1 MG Road"},
}

_ds = cffi.Session(impersonate="chrome110")
sh = {
    "Origin": "https://pay.openai.com",
    "Referer": "https://pay.openai.com/",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}


def fetch_proxy(region: str) -> str:
    for _ in range(3):
        try:
            r = _ds.get(KOOKEEY_API.format(region=region), timeout=15)
            parts = r.text.strip().split(":")
            if len(parts) == 4:
                return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        except Exception:
            pass
        time.sleep(1)
    raise ValueError("proxy fail: " + region)


def make_session(proxy_url: str):
    return cffi.Session(
        impersonate="chrome110",
        proxy=proxy_url,
        curl_options={CurlOpt.PRE_PROXY: PRE_PROXY},
    )


with open("scripts/_active_token.json", encoding="utf-8") as f:
    _tok = json.load(f)
TOKEN = _tok["token"]
COOKIE = _tok.get("cookie") or ""


def extract_amounts(d: dict) -> dict:
    out = {}
    if not isinstance(d, dict):
        return out
    eo = d.get("elements_options") or {}
    inv = d.get("invoice") or {}
    if isinstance(eo, dict) and eo.get("amount") is not None:
        out["eo_amount"] = eo.get("amount")
    if isinstance(inv, dict):
        if inv.get("amount_due") is not None:
            out["inv_due"] = inv.get("amount_due")
        if inv.get("total") is not None:
            out["inv_total"] = inv.get("total")
        if inv.get("subtotal") is not None:
            out["inv_subtotal"] = inv.get("subtotal")
    td = d.get("total_details") or d.get("total_summary") or {}
    if isinstance(td, dict) and td.get("amount_due") is not None:
        out["total_due"] = td.get("amount_due")
    return out


def pick_amt(amts: dict, default=""):
    for k in ("eo_amount", "inv_due", "total_due", "inv_total", "inv_subtotal"):
        if amts.get(k) is not None:
            return str(amts.get(k))
    return default


def find_redirect(d):
    if not isinstance(d, dict):
        return ""

    def _s(v):
        if isinstance(v, str):
            m = PM_REDIRECT_RE.search(v)
            return m.group(0) if m else ""
        if isinstance(v, dict):
            na = v.get("next_action")
            if isinstance(na, dict) and na.get("type") == "redirect_to_url":
                rtu = na.get("redirect_to_url")
                if isinstance(rtu, dict):
                    url = str(rtu.get("url") or "").strip()
                    if url:
                        return url
            for x in v.values():
                r = _s(x)
                if r:
                    return r
        if isinstance(v, list):
            for x in v:
                r = _s(x)
                if r:
                    return r
        return ""

    for k in ("setup_intent", "payment_intent"):
        r = _s(d.get(k) or {})
        if r:
            return r
    return _s(d)


def auth_headers():
    h = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if COOKIE:
        h["cookie"] = COOKIE
    return h


def payment_pages_update(sess, cs, pk, addr, ck=""):
    # Match ba_multi_ip_hammer: only tax_region + key + version.
    # init_checksum / billing_details cause parameter_unknown on this endpoint.
    tb = {
        "eid": str(uuid.uuid4()),
        "tax_region[country]": addr["country"],
        "tax_region[postal_code]": addr["postal_code"],
        "tax_region[line1]": addr["line1"],
        "tax_region[city]": addr["city"],
        "key": pk,
        "_stripe_version": STRIPE_INIT_VERSION,
    }
    if addr.get("state"):
        tb["tax_region[state]"] = addr["state"]
    r = sess.post(f"{STRIPE_API_BASE}/payment_pages/{cs}", data=tb, headers=sh, timeout=30)
    return r


def try_once(attempt_no: int) -> dict:
    bill_addr = ADDRS.get(BILLING_COUNTRY, ADDRS["US"])
    promo_addr = ADDRS.get(PROMO_ADDR_COUNTRY, bill_addr)
    res = {
        "attempt": attempt_no,
        "checkout_proxy": CHECKOUT_PROXY,
        "stripe_proxy": STRIPE_PROXY,
        "approve_proxy": APPROVE_PROXY,
        "promo_proxy": PROMO_PROXY,
        "tax_country": TAX_COUNTRY,
        "billing_country": BILLING_COUNTRY,
        "billing_currency": BILLING_CURRENCY,
        "promo_addr_country": PROMO_ADDR_COUNTRY,
        "ui_mode": UI_MODE,
        "promo_when": PROMO_WHEN,
        "approve_result": "",
        "ba_ok": False,
        "ba": "",
        "ba_url": "",
        "amt": "",
        "amt_after_promo": "",
        "zero": False,
        "paypal": False,
        "pmt": None,
        "one_click_trial_eligible": None,
        "promo_campaign": None,
        "err": "",
        "steps": {},
    }
    s_chk = None
    s_stripe = None
    s_appr = None
    s_promo = None
    sessions = []
    try:
        p1 = fetch_proxy(CHECKOUT_PROXY)
        s_chk = make_session(p1)
        sessions.append(s_chk)
        res["steps"]["proxy_checkout"] = p1.split("@")[-1]

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

        if PROMO_WHEN != "none" and PROMO_PROXY:
            if PROMO_PROXY == STRIPE_PROXY:
                s_promo = s_stripe
            elif PROMO_PROXY == CHECKOUT_PROXY:
                s_promo = s_chk
            else:
                p2 = fetch_proxy(PROMO_PROXY)
                s_promo = make_session(p2)
                sessions.append(s_promo)
                res["steps"]["proxy_promo"] = p2.split("@")[-1]

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
        res["one_click_trial_eligible"] = data.get("one_click_trial_eligible")
        res["promo_campaign"] = data.get("promo_campaign")
        res["checkout_ui_mode_resp"] = data.get("checkout_ui_mode")
        if not cs:
            res["err"] = "no cs"
            return res

        ib = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Shanghai",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout" if UI_MODE == "custom" else "checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": pk,
            "_stripe_version": STRIPE_INIT_VERSION,
        }
        ir = s_stripe.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/init", data=ib, headers=sh, timeout=30)
        res["steps"]["init"] = ir.status_code
        if ir.status_code != 200:
            res["err"] = "init " + str(ir.status_code)
            res["body"] = ir.text[:180]
            return res
        init = ir.json()
        ck = init.get("init_checksum", "")
        cid = str(init.get("config_id") or "")
        pmt = init.get("payment_method_types") or []
        amts = extract_amounts(init)
        amt = pick_amt(amts, "?")
        res["pmt"] = pmt
        res["amt"] = amt
        res["amts"] = amts
        res["paypal"] = "paypal" in [str(x).lower() for x in pmt] if isinstance(pmt, list) else False
        res["zero"] = str(amt) == "0"

        # promotion update after init
        if PROMO_WHEN == "after_init" and s_promo is not None:
            pr = payment_pages_update(s_promo, cs, pk, promo_addr, ck)
            res["steps"]["promo_update"] = pr.status_code
            if pr.status_code == 200:
                pd = pr.json()
                ck = pd.get("init_checksum", ck)
                if pd.get("payment_method_types"):
                    pmt = pd.get("payment_method_types") or pmt
                    res["pmt"] = pmt
                    res["paypal"] = "paypal" in [str(x).lower() for x in pmt] if isinstance(pmt, list) else False
                amts2 = extract_amounts(pd)
                amt2 = pick_amt(amts2, amt)
                res["amt_after_promo"] = amt2
                res["amts_after_promo"] = amts2
                res["zero"] = str(amt2) == "0"
                res["amt"] = amt2
            else:
                res["steps"]["promo_body"] = pr.text[:160]

        if not DO_FULL:
            return res

        if UI_MODE == "custom":
            if not res["paypal"]:
                res["err"] = "no paypal"
                return res

            # tax_region on checkout session (may re-bind amount)
            tr = payment_pages_update(s_stripe, cs, pk, ADDRS.get(TAX_COUNTRY, bill_addr), ck)
            res["steps"]["tax"] = tr.status_code
            if tr.status_code == 200:
                td = tr.json()
                ck = td.get("init_checksum", ck)
                amts3 = extract_amounts(td)
                amt3 = pick_amt(amts3, res["amt"])
                res["amt"] = amt3
                res["zero"] = str(amt3) == "0"
                res["amts_tax"] = amts3
            else:
                res["steps"]["tax_body"] = tr.text[:160]

            if PROMO_WHEN == "after_tax" and s_promo is not None:
                pr = payment_pages_update(s_promo, cs, pk, promo_addr, ck)
                res["steps"]["promo_update"] = pr.status_code
                if pr.status_code == 200:
                    pd = pr.json()
                    ck = pd.get("init_checksum", ck)
                    amts2 = extract_amounts(pd)
                    amt2 = pick_amt(amts2, res["amt"])
                    res["amt_after_promo"] = amt2
                    res["zero"] = str(amt2) == "0"
                    res["amt"] = amt2
                    if pd.get("payment_method_types"):
                        pmt = pd.get("payment_method_types") or pmt
                        res["pmt"] = pmt
                        res["paypal"] = "paypal" in [str(x).lower() for x in pmt] if isinstance(pmt, list) else False
                else:
                    res["steps"]["promo_body"] = pr.text[:160]

            if not res["paypal"]:
                res["err"] = "no paypal after promo/tax"
                return res

            ctx = {
                "stripe_js_id": str(uuid.uuid4()),
                "elements_session_id": "es_" + uuid.uuid4().hex[:11],
                "elements_session_config_id": cid or str(uuid.uuid4()),
                "config_id": cid,
                "init_checksum": ck,
                "locale": "en",
                "runtime_version": STRIPE_RUNTIME_VERSION,
            }
            pmb = {
                "billing_details[name]": bill_addr["name"],
                "billing_details[email]": bill_addr["email"],
                "billing_details[address][country]": bill_addr["country"],
                "billing_details[address][line1]": bill_addr["line1"],
                "billing_details[address][city]": bill_addr["city"],
                "billing_details[address][postal_code]": bill_addr["postal_code"],
                "type": "paypal",
                "payment_user_agent": "stripe.js/" + STRIPE_RUNTIME_VERSION + "; stripe-js-v3/" + STRIPE_RUNTIME_VERSION + "; payment-element; deferred-intent",
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
                "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
                "guid": uuid.uuid4().hex,
                "muid": uuid.uuid4().hex,
                "sid": uuid.uuid4().hex,
                "key": pk,
                "_stripe_version": STRIPE_INIT_VERSION,
            }
            if bill_addr.get("state"):
                pmb["billing_details[address][state]"] = bill_addr["state"]
            pmr = s_stripe.post(f"{STRIPE_API_BASE}/payment_methods", data=pmb, headers=sh, timeout=30)
            res["steps"]["pm"] = pmr.status_code
            if pmr.status_code != 200:
                res["err"] = "pm " + str(pmr.status_code)
                res["body"] = pmr.text[:180]
                return res
            pmid = str(pmr.json().get("id") or "")
            if not pmid.startswith("pm_"):
                res["err"] = "bad pm"
                return res

            surl = "https://chatgpt.com/checkout/verify?stripe_session_id=" + cs + "&processor_entity=" + ent + "&plan_type=plus"
            rurl_base = "https://pay.openai.com/c/pay/" + cs + "?success_return_url=" + surl
            conf = {
                "guid": uuid.uuid4().hex,
                "muid": uuid.uuid4().hex,
                "sid": uuid.uuid4().hex,
                "payment_method": pmid,
                "init_checksum": ck,
                "version": STRIPE_RUNTIME_VERSION,
                "expected_amount": str(res.get("amt") or "0"),
                "expected_payment_method_type": "paypal",
                "return_url": rurl_base,
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
            cr = s_stripe.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/confirm", data=conf, headers=sh, timeout=30)
            res["steps"]["confirm"] = cr.status_code
            if cr.status_code != 200:
                res["err"] = "confirm " + str(cr.status_code)
                res["body"] = cr.text[:180]
                return res
            cd = cr.json()
            pmr_url = find_redirect(cd)
            if pmr_url:
                rg = s_stripe.get(pmr_url, allow_redirects=True, timeout=20, headers={"Referer": "https://pay.openai.com/"})
                fin = str(getattr(rg, "url", ""))
                bm = BA_RE.search(fin)
                if bm:
                    res["ba"] = bm.group(1)
                    res["ba_url"] = fin[:220]
                    res["ba_ok"] = True
                    res["approve_result"] = "confirm_redirect"
                else:
                    res["err"] = "no ba: " + fin[:80]
                return res

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
            ah = {
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/json",
                "oai-language": "zh-CN",
                "Referer": f"https://chatgpt.com/checkout/{ent}/{cs}",
                "x-openai-target-path": pt,
                "x-openai-target-route": pt,
            }
            if COOKIE:
                ah["cookie"] = COOKIE
            ar = s_appr.post(
                "https://chatgpt.com" + pt,
                json={"checkout_session_id": cs, "processor_entity": ent},
                headers=ah,
                timeout=20,
            )
            res["steps"]["approve_http"] = ar.status_code
            res["steps"]["approve_path"] = pt
            try:
                ab = ar.json()
                res["approve_result"] = ab.get("result", "unknown_" + str(ar.status_code))
            except Exception:
                res["approve_result"] = "http_" + str(ar.status_code)
            if ar.status_code != 200:
                res["err"] = "approve " + str(ar.status_code)
                return res
            if res["approve_result"] != "approved":
                return res

            pp = {
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
            for i in range(8):
                time.sleep(1.5)
                pr = s_stripe.get(f"{STRIPE_API_BASE}/payment_pages/{cs}", params=pp, headers=ph, timeout=8)
                if pr.status_code != 200:
                    continue
                pd = pr.json()
                pmr_url = find_redirect(pd)
                if pmr_url:
                    rg = s_stripe.get(pmr_url, allow_redirects=True, timeout=20, headers={"Referer": "https://pay.openai.com/"})
                    fin = str(getattr(rg, "url", ""))
                    bm = BA_RE.search(fin)
                    if bm:
                        res["ba"] = bm.group(1)
                        res["ba_url"] = fin[:220]
                        res["ba_ok"] = True
                    else:
                        res["err"] = "no ba poll: " + fin[:80]
                    return res
                psa = pd.get("submission_attempt") or {}
                pst = psa.get("state", "") if isinstance(psa, dict) else ""
                if pst == "failed":
                    pe = psa.get("error", {}) if isinstance(psa, dict) else {}
                    res["err"] = "failed:" + str(pe.get("code", "") if isinstance(pe, dict) else "")
                    return res
            res["err"] = "timeout approved but no redirect"
            return res

        # hosted path: confirm with paypal expected type after optional promo
        conf = {
            "eid": str(uuid.uuid4()),
            "expected_payment_method_type": "paypal",
            "key": pk,
            "_stripe_version": STRIPE_INIT_VERSION,
            "init_checksum": ck,
        }
        # include expected amount if known
        if res.get("amt") not in ("", None, "?"):
            conf["expected_amount"] = str(res["amt"])
        cr = s_stripe.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/confirm", data=conf, headers=sh, timeout=30)
        res["steps"]["confirm"] = cr.status_code
        if cr.status_code != 200:
            res["err"] = "hosted confirm " + str(cr.status_code)
            res["body"] = cr.text[:200]
            return res
        body = cr.json()
        redir = find_redirect(body) or ""
        if not redir:
            # poll
            pp = {
                "key": pk,
                "_stripe_version": STRIPE_INIT_VERSION,
            }
            for i in range(8):
                time.sleep(1.2)
                pr = s_stripe.get(f"{STRIPE_API_BASE}/payment_pages/{cs}", params=pp, headers={"Accept": "application/json", "Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/"}, timeout=8)
                if pr.status_code == 200:
                    redir = find_redirect(pr.json())
                    if redir:
                        break
        if not redir:
            res["err"] = "hosted no redirect"
            return res
        rg = s_chk.get(redir, allow_redirects=True, timeout=20, headers={"Referer": "https://pay.openai.com/"})
        fin = str(getattr(rg, "url", ""))
        bm = BA_RE.search(fin)
        if bm:
            res["ba"] = bm.group(1)
            res["ba_url"] = fin[:220]
            res["ba_ok"] = True
            res["approve_result"] = "hosted_redirect"
        else:
            res["err"] = "hosted no ba: " + fin[:100]
            res["ba_url"] = fin[:220]
        return res
    except Exception as e:
        res["err"] = str(e)[:160]
        low = res["err"].lower()
        if any(x in low for x in ("tls", "timeout", "proxy", "connect", "ssl")):
            res["approve_result"] = "network_error"
        return res
    finally:
        for s in (sessions if "sessions" in locals() else [s_chk, s_promo]):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass


def main():
    print(
        f"=== triple extract chk={CHECKOUT_PROXY} stripe={STRIPE_PROXY} approve={APPROVE_PROXY} promo={PROMO_PROXY or '-'} "
        f"bill={BILLING_COUNTRY}/{BILLING_CURRENCY} ui={UI_MODE} promo_when={PROMO_WHEN} "
        f"promo_addr={PROMO_ADDR_COUNTRY} full={DO_FULL} max={MAX_ATTEMPTS} ===",
        flush=True,
    )
    results = []
    for i in range(1, MAX_ATTEMPTS + 1):
        r = try_once(i)
        results.append(r)
        print(
            f"  [{i:02d}] approve={str(r.get('approve_result') or ''):16s} "
            f"ba_ok={str(r.get('ba_ok')):5s} amt={str(r.get('amt') or ''):6s} "
            f"zero={str(r.get('zero')):5s} paypal={str(r.get('paypal')):5s} "
            f"pmt={r.get('pmt')} err={str(r.get('err') or '')[:70]}",
            flush=True,
        )
        if r.get("steps"):
            print(f"       steps={r.get('steps')}", flush=True)
        if r.get("ba_ok") and STOP_ON_FIRST:
            print(f"  BA_URL={r.get('ba_url')}", flush=True)
            break
        time.sleep(0.4)

    print("\n=== SUMMARY ===", flush=True)
    cnt = Counter((r.get("approve_result") or "empty") for r in results)
    for k, v in cnt.most_common():
        ok = sum(1 for r in results if (r.get("approve_result") or "empty") == k and r.get("ba_ok"))
        print(f"  {k}: {v} total, {ok} ba_ok", flush=True)
    ba_ok = sum(1 for r in results if r.get("ba_ok"))
    zero_pp = sum(1 for r in results if r.get("zero") and r.get("paypal"))
    print(f"  overall_ba_ok={ba_ok}/{len(results)} zero_and_paypal={zero_pp}", flush=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  saved={OUT_JSON}", flush=True)
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
