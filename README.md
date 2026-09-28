# Prime Industrial ERP — read-only demo backend

Public-safe backend for the `/labs/prime-erp` showcase on artemis-omni. It serves
the same `/api/*` JSON as the real `erp_server.py`, with three differences:

* **Synthetic data.** `prime_industrial_demo.db` comes from `sanitize.py`. It has the
  real schema and the same row count in every table. Every name, email, address,
  phone, taxpayer ID, external ID and file path is fake (`Acme Corp`,
  `demo-0001@example.com`, `00-0000000`, …). Money amounts are scaled by a
  deterministic per-order factor, and the ledger, invoices and tax-package figures
  are then recalculated so the books still reconcile.
* **Read-only.** Every write route (POST/PUT/PATCH/DELETE) has been removed, and
  `_demo_guard()` answers any write with `405 {"error": "read_only_demo"}`. The
  SQLite file is also opened with `mode=ro`.
* **No login for reads.** With `DEMO_MODE=1` (the default), `GET /api/*` needs no
  session cookie. Setting `DEMO_MODE=0` turns the original password gate back on.

No outbound calls are made: Squarespace sync, Plaid and PDF generation are all
removed. The file-download routes (invoice PDFs, tax-form PDFs/ZIPs and filing
packages) return a JSON 404. No real documents are included.

## Files

| File | Purpose |
|---|---|
| `demo_server.py` | Copy of `erp_server.py` with the demo changes (search for `[DEMO]`) |
| `demo_db.py` | Copy of `erp_db.py`. Reads `PRIME_ERP_DATA_DIR`, uses a read-only connection, and seeds the data dir on first boot |
| `demo_owner_control.py` | Copy of `owner_control.py`, pointed at `demo_db` (God Mode and tax-package calendar) |
| `tax_engine.py`, `tax_forms_config.py` | Unchanged copies (used for validation and config only; PDF rendering is never called) |
| `prime_industrial_demo.db` | Sanitized seed database (the only DB that belongs in this repo) |
| `sanitize.py` | Rebuilds the seed from the real DB. Run it on the owner's Mac only |
| `render.yaml` | Render Blueprint (gunicorn, persistent disk, Python version) |
| `.env.example` | Environment variable names (no values) |

## Run locally

```bash
cd demo
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 demo_server.py                      # http://127.0.0.1:5050
curl http://127.0.0.1:5050/api/summary      # 200, no cookie needed
curl -X POST http://127.0.0.1:5050/api/sync # 405 read_only_demo
```

On first start, the DB is copied to `./data/` (git-ignored). You can point it elsewhere
with `PRIME_ERP_DATA_DIR=/some/dir`. To run it production-style:
`gunicorn demo_server:app --bind 0.0.0.0:5050`.

## Regenerate the sanitized DB

```bash
python3 sanitize.py            # reads ../AppSupport_PrimeIndustrial/prime_industrial.db read-only
```

The script stops without writing output if any table has no sanitizer handler, if a
row count differs, or if any real identifying value is still present.

## Deploy (Render + Vercel)

1. Create a **new private repo that contains only this `demo/` folder** (its contents
   form the repo root). Do **not** push the parent bundle repo: it tracks the real
   DB, invoice PDFs and tax forms.
2. In Render, choose **New → Blueprint**, select that repo, and apply `render.yaml`. This creates
   the `prime-erp-demo` web service (Starter plan, because persistent disks need a paid
   instance) with a 1 GB disk mounted at `/var/data`.
3. Check `https://<service>.onrender.com/healthz` and `/api/summary`.
4. In the Vercel project for artemis-omni, set `PRIME_ERP_BACKEND_URL` to
   `https://<service>.onrender.com` (no trailing slash) and redeploy. The rewrites in
   `next.config.mjs` proxy the 48 showcase paths server-side, so the URL is never
   sent to browsers.

The data is read-only, so the disk is optional. If you drop the `disk:` block, the
service can run on the free plan, and the seed DB is copied to `./data` on each boot.
