"""Invoice extraction + math audit.

Pure (checks.py tests these, no IO):
    parse_money, norm_date, parse_fields, parse_items, audit, reconcile
IO:
    extract_text(path) -> str          pymupdf text layer
    render_page(path, n) -> bytes|None PNG of page n for the review split-view
    page_count(path) -> int

Contract: parse_fields() returns
    {"vendor": str, "invoice_no": str, "date": str, "due": str,
     "subtotal": float|None, "tax": float|None, "total": float|None,
     "items": [{"desc": str, "qty": float|None, "price": float|None, "amount": float}]}
Empty/garbage input -> empty strings/None, never an exception.
"""
import datetime
import json
import re

MONEY = r"-?\$?\s*\d[\d,]*\.\d{2}"
DATE = (r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}"
        r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})")

RE_SUB = re.compile(r"(?i)(sub\s*total|net\s*total|total\s+before\s+tax|taxable\s+total)")
RE_TAX = re.compile(r"(?i)\b(vat|gst|hst|sales\s+tax|tax)\b")
RE_TOTAL = re.compile(r"(?i)(?<![a-z])(grand\s+total|total\s+due|amount\s+due|balance\s+due"
                      r"|total(?:\s+amount)?(?:\s+payable)?)\b")
RE_DATE_LABELED = re.compile(
    rf"(?i)\b(?:invoice\s*date|(?<!due )date|issued(?:\s+on)?)\s*[:\-]?\s*({DATE})")
RE_DUE = re.compile(rf"(?i)\b(?:due\s*date|payment\s+due|pay\s+by|due\s+on)\s*[:\-]?\s*({DATE})")
RE_INVNO = re.compile(
    r"(?i)\b(?:invoice|inv|bill)\s*(?:#|no\.?|number|num\.?|:)\s*[:\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-/]{1,29})"
    r"|\b(INV[-\s]?\d{2,}[A-Za-z0-9\-/]*)"
)
RE_PO = re.compile(r"(?i)\b(?:po|purchase\s+order)\s*(?:#|no\.?|:)\s*([A-Za-z0-9\-]{2,29})")
RE_MONEY = re.compile(MONEY)
RE_PLAIN = re.compile(r"^-?\d+(?:\.\d+)?$")
RE_AMT = re.compile(r"^-?[\d,]+\.\d{1,2}$")          # an amount has cents
RE_ITEM_SKIP = re.compile(
    r"(?i)\b(total|subtotal|tax|vat|gst|invoice|balance|due|date|paid|deposit|rate|"
    r"discount|shipping|freight|description|qty|price|amount|terms|receipt|"
    r"usd|eur|gbp|number)\b"
)
SKIP_VENDOR = ("invoice", "bill to", "ship to", "receipt", "purchase order", "statement",
               "amount", "total", "date", "qty", "description")


# --- primitives ----------------------------------------------------------

def parse_money(tok: str) -> float | None:
    """'$1,234.56' -> 1234.56, '(12.00)' -> -12.0, junk -> None."""
    s = (tok or "").strip().replace("$", "").replace("USD", "").replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if not RE_PLAIN.match(s):
        return None
    v = float(s)
    return -v if neg else v


def norm_date(s: str) -> str:
    """Best-effort ISO date, input untouched if unparseable.
    Numeric D/M/Y vs M/D/Y: US order unless the first part > 12."""
    s = (s or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = "20" + y if len(y) == 2 else y
        if a > 12:                       # D/M/Y only when the first part can't be a month
            mo, d = b, a
        else:                            # M/D/Y (US default)
            mo, d = a, b
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y}-{mo:02d}-{d:02d}"
        return s
    for fmt in ("%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.datetime.strptime(s.replace(".", ""), fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _line_money(line: str) -> float | None:
    hits = RE_MONEY.findall(line)
    return parse_money(hits[-1]) if hits else None


# --- parsing -------------------------------------------------------------

def parse_fields(text: str) -> dict:
    f = {"vendor": "", "invoice_no": "", "date": "", "due": "",
         "subtotal": None, "tax": None, "total": None, "items": []}
    if not text or not text.strip():
        return f
    nonempty = [l.strip() for l in text.splitlines() if l.strip()]

    for ln in nonempty:                      # subtotal -> tax -> total, line by line
        if RE_SUB.search(ln):
            v = _line_money(ln)
            if v is not None and f["subtotal"] is None:
                f["subtotal"] = v
        elif RE_TAX.search(ln) and RE_MONEY.search(ln):
            v = _line_money(ln)
            if v is not None and f["tax"] is None:
                f["tax"] = v
        elif RE_TOTAL.search(ln):
            v = _line_money(ln)
            if v is not None:
                f["total"] = v              # last wins: grand total comes last

    for ln in nonempty:
        m = RE_DATE_LABELED.search(ln)
        if m and not f["date"]:            # first date wins (Due Date comes second)
            f["date"] = norm_date(m.group(1))
        m = RE_DUE.search(ln)
        if m and not f["due"]:
            f["due"] = norm_date(m.group(1))
    if not f["date"]:
        m = re.search(DATE, text)
        if m:
            f["date"] = norm_date(m.group())

    for ln in nonempty:                      # invoice # first, PO number only as fallback
        m = RE_INVNO.search(ln)
        if m and (m.group(1) or m.group(2)):
            f["invoice_no"] = (m.group(1) or m.group(2)).strip(" :-")
            break
    if not f["invoice_no"]:
        for ln in nonempty:
            m = RE_PO.search(ln)
            if m:
                f["invoice_no"] = m.group(1).strip(" :-")
                break

    for ln in nonempty:                      # vendor = first non-label line
        low = ln.lower()
        if any(k in low for k in SKIP_VENDOR):
            continue
        if RE_MONEY.fullmatch(ln) or re.match(r"^[\d\s\-/.:]+$", ln) or len(ln) < 2:
            continue
        f["vendor"] = ln
        break

    f["items"] = parse_items(text)
    return f


def parse_items(text: str) -> list[dict]:
    """Table walk: a row ending in an amount (with cents), preceded by a
    description, is a line item; the last number is what we audit against.
    3 numbers = qty/price/amount, 2 = price/amount, 1 = flat amount."""
    items = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or RE_ITEM_SKIP.search(ln):
            continue
        toks = ln.split()
        if not RE_AMT.match(toks[-1].strip("()")):
            continue
        nums: list[float] = []
        i = len(toks)
        while i > 0 and len(nums) < 3:      # qty/price/amount is the widest row
            v = parse_money(toks[i - 1])
            if v is None:
                break
            nums.insert(0, v)
            i -= 1
        desc = " ".join(toks[:i]).strip(" |:-")
        if not desc or len(desc) < 3 or not re.search(r"[A-Za-z]", desc):
            continue
        if len(nums) >= 3:
            qty, price, amount = nums[-3], nums[-2], nums[-1]
        elif len(nums) == 2:
            qty, price, amount = None, nums[-2], nums[-1]
        else:
            qty, price, amount = None, None, nums[0]
        items.append({"desc": desc, "qty": qty, "price": price, "amount": amount})
    return items


# --- audit ---------------------------------------------------------------

def audit(fields: dict, dup: str | None = None) -> list[str]:
    """The math check (BUILD §6). [] means clean -> status ready."""
    w = []
    items = fields.get("items") or []
    sub, tax, total = fields.get("subtotal"), fields.get("tax"), fields.get("total")

    if not items:
        w.append("no line items extracted — add them before approving")
    elif sub is not None:
        s = round(sum(i.get("amount") or 0 for i in items), 2)
        if abs(s - sub) > 0.01:
            w.append(f"line items sum {s:.2f} != subtotal {sub:.2f}")
    if sub is not None and tax is not None and total is not None:
        if abs(round(sub + tax, 2) - total) > 0.01:
            w.append(f"subtotal {sub:.2f} + tax {tax:.2f} = {round(sub + tax, 2):.2f} "
                     f"!= total {total:.2f}")
    elif sub is not None and total is not None and tax is None:
        if abs(sub - total) > 0.01:
            w.append(f"subtotal {sub:.2f} != total {total:.2f} (no tax line found)")

    for k in ("vendor", "invoice_no", "date", "total"):
        if not fields.get(k):
            w.append(f"missing {k}")
    if dup:
        w.append(f"duplicate: invoice # {fields.get('invoice_no')} already saved as #{dup}")
    return w


def reconcile(stored_json: str, body: dict) -> tuple[dict, list[str]]:
    """Merge human corrections from the review form, re-audit.
    Numbers go through parse_money so '1,234.56' and '' both work;
    items arrive as [{desc, amount}, ...] or ['desc | amount', ...] lines."""
    f = json.loads(stored_json or "{}") or {}
    for k in ("vendor", "invoice_no", "date", "due"):
        if k in body:
            f[k] = (body.get(k) or "").strip()
    for k in ("subtotal", "tax", "total"):
        if k in body:
            f[k] = parse_money(str(body.get(k) or ""))
    if "items" in body:
        items = []
        for it in body.get("items") or []:
            if isinstance(it, dict):
                desc, amt = (it.get("desc") or "").strip(), parse_money(str(it.get("amount", "")))
            else:
                desc, _, s = str(it).rpartition("|")
                desc, amt = desc.strip(), parse_money(s)
            if desc and amt is not None:
                items.append({"desc": desc, "qty": None, "price": None, "amount": amt})
        f["items"] = items
    for k in ("vendor", "invoice_no", "date", "due"):
        f.setdefault(k, "")
    for k in ("subtotal", "tax", "total"):
        f.setdefault(k, None)
    f.setdefault("items", [])
    return f, audit(f)


# --- IO ------------------------------------------------------------------

def extract_text(path: str) -> str:
    """PDF text layer; "" on failure. MVP does not OCR scanned images —
    an empty text layer becomes a needs_review warning in jobs.py."""
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return "\n".join(p.get_text() for p in doc)
    except Exception:
        return ""


def render_page(path: str, n: int) -> bytes | None:
    """PNG bytes of 1-based page n for the side-by-side review preview."""
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            if not 1 <= n <= doc.page_count:
                return None
            return doc[n - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes("png")
    except Exception:
        return None


def page_count(path: str) -> int:
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception:
        return 0
