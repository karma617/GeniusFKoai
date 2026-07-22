from pathlib import Path
p = Path('scripts/pp_triple_proxy_extract.py')
t = p.read_text(encoding='utf-8')
t = t.replace('Dual-proxy PP BA extractor based on oaipay mode2/mode6 contract.', 'Triple-pool PP BA extractor (checkout/stripe/approve + optional promo).')
t = t.replace('scripts/pp_dual_proxy_results.json', 'scripts/pp_triple_proxy_results.json')
if 'BA_STRIPE_PROXY' not in t:
    needle = 'CHECKOUT_PROXY = os.environ.get("BA_CHECKOUT_PROXY", "US").upper()\n'
    insert = needle + 'STRIPE_PROXY = os.environ.get("BA_STRIPE_PROXY", CHECKOUT_PROXY).upper()\nAPPROVE_PROXY = os.environ.get("BA_APPROVE_PROXY", "US").upper()\n'
    if needle not in t:
        raise SystemExit('checkout proxy line missing')
    t = t.replace(needle, insert, 1)
if 'BA_TAX_COUNTRY' not in t:
    needle = 'PROMO_ADDR_COUNTRY = os.environ.get("BA_PROMO_ADDR_COUNTRY", BILLING_COUNTRY).upper()\n'
    insert = needle + 'TAX_COUNTRY = os.environ.get("BA_TAX_COUNTRY", BILLING_COUNTRY).upper()\n'
    t = t.replace(needle, insert, 1)
t = t.replace('os.environ.get("BA_PROMO_PROXY", "TR")', 'os.environ.get("BA_PROMO_PROXY", "")')
t = t.replace('os.environ.get("BA_PROMO_WHEN", "after_init")', 'os.environ.get("BA_PROMO_WHEN", "none")')
p.write_text(t, encoding='utf-8')
print('base patch ok', len(t))
