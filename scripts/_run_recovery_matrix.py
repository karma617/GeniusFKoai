import json, os, subprocess, sys, time
from pathlib import Path

py = r".\.venv\Scripts\python.exe"
base = {
    "PYTHONIOENCODING": "utf-8",
    "BA_MAX_ATTEMPTS": "2",
    "BA_STOP_ON_FIRST": "1",
    "BA_DO_FULL": "1",
    "BA_APPROVE_PROXY": "US",
}

jobs = [
    {
        "name": "US_promoBR_skipTax_pm",
        "env": {
            "BA_CHECKOUT_PROXY": "US",
            "BA_STRIPE_PROXY": "US",
            "BA_PROMO_PROXY": "BR",
            "BA_PROMO_TAX_COUNTRY": "BR",
            "BA_BILLING_COUNTRY": "US",
            "BA_BILLING_CURRENCY": "USD",
            "BA_TAX_COUNTRY": "US",
            "BA_SKIP_MAIN_TAX": "1",
            "BA_CONFIRM_MODE": "pm",
            "BA_OUT_JSON": "scripts/pp_complete_US_promoBR_skipTax_pm.json",
        },
    },
    {
        "name": "US_promoJP_skipTax_pm",
        "env": {
            "BA_CHECKOUT_PROXY": "US",
            "BA_STRIPE_PROXY": "US",
            "BA_PROMO_PROXY": "JP",
            "BA_PROMO_TAX_COUNTRY": "JP",
            "BA_BILLING_COUNTRY": "US",
            "BA_BILLING_CURRENCY": "USD",
            "BA_TAX_COUNTRY": "US",
            "BA_SKIP_MAIN_TAX": "1",
            "BA_CONFIRM_MODE": "pm",
            "BA_OUT_JSON": "scripts/pp_complete_US_promoJP_skipTax_pm.json",
        },
    },
    {
        "name": "US_promoBR_skipTax_direct",
        "env": {
            "BA_CHECKOUT_PROXY": "US",
            "BA_STRIPE_PROXY": "US",
            "BA_PROMO_PROXY": "BR",
            "BA_PROMO_TAX_COUNTRY": "BR",
            "BA_BILLING_COUNTRY": "US",
            "BA_BILLING_CURRENCY": "USD",
            "BA_TAX_COUNTRY": "US",
            "BA_SKIP_MAIN_TAX": "1",
            "BA_CONFIRM_MODE": "direct",
            "BA_OUT_JSON": "scripts/pp_complete_US_promoBR_skipTax_direct.json",
        },
    },
    {
        "name": "BR_create_probe",
        "env": {
            "BA_CHECKOUT_PROXY": "BR",
            "BA_STRIPE_PROXY": "JP",
            "BA_PROMO_PROXY": "",
            "BA_PROMO_TAX_COUNTRY": "",
            "BA_BILLING_COUNTRY": "US",
            "BA_BILLING_CURRENCY": "USD",
            "BA_TAX_COUNTRY": "US",
            "BA_SKIP_MAIN_TAX": "0",
            "BA_CONFIRM_MODE": "pm",
            "BA_DO_FULL": "0",
            "BA_MAX_ATTEMPTS": "2",
            "BA_OUT_JSON": "scripts/pp_complete_BR_create_probe.json",
        },
    },
    {
        "name": "JP_create_probe",
        "env": {
            "BA_CHECKOUT_PROXY": "JP",
            "BA_STRIPE_PROXY": "BR",
            "BA_PROMO_PROXY": "",
            "BA_PROMO_TAX_COUNTRY": "",
            "BA_BILLING_COUNTRY": "US",
            "BA_BILLING_CURRENCY": "USD",
            "BA_TAX_COUNTRY": "US",
            "BA_SKIP_MAIN_TAX": "0",
            "BA_CONFIRM_MODE": "pm",
            "BA_DO_FULL": "0",
            "BA_MAX_ATTEMPTS": "2",
            "BA_OUT_JSON": "scripts/pp_complete_JP_create_probe.json",
        },
    },
]

summary = []
for job in jobs:
    name = job["name"]
    env = os.environ.copy()
    env.update(base)
    env.update({k: str(v) for k, v in job["env"].items() if v is not None})
    out_txt = Path(f"scripts/pp_complete_{name}.txt")
    print(f"\n===== RUN {name} =====", flush=True)
    p = subprocess.run(
        [py, "scripts/pp_complete_extract.py"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    out_txt.write_text(text, encoding="utf-8")
    print(text[-2500:], flush=True)
    row = {"name": name, "rc": p.returncode}
    jpath = Path(job["env"].get("BA_OUT_JSON", ""))
    if jpath.exists():
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else [data]
            best = None
            for it in items:
                if not isinstance(it, dict):
                    continue
                if best is None or it.get("ba_ok"):
                    best = it
            if best:
                for k in ["ba_ok","ba","amt","zero","paypal","pmt","approve_result","err","amt_after_promo","trial","promo_campaign","expected_amount","amts","amts_tax"]:
                    if k in best:
                        row[k] = best.get(k)
                row["steps"] = best.get("steps")
        except Exception as e:
            row["parse_err"] = str(e)
    summary.append(row)
    time.sleep(0.5)

Path("scripts/pp_recovery_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n===== SUMMARY =====")
print(json.dumps(summary, ensure_ascii=False, indent=2))