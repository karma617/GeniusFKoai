code = open('scripts/ba_chain_multi_ip.py','r',encoding='utf-8').read()
# Fix variable name collision: ru is used for both return_url and find_redirect result
code = code.replace("rurl = 'https://pay.openai.com/c/pay/' + cs + '?success_return_url=' + surl", "rurl_base = 'https://pay.openai.com/c/pay/' + cs + '?success_return_url=' + surl")
code = code.replace("'return_url': rurl,", "'return_url': rurl_base,")
# In confirm response handling
code = code.replace("ru = find_redirect(cd)\n        if ru:", "pmr_url = find_redirect(cd)\n        if pmr_url:")
code = code.replace("res['pm_redirect'] = ru\n            rg = s2.get(ru,", "res['pm_redirect'] = pmr_url\n            rg = s2.get(pmr_url,")
# In poll loop
code = code.replace("ru = find_redirect(pd)\n                if ru:", "pmr_url = find_redirect(pd)\n                if pmr_url:")
code = code.replace("rg = s2.get(ru,", "rg = s2.get(pmr_url,")
open('scripts/ba_chain_multi_ip.py','w',encoding='utf-8').write(code)
print('fixed ok')
