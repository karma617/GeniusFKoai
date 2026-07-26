#!/usr/bin/env python3
import json
import urllib.parse
import urllib.request

API_KEY = "UkcCVDVonOsHRZCUc1HcurxaXVVAPYuw"
BASE = "https://smsbower.page"


def get(path: str, params: dict) -> str:
    qs = urllib.parse.urlencode(params)
    url = f"{BASE}{path}?{qs}"
    print("GET", url)
    req = urllib.request.Request(url, headers={"User-Agent": "GeniusFKoai/1.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "replace")


def main() -> None:
    for path, params in [
        ("/stubs/handler_api.php", {"api_key": API_KEY, "action": "getMailServicesList"}),
        ("/api/mail/getPriceRests", {"api_key": API_KEY}),
        ("/api/mail/getPriceRests", {"api_key": API_KEY, "domain": "gmail.com"}),
        ("/api/mail/getPriceRests", {"api_key": API_KEY, "service": "openai", "domain": "gmail.com"}),
        ("/api/mail/getPriceRests", {"api_key": API_KEY, "service": "chatgpt", "domain": "gmail.com"}),
        ("/api/mail/getPriceRests", {"api_key": API_KEY, "service": "oa", "domain": "gmail.com"}),
    ]:
        try:
            raw = get(path, params)
            print(raw[:4000])
        except Exception as exc:
            print("ERR", exc)
        print("---")


if __name__ == "__main__":
    main()
