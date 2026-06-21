# Empire Signals - Telegram relay (execution-decoupled)

This service ONLY posts TradingView alerts to Telegram. It does not place trades
and has no MT5 queue, so it cannot interfere with your copier/execution path.

## 1) Railway environment variables (Variables tab)

Set these (do NOT hardcode in code):

| Variable        | Value                                  |
|-----------------|----------------------------------------|
| `VIP_BOT_TOKEN` | token of your VIP bot (@BotFather)     |
| `VIP_CHAT_ID`   | numeric id of the VIP channel          |
| `REG_BOT_TOKEN` | token of your regular bot              |
| `REG_CHAT_ID`   | numeric id of the regular channel      |
| `RESULT_MODE`   | `pct` (default) \| `pips` \| `points` \| `dollar` |
| `PIP_SIZE`      | pip size for pips mode (default `0.0001`, use `0.01` for JPY) |
| `POINT_VALUE`   | multiplier for points mode (default `1`)  |

The bot for each channel must be an **admin** of that channel.

Find a channel's numeric id: add the bot as admin, post any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`
(channels look like `-100xxxxxxxxxx`).

## 2) Start command

`gunicorn app:app --bind 0.0.0.0:$PORT` (see `Procfile`).

## 3) TradingView alert (create a NEW alert, leave the copier alert alone)

- Condition: your strategy, **"Order fills only"**
- Webhook URL: `https://<your-railway-domain>/vip`  (or `/regular`)
- Message:

```
{"symbol":"{{ticker}}","action":"{{strategy.order.action}}","price":"{{strategy.order.price}}","contracts":"{{strategy.order.contracts}}","order_id":"{{strategy.order.id}}","pine":{{strategy.order.alert_message}}}
```

This gives the relay the fill price, symbol, side, size, and your SL/TP (from
the embedded Pine alert message) without editing your strategy.

## 4) Verify

- `GET /health` -> shows `channels_configured` true/false and open trade count.
- Test send:

```
curl -X POST https://<domain>/vip -H 'Content-Type: application/json' \
  -d '{"symbol":"BTCUSD","action":"buy","price":"65200","contracts":"0.1","order_id":"Empire Long","pine":{"event":"entry","side":"long","ticker":"BTCUSD","sl":"64800","tp":"66400"}}'
```

## Notes

- WIN/LOSS is computed by remembering the entry price, then comparing to the
  exit on close. If the service restarts between entry and close, that one trade
  posts `CLOSED` (no result). 
- Use one strategy per symbol per channel for accurate results.
