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
import subprocess
import tempfile
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


def log(*args):
    # Render ghi stdout ra Logs, để xem lỗi tải thật khi cần.
    print("[timenow]", *args, flush=True)


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


MAX_MUSIC_BYTES = 20_000_000


def tg_multipart_audio(chat_id, path, filename, caption=""):
    """Gửi file audio lên Telegram bằng multipart (file thật, có đuôi)."""
    boundary = "----timenow" + str(int(time.time() * 1000))
    body = bytearray()

    def field(name, value):
        body.extend(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8"))

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption[:1000])
    with open(path, "rb") as f:
        filedata = f.read()
    mime = "audio/mpeg" if filename.endswith(".mp3") else "audio/mp4"
    body.extend(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"audio\"; "
        f"filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
    body.extend(filedata)
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendAudio",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram lỗi: {res}")
    return res


def send_music_file(chat_id, url, caption="🎵 Nhạc nền của video"):
    """Gửi file nhạc: tải về rồi upload lại dạng MP3.
    Gửi thẳng bằng link hay bị Telegram nhận nhầm thành file video/mp4
    vì link nhạc TikTok không có đuôi .mp3 và CDN trả sai Content-Type
    (file thật là MP3 nhưng Telegram lưu thành file .mp4)."""
    tmp = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(MAX_MUSIC_BYTES)
        if not data:
            raise RuntimeError("file nhạc rỗng")
        if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF
                                 and (data[1] & 0xE0) == 0xE0):
            ext = "mp3"
        elif len(data) > 8 and data[4:8] == b"ftyp":
            ext = "m4a"
        else:
            ext = "mp3"
        fd, tmp = tempfile.mkstemp(suffix=f".{ext}", prefix="timenow_music_")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tg_multipart_audio(chat_id, tmp, f"nhac.{ext}", caption)
    except Exception as e:  # noqa: BLE001 - thử gửi bằng link như trước
        log(f"Tải/upload nhạc lỗi ({e}), thử gửi bằng link")
        send_audio(chat_id, url, caption)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


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


def fetch_tikwm(url):
    """Gọi API TikWM, thử lại tối đa 2 lần. Trả dict data hoặc raise."""
    api_url = TIKWM_API + urllib.parse.quote(url, safe="")
    last_err = None
    for i in range(2):
        try:
            req = urllib.request.Request(api_url, headers={
                "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.load(r)
            if data.get("code") == 0 and data.get("data"):
                return data["data"]
            last_err = data.get("msg", "API lỗi")
        except Exception as e:  # noqa: BLE001 - thử lại / fallback sau
            last_err = e
        log(f"TikWM lần {i + 1} lỗi: {last_err}")
        time.sleep(1)
    raise RuntimeError(f"TikWM lỗi: {last_err}")


def ytdlp_info(url):
    """Lấy metadata TikTok bằng yt-dlp (tải thẳng từ TikTok, không qua TikWM)."""
    cmd = ["python3", "-m", "yt_dlp", "--no-playlist", "--dump-json",
           "--no-download", "--no-warnings", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError("yt-dlp lỗi: " + (r.stderr or "").strip()[-300:])
    return json.loads(r.stdout)


def best_video_url(info):
    """URL mp4 tốt nhất dưới 45MB (giới hạn gửi file của Telegram)."""
    cands = []
    for f in info.get("formats") or []:
        if f.get("ext") != "mp4" or f.get("vcodec") in (None, "none"):
            continue
        if f.get("acodec") in (None, "none"):
            continue
        if not f.get("url"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if size and size > 45_000_000:
            continue
        cands.append((f.get("height") or 0, f["url"]))
    if cands:
        cands.sort()
        return cands[-1][1]
    if info.get("ext") == "mp4" and info.get("url"):
        return info["url"]
    return None


def best_audio_url(info):
    cands = []
    for f in info.get("formats") or []:
        if f.get("vcodec") not in (None, "none"):
            continue
        if f.get("acodec") in (None, "none") or not f.get("url"):
            continue
        cands.append((f.get("abr") or 0, f["url"]))
    if cands:
        cands.sort()
        return cands[-1][1]
    return None


MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")


def fetch_tiktok_page(url):
    """Đọc trang TikTok: vừa resolve link ngắn ra URL đầy đủ, vừa lấy HTML.
    Trả (final_url, html). Lỗi thì trả (url, '')."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.geturl(), r.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001 - caller sẽ fallback tiếp
        log(f"Đọc trang TikTok lỗi: {e}")
        return url, ""


def extract_photo_images(html):
    """Trích link ảnh bài đăng ảnh từ HTML trang TikTok (imagePost).
    URL trong HTML có thể ở dạng thường hoặc escape JSON, nên gỡ escape
    trước rồi tìm. Cùng 1 ảnh có thể xuất hiện ở 2 host CDN khác nhau
    (p16/p19...) nên lọc trùng theo phần đường dẫn sau host."""
    html = html.replace("\\u002F", "/").replace("\\/", "/")
    urls = re.findall(r"https://[^\"'\\\s]+photomode-image[^\"'\\\s]*", html)
    # Ưu tiên ảnh gốc, bỏ ảnh bìa/crop (image-cover).
    full = [u for u in urls if "image-cover" not in u]
    urls = full or urls
    seen, out = set(), []
    for u in urls:
        # Lọc trùng theo đường dẫn sau host, bỏ phần query (cùng 1 ảnh có
        # thể xuất hiện ở 2 host CDN với chữ ký khác nhau).
        key = u.split("/", 3)[-1].split("?")[0] if u.count("/") >= 3 else u
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


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


def send_from_tikwm(chat_id, data):
    """Gửi đủ ảnh bìa + video + nhạc từ dữ liệu TikWM.
    Trả tập hợp những gì đã gửi được: {"anh", "video", "nhac"}."""
    sent = set()
    author = (data.get("author") or {})
    nick = author.get("nickname") or author.get("unique_id") or "TikTok"
    title = (data.get("title") or "").strip()
    dur = data.get("duration") or 0
    caption = f"🎬 @{author.get('unique_id', '')} ({nick})\n{title}"
    if dur:
        caption += f"\n⏱ {dur}s • 👁 {fmt_num(data.get('play_count'))} • ❤️ {fmt_num(data.get('digg_count'))}"

    def send_music(url):
        try:
            send_music_file(chat_id, url, "🎵 Nhạc nền của video")
            sent.add("nhac")
            with LOCK:
                LAST_MUSIC[chat_id] = url
        except Exception as e:  # noqa: BLE001 - nhạc lỗi thì vẫn còn ảnh/video
            log(f"Gửi nhạc lỗi: {e}")

    # Bài đăng ảnh -> gửi lại từng ảnh, rồi gửi luôn file nhạc.
    images = data.get("images") or []
    if images:
        ok = False
        for i, img in enumerate(images[:10]):
            try:
                send_photo(chat_id, img, caption if i == 0 else "")
                ok = True
            except Exception as e:  # noqa: BLE001 - ảnh lỗi thì gửi ảnh khác
                log(f"Gửi ảnh {i} lỗi: {e}")
        if ok:
            sent.add("anh")
        if data.get("music"):
            send_music(data["music"])
        return sent

    # Video -> ảnh bìa trước, rồi file video không logo, rồi file nhạc.
    play = data.get("hdplay") or data.get("play")
    if play:
        cover = data.get("cover")
        if cover:
            try:
                send_photo(chat_id, cover, caption)
                sent.add("anh")
            except Exception as e:  # noqa: BLE001 - bìa lỗi thì vẫn gửi video
                log(f"Gửi ảnh bìa lỗi: {e}")
        size = int(data.get("size") or 0)
        if size > 45_000_000:
            send(chat_id, "Video nặng hơn 45MB nên bot không gửi file được, "
                          f"Huy bấm link này để tải: {play}")
        else:
            try:
                send_video(chat_id, play, "" if "anh" in sent else caption)
            except Exception:  # noqa: BLE001 - thử lại bằng link thường
                send(chat_id, "Gửi video lỗi, thử lại bằng link tải trực tiếp:\n"
                              f"{play}")
        sent.add("video")
        if data.get("music"):
            send_music(data["music"])
        return sent

    # Chỉ còn nhạc -> gửi file âm thanh.
    if data.get("music"):
        send_music_file(chat_id, data["music"], caption)
        sent.add("nhac")
        with LOCK:
            LAST_MUSIC[chat_id] = data["music"]
        return sent

    return sent


def extract_music_url(html):
    """Link nhạc nền trong HTML trang TikTok (music.playUrl).
    URL nhạc không chứa chữ 'music', nó nằm trong object music của trang."""
    html2 = html.replace("\\u002F", "/").replace("\\/", "/")
    m = re.search(r'"music"\s*:\s*\{[^}]*?"playUrl"\s*:\s*"([^"]+)"', html2)
    if m:
        return m.group(1)
    for m in re.finditer(r'"playUrl"\s*:\s*"([^"]+)"', html2):
        if "audio" in m.group(1):
            return m.group(1)
    return None


def handle_with_ytdlp(chat_id, url):
    """Cách 2: tải thẳng từ TikTok bằng yt-dlp / đọc HTML trang.
    Trả tập hợp những gì đã gửi được: {"anh", "video", "nhac"}."""
    # Đọc thử trang TikTok: resolve link ngắn, đồng thời lấy HTML để trích ảnh.
    final_url, html = fetch_tiktok_page(url)
    sent = set()
    if "/photo/" in final_url:
        images = extract_photo_images(html)
        if not images:
            raise RuntimeError("không trích được ảnh từ trang TikTok")
        for i, img in enumerate(images[:10]):
            send_photo(chat_id, img, "🖼️ Ảnh TikTok" if i == 0 else "")
        sent.add("anh")
        music = extract_music_url(html)
        if music:
            try:
                send_music_file(chat_id, music, "🎵 Nhạc nền")
                sent.add("nhac")
                with LOCK:
                    LAST_MUSIC[chat_id] = music
            except Exception as e:  # noqa: BLE001 - nhạc lỗi thì vẫn còn ảnh
                log(f"Gửi nhạc lỗi: {e}")
        return sent
    info = ytdlp_info(url)
    video_url = best_video_url(info)
    if not video_url:
        raise RuntimeError("yt-dlp không tìm được link video")
    title = (info.get("title") or "").strip()
    uploader = info.get("uploader") or ""
    dur = info.get("duration") or 0
    caption = f"🎬 @{uploader}\n{title}"
    if dur:
        caption += f"\n⏱ {dur}s"
    thumb = info.get("thumbnail")
    if thumb:
        try:
            send_photo(chat_id, thumb, caption)
            sent.add("anh")
        except Exception as e:  # noqa: BLE001 - bìa lỗi thì vẫn gửi video
            log(f"Gửi ảnh bìa lỗi: {e}")
    send_video(chat_id, video_url, "" if "anh" in sent else caption)
    sent.add("video")
    audio = best_audio_url(info)
    if audio:
        try:
            send_music_file(chat_id, audio, "🎵 Nhạc nền của video")
            sent.add("nhac")
            with LOCK:
                LAST_MUSIC[chat_id] = audio
        except Exception as e:  # noqa: BLE001 - nhạc lỗi thì vẫn còn video
            log(f"Gửi nhạc lỗi: {e}")
    return sent


def send_done(chat_id, sent):
    """Câu chốt sau khi tải xong: nói rõ đã gửi những gì."""
    parts = []
    if "anh" in sent:
        parts.append("ảnh")
    if "video" in sent:
        parts.append("video")
    if "nhac" in sent:
        parts.append("nhạc MP3")
    if not parts:
        return
    send(chat_id, "✅ Xong rồi Huy! Mình gửi " + ", ".join(parts) +
                  " ở trên, thích cái nào thì tải cái đó nhé.")


def handle_tiktok(chat_id, url):
    send(chat_id, "⏳ Đang tải cho Huy đây, chờ mình vài giây nhé...")
    # Cách 1: API TikWM (nhanh). Lỗi thì sang cách 2.
    data = None
    try:
        data = fetch_tikwm(url)
    except Exception as e:  # noqa: BLE001 - fallback sang yt-dlp
        log(f"TikWM thất bại, chuyển sang yt-dlp: {e}")
    if data is not None:
        try:
            sent = send_from_tikwm(chat_id, data)
        except Exception as e:  # noqa: BLE001 - fallback sang yt-dlp
            log(f"Gửi từ TikWM lỗi, chuyển sang yt-dlp: {e}")
            sent = set()
        if sent:
            send_done(chat_id, sent)
            return
        log("TikWM có dữ liệu nhưng không gửi được, chuyển sang yt-dlp")
    # Cách 2: yt-dlp / đọc HTML trang.
    try:
        sent = handle_with_ytdlp(chat_id, url)
    except Exception as e:  # noqa: BLE001 - báo lỗi gọn cho người dùng
        log(f"yt-dlp thất bại: {e}")
        send(chat_id, "Không tải được link này, thử lại sau vài phút nhé. "
                      "(Link vẫn công khai, do server tải đang bận.)")
        return
    send_done(chat_id, sent)


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
                "Gửi link TikTok vào đây, mình gửi đủ cả ảnh bìa, video "
                "không logo và nhạc MP3 để bạn thích cái nào thì tải cái đó.\n\n"
                "📌 Video -> ảnh bìa + file video + file nhạc\n"
                "🖼️ Bài đăng ảnh -> từng ảnh + file nhạc\n"
                "🎵 Nhắn 'nhac' để gửi lại file nhạc của link vừa tải")
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
                send_music_file(chat_id, music, "🎵 Nhạc nền TikTok")
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
