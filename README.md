# BridgeConnect — TradingView → MT5 Bridge

Copies trades from a TradingView strategy to MetaTrader 5, preserving the
**entry, stop-loss, take-profit, and the exact dynamic lot size** computed by
your Pine script. No Telegram — pure execution bridge.

```
TradingView ("BTC 5m" Pine)
   │  alert webhook  (POST, body = {{strategy.order.alert_message}})
   ▼
FLASK BRIDGE  (app.py)
   ├─ converts each alert into a "pending trade"
   └─ control panel web UI (status + connection tests)  →  http://<host>/
   ▲     │
   │     │  GET /api/trades/pending?account_id=...   (EA polls every 500 ms)
   │     ▼
MT5 EA  (mt5/TV_Copier.mq5)
   │  opens BUY/SELL with SL+TP, sizes the lot from the alert, closes on signal
   └─ POST /api/trades/confirm, /api/mt5/ping, /api/mt5/position-closed
```

## Components

| File | What it does |
|------|--------------|
| `pine/btc_5m.pine` | TradingView strategy. Fires JSON alerts with `event`, `side`, `ticker`, `sl`, `tp`, `size`. |
| `app.py` | Flask bridge. Ingests alerts, queues trades, serves the EA, and hosts the control panel UI (embedded in the file — no `templates/` folder needed). |
| `mt5/TV_Copier.mq5` | MetaTrader 5 Expert Advisor. Polls the bridge and executes trades. |

## How the lot size stays in sync

Your Pine computes `pos_qty` dynamically
(`pos_qty = sl_dist_dollars >= 1 ? math.max(math.min(math.round(max_risk / sl_dist_dollars, 2), 10), 0.01) : 0.01`).
That value is sent as `"size"` in the alert. The server maps it to
`risk_type:"lots"` + `risk_value:<size>`, and the EA trades **exactly** that
many lots — no recalculation on the MT5 side.

## Alert message format

Entry (long shown):
```json
{"event":"entry","side":"long","ticker":"BTCUSD","sl":"63000.0","tp":"63600.0","size":"1.0"}
```
Close:
```json
{"event":"close","side":"long","ticker":"BTCUSD"}
```

## Endpoints

| Method | Path | Used by | Purpose |
|--------|------|---------|---------|
| POST | `/webhook` | TradingView | Receive alert, queue a pending trade |
| POST | `/api/mt5/ping` | MT5 EA | Heartbeat / register account |
| GET | `/api/trades/pending?account_id=...` | MT5 EA | Fetch + clear pending trades |
| POST | `/api/trades/confirm` | MT5 EA | Report execution result |
| POST | `/api/mt5/position-closed` | MT5 EA | Report a manual close |
| GET | `/` | You | Control panel |
| GET | `/api/status` | Panel | Live JSON status (accounts, queue, logs) |
| POST | `/api/test/entry` · `/api/test/close` | Panel | Inject test signals |

## Running the server

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py            # dev, serves on 0.0.0.0:5000
# production:
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 30
```

> **Keep `--workers 1`.** The trade queue is in-memory; multiple workers would
> each hold a separate queue and the EA would miss trades.

- **Control panel:** `http://localhost:5000/`
- **Webhook URL (for TradingView):** `https://<your-public-host>/webhook`
- **EA poll URL:** `http://127.0.0.1:5000/api/trades/pending?account_id=...`

TradingView is cloud-based, so the server must be reachable from the internet
(Railway, a VPS, Cloudflare tunnel, ngrok, …). The MT5 EA can reach it either
locally (`127.0.0.1`) or via the same public URL.

## TradingView alert setup

1. Create an alert on the **BTC 5m** strategy, condition **"Order fills only"**.
2. **Webhook URL:** `https://<your-host>/webhook`
3. **Message:** `{{strategy.order.alert_message}}`

## MT5 EA setup

1. MetaEditor (F4) → new EA → paste `mt5/TV_Copier.mq5` → compile (F7).
2. Attach to a chart of the symbol you trade.
3. **Allow WebRequest:** Tools → Options → Expert Advisors → tick *Allow
   WebRequest for listed URL* and add your `ServerURL`.
4. Key inputs: `ServerURL`, `FallbackLots` (only used if TV sends `size:0`),
   `MagicNumber` (isolates these trades).

## Testing the connection

Open the control panel and use **Send Test Entry / Send Test Close**. The signal
is queued, the EA picks it up on its next poll, executes on the (demo) account,
and the result appears in **Trade History**. The **MT5 Accounts** panel shows a
green dot when the EA is online.
