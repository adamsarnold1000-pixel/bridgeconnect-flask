"""
TradingView → MT5 Bridge Server
Receives TradingView webhook alerts, queues them as pending trades,
and serves them to the MT5 EA via a polling API.
"""

import os
import time
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

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

# Control panel HTML is embedded here so the whole server is a single
# self-contained app.py (no templates/ folder needed for deployment).
PANEL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TV &rarr; MT5 Bridge Control Panel</title>
<style>
  :root {
    --bg: #0f1419; --bg2: #1a1f2e; --bg3: #252b3b;
    --text: #e0e6ed; --muted: #8899a6; --green: #17bf63;
    --red: #e0245e; --blue: #1da1f2; --orange: #ffad1f;
    --border: #38444d;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
         background: var(--bg); color: var(--text); padding: 20px; }
  h1 { font-size: 1.4em; margin-bottom: 4px; }
  h2 { font-size: 1.1em; margin-bottom: 10px; color: var(--blue); }
  .subtitle { color: var(--muted); font-size: 0.85em; margin-bottom: 20px; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }

  .card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card-full { grid-column: 1 / -1; }

  /* Status dots */
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
  .dot-green { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot-red { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .dot-orange { background: var(--orange); }

  /* Accounts table */
  table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; }

  /* Webhook URL */
  .url-box { background: var(--bg3); border: 1px solid var(--border); border-radius: 4px;
             padding: 10px 14px; font-family: monospace; font-size: 0.9em; word-break: break-all;
             display: flex; align-items: center; justify-content: space-between; margin: 8px 0; }
  .url-box button { margin-left: 10px; flex-shrink: 0; }

  /* Buttons */
  .btn { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer;
         font-size: 0.85em; font-weight: 600; color: #fff; }
  .btn-green { background: var(--green); }
  .btn-red { background: var(--red); }
  .btn-blue { background: var(--blue); }
  .btn-orange { background: var(--orange); color: #000; }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }

  /* Test form */
  .test-form { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-end; margin-top: 10px; }
  .test-form label { font-size: 0.8em; color: var(--muted); display: block; }
  .test-form input { background: var(--bg3); border: 1px solid var(--border); color: var(--text);
                     padding: 6px 8px; border-radius: 4px; width: 110px; font-size: 0.85em; }

  /* Log entries */
  .log-entry { font-size: 0.8em; padding: 4px 0; border-bottom: 1px solid var(--border);
               font-family: monospace; word-break: break-word; }
  .log-time { color: var(--muted); }
  .log-event { color: var(--blue); font-weight: 600; }
  .log-event-err { color: var(--red); }
  .log-scroll { max-height: 350px; overflow-y: auto; }

  /* Toast */
  .toast { position: fixed; bottom: 20px; right: 20px; background: var(--bg3);
           border: 1px solid var(--green); color: var(--green); padding: 10px 18px;
           border-radius: 6px; font-size: 0.85em; display: none; z-index: 999; }
  .toast.error { border-color: var(--red); color: var(--red); }

  .status-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.75em;
                  font-weight: 600; }
  .badge-ok { background: rgba(23,191,99,0.2); color: var(--green); }
  .badge-fail { background: rgba(224,36,94,0.2); color: var(--red); }
  .badge-pending { background: rgba(255,173,31,0.2); color: var(--orange); }
</style>
</head>
<body>

<h1>TV &rarr; MT5 Bridge</h1>
<p class="subtitle">Control Panel &middot; Server time: <span id="serverTime">{{ server_time }}</span></p>

<div class="grid">

  <!-- WEBHOOK URL -->
  <div class="card">
    <h2>Webhook URL</h2>
    <p style="font-size:0.8em;color:var(--muted);margin-bottom:6px;">
      Paste into TradingView alert &rarr; Webhook URL. Message: <code>{% raw %}{{strategy.order.alert_message}}{% endraw %}</code>
    </p>
    <div class="url-box">
      <span id="webhookUrl">Loading...</span>
      <button class="btn btn-blue" onclick="copyUrl()">Copy</button>
    </div>
  </div>

  <!-- MT5 ACCOUNTS -->
  <div class="card">
    <h2>MT5 Accounts</h2>
    <div id="accountsTable">
      <table>
        <thead><tr><th>Status</th><th>Account</th><th>Server</th><th>Balance</th><th>Type</th><th>Last Ping</th></tr></thead>
        <tbody id="accountsBody">
          {% if accounts %}
            {% for acc in accounts %}
            <tr>
              <td><span class="dot {{ 'dot-green' if acc.online else 'dot-red' }}"></span></td>
              <td>{{ acc.account_id }}</td>
              <td>{{ acc.server }}</td>
              <td>${{ "%.2f"|format(acc.balance) }}</td>
              <td>{{ acc.type }}</td>
              <td>{{ acc.ping_age_s }}s ago</td>
            </tr>
            {% endfor %}
          {% else %}
            <tr><td colspan="6" style="color:var(--muted)">No MT5 accounts connected yet</td></tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- TEST CONTROLS -->
  <div class="card card-full">
    <h2>Connection Test</h2>
    <p style="font-size:0.8em;color:var(--muted);margin-bottom:6px;">
      Send test signals to verify the EA receives and executes them.
    </p>
    <div class="test-form">
      <div>
        <label>Symbol</label>
        <input id="testSymbol" value="BTCUSD" />
      </div>
      <div>
        <label>Side</label>
        <select id="testSide" style="background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:6px;border-radius:4px;font-size:0.85em;">
          <option value="BUY">BUY (Long)</option>
          <option value="SELL">SELL (Short)</option>
        </select>
      </div>
      <div>
        <label>SL</label>
        <input id="testSL" value="0" type="number" step="0.01" />
      </div>
      <div>
        <label>TP</label>
        <input id="testTP" value="0" type="number" step="0.01" />
      </div>
      <div>
        <label>Lot Size</label>
        <input id="testSize" value="0.01" type="number" step="0.01" />
      </div>
      <button class="btn btn-green" onclick="sendTestEntry()">Send Test Entry</button>
      <button class="btn btn-red" onclick="sendTestClose()">Send Test Close</button>
    </div>
  </div>

  <!-- PENDING QUEUE -->
  <div class="card">
    <h2>Pending Queue</h2>
    <div id="queueInfo">
      {% if queue_counts %}
        {% for k, v in queue_counts.items() %}
          <p>{{ k }}: <strong>{{ v }}</strong> pending</p>
        {% endfor %}
      {% else %}
        <p style="color:var(--muted)">Queue empty</p>
      {% endif %}
    </div>
  </div>

  <!-- TRADE HISTORY -->
  <div class="card">
    <h2>Trade History</h2>
    <div class="log-scroll" id="historyLog">
      {% for h in history[:20] %}
      <div class="log-entry">
        <span class="log-time">{{ h.confirmed_at or h.time or '' }}</span>
        <span class="status-badge {{ 'badge-ok' if h.status == 'executed' else 'badge-fail' if h.status == 'failed' else 'badge-pending' }}">
          {{ h.status or h.event or '?' }}
        </span>
        Trade #{{ h.trade_id }} &middot; ticket={{ h.ticket_id or '-' }} &middot; P&L={{ h.profit or 0 }}
        {% if h.error_message %}<br><span style="color:var(--red)">{{ h.error_message }}</span>{% endif %}
      </div>
      {% endfor %}
      {% if not history %}
        <p style="color:var(--muted)">No trades confirmed yet</p>
      {% endif %}
    </div>
  </div>

  <!-- SIGNAL LOG -->
  <div class="card card-full">
    <h2>Signal Log</h2>
    <div class="log-scroll" id="signalLog">
      {% for log in logs %}
      <div class="log-entry">
        <span class="log-time">{{ log.time }}</span>
        <span class="log-event">{{ log.event }}</span>
        {{ log.data }}
      </div>
      {% endfor %}
      {% if not logs %}
        <p style="color:var(--muted)">No signals received yet</p>
      {% endif %}
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
// Auto-detect webhook URL
const loc = window.location;
const baseUrl = loc.protocol + '//' + loc.host;
document.getElementById('webhookUrl').textContent = baseUrl + '/webhook';

function copyUrl() {
  navigator.clipboard.writeText(baseUrl + '/webhook').then(() => showToast('Webhook URL copied!'));
}

function showToast(msg, isError) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast' + (isError ? ' error' : '');
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

async function sendTestEntry() {
  const body = {
    symbol: document.getElementById('testSymbol').value,
    side: document.getElementById('testSide').value,
    sl: parseFloat(document.getElementById('testSL').value) || 0,
    tp: parseFloat(document.getElementById('testTP').value) || 0,
    size: parseFloat(document.getElementById('testSize').value) || 0.01,
  };
  try {
    const r = await fetch('/api/test/entry', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    showToast('Test entry queued: ' + d.trade.action + ' ' + d.trade.symbol + ' (ID=' + d.trade.id + ')');
    refreshStatus();
  } catch(e) { showToast('Error: ' + e.message, true); }
}

async function sendTestClose() {
  const body = { symbol: document.getElementById('testSymbol').value };
  try {
    const r = await fetch('/api/test/close', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    showToast('Test CLOSE queued for ' + body.symbol + ' (ID=' + d.trade.id + ')');
    refreshStatus();
  } catch(e) { showToast('Error: ' + e.message, true); }
}

// Auto-refresh status every 3 seconds
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('serverTime').textContent = d.server_time;

    // Accounts
    let html = '';
    if (d.accounts.length === 0) {
      html = '<tr><td colspan="6" style="color:var(--muted)">No MT5 accounts connected yet</td></tr>';
    } else {
      d.accounts.forEach(a => {
        html += `<tr>
          <td><span class="dot ${a.online ? 'dot-green' : 'dot-red'}"></span></td>
          <td>${a.account_id}</td><td>${a.server}</td>
          <td>$${Number(a.balance).toFixed(2)}</td><td>${a.type}</td>
          <td>${a.ping_age_s}s ago</td></tr>`;
      });
    }
    document.getElementById('accountsBody').innerHTML = html;

    // Queue
    let qHtml = '';
    const qc = d.queue_counts;
    if (Object.keys(qc).length === 0) {
      qHtml = '<p style="color:var(--muted)">Queue empty</p>';
    } else {
      for (const [k,v] of Object.entries(qc)) {
        qHtml += `<p>${k}: <strong>${v}</strong> pending</p>`;
      }
    }
    document.getElementById('queueInfo').innerHTML = qHtml;

    // History
    let hHtml = '';
    d.history.forEach(h => {
      const cls = h.status === 'executed' ? 'badge-ok' : h.status === 'failed' ? 'badge-fail' : 'badge-pending';
      hHtml += `<div class="log-entry">
        <span class="log-time">${h.confirmed_at || h.time || ''}</span>
        <span class="status-badge ${cls}">${h.status || h.event || '?'}</span>
        Trade #${h.trade_id} &middot; ticket=${h.ticket_id || '-'} &middot; P&amp;L=${h.profit || 0}
        ${h.error_message ? '<br><span style="color:var(--red)">' + h.error_message + '</span>' : ''}
      </div>`;
    });
    document.getElementById('historyLog').innerHTML = hHtml || '<p style="color:var(--muted)">No trades confirmed yet</p>';

    // Logs
    let lHtml = '';
    d.logs.forEach(l => {
      lHtml += `<div class="log-entry">
        <span class="log-time">${l.time}</span>
        <span class="log-event">${l.event}</span>
        ${typeof l.data === 'object' ? JSON.stringify(l.data) : l.data}
      </div>`;
    });
    document.getElementById('signalLog').innerHTML = lHtml || '<p style="color:var(--muted)">No signals received yet</p>';

  } catch(e) { /* silent */ }
}

setInterval(refreshStatus, 3000);
</script>

</body>
</html>"""


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

    return render_template_string(
        PANEL_HTML,
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
