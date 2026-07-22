import sys, os, json, time, uuid, re
from collections import Counter
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

# TR billing templates
TR_ADDR = {'name':'Ahmet Yilmaz','email':'test@example.com','country':'TR','state':'','city':'Istanbul','postal_code':'34000','line1':'Istiklal Caddesi 1'}
US_ADDR = {'name':'John Smith','email':'test@example.com','country':'US','state':'NY','city':'New York','postal_code':'10001','line1':'350 5th Ave'}

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

def extract_amounts(init):
    eo = init.get('elements_options') or {}
    inv = init.get('invoice') or {}
    return {
        'eo_amount': eo.get('amount'),
        'inv_due': inv.get('amount_due'),
        'inv_total': inv.get('total'),
        'inv_subtotal': inv.get('subtotal'),
    }

def try_once(attempt, proxy_region, country, currency, bill_addr, do_full=True):
    res = {
        'attempt': attempt,
        'proxy': proxy_region,
        'country': country,
        'currency': currency,
        'approve_result': '',
        'ba_ok': False,
        'ba': '',
        'ba_url': '',
        'amt': '',
        'zero': False,
        'paypal': False,
        'pmt': None,
        'one_click_trial_eligible': None,
        'promo_campaign': None,
        'err': '',
    }
    try:
        s = make_session(fetch_proxy(proxy_region))
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
        if r.status_code != 200:
            res['err'] = 'checkout ' + str(r.status_code)
            res['body'] = r.text[:160]
            return res
        data = r.json()
        cs = data.get('checkout_session_id') or ''
        pk = data.get('publishable_key') or STRIPE_PK
        ent = data.get('processor_entity') or 'openai_llc'
        res['ent'] = ent
        res['one_click_trial_eligible'] = data.get('one_click_trial_eligible')
        res['promo_campaign'] = data.get('promo_campaign')
        if not cs:
            res['err'] = 'no cs'
            return res
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
        if ir.status_code != 200:
            res['err'] = 'init ' + str(ir.status_code)
            return res
        init = ir.json()
        ck = init.get('init_checksum', '')
        cid = str(init.get('config_id') or '')
        pmt = init.get('payment_method_types') or []
        amts = extract_amounts(init)
        amt = str(amts.get('eo_amount') if amts.get('eo_amount') is not None else amts.get('inv_due') if amts.get('inv_due') is not None else '?')
        res['pmt'] = pmt
        res['amt'] = amt
        res['amts'] = amts
        res['paypal'] = 'paypal' in [str(x).lower() for x in pmt] if isinstance(pmt, list) else False
        res['zero'] = str(amt) == '0'
        if not do_full:
            return res
        if not res['paypal']:
            res['err'] = 'no paypal'
            return res

        tb = {
            'eid': str(uuid.uuid4()),
            'tax_region[country]': bill_addr['country'],
            'tax_region[postal_code]': bill_addr['postal_code'],
            'tax_region[line1]': bill_addr['line1'],
            'tax_region[city]': bill_addr['city'],
            'key': pk, '_stripe_version': STRIPE_INIT_VERSION,
        }
        if bill_addr.get('state'):
            tb['tax_region[state]'] = bill_addr['state']
        tr = s.post(f'{STRIPE_API_BASE}/payment_pages/{cs}', data=tb, headers=sh, timeout=30)
        res['tax'] = tr.status_code
        if tr.status_code == 200:
            td = tr.json()
            ck = td.get('init_checksum', ck)
            amts2 = extract_amounts(td)
            amt = str(amts2.get('eo_amount') if amts2.get('eo_amount') is not None else amts2.get('inv_due') if amts2.get('inv_due') is not None else amt)
            res['amt'] = amt
            res['amts_tax'] = amts2
            res['zero'] = str(amt) == '0'
        else:
            res['tax_body'] = tr.text[:160]

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
            'billing_details[name]': bill_addr['name'],
            'billing_details[email]': bill_addr['email'],
            'billing_details[address][country]': bill_addr['country'],
            'billing_details[address][line1]': bill_addr['line1'],
            'billing_details[address][city]': bill_addr['city'],
            'billing_details[address][postal_code]': bill_addr['postal_code'],
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
        if bill_addr.get('state'):
            pmb['billing_details[address][state]'] = bill_addr['state']
        pmr = s.post(f'{STRIPE_API_BASE}/payment_methods', data=pmb, headers=sh, timeout=20)
        if pmr.status_code != 200:
            res['err'] = 'pm ' + str(pmr.status_code)
            res['pm_body'] = pmr.text[:160]
            return res
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
        if cr.status_code != 200:
            res['err'] = 'confirm ' + str(cr.status_code)
            res['confirm_body'] = cr.text[:160]
            return res
        cd = cr.json()
        sa = cd.get('submission_attempt') or {}
        res['confirm_sa'] = sa.get('state','') if isinstance(sa, dict) else ''
        pmr_url = find_redirect(cd)
        if pmr_url:
            rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer':'https://pay.openai.com/'})
            fin = str(getattr(rg, 'url', ''))
            bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
            if bm:
                res['ba'] = bm.group(1); res['ba_url'] = fin[:200]; res['ba_ok'] = True
                res['approve_result'] = 'confirm_redirect'
            else:
                res['err'] = 'no ba confirm: ' + fin[:60]
            return res
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
        try:
            ab = ar.json(); res['approve_result'] = ab.get('result', 'unknown_' + str(ar.status_code))
        except Exception:
            res['approve_result'] = 'http_' + str(ar.status_code)
        if ar.status_code != 200:
            res['err'] = 'approve ' + str(ar.status_code)
            return res
        if res['approve_result'] != 'approved':
            return res
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
            if pmr_url:
                rg = s.get(pmr_url, allow_redirects=True, timeout=20, headers={'Referer':'https://pay.openai.com/'})
                fin = str(getattr(rg, 'url', ''))
                bm = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', fin)
                if bm:
                    res['ba'] = bm.group(1); res['ba_url'] = fin[:200]; res['ba_ok'] = True
                else:
                    res['err'] = 'no ba poll: ' + fin[:60]
                return res
            psa = pd.get('submission_attempt') or {}
            pst = psa.get('state','') if isinstance(psa, dict) else ''
            if pst == 'failed':
                pe = psa.get('error', {}) if isinstance(psa, dict) else {}
                res['err'] = 'failed:' + str(pe.get('code','') if isinstance(pe, dict) else '')
                return res
        res['err'] = 'timeout approved no redirect'
    except Exception as e:
        res['err'] = str(e)[:160]
        low = res['err'].lower()
        if any(x in low for x in ('tls','timeout','proxy','connect','ssl')):
            res['approve_result'] = res['approve_result'] or 'network_error'
    return res

results = []
print('=== US+TR probe (free-trial token) ===', flush=True)

# 1) single probes for matrix around TR
probe_cases = [
    ('US', 'TR', 'TRY', TR_ADDR, False),
    ('US', 'TR', 'USD', TR_ADDR, False),
    ('TR', 'TR', 'TRY', TR_ADDR, False),
    ('TR', 'US', 'USD', US_ADDR, False),
    ('US', 'US', 'USD', US_ADDR, False),
]
print('--- matrix ---', flush=True)
for pr, c, cur, addr, full in probe_cases:
    r = try_once(0, pr, c, cur, addr, do_full=full)
    results.append({'phase':'matrix', **r})
    print(json.dumps({
        'proxy': pr, 'country': c, 'currency': cur,
        'amt': r.get('amt'), 'zero': r.get('zero'), 'paypal': r.get('paypal'),
        'pmt': r.get('pmt'), 'one_click': r.get('one_click_trial_eligible'),
        'promo': r.get('promo_campaign'), 'err': r.get('err','')[:80]
    }, ensure_ascii=False), flush=True)
    time.sleep(0.6)

# 2) if US+TR has paypal, hammer full flow
print('\n--- hammer US proxy + TR checkout x12 ---', flush=True)
# choose currency based on matrix: prefer one with paypal
best_currency = 'TRY'
for item in results:
    if item.get('proxy')=='US' and item.get('country')=='TR' and item.get('paypal'):
        best_currency = item.get('currency') or 'TRY'
        break

for i in range(1, 13):
    # try TRY first preference; if earlier matrix said USD better, still try both if needed
    currency = best_currency if i <= 8 else ('USD' if best_currency == 'TRY' else 'TRY')
    r = try_once(i, 'US', 'TR', currency, TR_ADDR, do_full=True)
    results.append({'phase':'hammer', **r})
    print(f'  [{i:02d}] cur={currency} amt={str(r.get("amt")):6s} zero={str(r.get("zero")):5s} pp={str(r.get("paypal")):5s} approve={str(r.get("approve_result")):16s} ba_ok={str(r.get("ba_ok")):5s} ba={r.get("ba","")} err={str(r.get("err",""))[:50]}', flush=True)
    if r.get('ba_ok') and r.get('zero'):
        print('  HIT zero+BA: ' + r.get('ba_url',''), flush=True)
        break
    if r.get('ba_ok'):
        print('  HIT BA(non-zero): ' + r.get('ba_url',''), flush=True)
        # continue a bit more to seek zero+BA
    time.sleep(0.5)

print('\n=== SUMMARY ===', flush=True)
ham = [r for r in results if r.get('phase')=='hammer']
print(f'hammer_total={len(ham)} ba_ok={sum(1 for r in ham if r.get("ba_ok"))} zero={sum(1 for r in ham if r.get("zero"))} zero_and_ba={sum(1 for r in ham if r.get("ba_ok") and r.get("zero"))}', flush=True)
print('approve counts:', dict(Counter((r.get('approve_result') or 'empty') for r in ham)), flush=True)
print('amt counts:', dict(Counter(str(r.get('amt')) for r in ham)), flush=True)
print('paypal counts:', dict(Counter(str(r.get('paypal')) for r in ham)), flush=True)

with open('scripts/ba_us_tr_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print('saved scripts/ba_us_tr_results.json', flush=True)
print('=== DONE ===', flush=True)
