import json, os, sys, time, uuid
from pathlib import Path
sys.path.insert(0, ".")
os.environ["PYTHONIOENCODING"] = "utf-8"
from scripts.pp_complete_extract import fetch_proxy, make_session, auth_headers, PAYMENT_CHECKOUT_URL, has_paypal
from platforms.chatgpt import stripe_http

TOKEN_INFO = json.load(open("scripts/_active_token.json", encoding="utf-8"))

def probe(label, checkout_region, bill_country, bill_currency, ui_mode, with_promo=True, entry="all_plans_pricing_modal"):
    res = {"label": label}
    try:
        s = make_session(fetch_proxy(checkout_region))
        pl = {
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": bill_country, "currency": bill_currency},
            "entry_point": entry,
            "checkout_ui_mode": ui_mode,
        }
        if with_promo:
            pl["promo_campaign"] = {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False}
        r = s.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=35)
        res["checkout_http"] = r.status_code
        if r.status_code != 200:
            res["err"] = r.text[:200]
            print(json.dumps(res, ensure_ascii=False), flush=True)
            return res
        data = r.json()
        keys_keep = [
            "checkout_session_id","processor_entity","currency","country",
            "one_click_trial_eligible","promo_campaign","checkout_ui_mode",
            "url","stripe_hosted_url","publishable_key"
        ]
        for k in keys_keep:
            if k in data:
                res[k] = data.get(k)
        # extra unknown-ish fields
        res["checkout_keys"] = sorted(list(data.keys()))[:40]
        cs = data.get("checkout_session_id")
        init = stripe_http.stripe_init(s, cs_id=cs)
        pmt = init.get("payment_method_types") or []
        amt = stripe_http.extract_expected_amount(init)
        res["pmt"] = pmt
        res["amt"] = amt
        res["paypal"] = has_paypal(pmt)
        res["zero"] = str(amt) == "0"
        res["amts"] = stripe_http.extract_display_amounts(init)
        res["currency_init"] = init.get("currency")
        res["ui_mode_init"] = init.get("ui_mode")
        # discount markers
        inv = init.get("invoice") or init.get("total_summary") or {}
        if isinstance(inv, dict):
            for k in ["amount_due","total","subtotal","total_discount_amounts","discounts"]:
                if k in inv: res[f"inv_{k}"] = inv.get(k)
        s.close()
    except Exception as e:
        res["err"] = str(e)[:200]
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return res

jobs = [
    ("US_custom_promo", "US", "US", "USD", "custom", True),
    ("US_hosted_promo", "US", "US", "USD", "hosted", True),
    ("BR_custom_promo_US_bill", "BR", "US", "USD", "custom", True),
    ("BR_hosted_promo_US_bill", "BR", "US", "USD", "hosted", True),
    ("BR_custom_no_promo_US_bill", "BR", "US", "USD", "custom", False),
    ("BR_custom_promo_BR_bill", "BR", "BR", "BRL", "custom", True),
    ("JP_custom_promo_US_bill", "JP", "US", "USD", "custom", True),
    ("JP_hosted_promo_US_bill", "JP", "US", "USD", "hosted", True),
    ("JP_custom_no_promo_US_bill", "JP", "US", "USD", "custom", False),
    ("JP_custom_promo_JP_bill", "JP", "JP", "JPY", "custom", True),
    ("US_custom_no_promo", "US", "US", "USD", "custom", False),
]
results = []
for j in jobs:
    for attempt in range(3):
        r = probe(*j)
        if r.get("pmt") is not None or attempt == 2:
            results.append(r)
            break
        time.sleep(0.8)
Path("scripts/pp_create_field_compare.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print("saved", len(results))