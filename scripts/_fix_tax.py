from pathlib import Path
p = Path("scripts/pp_dual_proxy_extract.py")
t = p.read_text(encoding="utf-8")
start = t.find("def payment_pages_update")
end = t.find("\ndef auth_headers")
if start < 0:
    # maybe order different
    end = t.find("\ndef try_once")
print("start", start)
# find function end by next def after start
import re
m = re.search(r"\ndef\s+\w+", t[start+1:])
end = start+1+m.start() if m else -1
print("end", end)
print(t[start:end][:500])
new = '''def payment_pages_update(sess, cs, pk, addr, ck=""):
    # Only tax_region fields are accepted on payment_pages update.
    # Extra billing_details fields trigger parameter_unknown and amount mismatch.
    tb = {
        "eid": str(uuid.uuid4()),
        "tax_region[country]": addr["country"],
        "tax_region[postal_code]": addr["postal_code"],
        "tax_region[line1]": addr["line1"],
        "tax_region[city]": addr["city"],
        "key": pk,
        "_stripe_version": STRIPE_INIT_VERSION,
    }
    if addr.get("state"):
        tb["tax_region[state]"] = addr["state"]
    if ck:
        tb["init_checksum"] = ck
    r = sess.post(f"{STRIPE_API_BASE}/payment_pages/{cs}", data=tb, headers=sh, timeout=30)
    return r

'''
p.write_text(t[:start] + new + t[end:], encoding="utf-8")
print("patched payment_pages_update")
