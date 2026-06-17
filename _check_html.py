import sys
sys.stdout.reconfigure(encoding='utf-8')
html = open(r'D:\tmp\phone_fill_failed.html', 'r', encoding='utf-8', errors='replace').read()
# Find input elements
import re
inputs = re.findall(r'<input[^>]*>', html)
for inp in inputs:
    print(inp[:200])
print("---")
# Find form elements
forms = re.findall(r'<form[^>]*>', html)
for f in forms:
    print(f[:200])
