import json, os, subprocess, time
from pathlib import Path

py = str(Path('.venv/Scripts/python.exe'))
script = 'scripts/pp_complete_extract.py'
combos = [
    ('US_pm', dict(BA_CHECKOUT_PROXY='US', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_CONFIRM_MODE='pm', BA_MAX_ATTEMPTS='2')),
    ('US_direct', dict(BA_CHECKOUT_PROXY='US', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_CONFIRM_MODE='direct', BA_MAX_ATTEMPTS='2')),
    ('BRJP_pm_US_bill', dict(BA_CHECKOUT_PROXY='BR', BA_STRIPE_PROXY='JP', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_CONFIRM_MODE='pm', BA_MAX_ATTEMPTS='3')),
    ('BRJP_direct_US_bill', dict(BA_CHECKOUT_PROXY='BR', BA_STRIPE_PROXY='JP', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_CONFIRM_MODE='direct', BA_MAX_ATTEMPTS='3')),
    ('BRJP_pm_BR_bill', dict(BA_CHECKOUT_PROXY='BR', BA_STRIPE_PROXY='JP', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='BR', BA_BILLING_CURRENCY='BRL', BA_TAX_COUNTRY='BR', BA_CONFIRM_MODE='pm', BA_MAX_ATTEMPTS='3')),
    ('JPBR_pm_JP_bill', dict(BA_CHECKOUT_PROXY='JP', BA_STRIPE_PROXY='BR', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='JP', BA_BILLING_CURRENCY='JPY', BA_TAX_COUNTRY='JP', BA_CONFIRM_MODE='pm', BA_MAX_ATTEMPTS='3')),
    ('US_pm_promoJP', dict(BA_CHECKOUT_PROXY='US', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_PROMO_PROXY='JP', BA_PROMO_TAX_COUNTRY='JP', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_CONFIRM_MODE='pm', BA_MAX_ATTEMPTS='2')),
    ('US_direct_promoBR', dict(BA_CHECKOUT_PROXY='US', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_PROMO_PROXY='BR', BA_PROMO_TAX_COUNTRY='BR', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_CONFIRM_MODE='direct', BA_MAX_ATTEMPTS='2')),
]
summary=[]
for name, env in combos:
    out_json=f'scripts/pp_complete_{name}.json'
    out_txt=f'scripts/pp_complete_{name}.txt'
    e=os.environ.copy(); e.update(env)
    e['BA_OUT_JSON']=out_json; e['BA_STOP_ON_FIRST']='1'; e['BA_UI_MODE']='custom'; e['BA_DO_FULL']='1'; e['PYTHONIOENCODING']='utf-8'
    print(f'\n##### {name} #####', flush=True)
    with open(out_txt,'w',encoding='utf-8') as f:
        p=subprocess.run([py,script], env=e, stdout=f, stderr=subprocess.STDOUT, text=True)
    text=Path(out_txt).read_text(encoding='utf-8', errors='ignore')
    print(text[-1400:], flush=True)
    row={'name':name,'rc':p.returncode}
    if Path(out_json).exists():
        data=json.loads(Path(out_json).read_text(encoding='utf-8'))
        best=next((x for x in data if x.get('ba_ok')), data[-1] if data else None)
        if best:
            row.update({
                'ba_ok':best.get('ba_ok'),'ba':best.get('ba'),'amt':best.get('amt'),'zero':best.get('zero'),
                'paypal':best.get('paypal'),'pmt':best.get('pmt'),'approve':best.get('approve_result'),
                'err':(best.get('err') or '')[:140],'attempts':len(data)
            })
    summary.append(row)
    time.sleep(0.3)
Path('scripts/pp_complete_matrix_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print('\n=== SUMMARY ===', flush=True)
for r in summary:
    print(r, flush=True)
