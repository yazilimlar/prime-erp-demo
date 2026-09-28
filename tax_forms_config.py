#!/usr/bin/env python3
"""
tax_forms_config.py — CENTRALIZED tax-form configuration.

This is the SINGLE source of truth for form layouts, box definitions, rates,
thresholds, and revision metadata. Per the "dynamic form updates" requirement,
a new form revision (e.g. a new Box 12 code, a changed SS wage base, or a new
backup-withholding rate) is applied by editing THIS FILE ONLY — the engine in
tax_engine.py reads everything from here and never hard-codes a box or rate.

⚠️ COMPLIANCE NOTE: Figures below are configuration defaults and MUST be
verified against the official IRS publication for the filing year before use.
Outputs are DRAFT working copies / recipient copies and the EFW2/Pub-1220 data
files — NOT a substitute for official e-filing via IRS IRIS/FIRE or a CPA.
"""

# ── Filing year & revision metadata ───────────────────────────────────────────
TAX_YEAR = 2026
FORM_REVISIONS = {
    "W-2":        {"revision": "2026", "omb": "1545-0008",  "efile": "EFW2 (SSA)"},
    "1099-NEC":   {"revision": "2026", "omb": "1545-0116",  "efile": "Pub 1220 / IRIS"},
    "1099-MISC":  {"revision": "2026", "omb": "1545-0115",  "efile": "Pub 1220 / IRIS"},
    "W-9":        {"revision": "Rev. March 2024", "omb": "1545-0115", "efile": "N/A (collected, not filed)"},
    "1040-BWH":   {"revision": "2026", "omb": "1545-0074",  "efile": "Backup-withholding statement"},
}

# ── Federal rates & thresholds (verify yearly) ────────────────────────────────
RATES = {
    "ss_rate_employee":        0.062,     # Social Security (employee share)
    "ss_rate_employer":        0.062,
    "ss_wage_base":            183_600.0, # 2026 est. — VERIFY against SSA release
    "medicare_rate":           0.0145,    # Medicare (employee share)
    "medicare_rate_employer":  0.0145,
    "addl_medicare_rate":      0.009,     # Additional Medicare Tax
    "addl_medicare_threshold": 200_000.0, # single filer withholding threshold
    "backup_withholding_rate": 0.24,      # 24% flat — missing/invalid W-9
    "form_1099nec_threshold":  600.0,     # $600 reporting threshold
    "form_1099misc_threshold": 600.0,
    "form_1099misc_royalty_threshold": 10.0,
}

# ── W-2 box layout (Boxes 1–20). compute=True → ValidatorEngine/W2Calculator ──
# 'source' is the raw input key used by Source-Data-Mapping audit manifest.
W2_BOXES = [
    {"box": "a",  "label": "Employee SSN",                 "source": "payee.tin",          "compute": False},
    {"box": "b",  "label": "Employer EIN",                 "source": "payer.ein",          "compute": False},
    {"box": "c",  "label": "Employer name/address",        "source": "payer.address",      "compute": False},
    {"box": "e",  "label": "Employee name",                "source": "payee.legal_name",   "compute": False},
    {"box": "f",  "label": "Employee address",             "source": "payee.address",      "compute": False},
    {"box": "1",  "label": "Wages, tips, other comp.",     "source": "payments.gross_wages","compute": True},
    {"box": "2",  "label": "Federal income tax withheld",  "source": "payments.fed_wh",    "compute": True},
    {"box": "3",  "label": "Social Security wages",        "source": "payments.gross_wages","compute": True},
    {"box": "4",  "label": "Social Security tax withheld", "source": "computed",           "compute": True},
    {"box": "5",  "label": "Medicare wages and tips",      "source": "payments.gross_wages","compute": True},
    {"box": "6",  "label": "Medicare tax withheld",        "source": "computed",           "compute": True},
    {"box": "12", "label": "Box 12 codes",                 "source": "payments.box12",     "compute": False},
    {"box": "15", "label": "State / Employer state ID",    "source": "payer.state_id",     "compute": False},
    {"box": "16", "label": "State wages, tips, etc.",      "source": "payments.gross_wages","compute": True},
    {"box": "17", "label": "State income tax",             "source": "payments.state_wh",  "compute": True},
    {"box": "18", "label": "Local wages, tips, etc.",      "source": "payments.gross_wages","compute": True},
    {"box": "19", "label": "Local income tax",             "source": "payments.local_wh",  "compute": True},
    {"box": "20", "label": "Locality name",               "source": "payer.locality",     "compute": False},
]

# ── W-2 Box 12 codes (edit here to add new codes per revision) ────────────────
BOX_12_CODES = {
    "A": "Uncollected SS tax on tips",
    "B": "Uncollected Medicare tax on tips",
    "C": "Taxable cost of group-term life insurance over $50,000",
    "D": "401(k) elective deferrals",
    "E": "403(b) elective deferrals",
    "DD": "Cost of employer-sponsored health coverage",
    "W": "Employer HSA contributions",
    "AA": "Roth 401(k) contributions",
    "BB": "Roth 403(b) contributions",
    "FF": "Permitted benefits under a QSEHRA",
}

# ── 1099-NEC box layout ───────────────────────────────────────────────────────
FORM_1099NEC_BOXES = [
    {"box": "1", "label": "Nonemployee compensation",     "source": "payments.total_paid", "compute": True},
    {"box": "4", "label": "Federal income tax withheld",  "source": "computed",            "compute": True},
    {"box": "5", "label": "State tax withheld",           "source": "payments.state_wh",   "compute": True},
    {"box": "6", "label": "State/Payer's state no.",      "source": "payer.state_id",      "compute": False},
    {"box": "7", "label": "State income",                 "source": "payments.total_paid", "compute": True},
]

# ── 1099-MISC box layout (subset of common boxes) ─────────────────────────────
FORM_1099MISC_BOXES = [
    {"box": "1",  "label": "Rents",                       "source": "payments.rents",      "compute": False},
    {"box": "2",  "label": "Royalties",                   "source": "payments.royalties",  "compute": False},
    {"box": "3",  "label": "Other income",                "source": "payments.other",      "compute": False},
    {"box": "4",  "label": "Federal income tax withheld", "source": "computed",            "compute": True},
    {"box": "16", "label": "State tax withheld",          "source": "payments.state_wh",   "compute": True},
]

# ── W-9 collected fields ──────────────────────────────────────────────────────
W9_FIELDS = ["legal_name", "business_name", "tax_classification", "tin", "address",
             "certification_signed", "date_signed"]

# ── State / local default config (NY example) ─────────────────────────────────
STATE_CONFIG = {
    "NY": {"state_income_tax_default": 0.0,  "state_id_required": True,
           "locality_codes": {"NYC": "New York City", "YNK": "Yonkers"}},
}

# ── PDF presentation ──────────────────────────────────────────────────────────
PDF_WATERMARK = "DRAFT — WORKING COPY · NOT FOR OFFICIAL SUBMISSION"
PDF_DISCLAIMER = ("Draft generated by Prime Industrial ERP for preparation/recordkeeping. "
                  "TIN matching is SIMULATED (format + checksum only), not IRS e-Services. "
                  "File official returns via IRS IRIS/FIRE or a licensed preparer.")
