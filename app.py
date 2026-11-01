
import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AUDD_TOKEN = os.environ.get("AUDD_API_TOKEN", "")

def send_message(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20
    )

@app.get("/")
def health():
    return jsonify({"status": "ok", "bot": "Video Music Bot"})

@app.post("/webhook")
def webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if not chat_id:
        return jsonify({"ok": True})

    if text.startswith("/start"):
        send_message(
            chat_id,
            "Привет! 🎵 Отправь ссылку на TikTok, Instagram Reels "
            "или YouTube, и я помогу определить музыку."
        )
    elif text.startswith("http"):
        send_message(
            chat_id,
            "🔎 Ссылка получена! Следующим этапом подключим "
            "обработку видео и кнопку «Распознать музыку»."
        )
    else:
        send_message(chat_id, "Пришли ссылку на видео или напиши /start.")

    return jsonify({"ok": True})
