"""
webapp_api.py - Backend for the Telegram Mini App (the "web ينفتح" button).

Serves:
  GET  /                     -> webapp/index.html (the Mini App page)
  GET  /api/balance          -> current balance for the calling Telegram user
  POST /api/bet              -> place a bet, opens a server-side round
  GET  /api/round/<id>       -> poll current multiplier / busted state
  POST /api/round/<id>/cashout -> lock in winnings

SECURITY: every request must carry Telegram's `initData` string (sent
automatically by the Mini App via `Telegram.WebApp.initData`). We verify its
HMAC signature against BOT_TOKEN before trusting the user_id inside it -
never trust a user_id the client sends directly. This is the standard
Telegram Mini App auth pattern.

Run: python webapp_api.py   (separate process from bot.py and admin_panel.py)
"""
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
  return "Crash Game Web is Online!"


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=5000)

  
import hashlib
import hmac
import json
import logging
import math
import time
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify, send_from_directory

import config
import db
import game_logic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webapp_api")

app = Flask(__name__, static_folder="webapp", static_url_path="")
db.init_db()

_round_counter_lock = 0  # kept for symmetry with bot.py; DB autoincrement is the real counter


# ---------------------------------------------------------------- auth

def validate_init_data(init_data: str, max_age_seconds: int = 86400):
    """
    Verifies Telegram Mini App initData per Telegram's documented algorithm:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Returns the parsed user dict on success, or None if invalid/expired.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    auth_date = pairs.get("auth_date")
    if auth_date and (time.time() - int(auth_date)) > max_age_seconds:
        return None  # stale initData - reject (replay protection)

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        log.warning('{"event":"initdata_hmac_mismatch"}')
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None


def require_user():
    """Pulls initData from header or JSON body, validates it, returns user dict or None."""
    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data and request.is_json:
        init_data = request.json.get("initData")
    return validate_init_data(init_data)


# ---------------------------------------------------------------- static page

@app.route("/")
def index():
    return send_from_directory("webapp", "index.html")


# ---------------------------------------------------------------- API

@app.route("/api/balance")
def api_balance():
    user = require_user()
    if not user:
        return jsonify(error="unauthorized"), 401
    row = db.get_or_create_user_sync(user["id"], user.get("username"), config.STARTING_BALANCE)
    return jsonify(balance=row["balance"], currency=config.CURRENCY_NAME)


@app.route("/api/bet", methods=["POST"])
def api_bet():
    user = require_user()
    if not user:
        return jsonify(error="unauthorized"), 401
    user_id = user["id"]

    if db.get_active_round_by_user_sync(user_id):
        return jsonify(error="round_in_progress"), 409

    body = request.get_json(silent=True) or {}
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify(error="invalid_amount"), 400

    if amount < config.MIN_BET or amount > config.MAX_BET:
        return jsonify(error="amount_out_of_range", min=config.MIN_BET, max=config.MAX_BET), 400

    row = db.get_or_create_user_sync(user_id, user.get("username"), config.STARTING_BALANCE)
    if row["balance"] < amount:
        return jsonify(error="insufficient_balance", balance=row["balance"]), 400

    new_balance = db.adjust_balance_sync(user_id, -amount, "bet")

    server_seed = game_logic.generate_server_seed()
    start_ts = time.time()
    # round_id is only known after insert, but crash_point needs *a* round_id as
    # the HMAC nonce - insert first with a placeholder, then patch. Simpler: use
    # start_ts (high precision) as the nonce instead, which is unique per round.
    round_id = db.create_active_round_sync(user_id, amount, 0.0, server_seed, start_ts)
    crash_point = game_logic.crash_point_from_seed(server_seed, round_id)
    db._conn.execute("UPDATE active_rounds SET crash_point=? WHERE round_id=?",
                      (crash_point, round_id))
    db._conn.commit()

    log.info('{"event":"bet_placed","user":%d,"amount":%f,"round_id":%d,"new_balance":%f}',
             user_id, amount, round_id, new_balance)

    return jsonify(round_id=round_id, balance=new_balance, start_ts=start_ts)


@app.route("/api/round/<int:round_id>")
def api_round_status(round_id):
    user = require_user()
    if not user:
        return jsonify(error="unauthorized"), 401

    r = db.get_active_round_sync(round_id)
    if not r or r["user_id"] != user["id"]:
        return jsonify(error="not_found"), 404

    if r["status"] != "active":
        return jsonify(status=r["status"])

    elapsed = time.time() - r["start_ts"]
    current = round(math.exp(game_logic.GROWTH_RATE * elapsed * 1000), 2)

    if current >= r["crash_point"]:
        db.resolve_active_round_sync(round_id, "busted")
        db.record_round_sync(user["id"], r["bet_amount"], r["crash_point"], None, 0.0)
        db.delete_active_round_sync(round_id)
        log.info('{"event":"round_busted","user":%d,"round_id":%d,"crash_point":%f}',
                 user["id"], round_id, r["crash_point"])
        return jsonify(status="busted", crash_point=r["crash_point"])

    return jsonify(status="active", multiplier=current)


@app.route("/api/round/<int:round_id>/cashout", methods=["POST"])
def api_cashout(round_id):
    user = require_user()
    if not user:
        return jsonify(error="unauthorized"), 401

    r = db.get_active_round_sync(round_id)
    if not r or r["user_id"] != user["id"]:
        return jsonify(error="not_found"), 404
    if r["status"] != "active":
        return jsonify(error="already_resolved", status=r["status"]), 409

    elapsed = time.time() - r["start_ts"]
    current = round(math.exp(game_logic.GROWTH_RATE * elapsed * 1000), 2)

    if current >= r["crash_point"]:
        db.resolve_active_round_sync(round_id, "busted")
        db.record_round_sync(user["id"], r["bet_amount"], r["crash_point"], None, 0.0)
        db.delete_active_round_sync(round_id)
        return jsonify(status="busted", crash_point=r["crash_point"]), 200

    payout = round(r["bet_amount"] * current, 2)
    new_balance = db.adjust_balance_sync(user["id"], payout, "cashout")
    db.resolve_active_round_sync(round_id, "cashed")
    db.record_round_sync(user["id"], r["bet_amount"], r["crash_point"], current, payout)
    db.delete_active_round_sync(round_id)

    log.info('{"event":"cashout","user":%d,"round_id":%d,"multiplier":%f,"payout":%f}',
             user["id"], round_id, current, payout)

    return jsonify(status="cashed", multiplier=current, payout=payout, balance=new_balance)


if __name__ == "__main__":
    import os as _os
    # Render (and most PaaS) inject the real port via $PORT - must bind to it,
    # WEBAPP_API_PORT from config.py is only the local-dev fallback.
    port = int(_os.environ.get("PORT", config.WEBAPP_API_PORT))
    app.run(host="0.0.0.0", port=port, debug=False)
