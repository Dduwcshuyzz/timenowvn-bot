#!/usr/bin/env python3
"""Webhook server cho bot Telegram @timenowvn_bot — bot tải video TimeNow.

Cách hoạt động: Telegram đẩy tin nhắn tới server ngay khi có người nhắn
(webhook), server xử lý ngay tại chỗ, trả lời trong vài giây.

- Gửi link TikTok vào bot -> bot tải qua API TikWM rồi gửi lại file
  video không logo (hoặc từng ảnh nếu là bài đăng ảnh).
- Nhắn "nhac" sau khi tải video -> bot gửi lại file nhạc MP3.
- Huy (admin) nhắn câu hỏi -> bot trả lời như Muse qua AI (Groq).
- Người lạ nhắn chữ không phải link -> chỉ hướng dẫn gửi link TikTok.

Chạy:  python3 webhook_server.py   (mặc định cổng 8080)
Biến môi trường bắt buộc:
  TELEGRAM_BOT_TOKEN  - token bot lấy từ @BotFather
  AI_API_KEY          - key AI (Groq, tương thích OpenAI)
Tùy chọn:
  AI_API_BASE - mặc định https://api.openai.com/v1
  AI_MODEL    - mặc định gpt-4o-mini
  ADMIN_CHAT_ID - mặc định 7223454879 (Huy)
  PORT        - mặc định 8080

Sau khi server chạy có địa chỉ https công khai, đặt webhook:
  curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<địa-chỉ-của-bạn>/webhook"
Xóa webhook (quay về polling): setWebhook?url=
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AI_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE = os.environ.get("AI_API_BASE", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "7223454879"))
PORT = int(os.environ.get("PORT", "8080"))
LOCK = threading.Lock()

# Nhạc MP3 của video TikTok vừa tải xong, theo chat_id — để nhắn "nhac" là lấy.
LAST_MUSIC = {}

SYSTEM_PROMPT = (
    "Bạn là Muse, trợ lý của Huy (Le Duc Huy, CEO VietMess.Com, "
    "làm web TimeNow.vn tải video, kênh TikTok/YouTube 'Chuyện lạ / Bí ẩn 60 giây', "
    "app học tiếng Đức A1-C2, game Sinh Tồn Hà Nội). Trả lời bằng tiếng Việt, "
    "ngắn gọn nhất có thể, đúng giọng Muse, không bịa thông tin chưa kiểm chứng."
)

TIKWM_API = "https://www.tikwm.com/api/?url="
URL_RE = re.compile(r"https?://\S+")
TIKTOK_HOSTS = ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com")
OTHER_VIDEO_HOSTS = ("youtube.com", "youtu.be", "instagram.com",
                     "facebook.com", "fb.watch", "x.com", "twitter.com")


def tg(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def send(chat_id, text):
    # Telegram giới hạn 4096 ký tự / tin
    for i in range(0, len(text), 4000):
        tg("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4000]})


def send_video(chat_id, video_url, caption=""):
    tg("sendVideo", {"chat_id": chat_id, "video": video_url,
                     "caption": caption[:1000]})


def send_photo(chat_id, photo_url, caption=""):
    tg("sendPhoto", {"chat_id": chat_id, "photo": photo_url,
                     "caption": caption[:1000]})


def send_audio(chat_id, audio_url, caption=""):
    tg("sendAudio", {"chat_id": chat_id, "audio": audio_url,
                     "caption": caption[:1000]})


def call_ai(user_text):
    url = f"{AI_BASE}/chat/completions"
    body = {"model": AI_MODEL, "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ], "temperature": 0.7, "max_tokens": 800}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {AI_KEY}"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip()


def fetch_tiktok(url):
    """Gọi API TikWM, thử lại tối đa 3 lần (API miễn phí lúc cao điểm hay bận)."""
    api_url = TIKWM_API + urllib.parse.quote(url, safe="")
    last_err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(api_url, headers={
                "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            if data.get("code") == 0 and data.get("data"):
                return data["data"]
            last_err = data.get("msg", "API lỗi")
        except Exception as e:  # noqa: BLE001 - thử lại lần sau
            last_err = e
        time.sleep(2)
    raise RuntimeError(f"TikWM lỗi: {last_err}")


def fmt_num(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".", ",")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".", ",")
    return str(n)


def handle_tiktok(chat_id, url):
    send(chat_id, "⏳ Đang tải video, chờ mình vài giây nhé...")
    try:
        data = fetch_tiktok(url)
    except Exception:  # noqa: BLE001 - báo lỗi gọn cho người dùng
        send(chat_id, "Không tải được link này. Kiểm tra lại link có công khai "
                      "không rồi gửi lại nhé.")
        return
    author = (data.get("author") or {})
    nick = author.get("nickname") or author.get("unique_id") or "TikTok"
    title = (data.get("title") or "").strip()
    dur = data.get("duration") or 0
    caption = f"🎬 @{author.get('unique_id', '')} ({nick})\n{title}"
    if dur:
        caption += f"\n⏱ {dur}s • 👁 {fmt_num(data.get('play_count'))} • ❤️ {fmt_num(data.get('digg_count'))}"

    # Bài đăng ảnh -> gửi lại từng ảnh.
    images = data.get("images") or []
    if images:
        for i, img in enumerate(images[:10]):
            send_photo(chat_id, img, caption if i == 0 else "")
        if data.get("music"):
            with LOCK:
                LAST_MUSIC[chat_id] = data["music"]
            send(chat_id, "🎵 Muốn lấy file nhạc MP3 thì nhắn: nhac")
        return

    # Video -> gửi lại file video không logo (ưu tiên bản HD nếu có).
    play = data.get("hdplay") or data.get("play")
    if play:
        size = int(data.get("size") or 0)
        if size > 45_000_000:
            send(chat_id, "Video nặng hơn 45MB nên bot không gửi file được, "
                          f"Huy bấm link này để tải: {play}")
        else:
            try:
                send_video(chat_id, play, caption)
            except Exception:  # noqa: BLE001 - thử lại bằng link thường
                send(chat_id, "Gửi video lỗi, thử lại bằng link tải trực tiếp:\n"
                              f"{play}")
        if data.get("music"):
            with LOCK:
                LAST_MUSIC[chat_id] = data["music"]
            send(chat_id, "🎵 Muốn lấy file nhạc MP3 thì nhắn: nhac")
        return

    # Chỉ còn nhạc -> gửi file âm thanh.
    if data.get("music"):
        send_audio(chat_id, data["music"], caption)
        return

    send(chat_id, "Link này không có video hay ảnh, thử link khác nhé.")


def handle_update(update):
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return
    username = (msg.get("from") or {}).get("username", "")
    text = (msg.get("text") or "").strip()
    low = text.lower()

    # ---- /start ----
    if low.startswith("/start"):
        card = ("👋 Chào bạn, mình là bot TimeNow.\n"
                "Gửi link TikTok vào đây, mình tải video/ảnh/nhạc gửi lại "
                "cho bạn nhé.\n\n"
                "📌 Video -> gửi lại file video không logo\n"
                "🖼️ Bài đăng ảnh -> gửi lại từng ảnh\n"
                "🎵 Nhắn 'nhac' sau khi tải video để lấy file MP3")
        if chat_id == ADMIN_CHAT_ID:
            card += "\n\nHuy cứ nhắn câu hỏi, mình trả lời như Muse nhé."
        send(chat_id, card)
        return

    # ---- Lấy nhạc MP3 của video vừa tải ----
    if low in ("nhac", "mp3", "🎵", "nhạc"):
        with LOCK:
            music = LAST_MUSIC.get(chat_id)
        if music:
            try:
                send_audio(chat_id, music, "🎵 Nhạc nền TikTok")
            except Exception:  # noqa: BLE001 - gửi link thay cho file
                send(chat_id, f"Gửi file nhạc lỗi, Huy bấm link này để tải: {music}")
        else:
            send(chat_id, "Bạn chưa tải video nào, gửi link TikTok trước nhé.")
        return

    # ---- Có link trong tin nhắn ----
    m = URL_RE.search(text)
    if m:
        url = m.group(0).rstrip(").,")
        host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
        if host.endswith(TIKTOK_HOSTS) or host in TIKTOK_HOSTS:
            handle_tiktok(chat_id, url)
        elif any(host.endswith(h) or host == h for h in OTHER_VIDEO_HOSTS):
            send(chat_id, "Hiện bot mới tải được TikTok thôi, YouTube / "
                          "Instagram / Facebook sẽ thêm sau nhé.")
        else:
            send(chat_id, "Link này bot chưa tải được, hiện mới tải được "
                          "link TikTok thôi nhé.")
        return

    # ---- Hỏi đáp Muse (chỉ Huy) ----
    if chat_id == ADMIN_CHAT_ID and text:
        if not AI_KEY:
            send(chat_id, "Bot chưa gắn key AI nên chưa trả lời được câu hỏi này. "
                          "Huy gắn AI_API_KEY vào server webhook nhé.")
            return
        try:
            send(chat_id, call_ai(text))
        except Exception as e:  # noqa: BLE001 - báo lỗi gọn
            send(chat_id, f"AI đang lỗi, Huy thử lại sau nhé. ({e})")
        return

    # ---- Người lạ nhắn chữ ----
    if text:
        send(chat_id, "Gửi link TikTok vào đây, mình tải video/ảnh/nhạc "
                      "gửi lại cho bạn nhé.")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b'{"ok": true}'
        else:
            body = b'{"ok": false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            update = json.loads(raw.decode("utf-8"))
        except Exception:
            update = {}
        # Trả 200 ngay để Telegram không gửi lại, xử lý ở thread riêng.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
        threading.Thread(target=handle_update, args=(update,), daemon=True).start()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Thiếu TELEGRAM_BOT_TOKEN. Đặt biến môi trường trước khi chạy.")
    print(f"Webhook server chạy ở cổng {PORT} — POST /webhook", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
