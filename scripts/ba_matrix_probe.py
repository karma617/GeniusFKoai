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

# 测试不同 country/currency 组合在 US 代理下创建 checkout，看 amount 和 pmt
matrix = [
    ('US', 'USD'), ('BR', 'USD'), ('BR', 'BRL'),
    ('JP', 'JPY'), ('JP', 'USD'),
    ('GB', 'GBP'), ('DE', 'EUR'), ('FR', 'EUR'),
    ('IE', 'EUR'), ('NL', 'EUR'), ('SG', 'USD'),
    ('AU', 'AUD'), ('CA', 'CAD'), ('KR', 'USD'),
    ('MX', 'USD'), ('IN', 'USD'),
]

print('=== US proxy + different checkout countries ===', flush=True)
p = fetch_proxy('US')
s = make_session(p)
h = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json', 'oai-language': 'zh-CN', 'cookie': COOKIE, 'Origin': 'https://chatgpt.com', 'Referer': 'https://chatgpt.com/'}
sh = {'Origin': 'https://pay.openai.com', 'Referer': 'https://pay.openai.com/', 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}

for country, currency in matrix:
    try:
        pl = {'plan_name': 'chatgptplusplan', 'billing_details': {'country': country, 'currency': currency}, 'entry_point': 'all_plans_pricing_modal', 'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False}, 'checkout_ui_mode': 'custom'}
        r = s.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
        if r.status_code != 200:
            print(f'{country}/{currency}: checkout {r.status_code}', flush=True)
            continue
        data = r.json(); cs = data.get('checkout_session_id',''); pk = data.get('publishable_key','') or STRIPE_PK; ent = data.get('processor_entity','') or 'openai_llc'
        if not cs:
            print(f'{country}/{currency}: no cs', flush=True)
            continue
        ib = {'browser_locale': 'en-US', 'browser_timezone': 'Asia/Shanghai', 'elements_session_client[client_betas][0]': 'custom_checkout_server_updates_1', 'elements_session_client[client_betas][1]': 'custom_checkout_manual_approval_1', 'elements_session_client[elements_init_source]': 'custom_checkout', 'elements_session_client[referrer_host]': 'chatgpt.com', 'elements_session_client[stripe_js_id]': str(uuid.uuid4()), 'elements_session_client[locale]': 'en', 'elements_session_client[is_aggregation_expected]': 'false', 'elements_options_client[saved_payment_method][enable_save]': 'never', 'elements_options_client[saved_payment_method][enable_redisplay]': 'never', 'key': pk, '_stripe_version': STRIPE_INIT_VERSION}
        ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        if ir.status_code != 200:
            print(f'{country}/{currency}: init {ir.status_code}', flush=True)
            continue
        init = ir.json()
        pmt = init.get('payment_method_types', [])
        amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '?')
        zero = (amt == '0')
        paypal = 'paypal' in [str(x).lower() for x in pmt] if isinstance(pmt, list) else False
        print(f'{country}/{currency}: ent={ent} amt={amt} zero={zero} paypal={paypal} pmt={pmt}', flush=True)
        time.sleep(0.5)
    except Exception as e:
        print(f'{country}/{currency}: err {str(e)[:80]}', flush=True)
        time.sleep(0.5)

print('\n=== DONE ===', flush=True)
