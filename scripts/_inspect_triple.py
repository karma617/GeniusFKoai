from pathlib import Path
p = Path('scripts/pp_triple_proxy_extract.py')
t = p.read_text(encoding='utf-8')
print('has STRIPE_PROXY', 'STRIPE_PROXY' in t)
print('has APPROVE_PROXY', 'APPROVE_PROXY' in t)
print('has TAX_COUNTRY', 'TAX_COUNTRY' in t)
print('len', len(t))
# show setup around try_once session creation
i = t.find('s_chk = None')
print(t[i:i+900] if i>=0 else 'no s_chk=None')
i2 = t.find('s_chk.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/init"')
print('init uses s_chk', i2>=0)
i3 = t.find('s_stripe.post(f"{STRIPE_API_BASE}/payment_pages/{cs}/init"')
print('init uses s_stripe', i3>=0)
