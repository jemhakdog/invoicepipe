"""SQLite schema + helpers. One file, WAL mode, stdlib sqlite3."""
import pathlib
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "var" / "invoicepipe.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,
    path         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'processing',
        -- processing | ready | needs_review | approved
    fields       TEXT NOT NULL DEFAULT '{}',   -- JSON: vendor, invoice_no, date, due, subtotal, tax, total, items[]
    warnings     TEXT NOT NULL DEFAULT '[]',   -- JSON: list[str]
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT
);
"""


def get() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init() -> None:
    with get() as con:
        con.executescript(SCHEMA)


def get_setting(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def add_invoice(con: sqlite3.Connection, filename: str, path: str) -> int:
    cur = con.execute(
        "INSERT INTO invoices(filename,path) VALUES(?,?)", (filename, path)
    )
    return cur.lastrowid


def get_invoice(con: sqlite3.Connection, inv_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()


def pending(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM invoices WHERE status='processing' ORDER BY id"
    ).fetchall()


def save_fields(con: sqlite3.Connection, inv_id: int, fields: str,
                warnings: str, status: str) -> None:
    con.execute(
        "UPDATE invoices SET fields=?, warnings=?, status=?, "
        "processed_at=datetime('now') WHERE id=?",
        (fields, warnings, status, inv_id),
    )


def list_invoices(con: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM invoices ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
