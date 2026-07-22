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

sh = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}

# 完整流程
p = fetch_proxy('US')
s = make_session(p)
headers = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
payload = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': 'US', 'currency': 'USD'}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
r = s.post(PAYMENT_CHECKOUT_URL, headers=headers, json=payload, timeout=30)
data = r.json()
cs_id = data.get('checkout_session_id','')
pk = data.get('publishable_key','') or STRIPE_PK
entity = data.get('processor_entity','') or 'openai_llc'
print('cs_id=' + cs_id[:35])

# init
ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
init_r = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/init', data=ib, headers=sh, timeout=30)
init = init_r.json()
checksum = init.get('init_checksum','')
config_id = str(init.get('config_id') or '')
print('init: amt=' + str((init.get('elements_options') or {}).get('amount')) + ' pmt=' + str(init.get('payment_method_types')))

# tax_region
tax_body = {'eid': str(uuid.uuid4()), 'tax_region[country]': 'US', 'tax_region[state]': 'NY', 'tax_region[postal_code]': '10001', 'tax_region[line1]': '350 5th Ave', 'tax_region[city]': 'New York', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
tax_r = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}', data=tax_body, headers=sh, timeout=30)
td = tax_r.json()
checksum = td.get('init_checksum', checksum)
amt = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or '0')
print('tax: amt=' + amt)

# create pm
ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': 'elements_session_' + uuid.uuid4().hex[:11], 'elements_session_config_id': config_id or str(uuid.uuid4()), 'config_id': config_id, 'init_checksum': checksum, 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
pm_body = {'billing_details[name]': US_ADDR['name'], 'billing_details[email]': US_ADDR['email'], 'billing_details[address][country]': US_ADDR['country'], 'billing_details[address][line1]': US_ADDR['line1'], 'billing_details[address][city]': US_ADDR['city'], 'billing_details[address][postal_code]': US_ADDR['postal_code'], 'billing_details[address][state]': US_ADDR['state'], 'type': 'paypal', 'payment_user_agent': 'stripe.js/' + STRIPE_RUNTIME_VERSION + '; stripe-js-v3/' + STRIPE_RUNTIME_VERSION + '; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
pm_r = s.post(f'{STRIPE_API_BASE}/payment_methods', data=pm_body, headers=sh, timeout=20)
pm_id = str(pm_r.json().get('id') or '')
print('pm=' + pm_id[:25])

# confirm
success_url = 'https://chatgpt.com/checkout/verify?stripe_session_id=' + cs_id + '&processor_entity=' + entity + '&plan_type=plus'
return_url = 'https://pay.openai.com/c/pay/' + cs_id + '?success_return_url=' + success_url
cb = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm_id, 'init_checksum': checksum, 'version': STRIPE_RUNTIME_VERSION, 'expected_amount': amt, 'expected_payment_method_type': 'paypal', 'return_url': return_url, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': 'en', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
cr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/confirm', data=cb, headers=sh, timeout=30)
cdata = cr.json()
sa = cdata.get('submission_attempt') or {}
print('confirm: ' + str(cr.status_code) + ' sa=' + str(sa.get('state')))

# approve
try: s.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
except: pass
path = '/backend-api/payments/checkout/approve'
ah = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': 'https://chatgpt.com/checkout/' + entity + '/' + cs_id, 'x-openai-target-path': path, 'x-openai-target-route': path}
ar = s.post('https://chatgpt.com' + path, json={'checkout_session_id': cs_id, 'processor_entity': entity}, headers=ah, timeout=20)
print('approve: ' + str(ar.status_code) + ' ' + ar.text[:100])

# poll for pm-redirects URL
pp = {'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[session_id]': 'elements_session_' + uuid.uuid4().hex[:11], 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
ph = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'}
pm_redirect_url = ''
for i in range(10):
    time.sleep(2)
    pr = s.get(f'{STRIPE_API_BASE}/payment_pages/{cs_id}', params=pp, headers=ph, timeout=5)
    if pr.status_code == 200:
        pdata = pr.json()
        rurl = find_redirect(pdata)
        if rurl:
            pm_redirect_url = rurl
            print('pm-redirects URL: ' + rurl[:120])
            break
        psa = pdata.get('submission_attempt') or {}
        pstate = psa.get('state','') if isinstance(psa, dict) else ''
        if pstate == 'failed':
            perr = psa.get('error',{}) if isinstance(psa, dict) else {}
            print('poll[' + str(i) + ']: FAILED err=' + str(perr)[:120])
            break
        print('poll[' + str(i) + ']: sa=' + str(pstate))
    else:
        print('poll[' + str(i) + ']: ' + str(pr.status_code))

if not pm_redirect_url:
    print('NO pm-redirects URL found')
    sys.exit(1)

# 关键步骤: GET pm-redirects URL 跟随 302 拿 BA token
print('\n--- Following pm-redirects URL to get BA token ---')
# 用同一 session + 代理 GET pm-redirects URL
# 不自动跟随 redirect，手动跟
r2 = s.get(pm_redirect_url, allow_redirects=False, timeout=20, headers={'Referer': 'https://pay.openai.com/', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'})
print('GET pm-redirects: status=' + str(r2.status_code))
if r2.status_code in (301, 302, 303, 307, 308):
    location = r2.headers.get('Location', '')
    print('  Location: ' + location[:150])
    # 跟随 Location
    r3 = s.get(location, allow_redirects=True, timeout=20, headers={'Referer': pm_redirect_url})
    final_url = str(r3.url)
    print('  Final URL: ' + final_url[:200])
    # 从 URL 抽 ba_token
    ba_match = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', final_url)
    ec_match = re.search(r'token=(EC-[A-Za-z0-9]+)', final_url)
    if ba_match:
        print('  BA TOKEN: ' + ba_match.group(1))
    if ec_match:
        print('  EC TOKEN: ' + ec_match.group(1))
    # 检查所有 redirect 历史
    history = getattr(r3, 'history', [])
    for h in history:
        hurl = str(getattr(h, 'url', ''))
        ba_m = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', hurl)
        if ba_m:
            print('  BA in history: ' + ba_m.group(1))
        print('  redirect: ' + str(h.status_code) + ' -> ' + hurl[:150])
else:
    print('  body: ' + r2.text[:300])
    # 可能在响应 body 里
    ba_match = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', r2.text)
    if ba_match:
        print('  BA in body: ' + ba_match.group(1))
