print('=== FR/EUR + BE/EUR full flow ===', flush=True)
p = fetch_proxy('US')
s = make_session(p)
h = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
# FR and BE billing addresses (no state field)
addrs = {
    'FR': {'name': 'Jean Dupont', 'email': 'test@example.com', 'country': 'FR', 'city': 'Paris', 'postal_code': '75001', 'line1': '1 Rue de Rivoli'},
    'BE': {'name': 'Pierre Dubois', 'email': 'test@example.com', 'country': 'BE', 'city': 'Brussels', 'postal_code': '1000', 'line1': 'Rue Neuve 1'},
}
for country in ['FR', 'BE']:
    addr = addrs[country]
    try:
        pl = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': country, 'currency': 'EUR'}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
        r = s.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
        data = r.json(); cs = data.get('checkout_session_id',''); pk = data.get('publishable_key','') or STRIPE_PK; ent = data.get('processor_entity','') or 'openai_llc'
        ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        init = ir.json(); ck = init.get('init_checksum',''); cid = str(init.get('config_id') or '')
        amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '?')
        # tax_region without state
        tb = {'eid': str(uuid.uuid4()), 'tax_region[country]': addr['country'], 'tax_region[postal_code]': addr['postal_code'], 'tax_region[line1]': addr['line1'], 'tax_region[city]': addr['city'], 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        tr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb, headers=sh, timeout=30)
        if tr.status_code == 200:
            td = tr.json(); ck = td.get('init_checksum', ck); amt = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or amt)
        ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': 'es_' + uuid.uuid4().hex[:11], 'elements_session_config_id': cid or str(uuid.uuid4()), 'config_id': cid, 'init_checksum': ck, 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
        pmb = {'billing_details[name]': addr['name'], 'billing_details[email]': addr['email'], 'billing_details[address][country]': addr['country'], 'billing_details[address][line1]': addr['line1'], 'billing_details[address][city]': addr['city'], 'billing_details[address][postal_code]': addr['postal_code'], 'type': 'paypal', 'payment_user_agent': 'stripe.js/' + STRIPE_RUNTIME_VERSION + '; stripe-js-v3/' + STRIPE_RUNTIME_VERSION + '; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        pmr = s.post(f'{STRIPE_API_BASE}/payment_methods', data=pmb, headers=sh, timeout=20)
        pm = str(pmr.json().get('id') or '')
        surl = 'https://chatgpt.com/checkout/verify?stripe_session_id=' + cs + '&processor_entity=' + ent + '&plan_type=plus'
        rurl_base = 'https://pay.openai.com/c/pay/' + cs + '?success_return_url=' + surl
        cb = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm, 'init_checksum': ck, 'version': STRIPE_RUNTIME_VERSION, 'expected_amount': amt, 'expected_payment_method_type': 'paypal', 'return_url': rurl_base, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': 'en', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        cr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/confirm', data=cb, headers=sh, timeout=30)
        cd = cr.json()
        sa = cd.get('submission_attempt') or {}; ss = sa.get('state','') if isinstance(sa, dict) else ''
        pmr_url = find_redirect(cd)
        if not pmr_url:
            try: s.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
            except: pass
            pt = '/backend-api/payments/checkout/approve'
            ah = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': 'https://chatgpt.com/checkout/' + ent + '/' + cs, 'x-openai-target-path': pt, 'x-openai-target-route': pt}
            ar = s.post('https://chatgpt.com' + pt, json={'checkout_session_id': cs, 'processor_entity': ent}, headers=ah, timeout=20)
            ab = ar.json()
            if ab.get('result') == 'approved':
                pp = {'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[session_id]': 'es_' + uuid.uuid4().hex[:11], 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
                ph = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'}
                for i in range(6):
                    time.sleep(2)
                    pr = s.get(f'{STRIPE_API_BASE}/payment_pages/{cs}', params=pp, headers=ph, timeout=8)
                    if pr.status_code == 200:
                        pd = pr.json(); pmr_url = find_redirect(pd)
                        if pmr_url:
                            rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
                            fin = str(getattr(rg, 'url', '')); bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
                            print(f'{country}: amt={amt} approve=approved ba={bm.group(1) if bm else "NONE"}', flush=True)
                            break
                        sa2 = pd.get('submission_attempt') or {}; pst = sa2.get('state','') if isinstance(sa2, dict) else ''
                        if pst == 'failed':
                            print(f'{country}: amt={amt} approve=approved poll FAILED', flush=True); break
                else:
                    print(f'{country}: amt={amt} approve=approved poll TIMEOUT', flush=True)
            else:
                print(f'{country}: amt={amt} approve={ab.get("result","?")}', flush=True)
        else:
            rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
            fin = str(getattr(rg, 'url', '')); bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
            print(f'{country}: amt={amt} redirect=confirm ba={bm.group(1) if bm else "NONE"}', flush=True)
        time.sleep(0.5)
    except Exception as e:
        print(f'{country}: err {str(e)[:80]}', flush=True)
        time.sleep(0.5)
print('\n=== DONE ===', flush=True)
