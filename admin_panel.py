"""
admin_panel.py - Web admin panel for the Crash game bot (Flask).

Lets an operator, from a browser:
  - search/list players by user_id or username
  - view balance + play stats for any player
  - ADD points to a player by user_id
  - SET (force) a player's balance to an exact value
  - ZERO OUT ("reset") a player's balance

This panel talks to the SAME SQLite file the bot uses (db.py), so balance
changes take effect immediately for the bot. Run this as a separate process
from bot.py.

Run: python admin_panel.py
"""

import logging
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash

import config
import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("admin_panel")

app = Flask(__name__)
app.secret_key = config.ADMIN_PANEL_SECRET

db.init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == config.ADMIN_PANEL_USER and p == config.ADMIN_PANEL_PASS:
            session["logged_in"] = True
            log.info('{"event":"admin_login_success","user":%r}', u)
            return redirect(request.args.get("next") or url_for("dashboard"))
        log.warning('{"event":"admin_login_failed","user":%r,"ip":%r}', u, request.remote_addr)
        flash("بيانات دخول خاطئة")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    search = request.args.get("q", "").strip() or None
    page = max(int(request.args.get("page", 1)), 1)
    limit = 25
    users = db.list_users_sync(search, limit, (page - 1) * limit)
    return render_template("dashboard.html", users=users, search=search or "", page=page)


@app.route("/user/<int:user_id>")
@login_required
def user_detail(user_id):
    user = db.get_user_sync(user_id)
    stats = db.user_stats_sync(user_id)
    return render_template("user_detail.html", user=user, user_id=user_id, stats=stats)


@app.route("/user/<int:user_id>/add", methods=["POST"])
@login_required
def add_balance(user_id):
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        flash("قيمة غير صالحة")
        return redirect(url_for("user_detail", user_id=user_id))

    db.get_or_create_user_sync(user_id, None, 0)
    new_balance = db.adjust_balance_sync(user_id, amount, "admin_add")
    log.info('{"event":"admin_panel_add","target":%d,"amount":%f,"new_balance":%f}',
             user_id, amount, new_balance)
    flash(f"تمت الإضافة. الرصيد الجديد: {new_balance:.2f}")
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/user/<int:user_id>/set", methods=["POST"])
@login_required
def set_balance(user_id):
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        flash("قيمة غير صالحة")
        return redirect(url_for("user_detail", user_id=user_id))

    db.set_balance_sync(user_id, amount, "admin_set")
    log.info('{"event":"admin_panel_set","target":%d,"amount":%f}', user_id, amount)
    flash(f"تم تثبيت الرصيد على {amount:.2f}")
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/user/<int:user_id>/zero", methods=["POST"])
@login_required
def zero_balance(user_id):
    db.set_balance_sync(user_id, 0.0, "admin_reset")
    log.info('{"event":"admin_panel_zero","target":%d}', user_id)
    flash("تم تصفير الرصيد")
    return redirect(url_for("user_detail", user_id=user_id))


if __name__ == "__main__":
    # Dev server. For production, run behind gunicorn/uwsgi + nginx with HTTPS
    # and change ADMIN_PANEL_PASS / ADMIN_PANEL_SECRET in .env first.
    app.run(host="0.0.0.0", port=config.ADMIN_PANEL_PORT, debug=False)
