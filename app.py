
import os
import logging
import threading
import requests

from flask import Flask, request, jsonify

# ---------------- CONFIG ----------------

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
AUDD_TOKEN = os.getenv("AUDD_API_TOKEN", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

MAX_FILE_SIZE = 20 * 1024 * 1024
REQUEST_TIMEOUT = 60

# Храним ожидающие файлы в памяти.
# После перезапуска Render файл нужно отправить повторно.
PENDING = {}
PENDING_LOCK = threading.Lock()


# ---------------- TELEGRAM ----------------

def telegram(method, payload=None):
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в Render")

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=20
    )

    try:
        data = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError("Telegram вернул некорректный ответ")

    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram API {method}: {data.get('description', response.status_code)}"
        )

    return data.get("result")


def send_text(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

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


# ---------------- MUSIC RECOGNITION ----------------

def recognize_audio(audio_bytes, filename):
    if not AUDD_TOKEN:
        raise RuntimeError("Не задан AUDD_API_TOKEN в Render")

    response = requests.post(
        "https://api.audd.io/",
        data={
            "api_token": AUDD_TOKEN,
            "return": "spotify,apple_music"
        },
        files={
            "file": (
                filename,
                audio_bytes,
                "application/octet-stream"
            )
        },
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()
    data = response.json()

    if data.get("status") != "success":
        logging.error("AudD error: %s", data)
        return (
            "❌ Сервис распознавания не смог обработать файл.\n"
            "Попробуй другое видео или аудиофайл."
        )

    result = data.get("result")

    if not result:
        return (
            "🔎 Не удалось определить песню.\n"
            "Попробуй отправить видео с более чётким звуком."
        )

    title = result.get("title") or "Неизвестное название"
    artist = result.get("artist") or "Неизвестный исполнитель"

    lines = [
        "🎵 Музыка найдена!",
        "",
        f"🎶 Название: {title}",
        f"🎤 Исполнитель: {artist}"
    ]

    album = result.get("album")
    if album:
        lines.append(f"💿 Альбом: {album}")

    release_date = result.get("release_date")
    if release_date:
        lines.append(f"📅 Дата выхода: {release_date}")

    spotify = result.get("spotify") or {}
    spotify_url = spotify.get("external_urls", {}).get("spotify")
    if spotify_url:
        lines.append(f"🟢 Spotify: {spotify_url}")

    apple_music = result.get("apple_music") or {}
    apple_url = apple_music.get("url")
    if apple_url:
        lines.append(f"🍎 Apple Music: {apple_url}")

    return "\n".join(lines)


# ---------------- DOWNLOAD FROM TELEGRAM ----------------

def get_file_bytes(file_id):
    result = telegram("getFile", {"file_id": file_id})
    file_path = result.get("file_path")

    if not file_path:
        raise RuntimeError("Telegram не вернул путь к файлу")

    file_size = result.get("file_size", 0)

    if file_size and file_size > MAX_FILE_SIZE:
        raise RuntimeError("Файл превышает допустимый размер 20 МБ")

    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    content = response.content

    if len(content) > MAX_FILE_SIZE:
        raise RuntimeError("Файл превышает допустимый размер 20 МБ")

    filename = file_path.rsplit("/", 1)[-1] or "audio.bin"

    return content, filename


# ---------------- MESSAGE HANDLER ----------------

def handle_message(message):
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if not chat_id:
        return

    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        send_text(
            chat_id,
            "Привет! 🎵 Я помогу найти музыку в видео или аудио.\n\n"
            "1. Отправь видео, аудио или музыкальный файл прямо в чат.\n"
            "2. Дождись сообщения о принятом файле.\n"
            "3. Нажми кнопку «🎵 Распознать музыку» под этим сообщением.\n\n"
            "⚠️ Ссылки на видео пока не обрабатываются — отправляй сам файл.",
            reply_markup=main_keyboard()
        )
        return

    if text.startswith("/help"):
        send_text(
            chat_id,
            "🎵 Как распознать музыку:\n\n"
            "1. Отправь видео, аудио или документ с музыкой.\n"
            "2. Нажми кнопку под ответом о принятом файле.\n"
            "3. Дождись названия песни и исполнителя.\n\n"
            "Если не получилось, попробуй файл меньшего размера."
        )
        return

    media = (
        message.get("video")
        or message.get("audio")
        or message.get("voice")
        or message.get("document")
        or message.get("animation")
    )

    if not media:
        if text:
            send_text(
                chat_id,
                "Отправь видео или аудиофайл как файл или медиа, "
                "а затем нажми кнопку распознавания."
            )
        return

    file_id = media.get("file_id")
    file_size = media.get("file_size", 0)

    if not file_id:
        send_text(chat_id, "❌ Не удалось получить файл. Отправь его ещё раз.")
        return

    if file_size and file_size > MAX_FILE_SIZE:
        send_text(
            chat_id,
            "❌ Файл слишком большой. Максимальный размер — 20 МБ."
        )
        return

    with PENDING_LOCK:
        PENDING[chat_id] = file_id

    send_text(
        chat_id,
        "✅ Файл получен!\n\n"
        "Нажми кнопку ниже, и я попробую определить песню.",
        reply_markup=main_keyboard()
    )


# ---------------- BUTTON HANDLER ----------------

def handle_callback(callback):
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    data = callback.get("data")

    # Если Telegram не смог подтвердить нажатие,
    # продолжаем обработку кнопки.
    try:
        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": "Проверяю музыку..."
            }
        )
    except Exception:
        logging.exception("answerCallbackQuery failed")

    if data != "recognize" or not chat_id:
        return

    with PENDING_LOCK:
        file_id = PENDING.get(chat_id)

    if not file_id:
        send_text(
            chat_id,
            "Сначала отправь видео или аудиофайл отдельным сообщением. "
            "Затем нажми кнопку под ответом «Файл получен!»."
        )
        return

    send_text(chat_id, "⏳ Загружаю файл и ищу песню...")

    try:
        content, filename = get_file_bytes(file_id)
        result = recognize_audio(content, filename)
        send_text(chat_id, result)

        with PENDING_LOCK:
            PENDING.pop(chat_id, None)

    except Exception as error:
        logging.exception("Music recognition failed: %s", error)

        error_text = str(error).lower()

        if "20 мб" in error_text or "too big" in error_text:
            message_text = (
                "❌ Файл слишком большой. Попробуй файл размером до 20 МБ."
            )
        elif "audd_api_token" in error_text:
            message_text = (
                "❌ Не настроен AudD. Проверь переменную AUDD_API_TOKEN "
                "в Render → Environment."
            )
        else:
            message_text = (
                "❌ Не удалось распознать файл.\n\n"
                "Проверь, что AUDD_API_TOKEN в Render правильный, "
                "и попробуй другое видео или аудио."
            )

        try:
            send_text(chat_id, message_text)
        except Exception:
            logging.exception("Could not send error message")


# ---------------- FLASK ROUTES ----------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "service": "Telegram Music Recognition Bot"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    logging.info(
        "Telegram update received: %s",
        {
            "update_id": update.get("update_id"),
            "has_message": "message" in update,
            "has_callback": "callback_query" in update
        }
    )

    try:
        if "message" in update:
            handle_message(update["message"])

        elif "callback_query" in update:
            handle_callback(update["callback_query"])

    except Exception:
        logging.exception("Webhook processing failed")

    # Telegram должен получить HTTP 200, чтобы не повторять update.
    return jsonify({"ok": True}), 200


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    if not TOKEN:
        return jsonify({
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN is not configured"
        }), 500

    base_url = os.getenv(
        "RENDER_EXTERNAL_URL",
        "https://video-music-bot.onrender.com"
    ).rstrip("/")

    try:
        result = telegram(
            "setWebhook",
            {"url": f"{base_url}/webhook"}
        )
        return jsonify({"ok": True, "result": result})

    except Exception:
        logging.exception("Could not set webhook")
        return jsonify({
            "ok": False,
            "error": "Не удалось установить webhook. Проверь Render Logs."
        }), 500


@app.route("/webhook-info", methods=["GET"])
def webhook_info():
    try:
        result = telegram("getWebhookInfo")
        # Не возвращаем URL с токеном или секретные данные.
        return jsonify({
            "ok": True,
            "url": result.get("url"),
            "pending_update_count": result.get("pending_update_count"),
            "last_error_date": result.get("last_error_date"),
            "last_error_message": result.get("last_error_message")
        })

    except Exception:
        logging.exception("Could not get webhook info")
        return jsonify({
            "ok": False,
            "error": "Не удалось получить статус webhook"
        }), 500


# ---------------- START ----------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
