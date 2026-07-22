import argparse, base64, datetime, json, re, sqlite3, sys, time, uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
from curl_cffi import requests as cffi
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from platforms.chatgpt import payment, stripe_http
