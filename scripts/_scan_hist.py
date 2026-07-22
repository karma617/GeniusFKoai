import json,re
from pathlib import Path
hits=[]
for p in Path('scripts').rglob('*'):
    if p.suffix.lower() not in {'.json','.txt','.md','.log'}: continue
    try: t=p.read_text(encoding='utf-8', errors='ignore')
    except: continue
    if 'BA-' not in t and 'paypal' not in t.lower():
        continue
    # success with zero
    if re.search(r'"zero"\s*:\s*true[\s\S]{0,200}"paypal"\s*:\s*true|"paypal"\s*:\s*true[\s\S]{0,200}"zero"\s*:\s*true', t):
        hits.append(('zero_paypal_true', str(p)))
    if re.search(r'zero=True\s+paypal=True|paypal=True\s+zero=True|zero_and_paypal', t):
        hits.append(('zero_paypal_text', str(p)))
    if 'BA-' in t and re.search(r'BR|JP|BRL|JPY', t):
        for m in re.finditer(r'BA-[A-Z0-9]{5,}', t):
            i=m.start(); ctx=t[max(0,i-160):i+50].replace('\n',' ')
            if any(k in ctx for k in ['BR','JP','BRL','JPY','zero']):
                hits.append(('ba_ctx', f'{p.name}:{m.group(0)}:{ctx[:140]}'))
print('hits', len(hits))
for h in hits[:50]:
    print(h[0], '::', h[1][:220])