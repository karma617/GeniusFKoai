print('=== EUR extended probe ===', flush=True)
p = fetch_proxy('US')
s = make_session(p)
h = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
countries = [('BE','EUR'),('ES','EUR'),('IT','EUR'),('PT','EUR'),('AT','EUR'),('FI','EUR'),('LU','EUR')]
for country, currency in countries:
    try:
        pl = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': country, 'currency': currency}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
        r = s.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
        if r.status_code != 200: print(f'{country}: checkout {r.status_code}', flush=True); continue
        data = r.json(); cs = data.get('checkout_session_id',''); pk = data.get('publishable_key','') or STRIPE_PK; ent = data.get('processor_entity','') or 'openai_llc'
        if not cs: print(f'{country}: no cs', flush=True); continue
        ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        if ir.status_code != 200: print(f'{country}: init {ir.status_code}', flush=True); continue
        init = ir.json()
        pmt = init.get('payment_method_types', [])
        amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '?')
        paypal = 'paypal' in [str(x).lower() for x in pmt] if isinstance(pmt, list) else False
        print(f'{country}: ent={ent} amt={amt} paypal={paypal} pmt={pmt}', flush=True)
        time.sleep(0.5)
    except Exception as e:
        print(f'{country}: err {str(e)[:80]}', flush=True)
        time.sleep(0.5)
print('\n=== DONE ===', flush=True)
