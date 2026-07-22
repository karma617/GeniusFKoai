import json, os, sys, time
from pathlib import Path
sys.path.insert(0, ".")
os.environ["PYTHONIOENCODING"] = "utf-8"
from scripts.pp_complete_extract import fetch_proxy, make_session, auth_headers, PAYMENT_CHECKOUT_URL, has_paypal
from platforms.chatgpt import stripe_http

def one(label, region, bill_c, bill_cur, promo=True):
    res={"label":label,"region":region,"bill":f"{bill_c}/{bill_cur}"}
    try:
        s=make_session(fetch_proxy(region))
        pl={"plan_name":"chatgptplusplan","billing_details":{"country":bill_c,"currency":bill_cur},"entry_point":"all_plans_pricing_modal","checkout_ui_mode":"custom"}
        if promo:
            pl["promo_campaign"]={"promo_campaign_id":"plus-1-month-free","is_coupon_from_query_param":False}
        r=s.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=35)
        res["http"]=r.status_code
        if r.status_code!=200:
            res["err"]=r.text[:160]; print(json.dumps(res,ensure_ascii=False),flush=True); return res
        data=r.json(); cs=data.get("checkout_session_id")
        res["trial"]=data.get("one_click_trial_eligible"); res["promo"]=data.get("promo_campaign")
        init=stripe_http.stripe_init(s, cs_id=cs)
        pmt=init.get("payment_method_types") or []
        amt=stripe_http.extract_expected_amount(init)
        res.update({"pmt":pmt,"amt":amt,"paypal":has_paypal(pmt),"zero":str(amt)=="0","amts":stripe_http.extract_display_amounts(init)})
        s.close()
    except Exception as e:
        res["err"]=str(e)[:180]
    print(json.dumps(res,ensure_ascii=False),flush=True)
    return res

jobs=[
 ("US_bill_BR","US","BR","BRL",True),
 ("US_bill_JP","US","JP","JPY",True),
 ("US_bill_TR","US","TR","TRY",True),
 ("TR_bill_US","TR","US","USD",True),
 ("TR_bill_TR","TR","TR","TRY",True),
 ("VN_bill_US","VN","US","USD",True),
 ("TH_bill_US","TH","US","USD",True),
 ("IE_bill_US","IE","US","USD",True),
 ("BR_bill_US_nopromo","BR","US","USD",False),
 ("JP_bill_US_nopromo","JP","US","USD",False),
]
out=[]
for j in jobs:
  for a in range(3):
    r=one(*j)
    if r.get("pmt") is not None or a==2:
      out.append(r); break
    time.sleep(0.6)
Path("scripts/pp_create_geo_matrix.json").write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print("saved",len(out))