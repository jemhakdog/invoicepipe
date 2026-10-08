"""invoicepipe — upload invoice PDF -> extract -> math audit -> review -> save.
Run: uv run flask --app app run
"""
import json
import pathlib

import db
from flask import Flask, abort, jsonify, render_template, request, send_file

app = Flask(__name__)
db.init()

UPLOAD_DIR = pathlib.Path(__file__).parent / "var" / "uploads"
PREVIEW_DIR = pathlib.Path(__file__).parent / "var" / "previews"
ALLOWED = {".pdf"}
MAX_MB = 20

_worker = None


def worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        import jobs  # lazy: starts the background processing thread

        _worker = jobs.start_worker(app)
    return _worker


# --- pages ---------------------------------------------------------------

@app.get("/")
def index():
    with db.get() as con:
        rows = db.list_invoices(con)
    invoices = [{**dict(r), "fields": json.loads(r["fields"]),
                 "warnings": json.loads(r["warnings"])} for r in rows]
    return render_template("index.html", invoices=invoices)


@app.get("/invoice/<int:inv_id>")
def invoice(inv_id: int):
    import extract

    with db.get() as con:
        row = db.get_invoice(con, inv_id)
    if not row:
        abort(404)
    inv = dict(row)
    inv["fields"] = json.loads(row["fields"])
    inv["warnings"] = json.loads(row["warnings"])
    pages = extract.page_count(row["path"])
    return render_template("review.html", inv=inv, pages=pages)


# --- actions (json) ------------------------------------------------------

@app.post("/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, msg="no file"), 400
    ext = pathlib.Path(f.filename).suffix.lower()
    if ext not in ALLOWED:
        return jsonify(ok=False, msg=f"only {', '.join(sorted(ALLOWED))} files"), 400
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{len(list(UPLOAD_DIR.iterdir()))+1:04d}_{pathlib.Path(f.filename).name}"
    f.save(path)
    with db.get() as con:
        inv_id = db.add_invoice(con, f.filename, str(path))
    worker()  # make sure the thread is up
    return jsonify(ok=True, id=inv_id, msg="uploaded — extracting…")


@app.get("/invoice/<int:inv_id>/status")
def inv_status(inv_id: int):
    with db.get() as con:
        row = db.get_invoice(con, inv_id)
    if not row:
        abort(404)
    return jsonify(status=row["status"], warnings=json.loads(row["warnings"]))


@app.post("/invoice/<int:inv_id>/save")
def save(inv_id: int):
    """Human corrections from the review form -> re-audit (incl. duplicate #)."""
    import extract
    import jobs

    body = request.get_json(silent=True) or {}
    with db.get() as con:
        row = db.get_invoice(con, inv_id)
        if not row:
            abort(404)
        fields, _ = extract.reconcile(row["fields"], body)
        dup = jobs.find_dup(con, fields.get("invoice_no") or "", inv_id)
        warnings = extract.audit(fields, dup)
        status = "ready" if not warnings else "needs_review"
        db.save_fields(con, inv_id, json.dumps(fields), json.dumps(warnings), status)
    return jsonify(ok=True, status=status, warnings=warnings,
                   msg="saved — " + ("clean" if not warnings else f"{len(warnings)} warning(s)"))


@app.post("/invoice/<int:inv_id>/approve")
def approve(inv_id: int):
    with db.get() as con:
        row = db.get_invoice(con, inv_id)
        if not row:
            abort(404)
        db.save_fields(con, inv_id, row["fields"], row["warnings"], "approved")
    return jsonify(ok=True, status="approved", msg="approved ✓")


@app.get("/invoice/<int:inv_id>/page/<int:n>.png")
def page_png(inv_id: int, n: int):
    import extract

    with db.get() as con:
        row = db.get_invoice(con, inv_id)
    if not row:
        abort(404)
    png = extract.render_page(row["path"], n)
    if not png:
        abort(404)
    out = PREVIEW_DIR / f"{inv_id}_{n}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    return send_file(out, mimetype="image/png")


@app.get("/status")
def status():
    with db.get() as con:
        n = con.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"]
        p = con.execute(
            "SELECT COUNT(*) c FROM invoices WHERE status='processing'"
        ).fetchone()["c"]
    return jsonify(total=n, processing=p, worker_alive=bool(_worker and _worker.is_alive()))


worker()  # start the background loop at import time
