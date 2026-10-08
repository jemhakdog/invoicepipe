"""invoicepipe self-check: uv run python checks.py  (exit 0 = pass)"""
import json
import pathlib
import sys
import tempfile

import db
import extract
import jobs

fails = 0


def ok(cond, msg):
    global fails
    if cond:
        print(f"  ok: {msg}")
    else:
        fails += 1
        print(f"FAIL: {msg}")


CLEAN = """Globex Corporation
4120 Commerce Way, Seattle, WA

INVOICE
Invoice #: GLX-7741
Invoice Date: 15/03/2024
Due Date: 2024-04-14

Description      Qty    Price    Amount
Consulting hrs     10   120.00   1200.00
Travel flat         1   250.00    250.00

Subtotal:           1450.00
Tax (10.00%):        145.00
Grand Total:        1595.00
"""

print("[1] money + date parsing")
ok(extract.parse_money("$1,234.56") == 1234.56, "$1,234.56 -> 1234.56")
ok(extract.parse_money("(12.00)") == -12.0, "(12.00) -> -12.0 (credit)")
ok(extract.parse_money("12") == 12.0, "plain int accepted")
ok(extract.parse_money("n/a") is None, "junk -> None")
ok(extract.norm_date("15/03/2024") == "2024-03-15", "D/M/Y -> ISO")
ok(extract.norm_date("03/15/2024") == "2024-03-15", "M/D/Y -> ISO")
ok(extract.norm_date("2024-04-14") == "2024-04-14", "ISO passes through")
ok(extract.norm_date("Mar 3, 2024") == "2024-03-03", "month name -> ISO")
ok(extract.norm_date("someday") == "someday", "unparseable -> untouched")

print("[2] field extraction (pure text, no PDF)")
f = extract.parse_fields(CLEAN)
ok(f["vendor"] == "Globex Corporation", f"vendor (got {f['vendor']!r})")
ok(f["invoice_no"] == "GLX-7741", f"invoice # (got {f['invoice_no']!r})")
ok(f["date"] == "2024-03-15", f"invoice date not overwritten by due date (got {f['date']!r})")
ok(f["due"] == "2024-04-14", f"due date (got {f['due']!r})")
ok(f["subtotal"] == 1450.00, f"subtotal (got {f['subtotal']})")
ok(f["tax"] == 145.00, f"tax (got {f['tax']})")
ok(f["total"] == 1595.00, f"grand total (got {f['total']})")
ok(len(f["items"]) == 2, f"2 line items (got {len(f['items'])})")
ok(f["items"][0]["amount"] == 1200.00 and f["items"][0]["qty"] == 10,
   f"item 0 qty/amount (got {f['items'][0]})")
ok(extract.parse_fields("")["total"] is None, "empty text -> empty fields, no exception")

print("[3] math audit — the §6 check")
ok(extract.audit(f) == [], f"clean invoice -> no warnings (got {extract.audit(f)})")

bad = json.loads(json.dumps(f))
bad["total"] = 1700.00
w = extract.audit(bad)
ok(any("!= total 1700.00" in x for x in w), f"subtotal+tax != total flagged ({w})")

bad = json.loads(json.dumps(f))
bad["items"] = bad["items"][:1]
w = extract.audit(bad)
ok(any("line items sum" in x for x in w), f"line-item sum mismatch flagged ({w})")

bad = json.loads(json.dumps(f))
bad["invoice_no"] = ""
w = extract.audit(bad)
ok("missing invoice_no" in w, f"missing required field flagged ({w})")

bad = json.loads(json.dumps(f))
bad["items"] = []
w = extract.audit(bad)
ok(any("no line items" in x for x in w), f"no items extracted flagged ({w})")

w = extract.audit(f, dup="12")
ok(any("duplicate" in x for x in w), f"duplicate invoice # flagged ({w})")

print("[4] reconcile (review form corrections)")
broken = json.loads(json.dumps(f))
broken["total"] = 1700.00
ok(extract.audit(broken), "setup: broken total is flagged")
fixed, w = extract.reconcile(json.dumps(broken), {"total": "1,595.00"})
ok(w == [], f"fixing the total clears all warnings (got {w})")
fixed, w = extract.reconcile(json.dumps(f), {"total": ""})
ok(any("missing total" in x for x in w), f"blanking total re-flags ({w})")
fixed, w = extract.reconcile(json.dumps(f),
                             {"items": ["Consulting hrs | 1200.00", "Travel flat | 250.00"]})
ok(len(fixed["items"]) == 2 and w == [], f"'desc | amount' textarea lines parse ({w})")

print("[5] duplicate detection (sqlite json_extract)")
tmp = pathlib.Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "t.db"
db.init()
with db.get() as con:
    a = db.add_invoice(con, "a.pdf", "x/a.pdf")
    db.save_fields(con, a, json.dumps({"invoice_no": "DUP-9999"}), "[]", "ready")
    b = db.add_invoice(con, "b.pdf", "x/b.pdf")
    db.save_fields(con, b, json.dumps({}), "[]", "processing")
    ok(jobs.find_dup(con, "dup-9999", b) == str(a), "same invoice # (any case) -> dup id")
    ok(jobs.find_dup(con, "NEW-1", b) is None, "new invoice # -> None")
    ok(jobs.find_dup(con, "", b) is None, "empty invoice # -> None")

print("[6] end-to-end IO path (real PDF through pymupdf)")
import pymupdf

sample = tmp / "sample.pdf"
doc = pymupdf.open()
doc.new_page().insert_text((50, 72), CLEAN, fontsize=11)
doc.save(sample)
text = extract.extract_text(str(sample))
ok(len(text) > 100, "text layer extracted")
ok(extract.page_count(str(sample)) == 1, "page_count = 1")
ok(len(extract.render_page(str(sample), 1)) > 1000, "page 1 renders to PNG")
ok(extract.render_page(str(sample), 99) is None, "page out of range -> None")
ok(extract.audit(extract.parse_fields(text)) == [], "parsed PDF audits clean")

with db.get() as con:
    inv = db.add_invoice(con, "sample.pdf", str(sample))
    row = db.get_invoice(con, inv)
    res = jobs.process_one(con, row)
ok(res["status"] == "ready", f"worker marks clean invoice ready (got {res})")

print("[7] no hardcoded credentials")
bad_lines = []
for pth in pathlib.Path(__file__).parent.glob("*.py"):
    if pth.name == "checks.py":  # this file holds the literals on purpose
        continue
    for i, line in enumerate(pth.read_text(encoding="utf8").splitlines(), 1):
        if "sk-" in line or "Authorization: Bearer" in line or "serper" in line \
           or "hunter.io" in line:
            bad_lines.append(f"{pth.name}:{i}")
ok(not bad_lines, f"no keys in source ({bad_lines})")

print()
sys.exit(1 if fails else 0)
