from pathlib import Path
p = Path("scripts/pp_dual_proxy_extract.py")
t = p.read_text(encoding="utf-8")
old = '''def payment_pages_update(sess, cs, pk, addr, ck=""):
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
new = '''def payment_pages_update(sess, cs, pk, addr, ck=""):
    # Match ba_multi_ip_hammer: only tax_region + key + version.
    # init_checksum / billing_details cause parameter_unknown on this endpoint.
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
    r = sess.post(f"{STRIPE_API_BASE}/payment_pages/{cs}", data=tb, headers=sh, timeout=30)
    return r
'''
if old not in t:
    raise SystemExit("old missing")
p.write_text(t.replace(old, new), encoding="utf-8")
print("ok")
