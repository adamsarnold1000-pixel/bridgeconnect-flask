from flask import Flask, request, jsonify
import json
import requests

app = Flask(__name__)

# VIP Telegram config
VIP_BOT_TOKEN = "8851633323:AAEPBlRv2OZzfV4TlO-doGusmscXkSzK9b0"
VIP_CHAT_ID   = "-1003686680670"

client_signals = {}

@app.route('/signal/<client_id>', methods=['POST'])
def receive_signal(client_id):
    data = request.get_json()
    if data:
        client_signals[client_id] = data
        print(f"Signal received for client {client_id}: {data}")
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error"}), 400

@app.route('/signal/<client_id>', methods=['GET'])
def get_signal(client_id):
    signal = client_signals.get(client_id)
    if not signal:
        return jsonify({}), 200
    client_signals[client_id] = {}
    print(f"Signal served and cleared for client {client_id}: {signal}")
    return jsonify(signal), 200

# ====================================================
# VIP TELEGRAM ENDPOINT
# ====================================================
@app.route('/vip', methods=['POST'])
def vip_signal():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error"}), 400
    side   = data.get("side", "")
    symbol = data.get("symbol", "BTCUSD")
    entry  = data.get("entry", "")
    sl     = data.get("sl", "")
    tp     = data.get("tp", "")
    size   = data.get("size", "")
    risk   = data.get("risk", "")
    msg = (f"🔔 *{symbol} {side}*\n"
           f"Entry: {entry}\n"
           f"SL: {sl}\n"
           f"TP: {tp}")
    requests.post(
        f"https://api.telegram.org/bot{VIP_BOT_TOKEN}/sendMessage",
        json={"chat_id": VIP_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    )
    return jsonify({"status": "ok"}), 200

@app.route('/', methods=['GET'])
def home():
    return "BridgeConnect Universal Flask Running", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
