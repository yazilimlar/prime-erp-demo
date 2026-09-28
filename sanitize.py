#!/usr/bin/env python3
"""
sanitize.py — build the public-safe demo database for the Prime Industrial ERP showcase.

Reads the REAL database strictly read-only (sqlite URI mode=ro; the original file is
never written) and writes demo/prime_industrial_demo.db with:
  * the identical schema (tables + indexes copied verbatim from sqlite_master),
  * identical row counts per table,
  * synthetic values for every identifying field (names, emails, addresses, phones,
    taxpayer IDs, account numbers, file paths, external IDs),
  * money amounts perturbed by a deterministic per-order factor, then re-derived so
    orders ↔ invoices ↔ ledger stay internally consistent (the control-point checks
    in owner_control still pass/fail the same way).

Every table must have an explicit handler below — an unknown table aborts the run,
so a future schema change can never leak through unsanitized.

Usage:
  python3 sanitize.py [--src PATH_TO_REAL_DB] [--out demo/prime_industrial_demo.db]
"""

import argparse, hashlib, json, os, pathlib, random, re, sqlite3, sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_SRC = HERE.parent / "AppSupport_PrimeIndustrial" / "prime_industrial.db"
DEFAULT_OUT = HERE / "prime_industrial_demo.db"

SEED = 20260928
FACTOR_RANGE = (0.55, 1.45)

FAKE_COMPANIES = [
    "Acme Corp", "Globex Industrial", "Initech Supply", "Umbrella Facilities",
    "Stark Fabrication", "Wayne Maintenance", "Hooli Plant Ops", "Vandelay Industries",
    "Soylent Works", "Tyrell Machining", "Cyberdyne Services", "Wonka Manufacturing",
]


def fake_hex(kind, value, n=24):
    """Stable, non-reversible replacement for an external ID (salted by kind)."""
    return hashlib.sha256(f"prime-demo:{kind}:{value}".encode()).hexdigest()[:n]


def money(v):
    return round(float(v or 0), 2)


class Sanitizer:
    def __init__(self, src, dst):
        self.src, self.dst = src, dst
        self.rng = random.Random(SEED)
        self.emails, self.names = {}, {}
        self.order_map, self.order_num_map, self.invoice_map = {}, {}, {}
        self.order_new = {}          # new order id -> sanitized order dict
        self.order_factor = {}
        self.global_factor = round(self.rng.uniform(*FACTOR_RANGE), 4)
        self.assumptions = {}

    # ── identity maps ────────────────────────────────────────────────────────
    def email(self, real):
        if real is None:
            return None
        key = real.strip().lower()
        if key not in self.emails:
            self.emails[key] = f"demo-{len(self.emails) + 1:04d}@example.com"
        return self.emails[key]

    def name(self, real):
        if real is None:
            return None
        key = real.strip().lower()
        if key not in self.names:
            i = len(self.names)
            self.names[key] = FAKE_COMPANIES[i % len(FAKE_COMPANIES)] + (f" {i // len(FAKE_COMPANIES) + 1}" if i >= len(FAKE_COMPANIES) else "")
        return self.names[key]

    def factor(self, order_id):
        if order_id not in self.order_factor:
            self.order_factor[order_id] = round(self.rng.uniform(*FACTOR_RANGE), 4)
        return self.order_factor[order_id]

    # ── run ─────────────────────────────────────────────────────────────────
    def run(self):
        if self.dst.exists():
            self.dst.unlink()
        s = sqlite3.connect(f"file:{self.src}?mode=ro", uri=True)
        s.row_factory = sqlite3.Row
        d = sqlite3.connect(self.dst)

        schema = s.execute("""SELECT type, name, sql FROM sqlite_master
                              WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
                              ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, rowid""").fetchall()
        for r in schema:
            d.execute(r["sql"])
        tables = [r["name"] for r in schema if r["type"] == "table"]

        unknown = [t for t in tables if not hasattr(self, f"t_{t}")]
        if unknown:
            sys.exit(f"ABORT: no sanitizer handler for tables {unknown} — add one before running.")

        self.assumptions = {r["key"]: float(r["value"]) for r in s.execute("SELECT key, value FROM assumptions")}

        # orders first (builds the id / invoice maps the other tables depend on)
        order_first = ["orders", "invoices"] + [t for t in tables if t not in ("orders", "invoices")]
        for t in order_first:
            cols = [c[1] for c in s.execute(f'PRAGMA table_info("{t}")')]
            rows = [dict(r) for r in s.execute(f'SELECT * FROM "{t}" ORDER BY rowid')]
            out = [getattr(self, f"t_{t}")(r) for r in rows]
            if t == "tax_packages":
                out = [self._package_figures(d, r) for r in out]
            d.executemany(f'INSERT INTO "{t}" ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                          [[r[c] for c in cols] for r in out])
            d.commit()

        # AUTOINCREMENT counters, so new ids line up with the original numbering
        seq = s.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
        d.execute("DELETE FROM sqlite_sequence")
        d.executemany("INSERT INTO sqlite_sequence (name, seq) VALUES (?,?)", [tuple(r) for r in seq])
        d.commit()
        d.execute("VACUUM")

        # row-count parity
        print(f"{'table':24} {'real':>6} {'demo':>6}")
        ok = True
        for t in tables + ["sqlite_sequence"]:
            a = s.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            b = d.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            ok &= a == b
            print(f"{t:24} {a:6} {b:6}{'' if a == b else '  <-- MISMATCH'}")
        leaks = self._leak_scan(s, d)
        s.close(); d.close()
        if not ok:
            sys.exit("ABORT: row counts differ")
        if leaks:
            self.dst.unlink()
            sys.exit(f"ABORT: {len(leaks)} real values found in demo DB (deleted it): {leaks[:5]}")
        print(f"\n✓ wrote {self.dst} — schema identical, row counts identical, leak scan clean")

    # ── per-table handlers ──────────────────────────────────────────────────
    def t_orders(self, r):
        old_id = r["id"]
        n = len(self.order_map) + 1
        new_id = f"demo-order-{n:04d}"
        self.order_map[old_id] = new_id
        new_num = str(1000 + n)
        self.order_num_map[str(r["order_number"])] = new_num
        f = self.factor(old_id)

        # line items: keep catalogue names/SKUs (product mix drives margins), scale prices,
        # drop customer customizations, scramble Squarespace ids, strip store image URLs.
        items = json.loads(r["line_items"] or "[]")
        sub = 0.0
        for it in items:
            for k in ("id", "variantId", "productId"):
                if it.get(k):
                    it[k] = fake_hex(k, it[k])
            up = it.get("unitPricePaid")
            if isinstance(up, dict) and up.get("value") is not None:
                up["value"] = f"{money(float(up['value']) * f):.2f}"
                sub += float(up["value"]) * float(it.get("quantity") or 1)
            it["customizations"] = None
            it["imageUrl"] = None
        subtotal = money(sub) if items else money(float(r["subtotal"] or 0) * f)

        ship_listed = money(float(r["shipping_listed"] or 0) * f)
        ship_coll = money(float(r["shipping_collected"] or 0) * f)
        disc = money(float(r["discount_total"] or 0) * f)
        tax = money(float(r["tax"] or 0) * f)
        refund = money(float(r["refund_total"] or 0) * f)
        grand = money(subtotal + ship_coll + tax)

        dls = json.loads(r["discount_lines"] or "[]")
        for dl in dls:
            if dl.get("discountId"):
                dl["discountId"] = fake_hex("discount", dl["discountId"])
            if isinstance(dl.get("amount"), dict):
                dl["amount"]["value"] = f"{disc:.2f}" if len(dls) == 1 else f"{money(float(dl['amount'].get('value') or 0) * f):.2f}"
            if dl.get("promoCode"):
                dl["promoCode"] = "DEMOCODE"

        addr = json.loads(r["ship_address"] or "{}")
        cust = self.name(r["customer_name"])
        fake_addr = {k: None for k in addr}
        fake_addr.update({"firstName": "Demo", "lastName": cust, "address1": f"{100 + n} Example Ave",
                          "address2": None, "city": "Springfield", "state": addr.get("state") or "NY",
                          "countryCode": "US", "postalCode": "00000", "phone": "555-0100"})

        out = dict(r)
        out.update({
            "id": new_id, "order_number": new_num, "squarespace_num": new_num,
            "customer_email": self.email(r["customer_email"]), "customer_name": cust,
            "ship_address": json.dumps(fake_addr), "subtotal": subtotal,
            "shipping_listed": ship_listed, "shipping_collected": ship_coll,
            "discount_total": disc, "discount_lines": json.dumps(dls), "tax": tax,
            "refund_total": refund, "grand_total": grand, "line_items": json.dumps(items),
        })
        if "shipping" in out:  # legacy column
            out["shipping"] = money(float(r["shipping"] or 0) * f)
        self.order_new[new_id] = out
        return out

    def t_invoices(self, r):
        n = len(self.invoice_map) + 1
        new_num = f"DEMO-INV-{n:04d}"
        self.invoice_map[r["invoice_number"]] = new_num
        o = self.order_new.get(self.order_map.get(r["order_id"]), {})
        out = dict(r)
        out.update({
            "order_id": self.order_map.get(r["order_id"], r["order_id"] and f"demo-order-x{fake_hex('o', r['order_id'], 6)}"),
            "invoice_number": new_num,
            "pdf_path": f"invoices_pdf/invoice_{new_num}.pdf",   # placeholder — no file shipped
            "emailed_to": o.get("customer_email") or self.email(r["emailed_to"]),
            "grand_total": o.get("grand_total", money(float(r["grand_total"] or 0) * self.global_factor)),
        })
        return out

    def _ref(self, ref):
        if ref is None:
            return None
        if ref in self.invoice_map:
            return self.invoice_map[ref]
        if ref in self.order_num_map:
            return self.order_num_map[ref]
        m = re.match(r"^(VI|BANK):(\d+)$", ref)
        if m:
            return ref
        return f"DEMO-REF-{fake_hex('ref', ref, 8)}"

    def t_ledger(self, r):
        out = dict(r)
        oid = self.order_map.get(r["order_id"])
        o = self.order_new.get(oid)
        ref = self._ref(r["reference"])
        out["order_id"] = oid if r["order_id"] else None
        out["reference"] = ref
        acct = r["account"]
        if o is not None:
            mfee = money(o["grand_total"] * self.assumptions.get("merchant_fee_pct", 2.9) / 100
                         + self.assumptions.get("merchant_fee_fixed", 0.30))
            amt = {"REVENUE": o["subtotal"], "SHIPPING": o["shipping_collected"],
                   "DISCOUNT": o["discount_total"], "TAX": o["tax"], "MERCHANT_FEE": mfee}.get(acct)
            if amt is None:
                amt = money(max(float(r["debit"] or 0), float(r["credit"] or 0)) * self.factor(r["order_id"]))
        else:
            amt = money(max(float(r["debit"] or 0), float(r["credit"] or 0)) * self.global_factor)
        out["debit"], out["credit"] = (amt, 0.0) if float(r["debit"] or 0) > 0 else (0.0, amt)
        label = {"REVENUE": "Product revenue", "SHIPPING": "Shipping collected",
                 "DISCOUNT": "Free shipping / promo", "TAX": "Sales tax collected",
                 "MERCHANT_FEE": "Processing fee"}.get(acct, f"{acct.title()} entry")
        out["description"] = f"{label} — {ref}" if ref else label
        return out

    def t_assumptions(self, r):
        return dict(r)          # model parameters (fee %, overhead %) — no PII

    def t_reminders(self, r):
        return dict(r)          # generic NY sales-tax due-date reminders — no PII

    def t_tax_log(self, r):
        out = dict(r)
        for k in ("gross_sales", "taxable_sales", "tax_collected", "payment_amount"):
            out[k] = money(float(r[k] or 0) * self.global_factor)
        out["payment_ref"] = f"DEMO-PMT-{r['id']:04d}" if r["payment_ref"] else r["payment_ref"]
        out["notes"] = None
        return out

    def t_tax_payer(self, r):
        out = dict(r)
        out.update({"legal_name": "Acme Corp (Demo Payer)", "ein": "00-0000000",
                    "address": "100 Example St, Springfield, NY 00000",
                    "state_id": "NY-000000", "locality": "Demo City"})
        return out

    def t_tax_payees(self, r):
        out = dict(r)
        zero_tin = "000-00-0000" if r["kind"] == "EMPLOYEE" else "00-0000000"
        out.update({"legal_name": f"Demo Payee {r['id']:04d}",
                    "business_name": f"Acme Contractor {r['id']:04d}" if r["business_name"] else None,
                    "tin": zero_tin if r["tin"] else None,
                    "address": f"{r['id']} Example Rd, Springfield, NY 00000" if r["address"] else None})
        return out

    def t_tax_payments(self, r):
        out = dict(r)
        for k in ("gross_wages", "total_paid", "fed_wh", "state_wh", "local_wh", "pretax_sec125", "pretax_401k"):
            out[k] = money(round(float(r[k] or 0) * self.global_factor, -2))
        box12 = json.loads(r["box12"] or "[]")
        for b in box12:
            if "amount" in b:
                b["amount"] = money(round(float(b["amount"] or 0) * self.global_factor, -2))
        out["box12"] = json.dumps(box12)
        return out

    def t_tax_generated_forms(self, r):
        out = dict(r)
        stem = f"{(r['form_type'] or 'FORM').replace('-', '')}_Demo_Payee_{int(r['payee_id'] or 0):04d}"
        ext = "efw2" if r["form_type"] == "W-2" else "pub1220"
        out.update({"pdf_path": f"tax_forms/{r['tax_year']}/{stem}.pdf",          # placeholders — no files shipped
                    "zip_path": f"tax_forms/{r['tax_year']}/{stem}.audit.zip",
                    "efile_path": f"tax_forms/{r['tax_year']}/{stem}.{ext}",
                    "checksum_pdf": hashlib.sha256(f"demo-pdf-{r['id']}".encode()).hexdigest(),
                    "checksum_zip": hashlib.sha256(f"demo-zip-{r['id']}".encode()).hexdigest()})
        return out

    def t_tax_packages(self, r):
        out = dict(r)
        out.update({"zip_path": f"tax_forms/packages/filing_package_{r['tax_year']}_{r['period']}_DEMO.zip",
                    "sha256": hashlib.sha256(f"demo-pkg-{r['id']}".encode()).hexdigest()})
        return out

    def _package_figures(self, d, r):
        """Recompute the package's key-figures snapshot from the SANITIZED ledger."""
        fig = json.loads(r["figures"] or "{}")
        start, end = fig.get("start"), fig.get("end")
        if not (start and end):
            r["figures"] = "{}"
            return r

        def s(acct, col):
            return float(d.execute(f"SELECT COALESCE(SUM({col}),0) FROM ledger WHERE account=? AND entry_date BETWEEN ? AND ?",
                                   [acct, start, end]).fetchone()[0])
        rev, ship = s("REVENUE", "credit"), s("SHIPPING", "credit")
        disc, tax = s("DISCOUNT", "debit"), s("TAX", "credit")
        mfee, cogs, exp = s("MERCHANT_FEE", "debit"), s("COGS", "debit"), s("EXPENSE", "debit")
        gross = round(rev + ship, 2)
        r["figures"] = json.dumps({
            "period": fig.get("period"), "start": start, "end": end,
            "gross_revenue": gross, "product_revenue": round(rev, 2), "shipping_collected": round(ship, 2),
            "discounts_absorbed": round(disc, 2), "merchant_fees": round(mfee, 2),
            "sales_tax_collected": round(tax, 2), "cogs": round(cogs, 2), "expenses": round(exp, 2),
            "net_income_est": round(gross - disc - mfee - cogs - exp, 2)})
        return r

    # tables that are empty today — handlers keep future rows from leaking
    def t_vendors(self, r):
        out = dict(r)
        out.update({"name": f"Acme Supply {r['id']:04d}", "contact": "Demo Contact",
                    "email": f"demo-vendor-{r['id']:04d}@example.com", "phone": "555-0100", "notes": None})
        return out

    def t_vendor_invoices(self, r):
        out = dict(r)
        out.update({"invoice_number": f"DEMO-VI-{r['id']:04d}", "description": "Demo supplier invoice",
                    "order_id": self.order_map.get(r["order_id"]) if r["order_id"] else None,
                    "check_number": f"{9000 + r['id']}" if r["check_number"] else None, "notes": None})
        for k in ("amount", "paid_amount"):
            out[k] = money(float(r[k] or 0) * self.global_factor)
        return out

    def t_vendor_invoice_lines(self, r):
        out = dict(r)
        for k in ("unit_cost", "line_tax", "line_shipping", "line_fee"):
            out[k] = money(float(r[k] or 0) * self.global_factor)
        out["notes"] = None
        return out

    def t_checks(self, r):
        out = dict(r)
        out.update({"check_number": f"{9000 + r['id']}", "payee": f"Acme Payee {r['id']:04d}",
                    "memo": None, "bank_account": "Demo Bank ****0000",
                    "amount": money(float(r["amount"] or 0) * self.global_factor)})
        return out

    def t_plaid_items(self, r):
        out = dict(r)
        out.update({"item_id": f"demo-item-{r['id']:04d}", "access_token": "access-demo-redacted",
                    "institution_id": "ins_demo", "institution_name": "Demo Bank",
                    "account_id": f"demo-acct-{r['id']:04d}", "account_name": "Demo Checking",
                    "account_mask": "0000", "cursor": None})
        return out

    def t_bank_transactions(self, r):
        out = dict(r)
        out.update({"plaid_txn_id": f"demo-txn-{r['id']:06d}", "account_id": "demo-acct-0001",
                    "name": f"Demo Merchant {r['id']:04d}", "merchant_name": f"Demo Merchant {r['id']:04d}",
                    "memo": None, "matched_order_id": self.order_map.get(r["matched_order_id"]) if r["matched_order_id"] else None,
                    "amount": money(float(r["amount"] or 0) * self.global_factor)})
        return out

    # ── verification ────────────────────────────────────────────────────────
    def _leak_scan(self, s, d):
        """Collect identifying strings from the real DB and make sure none survive."""
        needles = set()
        for r in s.execute("SELECT customer_email, customer_name, ship_address, id FROM orders"):
            needles.update(filter(None, [r[0], r[1], r[3]]))
            a = json.loads(r[2] or "{}")
            needles.update(v for k, v in a.items() if isinstance(v, str) and k in
                           ("firstName", "lastName", "address1", "address2", "postalCode", "phone"))
        for r in s.execute("SELECT invoice_number, emailed_to FROM invoices"):
            needles.update(filter(None, r))
        for r in s.execute("SELECT legal_name, ein, address, state_id FROM tax_payer"):
            needles.update(filter(None, r))
        for r in s.execute("SELECT legal_name, tin, address FROM tax_payees"):
            needles.update(filter(None, r))
        needles.update(["/Users/", "Library/Application Support", "squarespace-cdn", "gmail.com"])
        needles = {n for n in needles if isinstance(n, str) and len(n.strip()) >= 4 and n not in ("NY", "US")}

        hits = []
        for (t,) in d.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in d.execute(f'SELECT * FROM "{t}"'):
                blob = "\x1f".join(str(v) for v in row if v is not None).lower()
                for n in needles:
                    if n.lower() in blob:
                        hits.append((t, n[:3] + "…"))
        return sorted(set(hits))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("PRIME_ERP_REAL_DB", str(DEFAULT_SRC)))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()
    src = pathlib.Path(a.src).resolve()
    out = pathlib.Path(a.out).resolve()
    if src == out:
        sys.exit("ABORT: --out must differ from --src")
    if not src.exists():
        sys.exit(f"ABORT: source DB not found: {src}")
    Sanitizer(src, out).run()
