
import os
import logging
import requests
from flask import Flask, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
AUDD_TOKEN = os.getenv("AUDD_API_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
TIMEOUT = 30


def telegram(method, payload=None):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(str(result))

    return result["result"]


def send_text(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    return telegram("sendMessage", payload)


def main_keyboard():
    return {
        "inline_keyboard": [[
            {
                "text": "🎵 Распознать музыку",
                "callback_data": "recognize"
            }
        ]]
    }


def recognize_audio(audio_bytes, filename="audio.mp3"):
    if not AUDD_TOKEN:
        return "Распознавание пока не настроено: добавь AUDD_API_TOKEN в Render."

    response = requests.post(
        "https://api.audd.io/",
        data={"api_token": AUDD_TOKEN, "return": "spotify"},
        files={"file": (filename, audio_bytes)},
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    track = data.get("result")

    if not track:
        return "Не удалось определить песню. Попробуй видео с более чистым звуком."

    title = track.get("title", "Неизвестный трек")
    artist = track.get("artist", "Неизвестный исполнитель")
    album = track.get("album")
    answer = f"🎵 {title}\n👤 Исполнитель: {artist}"
    if album:
        answer += f"\n💿 Альбом: {album}"
    if track.get("song_link"):
        answer += f"\n🔗 {track['song_link']}"
    return answer


def get_file_bytes(file_id):
    info = telegram("getFile", {"file_id": file_id})
    file_path = info["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content, file_path.rsplit("/", 1)[-1]


def handle_message(message):
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    text = (message.get("text") or message.get("caption") or "").strip()

    if text.startswith("/start"):
        send_text(
            chat_id,
            "Привет! 🎵 Отправь видео с музыкой или ссылку на видео, "
            "а затем нажми «Распознать музыку».\n\n"
            "Сейчас распознавание поддерживает видео, отправленные файлом. "
            "Обработка внешних ссылок пока ограничена.",
            main_keyboard(),
        )
        return

    if text.startswith("/help"):
        send_text(
            chat_id,
            "Отправь видео в чат и нажми «🎵 Распознать музыку»."
        )
        return

    video = message.get("video")
    document = message.get("document")
    audio = message.get("audio")

    media = video or audio
    if document:
        mime = document.get("mime_type", "")
        if mime.startswith("video/") or mime.startswith("audio/"):
            media = document

    if media:
        file_id = media["file_id"]
        # Store the file id as callback data through a short-lived in-memory map.
        PENDING[chat_id] = file_id
        send_text(
            chat_id,
            "Видео/аудио получено. Нажми кнопку ниже, чтобы распознать трек.",
            main_keyboard(),
        )
        return

    if text.startswith("http://") or text.startswith("https://"):
        send_text(
            chat_id,
            "Ссылку получил. Автоматическое скачивание видео по ссылкам "
            "TikTok/Instagram/YouTube пока не подключено. "
            "Пришли видео непосредственно в Telegram."
        )
        return

    send_text(
        chat_id,
        "Отправь видео или аудиофайл с музыкой.",
        main_keyboard(),
    )


PENDING = {}


def handle_callback(callback):
    callback_id = callback.get("id")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    data = callback.get("data")

    try:
        telegram("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Проверяю музыку..."
        })

        if data != "recognize" or not chat_id:
            return

        file_id = PENDING.get(chat_id)
        if not file_id:
            send_text(chat_id, "Сначала отправь видео или аудиофайл.")
            return

        send_text(chat_id, "⏳ Загружаю файл и ищу песню...")

        content, filename = get_file_bytes(file_id)
        result = recognize_audio(content, filename)
        send_text(chat_id, result)

    except Exception:
        logging.exception("Callback processing failed")
        if chat_id:
            try:
                send_text(chat_id, "Не удалось распознать файл. Проверь Logs в Render.")
            except Exception:
                logging.exception("Could not send error message")


@app.route("/", methods=["GET"])
def home():
    return "Music bot server is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    logging.info("Telegram update received: %s", data)

    try:
        if data.get("callback_query"):
            handle_callback(data["callback_query"])
        elif data.get("message"):
            handle_message(data["message"])
    except Exception:
        logging.exception("Webhook processing failed")
        return {"ok": False}, 500

    return {"ok": True}


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    if not TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is missing"}, 500

    webhook_url = request.host_url.rstrip("/") + "/webhook"
    try:
        result = telegram("setWebhook", {
            "url": webhook_url,
            "allowed_updates": ["message", "callback_query"],
        })
        return {"ok": True, "result": result, "url": webhook_url}
    except Exception as exc:
        logging.exception("Webhook registration failed")
        return {"ok": False, "error": str(exc)}, 500


@app.route("/webhook-info", methods=["GET"])
def webhook_info():
    try:
        result = telegram("getWebhookInfo")
        return {"ok": True, "result": result}
    except Exception as exc:
        logging.exception("Webhook info failed")
        return {"ok": False, "error": str(exc)}, 500
