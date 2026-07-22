# -*- coding: utf-8 -*-
"""Probe whether Chatgpt-Account-Id / accounts/check / payload shape restore paypal on BR/JP create."""
from __future__ import annotations
import json, os, sys, time, uuid, base64
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PYTHONIOENCODING"] = "utf-8"
from scripts.pp_complete_extract import fetch_proxy, make_session, has_paypal, PAYMENT_CHECKOUT_URL, EMAIL, TOKEN, COOKIE, STRIPE_PK
from platforms.chatgpt import stripe_http

def jwt_claims(tok: str) -> dict:
    p = tok.split(".")[1]
    p += "=" * ((4 - len(p) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(p.encode()))

claims = jwt_claims(TOKEN)
auth = claims.get("https://api.openai.com/auth") or {}
ACCOUNT_ID = auth.get("chatgpt_account_id") or ""
USER_ID = auth.get("chatgpt_user_id") or ""
print("account_id", ACCOUNT_ID, "plan", auth.get("chatgpt_plan_type"), "signup", auth.get("is_signup"), flush=True)

def headers(with_account=True, device_id=None, extra=None):
    h = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    if COOKIE:
        h["cookie"] = COOKIE
    if with_account and ACCOUNT_ID:
        h["Chatgpt-Account-Id"] = ACCOUNT_ID
    if device_id:
        h["oai-device-id"] = device_id
    if extra:
        h.update(extra)
    return h

results = []

def run_label(label, region, bill_c, bill_cur, *, with_account=True, promo=True, ui="hosted", entry="all_plans_pricing_modal", cancel=True, device=False, stripe_region=None, do_tax=False, tax_c=None, force_confirm=False):
    res = {"label": label, "region": region, "bill": f"{bill_c}/{bill_cur}", "with_account": with_account, "promo": promo, "ui": ui}
    for attempt in range(3):
        try:
            s = make_session(fetch_proxy(region))
            did = str(uuid.uuid4()) if device else None
            # accounts/check first
            try:
                cr = s.get("https://chatgpt.com/backend-api/accounts/check", headers=headers(with_account=with_account, device_id=did), params={"timezone_offset_min": "-480"}, timeout=25)
                res["accounts_check"] = cr.status_code
                if cr.status_code == 200:
                    ad = cr.json() if cr.headers.get("content-type","").startswith("application/json") else {}
                    # keep compact eligibility-ish fields
                    if isinstance(ad, dict):
                        accs = ad.get("accounts") or ad
                        res["accounts_keys"] = list(ad.keys())[:20]
                        # try dig plan / trial
                        blob = json.dumps(ad)[:4000]
                        for key in ["one_click_trial", "trial", "plus", "eligible", "payment", "subscription"]:
                            if key in blob.lower():
                                res.setdefault("accounts_hints", []).append(key)
                        res["accounts_snip"] = blob[:500]
            except Exception as e:
                res["accounts_check_err"] = str(e)[:120]

            pl = {
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": bill_c, "currency": bill_cur},
                "entry_point": entry,
                "checkout_ui_mode": ui,
            }
            if cancel:
                pl["cancel_url"] = "https://chatgpt.com/#pricing"
            if promo:
                pl["promo_campaign"] = {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False}

            r = s.post(PAYMENT_CHECKOUT_URL, headers=headers(with_account=with_account, device_id=did), json=pl, timeout=35)
            res["checkout"] = r.status_code
            if r.status_code != 200:
                res["err"] = r.text[:180]
                s.close()
                if attempt < 2 and ("TLS" in str(res.get("err")) or "curl" in str(res.get("err"))):
                    time.sleep(0.5); continue
                break
            data = r.json()
            cs = data.get("checkout_session_id") or ""
            res.update({
                "cs": cs[:28],
                "trial": data.get("one_click_trial_eligible"),
                "promo_resp": data.get("promo_campaign"),
                "checkout_pmt": data.get("payment_method_types"),
                "ent": data.get("processor_entity"),
                "requires_manual_approval": data.get("requires_manual_approval"),
            })
            # stripe on same or other region
            s_st = s
            if stripe_region and stripe_region != region:
                s_st = make_session(fetch_proxy(stripe_region))
            init = stripe_http.stripe_init(s_st, cs_id=cs)
            pmt = init.get("payment_method_types") or []
            amt = stripe_http.extract_expected_amount(init)
            res.update({"pmt": pmt, "amt": amt, "paypal": has_paypal(pmt), "zero": str(amt)=="0", "amts": stripe_http.extract_display_amounts(init)})
            latest = init
            if do_tax:
                addr_country = tax_c or bill_c
                addrs = {
                    "US": {"name":"John Smith","email":EMAIL,"country":"US","state":"NY","city":"New York","postal_code":"10001","line1":"350 5th Ave","line2":""},
                    "BR": {"name":"Joao Silva","email":EMAIL,"country":"BR","state":"SP","city":"Sao Paulo","postal_code":"01310-100","line1":"Av Paulista 1000","line2":""},
                    "JP": {"name":"Taro Yamada","email":EMAIL,"country":"JP","state":"Tokyo","city":"Tokyo","postal_code":"100-0001","line1":"1-1 Chiyoda","line2":""},
                }
                tax = stripe_http.stripe_update_tax_region(s_st, cs_id=cs, address=addrs.get(addr_country, addrs["US"]))
                latest = stripe_http.merge_checkout_payload(latest, tax) if hasattr(stripe_http, "merge_checkout_payload") else tax
                pmt = latest.get("payment_method_types") or pmt
                amt = stripe_http.extract_expected_amount(latest)
                res.update({"pmt_tax": pmt, "amt_tax": amt, "paypal_tax": has_paypal(pmt), "zero_tax": str(amt)=="0"})
            if force_confirm and has_paypal(pmt):
                # only if paypal present - full path handled elsewhere
                res["note"] = "paypal present"
            if s_st is not s:
                s_st.close()
            s.close()
            break
        except Exception as e:
            res["err"] = str(e)[:200]
            time.sleep(0.5)
    results.append(res)
    print(json.dumps({k:res.get(k) for k in ["label","checkout","amt","zero","paypal","pmt","trial","promo_resp","err","amt_tax","paypal_tax","pmt_tax"]}, ensure_ascii=False), flush=True)
    return res

jobs = [
    # account header toggles
    dict(label="BR_acct_on_promo", region="BR", bill_c="BR", bill_cur="BRL", with_account=True, promo=True),
    dict(label="BR_acct_off_promo", region="BR", bill_c="BR", bill_cur="BRL", with_account=False, promo=True),
    dict(label="JP_acct_on_promo", region="JP", bill_c="JP", bill_cur="JPY", with_account=True, promo=True),
    dict(label="JP_acct_off_promo", region="JP", bill_c="JP", bill_cur="JPY", with_account=False, promo=True),
    # device id
    dict(label="BR_device_promo", region="BR", bill_c="BR", bill_cur="BRL", with_account=True, promo=True, device=True),
    dict(label="JP_device_promo", region="JP", bill_c="JP", bill_cur="JPY", with_account=True, promo=True, device=True),
    # payload shape like generate_plus_link
    dict(label="BR_cancel_hosted", region="BR", bill_c="BR", bill_cur="BRL", with_account=True, promo=True, ui="hosted", cancel=True, entry="all_plans_pricing_modal"),
    dict(label="JP_cancel_hosted", region="JP", bill_c="JP", bill_cur="JPY", with_account=True, promo=True, ui="hosted", cancel=True),
    # dual proxy create/stripe
    dict(label="BR_create_JP_stripe", region="BR", bill_c="BR", bill_cur="BRL", with_account=True, promo=True, stripe_region="JP", do_tax=True, tax_c="BR"),
    dict(label="JP_create_BR_stripe", region="JP", bill_c="JP", bill_cur="JPY", with_account=True, promo=True, stripe_region="BR", do_tax=True, tax_c="JP"),
    dict(label="BR_create_US_stripe", region="BR", bill_c="US", bill_cur="USD", with_account=True, promo=True, stripe_region="US"),
    dict(label="JP_create_US_stripe", region="JP", bill_c="US", bill_cur="USD", with_account=True, promo=True, stripe_region="US"),
    # US controls with account header
    dict(label="US_acct_on_promo", region="US", bill_c="US", bill_cur="USD", with_account=True, promo=True),
    dict(label="US_acct_off_promo", region="US", bill_c="US", bill_cur="USD", with_account=False, promo=True),
    # IE which previously had paypal via US proxy, try BR/JP residency create
    dict(label="US_IE_EUR", region="US", bill_c="IE", bill_cur="EUR", with_account=True, promo=True),
    dict(label="BR_IE_EUR", region="BR", bill_c="IE", bill_cur="EUR", with_account=True, promo=True),
    dict(label="JP_IE_EUR", region="JP", bill_c="IE", bill_cur="EUR", with_account=True, promo=True),
]

for j in jobs:
    run_label(**j)
    time.sleep(0.3)

Path("scripts/pp_account_header_matrix.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
# summary
print("\n=== SUMMARY ===", flush=True)
for r in results:
    print(f"{r.get('label')}: amt={r.get('amt')} zero={r.get('zero')} paypal={r.get('paypal')} pmt={r.get('pmt')} err={str(r.get('err') or '')[:50]}", flush=True)
print("paypal_any", sum(1 for r in results if r.get("paypal")), "zero_paypal", sum(1 for r in results if r.get("zero") and r.get("paypal")), flush=True)