import sys, os, json, time, subprocess
os.environ['PYTHONIOENCODING'] = 'utf-8'
for attempt in range(5):
    print(f'Attempt {attempt+1}/5...', flush=True)
    ret = subprocess.run([sys.executable, '-u', 'scripts/ba_ie_fixed.py'], capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
    print(ret.stdout, flush=True)
    if 'BA-' in ret.stdout:
        print('SUCCESS!', flush=True)
        break
    if ret.stderr:
        print(f'STDERR: {ret.stderr[:300]}', flush=True)
    print('Retrying...', flush=True)
    time.sleep(2)
