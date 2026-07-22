import sys, os, json, time, uuid, re
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
_ds = cffi.Session(impersonate='chrome110')

def fetch_proxy(region):
    for _ in range(3):
        try:
            r = _ds.get(KOOKEEY_API.format(region=region), timeout=15)
            parts = r.text.strip().split(':')
            if len(parts) == 4: return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
        except: pass
        time.sleep(1)
    raise ValueError('proxy fail: ' + region)

def make_session(proxy_url):
    return cffi.Session(impersonate='chrome110', proxy=proxy_url, curl_options={CurlOpt.PRE_PROXY: PRE_PROXY})

with open('scripts/_active_token.json') as f:
    d = json.load(f)
TOKEN = d['token']; COOKIE = d['cookie']

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

def hammer_once(r1, r2):
    res = {'r1': r1, 'r2': r2, 'approve_result': '', 'ba_ok': False, 'ba': '', 'err': ''}
    try:
        p1 = fetch_proxy(r1)
        s1 = make_session(p1)
        h = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
        pl = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': 'US', 'currency': 'USD'}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
        r = s1.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
        if r.status_code != 200: res['err'] = 'checkout ' + str(r.status_code); return res
        data = r.json(); cs = data.get('checkout_session_id',''); pk = data.get('publishable_key','') or STRIPE_PK; ent = data.get('processor_entity','') or 'openai_llc'
        if not cs: res['err'] = 'no cs'; return res
        s2 = s1 if r2 == r1 else make_session(fetch_proxy(r2))
        ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ir = s2.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        if ir.status_code != 200: res['err'] = 'init ' + str(ir.status_code); return res
        init = ir.json(); ck = init.get('init_checksum',''); cid = str(init.get('config_id') or '')
        pmt = init.get('payment_method_types'); amt0 = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '0')
        if not (isinstance(pmt, list) and 'paypal' in [str(x).lower() for x in pmt]): res['err'] = 'no paypal'; return res
        tb = {'eid': str(uuid.uuid4()), 'tax_region[country]': 'US', 'tax_region[state]': 'NY', 'tax_region[postal_code]': '10001', 'tax_region[line1]': '350 5th Ave', 'tax_region[city]': 'New York', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        tr = s2.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb, headers=sh, timeout=30)
        if tr.status_code == 200:
            td = tr.json(); ck = td.get('init_checksum', ck); amt = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or amt0)
        else: amt = amt0
        ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': 'es_' + uuid.uuid4().hex[:11], 'elements_session_config_id': cid or str(uuid.uuid4()), 'config_id': cid, 'init_checksum': ck, 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
        pmb = {'billing_details[name]': US_ADDR['name'], 'billing_details[email]': US_ADDR['email'], 'billing_details[address][country]': US_ADDR['country'], 'billing_details[address][line1]': US_ADDR['line1'], 'billing_details[address][city]': US_ADDR['city'], 'billing_details[address][postal_code]': US_ADDR['postal_code'], 'billing_details[address][state]': US_ADDR['state'], 'type': 'paypal', 'payment_user_agent': 'stripe.js/' + STRIPE_RUNTIME_VERSION + '; stripe-js-v3/' + STRIPE_RUNTIME_VERSION + '; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        pmr = s2.post(f'{STRIPE_API_BASE}/payment_methods', data=pmb, headers=sh, timeout=20)
        if pmr.status_code != 200: res['err'] = 'pm ' + str(pmr.status_code); return res
        pm = str(pmr.json().get('id') or '')
        if not pm.startswith('pm_'): res['err'] = 'bad pm'; return res
        surl = 'https://chatgpt.com/checkout/verify?stripe_session_id=' + cs + '&processor_entity=' + ent + '&plan_type=plus'
        rurl_base = 'https://pay.openai.com/c/pay/' + cs + '?success_return_url=' + surl
        cb = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm, 'init_checksum': ck, 'version': STRIPE_RUNTIME_VERSION, 'expected_amount': amt, 'expected_payment_method_type': 'paypal', 'return_url': rurl_base, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': 'en', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        cr = s2.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/confirm', data=cb, headers=sh, timeout=30)
        if cr.status_code != 200: res['err'] = 'confirm ' + str(cr.status_code); return res
        cd = cr.json()
        pmr_url = find_redirect(cd)
        if pmr_url:
            rg = s2.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
            fin = str(getattr(rg, 'url', '')); bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
            if bm: res['ba'] = bm.group(1); res['ba_ok'] = True; res['approve_result'] = 'confirm_redirect'
            else: res['err'] = 'no ba: ' + fin[:60]
            return res
        try: s2.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
        except: pass
        pt = '/backend-api/payments/checkout/approve'
        ah = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': 'https://chatgpt.com/checkout/' + ent + '/' + cs, 'x-openai-target-path': pt, 'x-openai-target-route': pt}
        ar = s2.post('https://chatgpt.com' + pt, json={'checkout_session_id': cs, 'processor_entity': ent}, headers=ah, timeout=20)
        try:
            ab = ar.json(); res['approve_result'] = ab.get('result', 'unknown_' + str(ar.status_code))
        except:
            res['approve_result'] = 'http_' + str(ar.status_code)
        if ar.status_code != 200: res['err'] = 'approve ' + str(ar.status_code); return res
        if res['approve_result'] != 'approved': return res
        pp = {'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[session_id]': 'es_' + uuid.uuid4().hex[:11], 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ph = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'}
        for i in range(6):
            time.sleep(2)
            pr = s2.get(f'{STRIPE_API_BASE}/payment_pages/{cs}', params=pp, headers=ph, timeout=8)
            if pr.status_code == 200:
                pd = pr.json(); pmr_url = find_redirect(pd)
                if pmr_url:
                    rg = s2.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
                    fin = str(getattr(rg, 'url', '')); bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
                    if bm: res['ba'] = bm.group(1); res['ba_ok'] = True
                    else: res['err'] = 'no ba poll: ' + fin[:60]
                    return res
                psa = pd.get('submission_attempt') or {}; pst = psa.get('state','') if isinstance(psa, dict) else ''
                if pst == 'failed':
                    pe = psa.get('error',{}) if isinstance(psa, dict) else {}
                    res['err'] = 'failed:' + str(pe.get('code','') if isinstance(pe, dict) else ''); return res
        res['err'] = 'timeout approved but no redirect'
    except Exception as e:
        res['err'] = str(e)[:100]
    return res

# US x20: 统计 approved vs blocked vs network_error
print('=== US x20 (approve result statistics) ===', flush=True)
from collections import Counter
results = []
for i in range(20):
    r = hammer_once('US', 'US')
    results.append(r)
    ar = r.get('approve_result', 'N/A')
    ba = r.get('ba', '')
    err = r.get('err', '')[:40]
    print(f'  [{i+1:2d}] approve_result={ar:20s} ba_ok={str(r["ba_ok"]):5s} ba={ba} err={err}', flush=True)
    time.sleep(0.5)

print('\n=== SUMMARY ===', flush=True)
cnt = Counter(r.get('approve_result', 'N/A') for r in results)
for k, v in cnt.most_common():
    ba_ok = sum(1 for r in results if r.get('approve_result') == k and r.get('ba_ok'))
    print(f'  {k}: {v} total, {ba_ok} ba_ok', flush=True)

with open('scripts/ba_us_hammer_results.json', 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print('\n=== DONE ===', flush=True)
