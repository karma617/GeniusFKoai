import json, os, sys, time, uuid
sys.path.insert(0, '.')
os.environ['PYTHONIOENCODING'] = 'utf-8'
from scripts.pp_triple_proxy_extract import (
    PAYMENT_CHECKOUT_URL, STRIPE_API_BASE, STRIPE_PK, STRIPE_INIT_VERSION, STRIPE_RUNTIME_VERSION,
    ADDRS, sh, extract_amounts, pick_amt, find_redirect, BA_RE, payment_pages_update, make_session, fetch_proxy, auth_headers
)

def run(label, promo_country, re_tax_us=False, do_full=True):
    res = {'label': label, 'promo': promo_country, 're_tax_us': re_tax_us}
    try:
        s_us = make_session(fetch_proxy('US'))
        s_promo = make_session(fetch_proxy(promo_country))
        pl = {
            'plan_name': 'chatgptplusplan',
            'billing_details': {'country': 'US', 'currency': 'USD'},
            'entry_point': 'all_plans_pricing_modal',
            'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False},
            'checkout_ui_mode': 'custom',
        }
        r = s_us.post(PAYMENT_CHECKOUT_URL, headers=auth_headers(), json=pl, timeout=30)
        res['checkout'] = r.status_code
        if r.status_code != 200:
            res['err'] = r.text[:120]
            print(res, flush=True)
            return res
        data = r.json()
        cs = data.get('checkout_session_id')
        pk = data.get('publishable_key') or STRIPE_PK
        ent = data.get('processor_entity') or 'openai_llc'
        ib = {
            'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai',
            'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
            'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
            'elements_session_client[elements_init_source]': 'custom_checkout',
            'elements_session_client[referrer_host]': 'chatgpt.com',
            'elements_session_client[stripe_js_id]': str(uuid.uuid4()),
            'elements_session_client[locale]': 'en',
            'elements_session_client[is_aggregation_expected]': 'false',
            'elements_options_client[saved_payment_method][enable_save]': 'never',
            'elements_options_client[saved_payment_method][enable_redisplay]': 'never',
            'key': pk, '_stripe_version': STRIPE_INIT_VERSION,
        }
        ir = s_us.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        res['init'] = ir.status_code
        if ir.status_code != 200:
            res['err'] = ir.text[:120]
            print(res, flush=True)
            return res
        init = ir.json()
        ck = init.get('init_checksum', '')
        cid = str(init.get('config_id') or '')
        pmt = init.get('payment_method_types') or []
        amt = pick_amt(extract_amounts(init), '?')
        res['amt0'] = amt
        res['pmt0'] = pmt
        res['paypal0'] = 'paypal' in [str(x).lower() for x in pmt]
        res['zero0'] = str(amt) == '0'

        pr = payment_pages_update(s_promo, cs, pk, ADDRS[promo_country])
        res['promo_tax'] = pr.status_code
        if pr.status_code == 200:
            pd = pr.json()
            ck = pd.get('init_checksum', ck)
            pmt = pd.get('payment_method_types') or pmt
            amt = pick_amt(extract_amounts(pd), amt)
            res['amt1'] = amt
            res['pmt1'] = pmt
            res['paypal1'] = 'paypal' in [str(x).lower() for x in pmt]
            res['zero1'] = str(amt) == '0'
        else:
            res['promo_body'] = pr.text[:120]

        if re_tax_us:
            tr = payment_pages_update(s_us, cs, pk, ADDRS['US'])
            res['tax_us'] = tr.status_code
            if tr.status_code == 200:
                td = tr.json()
                ck = td.get('init_checksum', ck)
                pmt = td.get('payment_method_types') or pmt
                amt = pick_amt(extract_amounts(td), amt)
                res['amt2'] = amt
                res['pmt2'] = pmt
                res['paypal2'] = 'paypal' in [str(x).lower() for x in pmt]
                res['zero2'] = str(amt) == '0'

        res['final_amt'] = amt
        res['final_paypal'] = 'paypal' in [str(x).lower() for x in pmt]
        res['final_zero'] = str(amt) == '0'
        if (not do_full) or (not res['final_paypal']):
            print(res, flush=True)
            return res

        pm_addr = ADDRS['US'] if re_tax_us else ADDRS[promo_country]
        ctx = {
            'stripe_js_id': str(uuid.uuid4()),
            'elements_session_id': 'es_' + uuid.uuid4().hex[:11],
            'elements_session_config_id': cid or str(uuid.uuid4()),
            'config_id': cid,
        }
        pmb = {
            'billing_details[name]': pm_addr['name'],
            'billing_details[email]': pm_addr['email'],
            'billing_details[address][country]': pm_addr['country'],
            'billing_details[address][line1]': pm_addr['line1'],
            'billing_details[address][city]': pm_addr['city'],
            'billing_details[address][postal_code]': pm_addr['postal_code'],
            'type': 'paypal',
            'payment_user_agent': f'stripe.js/{STRIPE_RUNTIME_VERSION}; stripe-js-v3/{STRIPE_RUNTIME_VERSION}; payment-element; deferred-intent',
            'referrer': 'https://chatgpt.com',
            'time_on_page': '35000',
            'client_attribution_metadata[checkout_session_id]': cs,
            'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'],
            'client_attribution_metadata[checkout_config_id]': ctx.get('config_id') or '',
            'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'],
            'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'],
            'client_attribution_metadata[merchant_integration_source]': 'elements',
            'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
            'client_attribution_metadata[merchant_integration_version]': '2021',
            'client_attribution_metadata[payment_intent_creation_flow]': 'deferred',
            'client_attribution_metadata[payment_method_selection_flow]': 'automatic',
            'key': pk,
            '_stripe_version': STRIPE_INIT_VERSION,
        }
        if pm_addr.get('state'):
            pmb['billing_details[address][state]'] = pm_addr['state']
        pmr = s_us.post(f'{STRIPE_API_BASE}/payment_methods', data=pmb, headers=sh, timeout=30)
        res['pm'] = pmr.status_code
        if pmr.status_code != 200:
            res['err'] = 'pm ' + str(pmr.status_code)
            print(res, flush=True)
            return res
        pmid = pmr.json().get('id')
        surl = f'https://chatgpt.com/checkout/verify?stripe_session_id={cs}&processor_entity={ent}&plan_type=plus'
        rurl = f'https://pay.openai.com/c/pay/{cs}?success_return_url={surl}'
        conf = {
            'guid': uuid.uuid4().hex,
            'muid': uuid.uuid4().hex,
            'sid': uuid.uuid4().hex,
            'payment_method': pmid,
            'init_checksum': ck,
            'version': STRIPE_RUNTIME_VERSION,
            'expected_amount': str(amt),
            'expected_payment_method_type': 'paypal',
            'return_url': rurl,
            'elements_session_client[session_id]': ctx['elements_session_id'],
            'elements_session_client[locale]': 'en',
            'elements_session_client[referrer_host]': 'chatgpt.com',
            'elements_session_client[is_aggregation_expected]': 'false',
            'elements_session_client[elements_init_source]': 'custom_checkout',
            'elements_session_client[stripe_js_id]': ctx['stripe_js_id'],
            'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
            'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
            'elements_options_client[saved_payment_method][enable_save]': 'never',
            'elements_options_client[saved_payment_method][enable_redisplay]': 'never',
            'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'],
            'client_attribution_metadata[checkout_session_id]': cs,
            'client_attribution_metadata[checkout_config_id]': ctx.get('config_id', ''),
            'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'],
            'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'],
            'client_attribution_metadata[merchant_integration_source]': 'checkout',
            'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
            'client_attribution_metadata[merchant_integration_version]': 'custom',
            'client_attribution_metadata[payment_intent_creation_flow]': 'deferred',
            'client_attribution_metadata[payment_method_selection_flow]': 'automatic',
            'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment',
            'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address',
            'consent[terms_of_service]': 'accepted',
            'key': pk,
            '_stripe_version': STRIPE_INIT_VERSION,
        }
        cr = s_us.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/confirm', data=conf, headers=sh, timeout=30)
        res['confirm'] = cr.status_code
        if cr.status_code != 200:
            res['err'] = 'confirm ' + str(cr.status_code) + ' ' + cr.text[:100]
            print(res, flush=True)
            return res
        redir = find_redirect(cr.json())
        if redir:
            rg = s_us.get(redir, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
            fin = str(getattr(rg, 'url', ''))
            bm = BA_RE.search(fin)
            if bm:
                res['ba'] = bm.group(1)
                res['ba_ok'] = True
                res['ba_url'] = fin[:200]
                print(res, flush=True)
                return res
        pt = '/backend-api/payments/checkout/approve'
        ar = s_us.post(
            'https://chatgpt.com' + pt,
            json={'checkout_session_id': cs, 'processor_entity': ent},
            headers=auth_headers({
                'Referer': f'https://chatgpt.com/checkout/{ent}/{cs}',
                'x-openai-target-path': pt,
                'x-openai-target-route': pt,
            }),
            timeout=20,
        )
        ab = ar.json() if ar.status_code == 200 else {}
        res['approve'] = ab.get('result')
        res['approve_http'] = ar.status_code
        if ab.get('result') != 'approved':
            print(res, flush=True)
            return res
        pp = {
            'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1',
            'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1',
            'elements_session_client[elements_init_source]': 'custom_checkout',
            'elements_session_client[referrer_host]': 'chatgpt.com',
            'elements_session_client[session_id]': 'es_' + uuid.uuid4().hex[:11],
            'elements_session_client[stripe_js_id]': str(uuid.uuid4()),
            'elements_session_client[locale]': 'en',
            'elements_session_client[is_aggregation_expected]': 'false',
            'elements_options_client[saved_payment_method][enable_save]': 'never',
            'elements_options_client[saved_payment_method][enable_redisplay]': 'never',
            'key': pk,
            '_stripe_version': STRIPE_INIT_VERSION,
        }
        for _ in range(8):
            time.sleep(1.4)
            pr2 = s_us.get(
                f'{STRIPE_API_BASE}/payment_pages/{cs}',
                params=pp,
                headers={'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'},
                timeout=8,
            )
            if pr2.status_code != 200:
                continue
            redir = find_redirect(pr2.json())
            if redir:
                rg = s_us.get(redir, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
                fin = str(getattr(rg, 'url', ''))
                bm = BA_RE.search(fin)
                if bm:
                    res['ba'] = bm.group(1)
                    res['ba_ok'] = True
                    res['ba_url'] = fin[:200]
                else:
                    res['err'] = 'no ba ' + fin[:80]
                print(res, flush=True)
                return res
        res['err'] = 'poll timeout'
    except Exception as e:
        res['err'] = str(e)[:160]
    print(res, flush=True)
    return res

rows = []
for promo in ['BR', 'JP']:
    rows.append(run(f'US_init_promo_{promo}_only', promo, re_tax_us=False, do_full=True))
    time.sleep(0.5)
    rows.append(run(f'US_init_promo_{promo}_then_US', promo, re_tax_us=True, do_full=False))
    time.sleep(0.5)
open('scripts/pp_promo_only_probe.json', 'w', encoding='utf-8').write(json.dumps(rows, ensure_ascii=False, indent=2))
print('saved', len(rows), flush=True)

