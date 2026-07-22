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
EUR_ADDR = {
    'DE': {'name': 'Max Mustermann', 'email': 'test@example.com', 'country': 'DE', 'state': '', 'city': 'Berlin', 'postal_code': '10115', 'line1': 'Friedrichstrasse 1'},
    'FR': {'name': 'Jean Dupont', 'email': 'test@example.com', 'country': 'FR', 'state': '', 'city': 'Paris', 'postal_code': '75001', 'line1': '1 Rue de Rivoli'},
    'IE': {'name': 'Sean Murphy', 'email': 'test@example.com', 'country': 'IE', 'state': '', 'city': 'Dublin', 'postal_code': 'D01 F5P2', 'line1': '1 Grafton Street'},
    'NL': {'name': 'Jan de Vries', 'email': 'test@example.com', 'country': 'NL', 'state': '', 'city': 'Amsterdam', 'postal_code': '1011 AA', 'line1': 'Damrak 1'},
}
eur_countries = [('DE', 'EUR'), ('FR', 'EUR'), ('IE', 'EUR'), ('NL', 'EUR')]
print('=== US proxy + EUR checkout countries ===', flush=True)
p = fetch_proxy('US')
s = make_session(p)
h = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
for country, currency in eur_countries:
    addr = EUR_ADDR[country]
    try:
        pl = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': country, 'currency': currency}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
        r = s.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
        if r.status_code != 200: print(f'{country}: checkout {r.status_code}', flush=True); continue
        data = r.json(); cs = data.get('checkout_session_id',''); pk = data.get('publishable_key','') or STRIPE_PK; ent = data.get('processor_entity','') or 'openai_llc'
        if not cs: print(f'{country}: no cs', flush=True); continue
        ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        if ir.status_code != 200: print(f'{country}: init {ir.status_code}', flush=True); continue
        init = ir.json(); ck = init.get('init_checksum',''); cid = str(init.get('config_id') or '')
        pmt = init.get('payment_method_types', []); amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '?')
        tb = {'eid': str(uuid.uuid4()), 'tax_region[country]': addr['country'], 'tax_region[state]': addr['state'], 'tax_region[postal_code]': addr['postal_code'], 'tax_region[line1]': addr['line1'], 'tax_region[city]': addr['city'], 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        tr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb, headers=sh, timeout=30)
        if tr.status_code == 200:
            td = tr.json(); ck = td.get('init_checksum', ck); amt = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or amt)
        ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': 'es_' + uuid.uuid4().hex[:11], 'elements_session_config_id': cid or str(uuid.uuid4()), 'config_id': cid, 'init_checksum': ck, 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
        pmb = {'billing_details[name]': addr['name'], 'billing_details[email]': addr['email'], 'billing_details[address][country]': addr['country'], 'billing_details[address][line1]': addr['line1'], 'billing_details[address][city]': addr['city'], 'billing_details[address][postal_code]': addr['postal_code'], 'billing_details[address][state]': addr['state'], 'type': 'paypal', 'payment_user_agent': 'stripe.js/' + STRIPE_RUNTIME_VERSION + '; stripe-js-v3/' + STRIPE_RUNTIME_VERSION + '; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        pmr = s.post(f'{STRIPE_API_BASE}/payment_methods', data=pmb, headers=sh, timeout=20)
        if pmr.status_code != 200: print(f'{country}: pm {pmr.status_code}', flush=True); continue
        pm = str(pmr.json().get('id') or '')
        surl = 'https://chatgpt.com/checkout/verify?stripe_session_id=' + cs + '&processor_entity=' + ent + '&plan_type=plus'
        rurl_base = 'https://pay.openai.com/c/pay/' + cs + '?success_return_url=' + surl
        cb = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm, 'init_checksum': ck, 'version': STRIPE_RUNTIME_VERSION, 'expected_amount': amt, 'expected_payment_method_type': 'paypal', 'return_url': rurl_base, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': 'en', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        cr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/confirm', data=cb, headers=sh, timeout=30)
        if cr.status_code != 200: print(f'{country}: confirm {cr.status_code}', flush=True); continue
        cd = cr.json(); sa = cd.get('submission_attempt') or {}; ss = sa.get('state','') if isinstance(sa, dict) else ''
        pmr_url = find_redirect(cd)
        if pmr_url:
            rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
            fin = str(getattr(rg, 'url', '')); bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
            print(f'{country}: amt={amt} ent={ent} confirm_sa={ss} redirect=confirm approve=skip ba={"Y" if bm else "N"}', flush=True)
            continue
        try: s.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
        except: pass
        pt = '/backend-api/payments/checkout/approve'
        ah = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': 'https://chatgpt.com/checkout/' + ent + '/' + cs, 'x-openai-target-path': pt, 'x-openai-target-route': pt}
        ar = s.post('https://chatgpt.com' + pt, json={'checkout_session_id': cs, 'processor_entity': ent}, headers=ah, timeout=20)
        try: ab = ar.json(); apv = ab.get('result','?')
        except: apv = '?'
        ba = ''
        if apv == 'approved':
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
                        if bm: ba = bm.group(1)
                        break
        print(f'{country}: amt={amt} ent={ent} confirm_sa={ss} approve={apv} ba={ba}', flush=True)
        time.sleep(0.5)
    except Exception as e:
        print(f'{country}: err {str(e)[:80]}', flush=True)
        time.sleep(0.5)
print('\n=== DONE ===', flush=True)

