code = open('scripts/ba_chain_loop_probe.py','r',encoding='utf-8').read()
old_block = """        if rurl:
            result['pm_redirect'] = rurl
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
new_block = """        if rurl:
            result['pm_redirect'] = rurl
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
if old_block in code:
    code = code.replace(old_block, new_block)
    open('scripts/ba_chain_loop_probe.py','w',encoding='utf-8').write(code)
    print('fixed ok')
else:
    print('block not found')
