# init-only matrix for dual-proxy promo effect
import json, os, sys, time, uuid
sys.path.insert(0, '.')
os.environ['PYTHONIOENCODING']='utf-8'
from scripts.pp_dual_proxy_extract import (
    fetch_proxy, make_session, auth_headers, payment_pages_update, extract_amounts, pick_amt,
    PAYMENT_CHECKOUT_URL, STRIPE_API_BASE, STRIPE_PK, STRIPE_INIT_VERSION, ADDRS, sh
)

COMBOS = [
    # checkout_proxy, promo_proxy, bill_country, currency, promo_addr, ui
    ('US','TR','US','USD','US','custom'),
    ('US','TR','US','USD','TR','custom'),
    ('US','','US','USD','US','custom'),
    ('BR','JP','BR','BRL','JP','custom'),
    ('JP','BR','JP','JPY','BR','custom'),
    ('BR','JP','US','USD','JP','custom'),
    ('JP','','JP','JPY','JP','custom'),
    ('BR','','BR','BRL','BR','custom'),
    ('US','TR','US','USD','US','hosted'),
]

rows=[]
for chk, promo, bill, cur, padd, ui in COMBOS:
    row={'checkout':chk,'promo':promo or '-','bill':f'{bill}/{cur}','promo_addr':padd,'ui':ui}
    try:
        s1=make_session(fetch_proxy(chk))
        s2=make_session(fetch_proxy(promo)) if promo else None
        pl={
            'plan_name':'chatgptplusplan',
            'billing_details':{'country':bill,'currency':cur},
            'entry_point':'all_plans_pricing_modal',
            'promo_campaign':{'promo_campaign_id':'plus-1-month-free','is_coupon_from_query_param':False},
            'checkout_ui_mode':ui,
        }
        r=s1.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=30)
        row['checkout_http']=r.status_code
        if r.status_code!=200:
            row['err']=r.text[:120]; rows.append(row); print(row, flush=True); continue
        data=r.json(); cs=data.get('checkout_session_id') or ''; pk=data.get('publishable_key') or STRIPE_PK
        row['trial']=data.get('one_click_trial_eligible'); row['promo_campaign']=data.get('promo_campaign')
        ib={
            'browser_locale':'en-US','browser_timezone':'Asia/Shanghai',
            'elements_session_client[client_betas][0]':'custom_checkout_server_updates_1',
            'elements_session_client[client_betas][1]':'custom_checkout_manual_approval_1',
            'elements_session_client[elements_init_source]':'custom_checkout' if ui=='custom' else 'checkout',
            'elements_session_client[referrer_host]':'chatgpt.com',
            'elements_session_client[stripe_js_id]':str(uuid.uuid4()),
            'elements_session_client[locale]':'en',
            'elements_session_client[is_aggregation_expected]':'false',
            'elements_options_client[saved_payment_method][enable_save]':'never',
            'elements_options_client[saved_payment_method][enable_redisplay]':'never',
            'key':pk,'_stripe_version':STRIPE_INIT_VERSION,
        }
        ir=s1.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        row['init']=ir.status_code
        if ir.status_code!=200:
            row['err']=ir.text[:120]; rows.append(row); print(row, flush=True); continue
        init=ir.json(); pmt=init.get('payment_method_types') or []; amts=extract_amounts(init)
        amt=pick_amt(amts,'?'); row['amt']=amt; row['pmt']=pmt; row['paypal']='paypal' in [str(x).lower() for x in pmt]; row['zero']=str(amt)=='0'
        if s2 is not None:
            addr=ADDRS.get(padd, ADDRS['US'])
            pr=payment_pages_update(s2, cs, pk, addr)
            row['promo_http']=pr.status_code
            if pr.status_code==200:
                pd=pr.json(); pmt2=pd.get('payment_method_types') or pmt; am2=extract_amounts(pd); a2=pick_amt(am2,amt)
                row['amt2']=a2; row['pmt2']=pmt2; row['paypal2']='paypal' in [str(x).lower() for x in pmt2] if isinstance(pmt2,list) else False; row['zero2']=str(a2)=='0'
            else:
                row['promo_body']=pr.text[:120]
        # also tax on checkout pool with billing addr
        tr=payment_pages_update(s1, cs, pk, ADDRS.get(bill, ADDRS['US']))
        row['tax']=tr.status_code
        if tr.status_code==200:
            td=tr.json(); am3=extract_amounts(td); a3=pick_amt(am3, row.get('amt2', amt))
            pmt3=td.get('payment_method_types') or row.get('pmt2', pmt)
            row['amt_tax']=a3; row['pmt_tax']=pmt3; row['paypal_tax']='paypal' in [str(x).lower() for x in pmt3] if isinstance(pmt3,list) else False; row['zero_tax']=str(a3)=='0'
        else:
            row['tax_body']=tr.text[:100]
    except Exception as e:
        row['err']=str(e)[:140]
    rows.append(row)
    print(row, flush=True)
    time.sleep(0.3)

Path= __import__('pathlib').Path
Path('scripts/pp_dual_matrix_init.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
print('saved scripts/pp_dual_matrix_init.json')
