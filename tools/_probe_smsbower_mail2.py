#!/usr/bin/env python3
import json
import urllib.error
import urllib.parse
import urllib.request

API_KEY = "UkcCVDVonOsHRZCUc1HcurxaXVVAPYuw"
BASE = "https://smsbower.page"


def get(path: str, params: dict) -> str:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    url = f"{BASE}{path}?{qs}"
    print("GET", url)
    req = urllib.request.Request(url, headers={"User-Agent": "GeniusFKoai/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def main() -> None:
    for params in [
        {"api_key": API_KEY, "service": "dr"},
        {"api_key": API_KEY, "service": "dr", "domain": "gmail.com"},
        {"api_key": API_KEY, "service": "dr", "domain": "gmail.com", "alias": "0"},
        {"api_key": API_KEY, "service": "ot", "domain": "gmail.com"},
    ]:
        try:
            raw = get("/api/mail/getPriceRests", params)
            print(raw[:1500])
        except Exception as exc:
            print("ERR", exc)
        print("---")

    # try create mail for openai
    try:
        raw = get(
            "/api/mail/getActivation",
            {
                "api_key": API_KEY,
                "service": "dr",
                "domain": "gmail.com",
                "alias": "0",
            },
        )
        print("ACTIVATION", raw)
        data = json.loads(raw)
        mail_id = data.get("mailId") or data.get("mail_id") or data.get("id")
        if mail_id:
            # cancel to avoid locking funds
            cancel = get(
                "/api/mail/setStatus",
                {
                    "api_key": API_KEY,
                    "id": mail_id,
                    "status": 2,
                },
            )
            print("CANCEL", cancel)
    except Exception as exc:
        print("ACTIVATION ERR", exc)


if __name__ == "__main__":
    main()
