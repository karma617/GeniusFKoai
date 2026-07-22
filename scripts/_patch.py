code = open('scripts/ba_chain_loop_probe.py','r',encoding='utf-8').read()
old = "result['ba'] = rurl[:200]; result['ba_ok'] = True; return result"
new = """result['pm_redirect'] = rurl
                    r2g = s2.get(rurl, allow_redirects=True, timeout=20, headers={'Referer': 'https://pay.openai.com/'})
                    final = str(getattr(r2g, 'url', ''))
                    bam = re.search(r'ba_token=(BA-[A-Za-z0-9]+)', final)
                    if bam:
                        result['ba'] = bam.group(1)
                        result['ba_ok'] = True
                        result['ba_url'] = final[:200]
                    else:
                        result['ba'] = final[:100]
                        result['ba_ok'] = False
                        result['error'] = 'no ba_token: ' + final[:60]
                    return result"""
if old in code:
    code = code.replace(old, new)
    open('scripts/ba_chain_loop_probe.py','w',encoding='utf-8').write(code)
    print('patched ok')
else:
    print('old string not found')
    for i, line in enumerate(code.split(chr(10))):
        if 'rurl' in line and 'ba_ok' in line:
            print(f'line {i}: {line.strip()[:100]}')
