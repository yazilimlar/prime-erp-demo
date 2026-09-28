#!/usr/bin/env python3
"""
tax_engine.py — rule-driven, audit-level tax form processing engine.

Core classes
------------
  ValidatorEngine  — TIN/EIN/SSN validation, $600 threshold, W-9/backup-withholding,
                     simulated IRS TIN-matching, cross-validation exception report.
  AuditLogger      — source-data-mapping manifest, calculation step log, SHA-256
                     checksums, ISO-8601 timestamps, form-revision metadata.
  W2Calculator     — full W-2 box computation (the most complex form).
  Form1099Calculator — 1099-NEC / 1099-MISC incl. 24% backup withholding.
  TaxFormFactory   — builds a form package (data + audit) for any supported type.
  OutputBuilder    — print-ready PDF (watermark + PDF417), EFW2 / Pub-1220 file,
                     and a consolidated "Audit Defense" ZIP.

Everything reads layout/rates from tax_forms_config.py (single config file).
"""

import hashlib, json, io, zipfile, datetime, re, base64
from pathlib import Path
import tax_forms_config as CFG


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _money(v):
    try:    return round(float(v or 0), 2)
    except: return 0.0


# ════════════════════════════════════════════════════════════════════════════
#  ValidatorEngine
# ════════════════════════════════════════════════════════════════════════════
class ValidatorEngine:
    """Format + business-rule validation. IRS TIN-matching is SIMULATED:
    we validate structure (length, ranges, checksumable format) and flag
    obvious invalid TIN/Name combinations — we do NOT call IRS e-Services."""

    SSN_RE = re.compile(r"^\d{3}-?\d{2}-?\d{4}$")
    EIN_RE = re.compile(r"^\d{2}-?\d{7}$")

    @staticmethod
    def _digits(s): return re.sub(r"\D", "", s or "")

    @classmethod
    def validate_ein(cls, ein):
        d = cls._digits(ein)
        if len(d) != 9:
            return False, f"EIN must be 9 digits (got {len(d)})"
        # Invalid IRS campus prefixes are rare; flag all-zero / repeated
        if d == "000000000" or len(set(d)) == 1:
            return False, "EIN is structurally invalid (all-zero/repeated)"
        return True, "OK"

    @classmethod
    def validate_ssn(cls, ssn):
        d = cls._digits(ssn)
        if len(d) != 9:
            return False, f"SSN must be 9 digits (got {len(d)})"
        area, group, serial = d[:3], d[3:5], d[5:]
        if area in ("000", "666") or area[0] == "9":
            return False, f"SSN area '{area}' is not issuable"
        if group == "00" or serial == "0000":
            return False, "SSN group/serial cannot be all zeros"
        return True, "OK"

    @classmethod
    def validate_tin_name(cls, tin, name, is_employee=True):
        """Simulated TIN/Name match. Returns (match, reason)."""
        ok, msg = (cls.validate_ssn(tin) if is_employee else cls.validate_ein(tin))
        if not ok:
            return False, f"TIN invalid: {msg}"
        if not (name or "").strip():
            return False, "Name missing — cannot match to TIN"
        # Simulated deterministic 'match score': format valid + name present = match.
        return True, "Simulated match: format valid, name present"

    @classmethod
    def needs_backup_withholding(cls, payee):
        """Missing/invalid W-9 → 24% backup withholding (returns reason or None)."""
        if not payee.get("w9_on_file"):
            return "No valid W-9 on file"
        ok, msg = cls.validate_tin_name(payee.get("tin"), payee.get("legal_name"),
                                        is_employee=False)
        if not ok:
            return f"W-9 present but TIN/Name failed: {msg}"
        return None

    @classmethod
    def meets_1099nec_threshold(cls, total_paid):
        return _money(total_paid) >= CFG.RATES["form_1099nec_threshold"]

    @classmethod
    def cross_validation_report(cls, payer, payees):
        """Pre-finalization exception report (Requirement #4)."""
        exceptions = []
        ok, msg = cls.validate_ein(payer.get("ein"))
        if not ok:
            exceptions.append({"severity": "ERROR", "entity": "PAYER",
                               "name": payer.get("legal_name"), "issue": f"Payer EIN: {msg}"})
        for p in payees:
            is_emp = p.get("kind") == "EMPLOYEE"
            ok, msg = cls.validate_tin_name(p.get("tin"), p.get("legal_name"), is_employee=is_emp)
            if not ok:
                exceptions.append({"severity": "ERROR", "entity": p.get("kind"),
                                   "name": p.get("legal_name"), "issue": msg})
            if not is_emp:
                bw = cls.needs_backup_withholding(p)
                if bw:
                    exceptions.append({"severity": "WARN", "entity": "CONTRACTOR",
                                       "name": p.get("legal_name"),
                                       "issue": f"Backup withholding (24%) will apply — {bw}"})
        return exceptions


# ════════════════════════════════════════════════════════════════════════════
#  AuditLogger  — one per generated form
# ════════════════════════════════════════════════════════════════════════════
class AuditLogger:
    def __init__(self, form_type, entity_name):
        self.form_type = form_type
        self.entity_name = entity_name
        self.revision = CFG.FORM_REVISIONS.get(form_type, {})
        self.created_at = _now_iso()
        self.source_map = []     # field → raw input provenance
        self.calc_steps = []     # computed field breakdowns
        self.flags = []

    def map_source(self, box, label, value, source, ingested_at=None):
        self.source_map.append({
            "box": box, "label": label, "value": value,
            "source_field": source, "ingested_at": ingested_at or self.created_at,
        })

    def log_calc(self, field, formula, inputs, result):
        self.calc_steps.append({
            "field": field, "formula": formula, "inputs": inputs,
            "result": round(result, 2), "at": _now_iso(),
        })

    def flag(self, severity, message):
        self.flags.append({"severity": severity, "message": message, "at": _now_iso()})

    def manifest(self, file_artifacts=None):
        """file_artifacts: list of {name, sha256, bytes_len}."""
        return {
            "form_type": self.form_type,
            "entity": self.entity_name,
            "form_revision": self.revision,
            "tax_year": CFG.TAX_YEAR,
            "generated_at_iso": self.created_at,
            "source_data_mapping": self.source_map,
            "calculation_logic": self.calc_steps,
            "flags": self.flags,
            "artifacts": file_artifacts or [],
            "_disclaimer": CFG.PDF_DISCLAIMER,
        }


# ════════════════════════════════════════════════════════════════════════════
#  W-2 calculator (most complex form)
# ════════════════════════════════════════════════════════════════════════════
class W2Calculator:
    def __init__(self, audit: AuditLogger):
        self.a = audit
        self.r = CFG.RATES

    def compute(self, payer, employee, pay):
        """pay: dict of gross_wages, fed_wh, state_wh, local_wh, pretax_sec125,
        pretax_401k, box12 (list of {code, amount}), ingested_at."""
        ing = pay.get("ingested_at")
        gross   = _money(pay.get("gross_wages"))
        sec125  = _money(pay.get("pretax_sec125"))    # pretax health — reduces 1,3,5
        k401    = _money(pay.get("pretax_401k"))      # 401(k) — reduces Box 1 only
        fed_wh  = _money(pay.get("fed_wh"))
        state_wh= _money(pay.get("state_wh"))
        local_wh= _money(pay.get("local_wh"))

        # Box 1 — federal taxable wages
        box1 = round(gross - sec125 - k401, 2)
        self.a.log_calc("Box 1 (Wages)",
            "gross_wages − pretax_sec125 − pretax_401k",
            {"gross_wages": gross, "pretax_sec125": sec125, "pretax_401k": k401}, box1)

        # Box 3 — Social Security wages (cap at wage base; 401k NOT excluded)
        ss_eligible = round(gross - sec125, 2)
        box3 = round(min(ss_eligible, self.r["ss_wage_base"]), 2)
        self.a.log_calc("Box 3 (SS wages)",
            "min(gross − pretax_sec125, ss_wage_base)",
            {"gross_minus_sec125": ss_eligible, "ss_wage_base": self.r["ss_wage_base"]}, box3)

        # Box 4 — Social Security tax withheld
        box4 = round(box3 * self.r["ss_rate_employee"], 2)
        self.a.log_calc("Box 4 (SS tax)", "Box3 × 6.2%",
            {"box3": box3, "rate": self.r["ss_rate_employee"]}, box4)

        # Box 5 — Medicare wages (no cap; 401k NOT excluded)
        box5 = round(gross - sec125, 2)
        self.a.log_calc("Box 5 (Medicare wages)", "gross − pretax_sec125",
            {"gross": gross, "pretax_sec125": sec125}, box5)

        # Box 6 — Medicare tax + Additional Medicare over threshold
        base_med = box5 * self.r["medicare_rate"]
        addl = max(0.0, box5 - self.r["addl_medicare_threshold"]) * self.r["addl_medicare_rate"]
        box6 = round(base_med + addl, 2)
        self.a.log_calc("Box 6 (Medicare tax)",
            "Box5 × 1.45% + max(0, Box5 − 200,000) × 0.9%",
            {"box5": box5, "medicare_rate": self.r["medicare_rate"],
             "addl_rate": self.r["addl_medicare_rate"], "addl_base": max(0.0, box5 - self.r["addl_medicare_threshold"])}, box6)

        boxes = {
            "a": employee.get("tin"), "b": payer.get("ein"),
            "c": payer.get("legal_name") + " · " + (payer.get("address") or ""),
            "e": employee.get("legal_name"), "f": employee.get("address"),
            "1": box1, "2": fed_wh, "3": box3, "4": box4, "5": box5, "6": box6,
            "12": pay.get("box12") or [],
            "15": payer.get("state_id"), "16": box1, "17": state_wh,
            "18": box1, "19": local_wh, "20": payer.get("locality"),
        }
        # Source-data mapping for every box (Requirement #1)
        srcval = {"payments.gross_wages": gross, "payments.fed_wh": fed_wh,
                  "payments.state_wh": state_wh, "payments.local_wh": local_wh,
                  "payee.tin": employee.get("tin"), "payer.ein": payer.get("ein"),
                  "payee.legal_name": employee.get("legal_name"),
                  "payee.address": employee.get("address"),
                  "payer.address": payer.get("address"), "payer.state_id": payer.get("state_id"),
                  "payer.locality": payer.get("locality"), "computed": "see calculation_logic",
                  "payments.box12": pay.get("box12") or []}
        for b in CFG.W2_BOXES:
            self.a.map_source(b["box"], b["label"], boxes.get(b["box"]),
                              b["source"] + " → " + str(srcval.get(b["source"], "")), ing)
        return boxes


# ════════════════════════════════════════════════════════════════════════════
#  1099-NEC / 1099-MISC calculator (incl. backup withholding)
# ════════════════════════════════════════════════════════════════════════════
class Form1099Calculator:
    def __init__(self, audit: AuditLogger):
        self.a = audit
        self.r = CFG.RATES

    def compute_nec(self, payer, contractor, pay):
        ing = pay.get("ingested_at")
        total = _money(pay.get("total_paid"))
        self.a.log_calc("Box 1 (Nonemployee comp.)", "sum(quarterly payments)",
                        {"total_paid": total}, total)
        bw_reason = ValidatorEngine.needs_backup_withholding(contractor)
        box4 = 0.0
        if bw_reason:
            box4 = round(total * self.r["backup_withholding_rate"], 2)
            self.a.log_calc("Box 4 (Backup withholding)",
                "total_paid × 24% (missing/invalid W-9)",
                {"total_paid": total, "rate": self.r["backup_withholding_rate"]}, box4)
            self.a.flag("WARN", f"Backup withholding applied: {bw_reason}")
        boxes = {"1": total, "4": box4, "5": _money(pay.get("state_wh")),
                 "6": payer.get("state_id"), "7": total}
        for b in CFG.FORM_1099NEC_BOXES:
            self.a.map_source(b["box"], b["label"], boxes.get(b["box"]), b["source"], ing)
        return boxes, bw_reason


# ════════════════════════════════════════════════════════════════════════════
#  TaxFormFactory — dispatch + package
# ════════════════════════════════════════════════════════════════════════════
class TaxFormFactory:
    SUPPORTED = ("W-2", "1099-NEC", "1099-MISC", "W-9", "1040-BWH")

    @classmethod
    def create(cls, form_type, payer, payee, pay):
        if form_type not in cls.SUPPORTED:
            raise ValueError(f"Unsupported form: {form_type}")
        audit = AuditLogger(form_type, payee.get("legal_name"))
        meta = {"form_type": form_type, "tax_year": CFG.TAX_YEAR,
                "revision": CFG.FORM_REVISIONS.get(form_type, {}).get("revision"),
                "payer": payer, "payee": payee}
        if form_type == "W-2":
            meta["boxes"] = W2Calculator(audit).compute(payer, payee, pay)
        elif form_type == "1099-NEC":
            boxes, bw = Form1099Calculator(audit).compute_nec(payer, payee, pay)
            meta["boxes"] = boxes
            meta["backup_withholding"] = bw
            if not ValidatorEngine.meets_1099nec_threshold(pay.get("total_paid")):
                audit.flag("INFO", f"Below ${CFG.RATES['form_1099nec_threshold']:.0f} threshold — "
                                   "1099-NEC not required (generated for records).")
        elif form_type == "1099-MISC":
            total = _money(pay.get("total_paid"))
            meta["boxes"] = {"3": total, "4": 0.0, "16": _money(pay.get("state_wh"))}
            audit.map_source("3", "Other income", total, "payments.total_paid", pay.get("ingested_at"))
        elif form_type == "W-9":
            meta["fields"] = {k: payee.get(k) for k in CFG.W9_FIELDS}
        elif form_type == "1040-BWH":
            total = _money(pay.get("total_paid"))
            wh = round(total * CFG.RATES["backup_withholding_rate"], 2)
            audit.log_calc("Backup withholding statement", "total × 24%",
                           {"total": total}, wh)
            meta["boxes"] = {"total_subject": total, "backup_withheld": wh}
        meta["_audit"] = audit
        return meta


# ════════════════════════════════════════════════════════════════════════════
#  OutputBuilder — PDF (watermark + PDF417), EFW2/Pub-1220, Audit ZIP
# ════════════════════════════════════════════════════════════════════════════
class OutputBuilder:
    def __init__(self, out_dir):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)

    # ---- PDF417 2D barcode (graceful degradation) ----
    @staticmethod
    def _pdf417_png_b64(payload: str):
        try:
            from pdf417gen import encode, render_image
            codes = encode(payload, columns=6, security_level=4)
            img = render_image(codes, scale=2, ratio=3, padding=4)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    def _barcode_payload(self, form):
        b = form.get("boxes", {})
        return "|".join(str(x) for x in [
            form["form_type"], form["tax_year"], form["payer"].get("ein"),
            form["payee"].get("tin"), b.get("1", ""), b.get("2", ""),
            form["_audit"].created_at])

    # ---- One shared browser session for a whole generation batch ----
    def start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._br = self._pw.chromium.launch()
        self._pg = self._br.new_page()
        return self

    def stop(self):
        try:
            if getattr(self, "_br", None): self._br.close()
            if getattr(self, "_pw", None): self._pw.stop()
        except Exception:
            pass
        self._pg = self._br = self._pw = None

    def __enter__(self): return self.start()
    def __exit__(self, *a): self.stop()

    # ---- HTML → PDF via Playwright (reuses session page if started) ----
    _MARGIN = {"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"}
    def render_pdf(self, form, file_stem):
        html = self._form_html(form)
        pdf_path = self.out / f"{file_stem}.pdf"
        if getattr(self, "_pg", None):                       # batch mode
            self._pg.set_content(html, wait_until="load")
            self._pg.pdf(path=str(pdf_path), format="Letter", margin=self._MARGIN)
        else:                                                # one-off mode
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                br = p.chromium.launch(); pg = br.new_page()
                pg.set_content(html, wait_until="load")
                pg.pdf(path=str(pdf_path), format="Letter", margin=self._MARGIN)
                br.close()
        return pdf_path

    def _form_html(self, form):
        ft = form["form_type"]; b = form.get("boxes", {})
        payer, payee = form["payer"], form["payee"]
        rev = CFG.FORM_REVISIONS.get(ft, {})
        bc = self._pdf417_png_b64(self._barcode_payload(form))
        bc_html = (f'<img src="data:image/png;base64,{bc}" style="height:54px">' if bc
                   else '<div style="font:9px monospace;color:#888">[PDF417 barcode unavailable]</div>')
        # box rows from config so layout follows the single config file
        layout = {"W-2": CFG.W2_BOXES, "1099-NEC": CFG.FORM_1099NEC_BOXES,
                  "1099-MISC": CFG.FORM_1099MISC_BOXES}.get(ft, [])
        rows = ""
        for box in layout:
            val = b.get(box["box"], "")
            if isinstance(val, list):
                val = ", ".join(f'{x.get("code")}:{x.get("amount")}' for x in val) if val else ""
            disp = f"${val:,.2f}" if isinstance(val, (int, float)) else (val or "—")
            rows += (f'<tr><td class="bx">{box["box"]}</td><td>{box["label"]}</td>'
                     f'<td class="v">{disp}</td></tr>')
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
        @page{{size:Letter}}
        body{{font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#111;position:relative}}
        .wm{{position:fixed;top:42%;left:8%;font-size:34px;color:rgba(200,0,0,.10);
            transform:rotate(-18deg);font-weight:800;letter-spacing:1px;white-space:nowrap}}
        .hdr{{display:flex;justify-content:space-between;border-bottom:3px solid #111;padding-bottom:8px;margin-bottom:10px}}
        .ft-title{{font-size:22px;font-weight:800}}
        .ft-sub{{font-size:10px;color:#555}}
        .party{{display:flex;gap:20px;margin-bottom:10px}}
        .party div{{flex:1;border:1px solid #bbb;border-radius:6px;padding:8px}}
        .party h4{{font-size:9px;text-transform:uppercase;color:#777;margin:0 0 4px}}
        table{{width:100%;border-collapse:collapse}}
        td{{border:1px solid #ccc;padding:5px 7px}}
        .bx{{width:34px;font-weight:700;background:#f3f3f3;text-align:center}}
        .v{{text-align:right;font-variant-numeric:tabular-nums;font-weight:700;width:130px}}
        .foot{{margin-top:14px;display:flex;justify-content:space-between;align-items:flex-end}}
        .disc{{font-size:8px;color:#777;max-width:62%;line-height:1.4}}
        </style></head><body>
        <div class="wm">{CFG.PDF_WATERMARK}</div>
        <div class="hdr"><div><div class="ft-title">Form {ft}</div>
          <div class="ft-sub">Tax Year {form['tax_year']} · {rev.get('revision','')} · OMB {rev.get('omb','')}</div></div>
          <div style="text-align:right"><div class="ft-sub">Prime Industrial ERP</div>
          <div class="ft-sub">Generated {form['_audit'].created_at}</div></div></div>
        <div class="party">
          <div><h4>Payer / Employer</h4><strong>{payer.get('legal_name','')}</strong><br>
            EIN {payer.get('ein','')}<br>{payer.get('address','')}</div>
          <div><h4>{'Employee' if ft=='W-2' else 'Recipient'}</h4><strong>{payee.get('legal_name','')}</strong><br>
            TIN {payee.get('tin','')}<br>{payee.get('address','')}</div>
        </div>
        <table>{rows}</table>
        <div class="foot"><div class="disc">{CFG.PDF_DISCLAIMER}</div><div>{bc_html}</div></div>
        </body></html>"""

    # ---- EFW2 (W-2) / Pub-1220 (1099) machine-readable file ----
    def efile_record(self, form):
        ft = form["form_type"]; b = form.get("boxes", {})
        payer, payee = form["payer"], form["payee"]
        dig = ValidatorEngine._digits
        def cents(x): return str(int(round(_money(x) * 100))).zfill(11)
        if ft == "W-2":
            # Simplified EFW2 RE (employer) + RW (employee) records (512 cols truncated)
            re_rec = ("RE" + str(CFG.TAX_YEAR) + dig(payer.get("ein")) +
                      (payer.get("legal_name","")[:57].ljust(57)))
            rw_rec = ("RW" + dig(payee.get("tin")) +
                      (payee.get("legal_name","")[:37].ljust(37)) +
                      cents(b.get("1")) + cents(b.get("2")) +
                      cents(b.get("3")) + cents(b.get("4")) +
                      cents(b.get("5")) + cents(b.get("6")))
            return "EFW2", re_rec + "\n" + rw_rec + "\n"
        else:
            # Simplified Pub-1220 'B' (payee) record
            b_rec = ("B" + str(CFG.TAX_YEAR) + dig(payee.get("tin")) +
                     (payee.get("legal_name","")[:40].ljust(40)) +
                     cents(b.get("1")) + cents(b.get("4")))
            return "PUB1220", b_rec + "\n"

    # ---- Audit Defense ZIP (Requirement: Backup Documentation) ----
    def build_audit_zip(self, form, pdf_path, efile_name, efile_text, exceptions):
        audit = form["_audit"]
        pdf_bytes = Path(pdf_path).read_bytes()
        efile_bytes = efile_text.encode()
        artifacts = [
            {"name": Path(pdf_path).name, "sha256": _sha256(pdf_bytes), "bytes": len(pdf_bytes)},
            {"name": efile_name, "sha256": _sha256(efile_bytes), "bytes": len(efile_bytes)},
        ]
        manifest = audit.manifest(artifacts)
        snapshot = {"payer": form["payer"], "payee": form["payee"], "boxes": form.get("boxes"),
                    "ingested_snapshot_at": _now_iso()}
        checksum_txt = "\n".join(f"{a['sha256']}  {a['name']}  ({a['bytes']} bytes)" for a in artifacts)

        zip_path = Path(pdf_path).with_suffix(".audit.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(Path(pdf_path).name, pdf_bytes)
            z.writestr(efile_name, efile_bytes)
            z.writestr("audit_manifest.json", json.dumps(manifest, indent=2, default=str))
            z.writestr("source_data_snapshot.json", json.dumps(snapshot, indent=2, default=str))
            z.writestr("calculation_log.json", json.dumps(audit.calc_steps, indent=2, default=str))
            z.writestr("exception_report.json", json.dumps(exceptions, indent=2, default=str))
            z.writestr("checksum_manifest.txt",
                       f"# Tamper-evident SHA-256 checksums · generated {_now_iso()}\n" + checksum_txt + "\n")
            z.writestr("README.txt",
                       f"Audit Defense package for {form['form_type']} — {form['payee'].get('legal_name')}\n"
                       f"Tax Year {CFG.TAX_YEAR}\n\n{CFG.PDF_DISCLAIMER}\n")
        return zip_path, manifest, artifacts
