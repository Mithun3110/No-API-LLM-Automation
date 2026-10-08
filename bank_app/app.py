"""Fake legacy bank back-office app ("CoreServ Teller").

This is the TARGET system the automation drives. It knows nothing about AI.
Server-rendered HTML in an old style (tables, <font>, inputs named f1/f2) so the
automation cannot rely on a clean DOM or test IDs.

Errors only happen when triggered on purpose, never at random, so demos repeat:
- member 99999 does not exist; member 77777 is restricted
- /_admin/inject?error=<name> makes the NEXT page load fail in a chosen way
"""

import os
import re
import secrets
import sqlite3
import threading
import time
from functools import wraps

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

import db

APP_NAME = "CoreServ Teller"
APP_VERSION = "4.2"
# Not 5000: macOS AirPlay Receiver uses it. BANK_PORT lets tests run a second copy.
PORT = int(os.environ.get("BANK_PORT", "5050"))

ACCOUNT_TYPES = ["Savings", "Checking", "Money Market", "Certificate of Deposit"]
PHONE_PATTERN = re.compile(r"^\d{3}-\d{3}-\d{4}$")

app = Flask(__name__)
# Random per start: sessions end when the app restarts, like a real server restart.
app.secret_key = os.environ.get("BANK_SECRET_KEY") or secrets.token_hex(32)


# ---------------------------------------------------------------- error injection
# One pending error at a time. It fires on the next page load, then clears.
INJECTABLE_ERRORS = {"popup", "slow_page", "session_expired", "server_error"}
SLOW_PAGE_SECONDS = 12  # longer than the replay wait (10s), so it counts as slow
_pending_error = os.environ.get("INJECT_ERROR") or None
_pending_lock = threading.Lock()  # dev server is threaded


def take_pending_error(kinds: set[str]) -> str | None:
    """Return and clear the pending error if it is one of `kinds`."""
    global _pending_error
    with _pending_lock:
        if _pending_error in kinds:
            error, _pending_error = _pending_error, None
            return error
    return None


@app.get("/_admin/inject")
def inject_error():
    """Test-only switch. Not part of the bank; kept out of the automation allowlist."""
    global _pending_error
    error = request.args.get("error", "")
    if error not in INJECTABLE_ERRORS | {"none"}:
        return jsonify(error=f"unknown error '{error}'", allowed=sorted(INJECTABLE_ERRORS)), 400
    with _pending_lock:
        _pending_error = None if error == "none" else error
    return jsonify(pending=_pending_error)


def is_bank_page() -> bool:
    # Injection should not hit static files, the admin switch, or the login page
    # (the login page is where session_expired sends you).
    return not (request.path.startswith(("/static", "/_admin")) or request.path == "/login")


@app.before_request
def apply_injected_error():
    if not is_bank_page():
        return None
    error = take_pending_error({"slow_page", "session_expired", "server_error"})
    if error == "slow_page":
        time.sleep(SLOW_PAGE_SECONDS)
    elif error == "session_expired":
        session.clear()
        return redirect(url_for("login", expired=1))
    elif error == "server_error":
        return render_template("error.html"), 500
    return None


@app.context_processor
def template_globals():
    # The popup is consumed when an HTML page is actually rendered, so a redirect
    # (e.g. search -> member page) does not swallow it.
    show_notice = is_bank_page() and take_pending_error({"popup"}) is not None
    return {"app_name": APP_NAME, "app_version": APP_VERSION, "show_notice": show_notice,
            "user": session.get("display_name"), "role": session.get("role")}


# ---------------------------------------------------------------- helpers
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.template_filter("money")
def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def is_admin() -> bool:
    return session.get("role") == "admin"


# Masking depends on who is signed in: tellers see partial values, admins see full ones.
# Done in one place (the filters) so no template can forget to check the role.
@app.template_filter("mask_phone")
def mask_phone(phone: str) -> str:
    return phone if is_admin() else "***-***-" + phone[-4:]


@app.template_filter("mask_account")
def mask_account(number: str) -> str:
    return number if is_admin() else "xxxx" + number[-4:]


def parse_amount(text: str) -> int | None:
    """Parse '1,250.00' or '$50' into cents. None if not a valid number."""
    cleaned = text.strip().replace(",", "").lstrip("$")
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", cleaned):
        return None
    return db.to_cents(float(cleaned))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def load_member(member_id: str):
    """Return (member, None) or (None, error_response) for not found / not authorized."""
    member = get_db().execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
    if member is None:
        return None, (render_template("search.html", error="No member found.", query=member_id), 404)
    if member["restricted"] and not is_admin():
        return None, (render_template("denied.html"), 403)
    return member, None


def open_accounts(member_id: str) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM accounts WHERE member_id = ? AND status = 'Open' ORDER BY id", (member_id,)
    ).fetchall()


def record_confirmation(conn: sqlite3.Connection, member_id: str, action: str, details: str) -> str:
    cur = conn.execute(
        "INSERT INTO confirmations (member_id, action, details) VALUES (?, ?, ?)", (member_id, action, details)
    )
    return f"CNF-{cur.lastrowid}"


def new_account_number(conn: sqlite3.Connection, member_id: str) -> str:
    count = conn.execute("SELECT COUNT(*) FROM accounts WHERE member_id = ?", (member_id,)).fetchone()[0]
    return f"{member_id}{count + 1:02d}{secrets.randbelow(900) + 100}"


# ---------------------------------------------------------------- login
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("f1", "").strip()
        password = request.form.get("f2", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            session["role"] = user["role"]
            return redirect(url_for("search"))
        error = "Invalid user ID or password."
    expired = request.args.get("expired") == "1"
    return render_template("login.html", error=error, expired=expired)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def home():
    return redirect(url_for("search"))


# ---------------------------------------------------------------- search
@app.get("/search")
@login_required
def search():
    query = request.args.get("f1", "").strip()
    if "f1" not in request.args:
        return render_template("search.html")
    if not re.fullmatch(r"\d{1,5}", query):
        return render_template("search.html", error="Member ID must be 1 to 5 digits.", query=query)
    if len(query) == 5:
        # Full ID: go straight to the member (load_member handles not found / restricted)
        return redirect(url_for("member_detail", member_id=query))
    return redirect(url_for("results", q=query))


@app.get("/members")
@login_required
def results():
    query = request.args.get("q", "").strip()
    rows = get_db().execute(
        "SELECT member_id, name FROM members WHERE member_id LIKE ? ORDER BY member_id", (query + "%",)
    ).fetchall() if query.isdigit() else []
    if not rows:
        return render_template("search.html", error="No member found.", query=query), 404
    return render_template("results.html", rows=rows, query=query)


# ---------------------------------------------------------------- member pages
@app.get("/member/<member_id>")
@login_required
def member_detail(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    accounts = get_db().execute(
        "SELECT * FROM accounts WHERE member_id = ? ORDER BY id", (member_id,)
    ).fetchall()
    by_type = {a["type"]: a for a in accounts if a["status"] == "Open"}
    return render_template("member.html", m=member, accounts=accounts,
                           savings=by_type.get("Savings"), checking=by_type.get("Checking"))


@app.get("/member/<member_id>/transactions")
@login_required
def transactions(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    rows = get_db().execute(
        """SELECT t.date, t.description, t.amount_cents, a.type, a.account_number
           FROM transactions t JOIN accounts a ON a.account_number = t.account_number
           WHERE a.member_id = ? ORDER BY t.date DESC, t.id DESC""",
        (member_id,),
    ).fetchall()
    return render_template("transactions.html", m=member, rows=rows)


# ---------------------------------------------------------------- open sub-account
def validate_open_account(member_id: str, account_type: str, deposit_text: str) -> tuple[list[str], int | None]:
    errors: list[str] = []
    if account_type not in ACCOUNT_TYPES:
        errors.append("Please select an account type.")
    deposit = parse_amount(deposit_text)
    if deposit is None or deposit <= 0:
        errors.append("Initial deposit must be a positive amount.")
    if account_type in {a["type"] for a in open_accounts(member_id)}:
        # Business rule, not a typing mistake: shown on its own line for clarity
        errors.append("This account already exists.")
    return errors, deposit


@app.route("/member/<member_id>/open-account", methods=["GET", "POST"])
@login_required
def open_account(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    form = {"type": request.form.get("f1", ""), "deposit": request.form.get("f2", "")}
    if request.method == "POST":
        errors, deposit = validate_open_account(member_id, form["type"], form["deposit"])
        if not errors:
            return render_template("open_account_confirm.html", m=member, type=form["type"], deposit=deposit)
        return render_template("open_account.html", m=member, types=ACCOUNT_TYPES, form=form, errors=errors), 400
    return render_template("open_account.html", m=member, types=ACCOUNT_TYPES, form=form, errors=[])


@app.post("/member/<member_id>/open-account/confirm")
@login_required
def open_account_confirm(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    account_type, deposit_text = request.form.get("f1", ""), request.form.get("f2", "")
    # Re-validate: never trust hidden fields from the confirmation page
    errors, deposit = validate_open_account(member_id, account_type, deposit_text)
    if errors:
        form = {"type": account_type, "deposit": deposit_text}
        return render_template("open_account.html", m=member, types=ACCOUNT_TYPES, form=form, errors=errors), 400
    conn = get_db()
    with conn:  # account, opening transaction and confirmation all succeed or none do
        number = new_account_number(conn, member_id)
        conn.execute(
            "INSERT INTO accounts (account_number, member_id, type, balance_cents) VALUES (?, ?, ?, ?)",
            (number, member_id, account_type, deposit),
        )
        conn.execute(
            "INSERT INTO transactions (account_number, date, description, amount_cents) VALUES (?, date('now'), ?, ?)",
            (number, "Opening Deposit", deposit),
        )
        conf = record_confirmation(conn, member_id, "open_account", f"{account_type} {number}")
    return render_template("success.html", m=member, confirmation=conf,
                           message=f"{account_type} account opened successfully.")


# ---------------------------------------------------------------- update member info
def validate_update(phone: str, address: str) -> list[str]:
    errors = []
    if not PHONE_PATTERN.fullmatch(phone.strip()):
        errors.append("Phone number must be in the format 555-555-0100.")
    if not address.strip():
        errors.append("Address is required.")
    return errors


@app.route("/member/<member_id>/update", methods=["GET", "POST"])
@login_required
def update_member(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    if request.method == "POST":
        form = {"phone": request.form.get("f1", ""), "address": request.form.get("f2", "")}
        errors = validate_update(form["phone"], form["address"])
        if not errors:
            return render_template("update_confirm.html", m=member, form=form)
        return render_template("update.html", m=member, form=form, errors=errors), 400
    form = {"phone": member["phone"], "address": member["address"]}
    return render_template("update.html", m=member, form=form, errors=[])


@app.post("/member/<member_id>/update/confirm")
@login_required
def update_member_confirm(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    form = {"phone": request.form.get("f1", "").strip(), "address": request.form.get("f2", "").strip()}
    errors = validate_update(form["phone"], form["address"])
    if errors:
        return render_template("update.html", m=member, form=form, errors=errors), 400
    conn = get_db()
    with conn:
        conn.execute("UPDATE members SET phone = ?, address = ? WHERE member_id = ?",
                     (form["phone"], form["address"], member_id))
        conf = record_confirmation(conn, member_id, "update_info", "phone/address")
    return render_template("success.html", m=member, confirmation=conf,
                           message="Member information updated successfully.")


# ---------------------------------------------------------------- close account
def find_open_account(member_id: str, account_id: int) -> sqlite3.Row:
    # member_id in the WHERE clause: an id from another member's account is a 404
    account = get_db().execute(
        "SELECT * FROM accounts WHERE id = ? AND member_id = ? AND status = 'Open'",
        (account_id, member_id),
    ).fetchone()
    if account is None:
        abort(404)
    return account


@app.route("/member/<member_id>/close/<int:account_id>", methods=["GET", "POST"])
@login_required
def close_account(member_id: str, account_id: int):
    member, error = load_member(member_id)
    if error:
        return error
    account = find_open_account(member_id, account_id)
    if request.method == "GET":
        return render_template("close_confirm.html", m=member, a=account, errors=[])
    if account["balance_cents"] != 0:
        errors = ["Account balance must be $0.00 before closing."]
        return render_template("close_confirm.html", m=member, a=account, errors=errors), 400
    conn = get_db()
    with conn:
        conn.execute("UPDATE accounts SET status = 'Closed' WHERE id = ?", (account_id,))
        conf = record_confirmation(conn, member_id, "close_account", account["account_number"])
    return render_template("success.html", m=member, confirmation=conf,
                           message=f"{account['type']} account closed successfully.")


# ---------------------------------------------------------------- transfer
def validate_transfer(member_id: str, from_id: str, to_id: str, amount_text: str):
    # Keyed by internal id as a string, because form values arrive as strings.
    # Only this member's open accounts are valid choices.
    accounts = {str(a["id"]): a for a in open_accounts(member_id)}
    errors: list[str] = []
    amount = parse_amount(amount_text)
    if from_id not in accounts or to_id not in accounts:
        errors.append("Please select both accounts.")
    elif from_id == to_id:
        errors.append("From and To accounts must be different.")
    if amount is None or amount <= 0:
        errors.append("Transfer amount must be a positive amount.")
    elif from_id in accounts and amount > accounts[from_id]["balance_cents"]:
        errors.append("Insufficient funds.")
    return errors, amount, accounts


@app.route("/member/<member_id>/transfer", methods=["GET", "POST"])
@login_required
def transfer(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    form = {"from": request.form.get("f1", ""), "to": request.form.get("f2", ""), "amount": request.form.get("f3", "")}
    if request.method == "POST":
        errors, amount, accounts = validate_transfer(member_id, form["from"], form["to"], form["amount"])
        if not errors:
            return render_template("transfer_confirm.html", m=member, amount=amount,
                                   src=accounts[form["from"]], dst=accounts[form["to"]])
        return render_template("transfer.html", m=member, accounts=open_accounts(member_id),
                               form=form, errors=errors), 400
    return render_template("transfer.html", m=member, accounts=open_accounts(member_id), form=form, errors=[])


@app.post("/member/<member_id>/transfer/confirm")
@login_required
def transfer_confirm(member_id: str):
    member, error = load_member(member_id)
    if error:
        return error
    form = {"from": request.form.get("f1", ""), "to": request.form.get("f2", ""), "amount": request.form.get("f3", "")}
    errors, amount, accounts = validate_transfer(member_id, form["from"], form["to"], form["amount"])
    if errors:
        return render_template("transfer.html", m=member, accounts=list(accounts.values()),
                               form=form, errors=errors), 400
    src, dst = accounts[form["from"]], accounts[form["to"]]
    conn = get_db()
    with conn:  # both balances move together or not at all
        conn.execute("UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ?", (amount, src["id"]))
        conn.execute("UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?", (amount, dst["id"]))
        conn.execute("INSERT INTO transactions (account_number, date, description, amount_cents) "
                     "VALUES (?, date('now'), 'Transfer Out', ?)", (src["account_number"], -amount))
        conn.execute("INSERT INTO transactions (account_number, date, description, amount_cents) "
                     "VALUES (?, date('now'), 'Transfer In', ?)", (dst["account_number"], amount))
        details = f"{src['account_number']}->{dst['account_number']} {amount}"
        conf = record_confirmation(conn, member_id, "transfer", details)
    return render_template("success.html", m=member, confirmation=conf,
                           message=f"Transfer of {money(amount)} completed successfully.")


# ---------------------------------------------------------------- errors
@app.errorhandler(404)
def not_found(_e):
    return render_template("message.html", title="Not Found", text="The requested page or record was not found."), 404


@app.errorhandler(500)
def server_error(_e):
    return render_template("error.html"), 500


if __name__ == "__main__":
    db.init_db()
    # threaded: a slow page must not freeze the whole app; no reloader: one process,
    # so the in-memory pending error and session key stay consistent.
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True, use_reloader=False)
