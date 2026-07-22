print('=== IE/EUR fixed: no state in tax_region ===', flush=True)
p = fetch_proxy('US')
s = make_session(p)
h = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
addr = {'name': 'Sean Murphy', 'email': 'test@example.com', 'country': 'IE', 'state': '', 'city': 'Dublin', 'postal_code': 'D01 F5P2', 'line1': '1 Grafton Street'}
pl = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': 'IE', 'currency': 'EUR'}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
r = s.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
data = r.json(); cs = data.get('checkout_session_id',''); pk = data.get('publishable_key','') or STRIPE_PK; ent = data.get('processor_entity','') or 'openai_llc'
print(f'cs={cs[:25]} ent={ent}', flush=True)
ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
init = ir.json(); ck = init.get('init_checksum',''); cid = str(init.get('config_id') or '')
pmt = init.get('payment_method_types', []); amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '?')
print(f'init: pmt={pmt} amt={amt}', flush=True)
# IE tax_region - DON'T send state
tb = {'eid': str(uuid.uuid4()), 'tax_region[country]': 'IE', 'tax_region[postal_code]': 'D01 F5P2', 'tax_region[line1]': '1 Grafton Street', 'tax_region[city]': 'Dublin', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
tr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb, headers=sh, timeout=30)
print(f'tax IE (no state): {tr.status_code}', flush=True)
if tr.status_code == 200:
    td = tr.json(); ck = td.get('init_checksum', ck); amt = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or amt)
    print(f'  amt={amt}', flush=True)
else:
    print(f'  body: {tr.text[:300]}', flush=True)
ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': 'es_' + uuid.uuid4().hex[:11], 'elements_session_config_id': cid or str(uuid.uuid4()), 'config_id': cid, 'init_checksum': ck, 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
pmb = {'billing_details[name]': addr['name'], 'billing_details[email]': addr['email'], 'billing_details[address][country]': addr['country'], 'billing_details[address][line1]': addr['line1'], 'billing_details[address][city]': addr['city'], 'billing_details[address][postal_code]': addr['postal_code'], 'type': 'paypal', 'payment_user_agent': 'stripe.js/' + STRIPE_RUNTIME_VERSION + '; stripe-js-v3/' + STRIPE_RUNTIME_VERSION + '; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
pmr = s.post(f'{STRIPE_API_BASE}/payment_methods', data=pmb, headers=sh, timeout=20)
pm = str(pmr.json().get('id') or '')
print(f'pm: {pm}', flush=True)
surl = 'https://chatgpt.com/checkout/verify?stripe_session_id=' + cs + '&processor_entity=' + ent + '&plan_type=plus'
rurl_base = 'https://pay.openai.com/c/pay/' + cs + '?success_return_url=' + surl
cb = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm, 'init_checksum': ck, 'version': STRIPE_RUNTIME_VERSION, 'expected_amount': amt, 'expected_payment_method_type': 'paypal', 'return_url': rurl_base, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': 'en', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
cr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/confirm', data=cb, headers=sh, timeout=30)
cd = cr.json()
sa = cd.get('submission_attempt') or {}; ss = sa.get('state','') if isinstance(sa, dict) else ''
pmr_url = find_redirect(cd)
print(f'confirm: {cr.status_code} sa={ss} redirect={bool(pmr_url)}', flush=True)
if not pmr_url:
    try: s.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
    except: pass
    pt = '/backend-api/payments/checkout/approve'
    ah = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': 'https://chatgpt.com/checkout/' + ent + '/' + cs, 'x-openai-target-path': pt, 'x-openai-target-route': pt}
    ar = s.post('https://chatgpt.com' + pt, json={'checkout_session_id': cs, 'processor_entity': ent}, headers=ah, timeout=20)
    ab = ar.json()
    print(f'approve: {ar.status_code} result={ab.get("result","?")}', flush=True)
    if ab.get('result') == 'approved':
        pp = {'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[session_id]': 'es_' + uuid.uuid4().hex[:11], 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ph = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'}
        for i in range(10):
            time.sleep(2)
            pr = s.get(f'{STRIPE_API_BASE}/payment_pages/{cs}', params=pp, headers=ph, timeout=8)
            if pr.status_code == 200:
                pd = pr.json()
                sa2 = pd.get('submission_attempt') or {}; pst = sa2.get('state','') if isinstance(sa2, dict) else ''
                pmr_url = find_redirect(pd)
                print(f'  poll{i}: sa={pst} redirect={bool(pmr_url)}', flush=True)
                if pmr_url:
                    rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
                    fin = str(getattr(rg, 'url', '')); bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
                    print(f'  BA: {bm.group(1) if bm else "NONE"}', flush=True)
                    break
                if pst == 'failed':
                    pe = sa2.get('error',{}) if isinstance(sa2, dict) else {}
                    print(f'  FAILED: {json.dumps(pe, ensure_ascii=False)[:200]}', flush=True)
                    break
else:
    rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
    fin = str(getattr(rg, 'url', '')); bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
    print(f'BA: {bm.group(1) if bm else "NONE"}', flush=True)
print('\n=== DONE ===', flush=True)
