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
_direct_session = None
def get_direct_session():
    global _direct_session
    if _direct_session is None:
        _direct_session = cffi.Session(impersonate='chrome110')
    return _direct_session
def fetch_proxy(region):
    s = get_direct_session()
    r = s.get(KOOKEEY_API.format(region=region), timeout=15)
    parts = r.text.strip().split(':')
    return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
def make_session(proxy_url):
    return cffi.Session(impersonate='chrome110', proxy=proxy_url, curl_options={CurlOpt.PRE_PROXY: PRE_PROXY})
with open('scripts/_active_token.json') as f:
    d = json.load(f)
TOKEN = d['token']
COOKIE = d['cookie']
EMAIL = d['email']

def create_checkout(s, country, currency):
    headers = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
    payload = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': country, 'currency': currency}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
    return s.post(PAYMENT_CHECKOUT_URL, headers=headers, json=payload, timeout=30)

def stripe_init_custom(s, cs_id, pk):
    body = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
    headers = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
    return s.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/init', data=body, headers=headers, timeout=30)

def stripe_create_pm(s, cs_id, pk, billing, ctx):
    body = {'billing_details[name]': billing['name'], 'billing_details[email]': billing['email'], 'billing_details[address][country]': billing['country'], 'billing_details[address][line1]': billing['line1'], 'billing_details[address][city]': billing['city'], 'billing_details[address][postal_code]': billing['postal_code'], 'billing_details[address][state]': billing['state'], 'type': 'paypal', 'payment_user_agent': f'stripe.js/{STRIPE_RUNTIME_VERSION}; stripe-js-v3/{STRIPE_RUNTIME_VERSION}; payment-element; deferred-intent', 'referrer': 'https://chatgpt.com', 'time_on_page': '35000', 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[merchant_integration_source]': 'elements', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': '2021', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
    headers = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
    return s.post(f'{STRIPE_API_BASE}/payment_methods', data=body, headers=headers, timeout=20)

def stripe_confirm_custom(s, cs_id, pk, init, pm_id, ctx, billing_country, processor_entity, expected_amount):
    hosted_url = str(init.get('url') or init.get('stripe_hosted_url') or '')
    if hosted_url and 'checkout.stripe.com' in hosted_url:
        hosted_url = hosted_url.replace('checkout.stripe.com', 'pay.openai.com')
    if not hosted_url:
        hosted_url = f'https://pay.openai.com/c/pay/{cs_id}'
    entity = processor_entity or ('openai_llc' if billing_country == 'US' else 'openai_ie')
    success_url = f'https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus'
    parsed = urlsplit(hosted_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault('success_return_url', success_url)
    return_url = urlunsplit((parsed.scheme or 'https', parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    body = {'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex, 'payment_method': pm_id, 'init_checksum': str(init.get('init_checksum') or ctx.get('init_checksum') or ''), 'version': ctx['runtime_version'], 'expected_amount': str(expected_amount), 'expected_payment_method_type': 'paypal', 'return_url': return_url, 'elements_session_client[session_id]': ctx['elements_session_id'], 'elements_session_client[locale]': ctx['locale'], 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[stripe_js_id]': ctx['stripe_js_id'], 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'], 'client_attribution_metadata[checkout_session_id]': cs_id, 'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''), 'client_attribution_metadata[elements_session_id]': ctx['elements_session_id'], 'client_attribution_metadata[elements_session_config_id]': ctx['elements_session_config_id'], 'client_attribution_metadata[merchant_integration_source]': 'checkout', 'client_attribution_metadata[merchant_integration_subtype]': 'payment-element', 'client_attribution_metadata[merchant_integration_version]': 'custom', 'client_attribution_metadata[payment_intent_creation_flow]': 'deferred', 'client_attribution_metadata[payment_method_selection_flow]': 'automatic', 'client_attribution_metadata[merchant_integration_additional_elements][0]': 'payment', 'client_attribution_metadata[merchant_integration_additional_elements][1]': 'address', 'consent[terms_of_service]': 'accepted', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
    headers = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
    return s.post(f'{STRIPE_API_BASE}/payment_pages/{cs_id}/confirm', data=body, headers=headers, timeout=30)

def chatgpt_approve(s, cs_id, processor_entity):
    try:
        s.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path': '/backend-api/sentinel/ping', 'x-openai-target-route': '/backend-api/sentinel/ping'}, timeout=4)
    except: pass
    path = '/backend-api/payments/checkout/approve'
    headers = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Referer': f'https://chatgpt.com/checkout/{processor_entity}/{cs_id}', 'x-openai-target-path': path, 'x-openai-target-route': path}
    return s.post(f'https://chatgpt.com{path}', json={'checkout_session_id': cs_id, 'processor_entity': processor_entity}, headers=headers, timeout=20)

def stripe_poll(s, cs_id, pk, timeout_s=25):
    deadline = time.monotonic() + max(1.0, timeout_s)
    params = {'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[session_id]': f'elements_session_{uuid.uuid4().hex[:11]}', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
    headers = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Accept': 'application/json'}
    while time.monotonic() < deadline:
        r = s.get(f'{STRIPE_API_BASE}/payment_pages/{cs_id}', params=params, headers=headers, timeout=5)
        if r.status_code == 200:
            redirect = find_redirect(r.json())
            if redirect:
                return redirect
        time.sleep(0.75)
    return ''

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

def extract_amount(init):
    inv = init.get('invoice') or {}
    eo = init.get('elements_options') or {}
    amt = eo.get('amount') if isinstance(eo, dict) else None
    inv_amt = inv.get('amount_due') if isinstance(inv, dict) else None
    return str(amt or inv_amt or '0')

print('=== US custom checkout full flow ===')
try:
    p_us = fetch_proxy('US')
    s = make_session(p_us)
    r = create_checkout(s, 'US', 'USD')
    if r.status_code != 200:
        print(f'checkout fail: {r.status_code} {r.text[:200]}')
        sys.exit(1)
    data = r.json()
    cs_id = data.get('checkout_session_id','')
    pk = data.get('publishable_key','') or STRIPE_PK
    processor_entity = data.get('processor_entity','') or 'openai_llc'
    print(f'cs_id={cs_id[:35]} pk={pk[:20]} entity={processor_entity}')
    init_r = stripe_init_custom(s, cs_id, pk)
    init = init_r.json() if init_r.status_code == 200 else {}
    pmt = init.get('payment_method_types')
    expected_amount = extract_amount(init)
    config_id = str(init.get('config_id') or '')
    print(f'init: pmt={pmt} amt={expected_amount} status={init_r.status_code}')
    if init_r.status_code != 200:
        print(f'  body: {init_r.text[:300]}')
        sys.exit(1)
    ctx = {'stripe_js_id': str(uuid.uuid4()), 'elements_session_id': f'elements_session_{uuid.uuid4().hex[:11]}', 'elements_session_config_id': config_id or str(uuid.uuid4()), 'config_id': config_id, 'init_checksum': str(init.get('init_checksum') or ''), 'locale': 'en', 'runtime_version': STRIPE_RUNTIME_VERSION}
    pm_r = stripe_create_pm(s, cs_id, pk, US_ADDR, ctx)
    pm_data = pm_r.json() if pm_r.status_code == 200 else {}
    pm_id = str(pm_data.get('id') or '')
    print(f'pm: id={pm_id[:25]} status={pm_r.status_code}')
    if not pm_id.startswith('pm_'):
        print(f'  body: {pm_r.text[:300]}')
        sys.exit(1)
    confirm_r = stripe_confirm_custom(s, cs_id, pk, init, pm_id, ctx, 'US', processor_entity, expected_amount)
    cdata = confirm_r.json() if confirm_r.status_code == 200 else {}
    print(f'confirm: status={confirm_r.status_code}')
    redirect = find_redirect(cdata)
    if redirect:
        print(f'  BA FOUND (confirm): {redirect[:100]}')
    else:
        sa = cdata.get('submission_attempt') or {}
        sa_state = sa.get('state','') if isinstance(sa, dict) else ''
        print(f'  submission_attempt.state={sa_state}')
        print(f'  top keys: {list(cdata.keys())[:25]}')
        si = cdata.get('setup_intent')
        pi = cdata.get('payment_intent')
        if isinstance(si, dict):
            print(f'  setup_intent: status={si.get("status")} keys={list(si.keys())[:10]}')
        if isinstance(pi, dict):
            print(f'  payment_intent: status={pi.get("status")} keys={list(pi.keys())[:10]}')
        if sa_state == 'requires_approval':
            print('  -> requires_approval, calling approve...')
            approve_r = chatgpt_approve(s, cs_id, processor_entity)
            print(f'  approve: status={approve_r.status_code}')
            if approve_r.status_code == 200:
                adata = approve_r.json()
                redirect = find_redirect(adata)
                if redirect:
                    print(f'  BA FOUND (approve): {redirect[:100]}')
                else:
                    print(f'  approve body keys: {list(adata.keys())[:15]}')
            else:
                print(f'  approve body: {approve_r.text[:200]}')
        if not redirect:
            print('  -> polling Stripe for redirect...')
            redirect = stripe_poll(s, cs_id, pk, timeout_s=20)
            if redirect:
                print(f'  BA FOUND (poll): {redirect[:100]}')
            else:
                print('  NO BA found after poll')
except Exception as e:
    import traceback; traceback.print_exc()
