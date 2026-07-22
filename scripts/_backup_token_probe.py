# Probe backup tokens for BR create zero+paypal; stop early on first hit
from __future__ import annotations
import json, os, sys, time, base64
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))
os.environ['PYTHONIOENCODING']='utf-8'
from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt
from platforms.chatgpt import stripe_http

PRE_PROXY='socks5h://127.0.0.1:7897'
KOOKEEY='https://www.kookeey.com/pickdynamicips?t=2&auth=pwd&format=4&n=1&p=http&gate=global&g={region}&r=-1&type=txt&sign=874086cfbdb353e32d67a6dbebd498af&accessid=8239626&upf=1,1&dl=%5Cr%5Cn'
PAYMENT='https://chatgpt.com/backend-api/payments/checkout'
_ds=cffi.Session(impersonate='chrome110')

def fetch_proxy(region):
    for _ in range(3):
        r=_ds.get(KOOKEEY.format(region=region), timeout=15)
        parts=r.text.strip().split(':')
        if len(parts)==4:
            return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
        time.sleep(0.4)
    raise RuntimeError('proxy fail '+region)

def make_session(proxy):
    return cffi.Session(impersonate='chrome110', proxy=proxy, curl_options={CurlOpt.PRE_PROXY: PRE_PROXY})

def claims(tok):
    p=tok.split('.')[1]; p+='='*((4-len(p)%4)%4)
    return json.loads(base64.urlsafe_b64decode(p.encode()))

def extract_token(obj):
    if not isinstance(obj, dict): return '', ''
    for k in ('token','access_token','accessToken'):
        if isinstance(obj.get(k), str) and obj[k].startswith('eyJ'):
            return obj[k], obj.get('email') or obj.get('account_email') or ''
    # nested
    for v in obj.values():
        if isinstance(v, dict):
            t,e=extract_token(v)
            if t: return t,e or obj.get('email') or ''
        if isinstance(v, str) and v.startswith('eyJ') and len(v)>100:
            return v, obj.get('email') or ''
    return '', ''

files=sorted(Path('data/chatgpt_token_backups').glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)
# sample up to 8 newest unique emails
seen=set(); picks=[]
for f in files:
    try: d=json.loads(f.read_text(encoding='utf-8'))
    except: continue
    tok,email=extract_token(d)
    if not tok: continue
    key=email or f.name
    if key in seen: continue
    seen.add(key)
    picks.append((f, tok, email or f.stem))
    if len(picks)>=8: break

print('picked', len(picks), flush=True)
results=[]
for f, tok, email in picks:
    row={'file':f.name,'email':email}
    try:
        c=claims(tok)
        auth=c.get('https://api.openai.com/auth') or {}
        row.update({'plan':auth.get('chatgpt_plan_type'),'signup':auth.get('is_signup'),'exp':c.get('exp'),'aid':auth.get('chatgpt_account_id')})
        # exp check rough (now ~ 1784e9? user token iat 1784173521 -> year 2026)
        import time as _t
        row['expired']=bool(c.get('exp') and c['exp'] < _t.time())
    except Exception as e:
        row['jwt_err']=str(e)[:80]
    # US control + BR create
    for region, bill_c, bill_cur in [('US','US','USD'),('BR','BR','BRL'),('JP','JP','JPY')]:
        key=f'{region}_create'
        try:
            s=make_session(fetch_proxy(region))
            h={'Authorization':'Bearer '+tok,'Content-Type':'application/json','oai-language':'zh-CN','Origin':'https://chatgpt.com','Referer':'https://chatgpt.com/'}
            if row.get('aid'): h['Chatgpt-Account-Id']=row['aid']
            pl={'plan_name':'chatgptplusplan','billing_details':{'country':bill_c,'currency':bill_cur},'entry_point':'all_plans_pricing_modal','promo_campaign':{'promo_campaign_id':'plus-1-month-free','is_coupon_from_query_param':False},'checkout_ui_mode':'hosted','cancel_url':'https://chatgpt.com/#pricing'}
            r=s.post(PAYMENT, headers=h, json=pl, timeout=30)
            row[key+'_http']=r.status_code
            if r.status_code!=200:
                row[key+'_err']=r.text[:100]
                s.close(); continue
            data=r.json(); cs=data.get('checkout_session_id')
            init=stripe_http.stripe_init(s, cs_id=cs)
            pmt=init.get('payment_method_types') or []
            amt=stripe_http.extract_expected_amount(init)
            row[key]={'pmt':pmt,'amt':amt,'paypal':('paypal' in [str(x).lower() for x in pmt]),'zero':str(amt)=='0','trial':data.get('one_click_trial_eligible'),'promo':data.get('promo_campaign')}
            s.close()
        except Exception as e:
            row[key+'_err']=str(e)[:120]
    results.append(row)
    print(json.dumps({'email':email,'expired':row.get('expired'),'US':row.get('US_create'),'BR':row.get('BR_create'),'JP':row.get('JP_create'),'err':row.get('BR_create_err') or row.get('US_create_err')}, ensure_ascii=False), flush=True)
    # if BR or JP has zero+paypal, mark and break
    for k in ('BR_create','JP_create'):
        v=row.get(k) or {}
        if v.get('zero') and v.get('paypal'):
            print('HIT', email, k, v, flush=True)
            Path('scripts/pp_backup_token_hit.json').write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
            break
    time.sleep(0.3)

Path('scripts/pp_backup_token_probe.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print('saved', len(results), 'zero_paypal_hits', sum(1 for r in results for k in ('BR_create','JP_create') if (r.get(k) or {}).get('zero') and (r.get(k) or {}).get('paypal')))