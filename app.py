
import os
import requests
from flask import Flask, request

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"


def send_message(chat_id, text):
    requests.post(
        f"{API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    ).raise_for_status()


@app.route("/", methods=["GET"])
def home():
    return "Music bot server is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    message = data.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if chat_id and text == "/start":
        send_message(
            chat_id,
            "Привет! 🎵 Я бот для распознавания музыки. "
            "Скоро смогу принимать видео и искать треки."
        )

    elif chat_id:
        send_message(
            chat_id,
            "Пока я проверяю подключение. Отправь /start."
        )

    return {"ok": True}


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    if not TOKEN:
        return {"ok": False, "error": "Missing bot token"}, 500

    base_url = request.host_url.rstrip("/")
    response = requests.post(
        f"{API}/setWebhook",
        json={"url": f"{base_url}/webhook"},
        timeout=15,
    )
    return response.json()


@app.route("/webhook-info", methods=["GET"])
def webhook_info():
    response = requests.get(
        f"{API}/getWebhookInfo",
        timeout=15,
    )
    return response.json()
