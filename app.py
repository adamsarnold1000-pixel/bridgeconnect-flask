"""
TradingView → MT5 Bridge Server
Receives TradingView webhook alerts, queues them as pending trades,
and serves them to the MT5 EA via a polling API.
"""

import os
import time
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ============================================================
# IN-MEMORY STATE
# ============================================================

# Auto-incrementing trade ID
_trade_id_lock = threading.Lock()
_next_trade_id = 1

# Pending trades keyed by account_id → list of trade dicts
# When no specific account mapping exists, trades go to "__global__"
# and any account polling gets them.
pending_trades: dict[str, list[dict]] = {}
pending_lock = threading.Lock()

# Connected MT5 accounts: account_id → last ping info
mt5_accounts: dict[str, dict] = {}
accounts_lock = threading.Lock()

# Signal log (most recent N entries for the control panel)
MAX_LOG = 200
signal_log: list[dict] = []
log_lock = threading.Lock()

# Trade history (confirmed trades)
trade_history: list[dict] = []
history_lock = threading.Lock()


def _next_id() -> int:
    global _next_trade_id
    with _trade_id_lock:
        tid = _next_trade_id
        _next_trade_id += 1
        return tid


def _log(event: str, data: dict | str):
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "data": data,
    }
    with log_lock:
        signal_log.insert(0, entry)
        if len(signal_log) > MAX_LOG:
            signal_log.pop()
    print(f"[{entry['time']}] {event}: {data}")


# ============================================================
# TRADINGVIEW WEBHOOK  (entry point for alerts)
# ============================================================

def _map_alert_to_trade(data: dict) -> dict:
    """
    Convert a TradingView alert_message JSON into a pending trade
    object that the MT5 EA understands.

    TV sends:
      entry → {"event":"entry","side":"long","ticker":"BTCUSD","sl":"...","tp":"...","size":"..."}
      close → {"event":"close","side":"long","ticker":"BTCUSD"}

    EA expects:
      {"id":N,"symbol":"BTCUSD","action":"BUY","sl":63000.0,"tp":63600.0,
       "price":0,"risk_type":"lots","risk_value":1.0}
    """
    event = (data.get("event") or "").strip().upper()
    side = (data.get("side") or "").strip().upper()
    ticker = data.get("ticker") or data.get("symbol") or ""

    # Strip exchange prefix (e.g. "BINANCE:BTCUSDT" → "BTCUSDT")
    if ":" in ticker:
        ticker = ticker.split(":", 1)[1]

    trade: dict = {
        "id": _next_id(),
        "symbol": ticker,
        "action": "",
        "sl": _to_float(data.get("sl", 0)),
        "tp": _to_float(data.get("tp", 0)),
        "price": _to_float(data.get("price", 0)),
        "risk_type": "lots",
        "risk_value": _to_float(data.get("size", 0)),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if event == "ENTRY" or event == "BUY" or event == "SELL":
        if side == "LONG" or side == "BUY" or event == "BUY":
            trade["action"] = "BUY"
        elif side == "SHORT" or side == "SELL" or event == "SELL":
            trade["action"] = "SELL"
        else:
            trade["action"] = "BUY"  # default
    elif event == "CLOSE":
        trade["action"] = "CLOSE"
    elif event == "CLOSE_PARTIAL":
        trade["action"] = "CLOSE_PARTIAL"
    else:
        # Try to infer from side
        if side in ("LONG", "BUY"):
            trade["action"] = "BUY"
        elif side in ("SHORT", "SELL"):
            trade["action"] = "SELL"
        else:
            trade["action"] = event or "UNKNOWN"

    return trade


def _to_float(val) -> float:
    """Safely convert a value to float."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    """
    Receives TradingView webhook alerts.
    The TradingView alert message box should contain:
        {{strategy.order.alert_message}}
    And the webhook URL should point to:
        https://<your-domain>/webhook
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        # Try form-encoded or raw text (TradingView sometimes sends plain text)
        raw = request.get_data(as_text=True)
        if raw:
            try:
                import json
                data = json.loads(raw)
            except Exception:
                _log("WEBHOOK_ERROR", f"Could not parse body: {raw[:200]}")
                return jsonify({"status": "error", "message": "Invalid JSON"}), 400
        else:
            return jsonify({"status": "error", "message": "Empty body"}), 400

    _log("WEBHOOK_RECV", data)

    trade = _map_alert_to_trade(data)

    if trade["action"] == "UNKNOWN":
        _log("WEBHOOK_SKIP", f"Unknown action for alert: {data}")
        return jsonify({"status": "error", "message": "Unknown event/side"}), 400

    # Queue the trade for all connected accounts (broadcast)
    with pending_lock:
        # If specific accounts are connected, queue for each
        with accounts_lock:
            account_ids = list(mt5_accounts.keys()) if mt5_accounts else ["__global__"]

        for aid in account_ids:
            if aid not in pending_trades:
                pending_trades[aid] = []
            # Each account gets its own copy (with unique ID for tracking)
            if len(account_ids) > 1 and aid != account_ids[0]:
                trade_copy = dict(trade)
                trade_copy["id"] = _next_id()
                pending_trades[aid].append(trade_copy)
            else:
                pending_trades[aid].append(trade)

    _log("TRADE_QUEUED", {
        "id": trade["id"],
        "action": trade["action"],
        "symbol": trade["symbol"],
        "sl": trade["sl"],
        "tp": trade["tp"],
        "size": trade["risk_value"],
        "accounts": account_ids,
    })

    return jsonify({
        "status": "ok",
        "trade_id": trade["id"],
        "action": trade["action"],
        "symbol": trade["symbol"],
    }), 200


# ============================================================
# MT5 EA API
# ============================================================

@app.route("/api/mt5/ping", methods=["POST"])
def mt5_ping():
    """
    EA heartbeat — registers/updates the MT5 account.
    Body: {account_id, name, server, balance, type, symbol}
    """
    data = request.get_json(force=True, silent=True) or {}
    account_id = str(data.get("account_id", ""))
    if not account_id:
        return jsonify({"status": "error", "message": "No account_id"}), 400

    now = datetime.now(timezone.utc)
    with accounts_lock:
        mt5_accounts[account_id] = {
            "account_id": account_id,
            "name": data.get("name", ""),
            "server": data.get("server", ""),
            "balance": _to_float(data.get("balance", 0)),
            "type": data.get("type", ""),
            "symbol": data.get("symbol", ""),
            "last_ping": now.isoformat(timespec="seconds"),
            "last_ping_ts": now.timestamp(),
        }

    # Migrate any __global__ pending trades to this account
    with pending_lock:
        if "__global__" in pending_trades and pending_trades["__global__"]:
            if account_id not in pending_trades:
                pending_trades[account_id] = []
            pending_trades[account_id].extend(pending_trades.pop("__global__"))

    return jsonify({"status": "ok"}), 200


@app.route("/api/trades/pending", methods=["GET"])
def get_pending_trades():
    """
    EA polls this to get trades to execute.
    Query: ?account_id=12345
    Returns: {"trades": [{id, symbol, action, sl, tp, price, risk_type, risk_value}, ...]}
    """
    account_id = request.args.get("account_id", "")

    with pending_lock:
        # Try account-specific queue, then global fallback
        trades = pending_trades.get(account_id, [])
        if not trades and not account_id:
            trades = pending_trades.get("__global__", [])

        if not trades:
            return jsonify({"trades": []}), 200

        # Return all pending trades and clear the queue
        result = list(trades)
        trades.clear()

    _log("TRADES_SERVED", {
        "account_id": account_id,
        "count": len(result),
        "trade_ids": [t["id"] for t in result],
    })

    return jsonify({"trades": result}), 200


@app.route("/api/trades/confirm", methods=["POST"])
def confirm_trade():
    """
    EA confirms trade execution.
    Body: {id, status, profit, ticket_id, error_message?}
    """
    data = request.get_json(force=True, silent=True) or {}
    trade_id = data.get("id", 0)
    status = data.get("status", "")
    profit = _to_float(data.get("profit", 0))
    ticket_id = data.get("ticket_id", 0)
    error_msg = data.get("error_message", "")

    entry = {
        "trade_id": trade_id,
        "status": status,
        "profit": profit,
        "ticket_id": ticket_id,
        "error_message": error_msg,
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with history_lock:
        trade_history.insert(0, entry)
        if len(trade_history) > MAX_LOG:
            trade_history.pop()

    _log("TRADE_CONFIRMED", entry)

    return jsonify({"status": "ok"}), 200


@app.route("/api/mt5/position-closed", methods=["POST"])
def position_closed():
    """
    EA reports a position was closed manually (not via TradingView).
    Body: {trade_id, ticket_id, profit, account_id}
    """
    data = request.get_json(force=True, silent=True) or {}

    entry = {
        "trade_id": data.get("trade_id", 0),
        "ticket_id": data.get("ticket_id", 0),
        "profit": _to_float(data.get("profit", 0)),
        "account_id": data.get("account_id", ""),
        "event": "manual_close",
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with history_lock:
        trade_history.insert(0, entry)
        if len(trade_history) > MAX_LOG:
            trade_history.pop()

    _log("MANUAL_CLOSE", entry)

    return jsonify({"status": "ok"}), 200


# ============================================================
# CONTROL PANEL
# ============================================================

@app.route("/", methods=["GET"])
def control_panel():
    """Render the web control panel."""
    now_ts = datetime.now(timezone.utc).timestamp()

    with accounts_lock:
        accounts = []
        for acc in mt5_accounts.values():
            acc_view = dict(acc)
            age = now_ts - acc.get("last_ping_ts", 0)
            acc_view["online"] = age < 15  # online if pinged within 15s
            acc_view["ping_age_s"] = round(age, 1)
            accounts.append(acc_view)

    with log_lock:
        logs = list(signal_log[:50])

    with history_lock:
        history = list(trade_history[:50])

    with pending_lock:
        queue_counts = {k: len(v) for k, v in pending_trades.items() if v}

    return render_template(
        "panel.html",
        accounts=accounts,
        logs=logs,
        history=history,
        queue_counts=queue_counts,
        server_time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


@app.route("/api/test/entry", methods=["POST"])
def test_entry():
    """Inject a test BUY trade into the queue (for connection testing)."""
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol", "BTCUSD")
    side = data.get("side", "BUY")
    sl = _to_float(data.get("sl", 0))
    tp = _to_float(data.get("tp", 0))
    size = _to_float(data.get("size", 0.01))

    trade = {
        "id": _next_id(),
        "symbol": symbol,
        "action": side.upper(),
        "sl": sl,
        "tp": tp,
        "price": 0,
        "risk_type": "lots",
        "risk_value": size,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_test": True,
    }

    with pending_lock:
        with accounts_lock:
            account_ids = list(mt5_accounts.keys()) if mt5_accounts else ["__global__"]
        for aid in account_ids:
            if aid not in pending_trades:
                pending_trades[aid] = []
            pending_trades[aid].append(dict(trade))

    _log("TEST_ENTRY", trade)
    return jsonify({"status": "ok", "trade": trade}), 200


@app.route("/api/test/close", methods=["POST"])
def test_close():
    """Inject a test CLOSE trade into the queue."""
    data = request.get_json(force=True, silent=True) or {}
    symbol = data.get("symbol", "BTCUSD")

    trade = {
        "id": _next_id(),
        "symbol": symbol,
        "action": "CLOSE",
        "sl": 0,
        "tp": 0,
        "price": 0,
        "risk_type": "",
        "risk_value": 0,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_test": True,
    }

    with pending_lock:
        with accounts_lock:
            account_ids = list(mt5_accounts.keys()) if mt5_accounts else ["__global__"]
        for aid in account_ids:
            if aid not in pending_trades:
                pending_trades[aid] = []
            pending_trades[aid].append(dict(trade))

    _log("TEST_CLOSE", trade)
    return jsonify({"status": "ok", "trade": trade}), 200


@app.route("/api/status", methods=["GET"])
def api_status():
    """API endpoint for control panel AJAX polling."""
    now_ts = datetime.now(timezone.utc).timestamp()

    with accounts_lock:
        accounts = []
        for acc in mt5_accounts.values():
            acc_view = dict(acc)
            age = now_ts - acc.get("last_ping_ts", 0)
            acc_view["online"] = age < 15
            acc_view["ping_age_s"] = round(age, 1)
            accounts.append(acc_view)

    with log_lock:
        logs = list(signal_log[:30])

    with history_lock:
        history = list(trade_history[:30])

    with pending_lock:
        queue_counts = {k: len(v) for k, v in pending_trades.items() if v}

    return jsonify({
        "accounts": accounts,
        "logs": logs,
        "history": history,
        "queue_counts": queue_counts,
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }), 200


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n=== TradingView → MT5 Bridge Server ===")
    print(f"  Webhook URL:   http://0.0.0.0:{port}/webhook")
    print(f"  Control Panel: http://0.0.0.0:{port}/")
    print(f"  EA Poll URL:   http://127.0.0.1:{port}/api/trades/pending?account_id=YOUR_ID")
    print(f"========================================\n")
    app.run(host="0.0.0.0", port=port, debug=False)
