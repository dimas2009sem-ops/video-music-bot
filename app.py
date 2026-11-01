
import os
import re
import logging
import tempfile
import threading
import subprocess
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
AUDD_TOKEN = os.getenv("AUDD_API_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_AUDIO_SIZE = 15 * 1024 * 1024
DOWNLOAD_TIMEOUT = 180

PENDING = {}
PENDING_LOCK = threading.Lock()
BUSY = set()
BUSY_LOCK = threading.Lock()


def telegram(method, payload=None):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не настроен")

    r = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=25
    )
    data = r.json()

    if not r.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram {method}: {data.get('description', r.status_code)}"
        )

    return data.get("result")


def send_text(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return telegram("sendMessage", payload)


def keyboard():
    return {
        "inline_keyboard": [[
            {"text": "🎵 Распознать музыку", "callback_data": "recognize"}
        ]]
    }


def recognize_audio(audio_bytes, filename):
    if not AUDD_TOKEN:
        raise RuntimeError("AUDD_API_TOKEN не настроен в Render")

    r = requests.post(
        "https://api.audd.io/",
        data={
            "api_token": AUDD_TOKEN,
            "return": "spotify,apple_music"
        },
        files={
            "file": (filename, audio_bytes, "application/octet-stream")
        },
        timeout=90
    )
    r.raise_for_status()
    data = r.json()

    if data.get("status") != "success":
        logging.error("AudD response: %s", data)
        return "❌ AudD не смог обработать звук. Попробуй другой файл."

    result = data.get("result")
    if not result:
        return (
            "🔎 Песня не найдена. Попробуй другое видео "
            "или фрагмент, где музыка слышна отчётливо."
        )

    lines = [
        "🎵 Музыка найдена!",
        "",
        f"🎶 Название: {result.get('title', 'Неизвестно')}",
        f"🎤 Исполнитель: {result.get('artist', 'Неизвестен')}"
    ]

    if result.get("album"):
        lines.append(f"💿 Альбом: {result['album']}")
    if result.get("release_date"):
        lines.append(f"📅 Дата выхода: {result['release_date']}")

    spotify = result.get("spotify") or {}
    spotify_url = (spotify.get("external_urls") or {}).get("spotify")
    if spotify_url:
        lines.append(f"🟢 Spotify: {spotify_url}")

    apple = result.get("apple_music") or {}
    if apple.get("url"):
        lines.append(f"🍎 Apple Music: {apple['url']}")

    return "\n".join(lines)


def download_telegram_file(file_id):
    info = telegram("getFile", {"file_id": file_id})
    path = info.get("file_path")

    if not path:
        raise RuntimeError("Telegram не предоставил путь к файлу")

    if info.get("file_size", 0) > MAX_FILE_SIZE:
        raise RuntimeError("Файл больше 20 МБ")

    r = requests.get(
        f"https://api.telegram.org/file/bot{TOKEN}/{path}",
        timeout=60
    )
    r.raise_for_status()

    if len(r.content) > MAX_FILE_SIZE:
        raise RuntimeError("Файл больше 20 МБ")

    return r.content, os.path.basename(path) or "audio.bin"


def validate_video_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Пришли полную ссылку, начинающуюся с https://")

    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
        or host == "tiktok.com"
        or host.endswith(".tiktok.com")
        or host == "instagram.com"
        or host.endswith(".instagram.com")
        or host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fb.watch"
        or host == "x.com"
        or host.endswith(".x.com")
        or host == "twitter.com"
        or host.endswith(".twitter.com")
        or host == "vimeo.com"
        or host.endswith(".vimeo.com")
    )

    if not allowed:
        raise ValueError(
            "Пока поддерживаются ссылки YouTube, TikTok, Instagram, "
            "Facebook, X/Twitter и Vimeo."
        )

    return url


def download_audio_from_url(url):
    validate_video_url(url)

    # Загружаем только аудио. Ограничиваем размер и время обработки.
    with tempfile.TemporaryDirectory() as tmp:
        output_template = os.path.join(tmp, "audio.%(ext)s")

        command = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "20",
            "--retries", "1",
            "--max-filesize", "100M",
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "5",
            "-o", output_template,
            url
        ]

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=DOWNLOAD_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "Скачивание заняло слишком много времени. Попробуй другую ссылку."
            )

        if process.returncode != 0:
            logging.warning("yt-dlp failed: %s", process.stderr[-2500:])
            raise RuntimeError(
                "Не получилось скачать видео по этой ссылке. "
                "Возможно, оно приватное, сайт требует авторизацию "
                "или ссылка больше не работает."
            )

        files = [
            os.path.join(tmp, name)
            for name in os.listdir(tmp)
            if os.path.isfile(os.path.join(tmp, name))
        ]

        if not files:
            raise RuntimeError("Загрузчик не создал аудиофайл")

        audio_path = max(files, key=os.path.getsize)
        size = os.path.getsize(audio_path)

        if size == 0:
            raise RuntimeError("Получился пустой аудиофайл")

        if size > MAX_AUDIO_SIZE:
            raise RuntimeError(
                "Аудио слишком длинное или большое. Попробуй короткий ролик."
            )

        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        return audio_bytes, "music.mp3"


def process_file(chat_id, file_id):
    send_text(chat_id, "⏳ Загружаю файл и распознаю музыку...")
    content, filename = download_telegram_file(file_id)
    send_text(chat_id, recognize_audio(content, filename))


def process_url(chat_id, url):
    send_text(
        chat_id,
        "⏳ Получил ссылку. Скачиваю аудио из видео — это может занять минуту..."
    )
    content, filename = download_audio_from_url(url)
    send_text(chat_id, recognize_audio(content, filename))


def run_job(chat_id, job, value):
    with BUSY_LOCK:
        if chat_id in BUSY:
            send_text(
                chat_id,
                "⌛ Я уже обрабатываю твой предыдущий запрос. Подожди немного."
            )
            return
        BUSY.add(chat_id)

    try:
        if job == "url":
            process_url(chat_id, value)
        else:
            process_file(chat_id, value)

    except Exception as e:
        logging.exception("Recognition job failed")

        message = str(e)
        if "AUDD_API_TOKEN" in message:
            answer = (
                "❌ Не настроен AUDD_API_TOKEN. "
                "Проверь переменные в Render → Environment."
            )
        elif "20 МБ" in message or "слишком длинное" in message:
            answer = f"❌ {message}"
        elif "поддерживаются ссылки" in message or "https://" in message:
            answer = f"❌ {message}"
        else:
            answer = (
                "❌ Не удалось распознать музыку.\n\n"
                "Проверь доступность ссылки и настройки AudD. "
                "Если ошибка повторится, посмотри Render Logs."
            )

        try:
            send_text(chat_id, answer)
        except Exception:
            logging.exception("Failed to send error to Telegram")

    finally:
        with BUSY_LOCK:
            BUSY.discard(chat_id)


def handle_message(message):
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return

    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        send_text(
            chat_id,
            "Привет! 🎵 Я ищу музыку в видео.\n\n"
            "Можно:\n"
            "• отправить видео или аудио как файл;\n"
            "• прислать публичную ссылку на YouTube, TikTok, "
            "Instagram и некоторые другие платформы.\n\n"
            "Отправь файл или ссылку и нажми кнопку распознавания.",
            reply_markup=keyboard()
        )
        return

    if text.startswith("/help"):
        send_text(
            chat_id,
            "🎵 Отправь видео/аудиофайл или публичную ссылку на видео. "
            "Затем нажми «Распознать музыку» под ответом бота."
        )
        return

    # Ссылку обрабатываем как запрос, а не как файл.
    urls = re.findall(r"https?://[^\s<>]+", text)
    if urls:
        url = urls[0].rstrip(".,!?)]}»")
        try:
            validate_video_url(url)
        except ValueError as e:
            send_text(chat_id, f"❌ {e}")
            return

        with PENDING_LOCK:
            PENDING[chat_id] = {"type": "url", "value": url}

        send_text(
            chat_id,
            "✅ Ссылка получена!\n\n"
            "Нажми кнопку, чтобы скачать звук и найти песню.",
            reply_markup=keyboard()
        )
        return

    media = (
        message.get("video")
        or message.get("audio")
        or message.get("voice")
        or message.get("document")
        or message.get("animation")
        or message.get("video_note")
    )

    if media:
        file_id = media.get("file_id")
        size = media.get("file_size", 0)

        if not file_id:
            send_text(chat_id, "❌ Не удалось получить файл. Отправь его ещё раз.")
            return

        if size and size > MAX_FILE_SIZE:
            send_text(chat_id, "❌ Файл слишком большой. Максимум 20 МБ.")
            return

        with PENDING_LOCK:
            PENDING[chat_id] = {"type": "file", "value": file_id}

        send_text(
            chat_id,
            "✅ Файл получен! Нажми кнопку ниже для распознавания.",
            reply_markup=keyboard()
        )
        return

    if text:
        send_text(
            chat_id,
            "Отправь видео, аудио или ссылку на видео с поддерживаемого сайта."
        )


def handle_callback(callback):
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    try:
        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": "Начинаю распознавание..."
            }
        )
    except Exception:
        logging.warning("Could not answer callback query", exc_info=True)

    if not chat_id or callback.get("data") != "recognize":
        return

    with PENDING_LOCK:
        item = PENDING.get(chat_id)

    if not item:
        send_text(
            chat_id,
            "Сначала отправь видео, аудио или ссылку, "
            "затем нажми кнопку под сообщением «получено»."
        )
        return

    run_job(chat_id, item["type"], item["value"])

    # Удаляем запрос только после завершения обработки.
    with PENDING_LOCK:
        if PENDING.get(chat_id) == item:
            PENDING.pop(chat_id, None)


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
        "Telegram update received: update_id=%s message=%s callback=%s",
        update.get("update_id"),
        "message" in update,
        "callback_query" in update
    )

    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception:
        logging.exception("Webhook processing failed")

    return jsonify({"ok": True}), 200


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
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
        logging.exception("Webhook setup failed")
        return jsonify({"ok": False, "error": "Webhook setup failed"}), 500


@app.route("/webhook-info", methods=["GET"])
def webhook_info():
    try:
        result = telegram("getWebhookInfo")
        return jsonify({
            "ok": True,
            "url": result.get("url"),
            "pending_update_count": result.get("pending_update_count"),
            "last_error_date": result.get("last_error_date"),
            "last_error_message": result.get("last_error_message")
        })
    except Exception:
        logging.exception("Webhook info failed")
        return jsonify({"ok": False, "error": "Could not get webhook info"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
