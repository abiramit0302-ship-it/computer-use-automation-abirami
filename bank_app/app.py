"""Harbor Point Credit Union: a deliberately legacy-style member services console.

This is the TARGET application our automation system drives. It is not part
of the automation system itself.
"""

import os
import re
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


def login_required(view):
    """Send anyone who isn't signed in to the login page, remembering where they were going."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("operator"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


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
    member = MEMBERS.get(member_id)
    if member is None:
        message = f"No member found with ID {member_id}."
        return render_template("search.html", message=message, member_id=member_id), 404
    if member["restricted"]:
        return render_template("restricted.html", member_id=member_id), 403
    return render_template("member.html", member_id=member_id, member=member)