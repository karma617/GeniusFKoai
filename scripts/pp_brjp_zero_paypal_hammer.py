# -*- coding: utf-8 -*-
"""BR/JP multi-IP hammer for zero+paypal BA recovery.

Matches oaipay production gate:
  create -> init -> require (zero_amount AND paypal) -> confirm -> approve/poll -> BA

Uses Kookeey dynamic IPs. Cycles BR and JP heavily.
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
from scripts.pp_complete_extract import (
    fetch_proxy,
    make_session,
    auth_headers,
    has_paypal,
    find_redirect,
    follow_ba,
    PAYMENT_CHECKOUT_URL,
    STRIPE_PK,
    STRIPE_INIT_VERSION,
    EMAIL,
    TOKEN,
)

MAX_ATTEMPTS = int(os.environ.get("BA_MAX_ATTEMPTS", "40"))
STOP_ON_FIRST = os.environ.get("BA_STOP_ON_FIRST", "1") not in {"0", "false", "False"}
REGIONS = [x.strip().upper() for x in os.environ.get("BA_HAMMER_REGIONS", "BR,JP").split(",") if x.strip()]
UI_MODE = os.environ.get("BA_UI_MODE", "hosted").lower()
APPROVE_REGION = os.environ.get("BA_APPROVE_PROXY", "US").upper()
OUT_JSON = os.environ.get("BA_OUT_JSON", "scripts/pp_brjp_zero_paypal_hammer.json")
OUT_TXT = os.environ.get("BA_OUT_TXT", "scripts/pp_brjp_zero_paypal_hammer.txt")

BILL_BY_REGION = {
    "BR": ("BR", "BRL"),
    "JP": ("JP", "JPY"),
    "US": ("US", "USD"),
    "TR": ("US", "USD"),
    "VN": ("US", "USD"),
    "TH": ("US", "USD"),
    "IE": ("IE", "EUR"),
}

ADDRS = {
    "US": {"name": "John Smith", "email": EMAIL, "country": "US", "state": "NY", "city": "New York", "postal_code": "10001", "line1": "350 5th Ave", "line2": ""},
    "BR": {"name": "Joao Silva", "email": EMAIL, "country": "BR", "state": "SP", "city": "Sao Paulo", "postal_code": "01310-100", "line1": "Av Paulista 1000", "line2": ""},
    "JP": {"name": "Taro Yamada", "email": EMAIL, "country": "JP", "state": "Tokyo", "city": "Tokyo", "postal_code": "100-0001", "line1": "1-1 Chiyoda", "line2": ""},
    "IE": {"name": "Sean Murphy", "email": EMAIL, "country": "IE", "state": "", "city": "Dublin", "postal_code": "D01 F5P2", "line1": "1 Grafton Street", "line2": ""},
}


def try_once(n: int, region: str) -> dict:
    bill_c, bill_cur = BILL_BY_REGION.get(region, ("US", "USD"))
    # alternate bill mode: even attempts local bill, odd US bill
    if n % 2 == 0:
        bill_c, bill_cur = ("US", "USD")
        tax_c = "US"
    else:
        tax_c = region if region in ADDRS else "US"

    res = {
        "n": n,
        "region": region,
        "bill": f"{bill_c}/{bill_cur}",
        "tax": tax_c,
        "ui": UI_MODE,
        "proxy_ip": "",
        "amt": "",
        "zero": False,
        "paypal": False,
        "pmt": None,
        "promo": None,
        "trial": None,
        "ba_ok": False,
        "ba": "",
        "ba_url": "",
        "approve": "",
        "err": "",
        "steps": {},
    }
    sessions = []
    try:
        p = fetch_proxy(region)
        res["proxy_ip"] = p.split("@")[-1]
        s = make_session(p)
        sessions.append(s)

        # approve session: prefer US, else same
        if APPROVE_REGION == region:
            s_ap = s
        else:
            p_ap = fetch_proxy(APPROVE_REGION)
            s_ap = make_session(p_ap)
            sessions.append(s_ap)
            res["steps"]["proxy_approve"] = p_ap.split("@")[-1]

        pl = {
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": bill_c, "currency": bill_cur},
            "entry_point": "all_plans_pricing_modal",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
            "checkout_ui_mode": UI_MODE,
        }
        r = s.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=35)
        res["steps"]["checkout"] = r.status_code
        if r.status_code != 200:
            res["err"] = f"checkout {r.status_code} {r.text[:100]}"
            return res
        data = r.json()
        cs = data.get("checkout_session_id") or ""
        ent = data.get("processor_entity") or "openai_llc"
        pk = data.get("publishable_key") or STRIPE_PK
        res["cs"] = cs
        res["trial"] = data.get("one_click_trial_eligible")
        res["promo"] = data.get("promo_campaign")
        res["checkout_pmt"] = data.get("payment_method_types")
        if not cs:
            res["err"] = "no cs"
            return res

        init = stripe_http.stripe_init(s, cs_id=cs)
        res["steps"]["init"] = 200
        pmt = init.get("payment_method_types") or []
        amt = stripe_http.extract_expected_amount(init)
        res["pmt"] = pmt
        res["amt"] = amt
        res["paypal"] = has_paypal(pmt)
        res["zero"] = str(amt) == "0"
        res["amts"] = stripe_http.extract_display_amounts(init)
        latest = dict(init)
        ck = str(init.get("init_checksum") or "")
        cid = str(init.get("config_id") or "")

        # optional tax with same proxy (local or US)
        tax_addr = dict(ADDRS.get(tax_c, ADDRS["US"]))
        tax_addr["email"] = EMAIL
        try:
            tax = stripe_http.stripe_update_tax_region(s, cs_id=cs, address=tax_addr)
            res["steps"]["tax"] = 200
            latest = stripe_http.merge_checkout_payload(latest, tax) if hasattr(stripe_http, "merge_checkout_payload") else dict(tax)
            ck = str(latest.get("init_checksum") or ck)
            pmt = latest.get("payment_method_types") or pmt
            amt = stripe_http.extract_expected_amount(latest)
            res["pmt"] = pmt
            res["amt"] = amt
            res["paypal"] = has_paypal(pmt)
            res["zero"] = str(amt) == "0"
            res["amts_tax"] = stripe_http.extract_display_amounts(latest)
        except Exception as e:
            res["steps"]["tax_err"] = str(e)[:120]

        # production gate
        if not (res["zero"] and res["paypal"]):
            res["err"] = f"gate_fail zero={res['zero']} paypal={res['paypal']} pmt={res['pmt']}"
            return res

        # full confirm path if gate passes
        expected_amount, expected_on_bca = stripe_http.extract_confirm_expected_amounts(latest, fallback_amount=str(amt or "0"))
        displayed = stripe_http.extract_display_amounts(latest)
        success_url = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs}&processor_entity={ent}&plan_type=plus"
        return_url = stripe_http.build_confirm_return_url(latest, cs_id=cs, fallback_url=success_url)
        referrer = stripe_http.build_confirm_referrer_url(latest, cs_id=cs, fallback_url=f"https://pay.openai.com/c/pay/{cs}")

        conf = stripe_http.stripe_confirm_paypal_direct(
            s,
            cs_id=cs,
            init_checksum=ck,
            email=EMAIL,
            address=tax_addr,
            return_url=return_url,
            expected_amount=expected_amount,
            expected_amount_on_bca=expected_on_bca,
            displayed_amounts=displayed,
            referrer=referrer,
        )
        res["steps"]["confirm"] = 200
        url, _ = stripe_http.extract_paypal_redirect_url(conf)
        if url:
            ok, ba, ba_url = follow_ba(s, url)
            if ok:
                res["ba_ok"] = True
                res["ba"] = ba
                res["ba_url"] = ba_url
                res["approve"] = "confirm_redirect"
                return res

        # approve + poll
        pt = "/backend-api/payments/checkout/approve"
        ar = s_ap.post(
            "https://chatgpt.com" + pt,
            headers=auth_headers({"Referer": f"https://chatgpt.com/checkout/{ent}/{cs}", "x-openai-target-path": pt, "x-openai-target-route": pt}),
            json={"checkout_session_id": cs, "processor_entity": ent},
            timeout=30,
        )
        res["steps"]["approve_http"] = ar.status_code
        try:
            ab = ar.json()
            res["approve"] = ab.get("result", "unknown")
        except Exception:
            res["approve"] = f"http_{ar.status_code}"

        poll_params = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Shanghai",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": pk,
            "_stripe_version": STRIPE_INIT_VERSION,
        }
        ph = {"Origin": "https://pay.openai.com", "Referer": "https://pay.openai.com/", "Accept": "application/json"}
        for _ in range(12):
            time.sleep(1.2)
            pr = s.get(f"https://api.stripe.com/v1/payment_pages/{cs}", params=poll_params, headers=ph, timeout=10)
            if pr.status_code != 200:
                continue
            poll = pr.json()
            redir = find_redirect(poll)
            if redir:
                ok, ba, ba_url = follow_ba(s, redir)
                if ok:
                    res["ba_ok"] = True
                    res["ba"] = ba
                    res["ba_url"] = ba_url
                    return res
            psa = poll.get("submission_attempt") or {}
            if isinstance(psa, dict) and psa.get("state") == "failed":
                pe = psa.get("error") or {}
                res["err"] = "failed:" + str(pe.get("code") if isinstance(pe, dict) else pe)
                return res
        if not res.get("err"):
            res["err"] = "timeout no redirect"
        return res
    except Exception as e:
        res["err"] = str(e)[:200]
        low = res["err"].lower()
        if any(x in low for x in ("tls", "timeout", "proxy", "connect", "ssl", "curl", "407")):
            res["approve"] = "network_error"
        return res
    finally:
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass


def main():
    lines = []
    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    log(f"=== BR/JP zero+paypal hammer regions={REGIONS} ui={UI_MODE} approve={APPROVE_REGION} max={MAX_ATTEMPTS} ===")
    results = []
    stats = Counter()
    for i in range(1, MAX_ATTEMPTS + 1):
        region = REGIONS[(i - 1) % len(REGIONS)]
        r = try_once(i, region)
        results.append(r)
        tag = "HIT" if (r.get("zero") and r.get("paypal")) else ("BA" if r.get("ba_ok") else "miss")
        if r.get("ba_ok"):
            stats["ba_ok"] += 1
        if r.get("zero") and r.get("paypal"):
            stats["zero_paypal"] += 1
        if r.get("zero") and not r.get("paypal"):
            stats["zero_no_paypal"] += 1
        if r.get("paypal") and not r.get("zero"):
            stats["paypal_no_zero"] += 1
        if r.get("approve") == "network_error" or "TLS" in str(r.get("err") or "") or "curl" in str(r.get("err") or ""):
            stats["network"] += 1
        log(
            f"  [{i:02d}/{MAX_ATTEMPTS}] {region:2s} bill={r.get('bill')} amt={str(r.get('amt') or ''):6s} "
            f"zero={r.get('zero')} paypal={r.get('paypal')} pmt={r.get('pmt')} "
            f"ba={r.get('ba') or '-'} tag={tag} err={str(r.get('err') or '')[:70]}"
        )
        if r.get("ba_ok") and STOP_ON_FIRST:
            log(f"  BA_URL={r.get('ba_url')}")
            break
        if r.get("zero") and r.get("paypal") and not r.get("ba_ok"):
            # keep going but this is the rare gate hit
            log(f"  !!! GATE HIT without BA: {json.dumps({k:r.get(k) for k in ['cs','amt','pmt','approve','err']}, ensure_ascii=False)}")
        time.sleep(0.25)

    Path(OUT_JSON).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "attempts": len(results),
        "stats": dict(stats),
        "ba_ok": sum(1 for x in results if x.get("ba_ok")),
        "zero_paypal_hits": sum(1 for x in results if x.get("zero") and x.get("paypal")),
        "sample_gate_fail": Counter(str(x.get("err") or "")[:40] for x in results).most_common(8),
    }
    Path(OUT_JSON.replace(".json", "_summary.json")).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log("\n=== SUMMARY ===")
    log(json.dumps(summary, ensure_ascii=False, indent=2))
    Path(OUT_TXT).write_text("\n".join(lines), encoding="utf-8")
    log(f"saved {OUT_JSON}")


if __name__ == "__main__":
    main()