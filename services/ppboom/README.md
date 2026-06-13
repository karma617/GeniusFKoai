# PPBoom

Local helper for PayPal burst checkout creation.

## Run

```powershell
cd services/ppboom
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8787
```

Health:

```text
http://127.0.0.1:8787/health
```

Main APIs:

- `POST /api/paypal-link`
- `POST /api/paypal-link/jobs`
- `GET /api/paypal-link/jobs/{jobId}`
- `POST /api/paypal-link/jobs/{jobId}/pause`
- `POST /api/paypal-link/jobs/{jobId}/resume`
