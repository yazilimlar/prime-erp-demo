#!/usr/bin/env python3
"""
Prime Industrial — ERP Server  v2  — PUBLIC READ-ONLY DEMO
Derived from erp_server.py for the /labs/prime-erp showcase.

Differences from erp_server.py (search for "[DEMO]"):
  * DEMO_MODE (env, default on): GET/HEAD /api/* need no login cookie.
  * Every write route (POST/PUT/PATCH/DELETE) is removed and _demo_guard() refuses
    any non-read method with 405 — unconditionally, even with DEMO_MODE off.
  * The DB is opened read-only from PRIME_ERP_DATA_DIR (default ./data) and holds
    only synthetic data produced by sanitize.py.
  * No outbound calls: Squarespace sync, invoice-PDF regeneration and Plaid are gone.
  * File downloads (invoice PDFs, tax-form PDFs/ZIPs, filing packages) return a
    JSON 404 — no real documents are shipped with the demo.

Run locally:  python3 demo_server.py   →  http://127.0.0.1:5050/api/summary
Production:   gunicorn demo_server:app (see render.yaml)
"""

import os, sys, json, datetime, pathlib, sqlite3, re, hashlib, hmac, html, secrets, time
from pathlib import Path
from flask import Flask, jsonify, request, session, redirect

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# [DEMO] DB path comes from demo_db (PRIME_ERP_DATA_DIR), seeded from the sanitized copy.
import demo_db
DATA_DIR = demo_db.DATA_DIR
DB_PATH  = demo_db.ensure_seeded()

# [DEMO] Public read access without login. Anything other than an explicit off value keeps it on.
DEMO_MODE = os.environ.get("DEMO_MODE", "1").strip().lower() not in ("0", "false", "no", "off")
READ_METHODS = ("GET", "HEAD", "OPTIONS")

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("ERP_SESSION_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_NAME="prime_industrial_erp_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ERP_COOKIE_SECURE", "1").lower() in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=10),
)

AUTH_AUDIT_LOG = DATA_DIR / "erp_auth_audit.log"
LOGIN_ATTEMPTS = {}
MAX_LOGIN_ATTEMPTS = int(os.environ.get("ERP_MAX_LOGIN_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("ERP_LOGIN_WINDOW_SECONDS", "600"))


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or "unknown"


def _audit_auth(event, detail=""):
    line = json.dumps({
        "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "event": event,
        "ip": _client_ip(),
        "path": request.path,
        "detail": detail,
    }, separators=(",", ":"))
    with AUTH_AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _configured_password_ok(candidate):
    candidate = candidate or ""
    password_hash = os.environ.get("ERP_PASSWORD_SHA256", "").strip().lower()
    if password_hash:
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, password_hash)

    password = os.environ.get("ERP_PASSWORD", "")
    if password:
        return hmac.compare_digest(candidate, password)

    return False


def _safe_next(value):
    value = value or "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith("/login") or value.startswith("/logout"):
        return "/"
    return value


def _login_limited():
    now = time.time()
    ip = _client_ip()
    attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    LOGIN_ATTEMPTS[ip] = attempts
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def _record_failed_login():
    ip = _client_ip()
    LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())
    _audit_auth("login_failed")


def _login_page(error="", next_url="/"):
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    next_html = html.escape(_safe_next(next_url), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Prime Industrial ERP Login</title>
  <style>
    :root{{color-scheme:dark;--bg:#0f1117;--panel:#1a1d27;--border:#2a2d3e;--text:#e8eaf0;--muted:#8891aa;--gold:#febd69;--red:#e74c3c}}
    *{{box-sizing:border-box}} body{{margin:0;min-height:100dvh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:20px}}
    .card{{width:380px;max-width:100%;background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:34px 36px;box-shadow:0 24px 70px rgba(0,0,0,.6);text-align:center}}
    .brand{{color:var(--gold);font-weight:800;font-size:18px;letter-spacing:.3px}} .sub{{color:var(--muted);font-size:12px;margin:5px 0 22px}}
    label{{display:block;text-align:left;font-size:12px;color:var(--muted);margin-bottom:8px}} input{{width:100%;background:#0f1117;border:1px solid var(--border);color:var(--text);padding:12px;border-radius:8px;font-size:15px;text-align:center;letter-spacing:2px}}
    button{{width:100%;background:var(--gold);color:#000;border:0;padding:12px;border-radius:8px;font-weight:800;font-size:14px;cursor:pointer;margin-top:12px}}
    .error{{color:var(--red);font-size:12px;line-height:1.4;margin:0 0 12px}} .note{{color:var(--muted);font-size:11px;line-height:1.5;margin-top:16px}}
  </style>
</head>
<body>
  <form class="card" method="post" action="/login" autocomplete="off">
    <div class="brand">Prime Industrial</div>
    <div class="sub">ERP &amp; Financial Control</div>
    {error_html}
    <input type="hidden" name="next" value="{next_html}">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" inputmode="numeric" autofocus required>
    <button type="submit">Unlock ERP</button>
    <div class="note">Server-side access gate. All dashboard, API, PDF, export, Plaid, bank, and tax routes require this session.</div>
  </form>
</body>
</html>"""


@app.before_request
def _demo_guard():
    """[DEMO] Read-only: refuse every write method on /api/*, logged in or not.
    Registered before require_login so writes get a clear 405, never a 401."""
    if request.path.startswith("/api/") and request.method not in READ_METHODS:
        return jsonify({"error": "read_only_demo",
                        "detail": "This is a read-only demo backend with synthetic data; writes are disabled."}), 405
    return None


@app.before_request
def require_login():
    if request.path in ("/login", "/logout", "/healthz"):
        return None
    # [DEMO] Public reads: GET/HEAD /api/* skip the login gate when DEMO_MODE is on.
    if DEMO_MODE and request.path.startswith("/api/") and request.method in READ_METHODS:
        return None
    if session.get("erp_authenticated") is True:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication_required"}), 401
    return redirect(f"/login?next={_safe_next(request.full_path if request.query_string else request.path)}")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return _login_page(next_url=request.args.get("next", "/"))

    next_url = _safe_next(request.form.get("next", "/"))
    if _login_limited():
        _audit_auth("login_rate_limited")
        return _login_page("Too many attempts. Wait a few minutes and try again.", next_url), 429

    if not _configured_password_ok(request.form.get("password", "")):
        _record_failed_login()
        return _login_page("Incorrect password.", next_url), 401

    session.clear()
    session.permanent = True
    session["erp_authenticated"] = True
    session["login_at"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    LOGIN_ATTEMPTS.pop(_client_ip(), None)
    _audit_auth("login_success")
    return redirect(next_url)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    response = redirect("/login")
    response.delete_cookie(app.config["SESSION_COOKIE_NAME"], path="/")
    return response

@app.route("/healthz")
def healthz():
    """[DEMO] Unauthenticated liveness probe for Render."""
    return jsonify({"ok": True, "demo_mode": DEMO_MODE, "read_only": True})

# ── DB helpers ────────────────────────────────────────────────────────────────
def db():
    # [DEMO] read-only connection (see demo_db.get_conn)
    return demo_db.get_conn()

def _assumptions():
    with db() as c:
        rows = c.execute("SELECT key, value FROM assumptions").fetchall()
    return {r["key"]: float(r["value"]) for r in rows}

# ── Summary / Dashboard ───────────────────────────────────────────────────────
@app.route("/api/summary")
def api_summary():
    a = _assumptions()
    this_m = datetime.date.today().strftime("%Y-%m")
    with db() as c:
        def ledger_sum(account, col):
            return float(c.execute(
                f"SELECT COALESCE(SUM({col}),0) FROM ledger WHERE account=?", [account]
            ).fetchone()[0])

        revenue       = ledger_sum("REVENUE", "credit")
        shipping_cr   = ledger_sum("SHIPPING", "credit")
        discount_dr   = ledger_sum("DISCOUNT", "debit")
        tax_cr        = ledger_sum("TAX", "credit")
        mfee_dr       = ledger_sum("MERCHANT_FEE", "debit")
        cogs_dr       = ledger_sum("COGS", "debit")
        expense_dr    = ledger_sum("EXPENSE", "debit")
        opex_est      = round(revenue * a.get("opex_pct", 10) / 100, 2)
        gross         = round(revenue + shipping_cr, 2)
        net_after_fees = round(gross - discount_dr - mfee_dr, 2)
        overhead      = round(net_after_fees * a.get("overhead_pct", 5) / 100, 2)
        pre_cogs_net  = round(net_after_fees - overhead, 2)                 # before any COGS/OpEx
        net_est       = round(gross - discount_dr - mfee_dr - overhead - cogs_dr - expense_dr, 2)
        cogs_posted   = (cogs_dr + expense_dr) > 0

        orders        = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        invoices      = c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        pending       = c.execute("SELECT COUNT(*) FROM orders WHERE fulfillment_status='PENDING'").fetchone()[0]
        m_rev         = float(c.execute(
            "SELECT COALESCE(SUM(credit),0) FROM ledger WHERE account='REVENUE' AND substr(entry_date,1,7)=?",
            [this_m]).fetchone()[0])
        open_ap       = float(c.execute(
            "SELECT COALESCE(SUM(amount-paid_amount),0) FROM vendor_invoices WHERE status!='PAID'"
        ).fetchone()[0])
        reminders     = c.execute(
            "SELECT * FROM reminders WHERE dismissed=0 AND due_date>=? ORDER BY due_date LIMIT 6",
            [datetime.date.today().isoformat()]).fetchall()

    return jsonify({
        "revenue": revenue, "shipping_collected": shipping_cr,
        "discount_absorbed": discount_dr, "tax_collected": tax_cr,
        "merchant_fees": mfee_dr, "gross": gross, "net_after_fees": net_after_fees,
        "overhead": overhead, "net_est": net_est,
        "pre_cogs_net": pre_cogs_net, "cogs": round(cogs_dr, 2),
        "expense": round(expense_dr, 2), "cogs_posted": cogs_posted,
        "opex_est": opex_est, "open_ap": open_ap,
        "order_count": orders, "invoice_count": invoices, "pending_orders": pending,
        "this_month_revenue": round(m_rev, 2),
        "reminders": [dict(r) for r in reminders],
    })

# ── Monthly P&L ───────────────────────────────────────────────────────────────
@app.route("/api/pl/monthly")
def api_monthly_pl():
    from demo_db import get_monthly_pl
    return jsonify(get_monthly_pl())

# ── Financial Statements ──────────────────────────────────────────────────────
@app.route("/api/statements")
def api_statements():
    a = _assumptions()
    with db() as c:
        def s(account, col):
            return float(c.execute(
                f"SELECT COALESCE(SUM({col}),0) FROM ledger WHERE account=?", [account]
            ).fetchone()[0])
        revenue     = s("REVENUE", "credit")
        ship_cr     = s("SHIPPING", "credit")
        disc        = s("DISCOUNT", "debit")
        tax         = s("TAX", "credit")
        mfee        = s("MERCHANT_FEE", "debit")
        cogs        = s("COGS", "debit")
        expense     = s("EXPENSE", "debit")
        open_ap     = float(c.execute(
            "SELECT COALESCE(SUM(amount-paid_amount),0) FROM vendor_invoices WHERE status!='PAID'"
        ).fetchone()[0])
        # Estimated supplier invoices (auto-allocated placeholder)
        est_supplier = float(c.execute("""
            SELECT COALESCE(SUM(o.subtotal - o.tax - (o.grand_total * ? / 100 + ?)), 0)
            FROM orders o WHERE o.fulfillment_status='FULFILLED'
        """, [a.get("merchant_fee_pct", 2.9), a.get("merchant_fee_fixed", 0.30)]).fetchone()[0])

    gross        = revenue + ship_cr
    net_after_fees = round(gross - disc - mfee, 2)   # after discounts + merchant fees
    plat_fee     = round(gross * a.get("platform_fee_pct", 0) / 100, 2)
    overhead     = round(net_after_fees * a.get("overhead_pct", 5) / 100, 2)
    opex_est     = round(gross * a.get("opex_pct", 10) / 100, 2)
    supplier_ph  = round(max(0, est_supplier) * a.get("supplier_invoice_auto_pct", 100) / 100, 2)
    actual_cogs  = cogs if cogs > 0 else supplier_ph   # use actual if posted, else placeholder
    net          = round(gross - disc - mfee - plat_fee - overhead - actual_cogs - expense, 2)
    target       = round(gross * a.get("target_margin_pct", 5) / 100, 2)

    pl = [
        {"section": "Revenue",       "line": "Product sales",                       "amount":  revenue,       "note": "Subtotals from fulfilled orders"},
        {"section": "Revenue",       "line": "Shipping collected",                   "amount":  ship_cr,       "note": "Net shipping paid by customers"},
        {"section": "Revenue",       "line": "GROSS REVENUE",                        "amount":  gross,         "note": "", "bold": True},
        {"section": "Deductions",    "line": "Free shipping / promotions absorbed",  "amount": -disc,          "note": "Cost of shipping comped to customers"},
        {"section": "Deductions",    "line": "Merchant / processing fees",           "amount": -mfee,          "note": f"{a.get('merchant_fee_pct',2.9)}% + ${a.get('merchant_fee_fixed',0.30):.2f}/txn"},
        {"section": "Deductions",    "line": "Platform / commission fees",           "amount": -plat_fee,      "note": f"{a.get('platform_fee_pct',0)}% of gross"},
        {"section": "Deductions",    "line": "NET AFTER FEES",                       "amount":  net_after_fees,"note": "Cash available after all deductions", "bold": True},
        {"section": "Overhead",      "line": f"Overhead allocation ({a.get('overhead_pct',5)}%)", "amount": -overhead, "note": "Utilities, packaging, labor, misc — applied after fees"},
        {"section": "COGS",          "line": "Cost of goods — actual (vendor inv.)", "amount": -cogs,          "note": "Posted from vendor invoices"},
        {"section": "COGS",          "line": "Cost of goods — est. placeholder",    "amount": -supplier_ph if cogs == 0 else 0, "note": "Auto: revenue − tax − fees (cleared when actual COGS posted)"},
        {"section": "Operating Exp", "line": "OpEx allocation (est.)",              "amount": -opex_est,      "note": f"{a.get('opex_pct',10)}% of gross — general overhead"},
        {"section": "Operating Exp", "line": "Other expenses",                      "amount": -expense,       "note": "Manual expense entries"},
        {"section": "Net Income",    "line": "ESTIMATED NET INCOME",                "amount":  net,           "note": "Management estimate", "bold": True},
        {"section": "Target",        "line": f"Target profit ({a.get('target_margin_pct',5)}%)", "amount": target, "note": "Scenario target"},
        {"section": "Target",        "line": "Net vs. target",                      "amount":  net - target,  "note": "Surplus / (shortfall)"},
    ]
    bs = [
        {"section": "Assets",      "line": "Gross receipts (cash in)",          "amount":  gross - tax,   "note": "Revenue + shipping − NY tax"},
        {"section": "Assets",      "line": "Tax collected (held for NY)",        "amount":  tax,           "note": "Held liability — remit quarterly"},
        {"section": "Liabilities", "line": "Sales tax payable (NY 8.875%)",     "amount": -tax,           "note": "ST-100 quarterly"},
        {"section": "Liabilities", "line": "Accounts payable — open",           "amount": -open_ap,       "note": "Unpaid vendor invoices"},
        {"section": "Liabilities", "line": "Est. supplier COGS payable",        "amount": -supplier_ph if cogs == 0 else 0, "note": "Placeholder until actual invoices posted"},
        {"section": "Equity",      "line": "Retained earnings (est.)",          "amount":  net,           "note": "Current period estimate"},
    ]
    return jsonify({"pl": pl, "bs": bs, "overhead": overhead, "net_after_fees": net_after_fees,
                    "supplier_ph": supplier_ph, "actual_cogs": actual_cogs})

# ── Orders ────────────────────────────────────────────────────────────────────
@app.route("/api/orders")
def api_orders():
    page  = max(1, int(request.args.get("page", 1)))
    limit = min(200, max(1, int(request.args.get("limit", 20))))
    q     = (request.args.get("q", "") or "").strip()
    offset = (page - 1) * limit
    whitelist = {"created_on": "o.created_on", "grand_total": "o.grand_total",
                 "customer_name": "o.customer_name", "order_number": "o.order_number",
                 "subtotal": "o.subtotal", "tax": "o.tax", "discount_total": "o.discount_total"}
    order_by = _sort_clause(request.args.get("sort"), request.args.get("dir"),
                            whitelist, "created_on")
    base = """SELECT o.*, i.invoice_number, i.emailed_at
              FROM orders o LEFT JOIN invoices i ON i.order_id=o.id"""
    where, params = "", []
    if q:
        where = " WHERE (o.customer_name LIKE ? OR o.customer_email LIKE ? OR o.order_number LIKE ?)"
        like = f"%{q}%"; params = [like, like, like]
    with db() as c:
        rows = c.execute(base + where + f" {order_by} LIMIT ? OFFSET ?",
                         params + [limit, offset]).fetchall()
        total = c.execute("SELECT COUNT(*) FROM orders o" + where, params).fetchone()[0]
    return jsonify({"orders": [dict(r) for r in rows], "total": total, "page": page,
                    "pages": max(1, (total + limit - 1) // limit)})

# ── Invoices ──────────────────────────────────────────────────────────────────
@app.route("/api/invoices/recent")
def api_recent_invoices():
    with db() as c:
        rows = c.execute("""SELECT i.*, o.customer_name, o.customer_email
                            FROM invoices i JOIN orders o ON o.id=i.order_id
                            ORDER BY i.created_at DESC LIMIT 100""").fetchall()
    return jsonify([dict(r) for r in rows])

def _sort_clause(sort, dir_, whitelist, default):
    """Whitelist sort column + direction to keep ORDER BY index-backed and
    injection-safe. Returns a SQL fragment."""
    col = whitelist.get(sort, whitelist[default])
    d = "ASC" if (dir_ or "").lower() == "asc" else "DESC"
    return f"ORDER BY {col} {d}"

@app.route("/api/invoices")
def api_invoices():
    """Scalable invoices list: server-side search + sort + pagination.
    Never returns more than `limit` rows, so it works the same with 50 or
    50,000,000 invoices in the table."""
    page  = max(1, int(request.args.get("page", 1)))
    limit = min(200, max(1, int(request.args.get("limit", 25))))
    q     = (request.args.get("q", "") or "").strip()
    offset = (page - 1) * limit
    whitelist = {"invoice_date": "i.invoice_date", "grand_total": "i.grand_total",
                 "invoice_number": "i.invoice_number", "customer_name": "o.customer_name",
                 "emailed_at": "i.emailed_at", "created_at": "i.created_at"}
    order_by = _sort_clause(request.args.get("sort"), request.args.get("dir"),
                            whitelist, "created_at")
    base = """FROM invoices i JOIN orders o ON o.id=i.order_id"""
    params = []
    where = ""
    if q:
        where = (" WHERE (i.invoice_number LIKE ? OR o.customer_name LIKE ? "
                 "OR i.emailed_to LIKE ?)")
        like = f"%{q}%"
        params = [like, like, like]
    with db() as c:
        # Count without the JOIN when unfiltered — far faster at 1M+ rows.
        if where:
            total = c.execute(f"SELECT COUNT(*) {base}{where}", params).fetchone()[0]
        else:
            total = c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        rows = c.execute(
            f"SELECT i.*, o.customer_name, o.customer_email {base}{where} "
            f"{order_by} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    return jsonify({"invoices": [dict(r) for r in rows], "total": total,
                    "page": page, "pages": max(1, (total + limit - 1) // limit),
                    "limit": limit})

# ── Ledger ────────────────────────────────────────────────────────────────────
@app.route("/api/ledger")
def api_ledger():
    page   = int(request.args.get("page", 1))
    limit  = int(request.args.get("limit", 50))
    offset = (page - 1) * limit
    with db() as c:
        rows  = c.execute("SELECT * FROM ledger ORDER BY entry_date DESC, id DESC LIMIT ? OFFSET ?", [limit, offset]).fetchall()
        total = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    return jsonify({"entries": [dict(r) for r in rows], "total": total})

# ── Assumptions ───────────────────────────────────────────────────────────────
@app.route("/api/assumptions")
def api_assumptions():
    from demo_db import get_assumptions
    return jsonify(get_assumptions())

# [DEMO] WRITE ROUTE REMOVED — POST /api/assumptions (api_save_assumptions). Refused by _demo_guard.

# ── Vendors / AP ─────────────────────────────────────────────────────────────
@app.route("/api/vendors")
def api_vendors():
    from demo_db import get_vendors
    return jsonify(get_vendors())

# [DEMO] WRITE ROUTE REMOVED — POST /api/vendors (api_create_vendor). Refused by _demo_guard.

@app.route("/api/vendor_invoices")
def api_vendor_invoices():
    status = request.args.get("status")
    from demo_db import get_vendor_invoices
    return jsonify(get_vendor_invoices(status))

# [DEMO] WRITE ROUTE REMOVED — POST /api/vendor_invoices (api_create_vi). Refused by _demo_guard.

@app.route("/api/vendor_invoices/<int:vi_id>/lines")
def api_vi_lines(vi_id):
    from demo_db import get_invoice_lines
    return jsonify(get_invoice_lines(vi_id))

# [DEMO] WRITE ROUTE REMOVED — PUT /api/vendor_invoices/<int:vi_id>/lines (api_vi_lines_replace). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — PATCH /api/vendor_invoices/<int:vi_id> (api_vi_update). Refused by _demo_guard.

@app.route("/api/margins")
def api_margins():
    by = request.args.get("by", "category")
    from demo_db import get_margins
    return jsonify({"by": by, "rows": get_margins("product" if by == "product" else "category")})

@app.route("/api/products/sold")
def api_products_sold():
    """Distinct product names actually sold (from Squarespace order line items),
    with units sold + revenue, plus any category already assigned on the cost side.
    Feeds the line-item product picker so cost entries match revenue."""
    agg = {}
    with db() as c:
        rows = c.execute(
            "SELECT line_items FROM orders WHERE fulfillment_status='FULFILLED' AND line_items IS NOT NULL"
        ).fetchall()
        cat_rows = c.execute(
            "SELECT DISTINCT LOWER(TRIM(product_name)) k, category FROM vendor_invoice_lines WHERE category IS NOT NULL"
        ).fetchall()
    cat_map = {r["k"]: r["category"] for r in cat_rows}
    for o in rows:
        try:
            items = json.loads(o["line_items"] or "[]")
        except Exception:
            continue
        for it in items:
            name = (it.get("productName") or it.get("title") or "").strip()
            if not name:
                continue
            qty = float(it.get("quantity") or 1)
            up = it.get("unitPricePaid") or {}
            price = float(up.get("value") if isinstance(up, dict) else up or 0)
            e = agg.setdefault(name, {"product": name, "sku": it.get("sku") or "",
                                      "units": 0.0, "revenue": 0.0,
                                      "category": cat_map.get(name.lower(), "")})
            e["units"] += qty
            e["revenue"] += price * qty
    out = sorted(agg.values(), key=lambda x: x["revenue"], reverse=True)
    for e in out:
        e["units"] = round(e["units"], 2)
        e["revenue"] = round(e["revenue"], 2)
    return jsonify(out)

# [DEMO] WRITE ROUTE REMOVED — POST /api/vendor_invoices/<int:vi_id>/pay (api_pay_vi). Refused by _demo_guard.

# ── Checks ────────────────────────────────────────────────────────────────────
@app.route("/api/checks")
def api_checks():
    from demo_db import get_checks
    return jsonify(get_checks(200))

# [DEMO] WRITE ROUTE REMOVED — POST /api/checks (api_create_check). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/checks/<int:cid>/clear (api_clear_check). Refused by _demo_guard.

# ── Reconciliation ────────────────────────────────────────────────────────────
@app.route("/api/reconciliation")
def api_reconciliation():
    with db() as c:
        # Monthly: expected cash = revenue + shipping - discount - tax (what should hit bank)
        months = c.execute("""
            SELECT substr(entry_date,1,7) as month,
                   account, SUM(credit) cr, SUM(debit) dr
            FROM ledger GROUP BY month, account ORDER BY month
        """).fetchall()
        checks_by_month = c.execute("""
            SELECT substr(check_date,1,7) as month, SUM(amount) as total
            FROM checks WHERE voided=0 GROUP BY month
        """).fetchall()

    monthly = {}
    for r in months:
        m = r["month"]
        if m not in monthly: monthly[m] = {}
        monthly[m][r["account"]] = {"cr": float(r["cr"] or 0), "dr": float(r["dr"] or 0)}

    check_map = {r["month"]: float(r["total"] or 0) for r in checks_by_month}
    result = []
    for m, accts in sorted(monthly.items()):
        rev    = accts.get("REVENUE", {}).get("cr", 0)
        ship   = accts.get("SHIPPING", {}).get("cr", 0)
        disc   = accts.get("DISCOUNT", {}).get("dr", 0)
        tax    = accts.get("TAX", {}).get("cr", 0)
        mfee   = accts.get("MERCHANT_FEE", {}).get("dr", 0)
        # Expected net deposit = gross - discount - merchant fee (tax collected but held)
        expected_deposit = round(rev + ship - disc - mfee, 2)
        checks_out = check_map.get(m, 0)
        result.append({
            "month": m, "revenue": round(rev,2), "shipping": round(ship,2),
            "discounts": round(disc,2), "merchant_fees": round(mfee,2),
            "expected_deposit": expected_deposit,
            "checks_issued": round(checks_out,2),
            "net_cash": round(expected_deposit - checks_out, 2),
            "tax_held": round(tax,2),
        })
    return jsonify(result)

# ── Tax ───────────────────────────────────────────────────────────────────────
@app.route("/api/tax")
def api_tax():
    today = datetime.date.today()
    year  = today.year
    quarters = [
        {"label": f"Q1 {year}", "start": f"{year}-01-01", "end": f"{year}-03-31", "due": f"{year}-03-20"},
        {"label": f"Q2 {year}", "start": f"{year}-04-01", "end": f"{year}-06-30", "due": f"{year}-06-20"},
        {"label": f"Q3 {year}", "start": f"{year}-07-01", "end": f"{year}-09-30", "due": f"{year}-09-20"},
        {"label": f"Q4 {year}", "start": f"{year}-10-01", "end": f"{year}-12-31", "due": f"{year+1}-03-20"},
    ]
    result = []
    with db() as c:
        for q in quarters:
            rev = float(c.execute("SELECT COALESCE(SUM(credit),0) FROM ledger WHERE account='REVENUE' AND entry_date BETWEEN ? AND ?", [q["start"], q["end"]]).fetchone()[0])
            tax = float(c.execute("SELECT COALESCE(SUM(credit),0) FROM ledger WHERE account='TAX' AND entry_date BETWEEN ? AND ?", [q["start"], q["end"]]).fetchone()[0])
            filed = c.execute("SELECT filed_on FROM tax_log WHERE period_start=? AND period_end=?", [q["start"], q["end"]]).fetchone()
            result.append({**q, "revenue": round(rev,2), "tax": round(tax,2),
                           "filed": filed["filed_on"] if filed else None,
                           "overdue": today.isoformat() > q["due"] and not (filed and filed["filed_on"])})
    return jsonify(result)

# [DEMO] WRITE ROUTE REMOVED — POST /api/tax/file (api_tax_file). Refused by _demo_guard.

# ── Federal Tax Forms engine (1099-NEC / 1099-MISC / W-2 / W-9 / 1040-BWH) ─────
# [DEMO] No tax_forms/ directory is shipped; generated-form rows point at placeholder paths.

def _demo_no_file(kind):
    """[DEMO] Stand-in for every file-download route: no real documents are shipped."""
    return jsonify({"error": f"{kind} not available in the demo",
                    "detail": "The public demo ships synthetic data only; generated documents are not included."}), 404

@app.route("/api/tax-forms/config")
def api_tf_config():
    import tax_forms_config as CFG
    return jsonify({"tax_year": CFG.TAX_YEAR, "revisions": CFG.FORM_REVISIONS,
                    "rates": CFG.RATES, "box12_codes": CFG.BOX_12_CODES,
                    "disclaimer": CFG.PDF_DISCLAIMER})

# [DEMO] POST branch removed (set payer) — GET only.
@app.route("/api/tax-forms/payer")
def api_tf_payer():
    with db() as c:
        r = c.execute("SELECT * FROM tax_payer ORDER BY id DESC LIMIT 1").fetchone()
    return jsonify(dict(r) if r else {})

# [DEMO] POST branch removed (create payee + payments) — GET only.
@app.route("/api/tax-forms/payees")
def api_tf_payees():
    with db() as c:
        rows = c.execute("""
            SELECT p.*, pm.gross_wages, pm.total_paid, pm.fed_wh, pm.state_wh, pm.local_wh,
                   pm.pretax_sec125, pm.pretax_401k, pm.box12, pm.tax_year
            FROM tax_payees p
            LEFT JOIN tax_payments pm ON pm.payee_id=p.id
            ORDER BY p.id DESC""").fetchall()
    return jsonify([dict(r) for r in rows])

# [DEMO] WRITE ROUTE REMOVED — DELETE /api/tax-forms/payees/<int:pid> (api_tf_payee_delete). Refused by _demo_guard.

def _tf_year():
    import tax_forms_config as CFG
    return CFG.TAX_YEAR

def _tf_payer_dict(c):
    r = c.execute("SELECT * FROM tax_payer ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else {}

@app.route("/api/tax-forms/validate")
def api_tf_validate():
    import tax_engine as TE
    with db() as c:
        payer = _tf_payer_dict(c)
        payees = [dict(r) for r in c.execute("SELECT * FROM tax_payees").fetchall()]
    for p in payees:
        p["w9_on_file"] = bool(p.get("w9_on_file"))
    exceptions = TE.ValidatorEngine.cross_validation_report(payer, payees)
    return jsonify({"payer": payer, "payee_count": len(payees), "exceptions": exceptions,
                    "clean": not any(e["severity"] == "ERROR" for e in exceptions)})

# [DEMO] WRITE ROUTE REMOVED — POST /api/tax-forms/generate (api_tf_generate). Refused by _demo_guard.

@app.route("/api/tax-forms/generated")
def api_tf_generated():
    with db() as c:
        rows = c.execute("""
            SELECT g.*, p.legal_name, p.kind FROM tax_generated_forms g
            LEFT JOIN tax_payees p ON p.id=g.payee_id ORDER BY g.id DESC""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tax-forms/<int:fid>/pdf")
def api_tf_pdf(fid):
    return _demo_no_file("Tax form PDF")          # [DEMO] was send_file(pdf_path)

@app.route("/api/tax-forms/<int:fid>/audit-zip")
def api_tf_zip(fid):
    return _demo_no_file("Tax form audit ZIP")    # [DEMO] was send_file(zip_path)

# ── Reminders ─────────────────────────────────────────────────────────────────
@app.route("/api/reminders")
def api_reminders():
    with db() as c:
        rows = c.execute("SELECT * FROM reminders WHERE dismissed=0 ORDER BY due_date").fetchall()
    return jsonify([dict(r) for r in rows])

# [DEMO] WRITE ROUTE REMOVED — POST /api/reminders/dismiss/<int:rid> (api_dismiss). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/reminders (api_create_reminder). Refused by _demo_guard.

# ── Sync ──────────────────────────────────────────────────────────────────────
# [DEMO] WRITE ROUTE REMOVED — POST /api/sync (api_sync). Refused by _demo_guard.

# ── Export CSV ────────────────────────────────────────────────────────────────
@app.route("/api/export/orders.csv")
def export_orders_csv():
    from flask import Response
    with db() as c:
        rows = c.execute("""SELECT o.created_on, o.fulfilled_on, o.order_number,
                            o.customer_name, o.customer_email,
                            o.subtotal, o.shipping_listed, o.shipping_collected,
                            o.discount_total, o.tax, o.refund_total, o.grand_total,
                            o.fulfillment_status, o.shipping_method,
                            i.invoice_number, i.emailed_at
                            FROM orders o LEFT JOIN invoices i ON i.order_id=o.id
                            ORDER BY o.created_on""").fetchall()
    lines = ["Date,Fulfilled,Order#,Customer,Email,Subtotal,ShippingListed,ShippingCollected,Discount,Tax,Refund,GrandTotal,Status,ShippingMethod,InvoiceNumber,InvoicedAt"]
    for r in rows:
        lines.append(",".join(str(v or "") for v in r))
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=prime_industrial_orders.csv"})

@app.route("/api/export/ledger.csv")
def export_ledger_csv():
    from flask import Response
    with db() as c:
        rows = c.execute("SELECT entry_date,account,description,debit,credit,reference FROM ledger ORDER BY entry_date").fetchall()
    lines = ["Date,Account,Description,Debit,Credit,Reference"]
    for r in rows:
        lines.append(",".join(f'"{v}"' if "," in str(v or "") else str(v or "") for v in r))
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=prime_industrial_ledger.csv"})

# ── Plaid / Bank integration ───────────────────────────────────────────────────
def _plaid():
    # [DEMO] Plaid is never contacted — plaid_integration is not shipped with the demo.
    return None

def _public_item(row):
    """[DEMO] Never serialize Plaid credentials/cursors, even synthetic ones."""
    d = dict(row)
    d.pop("access_token", None)
    d.pop("cursor", None)
    return d

# [DEMO] WRITE ROUTE REMOVED — POST /api/plaid/link-token (api_plaid_link_token). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/plaid/exchange-token (api_plaid_exchange). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/plaid/set-account (api_plaid_set_account). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/plaid/sync (api_plaid_sync). Refused by _demo_guard.

@app.route("/api/plaid/accounts")
def api_plaid_accounts():
    with db() as c:
        items = c.execute("SELECT * FROM plaid_items").fetchall()
    fns = _plaid()
    out = []
    for row in items:
        item = _public_item(row)
        if fns:
            try:
                item["accounts"] = fns[4](row["item_id"])
            except Exception:
                item["accounts"] = []
        out.append(item)
    return jsonify(out)

@app.route("/api/plaid/items")
def api_plaid_items():
    with db() as c:
        rows = c.execute("SELECT * FROM plaid_items").fetchall()
    return jsonify([_public_item(r) for r in rows])

@app.route("/api/bank/transactions")
def api_bank_transactions():
    page    = int(request.args.get("page", 1))
    limit   = int(request.args.get("limit", 50))
    q       = request.args.get("q", "").strip()
    cat     = request.args.get("category", "").strip()
    recon   = request.args.get("reconciled", "")
    offset  = (page - 1) * limit
    clauses, params = [], []
    if q:
        clauses.append("(name LIKE ? OR merchant_name LIKE ? OR memo LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if cat:
        clauses.append("erp_category=?"); params.append(cat)
    if recon == "0":
        clauses.append("reconciled=0 AND ignored=0")
    elif recon == "1":
        clauses.append("reconciled=1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM bank_transactions {where}", params).fetchone()[0]
        rows  = c.execute(
            f"SELECT * FROM bank_transactions {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
    return jsonify({"total": total, "transactions": [dict(r) for r in rows]})

# [DEMO] WRITE ROUTE REMOVED — PATCH /api/bank/transactions/<int:tid> (api_bank_update_txn). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/bank/transactions/batch-memo (api_bank_batch_memo). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/bank/repost-ledger (api_bank_repost_ledger). Refused by _demo_guard.

@app.route("/api/bank/summary")
def api_bank_summary():
    with db() as c:
        row = c.execute("""
            SELECT
              COUNT(*) total,
              SUM(CASE WHEN reconciled=0 AND ignored=0 AND pending=0 THEN 1 ELSE 0 END) unreconciled,
              SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) total_debits,
              SUM(CASE WHEN amount<0 THEN ABS(amount) ELSE 0 END) total_credits,
              MIN(date) earliest, MAX(date) latest
            FROM bank_transactions WHERE ignored=0
        """).fetchone()
        items = c.execute("SELECT * FROM plaid_items").fetchall()
    return jsonify({
        "connected": len(items) > 0,
        "items": [_public_item(i) for i in items],
        "total_transactions": row["total"],
        "unreconciled": row["unreconciled"],
        "total_debits": row["total_debits"] or 0,
        "total_credits": row["total_credits"] or 0,
        "date_range": f"{row['earliest'] or '—'} → {row['latest'] or '—'}",
    })

@app.route("/api/bank/suggest-matches/<int:tid>")
def api_bank_suggest(tid):
    """Suggest ERP order or vendor invoice matches for a bank transaction."""
    with db() as c:
        txn = c.execute("SELECT * FROM bank_transactions WHERE id=?", [tid]).fetchone()
        if not txn:
            return jsonify([])
        txn = dict(txn)
        amt = abs(txn["amount"])
        date = txn["date"]
        # Find orders or invoices within $2 and 7 days
        orders = c.execute("""
            SELECT id, order_number, customer_name, grand_total, created_on
            FROM orders WHERE ABS(grand_total - ?) < 2
              AND ABS(julianday(created_on) - julianday(?)) <= 7
            ORDER BY ABS(grand_total - ?) LIMIT 5
        """, [amt, date, amt]).fetchall()
        vis = c.execute("""
            SELECT vi.id, vi.invoice_number, v.name vendor_name, vi.amount, vi.invoice_date
            FROM vendor_invoices vi JOIN vendors v ON vi.vendor_id=v.id
            WHERE ABS(vi.amount - ?) < 2
              AND ABS(julianday(vi.invoice_date) - julianday(?)) <= 14
            ORDER BY ABS(vi.amount - ?) LIMIT 5
        """, [amt, date, amt]).fetchall()
    return jsonify({
        "orders": [dict(r) for r in orders],
        "vendor_invoices": [dict(r) for r in vis],
    })

@app.route("/api/cost-scenarios")
def api_cost_scenarios():
    """What-if cost scenarios. Because no real COGS is posted yet, net income
    currently reflects gross profit after fees + overhead only. These scenarios
    model different cost assumptions so the user can see the spread."""
    a = _assumptions()
    with db() as c:
        def s(account, col):
            return float(c.execute(
                f"SELECT COALESCE(SUM({col}),0) FROM ledger WHERE account=?", [account]
            ).fetchone()[0])
        revenue   = s("REVENUE", "credit")     # product subtotal
        ship_cr   = s("SHIPPING", "credit")    # shipping collected from customers
        disc      = s("DISCOUNT", "debit")     # shipping absorbed / promos
        tax       = s("TAX", "credit")
        mfee      = s("MERCHANT_FEE", "debit")
        cogs_real = s("COGS", "debit")
        expense   = s("EXPENSE", "debit")
        with_ship = float(c.execute(
            "SELECT COALESCE(SUM(shipping_listed),0) FROM orders WHERE fulfillment_status='FULFILLED'"
        ).fetchone()[0])

    overhead_pct   = a.get("overhead_pct", 5)
    gross          = round(revenue + ship_cr, 2)
    net_after_fees = round(gross - disc - mfee, 2)
    overhead       = round(net_after_fees * overhead_pct / 100, 2)
    baseline_net   = round(net_after_fees - overhead - cogs_real - expense, 2)  # current shown net
    sale_price     = round(revenue, 2)               # "cost = actual sale price"
    shipping_cost  = round(disc, 2)                  # shipping absorbed by business
    shipping_listed= round(with_ship, 2)
    breakeven_cogs = round(net_after_fees - overhead - expense, 2)  # COGS that makes net = 0

    def net_with_cogs(cogs):
        return round(net_after_fees - overhead - cogs - expense, 2)

    has_real_cogs = (cogs_real + expense) > 0
    scenarios = [
        {
            "key": "current",
            "title": "Reconciled Actual" if has_real_cogs else "Current (no COGS posted)",
            "desc": ("Real cost of goods from posted vendor invoices. This is your true "
                     "net income." if has_real_cogs else
                     "Cost of goods is $0 because no supplier invoices are recorded yet. "
                     "This figure is really gross profit after fees & overhead, not true net income."),
            "cogs": round(cogs_real + expense, 2), "net": baseline_net,
            "status": "ACTUAL" if has_real_cogs else "OVERSTATED",
            "color": "green" if has_real_cogs else "amber",
        },
        {
            "key": "cost_eq_sale", "title": "Cost = Sale Price",
            "desc": "Assumes each item cost exactly what it sold for (100% COGS). "
                    "Product margin is zero; the loss equals fees + overhead.",
            "cogs": sale_price, "net": net_with_cogs(sale_price),
            "status": "LOSS", "color": "red",
        },
        {
            "key": "breakeven", "title": "Break-even",
            "desc": "The COGS at which net income is exactly $0. Equals the current "
                    "after-fees-&-overhead figure. Any real cost above this is a loss.",
            "cogs": breakeven_cogs, "net": net_with_cogs(breakeven_cogs),
            "status": "BREAK-EVEN", "color": "blue",
        },
        {
            "key": "loss_eq_shipping", "title": "Loss = Shipping Cost",
            "desc": "Break-even COGS plus the shipping the business absorbs. "
                    "The operation runs at a loss exactly equal to absorbed shipping.",
            "cogs": round(breakeven_cogs + shipping_cost, 2),
            "net": net_with_cogs(breakeven_cogs + shipping_cost),
            "status": "LOSS", "color": "red",
        },
    ]
    return jsonify({
        "inputs": {
            "revenue": revenue, "shipping_collected": ship_cr, "gross": gross,
            "discount_absorbed": disc, "merchant_fees": mfee, "tax": tax,
            "overhead_pct": overhead_pct, "overhead": overhead,
            "net_after_fees": net_after_fees, "expense": expense,
            "cogs_actual": round(cogs_real, 2), "sale_price": sale_price,
            "shipping_cost": shipping_cost, "shipping_listed": shipping_listed,
            "breakeven_cogs": breakeven_cogs, "baseline_net": baseline_net,
        },
        "scenarios": scenarios,
    })

# ── Invoice PDF serving ───────────────────────────────────────────────────────
# [DEMO] No invoices_pdf/ store and no Squarespace regeneration (_regenerate_pdf removed:
# it called the Squarespace API and wrote to the DB). Real invoice PDFs contain customer
# names/addresses and are never shipped with the demo.
@app.route("/api/invoices/<invoice_number>/pdf")
def api_invoice_pdf(invoice_number):
    with db() as c:
        row = c.execute("SELECT 1 FROM invoices WHERE invoice_number=?", [invoice_number]).fetchone()
    if not row:
        return jsonify({"error": "Invoice not found"}), 404
    return _demo_no_file("Invoice PDF")

# ── God Mode (Owner Control Center) ───────────────────────────────────────────
@app.route("/api/godmode/overview")
def api_godmode_overview():
    """All control points: ledger, invoices, checks, bank, tax, forms, security."""
    import demo_owner_control as OC
    return jsonify(OC.run_control_points())

@app.route("/api/godmode/check-matches")
def api_godmode_check_matches():
    import demo_owner_control as OC
    return jsonify(OC.suggest_check_matches())

# [DEMO] WRITE ROUTE REMOVED — POST /api/godmode/match-check (api_godmode_match_check). Refused by _demo_guard.

# [DEMO] WRITE ROUTE REMOVED — POST /api/godmode/rebuild (api_godmode_rebuild). Refused by _demo_guard.

# ── Tax Filing Packages ───────────────────────────────────────────────────────
@app.route("/api/tax-package/calendar")
def api_taxpkg_calendar():
    import demo_owner_control as OC
    return jsonify(OC.filing_calendar(request.args.get("year")))

@app.route("/api/tax-package/list")
def api_taxpkg_list():
    import demo_owner_control as OC
    return jsonify(OC.list_packages())

# [DEMO] WRITE ROUTE REMOVED — POST /api/tax-package/generate (api_taxpkg_generate). Refused by _demo_guard.

@app.route("/api/tax-package/<int:pid>/download")
def api_taxpkg_download(pid):
    return _demo_no_file("Filing package ZIP")   # [DEMO] was send_file(zip_path)

# ── Dashboard ──────────────────────────────────────────────────────────────────
# [DEMO] The dashboard HTML is served by the showcase site (artemis-omni), not here.
@app.route("/")
def dashboard():
    return jsonify({"service": "Prime Industrial ERP — read-only demo API",
                    "data": "synthetic (sanitized)", "demo_mode": DEMO_MODE,
                    "try": ["/api/summary", "/api/orders", "/api/ledger", "/api/tax", "/api/bank/summary"]})

if __name__ == "__main__":
    # [DEMO] No init_db / reminder seeding — the demo DB is read-only.
    port = int(os.environ.get("PORT", "5050"))
    print("\n" + "="*55)
    print(f"  Prime Industrial ERP DEMO — http://127.0.0.1:{port}")
    print(f"  DB: {DB_PATH}  (read-only)  DEMO_MODE={DEMO_MODE}")
    print("="*55 + "\n")
    app.run(host="127.0.0.1", port=port, debug=False)
