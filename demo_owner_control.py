#!/usr/bin/env python3
"""
owner_control.py — God Mode (Owner Control Center) + Tax Filing Packages.

Control points: cross-checks between orders, invoices, ledger, checks, bank
transactions, sales-tax log and the federal tax-forms engine. Each control
point returns PASS / WARN / FAIL with drill-down items so the owner can see
at a glance whether the books are internally consistent.

Filing packages: periodic (quarterly) and annual submission bundles — cover
sheet, ST-100 worksheet, P&L, ledger/orders/checks/bank CSVs, federal form
PDFs and a SHA-256 manifest — zipped for the accountant / audit file.

All figures are management/bookkeeping copies, not official filings.
"""

import csv, datetime, hashlib, io, json, pathlib, re, zipfile

from demo_db import get_conn, DATA_DIR

# DEMO: paths follow PRIME_ERP_DATA_DIR (see demo_db). No package ZIPs are shipped.
BASE_DIR = DATA_DIR
PACKAGES_DIR = BASE_DIR / "tax_forms" / "packages"
AUTH_LOG = BASE_DIR / "erp_auth_audit.log"

DISCLAIMER = ("Prepared by Prime Industrial ERP for bookkeeping and return preparation. "
              "Figures are management copies — verify against official IRS / NYS DTF "
              "instructions for the filing year. Not a substitute for a licensed preparer.")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _today():
    return datetime.date.today()


# ═══════════════════════════════════════════════════════════════════════════════
# CONTROL POINTS
# ═══════════════════════════════════════════════════════════════════════════════

def _cp(cp_id, title, status, summary, metrics=None, items=None, action=None):
    return {"id": cp_id, "title": title, "status": status, "summary": summary,
            "metrics": metrics or [], "items": items or [], "action": action}


def cp_ledger_integrity(c):
    """CP-1: double-entry ledger is internally consistent with orders."""
    fulfilled = c.execute("SELECT COUNT(*) FROM orders WHERE fulfillment_status='FULFILLED'").fetchone()[0]
    posted = c.execute("SELECT COUNT(DISTINCT order_id) FROM ledger WHERE account='REVENUE' AND order_id IS NOT NULL").fetchone()[0]
    dupes = c.execute("""SELECT order_id, COUNT(*) n FROM ledger
                         WHERE account='REVENUE' AND order_id IS NOT NULL
                         GROUP BY order_id HAVING n>1""").fetchall()
    orphan_vi = c.execute("""SELECT DISTINCT reference FROM ledger
        WHERE reference LIKE 'VI:%' AND CAST(substr(reference,4) AS INTEGER)
          NOT IN (SELECT id FROM vendor_invoices)""").fetchall()
    orphan_bank = c.execute("""SELECT DISTINCT reference FROM ledger
        WHERE reference LIKE 'BANK:%' AND CAST(substr(reference,6) AS INTEGER)
          NOT IN (SELECT id FROM bank_transactions WHERE reconciled=1 AND ignored=0)""").fetchall()
    sums = {r["account"]: {"dr": round(r["dr"], 2), "cr": round(r["cr"], 2)} for r in c.execute(
        "SELECT account, COALESCE(SUM(debit),0) dr, COALESCE(SUM(credit),0) cr FROM ledger GROUP BY account")}

    items = []
    if fulfilled != posted:
        missing = c.execute("""SELECT order_number, customer_name, grand_total FROM orders
            WHERE fulfillment_status='FULFILLED' AND id NOT IN
              (SELECT order_id FROM ledger WHERE account='REVENUE' AND order_id IS NOT NULL) LIMIT 20""").fetchall()
        items += [{"issue": "Fulfilled order not posted to ledger",
                   "detail": f"#{r['order_number']} — {r['customer_name']} (${r['grand_total']:,.2f})"} for r in missing]
    items += [{"issue": "Duplicate REVENUE posting", "detail": f"order {r['order_id']} posted {r['n']}×"} for r in dupes]
    items += [{"issue": "Orphan vendor-invoice ledger ref", "detail": r["reference"]} for r in orphan_vi]
    items += [{"issue": "Orphan / stale bank ledger ref", "detail": r["reference"]} for r in orphan_bank]

    status = "FAIL" if (dupes or orphan_vi or fulfilled > posted) else ("WARN" if orphan_bank else "PASS")
    return _cp("ledger", "Ledger Integrity", status,
               f"{posted}/{fulfilled} fulfilled orders posted · {len(dupes)} duplicates · "
               f"{len(orphan_vi) + len(orphan_bank)} orphan references",
               metrics=[{"label": a, "value": f"dr {v['dr']:,.2f} / cr {v['cr']:,.2f}"} for a, v in sorted(sums.items())],
               items=items,
               action={"label": "Rebuild ledger", "endpoint": "/api/godmode/rebuild"} if status != "PASS" else None)


def cp_invoice_coverage(c):
    """CP-2: every fulfilled order has an emailed invoice, totals agree."""
    no_inv = c.execute("""SELECT o.order_number, o.customer_name, o.grand_total FROM orders o
        LEFT JOIN invoices i ON i.order_id=o.id
        WHERE o.fulfillment_status='FULFILLED' AND i.id IS NULL LIMIT 20""").fetchall()
    mismatch = c.execute("""SELECT i.invoice_number, i.grand_total ig, o.grand_total og FROM invoices i
        JOIN orders o ON o.id=i.order_id WHERE ABS(COALESCE(i.grand_total,0)-COALESCE(o.grand_total,0))>0.01 LIMIT 20""").fetchall()
    not_emailed = c.execute("SELECT COUNT(*) FROM invoices WHERE emailed_at IS NULL").fetchone()[0]
    total_inv = c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]

    items = [{"issue": "Fulfilled order without invoice",
              "detail": f"#{r['order_number']} — {r['customer_name']} (${r['grand_total']:,.2f})"} for r in no_inv]
    items += [{"issue": "Invoice/order total mismatch",
               "detail": f"{r['invoice_number']}: invoice ${r['ig']:,.2f} vs order ${r['og']:,.2f}"} for r in mismatch]
    status = "FAIL" if mismatch else ("WARN" if no_inv or not_emailed else "PASS")
    return _cp("invoices", "Invoice Coverage", status,
               f"{total_inv} invoices · {len(no_inv)} fulfilled orders missing invoice · "
               f"{len(mismatch)} total mismatches · {not_emailed} not emailed", items=items)


def cp_check_log(c):
    """CP-3: check log hygiene — outstanding, stale, duplicates, VI consistency."""
    today = _today().isoformat()
    outstanding = c.execute("""SELECT id, check_date, check_number, payee, amount,
        CAST(julianday(?)-julianday(check_date) AS INTEGER) age
        FROM checks WHERE voided=0 AND cleared_on IS NULL ORDER BY check_date""", [today]).fetchall()
    dupes = c.execute("""SELECT check_number, COUNT(*) n FROM checks
        WHERE voided=0 AND check_number IS NOT NULL AND check_number!=''
        GROUP BY check_number HAVING n>1""").fetchall()
    vi_gap = c.execute("""SELECT ch.check_number, ch.amount ca, vi.paid_amount pa, vi.invoice_number
        FROM checks ch JOIN vendor_invoices vi ON vi.id=ch.vendor_invoice_id
        WHERE ch.voided=0 AND ABS(ch.amount - vi.paid_amount) > 0.01 LIMIT 20""").fetchall()
    total_out = sum(float(r["amount"] or 0) for r in outstanding)
    stale = [r for r in outstanding if (r["age"] or 0) > 90]

    items = [{"issue": f"Outstanding {'· STALE >90d' if (r['age'] or 0) > 90 else ''}".strip(),
              "detail": f"Check #{r['check_number']} {r['check_date']} — {r['payee']} ${float(r['amount'] or 0):,.2f} ({r['age']}d)"}
             for r in outstanding]
    items += [{"issue": "Duplicate check number", "detail": f"#{r['check_number']} used {r['n']}×"} for r in dupes]
    items += [{"issue": "Check ≠ vendor invoice paid amount",
               "detail": f"Check #{r['check_number']} ${r['ca']:,.2f} vs VI {r['invoice_number']} paid ${r['pa']:,.2f}"} for r in vi_gap]
    status = "FAIL" if (dupes or vi_gap) else ("WARN" if stale else "PASS")
    return _cp("checks", "Check Log Control", status,
               f"{len(outstanding)} outstanding (${total_out:,.2f}) · {len(stale)} stale >90d · "
               f"{len(dupes)} duplicate numbers", items=items)


def cp_bank_recon(c):
    """CP-4: bank feed ↔ ledger reconciliation state."""
    row = c.execute("""SELECT COUNT(*) total,
        SUM(CASE WHEN reconciled=0 AND ignored=0 AND pending=0 THEN 1 ELSE 0 END) unrecon,
        SUM(CASE WHEN pending=1 THEN 1 ELSE 0 END) pending,
        SUM(CASE WHEN reconciled=1 AND ignored=0 AND erp_category IS NULL THEN 1 ELSE 0 END) recon_nocat
        FROM bank_transactions""").fetchone()
    connected = c.execute("SELECT COUNT(*) FROM plaid_items").fetchone()[0] > 0
    # Reconciled + posting category but missing its BANK: ledger entry
    unposted = c.execute("""SELECT id, date, name, amount, erp_category FROM bank_transactions
        WHERE reconciled=1 AND ignored=0 AND erp_category IN ('EXPENSE','COGS','FEE','TAX')
          AND ('BANK:' || id) NOT IN (SELECT DISTINCT reference FROM ledger WHERE reference LIKE 'BANK:%')
        LIMIT 20""").fetchall()

    items = [{"issue": "Reconciled but not posted to ledger",
              "detail": f"{r['date']} {r['name']} ${abs(float(r['amount'] or 0)):,.2f} [{r['erp_category']}]"} for r in unposted]
    if not connected:
        items.append({"issue": "No bank connected", "detail": "Connect the business bank via Plaid on the Bank tab"})
    total, unrecon = row["total"] or 0, row["unrecon"] or 0
    status = ("WARN" if not connected or unrecon or unposted or (row["recon_nocat"] or 0) else "PASS")
    return _cp("bank", "Bank Reconciliation", status,
               (f"{total} transactions · {unrecon} unreconciled · {row['pending'] or 0} pending · "
                f"{len(unposted)} reconciled-but-unposted") if connected else "Bank feed not connected (Plaid)",
               items=items,
               action={"label": "Repost bank → ledger", "endpoint": "/api/bank/repost-ledger"} if unposted else None)


def cp_check_bank_match(c):
    """CP-5: checks ↔ bank debits cross-match (the checks-vs-bank control)."""
    suggestions = suggest_check_matches(c)
    cleared_unlinked = c.execute("""SELECT COUNT(*) FROM checks
        WHERE voided=0 AND cleared_on IS NOT NULL AND (bank_txn_id IS NULL OR bank_txn_id=0)""").fetchone()[0]
    linked = c.execute("SELECT COUNT(*) FROM checks WHERE voided=0 AND bank_txn_id IS NOT NULL AND bank_txn_id!=0").fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM checks WHERE voided=0").fetchone()[0]

    items = [{"issue": "Suggested match",
              "detail": f"Check #{s['check_number']} ${s['check_amount']:,.2f} ↔ bank {s['txn_date']} "
                        f"{s['txn_name']} ${s['txn_amount']:,.2f}",
              "check_id": s["check_id"], "bank_txn_id": s["txn_id"]} for s in suggestions]
    status = "WARN" if (suggestions or cleared_unlinked) else "PASS"
    if total == 0:
        status = "PASS"
    return _cp("checkmatch", "Checks ↔ Bank Matching", status,
               f"{linked}/{total} checks linked to bank · {len(suggestions)} suggested matches · "
               f"{cleared_unlinked} cleared without bank link", items=items)


def cp_sales_tax(c):
    """CP-6: NY sales tax collected vs filed, overdue quarters."""
    today = _today()
    year = today.year
    quarters = [
        (f"Q1 {year}", f"{year}-01-01", f"{year}-03-31", f"{year}-03-20"),
        (f"Q2 {year}", f"{year}-04-01", f"{year}-06-30", f"{year}-06-20"),
        (f"Q3 {year}", f"{year}-07-01", f"{year}-09-30", f"{year}-09-20"),
        (f"Q4 {year}", f"{year}-10-01", f"{year}-12-31", f"{year+1}-03-20"),
    ]
    items, overdue, held = [], 0, 0.0
    for label, s, e, due in quarters:
        tax = float(c.execute("SELECT COALESCE(SUM(credit),0) FROM ledger WHERE account='TAX' AND entry_date BETWEEN ? AND ?", [s, e]).fetchone()[0])
        f = c.execute("SELECT filed_on, payment_amount FROM tax_log WHERE period_start=? AND period_end=?", [s, e]).fetchone()
        filed = f and f["filed_on"]
        if filed and abs(float(f["payment_amount"] or 0) - tax) > 0.01 and tax > 0:
            items.append({"issue": "Filed amount ≠ collected", "detail":
                          f"{label}: filed ${float(f['payment_amount'] or 0):,.2f} vs collected ${tax:,.2f}"})
        if not filed:
            held += tax
            if today.isoformat() > due:
                overdue += 1
                items.append({"issue": "OVERDUE filing", "detail": f"{label} (ST-100 due {due}) — ${tax:,.2f} collected"})
    status = "FAIL" if overdue else ("WARN" if any(i["issue"].startswith("Filed") for i in items) else "PASS")
    return _cp("salestax", "Sales Tax Control (NY ST-100)", status,
               f"${held:,.2f} collected & unremitted · {overdue} overdue quarters", items=items)


def cp_federal_forms(c):
    """CP-7: federal tax forms engine readiness for the filing year."""
    import tax_forms_config as CFG
    year = CFG.TAX_YEAR
    payer = c.execute("SELECT * FROM tax_payer ORDER BY id DESC LIMIT 1").fetchone()
    payees = c.execute("SELECT * FROM tax_payees").fetchall()
    gen = c.execute("SELECT COUNT(*) FROM tax_generated_forms WHERE tax_year=?", [year]).fetchone()[0]
    no_w9 = [p for p in payees if p["kind"] == "CONTRACTOR" and not p["w9_on_file"]]
    example_names = {"jane worker", "bob vendor", "tiny gig"}
    examples = [p for p in payees if (p["legal_name"] or "").strip().lower() in example_names]

    items = []
    if not (payer and payer["ein"]):
        items.append({"issue": "Payer not configured", "detail": "Set legal name + EIN on Federal Tax Forms tab"})
    items += [{"issue": "Contractor missing W-9 → 24% backup withholding",
               "detail": p["legal_name"]} for p in no_w9]
    items += [{"issue": "EXAMPLE payee still in system — delete before real filing",
               "detail": p["legal_name"]} for p in examples]
    status = "FAIL" if not (payer and payer["ein"]) else ("WARN" if no_w9 or examples else "PASS")
    return _cp("fedforms", f"Federal Forms Readiness ({year})", status,
               f"{len(payees)} payees · {gen} forms generated · {len(no_w9)} missing W-9 · "
               f"{len(examples)} example records", items=items)


def cp_security(c):
    """CP-8: access-gate audit trail + operational freshness."""
    events, failed7 = [], 0
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    if AUTH_LOG.exists():
        lines = AUTH_LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        for ln in lines[-25:]:
            try:
                events.append(json.loads(ln))
            except Exception:
                pass
        for ln in lines:
            try:
                ev = json.loads(ln)
                if ev.get("event") in ("login_failed", "login_rate_limited") and ev.get("ts", "") >= cutoff:
                    failed7 += 1
            except Exception:
                pass
    last_import = c.execute("SELECT MAX(imported_at) FROM orders").fetchone()[0]
    last_invoice = c.execute("SELECT MAX(created_at) FROM invoices").fetchone()[0]
    stale_sync = False
    if last_import:
        try:
            dt = datetime.datetime.fromisoformat(str(last_import).replace("Z", ""))
            stale_sync = (datetime.datetime.utcnow() - dt) > datetime.timedelta(hours=12)
        except Exception:
            pass

    items = [{"issue": e.get("event", "?"), "detail": f"{e.get('ts','')} · {e.get('ip','')} · {e.get('path','')}"}
             for e in reversed(events)]
    status = "FAIL" if failed7 >= 10 else ("WARN" if failed7 > 0 or stale_sync else "PASS")
    return _cp("security", "Access & Audit Trail", status,
               f"{failed7} failed logins (7d) · last order sync {last_import or '—'} · "
               f"last invoice {last_invoice or '—'}" + (" · SYNC STALE >12h" if stale_sync else ""),
               items=items)


def run_control_points():
    with get_conn() as c:
        cps = [cp_ledger_integrity(c), cp_invoice_coverage(c), cp_check_log(c),
               cp_bank_recon(c), cp_check_bank_match(c), cp_sales_tax(c),
               cp_federal_forms(c), cp_security(c)]
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for cp in cps:
        counts[cp["status"]] += 1
    return {"generated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "counts": counts,
            "overall": "FAIL" if counts["FAIL"] else ("WARN" if counts["WARN"] else "PASS"),
            "control_points": sorted(cps, key=lambda x: order[x["status"]])}


# ── Check ↔ bank matching ─────────────────────────────────────────────────────

def suggest_check_matches(c=None):
    """Uncleared/unlinked checks vs bank debits: amount within $0.50, date within 30d."""
    own = c is None
    if own:
        conn = get_conn(); c = conn
    try:
        rows = c.execute("""
            SELECT ch.id check_id, ch.check_number, ch.check_date, ch.payee, ch.amount check_amount,
                   bt.id txn_id, bt.date txn_date, bt.name txn_name, bt.amount txn_amount
            FROM checks ch JOIN bank_transactions bt
              ON bt.amount > 0 AND ABS(bt.amount - ch.amount) <= 0.5
             AND ABS(julianday(bt.date) - julianday(ch.check_date)) <= 30
            WHERE ch.voided=0 AND (ch.bank_txn_id IS NULL OR ch.bank_txn_id=0)
              AND bt.ignored=0 AND bt.pending=0
              AND bt.id NOT IN (SELECT bank_txn_id FROM checks WHERE bank_txn_id IS NOT NULL)
            ORDER BY ABS(bt.amount - ch.amount), ABS(julianday(bt.date) - julianday(ch.check_date))
        """).fetchall()
        seen_checks, seen_txns, out = set(), set(), []
        for r in rows:
            if r["check_id"] in seen_checks or r["txn_id"] in seen_txns:
                continue
            seen_checks.add(r["check_id"]); seen_txns.add(r["txn_id"])
            d = dict(r)
            d["check_amount"] = float(d["check_amount"] or 0)
            d["txn_amount"] = float(d["txn_amount"] or 0)
            out.append(d)
        return out
    finally:
        if own:
            conn.close()


def match_check_to_bank(check_id, txn_id):
    """Link a check to a bank debit: mark check cleared, txn reconciled, post to ledger."""
    with get_conn() as c:
        ch = c.execute("SELECT * FROM checks WHERE id=?", [check_id]).fetchone()
        bt = c.execute("SELECT * FROM bank_transactions WHERE id=?", [txn_id]).fetchone()
        if not ch or not bt:
            return {"error": "check or transaction not found"}
        c.execute("UPDATE checks SET bank_txn_id=?, cleared_on=COALESCE(cleared_on, ?) WHERE id=?",
                  [txn_id, bt["date"], check_id])
        cat = bt["erp_category"] or ("COGS" if ch["category"] == "VENDOR" else
                                     "TAX" if ch["category"] == "TAX" else "EXPENSE")
        c.execute("""UPDATE bank_transactions SET reconciled=1, erp_category=?,
                     memo=COALESCE(NULLIF(memo,''), ?) WHERE id=?""",
                  [cat, f"Check #{ch['check_number']} — {ch['payee']}", txn_id])
    from demo_db import post_bank_txn_to_ledger
    post_bank_txn_to_ledger(txn_id)
    return {"ok": True, "check_id": check_id, "txn_id": txn_id, "category": cat}


def rebuild_all():
    """Full idempotent rebuild: orders → ledger, vendor invoices, bank postings."""
    from demo_db import repost_all_ledger, repost_all_vendor_invoices, repost_all_bank_ledger
    repost_all_ledger()
    n_vi = repost_all_vendor_invoices()
    n_bank = repost_all_bank_ledger()
    with get_conn() as c:
        n_ledger = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    return {"ok": True, "vendor_invoices_reposted": n_vi,
            "bank_txns_reposted": n_bank, "ledger_entries": n_ledger}


# ═══════════════════════════════════════════════════════════════════════════════
# TAX FILING CALENDAR & PACKAGES
# ═══════════════════════════════════════════════════════════════════════════════

def _quarter_ranges(year):
    return {
        "Q1": (f"{year}-01-01", f"{year}-03-31"),
        "Q2": (f"{year}-04-01", f"{year}-06-30"),
        "Q3": (f"{year}-07-01", f"{year}-09-30"),
        "Q4": (f"{year}-10-01", f"{year}-12-31"),
        "ANNUAL": (f"{year}-01-01", f"{year}-12-31"),
    }


def filing_calendar(year=None):
    """Everything the company submits periodically + annually, with live status."""
    today = _today()
    year = int(year or today.year)
    qr = _quarter_ranges(year)
    with get_conn() as c:
        has_employees = c.execute("SELECT COUNT(*) FROM tax_payees WHERE kind='EMPLOYEE'").fetchone()[0] > 0
        has_contractors = c.execute("SELECT COUNT(*) FROM tax_payees WHERE kind='CONTRACTOR'").fetchone()[0] > 0
        w2_gen = c.execute("SELECT COUNT(*) FROM tax_generated_forms WHERE tax_year=? AND form_type='W-2'", [year]).fetchone()[0]
        n1099_gen = c.execute("SELECT COUNT(*) FROM tax_generated_forms WHERE tax_year=? AND form_type LIKE '1099%'", [year]).fetchone()[0]

        def tax_in(s, e):
            return float(c.execute("SELECT COALESCE(SUM(credit),0) FROM ledger WHERE account='TAX' AND entry_date BETWEEN ? AND ?", [s, e]).fetchone()[0])

        def rev_in(s, e):
            return float(c.execute("SELECT COALESCE(SUM(credit),0) FROM ledger WHERE account='REVENUE' AND entry_date BETWEEN ? AND ?", [s, e]).fetchone()[0])

        def filed(s, e):
            r = c.execute("SELECT filed_on FROM tax_log WHERE period_start=? AND period_end=?", [s, e]).fetchone()
            return r["filed_on"] if r else None

        rows = []

        def add(form, agency, freq, period, due, amount=None, status=None, note=""):
            if status is None:
                status = "OVERDUE" if today.isoformat() > due else "UPCOMING"
            rows.append({"form": form, "agency": agency, "frequency": freq, "period": period,
                         "due": due, "amount": amount, "status": status, "note": note})

        # NY sales tax — quarterly ST-100 (app convention: Mar/Jun/Sep 20, Q4 → Mar 20 next yr)
        st_due = {"Q1": f"{year}-03-20", "Q2": f"{year}-06-20", "Q3": f"{year}-09-20", "Q4": f"{year+1}-03-20"}
        for q in ("Q1", "Q2", "Q3", "Q4"):
            s, e = qr[q]
            f = filed(s, e)
            t = tax_in(s, e)
            add("ST-100 Sales Tax Return", "NYS DTF", "Quarterly", f"{q} {year}", st_due[q], t,
                "FILED" if f else None, f"Filed {f}" if f else "8.875% NY combined rate")
        add("ST-101 Annual Sales Tax Return", "NYS DTF", "Annual", str(year), f"{year+1}-03-20",
            tax_in(*qr["ANNUAL"]),
            "FILED" if filed(*qr["ANNUAL"]) else None,
            "Annual summary return (if on annual basis) — verify filing basis with DTF")

        # Federal payroll — only when employees exist
        p941_due = {"Q1": f"{year}-04-30", "Q2": f"{year}-07-31", "Q3": f"{year}-10-31", "Q4": f"{year+1}-01-31"}
        for q in ("Q1", "Q2", "Q3", "Q4"):
            add("Form 941 Employer Quarterly", "IRS", "Quarterly", f"{q} {year}", p941_due[q], None,
                None if has_employees else "N/A",
                "Payroll withholding + FICA" if has_employees else "No employees on file")
        add("Form 940 (FUTA)", "IRS", "Annual", str(year), f"{year+1}-01-31", None,
            None if has_employees else "N/A",
            "Federal unemployment" if has_employees else "No employees on file")
        for q in ("Q1", "Q2", "Q3", "Q4"):
            add("NYS-45 Withholding/UI", "NYS DTF", "Quarterly", f"{q} {year}", p941_due[q], None,
                None if has_employees else "N/A",
                "NY payroll withholding + unemployment insurance" if has_employees else "No employees on file")

        # Information returns
        add("W-2 / W-3 to SSA", "SSA", "Annual", str(year), f"{year+1}-01-31", None,
            ("GENERATED" if w2_gen else None) if has_employees else "N/A",
            f"{w2_gen} W-2 generated (EFW2 e-file ready)" if w2_gen else
            ("Generate on Federal Tax Forms tab" if has_employees else "No employees on file"))
        add("1099-NEC + 1096", "IRS", "Annual", str(year), f"{year+1}-01-31", None,
            ("GENERATED" if n1099_gen else None) if has_contractors else "N/A",
            f"{n1099_gen} × 1099 generated (Pub-1220/IRIS ready)" if n1099_gen else
            ("Generate on Federal Tax Forms tab" if has_contractors else "No contractors on file"))

        # Income return + estimates
        add("Business Income Tax Return", "IRS", "Annual", str(year), f"{year+1}-04-15",
            rev_in(*qr["ANNUAL"]), None,
            "1120 / 1120-S (due Mar 15) / 1065 / Sch C — VERIFY entity type; extensions available")
        add("NY Corporate Franchise (CT-3) / IT-204", "NYS DTF", "Annual", str(year), f"{year+1}-04-15",
            None, None, "Matches federal entity type — verify with preparer")
        est_due = [f"{year}-04-15", f"{year}-06-15", f"{year}-09-15", f"{year+1}-01-15"]
        for i, d in enumerate(est_due, 1):
            add("Estimated Income Tax Payment", "IRS", "Quarterly", f"{year} #{i}", d, None, None,
                "1040-ES / 1120-W safe-harbor payment")

    order = {"OVERDUE": 0, "UPCOMING": 1, "GENERATED": 2, "FILED": 3, "N/A": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 1), r["due"]))
    return {"year": year, "rows": rows,
            "overdue": sum(1 for r in rows if r["status"] == "OVERDUE"),
            "upcoming_30d": sum(1 for r in rows if r["status"] == "UPCOMING"
                                and r["due"] <= (today + datetime.timedelta(days=30)).isoformat())}


# ── Package builder ───────────────────────────────────────────────────────────

def _csv_str(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def build_filing_package(year=None, period="ANNUAL"):
    """Assemble the submission bundle for a quarter or the full year."""
    year = int(year or _today().year)
    period = (period or "ANNUAL").upper()
    qr = _quarter_ranges(year)
    if period not in qr:
        return {"error": f"period must be one of {list(qr)}"}
    start, end = qr[period]
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)

    with get_conn() as c:
        payer = c.execute("SELECT * FROM tax_payer ORDER BY id DESC LIMIT 1").fetchone()
        company = (payer["legal_name"] if payer else None) or "Prime Industrial"
        ein = (payer["ein"] if payer else "") or "—"

        def s(account, col, s_=start, e_=end):
            return float(c.execute(
                f"SELECT COALESCE(SUM({col}),0) FROM ledger WHERE account=? AND entry_date BETWEEN ? AND ?",
                [account, s_, e_]).fetchone()[0])

        revenue, ship = s("REVENUE", "credit"), s("SHIPPING", "credit")
        disc, tax = s("DISCOUNT", "debit"), s("TAX", "credit")
        mfee, cogs, exp = s("MERCHANT_FEE", "debit"), s("COGS", "debit"), s("EXPENSE", "debit")
        gross = round(revenue + ship, 2)
        net = round(gross - disc - mfee - cogs - exp, 2)

        ledger_rows = c.execute("""SELECT entry_date, account, description, debit, credit, reference
            FROM ledger WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date, id""", [start, end]).fetchall()
        order_rows = c.execute("""SELECT created_on, order_number, customer_name, customer_email, subtotal,
            shipping_collected, discount_total, tax, grand_total, fulfillment_status
            FROM orders WHERE created_on BETWEEN ? AND ? ORDER BY created_on""", [start, end]).fetchall()
        check_rows = c.execute("""SELECT check_date, check_number, payee, amount, memo, category,
            cleared_on, voided FROM checks WHERE check_date BETWEEN ? AND ? ORDER BY check_date""", [start, end]).fetchall()
        bank_rows = c.execute("""SELECT date, name, merchant_name, amount, erp_category, memo, reconciled
            FROM bank_transactions WHERE date BETWEEN ? AND ? AND ignored=0 ORDER BY date""", [start, end]).fetchall()
        ap_rows = c.execute("""SELECT vi.invoice_date, v.name, vi.invoice_number, vi.amount, vi.paid_amount,
            vi.status FROM vendor_invoices vi LEFT JOIN vendors v ON v.id=vi.vendor_id
            WHERE vi.status!='PAID' ORDER BY vi.invoice_date""").fetchall()

        # ST-100 worksheet — per quarter of the year (always include all 4 for context)
        st_rows = []
        for q in ("Q1", "Q2", "Q3", "Q4"):
            qs, qe = qr[q]
            qrev, qtax = s("REVENUE", "credit", qs, qe), s("TAX", "credit", qs, qe)
            f = c.execute("SELECT filed_on, payment_amount, payment_ref FROM tax_log WHERE period_start=? AND period_end=?", [qs, qe]).fetchone()
            st_rows.append([f"{q} {year}", qs, qe, round(qrev + s("SHIPPING", "credit", qs, qe), 2),
                            round(qrev, 2), round(qtax, 2), "8.875%",
                            (f["filed_on"] if f else ""), (f["payment_amount"] if f else ""),
                            (f["payment_ref"] if f else "")])

        # Monthly P&L within period
        pl_rows = []
        months = c.execute("""SELECT substr(entry_date,1,7) m, account,
            COALESCE(SUM(credit),0) cr, COALESCE(SUM(debit),0) dr
            FROM ledger WHERE entry_date BETWEEN ? AND ? GROUP BY m, account ORDER BY m""", [start, end]).fetchall()
        agg = {}
        for r in months:
            agg.setdefault(r["m"], {})[r["account"]] = (float(r["cr"]), float(r["dr"]))
        for m, a in sorted(agg.items()):
            g = a.get("REVENUE", (0, 0))[0] + a.get("SHIPPING", (0, 0))[0]
            n = round(g - a.get("DISCOUNT", (0, 0))[1] - a.get("MERCHANT_FEE", (0, 0))[1]
                      - a.get("COGS", (0, 0))[1] - a.get("EXPENSE", (0, 0))[1], 2)
            pl_rows.append([m, round(a.get("REVENUE", (0, 0))[0], 2), round(a.get("SHIPPING", (0, 0))[0], 2),
                            round(a.get("DISCOUNT", (0, 0))[1], 2), round(a.get("MERCHANT_FEE", (0, 0))[1], 2),
                            round(a.get("TAX", (0, 0))[0], 2), round(a.get("COGS", (0, 0))[1], 2),
                            round(a.get("EXPENSE", (0, 0))[1], 2), n])

        fed_forms = c.execute("""SELECT g.form_type, g.pdf_path, g.zip_path, g.checksum_pdf, p.legal_name
            FROM tax_generated_forms g LEFT JOIN tax_payees p ON p.id=g.payee_id
            WHERE g.tax_year=?""", [year]).fetchall()

    generated_at = datetime.datetime.now().isoformat(timespec="seconds")
    figures = {"period": f"{period} {year}", "start": start, "end": end,
               "gross_revenue": gross, "product_revenue": round(revenue, 2),
               "shipping_collected": round(ship, 2), "discounts_absorbed": round(disc, 2),
               "merchant_fees": round(mfee, 2), "sales_tax_collected": round(tax, 2),
               "cogs": round(cogs, 2), "expenses": round(exp, 2), "net_income_est": net}

    artifacts = {}  # name -> bytes
    artifacts["sales_tax_ST100_worksheet.csv"] = _csv_str(
        ["Period", "Start", "End", "GrossSales", "TaxableSales", "TaxCollected", "Rate", "FiledOn", "PaymentAmount", "PaymentRef"],
        st_rows).encode()
    artifacts["profit_and_loss_monthly.csv"] = _csv_str(
        ["Month", "Revenue", "Shipping", "Discounts", "MerchantFees", "TaxCollected", "COGS", "Expenses", "NetEst"],
        pl_rows).encode()
    artifacts["general_ledger.csv"] = _csv_str(
        ["Date", "Account", "Description", "Debit", "Credit", "Reference"],
        [[r["entry_date"], r["account"], r["description"], r["debit"], r["credit"], r["reference"]] for r in ledger_rows]).encode()
    artifacts["orders.csv"] = _csv_str(
        ["Date", "Order#", "Customer", "Email", "Subtotal", "ShippingCollected", "Discount", "Tax", "GrandTotal", "Status"],
        [[r["created_on"], r["order_number"], r["customer_name"], r["customer_email"], r["subtotal"],
          r["shipping_collected"], r["discount_total"], r["tax"], r["grand_total"], r["fulfillment_status"]] for r in order_rows]).encode()
    artifacts["checks_register.csv"] = _csv_str(
        ["Date", "Check#", "Payee", "Amount", "Memo", "Category", "ClearedOn", "Voided"],
        [[r["check_date"], r["check_number"], r["payee"], r["amount"], r["memo"], r["category"],
          r["cleared_on"], r["voided"]] for r in check_rows]).encode()
    artifacts["bank_transactions.csv"] = _csv_str(
        ["Date", "Name", "Merchant", "Amount", "ERPCategory", "Memo", "Reconciled"],
        [[r["date"], r["name"], r["merchant_name"], r["amount"], r["erp_category"], r["memo"], r["reconciled"]] for r in bank_rows]).encode()
    artifacts["accounts_payable_open.csv"] = _csv_str(
        ["InvoiceDate", "Vendor", "Invoice#", "Amount", "Paid", "Status"],
        [[r["invoice_date"], r["name"], r["invoice_number"], r["amount"], r["paid_amount"], r["status"]] for r in ap_rows]).encode()

    # Federal form PDFs (annual packages only — they're year-level documents)
    fed_included = []
    if period == "ANNUAL":
        for f in fed_forms:
            p = pathlib.Path(f["pdf_path"] or "")
            if p.exists():
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", p.name)
                artifacts[f"federal_forms/{safe}"] = p.read_bytes()
                fed_included.append({"form": f["form_type"], "recipient": f["legal_name"], "file": f"federal_forms/{safe}"})

    fig_rows = "".join(
        f"<tr><td>{k.replace('_', ' ').title()}</td><td style='text-align:right'>${v:,.2f}</td></tr>"
        for k, v in figures.items() if isinstance(v, (int, float)))
    fed_rows = "".join(f"<li>{f['form']} — {f['recipient']} ({f['file']})</li>" for f in fed_included) or "<li>None included</li>"
    artifacts["cover_sheet.html"] = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Filing Package — {company} — {period} {year}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:760px;margin:40px auto;line-height:1.55;color:#1a1d27}}
h1{{font-size:22px;border-bottom:3px solid #1a1d27;padding-bottom:10px}}h2{{font-size:15px;color:#0b6b8a;margin-top:26px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #d8dbe4;padding:6px 10px;font-size:13px}}
.warn{{background:#fdf1f0;border-left:4px solid #b23a2e;padding:10px 14px;font-size:12.5px;margin:18px 0}}</style></head><body>
<h1>Tax Filing Package — {period} {year}</h1>
<p><b>{company}</b> · EIN {ein} · Period {start} → {end} · Generated {generated_at}</p>
<div class="warn"><b>Working copies.</b> {DISCLAIMER}</div>
<h2>Key Figures (general ledger)</h2><table>{fig_rows}</table>
<h2>What to file for this period</h2>
<ul>
<li><b>NY ST-100</b> quarterly sales tax — worksheet: sales_tax_ST100_worksheet.csv</li>
<li><b>Federal information returns</b> (annual): W-2/W-3 to SSA, 1099-NEC/1096 to IRS — due Jan 31 {year + 1}</li>
<li><b>Business income tax return</b> — due Mar 15 / Apr 15 {year + 1} depending on entity type</li>
<li><b>Payroll returns</b> (941 / 940 / NYS-45) — only if employees on payroll</li>
</ul>
<h2>Federal form PDFs included</h2><ul>{fed_rows}</ul>
<h2>Contents</h2><ul>{"".join(f"<li>{n}</li>" for n in sorted(artifacts) if n != "cover_sheet.html")}</ul>
</body></html>""".encode()

    manifest = {"package": f"{period} {year}", "company": company, "ein": ein,
                "generated_at": generated_at, "figures": figures,
                "federal_forms": fed_included, "disclaimer": DISCLAIMER,
                "files": {name: {"sha256": _sha256(data), "bytes": len(data)}
                          for name, data in artifacts.items()}}
    artifacts["manifest.json"] = json.dumps(manifest, indent=2).encode()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = PACKAGES_DIR / f"filing_package_{year}_{period}_{ts}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in artifacts.items():
            z.writestr(f"filing_package_{year}_{period}/{name}", data)
    zip_bytes = zip_path.read_bytes()

    with get_conn() as c:
        cur = c.execute("""INSERT INTO tax_packages (tax_year, period, zip_path, sha256, file_count, bytes, figures)
                           VALUES (?,?,?,?,?,?,?)""",
                        [year, period, str(zip_path), _sha256(zip_bytes), len(artifacts),
                         len(zip_bytes), json.dumps(figures)])
        pkg_id = cur.lastrowid
    return {"ok": True, "id": pkg_id, "zip_path": str(zip_path), "sha256": _sha256(zip_bytes),
            "file_count": len(artifacts), "bytes": len(zip_bytes), "figures": figures}


def list_packages():
    with get_conn() as c:
        rows = c.execute("SELECT * FROM tax_packages ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["figures"] = json.loads(d.get("figures") or "{}")
        except Exception:
            d["figures"] = {}
        d["exists"] = pathlib.Path(d["zip_path"] or "").exists()
        out.append(d)
    return out
