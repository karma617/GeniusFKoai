import sys, os, json, time, uuid, re, random
sys.path.insert(0, '.')
os.environ['PYTHONIOENCODING'] = 'utf-8'
from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt

PRE_PROXY = 'socks5h://127.0.0.1:7897'
KOOKEEY_API = 'https://www.kookeey.com/pickdynamicips?t=2&auth=pwd&format=4&n=1&p=http&gate=global&g={region}&r=-1&type=txt&sign=874086cfbdb353e32d67a6dbebd498af&accessid=8239626&upf=1,1&dl=%5Cr%5Cn'
PAYMENT_CHECKOUT_URL = 'https://chatgpt.com/backend-api/payments/checkout'
STRIPE_API_BASE = 'https://api.stripe.com/v1'
STRIPE_PK = 'pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n'
STRIPE_INIT_VERSION = '2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1'
STRIPE_RUNTIME_VERSION = '6f8494a281'
PM_REDIRECT_RE = re.compile(r'https://pm-redirects\.stripe\.com/authorize/[^"\s<>]+')
US_ADDR = {'name':'John Smith','email':'test@example.com','country':'US','state':'NY','city':'New York','postal_code':'10001','line1':'350 5th Ave','line2':'New York'}
TEST_REGIONS = ['US','BR','JP','VN','TH','KR','TW','HK','KH','ID','PH','MY','SG','AU','GB','CA','DE','FR','IT','ES','NL','IE','PT','BE','FI','AT','CH','SE','NO','DK','PL','CZ','MX','NZ','LU']
_ds = cffi.Session(impersonate='chrome110')
def fetch_proxy(region):
    r = _ds.get(KOOKEEY_API.format(region=region), timeout=15)
    parts = r.text.strip().split(':')
    if len(parts) != 4: raise ValueError('proxy parse fail: ' + r.text[:100])
    return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
def make_session(proxy_url):
    return cffi.Session(impersonate='chrome110', proxy=proxy_url, curl_options={CurlOpt.PRE_PROXY: PRE_PROXY})
with open('scripts/_active_token.json') as f:
    d = json.load(f)
TOKEN = d['token']; COOKIE = d['cookie']; EMAIL = d['email']
def find_redirect(d):
    if not isinstance(d, dict): return ''
    def _s(v):
        if isinstance(v, str):
            m = PM_REDIRECT_RE.search(v); return m.group(0) if m else ''
        if isinstance(v, dict):
            na = v.get('next_action')
            if isinstance(na, dict) and na.get('type') == 'redirect_to_url':
                rtu = na.get('redirect_to_url')
                if isinstance(rtu, dict):
                    url = str(rtu.get('url') or '').strip()
                    if url: return url
            for x in v.values():
                r = _s(x)
                if r: return r
        if isinstance(v, list):
            for x in v:
                r = _s(x)
                if r: return r
        return ''
    for k in ('setup_intent', 'payment_intent'):
        r = _s(d.get(k) or {})
        if r: return r
    return _s(d)
sh = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}

def try_ba_chain(r1_region, r2_region):
    result = {'r1': r1_region, 'r2': r2_region, 'ba': '', 'ba_ok': False, 'amt': '', 'pmt': None}
    try:
        p1 = fetch_proxy(r1_region)
        s1 = make_session(p1)
        headers = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
        payload = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': 'US', 'currency': 'USD'}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
        r = s1.post(PAYMENT_CHECKOUT_URL, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            result['error'] = 'checkout ' + str(r.status_code) + ': ' + r.text[:80]; return result
        data = r.json()
        cs_id = data.get('checkout_session_id','')
        pk = data.get('publishable_key','') or STRIPE_PK
        entity = data.get('processor_entity','') or 'openai_llc'
        if not cs_id:
            result['error'] = 'no cs_id'; return result
        if r2_region == r1_region:
            s2 = s1
        else:
            p2 = fetch_proxy(r2_region)
            s2 = make_session(p2)
        ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        init_r = s2.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/init', data=ib, headers=sh, timeout=30)
        if init_r.status_code != 200:
            result['error'] = 'init ' + str(init_r.status_code); return result
        init = init_r.json()
        checksum = init.get('init_checksum','')
        config_id = str(init.get('config_id') or '')
        pmt = init.get('payment_method_types')
        amt_init = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '0')
        result['pmt'] = pmt; result['amt'] = amt_init
        has_pp = isinstance(pmt, list) and 'paypal' in [str(x).lower() for x in pmt]
        result['paypal'] = has_pp; result['zero'] = (amt_init == '0')
        if not has_pp:
            result['error'] = 'no paypal: ' + str(pmt); return result
        tax_body = {'eid': str(uuid.uuid4()), 'tax_region[country]': 'US', 'tax_region[state]': 'NY', 'tax_region[postal_code]': '10001', 'tax_region[line1]': '350 5th Ave', 'tax_region[city]': 'New York', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        tax_r = s2.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}', data=tax_body, headers=sh, timeout=30)
        if tax_r.status_code == 200:
            td = tax_r.json(); checksum = td.get('init_checksum', checksum)
            amt_tax = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or amt_init)
            pmt2 = td.get('payment_method_types', pmt); result['pmt'] = pmt2; result['amt'] = amt_tax
            has_pp = isinstance(pmt2, list) and 'paypal' in [str(x).lower() for x in pmt2]
            if not has_pp:
                result['error'] = 'paypal lost after tax: ' + str(pmt2); return result
        else:
            amt_tax = amt_init
        ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': 'elements_session_' + uuid.uuid4().hex[:11], 'elements_session_config_id': config_id or str(uuid.uuid4()), 'config_id': config_id, 'init_checksum': checksum, 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
        pm_body = {'billing_details[name]': US_ADDR['name'], 'billing_details[email]': US_ADDR['email'], 'billing_details[address][country]': US_ADDR['country'], 'billing_details[address][line1]': US_ADDR['line1'], 'billing_details[address][city]': US_ADDR['city'], 'billing_details[address][postal_code]': US_ADDR['postal_code'], 'billing_details[address][state]': US_ADDR['state'], 'type': 'paypal', 'payment_user_agent': 'stripe.js/' + STRIPE_RUNTIME_VERSION + '; stripe-js-v3/' + STRIPE_RUNTIME_VERSION + '; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        pm_r = s2.post(f'{STRIPE_API_BASE}/payment_methods', data=pm_body, headers=sh, timeout=20)
        if pm_r.status_code != 200:
            result['error'] = 'pm ' + str(pm_r.status_code); return result
        pm_id = str(pm_r.json().get('id') or '')
        if not pm_id.startswith('pm_'):
            result['error'] = 'bad pm'; return result
        success_url = 'https://chatgpt.com/checkout/verify?stripe_session_id=' + cs_id + '&processor_entity=' + entity + '&plan_type=plus'
        return_url = 'https://pay.openai.com/c/pay/' + cs_id + '?success_return_url=' + success_url
        cb = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm_id, 'init_checksum': checksum, 'version': STRIPE_RUNTIME_VERSION, 'expected_amount': amt_tax, 'expected_payment_method_type': 'paypal', 'return_url': return_url, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': 'en', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        cr = s2.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/confirm', data=cb, headers=sh, timeout=30)
        if cr.status_code != 200:
            result['error'] = 'confirm ' + str(cr.status_code) + ': ' + cr.text[:100]; return result
        cdata = cr.json()
        sa = cdata.get('submission_attempt') or {}
        sa_state = sa.get('state','') if isinstance(sa, dict) else ''
        result['sa'] = sa_state
        rurl = find_redirect(cdata)
        if rurl:
            result['pm_redirect'] = rurl
            r2g = s2.get(rurl, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
            final = str(getattr(r2g, 'url', ''))
            bam = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', final)
            if bam:
                result['ba'] = bam.group(1)
                result['ba_ok'] = True
                result['ba_url'] = final[:200]
            else:
                result['ba'] = final[:100]
                result['ba_ok'] = False
                result['error'] = 'no ba_token: ' + final[:60]
            return result
        if sa_state != 'requires_approval':
            result['error'] = 'sa=' + sa_state; return result
        try: s2.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
        except: pass
        path = '/backend-api/payments/checkout/approve'
        ah = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': 'https://chatgpt.com/checkout/' + entity + '/' + cs_id, 'x-openai-target-path': path, 'x-openai-target-route': path}
        ar = s2.post('https://chatgpt.com' + path, json={'checkout_session_id': cs_id, 'processor_entity': entity}, headers=ah, timeout=20)
        result['approve'] = ar.status_code
        if ar.status_code != 200:
            result['error'] = 'approve ' + str(ar.status_code); return result
        pp = {'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[session_id]': 'elements_session_' + uuid.uuid4().hex[:11], 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ph = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'}
        for i in range(8):
            time.sleep(2)
            pr = s2.get(f'{STRIPE_API_BASE}/payment_pages/{cs_id}', params=pp, headers=ph, timeout=5)
            if pr.status_code == 200:
                rurl = find_redirect(pr.json())
                if rurl:
                    result['pm_redirect'] = rurl
                    r2g = s2.get(rurl, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
                    final = str(getattr(r2g, 'url', ''))
                    bam = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', final)
                    if bam:
                        result['ba'] = bam.group(1)
                        result['ba_ok'] = True
                        result['ba_url'] = final[:200]
                    else:
                        result['ba'] = final[:100]
                        result['ba_ok'] = False
                        result['error'] = 'no ba_token: ' + final[:60]
                    return result
        psa = (pr.json() if pr.status_code == 200 else {}).get('submission_attempt') or {}
        result['error'] = 'poll timeout sa=' + str(psa.get('state','') if isinstance(psa, dict) else '')
    except Exception as e:
        result['error'] = str(e)[:150]
    return result

# === 主循环: US 创建 + 不同 r2 代理撞链 ===
results = []
# 先测试 US 全程确认基线
for region in ['US', 'BR', 'JP', 'VN', 'TH', 'KR', 'TW', 'HK', 'KH', 'ID', 'PH', 'MY', 'SG', 'AU', 'GB', 'CA', 'DE', 'FR', 'IT', 'ES', 'NL', 'IE', 'PT', 'BE', 'FI', 'AT', 'CH', 'SE', 'NO', 'DK', 'PL', 'CZ', 'MX', 'NZ', 'LU']:
    print('\n--- r1=US r2=' + region + ' ---')
    try:
        res = try_ba_chain('US', region)
        ba_short = res.get('ba','')[:60] if res.get('ba_ok') else ''
        amt = res.get('amt','')
        pmt = res.get('pmt')
        zero = res.get('zero', False)
        pp = res.get('paypal', False)
        err = res.get('error','')[:80]
        print('  amt=' + str(amt) + ' pmt=' + str(pmt) + ' zero=' + str(zero) + ' pp=' + str(pp) + ' ba=' + str(res['ba_ok']) + ' err=' + err)
        if ba_short:
            print('  BA URL: ' + ba_short)
        results.append(res)
    except Exception as e:
        print('  EXCEPTION: ' + str(e)[:100])
        results.append({'r1':'US','r2':region,'ba_ok':False,'error':str(e)[:100]})
    time.sleep(1)

# 保存结果
with open('scripts/ba_chain_loop_results.json', 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

# 汇总
print('\n\n=== 汇总 ===')
for r in results:
    r2 = r.get('r2','')
    amt = r.get('amt','')
    pmt = r.get('pmt')
    zero = r.get('zero', False)
    pp = r.get('paypal', False)
    ba = r.get('ba_ok', False)
    err = r.get('error','')[:60]
    print(f'r2={r2:4s} amt={str(amt):6s} pmt={str(pmt):25s} zero={str(zero):5s} pp={str(pp):5s} ba={str(ba):5s} err={err}')
