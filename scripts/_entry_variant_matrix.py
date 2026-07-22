import json, os, sys, time
from pathlib import Path
sys.path.insert(0, ".")
os.environ["PYTHONIOENCODING"]="utf-8"
from scripts.pp_complete_extract import fetch_proxy, make_session, auth_headers, PAYMENT_CHECKOUT_URL, has_paypal
from platforms.chatgpt import stripe_http

entries = [
  "all_plans_pricing_modal",
  "pricing_modal",
  "settings",
  "side_nav",
  "gift_code",
  "plus_upsell",
  "account_payment_method",
]
variants=[]
for region in ["BR","JP"]:
  for entry in entries:
    for promo in [True, False]:
      for ui in ["hosted","custom"]:
        label=f"{region}_{entry}_{'promo' if promo else 'nopromo'}_{ui}"
        res={"label":label,"region":region,"entry":entry,"promo":promo,"ui":ui}
        ok=False
        for a in range(2):
          try:
            s=make_session(fetch_proxy(region))
            pl={"plan_name":"chatgptplusplan","billing_details":{"country":region,"currency":"BRL" if region=="BR" else "JPY"},"entry_point":entry,"checkout_ui_mode":ui}
            if promo:
              pl["promo_campaign"]={"promo_campaign_id":"plus-1-month-free","is_coupon_from_query_param":False}
            r=s.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=30)
            res["http"]=r.status_code
            if r.status_code!=200:
              res["err"]=r.text[:120]
              s.close(); continue
            data=r.json(); cs=data.get("checkout_session_id")
            res["trial"]=data.get("one_click_trial_eligible"); res["promo_resp"]=data.get("promo_campaign")
            init=stripe_http.stripe_init(s, cs_id=cs)
            pmt=init.get("payment_method_types") or []
            amt=stripe_http.extract_expected_amount(init)
            res.update({"pmt":pmt,"amt":amt,"paypal":has_paypal(pmt),"zero":str(amt)=="0"})
            s.close(); ok=True; break
          except Exception as e:
            res["err"]=str(e)[:140]
            time.sleep(0.4)
        variants.append(res)
        print(json.dumps({k:res.get(k) for k in ["label","http","amt","zero","paypal","pmt","err"]}, ensure_ascii=False), flush=True)
        if res.get("zero") and res.get("paypal"):
          print("HIT", label, flush=True)
Path("scripts/pp_entry_variant_matrix.json").write_text(json.dumps(variants, ensure_ascii=False, indent=2), encoding="utf-8")
hits=[v for v in variants if v.get("zero") and v.get("paypal")]
pp=[v for v in variants if v.get("paypal")]
print("saved", len(variants), "hits", len(hits), "paypal_any", len(pp))