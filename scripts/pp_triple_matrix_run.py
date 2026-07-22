import json, os, subprocess, time
from pathlib import Path

py = str(Path('.venv/Scripts/python.exe'))
script = 'scripts/pp_triple_proxy_extract.py'
combos = [
    ('US_all_USUSD', dict(BA_CHECKOUT_PROXY='US', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_PROMO_WHEN='none', BA_MAX_ATTEMPTS='2')),
    ('BR_create_US_stripe_US_approve_USUSD', dict(BA_CHECKOUT_PROXY='BR', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_PROMO_WHEN='none', BA_MAX_ATTEMPTS='3')),
    ('JP_create_US_stripe_US_approve_USUSD', dict(BA_CHECKOUT_PROXY='JP', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_PROMO_WHEN='none', BA_MAX_ATTEMPTS='3')),
    ('BR_create_JP_stripe_US_approve_USUSD', dict(BA_CHECKOUT_PROXY='BR', BA_STRIPE_PROXY='JP', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_PROMO_WHEN='none', BA_MAX_ATTEMPTS='4')),
    ('BR_create_JP_stripe_US_approve_BRBRL', dict(BA_CHECKOUT_PROXY='BR', BA_STRIPE_PROXY='JP', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='BR', BA_BILLING_CURRENCY='BRL', BA_TAX_COUNTRY='BR', BA_PROMO_WHEN='none', BA_MAX_ATTEMPTS='3')),
    ('JP_create_BR_stripe_US_approve_JPJPY', dict(BA_CHECKOUT_PROXY='JP', BA_STRIPE_PROXY='BR', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='JP', BA_BILLING_CURRENCY='JPY', BA_TAX_COUNTRY='JP', BA_PROMO_WHEN='none', BA_MAX_ATTEMPTS='3')),
    ('US_create_US_stripe_promoBR_taxUS', dict(BA_CHECKOUT_PROXY='US', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_PROMO_PROXY='BR', BA_PROMO_WHEN='after_init', BA_PROMO_ADDR_COUNTRY='BR', BA_MAX_ATTEMPTS='2')),
    ('US_create_US_stripe_promoJP_taxUS', dict(BA_CHECKOUT_PROXY='US', BA_STRIPE_PROXY='US', BA_APPROVE_PROXY='US', BA_BILLING_COUNTRY='US', BA_BILLING_CURRENCY='USD', BA_TAX_COUNTRY='US', BA_PROMO_PROXY='JP', BA_PROMO_WHEN='after_init', BA_PROMO_ADDR_COUNTRY='JP', BA_MAX_ATTEMPTS='2')),
]

summary = []
for name, env in combos:
    out_json = f'scripts/pp_triple_{name}.json'
    out_txt = f'scripts/pp_triple_{name}.txt'
    e = os.environ.copy()
    e.update(env)
    e['BA_OUT_JSON'] = out_json
    e['BA_STOP_ON_FIRST'] = '1'
    e['BA_UI_MODE'] = 'custom'
    e['BA_DO_FULL'] = '1'
    e['PYTHONIOENCODING'] = 'utf-8'
    print(f'\n##### RUN {name} #####', flush=True)
    with open(out_txt, 'w', encoding='utf-8') as f:
        p = subprocess.run([py, script], env=e, stdout=f, stderr=subprocess.STDOUT, text=True)
    text = Path(out_txt).read_text(encoding='utf-8', errors='ignore')
    print(text[-1500:], flush=True)
    row = {'name': name, 'returncode': p.returncode}
    if Path(out_json).exists():
        data = json.loads(Path(out_json).read_text(encoding='utf-8'))
        best = next((x for x in data if x.get('ba_ok')), data[-1] if data else None)
        if best:
            row.update({
                'ba_ok': best.get('ba_ok'), 'ba': best.get('ba'), 'amt': best.get('amt'),
                'zero': best.get('zero'), 'paypal': best.get('paypal'), 'pmt': best.get('pmt'),
                'approve': best.get('approve_result'), 'err': (best.get('err') or '')[:120],
                'attempts': len(data),
            })
    summary.append(row)
    time.sleep(0.3)

Path('scripts/pp_triple_matrix_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print('\n=== MATRIX SUMMARY ===', flush=True)
for r in summary:
    print(r, flush=True)
