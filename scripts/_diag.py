import sys, os, json, sqlite3, time, base64, uuid, re
sys.path.insert(0, '.')
os.environ['PYTHONIOENCODING'] = 'utf-8'
from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

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
    r = _ds.get(KOOKEEY_API.format(region=region), timeout=15)
    parts = r.text.strip().split(':')
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
            m = PM_REDIRECT_RE.search(v)
            return m.group(0) if m else ''
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

p = fetch_proxy('US')
s = make_session(p)
headers = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
payload = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': 'US', 'currency': 'USD'}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
r = s.post(PAYMENT_CHECKOUT_URL, headers=headers, json=payload, timeout=30)
data = r.json()
cs_id = data.get('checkout_session_id','')
pk = data.get('publishable_key','') or STRIPE_PK
entity = data.get('processor_entity','') or 'openai_llc'
print(f'cs_id={cs_id[:35]} entity={entity}')

# init custom
ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
sh = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
init_r = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/init', data=ib, headers=sh, timeout=30)
init = init_r.json()
amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '0')
checksum = init.get('init_checksum','')
config_id = str(init.get('config_id') or '')
print(f'init: pmt={init.get("payment_method_types")} amt={amt}')

# create pm
ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': f'elements_session_{uuid.uuid4().hex[:11]}', 'elements_session_config_id': config_id or str(uuid.uuid4()), 'config_id': config_id, 'init_checksum': checksum, 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
pm_body = {'billing_details[name]': US_ADDR['name'], 'billing_details[email]': US_ADDR['email'], 'billing_details[address][country]': US_ADDR['country'], 'billing_details[address][line1]': US_ADDR['line1'], 'billing_details[address][city]': US_ADDR['city'], 'billing_details[address][postal_code]': US_ADDR['postal_code'], 'billing_details[address][state]': US_ADDR['state'], 'type': 'paypal', 'payment_user_agent': f'stripe.js/{STRIPE_RUNTIME_VERSION}; stripe-js-v3/{STRIPE_RUNTIME_VERSION}; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
pm_r = s.post(f'{STRIPE_API_BASE}/payment_methods', data=pm_body, headers=sh, timeout=20)
pm_id = str(pm_r.json().get('id') or '')
print(f'pm={pm_id[:25]}')

# confirm
success_url = f'https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus'
return_url = f'https://pay.openai.com/c/pay/{cs_id}?success_return_url={success_url}'
cb = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm_id, 'init_checksum': checksum, 'version': STRIPE_RUNTIME_VERSION, 'expected_amount': amt, 'expected_payment_method_type': 'paypal', 'return_url': return_url, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': 'en', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
cr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/confirm', data=cb, headers=sh, timeout=30)
cdata = cr.json()
print(f'confirm: {cr.status_code}')
sa = cdata.get('submission_attempt') or {}
print(f'sa.state={sa.get("state") if isinstance(sa,dict) else "N/A"}')
si = cdata.get('setup_intent') or {}
pi = cdata.get('payment_intent') or {}
if isinstance(si, dict) and si:
    print(f'si: id={str(si.get("id",""))[:25]} status={si.get("status")} na_type={si.get("next_action",{}).get("type") if isinstance(si.get("next_action"),dict) else si.get("next_action")}')
if isinstance(pi, dict) and pi:
    print(f'pi: id={str(pi.get("id",""))[:25]} status={pi.get("status")} na_type={pi.get("next_action",{}).get("type") if isinstance(pi.get("next_action"),dict) else pi.get("next_action")}')

# approve
try: s.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
except: pass
path = '/backend-api/payments/checkout/approve'
ah = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': f'https://chatgpt.com/checkout/{entity}/{cs_id}', 'x-openai-target-path': path, 'x-openai-target-route': path}
ar = s.post(f'https://chatgpt.com{path}', json={'checkout_session_id': cs_id, 'processor_entity': entity}, headers=ah, timeout=20)
print(f'approve: {ar.status_code}')
print(f'approve body: {ar.text[:300]}')

# poll 5 times with 2s delay
pp = {'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[session_id]': f'elements_session_{uuid.uuid4().hex[:11]}', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
ph = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'}
for i in range(8):
    time.sleep(2)
    pr = s.get(f'{STRIPE_API_BASE}/payment_pages/{cs_id}', params=pp, headers=ph, timeout=5)
    if pr.status_code == 200:
        pdata = pr.json()
        psa = pdata.get('submission_attempt') or {}
        psi = pdata.get('setup_intent') or {}
        ppi = pdata.get('payment_intent') or {}
        sa_state = psa.get('state') if isinstance(psa, dict) else 'N/A'
        si_status = psi.get('status') if isinstance(psi, dict) else 'N/A'
        pi_status = ppi.get('status') if isinstance(ppi, dict) else 'N/A'
        print(f'poll[{i}]: sa={sa_state} si={si_status} pi={pi_status}')
        rurl = find_redirect(pdata)
        if rurl:
            print(f'  BA FOUND: {rurl[:100]}')
            break
    else:
        print(f'poll[{i}]: {pr.status_code}')
else:
    print('NO BA after 8 polls')
