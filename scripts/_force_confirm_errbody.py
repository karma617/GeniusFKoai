import json, os, sys
sys.path.insert(0, ".")
os.environ["PYTHONIOENCODING"]="utf-8"
from scripts.pp_complete_extract import fetch_proxy, make_session, auth_headers, PAYMENT_CHECKOUT_URL, EMAIL
from platforms.chatgpt import stripe_http

def run(region, bill_c, bill_cur):
    s=make_session(fetch_proxy(region))
    pl={"plan_name":"chatgptplusplan","billing_details":{"country":bill_c,"currency":bill_cur},"entry_point":"all_plans_pricing_modal","promo_campaign":{"promo_campaign_id":"plus-1-month-free","is_coupon_from_query_param":False},"checkout_ui_mode":"hosted"}
    r=s.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=35)
    print("checkout", r.status_code)
    data=r.json(); cs=data["checkout_session_id"]; print("cs", cs, "promo", data.get("promo_campaign"), "trial", data.get("one_click_trial_eligible"), "checkout_pmt", data.get("payment_method_types"))
    init=stripe_http.stripe_init(s, cs_id=cs)
    print("init pmt", init.get("payment_method_types"), "amt", stripe_http.extract_expected_amount(init))
    addr={"name":"Taro Yamada","email":EMAIL,"country":bill_c,"state":"Tokyo" if bill_c=="JP" else ("SP" if bill_c=="BR" else "NY"),"city":"Tokyo" if bill_c=="JP" else ("Sao Paulo" if bill_c=="BR" else "New York"),"postal_code":"100-0001" if bill_c=="JP" else ("01310-100" if bill_c=="BR" else "10001"),"line1":"1-1 Chiyoda" if bill_c=="JP" else ("Av Paulista 1000" if bill_c=="BR" else "350 5th Ave"),"line2":""}
    exp,on=stripe_http.extract_confirm_expected_amounts(init, fallback_amount=str(stripe_http.extract_expected_amount(init) or "0"))
    disp=stripe_http.extract_display_amounts(init)
    ret=stripe_http.build_confirm_return_url(init, cs_id=cs, fallback_url=f"https://chatgpt.com/checkout/verify?stripe_session_id={cs}&processor_entity=openai_llc&plan_type=plus")
    ref=stripe_http.build_confirm_referrer_url(init, cs_id=cs, fallback_url=f"https://pay.openai.com/c/pay/{cs}")
    try:
        conf=stripe_http.stripe_confirm_paypal_direct(s, cs_id=cs, init_checksum=str(init.get("init_checksum") or ""), email=EMAIL, address=addr, return_url=ret, expected_amount=exp, expected_amount_on_bca=on, displayed_amounts=disp, referrer=ref)
        print("confirm unexpected ok", list(conf.keys())[:10])
    except Exception as e:
        print("CONFIRM_EXC", str(e)[:800])
    s.close()

for item in [("JP","JP","JPY"),("BR","BR","BRL")]:
    for i in range(3):
        try:
            run(*item); break
        except Exception as e:
            print("retry", item, e)