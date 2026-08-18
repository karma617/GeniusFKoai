# -*- coding: utf-8 -*-
import json, urllib.request, ssl

OUT = r'D:\work\ai\GeniusFKoai\.tmp\rdap_proxysale.txt'
ips = ['210.171.232.211', '210.171.232.213', '210.171.233.202']

lines = []
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def pick(d):
    out = {}
    for k in ['handle', 'name', 'type', 'country', 'startAddress', 'endAddress', 'parentHandle']:
        if k in d:
            out[k] = d[k]
    if 'remarks' in d and d['remarks']:
        out['remarks'] = d['remarks'][0]
    return out

endpoints = [
    'https://rdap.apnic.net/ip/{}',
    'https://rdap.db.ripe.net/ip/{}',
    'https://rdap.arin.net/registry/ip/{}',
]

for ip in ips:
    lines.append(f"\n===== {ip} =====")
    for ep in endpoints:
        try:
            req = urllib.request.Request(ep.format(ip), headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                d = json.loads(r.read().decode('utf-8', errors='replace'))
            lines.append(f"  [{ep.split('/')[2]}] {json.dumps(pick(d), ensure_ascii=False)}")
            break
        except Exception as e:
            lines.append(f"  [{ep.split('/')[2]}] err: {str(e)[:100]}")

with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE")
