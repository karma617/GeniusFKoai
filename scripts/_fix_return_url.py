from pathlib import Path
import re
p = Path('scripts/pp_complete_extract.py')
t = p.read_text(encoding='utf-8')
i = t.find('success_url = f"https://chatgpt.com/checkout/verify')
print('idx', i)
print(t[i:i+450])
pat = r'success_url = f"https://chatgpt.com/checkout/verify\?stripe_session_id=\{cs\}&processor_entity=\{ent\}&plan_type=plus"\n.*?referrer = .*?\n'
new = '''success_url = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs}&processor_entity={ent}&plan_type=plus"
        return_url = stripe_http.build_confirm_return_url(latest, cs_id=cs, fallback_url=success_url)
        referrer = stripe_http.build_confirm_referrer_url(latest, cs_id=cs, fallback_url=f"https://pay.openai.com/c/pay/{cs}")
'''
t2, n = re.subn(pat, new, t, count=1, flags=re.S)
print('n', n)
if n != 1:
    raise SystemExit('replace failed')
p.write_text(t2, encoding='utf-8')
print('ok')
