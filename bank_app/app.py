"""Harbor Point Credit Union: a deliberately legacy-style member services console.

This is the TARGET application our automation system drives. It is not part
of the automation system itself.
"""

import os
import random
import re
from decimal import Decimal, InvalidOperation
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session, url_for

from bank_app.data import MEMBERS

load_dotenv()

app = Flask(__name__)
# Random key generated at startup: sessions reset whenever the app restarts.
app.secret_key = os.urandom(32)

OPERATOR_USER = os.environ.get("BANK_USER")
OPERATOR_PASSWORD = os.environ.get("BANK_PASSWORD")
if not OPERATOR_USER or not OPERATOR_PASSWORD:
    raise RuntimeError("Set BANK_USER and BANK_PASSWORD in .env (see .env.example)")

MEMBER_ID_PATTERN = re.compile(r"^\d{5}$")

# Account types a member can open: (account-number code, minimum opening deposit)
ACCOUNT_TYPES = {
    "Share Savings": ("S", Decimal("25.00")),
    "Money Market": ("M", Decimal("1000.00")),
    "Holiday Club": ("H", Decimal("10.00")),
}


def login_required(view):
    """Send anyone who isn't signed in to the login page, remembering where they were going."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("operator"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def load_member(member_id):
    """Return (member, None) if the member can be viewed, or (None, error_page) if not."""
    member = MEMBERS.get(member_id)
    if member is None:
        message = f"No member found with ID {member_id}."
        return None, (render_template("search.html", message=message, member_id=member_id), 404)
    if member["restricted"]:
        return None, (render_template("restricted.html", member_id=member_id), 403)
    return member, None


def parse_amount(text):
    """Turn user text like '$1,000.50' into a Decimal, or None if it isn't a valid amount."""
    cleaned = text.replace("$", "").replace(",", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        return None  # rejects 'NaN', 'Infinity', and amounts with more than 2 decimals
    return amount.quantize(Decimal("0.01"))


def validate_new_account(form):
    """Return an error message, or None if the form is valid."""
    if form["type"] not in ACCOUNT_TYPES:
        return "Select an account type."
    amount = parse_amount(form["deposit"])
    if amount is None:
        return "Initial deposit must be a dollar amount, e.g. 100.00."
    minimum = ACCOUNT_TYPES[form["type"]][1]
    if amount < minimum:
        return f"Minimum opening deposit for {form['type']} is ${minimum:,.2f}."
    if len(form["nickname"]) > 20:
        return "Nickname must be 20 characters or fewer."
    return None


@app.route("/")
def index():
    return redirect(url_for("search"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (request.form.get("f1") == OPERATOR_USER
                and request.form.get("f2") == OPERATOR_PASSWORD):
            session["operator"] = OPERATOR_USER
            next_path = request.args.get("next", "")
            # Only allow local paths, so the redirect can't send someone to another site.
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = url_for("search")
            return redirect(next_path)
        error = "Invalid operator ID or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search", methods=["GET", "POST"])
@login_required
def search():
    message = None
    member_id = ""
    if request.method == "POST":
        member_id = request.form.get("f1", "").strip()
        if not MEMBER_ID_PATTERN.match(member_id):
            message = "Invalid Member ID. Enter exactly 5 digits."
        elif member_id not in MEMBERS:
            message = f"No member found with ID {member_id}."
        else:
            return redirect(url_for("member_detail", member_id=member_id))
    return render_template("search.html", message=message, member_id=member_id)


@app.route("/member/<member_id>")
@login_required
def member_detail(member_id):
    member, error_page = load_member(member_id)
    if error_page:
        return error_page
    return render_template("member.html", member_id=member_id, member=member)


@app.route("/member/<member_id>/open-account", methods=["GET", "POST"])
@login_required
def open_account(member_id):
    member, error_page = load_member(member_id)
    if error_page:
        return error_page

    error = None
    form = {"type": "", "deposit": "", "nickname": ""}
    if request.method == "POST":
        form = {
            "type": request.form.get("f1", ""),
            "deposit": request.form.get("f2", "").strip(),
            "nickname": request.form.get("f3", "").strip(),
        }
        error = validate_new_account(form)
        if error is None:
            # Nothing is created yet: the request waits in the session until confirmed.
            session["pending_account"] = {
                "member_id": member_id,
                "type": form["type"],
                "deposit": str(parse_amount(form["deposit"])),
                "nickname": form["nickname"],
            }
            return redirect(url_for("review_account", member_id=member_id))

    return render_template("open_account.html", member_id=member_id, member=member,
                           form=form, error=error, account_types=ACCOUNT_TYPES)


@app.route("/member/<member_id>/open-account/review", methods=["GET", "POST"])
@login_required
def review_account(member_id):
    member, error_page = load_member(member_id)
    if error_page:
        return error_page

    pending = session.get("pending_account")
    if not pending or pending["member_id"] != member_id:
        return render_template("message.html", title="No Pending Request",
                               message="There is no pending account request for this member.",
                               member_id=member_id), 409

    if request.method == "POST":
        if request.form.get("action") == "Confirm":
            # THE IRREVERSIBLE STEP: this actually creates the account.
            code = ACCOUNT_TYPES[pending["type"]][0]
            number = f"{member_id}-{code}{len(member['accounts']) + 20}"
            member["accounts"].append({
                "type": pending["type"],
                "number": number,
                "balance": Decimal(pending["deposit"]),
            })
            session.pop("pending_account", None)
            session["last_confirmation"] = {
                "member_id": member_id,
                "confirmation": f"HP{random.randint(100000, 999999)}",
                "number": number,
                "type": pending["type"],
                "deposit": pending["deposit"],
            }
            return redirect(url_for("account_opened", member_id=member_id))
        # Any other button (Cancel) throws the request away safely.
        session.pop("pending_account", None)
        return redirect(url_for("member_detail", member_id=member_id))

    deposit_display = f"{Decimal(pending['deposit']):,.2f}"
    return render_template("review_account.html", member_id=member_id, member=member,
                           pending=pending, deposit_display=deposit_display)


@app.route("/member/<member_id>/open-account/done")
@login_required
def account_opened(member_id):
    receipt = session.get("last_confirmation")
    if not receipt or receipt["member_id"] != member_id:
        return render_template("message.html", title="No Pending Request",
                               message="There is no completed account request to display.",
                               member_id=member_id), 409
    deposit_display = f"{Decimal(receipt['deposit']):,.2f}"
    return render_template("account_opened.html", member_id=member_id,
                           receipt=receipt, deposit_display=deposit_display)