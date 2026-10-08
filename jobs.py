"""Background worker: drain the processing queue -> extract -> audit -> save.

Pure/IO split: run_once() is the whole pipeline; extract.py holds the logic
that checks.py tests. One daemon thread, no broker (BUILD §2).
"""
import json
import threading
import time

import db
import extract


def find_dup(con, invoice_no: str, self_id: int) -> str | None:
    """Duplicate invoice # already in the store (PRD key feature 5)."""
    if not invoice_no:
        return None
    row = con.execute(
        "SELECT id FROM invoices WHERE id != ? "
        "AND lower(json_extract(fields,'$.invoice_no')) = lower(?) LIMIT 1",
        (self_id, invoice_no),
    ).fetchone()
    return str(row["id"]) if row else None


def process_one(con, row) -> dict:
    """One invoice: text -> fields -> dup check -> audit -> status."""
    text = extract.extract_text(row["path"])
    fields = extract.parse_fields(text)
    if not text.strip():
        warnings = ["no text layer (scanned image) — MVP has no OCR, "
                    "type the fields in manually, then approve"]
        status = "needs_review"
    else:
        warnings = extract.audit(fields, find_dup(con, fields.get("invoice_no", ""), row["id"]))
        status = "ready" if not warnings else "needs_review"
    db.save_fields(con, row["id"], json.dumps(fields), json.dumps(warnings), status)
    return {"id": row["id"], "status": status, "warnings": warnings}


def run_once(app=None) -> list[dict]:
    out = []
    with db.get() as con:
        for row in db.pending(con):
            try:
                out.append(process_one(con, row))
            except Exception as e:  # never let one bad file kill the queue
                db.save_fields(con, row["id"], json.dumps({}), 
                               json.dumps([f"extraction error: {e}"]), "needs_review")
                out.append({"id": row["id"], "status": "needs_review",
                            "warnings": [f"extraction error: {e}"]})
    if app and out:
        print(f"[invoicepipe] processed {len(out)}: "
              f"{[ (r['id'], r['status']) for r in out ]}")
    return out


def _loop() -> None:
    while True:
        try:
            run_once()
        except Exception as e:  # never let the thread die
            print(f"[invoicepipe] worker error: {e}")
        time.sleep(1)


def start_worker(_app=None) -> threading.Thread:
    t = threading.Thread(target=_loop, daemon=True, name="invoicepipe-worker")
    t.start()
    return t
