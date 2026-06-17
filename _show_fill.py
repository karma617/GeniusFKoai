import sys
sys.stdout.reconfigure(encoding='utf-8')
path = 'platforms/chatgpt/browser_register.py'
lines = open(path, 'r', encoding='utf-8').readlines()
for i in range(1463, 1520):
    print(f'{i+1}: {lines[i].rstrip()}')
