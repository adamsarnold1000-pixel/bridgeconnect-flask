"""
Empire Signals - Telegram-only signal relay.

Decoupled from execution: this service ONLY formats TradingView alerts and
posts them to Telegram. It does NOT place trades and has no MT5 queue, so it
can never interfere with the copier / execution path.

Endpoints:
  POST /vip          -> posts to the VIP channel
  POST /regular      -> posts to the REGULAR (free) channel
  POST /signal/<id>  -> backward-compatible alias -> REGULAR channel
  GET  /             -> health text
  GET  /health       -> JSON health + how many open trades are being tracked

Secrets are read from environment variables (set these in Railway -> Variables):
  VIP_BOT_TOKEN, VIP_CHAT_ID, REG_BOT_TOKEN, REG_CHAT_ID
Optional:
  DRY_RUN = "1" to log instead of calling Telegram (for local testing)

Result format controlled by RESULT_MODE env var:
  pct    -> +2.35%  (percentage of entry price)
  pips   -> +50 pips (move / PIP_SIZE, default 0.0001)
  points -> +12.5 pts (raw price difference)
  dollar -> +$73.45  (move * contracts)
"""

import json
import os
import threading

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ----------------------------------------------------------------------------
# Config (from environment - never hardcode tokens)
# ----------------------------------------------------------------------------
CHANNELS = {
    "vip": {
        "token": os.environ.get("VIP_BOT_TOKEN", ""),
        "chat_id": os.environ.get("VIP_CHAT_ID", ""),
    },
    "regular": {
        "token": os.environ.get("REG_BOT_TOKEN", ""),
        "chat_id": os.environ.get("REG_CHAT_ID", ""),
    },
}

DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "True")
RESULT_MODE = os.environ.get("RESULT_MODE", "pct").lower().strip()
PIP_SIZE = float(os.environ.get("PIP_SIZE", "0.0001"))
POINT_VALUE = float(os.environ.get("POINT_VALUE", "1"))

# In-memory store of open trades so we can compute WIN/LOSS on close.
# Keyed by (channel, symbol). Note: if the service restarts between an entry
# and its close, that trade's result can't be computed (we post "CLOSED").
_OPEN = {}
_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _fmt(num):
    """Format a price/number cleanly (drop trailing zeros, max 5 dp)."""
    try:
        f = float(num)
    except (TypeError, ValueError):
        return str(num)
    s = f"{f:.5f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract(data):
    """Normalize a TradingView payload into a common dict.

    Supports two shapes:
      1) Wrapped (recommended):
         {"symbol","action","price","contracts","order_id","pine":{...}}
      2) Raw pine alert_message only:
         {"event","side","ticker","sl","tp"}
    """
    pine = data.get("pine")
    if not isinstance(pine, dict):
        pine = data

    event = (pine.get("event") or "").lower()
    order_id = (data.get("order_id") or "").lower()
    if not event:
        event = "close" if "exit" in order_id else "entry"

    side = (pine.get("side") or "").lower()
    if not side:
        action = (data.get("action") or "").lower()
        side = "long" if action == "buy" else "short" if action == "sell" else ""

    symbol = data.get("symbol") or pine.get("ticker") or "?"

    return {
        "event": event,
        "side": side,
        "symbol": symbol,
        "price": _to_float(data.get("price")),
        "contracts": _to_float(data.get("contracts")),
        "sl": pine.get("sl"),
        "tp": pine.get("tp"),
    }


def _result_text(side, entry, exit_price, contracts=1.0):
    """Return (emoji_word, detail_string) for a closed trade.

    Result format is controlled by the RESULT_MODE env var.
    """
    if entry is None or exit_price is None:
        return ("CLOSED", "")
    move = (exit_price - entry) if side == "long" else (entry - exit_price)
    win = move >= 0
    word = "✅ WIN" if win else "❌ LOSS"
    sign = "+" if move >= 0 else "-"

    if RESULT_MODE == "pct":
        pct = (move / entry) * 100 if entry != 0 else 0.0
        detail = f"{sign}{abs(pct):.2f}%"
    elif RESULT_MODE == "pips":
        pips = move / PIP_SIZE
        detail = f"{sign}{abs(pips):.1f} pips"
    elif RESULT_MODE == "points":
        pts = move * POINT_VALUE
        detail = f"{sign}{abs(pts):.2f} pts"
    else:  # dollar (legacy default)
        dollar = abs(move) * (contracts if contracts else 1.0)
        detail = f"{sign}${dollar:,.2f}"

    return (word, detail)


def build_message(data, channel):
    """Build the Telegram text and update open-trade state. Returns str."""
    s = _extract(data)
    symbol, side, event = s["symbol"], s["side"], s["event"]
    key = (channel, symbol)

    if event == "close":
        with _LOCK:
            rec = _OPEN.pop(key, None)
        entry = rec["entry"] if rec else None
        rside = rec["side"] if rec else side
        contracts = rec.get("contracts", 1.0) if rec else 1.0
        word, detail = _result_text(rside, entry, s["price"], contracts)
        tail = f" ({detail})" if detail else ""
        return f"{symbol} {word}{tail}"

    # entry — store side, entry price, and contracts for later close calc
    if s["price"] is not None:
        with _LOCK:
            _OPEN[key] = {
                "side": side,
                "entry": s["price"],
                "contracts": s["contracts"] or 1.0,
            }

    side_lbl = side.upper() if side else "?"
    lines = [f"🔔 *{symbol} {side_lbl}*"]
    if s["price"] is not None:
        lines.append(f"Entry: {_fmt(s['price'])}")
    if s["sl"] not in (None, ""):
        lines.append(f"SL: {_fmt(s['sl'])}")
    if s["tp"] not in (None, ""):
        lines.append(f"TP: {_fmt(s['tp'])}")
    if s["contracts"] not in (None, ""):
        lines.append(f"Size: {_fmt(s['contracts'])}")
    return "\n".join(lines)


def send_telegram(channel, msg):
    cfg = CHANNELS.get(channel, {})
    token, chat_id = cfg.get("token"), cfg.get("chat_id")
    if DRY_RUN:
        print(f"[DRY_RUN {channel}] -> {chat_id}\n{msg}")
        return 200, "dry_run"
    if not token or not chat_id:
        print(f"[CONFIG ERROR] missing token/chat_id for '{channel}'")
        return 500, "missing config"
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
        timeout=10,
    )
    print(f"[{channel.upper()} SENT] {msg!r} -> {r.status_code} {r.text}")
    return r.status_code, r.text


def _handle(channel):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "no JSON"}), 400
    try:
        msg = build_message(data, channel)
        code, _ = send_telegram(channel, msg)
        ok = 200 <= code < 300
        return jsonify({"status": "ok" if ok else "telegram_error", "sent": msg}), (
            200 if ok else 502
        )
    except Exception as exc:
        print(f"[ERROR] {exc} | payload={json.dumps(data)[:500]}")
        return jsonify({"status": "error", "message": str(exc)}), 400


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/vip", methods=["POST"])
def vip():
    return _handle("vip")


@app.route("/regular", methods=["POST"])
def regular():
    return _handle("regular")


@app.route("/signal/<client_id>", methods=["POST"])
def signal_alias(client_id):
    return _handle("regular")


@app.route("/", methods=["GET"])
def home():
    return "Empire Signals relay running", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "result_mode": RESULT_MODE,
            "pip_size": PIP_SIZE,
            "point_value": POINT_VALUE,
            "dry_run": DRY_RUN,
            "open_trades": len(_OPEN),
            "channels_configured": {
                c: bool(v.get("token") and v.get("chat_id"))
                for c, v in CHANNELS.items()
            },
        }
    ), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
