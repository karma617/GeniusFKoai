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
print('=== IE tax_region 400 diagnostic ===', flush=True)
p = fetch_proxy('US')
s = make_session(p)
h = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
pl = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': 'IE', 'currency': 'EUR'}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
r = s.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
data = r.json(); cs = data.get('checkout_session_id',''); pk = data.get('publishable_key','') or STRIPE_PK; ent = data.get('processor_entity','') or 'openai_llc'
print(f'cs={cs[:25]} ent={ent}', flush=True)
ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
init = ir.json(); ck = init.get('init_checksum',''); cid = str(init.get('config_id') or '')
pmt = init.get('payment_method_types', []); amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '?')
print(f'init: pmt={pmt} amt={amt} ck={ck[:20]}', flush=True)
# 试 IE tax_region
tb_ie = {'eid': str(uuid.uuid4()), 'tax_region[country]': 'IE', 'tax_region[state]': '', 'tax_region[postal_code]': 'D01 F5P2', 'tax_region[line1]': '1 Grafton Street', 'tax_region[city]': 'Dublin', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
tr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb_ie, headers=sh, timeout=30)
print(f'tax IE: {tr.status_code}', flush=True)
print(f'  body: {tr.text[:500]}', flush=True)
# 再试 US tax_region (用同一 session)
tb_us = {'eid': str(uuid.uuid4()), 'tax_region[country]': 'US', 'tax_region[state]': 'NY', 'tax_region[postal_code]': '10001', 'tax_region[line1]': '350 5th Ave', 'tax_region[city]': 'New York', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
tr2 = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb_us, headers=sh, timeout=30)
print(f'tax US: {tr2.status_code}', flush=True)
if tr2.status_code == 200:
    td = tr2.json(); ck2 = td.get('init_checksum', ck); amt2 = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or amt)
    print(f'  amt={amt2} ck_updated={ck2 != ck}', flush=True)
print('\n=== DONE ===', flush=True)

