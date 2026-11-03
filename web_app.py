import asyncio
import logging
import os
from threading import Thread
from flask import Flask
from bot import main

app = Flask(__name__)

@app.route('/')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
