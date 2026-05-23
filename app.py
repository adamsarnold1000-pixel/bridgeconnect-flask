from flask import Flask, request, jsonify
from collections import defaultdict
import json
import requests

app = Flask(__name__) 

# ====================================================
# VIP TELEGRAM CONFIG
# ====================================================
VIP_BOT_TOKEN = "8851633323:AAEPBlRv2OZzfV4TlO-doGusmscXkSzK9b0"
VIP_CHAT_ID   = "-1003686680670"
# ====================================================
# REGULAR TELEGRAM CONFIG
# ====================================================
REG_BOT_TOKEN = "8765162338:AAHQ1sc7XEbn5xjf69vq95dMKyTnhbddphE"
REG_CHAT_ID   = "-1003821837087"
# ====================================================
# SIGNAL QUEUE STORAGE
# ====================================================
client_signals = defaultdict(list)
# ====================================================
# VIP TELEGRAM ENDPOINT
# ====================================================
@app.route('/vip', methods=['POST'])
def vip_signal():

    data = request.get_json()

    if not data:
        return jsonify({"status": "error"}), 400

    signal_type = data.get("type", "")

    if signal_type == "close":
        symbol = data.get("symbol", "BTCUSD")
        result = data.get("result", "")
        pnl    = data.get("pnl", "")

        msg = f"{symbol} {result} {pnl}"

    else:
        side   = data.get("side", "")
        symbol = data.get("symbol", "BTCUSD")
        entry  = data.get("entry", "")
        sl     = data.get("sl", "")
        tp     = data.get("tp", "")
        size   = data.get("size", "")

        msg = (
            f"🔔 *{symbol} {side}*\n"
            f"Entry: {entry}\n"
            f"SL: {sl}\n"
            f"TP: {tp}\n"
            f"Size: {size}"
        )

    requests.post(
        f"https://api.telegram.org/bot{VIP_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": VIP_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }
    )

    print(f"[VIP SENT] {msg}")

    return jsonify({"status": "ok"}), 200
# ====================================================
# RECEIVE SIGNAL
# ====================================================
@app.route('/signal/<client_id>', methods=['POST'])
def receive_signal(client_id):

    data = request.get_json()

    if not data:
        return jsonify({
            "status": "error",
            "message": "No JSON received"
        }), 400

    client_signals[client_id].append(data)

    signal_type = data.get("type", "")

    if signal_type == "close":
        symbol = data.get("symbol", "BTCUSD")
        result = data.get("result", "")
        pnl    = data.get("pnl", "")

        msg = f"{symbol} {result} {pnl}"

    else:
        side   = data.get("side", "")
        symbol = data.get("symbol", "BTCUSD")
        entry  = data.get("entry", "")
        sl     = data.get("sl", "")
        tp     = data.get("tp", "")
        size   = data.get("size", "")

        msg = (
            f"🔔 {symbol} {side}\n"
            f"Entry: {entry}\n"
            f"SL: {sl}\n"
            f"TP: {tp}\n"
            f"Size: {size}"
        )

    requests.post(
        f"https://api.telegram.org/bot{REG_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": REG_CHAT_ID,
            "text": msg
        }
    )

    print(f"[QUEUE ADD] Client={client_id}")
    print(f"[REGULAR SENT] {msg}")
    print(f"[SIGNAL] {data}")
    print(f"[QUEUE SIZE] {len(client_signals[client_id])}")

    return jsonify({
        "status": "ok",
        "queue_size": len(client_signals[client_id])
    }), 200
# ====================================================
# SEND NEXT SIGNAL TO MT5
# ====================================================
@app.route('/signal/<client_id>', methods=['GET'])
def get_signal(client_id):

    queue = client_signals.get(client_id, [])

    # EMPTY QUEUE
    if len(queue) == 0:
        return jsonify({}), 200

    # GET FIRST SIGNAL
    signal = queue.pop(0)

    print(f"[QUEUE SERVE] Client={client_id}")
    print(f"[SERVED SIGNAL] {signal}")
    print(f"[QUEUE REMAINING] {len(queue)}")

    return jsonify(signal), 200

# ====================================================

# ====================================================
# HOME
# ====================================================
@app.route('/', methods=['GET'])
def home():
    return "BridgeConnect Queue Flask Running", 200

# ====================================================
# RUN APP
# ====================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
