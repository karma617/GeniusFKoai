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

ADDRS = {
    'US': {'name':'John Smith','email':'test@example.com','country':'US','state':'NY','city':'New York','postal_code':'10001','line1':'350 5th Ave'},
    'IE': {'name':'Sean Murphy','email':'test@example.com','country':'IE','state':'','city':'Dublin','postal_code':'D01 F5P2','line1':'1 Grafton Street'},
    'JP': {'name':'Taro Yamada','email':'test@example.com','country':'JP','state':'','city':'Tokyo','postal_code':'100-0001','line1':'1-1 Chiyoda'},
    'BR': {'name':'Joao Silva','email':'test@example.com','country':'BR','state':'SP','city':'Sao Paulo','postal_code':'01310-100','line1':'Av Paulista 1000'},
}

_ds = cffi.Session(impersonate='chrome110')

def fetch_proxy(region):
    last = ''
    for _ in range(4):
        try:
            r = _ds.get(KOOKEEY_API.format(region=region), timeout=15)
            parts = r.text.strip().split(':')
            if len(parts) == 4:
                return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
            last = r.text[:80]
        except Exception as e:
            last = str(e)[:80]
        time.sleep(1)
    raise ValueError('proxy fail ' + region + ' ' + last)

def make_session(proxy_url):
    return cffi.Session(impersonate='chrome110', proxy=proxy_url, curl_options={CurlOpt.PRE_PROXY: PRE_PROXY})

with open('scripts/_active_token.json', encoding='utf-8') as f:
    d = json.load(f)
TOKEN = d['token']; COOKIE = d.get('cookie') or ''

def find_redirect(obj):
    if not isinstance(obj, dict):
        return ''
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
                    if url:
                        return url
            for x in v.values():
                r = _s(x)
                if r:
                    return r
        if isinstance(v, list):
            for x in v:
                r = _s(x)
                if r:
                    return r
        return ''
    for k in ('setup_intent', 'payment_intent'):
        r = _s(obj.get(k) or {})
        if r:
            return r
    return _s(obj)

sh = {'Origin':'https://pay.openai.com','Referer':'https://pay.openai.com/','Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}

def probe(label, proxy_region, country, currency, full_flow=False):
    out = {'label': label, 'proxy': proxy_region, 'country': country, 'currency': currency}
    try:
        p = fetch_proxy(proxy_region)
        s = make_session(p)
        h = {
            'Authorization': 'Bearer ' + TOKEN,
            'Content-Type': 'application/json',
            'oai-language': 'zh-CN',
            'Origin': 'https://chatgpt.com',
            'Referer': 'https://chatgpt.com/',
        }
        if COOKIE:
            h['cookie'] = COOKIE
        pl = {
            'plan_name': 'chatgptplusplan',
            'billing_details': {'country': country, 'currency': currency},
            'entry_point': 'all_plans_pricing_modal',
            'promo_campaign': {'promo_campaign_id': 'plus-1-month-free', 'is_coupon_from_query_param': False},
            'checkout_ui_mode': 'custom',
        }
        r = s.post(PAYMENT_CHECKOUT_URL, headers=h, json=pl, timeout=30)
        out['checkout'] = r.status_code
        if r.status_code != 200:
            out['checkout_body'] = r.text[:200]
            return out
        data = r.json()
        cs = data.get('checkout_session_id') or ''
        pk = data.get('publishable_key') or STRIPE_PK
        ent = data.get('processor_entity') or 'openai_llc'
        out['ent'] = ent
        out['cs'] = cs[:24]
        if not cs:
            out['err'] = 'no cs'
            return out
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
        ir = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/init', data=ib, headers=sh, timeout=30)
        out['init'] = ir.status_code
        if ir.status_code != 200:
            out['init_body'] = ir.text[:200]
            return out
        init = ir.json()
        ck = init.get('init_checksum', '')
        cid = str(init.get('config_id') or '')
        pmt = init.get('payment_method_types') or []
        amt = str((init.get('elements_options') or {}).get('amount') or (init.get('invoice') or {}).get('amount_due') or '?')
        out['pmt'] = pmt
        out['amt_init'] = amt
        out['paypal'] = 'paypal' in [str(x).lower() for x in pmt] if isinstance(pmt, list) else False
        out['zero'] = (str(amt) == '0')
        if not full_flow:
            return out
        if not out['paypal']:
            out['err'] = 'no paypal'
            return out
        addr = ADDRS.get(country, ADDRS['US'])
        tb = {
            'eid': str(uuid.uuid4()),
            'tax_region[country]': addr['country'],
            'tax_region[postal_code]': addr['postal_code'],
            'tax_region[line1]': addr['line1'],
            'tax_region[city]': addr['city'],
            'key': pk, '_stripe_version': STRIPE_INIT_VERSION,
        }
        if addr.get('state'):
            tb['tax_region[state]'] = addr['state']
        tr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb, headers=sh, timeout=30)
        out['tax'] = tr.status_code
        if tr.status_code == 200:
            td = tr.json()
            ck = td.get('init_checksum', ck)
            amt = str((td.get('elements_options') or {}).get('amount') or (td.get('invoice') or {}).get('amount_due') or amt)
            out['amt_tax'] = amt
            out['zero'] = (str(amt) == '0')
        else:
            out['tax_body'] = tr.text[:180]
        ctx = {
            'stripe_js_id': str(uuid.uuid4()),
            'elements_session_id': 'es_' + uuid.uuid4().hex[:11],
            'elements_session_config_id': cid or str(uuid.uuid4()),
            'config_id': cid,
            'init_checksum': ck,
            'locale': 'en',
            'runtime_version': STRIPE_RUNTIME_VERSION,
        }
        pmb = {
            'billing_details[name]': addr['name'],
            'billing_details[email]': addr['email'],
            'billing_details[address][country]': addr['country'],
            'billing_details[address][line1]': addr['line1'],
            'billing_details[address][city]': addr['city'],
            'billing_details[address][postal_code]': addr['postal_code'],
            'type': 'paypal',
            'payment_user_agent': 'stripe.js/' + STRIPE_RUNTIME_VERSION + '; stripe-js-v3/' + STRIPE_RUNTIME_VERSION + '; payment-element; deferred-intent',
            'referrer': 'https://chatgpt.com',
            'time_on_page': '35000',
            'client_attribution_metadata[checkout_session_id]': cs,
            'client_attribution_metadata[client_session_id]': ctx['stripe_js_id'],
            'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''),
            'client_attribution_metadata[merchant_integration_source]': 'elements',
            'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
            'client_attribution_metadata[merchant_integration_version]': '2021',
            'client_attribution_metadata[payment_intent_creation_flow]': 'deferred',
            'client_attribution_metadata[payment_method_selection_flow]': 'automatic',
            'key': pk, '_stripe_version': STRIPE_INIT_VERSION,
        }
        if addr.get('state'):
            pmb['billing_details[address][state]'] = addr['state']
        pmr = s.post(f'{STRIPE_API_BASE}/payment_methods', data=pmb, headers=sh, timeout=20)
        out['pm'] = pmr.status_code
        if pmr.status_code != 200:
            out['pm_body'] = pmr.text[:180]
            return out
        pm = str(pmr.json().get('id') or '')
        surl = 'https://chatgpt.com/checkout/verify?stripe_session_id=' + cs + '&processor_entity=' + ent + '&plan_type=plus'
        rurl_base = 'https://pay.openai.com/c/pay/' + cs + '?success_return_url=' + surl
        cb = {
            'guid': uuid.uuid4().hex, 'muid': uuid.uuid4().hex, 'sid': uuid.uuid4().hex,
            'payment_method': pm, 'init_checksum': ck, 'version': STRIPE_RUNTIME_VERSION,
            'expected_amount': amt, 'expected_payment_method_type': 'paypal', 'return_url': rurl_base,
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
            'client_attribution_metadata[checkout_config_id]': ctx.get('config_id',''),
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
            'key': pk, '_stripe_version': STRIPE_INIT_VERSION,
        }
        cr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}/confirm', data=cb, headers=sh, timeout=30)
        out['confirm'] = cr.status_code
        if cr.status_code != 200:
            out['confirm_body'] = cr.text[:180]
            return out
        cd = cr.json()
        sa = cd.get('submission_attempt') or {}
        out['confirm_sa'] = sa.get('state','') if isinstance(sa, dict) else ''
        pmr_url = find_redirect(cd)
        out['redirect_confirm'] = bool(pmr_url)
        if not pmr_url:
            try:
                s.post('https://chatgpt.com/backend-api/sentinel/ping', json={}, headers={'x-openai-target-path':'/backend-api/sentinel/ping','x-openai-target-route':'/backend-api/sentinel/ping'}, timeout=4)
            except Exception:
                pass
            pt = '/backend-api/payments/checkout/approve'
            ah = {
                'Authorization': 'Bearer ' + TOKEN,
                'Content-Type': 'application/json',
                'oai-language': 'zh-CN',
                'Referer': 'https://chatgpt.com/checkout/' + ent + '/' + cs,
                'x-openai-target-path': pt,
                'x-openai-target-route': pt,
            }
            if COOKIE:
                ah['cookie'] = COOKIE
            ar = s.post('https://chatgpt.com' + pt, json={'checkout_session_id': cs, 'processor_entity': ent}, headers=ah, timeout=20)
            out['approve'] = ar.status_code
            try:
                ab = ar.json()
                out['approve_result'] = ab.get('result', '?')
            except Exception:
                out['approve_result'] = '?'
                out['approve_body'] = ar.text[:120]
            if out.get('approve_result') == 'approved':
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
                    'key': pk, '_stripe_version': STRIPE_INIT_VERSION,
                }
                ph = {'Origin':'https://pay.openai.com','Referer':'https://pay.openai.com/','Accept':'application/json'}
                for i in range(8):
                    time.sleep(1.5)
                    pr = s.get(f'{STRIPE_API_BASE}/payment_pages/{cs}', params=pp, headers=ph, timeout=8)
                    if pr.status_code != 200:
                        continue
                    pd = pr.json()
                    pmr_url = find_redirect(pd)
                    sa2 = pd.get('submission_attempt') or {}
                    pst = sa2.get('state','') if isinstance(sa2, dict) else ''
                    out['poll_sa'] = pst
                    if pmr_url:
                        rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer':'https://pay.openai.com/'})
                        fin = str(getattr(rg, 'url', ''))
                        bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
                        if bm:
                            out['ba'] = bm.group(1)
                            out['ba_url'] = fin[:180]
                            out['result'] = 'SUCCESS'
                        else:
                            out['result'] = 'NO_BA'
                            out['final_url'] = fin[:120]
                        return out
                    if pst == 'failed':
                        pe = sa2.get('error', {}) if isinstance(sa2, dict) else {}
                        out['result'] = 'FAILED'
                        out['poll_error'] = pe
                        return out
                out['result'] = 'TIMEOUT'
            else:
                out['result'] = 'APPROVE_' + str(out.get('approve_result'))
        else:
            rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer':'https://pay.openai.com/'})
            fin = str(getattr(rg, 'url', ''))
            bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
            if bm:
                out['ba'] = bm.group(1)
                out['ba_url'] = fin[:180]
                out['result'] = 'SUCCESS_CONFIRM'
            else:
                out['result'] = 'NO_BA_CONFIRM'
                out['final_url'] = fin[:120]
    except Exception as e:
        out['err'] = str(e)[:160]
    return out

cases = [
    # 只看 amount/pmt
    ('US_proxy_US_USD', 'US', 'US', 'USD', False),
    ('US_proxy_IE_EUR', 'US', 'IE', 'EUR', False),
    ('US_proxy_JP_JPY', 'US', 'JP', 'JPY', False),
    ('US_proxy_BR_BRL', 'US', 'BR', 'BRL', False),
    ('JP_proxy_JP_JPY', 'JP', 'JP', 'JPY', False),
    ('JP_proxy_US_USD', 'JP', 'US', 'USD', False),
    ('BR_proxy_BR_BRL', 'BR', 'BR', 'BRL', False),
    # 有 paypal 的全流程
    ('FULL_US_US', 'US', 'US', 'USD', True),
    ('FULL_US_IE', 'US', 'IE', 'EUR', True),
]

results = []
print('=== free-trial token probe ===', flush=True)
print('email=', d.get('email'), flush=True)
for label, pr, c, cur, full in cases:
    r = probe(label, pr, c, cur, full_flow=full)
    results.append(r)
    print(json.dumps(r, ensure_ascii=False), flush=True)
    time.sleep(0.8)

with open('scripts/ba_freetrial_probe_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print('=== DONE ===', flush=True)
