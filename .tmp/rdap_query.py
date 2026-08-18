# -*- coding: utf-8 -*-
import json, urllib.request, ssl

OUT = r'D:\work\ai\GeniusFKoai\.tmp\rdap_results.txt'
ips = [
    '240f:e1:38e8:1:1d51:5ad7:8800:45d5',  # 注册任务 账号#4 (velds) 出口
    '212.102.51.116',   # 账号#1 出口
    '61.195.230.183',   # 账号#2 出口
    '212.102.51.104',   # 账号#3 出口
    '212.102.51.95',    # 账号#5 出口
    '219.100.37.234',   # 失败任务 c9f9ee 账号#1 出口
]
endpoints = [
    'https://rdap.apnic.net/ip/{}',
    'https://rdap.jpnic.net/ip/{}',
    'https://rdap.db.ripe.net/ip/{}',
    'https://rdap.arin.net/registry/ip/{}',
]

lines = []
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def pick(d):
    out = {}
    for k in ['handle', 'name', 'type', 'country', 'startAddress', 'endAddress', 'parentHandle', 'status']:
        if k in d:
            out[k] = d[k]
    # entities
    if 'entities' in d:
        ents = []
        for e in d['entities'][:5]:
            en = {'handle': e.get('handle'), 'roles': e.get('roles')}
            if 'vcardArray' in e and e['vcardArray'] and len(e['vcardArray']) > 1:
                for item in e['vcardArray'][1]:
                    if item[0] in ('fn', 'adr'):
                        en[item[0]] = item[3]
            ents.append(en)
        out['entities'] = ents
    # remarks
    if 'remarks' in d:
        out['remarks'] = d['remarks'][:2]
    return out

for ip in ips:
    got = False
    lines.append(f"\n===== {ip} =====")
    for ep in endpoints:
        try:
            url = ep.format(urllib.parse.quote(ip)) if 'apnic' in ep or 'jpnic' in ep else ep.format(ip)
        except Exception:
            url = ep.format(ip)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                d = json.loads(r.read().decode('utf-8', errors='replace'))
            lines.append(f"  [{ep.split('/')[2]}] -> {json.dumps(pick(d), ensure_ascii=False, indent=2)}")
            got = True
            break
        except Exception as e:
            lines.append(f"  [{ep.split('/')[2]}] err: {type(e).__name__} {str(e)[:120]}")
    if not got:
        lines.append("  (no RDAP result)")

with open(OUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))
print("DONE")
