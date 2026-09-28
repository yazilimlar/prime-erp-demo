#!/usr/bin/env python3
"""
Prime Industrial — ERP Database Layer (DEMO copy of erp_db.py)
SQLite-backed store for orders, invoices, ledger entries, and tax records.

Demo changes vs erp_db.py:
  * DB location comes from PRIME_ERP_DATA_DIR (default ./data, relative to this
    file) instead of the hard-coded per-user macOS path.
  * get_conn() opens the database READ-ONLY (sqlite URI mode=ro), so the write
    helpers below fail loudly if anything ever reaches them.
  * ensure_seeded() copies the sanitized seed DB into the data dir on first boot
    (e.g. an empty Render persistent disk).
"""

import os, shutil, sqlite3, pathlib, datetime, json

HERE = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path(os.environ.get("PRIME_ERP_DATA_DIR", "./data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = (HERE / DATA_DIR).resolve()
DB_FILENAME = "prime_industrial_demo.db"
DB_PATH = DATA_DIR / DB_FILENAME
SEED_DB = HERE / DB_FILENAME   # produced by sanitize.py — synthetic data only


def ensure_seeded():
    """Make sure DATA_DIR holds the demo DB; copy the sanitized seed if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        if not SEED_DB.exists():
            raise FileNotFoundError(f"No demo DB at {DB_PATH} and no seed at {SEED_DB} — run sanitize.py")
        shutil.copyfile(SEED_DB, DB_PATH)
    return DB_PATH


def get_conn():
    # Read-only: the demo never writes. (No WAL pragma — changing journal mode is a write.)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        -- ── Orders ────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS orders (
            id                TEXT PRIMARY KEY,
            order_number      TEXT,
            squarespace_num   TEXT,
            created_on        TEXT,
            fulfilled_on      TEXT,
            customer_email    TEXT,
            customer_name     TEXT,
            ship_address      TEXT,           -- JSON
            subtotal          REAL,
            shipping_listed   REAL DEFAULT 0, -- carrier rate before discount
            shipping_collected REAL DEFAULT 0,-- what customer actually paid
            discount_total    REAL DEFAULT 0, -- total discount applied
            discount_lines    TEXT DEFAULT '[]', -- JSON array of discount line objects
            tax               REAL,
            refund_total      REAL DEFAULT 0,
            grand_total       REAL,
            fulfillment_status TEXT,
            shipping_method   TEXT,
            line_items        TEXT,           -- JSON
            imported_at       TEXT DEFAULT (datetime('now'))
        );

        -- ── Schema migrations (add new columns if missing) ────────────────────
        -- These are ignored if columns already exist.

        -- ── Invoices ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS invoices (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id          TEXT REFERENCES orders(id),
            invoice_number    TEXT UNIQUE,
            invoice_date      TEXT,
            delivered_date    TEXT,
            return_by_date    TEXT,
            pdf_path          TEXT,
            emailed_to        TEXT,
            emailed_at        TEXT,
            grand_total       REAL,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── General Ledger ────────────────────────────────────────────────────
        -- Accounts: REVENUE / SHIPPING / DISCOUNT / TAX / COGS / EXPENSE / MERCHANT_FEE
        CREATE TABLE IF NOT EXISTS ledger (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date        TEXT,
            account           TEXT,
            description       TEXT,
            debit             REAL DEFAULT 0,
            credit            REAL DEFAULT 0,
            order_id          TEXT,
            reference         TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Vendors / Accounts Payable ────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS vendors (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT NOT NULL,
            contact           TEXT,
            email             TEXT,
            phone             TEXT,
            terms             TEXT DEFAULT 'Net 30',
            notes             TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS vendor_invoices (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id         INTEGER REFERENCES vendors(id),
            invoice_number    TEXT,
            invoice_date      TEXT,
            due_date          TEXT,
            amount            REAL,
            description       TEXT,
            order_id          TEXT,           -- linked sales order if applicable
            paid_on           TEXT,
            paid_amount       REAL DEFAULT 0,
            payment_method    TEXT,           -- CHECK / ACH / CARD / WIRE
            check_number      TEXT,
            status            TEXT DEFAULT 'OPEN', -- OPEN / PARTIAL / PAID
            notes             TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Check Log ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS checks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            check_date        TEXT,
            check_number      TEXT,
            payee             TEXT,
            amount            REAL,
            memo              TEXT,
            bank_account      TEXT DEFAULT 'Chase Business',
            category          TEXT,           -- VENDOR / EXPENSE / TAX / OTHER
            vendor_id         INTEGER,
            vendor_invoice_id INTEGER,
            cleared_on        TEXT,
            voided            INTEGER DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Tax Log ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS tax_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            period_start      TEXT,
            period_end        TEXT,
            gross_sales       REAL,
            taxable_sales     REAL,
            tax_collected     REAL,
            tax_rate_pct      REAL,
            filing_due        TEXT,
            filed_on          TEXT,
            payment_amount    REAL,
            payment_ref       TEXT,
            notes             TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Assumptions ───────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS assumptions (
            key               TEXT PRIMARY KEY,
            value             TEXT,
            label             TEXT,
            notes             TEXT,
            updated_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Reminders ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS reminders (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            due_date          TEXT,
            category          TEXT,
            title             TEXT,
            description       TEXT,
            emailed           INTEGER DEFAULT 0,
            dismissed         INTEGER DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Plaid Bank Connection ──────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS plaid_items (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id           TEXT UNIQUE,
            access_token      TEXT NOT NULL,
            institution_id    TEXT,
            institution_name  TEXT,
            account_id        TEXT,       -- specific account we track
            account_name      TEXT,
            account_mask      TEXT,       -- last 4 digits
            account_type      TEXT,
            cursor            TEXT,       -- Plaid transactions cursor for incremental sync
            last_synced       TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Bank Transactions ──────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            plaid_txn_id      TEXT UNIQUE,
            plaid_item_id     INTEGER REFERENCES plaid_items(id),
            account_id        TEXT,
            date              TEXT,
            authorized_date   TEXT,
            name              TEXT,
            merchant_name     TEXT,
            amount            REAL,       -- positive = debit (money out), negative = credit (money in)
            currency          TEXT DEFAULT 'USD',
            category          TEXT,       -- Plaid category JSON array
            pending           INTEGER DEFAULT 0,
            memo              TEXT,       -- user-written memo
            erp_category      TEXT,       -- REVENUE / COGS / EXPENSE / TRANSFER / TAX / FEE / OTHER
            matched_order_id  TEXT,       -- linked ERP order if reconciled
            matched_vi_id     INTEGER,    -- linked vendor invoice if reconciled
            reconciled        INTEGER DEFAULT 0,
            ignored           INTEGER DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Vendor Invoice Line Items (product / SKU / category level) ─────────
        CREATE TABLE IF NOT EXISTS vendor_invoice_lines (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_invoice_id INTEGER REFERENCES vendor_invoices(id) ON DELETE CASCADE,
            product_name      TEXT NOT NULL,
            sku               TEXT,
            category          TEXT,
            quantity          REAL DEFAULT 1,
            unit_cost         REAL DEFAULT 0,   -- cost per unit (goods only)
            line_tax          REAL DEFAULT 0,   -- purchase tax on this line
            line_shipping     REAL DEFAULT 0,   -- inbound freight on this line
            line_fee          REAL DEFAULT 0,   -- other fees on this line
            notes             TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );

        -- ── Federal Tax Forms engine ──────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS tax_payer (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            legal_name    TEXT, ein TEXT, address TEXT,
            state_id      TEXT, locality TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tax_payees (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT DEFAULT 'CONTRACTOR',   -- EMPLOYEE | CONTRACTOR
            legal_name    TEXT, business_name TEXT, tin TEXT, address TEXT,
            tax_classification TEXT,
            w9_on_file    INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tax_payments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            payee_id      INTEGER REFERENCES tax_payees(id) ON DELETE CASCADE,
            tax_year      INTEGER,
            gross_wages   REAL DEFAULT 0, total_paid REAL DEFAULT 0,
            fed_wh        REAL DEFAULT 0, state_wh REAL DEFAULT 0, local_wh REAL DEFAULT 0,
            pretax_sec125 REAL DEFAULT 0, pretax_401k REAL DEFAULT 0,
            box12         TEXT DEFAULT '[]',
            ingested_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tax_generated_forms (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            form_type     TEXT, payee_id INTEGER, tax_year INTEGER,
            pdf_path      TEXT, zip_path TEXT, efile_path TEXT,
            checksum_pdf  TEXT, checksum_zip TEXT,
            flags         TEXT DEFAULT '[]',
            status        TEXT DEFAULT 'GENERATED',
            created_at    TEXT DEFAULT (datetime('now'))
        );

        -- ── Tax Filing Packages (periodic/annual submission bundles) ──────────
        CREATE TABLE IF NOT EXISTS tax_packages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tax_year      INTEGER,
            period        TEXT,            -- Q1 / Q2 / Q3 / Q4 / ANNUAL
            zip_path      TEXT,
            sha256        TEXT,
            file_count    INTEGER,
            bytes         INTEGER,
            figures       TEXT DEFAULT '{}',  -- JSON key-figures snapshot
            created_at    TEXT DEFAULT (datetime('now'))
        );

        -- ── Indexes ───────────────────────────────────────────────────────────
        -- Scale indexes: keep ORDER BY / pagination fast at 1M+ rows
        CREATE INDEX IF NOT EXISTS idx_orders_created    ON orders(created_on);
        CREATE INDEX IF NOT EXISTS idx_orders_grand      ON orders(grand_total);
        CREATE INDEX IF NOT EXISTS idx_orders_custname   ON orders(customer_name);
        CREATE INDEX IF NOT EXISTS idx_orders_ordernum   ON orders(order_number);
        CREATE INDEX IF NOT EXISTS idx_inv_created       ON invoices(created_at);
        CREATE INDEX IF NOT EXISTS idx_inv_date          ON invoices(invoice_date);
        CREATE INDEX IF NOT EXISTS idx_inv_grand         ON invoices(grand_total);
        CREATE INDEX IF NOT EXISTS idx_inv_emailedto     ON invoices(emailed_to);
        CREATE INDEX IF NOT EXISTS idx_inv_order         ON invoices(order_id);
        CREATE INDEX IF NOT EXISTS idx_taxpay_payee ON tax_payments(payee_id);
        CREATE INDEX IF NOT EXISTS idx_taxgen_payee ON tax_generated_forms(payee_id);
        CREATE INDEX IF NOT EXISTS idx_vil_invoice ON vendor_invoice_lines(vendor_invoice_id);
        CREATE INDEX IF NOT EXISTS idx_vil_category ON vendor_invoice_lines(category);
        CREATE INDEX IF NOT EXISTS idx_orders_fulfilled ON orders(fulfilled_on);
        CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(customer_email);
        CREATE INDEX IF NOT EXISTS idx_invoices_order ON invoices(order_id);
        CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger(entry_date);
        CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger(account);
        CREATE INDEX IF NOT EXISTS idx_ledger_order ON ledger(order_id);
        CREATE INDEX IF NOT EXISTS idx_tax_period ON tax_log(period_start, period_end);
        CREATE INDEX IF NOT EXISTS idx_checks_date ON checks(check_date);
        CREATE INDEX IF NOT EXISTS idx_vi_vendor ON vendor_invoices(vendor_id);
        CREATE INDEX IF NOT EXISTS idx_vi_status ON vendor_invoices(status);
        CREATE INDEX IF NOT EXISTS idx_bank_date ON bank_transactions(date);
        CREATE INDEX IF NOT EXISTS idx_bank_reconciled ON bank_transactions(reconciled);
        """)

        # Safe column migrations for existing DBs
        _add_column_if_missing(conn, "orders", "shipping_listed",    "REAL DEFAULT 0")
        _add_column_if_missing(conn, "orders", "shipping_collected", "REAL DEFAULT 0")
        _add_column_if_missing(conn, "orders", "discount_total",     "REAL DEFAULT 0")
        _add_column_if_missing(conn, "orders", "discount_lines",     "TEXT DEFAULT '[]'")
        _add_column_if_missing(conn, "orders", "refund_total",       "REAL DEFAULT 0")
        _add_column_if_missing(conn, "orders", "shipping_method",    "TEXT")
        _add_column_if_missing(conn, "vendor_invoices", "reconciled", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "checks", "bank_txn_id", "INTEGER")

    _seed_defaults()
    print(f"✓ Database initialized: {DB_PATH}")


def _add_column_if_missing(conn, table, column, col_def):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def _seed_defaults():
    defaults = [
        ("merchant_fee_pct",   "2.9",  "Merchant Fee %",         "Squarespace/Stripe processing fee"),
        ("merchant_fee_fixed", "0.30", "Merchant Fee Fixed ($)", "Per-transaction fixed fee"),
        ("opex_pct",           "10.0", "OpEx %",                 "General operating expenses % of revenue"),
        ("target_margin_pct",  "5.0",  "Target Net Margin %",    "Desired profit after all costs"),
        ("platform_fee_pct",   "0.0",  "Platform Fee %",         "Squarespace Commerce fee (if any)"),
        ("freight_review_threshold", "500.0", "Freight Review Threshold ($)", "Flag orders with shipping cost above this"),
    ]
    with get_conn() as conn:
        for key, val, label, notes in defaults:
            conn.execute("""
                INSERT OR IGNORE INTO assumptions (key, value, label, notes) VALUES (?,?,?,?)
            """, (key, val, label, notes))


# ── Order helpers ─────────────────────────────────────────────────────────────

def upsert_order(order_dict):
    o = order_dict
    addr = o.get("shippingAddress") or o.get("billingAddress") or {}
    name = f"{addr.get('firstName','')} {addr.get('lastName','')}".strip()

    shipping_listed   = float((o.get("shippingTotal") or {}).get("value", 0))
    discount_total    = float((o.get("discountTotal") or {}).get("value", 0))
    refund_total      = float((o.get("refundedTotal") or {}).get("value", 0))
    # What customer actually paid for shipping (net of discount)
    shipping_collected = max(0.0, shipping_listed - discount_total)

    discount_lines = o.get("discountLines", [])
    shipping_lines = o.get("shippingLines", [])
    shipping_method = shipping_lines[0].get("method", "") if shipping_lines else ""

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO orders
              (id, order_number, squarespace_num, created_on, fulfilled_on,
               customer_email, customer_name, ship_address,
               subtotal, shipping_listed, shipping_collected,
               discount_total, discount_lines,
               tax, refund_total, grand_total,
               fulfillment_status, shipping_method, line_items)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              fulfilled_on=excluded.fulfilled_on,
              fulfillment_status=excluded.fulfillment_status,
              grand_total=excluded.grand_total,
              tax=excluded.tax,
              shipping_listed=excluded.shipping_listed,
              shipping_collected=excluded.shipping_collected,
              discount_total=excluded.discount_total,
              discount_lines=excluded.discount_lines,
              refund_total=excluded.refund_total,
              shipping_method=excluded.shipping_method
        """, (
            o.get("id"),
            o.get("orderNumber"),
            o.get("orderNumber"),
            str(o.get("createdOn",""))[:10],
            str(o.get("fulfilledOn","") or "")[:10] or None,
            o.get("customerEmail"),
            name,
            json.dumps(addr),
            float((o.get("subtotal") or {}).get("value", 0)),
            shipping_listed,
            shipping_collected,
            discount_total,
            json.dumps(discount_lines),
            float((o.get("taxTotal") or {}).get("value", 0)),
            refund_total,
            float((o.get("grandTotal") or {}).get("value", 0)),
            o.get("fulfillmentStatus"),
            shipping_method,
            json.dumps(o.get("lineItems", [])),
        ))

def invoice_already_sent(order_id):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM invoices WHERE order_id=?", (order_id,)).fetchone()
        return row is not None

def record_invoice(order_id, invoice_number, invoice_date, delivered_date,
                   return_date, pdf_path, emailed_to, grand_total):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO invoices
              (order_id, invoice_number, invoice_date, delivered_date,
               return_by_date, pdf_path, emailed_to, emailed_at, grand_total)
            VALUES (?,?,?,?,?,?,?,datetime('now'),?)
        """, (order_id, invoice_number, invoice_date, delivered_date,
              return_date, str(pdf_path), emailed_to, grand_total))

def post_ledger(order_id, entry_date, subtotal, shipping_listed,
                shipping_collected, discount_total, tax, invoice_number,
                merchant_fee=0.0):
    """
    Post correct ledger entries for an order:
      REVENUE  = product subtotal (credit)
      SHIPPING = what customer actually paid for shipping (credit, usually 0)
      DISCOUNT = free shipping / promo cost absorbed (debit — it's a business cost)
      TAX      = sales tax collected (credit — liability)
      MERCHANT_FEE = processing fee estimate (debit — expense)
    """
    with get_conn() as conn:
        if conn.execute("SELECT id FROM ledger WHERE order_id=? AND account='REVENUE'",
                        (order_id,)).fetchone():
            return  # already posted

        entries = [
            (entry_date, "REVENUE",  f"Product revenue — {invoice_number}",
             0, subtotal, order_id, invoice_number),
        ]
        if shipping_collected > 0:
            entries.append(
                (entry_date, "SHIPPING", f"Shipping collected — {invoice_number}",
                 0, shipping_collected, order_id, invoice_number)
            )
        if discount_total > 0:
            entries.append(
                (entry_date, "DISCOUNT", f"Free shipping / promo — {invoice_number}",
                 discount_total, 0, order_id, invoice_number)
            )
        entries.append(
            (entry_date, "TAX", f"Sales tax collected — {invoice_number}",
             0, tax, order_id, invoice_number)
        )
        if merchant_fee > 0:
            entries.append(
                (entry_date, "MERCHANT_FEE", f"Processing fee — {invoice_number}",
                 merchant_fee, 0, order_id, invoice_number)
            )
        conn.executemany("""
            INSERT INTO ledger (entry_date, account, description, debit, credit, order_id, reference)
            VALUES (?,?,?,?,?,?,?)
        """, entries)


def repost_all_ledger():
    """Re-derive and fix all ledger entries from orders table (idempotent)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM ledger WHERE account IN ('REVENUE','SHIPPING','DISCOUNT','TAX','MERCHANT_FEE')")
        orders = conn.execute("""
            SELECT o.*, i.invoice_number, i.invoice_date
            FROM orders o LEFT JOIN invoices i ON i.order_id = o.id
            WHERE o.fulfillment_status = 'FULFILLED'
        """).fetchall()

        assumptions = {r["key"]: float(r["value"]) for r in
                       conn.execute("SELECT key, value FROM assumptions").fetchall()}
        mfee_pct   = assumptions.get("merchant_fee_pct", 2.9) / 100
        mfee_fixed = assumptions.get("merchant_fee_fixed", 0.30)

    for o in orders:
        subtotal           = float(o["subtotal"] or 0)
        shipping_listed    = float(o["shipping_listed"] or 0)
        shipping_collected = float(o["shipping_collected"] or 0)
        discount_total     = float(o["discount_total"] or 0)
        tax                = float(o["tax"] or 0)
        grand_total        = float(o["grand_total"] or 0)
        inv_num            = o["invoice_number"] or o["order_number"] or o["id"][:8]
        entry_date         = (o["invoice_date"] or o["fulfilled_on"] or o["created_on"])

        merchant_fee = round(grand_total * mfee_pct + mfee_fixed, 2)

        post_ledger(o["id"], entry_date, subtotal, shipping_listed,
                    shipping_collected, discount_total, tax, inv_num, merchant_fee)

    print(f"✓ Re-posted ledger for {len(orders)} orders")


# ── Bank → Ledger posting ─────────────────────────────────────────────────────

# Which ERP categories post to the ledger, and to which account.
# REVENUE is intentionally excluded (orders already credit REVENUE — avoids
# double counting). TRANSFER is internal (no P&L impact). OTHER is unclassified.
_BANK_LEDGER_MAP = {
    "EXPENSE": "EXPENSE",
    "COGS":    "COGS",
    "FEE":     "EXPENSE",   # bank/processing fees recorded as operating expense
    "TAX":     "EXPENSE",   # tax remittance leaving the account (cash out)
}

def post_bank_txn_to_ledger(txn_id):
    """Post (or re-post) a single reconciled bank transaction to the ledger.
    Idempotent: removes any prior BANK:<id> entries first."""
    with get_conn() as conn:
        t = conn.execute("SELECT * FROM bank_transactions WHERE id=?", [txn_id]).fetchone()
        if not t:
            return
        ref = f"BANK:{txn_id}"
        # Always clear prior postings for this txn so edits don't duplicate
        conn.execute("DELETE FROM ledger WHERE reference=?", [ref])

        if not (t["reconciled"] and not t["ignored"]):
            return  # only reconciled, non-ignored txns hit the books

        account = _BANK_LEDGER_MAP.get(t["erp_category"])
        if not account:
            return  # REVENUE / TRANSFER / OTHER / uncategorized → skip

        amt   = abs(float(t["amount"] or 0))
        if amt == 0:
            return
        desc  = (t["memo"] or t["merchant_name"] or t["name"] or "Bank transaction")
        label = f"{t['erp_category']}: {desc}"[:120]
        # Money out of the account = debit to expense/COGS account
        conn.execute("""
            INSERT INTO ledger (entry_date, account, description, debit, credit, reference)
            VALUES (?,?,?,?,0,?)
        """, [t["date"], account, label, amt, ref])

def repost_all_bank_ledger():
    """Re-post every reconciled bank transaction (idempotent full refresh)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM ledger WHERE reference LIKE 'BANK:%'")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM bank_transactions WHERE reconciled=1 AND ignored=0").fetchall()]
    for tid in ids:
        post_bank_txn_to_ledger(tid)
    return len(ids)


# ── Vendor invoice line items + COGS posting ──────────────────────────────────

def _line_goods(l):     return float(l["quantity"] or 0) * float(l["unit_cost"] or 0)
def _line_extra(l):     return float(l["line_tax"] or 0) + float(l["line_shipping"] or 0) + float(l["line_fee"] or 0)

def replace_invoice_lines(vi_id, lines):
    """Replace all line items for an invoice, recompute the invoice amount,
    and re-post the ledger entries (idempotent)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM vendor_invoice_lines WHERE vendor_invoice_id=?", [vi_id])
        for l in (lines or []):
            conn.execute("""
                INSERT INTO vendor_invoice_lines
                  (vendor_invoice_id, product_name, sku, category, quantity,
                   unit_cost, line_tax, line_shipping, line_fee, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, [vi_id, (l.get("product_name") or "").strip(), l.get("sku"),
                  l.get("category"), float(l.get("quantity") or 0),
                  float(l.get("unit_cost") or 0), float(l.get("line_tax") or 0),
                  float(l.get("line_shipping") or 0), float(l.get("line_fee") or 0),
                  l.get("notes")])
        # Recompute the invoice header amount from the lines (if any)
        rows = conn.execute("SELECT * FROM vendor_invoice_lines WHERE vendor_invoice_id=?", [vi_id]).fetchall()
        if rows:
            total = sum(_line_goods(r) + _line_extra(r) for r in rows)
            conn.execute("UPDATE vendor_invoices SET amount=? WHERE id=?", [round(total, 2), vi_id])
    post_vendor_invoice_to_ledger(vi_id)

def get_invoice_lines(vi_id):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM vendor_invoice_lines WHERE vendor_invoice_id=? ORDER BY id", [vi_id]).fetchall()]

def post_vendor_invoice_to_ledger(vi_id):
    """Idempotently post a vendor invoice to the ledger.
    Goods (qty x unit_cost) → COGS debit; tax+shipping+fees → EXPENSE debit.
    Falls back to the header `amount` as COGS if there are no line items.
    Reference tag `VI:<id>` so re-posting never duplicates and order re-posts
    never wipe it."""
    ref = f"VI:{vi_id}"
    with get_conn() as conn:
        conn.execute("DELETE FROM ledger WHERE reference=?", [ref])
        vi = conn.execute("SELECT * FROM vendor_invoices WHERE id=?", [vi_id]).fetchone()
        if not vi:
            return
        lines = conn.execute("SELECT * FROM vendor_invoice_lines WHERE vendor_invoice_id=?", [vi_id]).fetchall()
        date = vi["invoice_date"] or datetime.date.today().isoformat()
        label = vi["invoice_number"] or f"#{vi_id}"
        if lines:
            goods = round(sum(_line_goods(l) for l in lines), 2)
            extra = round(sum(_line_extra(l) for l in lines), 2)
            if goods > 0:
                conn.execute("INSERT INTO ledger (entry_date, account, description, debit, credit, reference) VALUES (?,?,?,?,0,?)",
                             [date, "COGS", f"Vendor COGS — {label} ({len(lines)} line items)", goods, ref])
            if extra > 0:
                conn.execute("INSERT INTO ledger (entry_date, account, description, debit, credit, reference) VALUES (?,?,?,?,0,?)",
                             [date, "EXPENSE", f"Vendor inbound tax/freight/fees — {label}", extra, ref])
        else:
            amt = float(vi["amount"] or 0)
            if amt > 0:
                conn.execute("INSERT INTO ledger (entry_date, account, description, debit, credit, reference) VALUES (?,?,?,?,0,?)",
                             [date, "COGS", f"Vendor COGS — {label}", amt, ref])

def repost_all_vendor_invoices():
    with get_conn() as conn:
        conn.execute("DELETE FROM ledger WHERE reference LIKE 'VI:%'")
        ids = [r["id"] for r in conn.execute("SELECT id FROM vendor_invoices").fetchall()]
    for i in ids:
        post_vendor_invoice_to_ledger(i)
    return len(ids)

def get_margins(group_by="category"):
    """Revenue (from Squarespace order line items) vs COGS (from vendor invoice
    line items), grouped by product or category. Match key = product name
    (case-insensitive); category is taken from the cost side."""
    # Cost side: per-product goods cost, qty, category
    with get_conn() as conn:
        cost_rows = conn.execute("""
            SELECT LOWER(TRIM(product_name)) AS pkey, product_name, sku, category,
                   SUM(quantity) AS qty, SUM(quantity*unit_cost) AS cogs
            FROM vendor_invoice_lines GROUP BY pkey
        """).fetchall()
        order_rows = conn.execute(
            "SELECT line_items FROM orders WHERE fulfillment_status='FULFILLED' AND line_items IS NOT NULL").fetchall()

    cost_by_product = {r["pkey"]: dict(r) for r in cost_rows}
    cat_of = {r["pkey"]: (r["category"] or "Uncategorized") for r in cost_rows}

    # Revenue side: parse order line items
    rev = {}   # pkey -> {revenue, units, name}
    for o in order_rows:
        try:
            items = json.loads(o["line_items"] or "[]")
        except Exception:
            continue
        for it in items:
            name = (it.get("productName") or it.get("title") or "").strip()
            if not name:
                continue
            pkey = name.lower()
            qty = float(it.get("quantity") or 1)
            unit = it.get("unitPricePaid") or it.get("unit_price") or {}
            price = float(unit.get("value") if isinstance(unit, dict) else unit or 0)
            line_rev = price * qty
            e = rev.setdefault(pkey, {"revenue": 0.0, "units": 0.0, "name": name})
            e["revenue"] += line_rev
            e["units"]   += qty

    # Merge product universe
    products = set(rev) | set(cost_by_product)
    prod_rows = []
    for pkey in products:
        r = rev.get(pkey, {})
        c = cost_by_product.get(pkey, {})
        revenue = round(r.get("revenue", 0.0), 2)
        cogs    = round(c.get("cogs", 0.0) or 0.0, 2)
        name    = r.get("name") or (c.get("product_name") if c else pkey)
        category= cat_of.get(pkey, "Uncategorized")
        margin  = round(revenue - cogs, 2)
        prod_rows.append({
            "product": name, "sku": c.get("sku"), "category": category,
            "units_sold": round(r.get("units", 0.0), 2),
            "units_purchased": round(c.get("qty", 0.0) or 0.0, 2),
            "revenue": revenue, "cogs": cogs, "margin": margin,
            "margin_pct": round(margin / revenue * 100, 1) if revenue else None,
            "has_cost": pkey in cost_by_product,
        })
    prod_rows.sort(key=lambda x: x["revenue"], reverse=True)

    if group_by == "product":
        return prod_rows

    # Aggregate by category
    cats = {}
    for p in prod_rows:
        c = cats.setdefault(p["category"], {"category": p["category"], "revenue": 0.0,
                                            "cogs": 0.0, "products": 0})
        c["revenue"] += p["revenue"]; c["cogs"] += p["cogs"]; c["products"] += 1
    out = []
    for c in cats.values():
        c["revenue"] = round(c["revenue"], 2); c["cogs"] = round(c["cogs"], 2)
        c["margin"] = round(c["revenue"] - c["cogs"], 2)
        c["margin_pct"] = round(c["margin"] / c["revenue"] * 100, 1) if c["revenue"] else None
        out.append(c)
    out.sort(key=lambda x: x["revenue"], reverse=True)
    return out


# ── Reporting ─────────────────────────────────────────────────────────────────

def get_summary(period_start=None, period_end=None):
    where, params = "", []
    if period_start:
        where += " AND entry_date >= ?"; params.append(period_start)
    if period_end:
        where += " AND entry_date <= ?"; params.append(period_end)
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT account,
                   COALESCE(SUM(credit),0) as total_credit,
                   COALESCE(SUM(debit),0)  as total_debit
            FROM ledger WHERE 1=1 {where} GROUP BY account
        """, params).fetchall()
    return {r["account"]: {"credit": float(r["total_credit"]),
                           "debit":  float(r["total_debit"])} for r in rows}

def get_monthly_pl():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT substr(entry_date,1,7) as month, account,
                   COALESCE(SUM(credit),0) as cr, COALESCE(SUM(debit),0) as dr
            FROM ledger GROUP BY month, account ORDER BY month
        """).fetchall()
        assumptions = {r["key"]: float(r["value"]) for r in
                       conn.execute("SELECT key, value FROM assumptions").fetchall()}
    months = {}
    for r in rows:
        m = r["month"]
        if m not in months:
            months[m] = {}
        months[m][r["account"]] = {"cr": float(r["cr"]), "dr": float(r["dr"])}

    opex_pct = assumptions.get("opex_pct", 10.0) / 100
    result = []
    for m, accts in sorted(months.items()):
        rev      = accts.get("REVENUE", {}).get("cr", 0)
        ship_cr  = accts.get("SHIPPING", {}).get("cr", 0)
        disc     = accts.get("DISCOUNT", {}).get("dr", 0)
        tax      = accts.get("TAX", {}).get("cr", 0)
        mfee     = accts.get("MERCHANT_FEE", {}).get("dr", 0)
        cogs     = accts.get("COGS", {}).get("dr", 0)
        expense  = accts.get("EXPENSE", {}).get("dr", 0)
        gross    = rev + ship_cr
        opex_est = round(rev * opex_pct, 2)
        net      = round(gross - disc - tax - mfee - cogs - expense - opex_est, 2)
        result.append({
            "month": m, "revenue": round(rev,2), "shipping_cr": round(ship_cr,2),
            "gross": round(gross,2), "discount": round(disc,2), "tax": round(tax,2),
            "merchant_fee": round(mfee,2), "cogs": round(cogs,2),
            "expense": round(expense,2), "opex_est": opex_est, "net": net,
        })
    return result

def get_assumptions():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM assumptions ORDER BY key").fetchall()
    return [dict(r) for r in rows]

def get_vendors():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT v.*, COALESCE(SUM(vi.amount),0) as total_invoiced,
                   COALESCE(SUM(vi.paid_amount),0) as total_paid
            FROM vendors v LEFT JOIN vendor_invoices vi ON vi.vendor_id=v.id
            GROUP BY v.id ORDER BY v.name
        """).fetchall()
    return [dict(r) for r in rows]

def get_vendor_invoices(status=None):
    where = "WHERE vi.status=?" if status else ""
    params = [status] if status else []
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT vi.*, v.name as vendor_name
            FROM vendor_invoices vi LEFT JOIN vendors v ON v.id=vi.vendor_id
            {where} ORDER BY vi.invoice_date DESC
        """, params).fetchall()
    return [dict(r) for r in rows]

def get_checks(limit=100):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.*, v.name as vendor_name
            FROM checks c LEFT JOIN vendors v ON v.id=c.vendor_id
            ORDER BY c.check_date DESC LIMIT ?
        """, [limit]).fetchall()
    return [dict(r) for r in rows]

def get_unfiled_tax_periods():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tax_log WHERE filed_on IS NULL ORDER BY period_start"
        ).fetchall()]

def seed_ny_tax_reminders():
    year = datetime.date.today().year
    quarters = [
        (f"{year}-01-01", f"{year}-03-20", "Q1 NY Sales Tax Filing (ST-100)",
         f"File NY ST-100 for {year} Q1 — covers Jan–Mar. Due Mar 20."),
        (f"{year}-04-01", f"{year}-06-20", "Q2 NY Sales Tax Filing (ST-100)",
         f"File NY ST-100 for {year} Q2 — covers Apr–Jun. Due Jun 20."),
        (f"{year}-07-01", f"{year}-09-20", "Q3 NY Sales Tax Filing (ST-100)",
         f"File NY ST-100 for {year} Q3 — covers Jul–Sep. Due Sep 20."),
        (f"{year+1}-01-01", f"{year+1}-03-20", "Annual NY Sales Tax Filing (ST-101)",
         f"File NY ST-101 annual return for {year}. Due Mar 20 {year+1}."),
    ]
    with get_conn() as conn:
        for due_date, _, title, desc in quarters:
            conn.execute("""
                INSERT OR IGNORE INTO reminders (due_date, category, title, description)
                SELECT ?,?,?,? WHERE NOT EXISTS
                  (SELECT 1 FROM reminders WHERE due_date=? AND title=?)
            """, (due_date, "TAX", title, desc, due_date, title))
    print(f"✓ NY tax reminders seeded for {year}")

if __name__ == "__main__":
    # Demo DB is read-only; init/seed/repost are intentionally not run here.
    print(f"Demo DB: {ensure_seeded()}")
