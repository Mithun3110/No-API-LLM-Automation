"""SQLite storage for the fake bank.

Plain SQL with the built-in sqlite3 module, no ORM, so every query is visible.
Money is stored as integer cents to avoid floating-point rounding errors.
The database is created and seeded from members.json the first time it is missing;
delete bank.db to start fresh.
"""

import json
import os
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

DATA_DIR = Path(__file__).parent / "data"
# BANK_DB_PATH lets tests run against a throwaway database instead of your bank.db.
DB_PATH = Path(os.environ.get("BANK_DB_PATH") or DATA_DIR / "bank.db")
SEED_PATH = DATA_DIR / "members.json"

SCHEMA = """
CREATE TABLE users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,          -- never store the plain password
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'teller'  -- teller sees masked data; admin sees full values
);

CREATE TABLE members (
    member_id  TEXT PRIMARY KEY,          -- 5-digit string; text keeps leading zeros
    name       TEXT NOT NULL,
    address    TEXT NOT NULL,
    phone      TEXT NOT NULL,
    restricted INTEGER NOT NULL DEFAULT 0 -- 1 = teller may not view (permission denied demo)
);

CREATE TABLE accounts (
    -- Internal id used in URLs and form values, so the full account number
    -- never reaches the page for users who should only see it masked.
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number TEXT NOT NULL UNIQUE,
    member_id      TEXT NOT NULL REFERENCES members(member_id),
    type           TEXT NOT NULL,         -- Savings, Checking, Money Market, Certificate of Deposit
    balance_cents  INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Open'  -- Open or Closed
);

CREATE TABLE transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number TEXT NOT NULL REFERENCES accounts(account_number),
    date           TEXT NOT NULL,         -- ISO date, sorts correctly as text
    description    TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL       -- negative = money out
);

-- One row per completed data-changing action, so every change has a reference number.
CREATE TABLE confirmations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  TEXT NOT NULL REFERENCES members(member_id),
    action     TEXT NOT NULL,             -- open_account, update_info, close_account, transfer
    details    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Start numbering at 100001 so confirmation numbers look like CNF-100001.
INSERT INTO sqlite_sequence (name, seq) VALUES ('confirmations', 100000);
"""


def to_cents(amount: float) -> int:
    # round() first: 0.1 + 0.2 style float noise must not lose a cent
    return int(round(amount * 100))


def connect() -> sqlite3.Connection:
    """Open a connection. Rows behave like dicts (row["name"])."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def is_initialized(conn: sqlite3.Connection) -> bool:
    # Check for the tables, not just the file: opening a missing path with the
    # sqlite3 CLI (or any client) silently creates an empty file.
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'members'").fetchone()
    return row is not None


def init_db() -> None:
    """Create and seed the database if it has not been set up yet."""
    conn = connect()
    if is_initialized(conn):
        conn.close()
        return
    seed = json.loads(SEED_PATH.read_text())
    try:
        with conn:  # one transaction: either fully seeded or not at all
            conn.executescript(SCHEMA)
            for user in seed["users"]:
                conn.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?)",
                    (user["username"], generate_password_hash(user["password"]), user["display_name"], user["role"]),
                )
            for m in seed["members"]:
                conn.execute(
                    "INSERT INTO members VALUES (?, ?, ?, ?, ?)",
                    (m["member_id"], m["name"], m["address"], m["phone"], int(m["restricted"])),
                )
                numbers_by_type = {}
                for a in m["accounts"]:
                    conn.execute(
                        "INSERT INTO accounts (account_number, member_id, type, balance_cents) VALUES (?, ?, ?, ?)",
                        (a["account_number"], m["member_id"], a["type"], to_cents(a["balance"])),
                    )
                    numbers_by_type[a["type"]] = a["account_number"]
                for t in m["transactions"]:
                    conn.execute(
                        "INSERT INTO transactions (account_number, date, description, amount_cents) VALUES (?, ?, ?, ?)",
                        (numbers_by_type[t["account_type"]], t["date"], t["description"], to_cents(t["amount"])),
                    )
    except Exception:
        conn.close()
        DB_PATH.unlink(missing_ok=True)  # don't leave a half-built file behind
        raise
    conn.close()
