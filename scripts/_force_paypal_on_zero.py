import json, os, sys, time, uuid
from pathlib import Path
sys.path.insert(0, ".")
os.environ["PYTHONIOENCODING"]="utf-8"
from scripts.pp_complete_extract import fetch_proxy, make_session, auth_headers, PAYMENT_CHECKOUT_URL, has_paypal, find_redirect, follow_ba, EMAIL, STRIPE_INIT_VERSION, STRIPE_PK
from platforms.chatgpt import stripe_http

results=[]
for region, bill_c, bill_cur in [("BR","BR","BRL"),("BR","US","USD"),("JP","JP","JPY"),("JP","US","USD")]:
  for attempt in range(2):
    res={"label":f"{region}_{bill_c}_{bill_cur}_force_pp","attempt":attempt+1}
    try:
      s=make_session(fetch_proxy(region))
      s_us=make_session(fetch_proxy("US"))
      pl={"plan_name":"chatgptplusplan","billing_details":{"country":bill_c,"currency":bill_cur},"entry_point":"all_plans_pricing_modal","promo_campaign":{"promo_campaign_id":"plus-1-month-free","is_coupon_from_query_param":False},"checkout_ui_mode":"hosted"}
      r=s.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=35)
      res["checkout"]=r.status_code
      if r.status_code!=200:
        res["err"]=r.text[:120]; results.append(res); continue
      data=r.json(); cs=data.get("checkout_session_id"); ent=data.get("processor_entity") or "openai_llc"
      init=stripe_http.stripe_init(s, cs_id=cs)
      pmt=init.get("payment_method_types") or []
      amt=stripe_http.extract_expected_amount(init)
      res.update({"pmt":pmt,"amt":amt,"paypal":has_paypal(pmt),"zero":str(amt)=="0","promo":data.get("promo_campaign")})
      # force paypal confirm even if not in pmt
      addr={"name":"Joao Silva","email":EMAIL,"country":bill_c if bill_c in ("BR","JP","US") else "US","state":"SP" if bill_c=="BR" else ("Tokyo" if bill_c=="JP" else "NY"),"city":"Sao Paulo" if bill_c=="BR" else ("Tokyo" if bill_c=="JP" else "New York"),"postal_code":"01310-100" if bill_c=="BR" else ("100-0001" if bill_c=="JP" else "10001"),"line1":"Av Paulista 1000" if bill_c=="BR" else ("1-1 Chiyoda" if bill_c=="JP" else "350 5th Ave"),"line2":""}
      try:
        tax=stripe_http.stripe_update_tax_region(s, cs_id=cs, address=addr)
        latest=stripe_http.merge_checkout_payload(init, tax)
      except Exception as e:
        latest=init; res["tax_err"]=str(e)[:80]
      exp, on_bca = stripe_http.extract_confirm_expected_amounts(latest, fallback_amount=str(stripe_http.extract_expected_amount(latest) or "0"))
      disp=stripe_http.extract_display_amounts(latest)
      success=f"https://chatgpt.com/checkout/verify?stripe_session_id={cs}&processor_entity={ent}&plan_type=plus"
      ret=stripe_http.build_confirm_return_url(latest, cs_id=cs, fallback_url=success)
      ref=stripe_http.build_confirm_referrer_url(latest, cs_id=cs, fallback_url=f"https://pay.openai.com/c/pay/{cs}")
      try:
        conf=stripe_http.stripe_confirm_paypal_direct(s, cs_id=cs, init_checksum=str(latest.get("init_checksum") or ""), email=EMAIL, address=addr, return_url=ret, expected_amount=exp, expected_amount_on_bca=on_bca, displayed_amounts=disp, referrer=ref)
        res["confirm_ok"]=True
        res["confirm_keys"]=list(conf.keys())[:15]
        url,_=stripe_http.extract_paypal_redirect_url(conf)
        res["redirect"]=bool(url)
        sa=(conf.get("submission_attempt") or {})
        res["confirm_sa"]=sa.get("state") if isinstance(sa,dict) else None
        if isinstance(sa,dict) and sa.get("error"): res["confirm_err"]=sa.get("error")
      except Exception as e:
        res["confirm_ok"]=False; res["confirm_exc"]=str(e)[:180]
      # also try force create payment_method paypal
      try:
        pm=stripe_http.stripe_create_paypal_payment_method(s, cs_id=cs, email=EMAIL, address=addr)
        res["pm_id"]=pm.get("id") if isinstance(pm,dict) else str(pm)[:40]
      except Exception as e:
        res["pm_exc"]=str(e)[:160]
      s.close(); s_us.close()
    except Exception as e:
      res["err"]=str(e)[:180]
    print(json.dumps(res, ensure_ascii=False), flush=True)
    results.append(res)
    if res.get("redirect"):
      break
Path("scripts/pp_force_paypal_on_zero.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print("saved", len(results))