import sys, os, json, time, uuid
sys.path.insert(0, '.')
os.environ['PYTHONIOENCODING'] = 'utf-8'
from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt

PRE_PROXY = 'socks5h://127.0.0.1:7897'
KOOKEEY_API = 'https://www.kookeey.com/pickdynamicips?t=2&auth=pwd&format=4&n=1&p=http&gate=global&g={region}&r=-1&type=txt&sign=874086cfbdb353e32d67a6dbebd498af&accessid=8239626&upf=1,1&dl=%5Cr%5Cn'
STRIPE_API_BASE = 'https://api.stripe.com/v1'
STRIPE_PK = 'pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n'
STRIPE_INIT_VERSION = '2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1'

_ds = cffi.Session(impersonate='chrome110')

def fetch_proxy(region):
    for _ in range(4):
        try:
            r = _ds.get(KOOKEEY_API.format(region=region), timeout=15)
            parts = r.text.strip().split(':')
            if len(parts) == 4:
                return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
        except Exception:
            pass
        time.sleep(1)
    raise ValueError('proxy fail ' + region)

def make_session(proxy_url):
    return cffi.Session(impersonate='chrome110', proxy=proxy_url, curl_options={CurlOpt.PRE_PROXY: PRE_PROXY})

with open('scripts/_active_token.json', encoding='utf-8') as f:
    d = json.load(f)
TOKEN = d['token']

def extract_amount(init):
    keys = []
    eo = init.get('elements_options') or {}
    inv = init.get('invoice') or {}
    for k, v in [
        ('elements_options.amount', eo.get('amount')),
        ('elements_options.total.amount', ((eo.get('total') or {}) if isinstance(eo.get('total'), dict) else {}).get('amount')),
        ('invoice.amount_due', inv.get('amount_due')),
        ('invoice.total', inv.get('total')),
        ('invoice.subtotal', inv.get('subtotal')),
        ('amount_total', init.get('amount_total')),
        ('total.amount', ((init.get('total') or {}) if isinstance(init.get('total'), dict) else {}).get('amount')),
        ('session.amount_total', ((init.get('session') or {}) if isinstance(init.get('session'), dict) else {}).get('amount_total')),
    ]:
        keys.append((k, v))
    # deep scan numeric amount-like fields shallowly
    cand = []
    for path, v in keys:
        if v is not None and str(v) != '':
            cand.append((path, v))
    return cand

def checkout_init(s, country, currency):
    h = {
        'Authorization': 'Bearer ' + TOKEN,
        'Content-Type': 'application/json',
        'oai-language': 'zh-CN',
        'Origin': 'https://chatgpt.com',
        'Referer': 'https://chatgpt.com/',
    }
    pl = {
        'plan_name': 'chatgptplusplan',
        'billing_details': {'country': country, 'currency': currency},
        'entry_point': 'all_plans_pricing_modal',
        'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False},
        'checkout_ui_mode': 'custom',
    }
    r = s.post('https://chatgpt.com/backend-api/payments/checkout', headers=h, json=pl, timeout=30)
    if r.status_code != 200:
        return {'checkout': r.status_code, 'body': r.text[:250]}
    data = r.json()
    cs = data.get('checkout_session_id') or ''
    pk = data.get('publishable_key') or STRIPE_PK
    ent = data.get('processor_entity') or ''
    # also keep checkout-side promo fields if any
    promo_keys = {k: data.get(k) for k in data.keys() if 'promo' in k.lower() or 'trial' in k.lower() or 'coupon' in k.lower() or 'discount' in k.lower()}
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
    sh = {'Origin':'https://pay.openai.com','Referer':'https://pay.openai.com/','Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}
    ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
    if ir.status_code != 200:
        return {'checkout': 200, 'init': ir.status_code, 'init_body': ir.text[:250], 'ent': ent, 'promo_keys': promo_keys}
    init = ir.json()
    amts = extract_amount(init)
    # scan for trial/promo strings
    blob = json.dumps(init, ensure_ascii=False)
    hits = []
    for kw in ['trial', 'promo', 'coupon', 'discount', 'free', 'amount_due', 'total', 'line_item']:
        if kw in blob.lower():
            hits.append(kw)
    return {
        'checkout': 200,
        'init': 200,
        'ent': ent,
        'cs': cs[:24],
        'pmt': init.get('payment_method_types'),
        'amount_candidates': amts[:12],
        'currency': init.get('currency') or ((init.get('invoice') or {}).get('currency')),
        'promo_keys_checkout': promo_keys,
        'keyword_hits': hits,
        'invoice_keys': list((init.get('invoice') or {}).keys())[:20] if isinstance(init.get('invoice'), dict) else type(init.get('invoice')).__name__,
        'elements_options_keys': list((init.get('elements_options') or {}).keys())[:20] if isinstance(init.get('elements_options'), dict) else type(init.get('elements_options')).__name__,
    }

def get_account_flags(s):
    h = {
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/json',
        'oai-language': 'zh-CN',
        'Referer': 'https://chatgpt.com/',
    }
    endpoints = [
        '/backend-api/accounts/check',
        '/backend-api/accounts/check/v4-2023-04-27',
        '/backend-api/subscriptions',
        '/backend-api/payments/customer_portal_url',
        '/backend-api/settings/user',
    ]
    out = {}
    for path in endpoints:
        try:
            r = s.get('https://chatgpt.com' + path, headers={**h, 'x-openai-target-path': path, 'x-openai-target-route': path}, timeout=20)
            out[path] = {'status': r.status_code}
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    out[path]['body'] = r.text[:200]
                    continue
                # extract trial-like fields
                text = json.dumps(data, ensure_ascii=False)
                interesting = {}
                def walk(obj, prefix=''):
                    if isinstance(obj, dict):
                        for k,v in obj.items():
                            p = f'{prefix}.{k}' if prefix else k
                            kl = str(k).lower()
                            if any(x in kl for x in ['trial','promo','eligible','plan','subscription','plus','billing','discount','coupon']):
                                if isinstance(v, (str,int,float,bool)) or v is None:
                                    interesting[p] = v
                                elif isinstance(v, dict):
                                    interesting[p] = {kk: vv for kk,vv in list(v.items())[:8] if isinstance(vv,(str,int,float,bool)) or vv is None}
                            if isinstance(v, (dict,list)) and prefix.count('.') < 4:
                                walk(v, p)
                    elif isinstance(obj, list) and prefix.count('.') < 4:
                        for i, item in enumerate(obj[:5]):
                            walk(item, f'{prefix}[{i}]')
                walk(data)
                out[path]['interesting'] = interesting
                out[path]['has_trial_word'] = 'trial' in text.lower()
                out[path]['has_eligible_word'] = 'eligible' in text.lower()
            else:
                out[path]['body'] = r.text[:160]
        except Exception as e:
            out[path] = {'err': str(e)[:120]}
    return out

results = {}
print('=== free trial deep probe ===', flush=True)

# 1) account flags via US proxy
for attempt in range(3):
    try:
        p = fetch_proxy('US')
        s = make_session(p)
        results['account_flags'] = get_account_flags(s)
        print('account_flags done', flush=True)
        break
    except Exception as e:
        print('account_flags retry', attempt, e, flush=True)
        time.sleep(1)

# 2) amount deep for US/JP/BR/IE
cases = [
    ('US', 'US', 'USD'),
    ('US', 'IE', 'EUR'),
    ('JP', 'JP', 'JPY'),
    ('BR', 'BR', 'BRL'),
    ('US', 'JP', 'JPY'),
    ('US', 'BR', 'BRL'),
]
amount_rows = []
for proxy, country, currency in cases:
    row = {'proxy': proxy, 'country': country, 'currency': currency}
    try:
        s = make_session(fetch_proxy(proxy))
        detail = checkout_init(s, country, currency)
        row.update(detail)
    except Exception as e:
        row['err'] = str(e)[:160]
    amount_rows.append(row)
    print(json.dumps(row, ensure_ascii=False)[:500], flush=True)
    time.sleep(0.6)

results['amount_rows'] = amount_rows
with open('scripts/ba_freetrial_deep_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print('=== DONE ===', flush=True)
