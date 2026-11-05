import asyncio
import logging
import os
from threading import Thread
from flask import Flask, request, jsonify
from flask_cors import CORS
from bot import main, get_subscription, cancel_subscription, PLANS

app = Flask(__name__)
CORS(app) # Разрешаем запросы с других доменов (с Netlify)

@app.route('/')
def health():
    return "API is running", 200

# --- API ДЛЯ MINI APP ---
@app.route('/api/subscription', methods=['POST'])
def api_sub():
    data = request.json
    user_id = data.get('userId')
    
    if not user_id:
        return jsonify({"status": "error", "message": "No user ID"}), 400
        
    sub = get_subscription(int(user_id))
    if not sub:
        return jsonify({"status": "none"})
    
    return jsonify({
        "status": "active",
        "plan": sub['plan'],
        "next_payment": sub['next_payment'],
        "id": sub['id']
    })

@app.route('/api/cancel', methods=['POST'])
def api_cancel():
    data = request.json
    sub_id = data.get('subId')
    
    if sub_id:
        cancel_subscription(int(sub_id))
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

# --- ЗАПУСК БОТА И СЕРВЕРА ---
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Запускаем Flask в фоне
    Thread(target=run_flask, daemon=True).start()
    # Запускаем бота
    asyncio.run(main())
