from flask import Flask, request, jsonify
from collections import defaultdict
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
# BUILD TELEGRAM MESSAGE
# ====================================================
def build_msg(data):
    signal_type = data.get("type", "")

    if signal_type == "close":
        symbol = data.get("symbol", "BTCUSD")
        result = data.get("result", "")
        pnl    = data.get("pnl", "")
        return f"{symbol} {result} {pnl}"

    side   = data.get("side", "")
    symbol = data.get("symbol", "BTCUSD")
    entry  = data.get("entry", "")
    sl     = data.get("sl", "")
    tp     = data.get("tp", "")
    size   = data.get("size", "")

    return (
        f"🔔 *{symbol} {side}*\n"
        f"Entry: {entry}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"Size: {size}"
    )

# ====================================================
# SEND TELEGRAM
# ====================================================
def send_telegram(bot_token, chat_id, msg, label):
    r = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown"
        }
    )

    print(f"[{label} SENT] {msg}")
    print(f"[{label} TELEGRAM RESPONSE] {r.status_code} {r.text}")

# ====================================================
# VIP TELEGRAM ENDPOINT
# ====================================================
@app.route('/vip', methods=['POST'])
def vip_signal():
    data = request.get_json()

    if not data:
        return jsonify({"status": "error"}), 400

    msg = build_msg(data)

    send_telegram(VIP_BOT_TOKEN, VIP_CHAT_ID, msg, "VIP")

    return jsonify({"status": "ok"}), 200

# ====================================================
# RECEIVE REGULAR SIGNAL
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

    msg = build_msg(data)

    send_telegram(REG_BOT_TOKEN, REG_CHAT_ID, msg, "REGULAR")

    print(f"[QUEUE ADD] Client={client_id}")
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

    if len(queue) == 0:
        return jsonify({}), 200

    signal = queue.pop(0)

    print(f"[QUEUE SERVE] Client={client_id}")
    print(f"[SERVED SIGNAL] {signal}")
    print(f"[QUEUE REMAINING] {len(queue)}")

    return jsonify(signal), 200

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
