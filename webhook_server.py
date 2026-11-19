#!/usr/bin/env python3
"""Webhook server cho bot Telegram @timenowvn_bot — bot tải video TimeNow.

Cách hoạt động: Telegram đẩy tin nhắn tới server ngay khi có người nhắn
(webhook), server xử lý ngay tại chỗ, trả lời trong vài giây.

- Gửi link TikTok / Douyin / YouTube / Instagram / Facebook / X vào bot
  -> bot gửi đủ ảnh bìa, video không logo và nhạc MP3 một lần.
  Douyin (v12) đọc trang chia sẻ iesdouyin (SSR) bằng cookie khách
  ttwid, không cần đăng nhập; nhạc MP3 tách từ file video bằng ffmpeg.
- Gửi nhiều link trong 1 tin nhắn (tối đa 3) -> bot tải từng cái.
- Sau khi tải xong có nút bấm: lấy lại nhạc, tải bản thường, mở link gốc.
- Nhắn "nhac" sau khi tải video -> bot gửi lại file nhạc MP3.
- /help xem hướng dẫn, /thongke xem số link đã tải.
- Huy (admin) nhắn câu hỏi -> bot trả lời như Muse qua AI (Groq).
- Lưu id mọi người dùng nhắn bot vào users.json (để gửi thông báo hàng loạt).
- Ghi lại số lượt tải + nền tảng của từng người dùng (admin xem trong /danhsach).
- Mỗi khi có người tải xong, bot nhắn báo ngay cho Huy (admin) kèm tên
  người tải, nền tảng và link (không báo khi chính Huy tải).
- Huy nhắn /thongbao <nội dung> để gửi thông báo tới toàn bộ người dùng đã lưu.
- Người lạ nhắn chữ không phải link -> chỉ hướng dẫn gửi link.
- Gặp sự cố khi tải -> tin báo lỗi kèm nút "🆘 Cần hỗ trợ": bấm nút là
  bot gửi yêu cầu hỗ trợ tới Huy (admin) kèm link lỗi, tin nhắn tiếp theo
  của người dùng được chuyển tiếp cho admin; admin trả lời bằng
  /traloi <chat_id> <nội dung>. /hotro mở hỗ trợ bất cứ lúc nào.
- Chỉ báo video đã gửi khi thực sự gửi được; TikWM gửi video lỗi thì
  tự thử yt-dlp tải file rồi upload lên Telegram.

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
import http.cookiejar
import json
import os
import re
import shutil
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
# Thông tin link vừa tải xong, theo chat_id — để nút bấm lấy lại nhạc / bản thường.
LAST_DOWNLOAD = {}
# Link lỗi của lần tải gần nhất, theo chat_id — để nút "Cần hỗ trợ" biết
# gửi link nào cho admin.
LAST_FAILED = {}
# Người dùng đã bấm "Cần hỗ trợ" và đang chờ mô tả thêm: tin nhắn chữ
# tiếp theo của họ được chuyển tiếp cho admin.
SUPPORT_PENDING = {}
# Thống kê trong bộ nhớ (reset khi server chạy lại).
STATS = {"total": 0, "platforms": {}}

# Danh sách người dùng đã nhắn bot (chat riêng), lưu vào users.json để
# gửi thông báo hàng loạt. File lưu trên ổ đĩa server (Render Free mất file
# khi deploy/khởi động lại thì danh sách tự ghi lại khi người dùng nhắn tiếp).
USERS_FILE = os.path.join(BASE, "users.json")


def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_users(users):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)


USERS = load_users()


def register_user(chat_id, user=None):
    """Lưu id người dùng khi họ nhắn bot (để sau này gửi thông báo hàng loạt).
    Chỉ lưu chat riêng (id > 0); nhóm/kênh không lưu."""
    if not chat_id or chat_id <= 0:
        return
    key = str(chat_id)
    now = int(time.time())
    with LOCK:
        u = USERS.get(key) or {"first_seen": now}
        u["last_seen"] = now
        if user:
            if user.get("username"):
                u["username"] = user.get("username")
            if user.get("first_name"):
                u["first_name"] = user.get("first_name")
        USERS[key] = u
        try:
            save_users(USERS)
        except Exception as e:  # noqa: BLE001 - lưu lỗi thì lần sau ghi lại
            log(f"Lưu users.json lỗi: {e}")


def fmt_ago(ts):
    """'vừa xong', '5 phút trước', '2 giờ trước', '3 ngày trước'."""
    try:
        delta = int(time.time()) - int(ts)
    except (TypeError, ValueError):
        return "không rõ"
    if delta < 60:
        return "vừa xong"
    if delta < 3600:
        return f"{delta // 60} phút trước"
    if delta < 86400:
        return f"{delta // 3600} giờ trước"
    return f"{delta // 86400} ngày trước"


def record_download(chat_id, platform):
    """Ghi lại hoạt động tải video của người dùng (để admin xem trong /danhsach)."""
    if not chat_id or chat_id <= 0:
        return
    key = str(chat_id)
    now = int(time.time())
    with LOCK:
        u = USERS.get(key) or {"first_seen": now}
        u["downloads"] = int(u.get("downloads") or 0) + 1
        u["last_platform"] = platform
        u["last_download"] = now
        u["last_seen"] = now
        USERS[key] = u
        try:
            save_users(USERS)
        except Exception as e:  # noqa: BLE001 - lưu lỗi thì lần sau ghi lại
            log(f"Lưu users.json lỗi: {e}")


def bump_stat(platform):
    with LOCK:
        STATS["total"] += 1
        STATS["platforms"][platform] = STATS["platforms"].get(platform, 0) + 1


def save_download(chat_id, **kw):
    with LOCK:
        d = LAST_DOWNLOAD.setdefault(chat_id, {})
        d.update(kw)

SYSTEM_PROMPT = (
    "Bạn là Muse, trợ lý của Huy (Le Duc Huy, CEO VietMess.Com, "
    "làm web TimeNow.vn tải video, kênh TikTok/YouTube 'Chuyện lạ / Bí ẩn 60 giây', "
    "app học tiếng Đức A1-C2, game Sinh Tồn Hà Nội). Trả lời bằng tiếng Việt, "
    "ngắn gọn nhất có thể, đúng giọng Muse, không bịa thông tin chưa kiểm chứng."
)

TIKWM_API = "https://www.tikwm.com/api/?url="
# Host dự phòng của TikWM (cùng API, khác host).
TIKWM_API_ALT = "https://tikwm.com/api/?url="
URL_RE = re.compile(r"https?://\S+")
TIKTOK_HOSTS = ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com")
DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
# Nền tảng tải bằng yt-dlp (đã kiểm chứng YouTube, Instagram trên máy;
# Facebook, X dùng chung cách tải, link riêng tư/khóa thì không tải được).
PLATFORM_HOSTS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook", "fb.watch": "Facebook",
    "x.com": "X", "twitter.com": "X",
}


def platform_of(host):
    if host.endswith(TIKTOK_HOSTS):
        return "TikTok"
    if host.endswith(DOUYIN_HOSTS):
        return "Douyin"
    for h, name in PLATFORM_HOSTS.items():
        if host == h or host.endswith("." + h):
            return name
    return None


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


def send_markup(chat_id, text, markup):
    tg("sendMessage", {"chat_id": chat_id, "text": text[:4000],
                       "reply_markup": markup})


SUPPORT_BUTTON = {"inline_keyboard": [
    [{"text": "🆘 Cần hỗ trợ", "callback_data": "support"}]]}


def notify_admin_download(chat_id, platform, url):
    """Báo cho Huy mỗi khi có người tải xong (không báo khi chính Huy tải)."""
    if chat_id == ADMIN_CHAT_ID:
        return
    user = (USERS.get(str(chat_id)) or {})
    who = f"@{user['username']}" if user.get("username") else str(chat_id)
    if user.get("first_name"):
        who += f" ({user['first_name']})"
    try:
        send(ADMIN_CHAT_ID, f"📥 {who} vừa tải {platform}\n🔗 {url}")
    except Exception as e:  # noqa: BLE001 - báo admin lỗi thì vẫn tải bình thường
        log(f"Báo admin lượt tải lỗi: {e}")


def send_error(chat_id, text, failed_url=None, platform=None):
    """Tin báo lỗi kèm nút hỗ trợ: bấm nút là bot báo cho admin kèm link lỗi.
    Lưu link lỗi lại để callback 'support' biết gửi link nào."""
    if failed_url:
        with LOCK:
            LAST_FAILED[chat_id] = {"url": failed_url,
                                    "platform": platform or ""}
    send_markup(chat_id, text, SUPPORT_BUTTON)


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


def tg_multipart(method, field, path, filename, mime, chat_id, caption=""):
    """Gửi file lên Telegram bằng multipart (file thật, có đuôi)."""
    boundary = "----timenow" + str(int(time.time() * 1000))
    body = bytearray()

    def field_part(name, value):
        body.extend(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8"))

    field_part("chat_id", str(chat_id))
    if caption:
        field_part("caption", caption[:1000])
    with open(path, "rb") as f:
        filedata = f.read()
    body.extend(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{field}\"; "
        f"filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
    body.extend(filedata)
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram lỗi: {res}")
    return res


def tg_multipart_audio(chat_id, path, filename, caption=""):
    mime = "audio/mpeg" if filename.endswith(".mp3") else "audio/mp4"
    return tg_multipart("sendAudio", "audio", path, filename, mime,
                        chat_id, caption)


def tg_multipart_video(chat_id, path, filename, caption=""):
    return tg_multipart("sendVideo", "video", path, filename, "video/mp4",
                        chat_id, caption)


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
    """Gọi API TikWM, thử lại tối đa 3 lần, đổi host www <-> không www.
    TikWM hay giới hạn theo IP (403) khi gọi dồn dập vì IP server dùng
    chung cho mọi người dùng bot, nên thử lại với chờ lâu dần.
    Trả dict data hoặc raise."""
    hosts = (TIKWM_API, TIKWM_API_ALT)
    last_err = None
    for i in range(3):
        api_url = hosts[i % len(hosts)] + urllib.parse.quote(url, safe="")
        try:
            req = urllib.request.Request(api_url, headers={
                "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            if data.get("code") == 0 and data.get("data"):
                return data["data"]
            last_err = data.get("msg", "API lỗi")
        except Exception as e:  # noqa: BLE001 - thử lại / fallback sau
            last_err = e
        log(f"TikWM lần {i + 1} lỗi: {last_err}")
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"TikWM lỗi: {last_err}")


def ytdlp_info(url):
    """Lấy metadata bằng yt-dlp, thử lại 3 lần (TikTok hay chặn IP lúc cao điểm).
    YouTube thử client tv_embedded rồi Android trước vì client web hay bị
    chặn IP datacenter (lỗi 'Failed to extract any player response')."""
    last_err = None
    for i, extra in enumerate((["--extractor-args",
                                "youtube:player_client=tv_embedded"],
                               ["--extractor-args",
                                "youtube:player_client=android"], [])):
        cmd = ["python3", "-m", "yt_dlp", "--no-playlist", "--dump-json",
               "--no-download", "--no-warnings"] + extra + [url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            return json.loads(r.stdout)
        last_err = (r.stderr or "").strip()[-300:]
        log(f"yt-dlp lần {i + 1} lỗi: {last_err}")
        time.sleep(3)
    raise RuntimeError("yt-dlp lỗi: " + (last_err or ""))


def ffmpeg_exe():
    """Đường dẫn ffmpeg tĩnh từ imageio-ffmpeg (để yt-dlp gộp hình + tiếng)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def download_merged(url, max_height=720):
    """Dùng yt-dlp tải video + nhạc rồi gộp thành 1 file mp4.
    Trả (đường dẫn file, thư mục tạm). Raise nếu lỗi hoặc file quá 45MB.
    YouTube thử client tv_embedded rồi Android trước vì client web hay bị
    chặn IP datacenter (lỗi 'Failed to extract any player response')."""
    tmpdir = tempfile.mkdtemp(prefix="timenow_dl_")
    out = os.path.join(tmpdir, "video.mp4")
    fmt = (f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
           f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]")
    ff = ffmpeg_exe()
    last_err = None
    for extra in (["--extractor-args", "youtube:player_client=tv_embedded"],
                  ["--extractor-args", "youtube:player_client=android"], []):
        cmd = ["python3", "-m", "yt_dlp", "--no-playlist", "--no-warnings",
               "-f", fmt, "--merge-output-format", "mp4",
               "-o", out] + extra + [url]
        if ff:
            cmd += ["--ffmpeg-location", ff]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if r.returncode == 0 and os.path.exists(out):
            if os.path.getsize(out) > 45_000_000:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise RuntimeError("Video nặng hơn 45MB")
            return out, tmpdir
        last_err = (r.stderr or "").strip()[-300:]
        log(f"yt-dlp tải lỗi: {last_err}")
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass
    shutil.rmtree(tmpdir, ignore_errors=True)
    raise RuntimeError("yt-dlp tải lỗi: " + (last_err or ""))


def mp4_urls(info):
    """Các URL mp4 có cả hình + tiếng, xếp theo chiều cao tăng dần."""
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
    cands.sort()
    return cands


def best_video_url(info):
    """URL mp4 tốt nhất dưới 45MB (giới hạn gửi file của Telegram)."""
    cands = mp4_urls(info)
    if cands:
        return cands[-1][1]
    if info.get("ext") == "mp4" and info.get("url"):
        return info["url"]
    return None


def smallest_video_url(info):
    """URL mp4 nhẹ nhất (bản thường cho nút bấm)."""
    cands = mp4_urls(info)
    if cands:
        return cands[0][1]
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
    last_err = None
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.geturl(), r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001 - thử lại rồi caller fallback tiếp
            last_err = e
            log(f"Đọc trang TikTok lần {i + 1} lỗi: {e}")
            time.sleep(2)
    log(f"Đọc trang TikTok lỗi: {last_err}")
    return url, ""


def extract_video_url(html):
    """Link video trong HTML trang TikTok (video.playAddr)."""
    html2 = html.replace("\\u002F", "/").replace("\\/", "/")
    m = re.search(r'"playAddr"\s*:\s*"([^"]+)"', html2)
    return m.group(1) if m else None


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


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36")
# Giới hạn gửi file của Telegram Bot API.
MAX_VIDEO_BYTES = 45_000_000


def download_video_file(url, max_bytes=120_000_000):
    """Tải file video về thư mục tạm, trả (đường dẫn file, thư mục tạm).
    CDN TikTok chặn request không có UA trình duyệt + Referer (403) nên
    phải giả trình duyệt. Raise nếu lỗi hoặc file quá max_bytes."""
    tmpdir = tempfile.mkdtemp(prefix="timenow_vid_")
    path = os.path.join(tmpdir, "video.mp4")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA, "Referer": "https://www.tiktok.com/"})
        total = 0
        with urllib.request.urlopen(req, timeout=180) as r, \
                open(path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("Video nặng hơn giới hạn tải")
                f.write(chunk)
        if total == 0:
            raise RuntimeError("file video rỗng")
        return path, tmpdir
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def reencode_fit(path, tmpdir, duration=0):
    """Nén lại video bằng ffmpeg để vừa giới hạn 45MB của Telegram.
    Tính bitrate từ thời lượng (giữ 90% giới hạn cho an toàn); thử 720p
    rồi 480p. Trả đường dẫn file vừa giới hạn (file gốc nếu đã vừa).
    Raise nếu vẫn quá nặng hoặc không có ffmpeg."""
    if os.path.getsize(path) <= MAX_VIDEO_BYTES:
        return path
    ff = ffmpeg_exe()
    if not ff:
        raise RuntimeError("Video nặng hơn 45MB và không có ffmpeg để nén")
    # Bitrate mục tiêu (kbps): tổng = video + nhạc.
    if duration and duration > 0:
        total_kbps = int(MAX_VIDEO_BYTES * 0.90 * 8 / duration / 1000)
        audio_kbps = 96
        video_kbps = max(200, total_kbps - audio_kbps)
    else:
        video_kbps = 1000
        audio_kbps = 128
    for height in (720, 480):
        out = os.path.join(tmpdir, f"video_{height}p.mp4")
        vb = video_kbps if height == 720 else max(200, video_kbps * 2 // 3)
        cmd = [ff, "-y", "-i", path,
               "-vf", f"scale=-2:min({height}\\,ih)",
               "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", f"{vb}k", "-maxrate", f"{vb * 3 // 2}k",
               "-bufsize", f"{vb * 3}k",
               "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", f"{audio_kbps}k",
               "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and os.path.exists(out):
            size = os.path.getsize(out)
            if size <= MAX_VIDEO_BYTES:
                try:
                    os.remove(path)
                except OSError:
                    pass
                return out
            log(f"Nén {height}p vẫn nặng {size} byte")
            try:
                os.remove(out)
            except OSError:
                pass
        else:
            log(f"ffmpeg nén {height}p lỗi: {(r.stderr or '').strip()[-200:]}")
    raise RuntimeError("Video vẫn nặng hơn 45MB sau khi nén")


def upload_tiktok_video(chat_id, video_url, caption="", duration=0):
    """Tải video TikTok về server rồi upload file lên Telegram.
    Link CDN TikTok chặn cả Telegram tải trực tiếp (403 khi không có UA
    trình duyệt) nên phải tải qua server. File nặng hơn 45MB thì nén lại.
    Raise nếu vẫn lỗi — caller tự fallback sang gửi link."""
    tmpdir = None
    try:
        path, tmpdir = download_video_file(video_url)
        path = reencode_fit(path, tmpdir, duration)
        tg_multipart_video(chat_id, path, "video.mp4", caption)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def send_from_tikwm(chat_id, data, url):
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
        save_download(chat_id, orig=url, platform="TikTok",
                      music=data.get("music"))
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
        # Tải file về server rồi upload lại: link CDN TikTok chặn request
        # không có UA trình duyệt (403), Telegram cũng không tải được link
        # đó nên gửi bằng link hay lỗi. File nặng hơn 45MB thì nén lại.
        # Chỉ ghi nhận đã gửi video khi thực sự gửi được — nếu cả 2 cách
        # đều lỗi thì để caller thử cách khác (yt-dlp), đừng báo xong giả.
        video_sent = False
        try:
            upload_tiktok_video(chat_id, play,
                                "" if "anh" in sent else caption, dur)
            video_sent = True
        except Exception as e:  # noqa: BLE001 - dự phòng: gửi bằng link
            log(f"Tải/upload video TikWM lỗi: {e}")
        if not video_sent:
            try:
                send_video(chat_id, play, "" if "anh" in sent else caption)
                video_sent = True
            except Exception:  # noqa: BLE001 - gửi link trực tiếp
                send(chat_id, "Gửi video lỗi, thử lại bằng link tải trực tiếp:\n"
                              f"{play}")
        if video_sent:
            sent.add("video")
        if data.get("music"):
            send_music(data["music"])
        hd = data.get("hdplay")
        sd = data.get("play")
        save_download(chat_id, orig=url, platform="TikTok",
                      music=data.get("music"), hd=hd,
                      sd=sd if sd and sd != hd else None)
        return sent

    # Chỉ còn nhạc -> gửi file âm thanh.
    if data.get("music"):
        send_music_file(chat_id, data["music"], caption)
        sent.add("nhac")
        with LOCK:
            LAST_MUSIC[chat_id] = data["music"]
        save_download(chat_id, orig=url, platform="TikTok",
                      music=data["music"])
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
        save_download(chat_id, orig=url, platform="TikTok", music=music)
        return sent
    # Video: yt-dlp lấy link mp4 có cả hình + tiếng; lỗi thì lấy link
    # video từ HTML trang (TikTok hay chặn yt-dlp lúc cao điểm).
    try:
        info = ytdlp_info(url)
    except Exception as e:  # noqa: BLE001 - fallback: lấy link video từ HTML
        log(f"yt-dlp lỗi, thử lấy link video từ HTML trang: {e}")
        vurl = extract_video_url(html)
        if not vurl:
            raise
        send_video(chat_id, vurl, "🎬 Video TikTok")
        sent.add("video")
        music = extract_music_url(html)
        if music:
            try:
                send_music_file(chat_id, music, "🎵 Nhạc nền")
                sent.add("nhac")
                with LOCK:
                    LAST_MUSIC[chat_id] = music
            except Exception as e2:  # noqa: BLE001 - nhạc lỗi thì vẫn còn video
                log(f"Gửi nhạc lỗi: {e2}")
        save_download(chat_id, orig=url, platform="TikTok", music=music)
        return sent
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
    # Tải video bằng yt-dlp rồi upload file lên Telegram: link CDN TikTok
    # hay chặn cả Telegram lẫn server tải trực tiếp, nên gửi bằng link
    # thường lỗi. Hết cách thì raise để báo lỗi kèm nút hỗ trợ.
    path = tmpdir = None
    video_sent = False
    try:
        path, tmpdir = download_merged(url, 720)
    except Exception as e:  # noqa: BLE001 - thử 480p
        log(f"yt-dlp tải TikTok 720p lỗi: {e}, thử 480p")
        try:
            path, tmpdir = download_merged(url, 480)
        except Exception as e2:  # noqa: BLE001 - báo lỗi gọn cho người dùng
            log(f"yt-dlp tải TikTok 480p lỗi: {e2}")
    if path:
        try:
            tg_multipart_video(chat_id, path, "video.mp4",
                               "" if "anh" in sent else caption)
            video_sent = True
        except Exception as e:  # noqa: BLE001 - thử gửi bằng link
            log(f"Upload video TikTok lỗi: {e}")
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
    if not video_sent:
        try:
            send_video(chat_id, video_url, "" if "anh" in sent else caption)
            video_sent = True
        except Exception as e:  # noqa: BLE001 - hết cách thì báo lỗi
            log(f"Gửi video TikTok bằng link lỗi: {e}")
    if not video_sent:
        raise RuntimeError("không gửi được video TikTok")
    sent.add("video")
    audio = best_audio_url(info)
    sd = smallest_video_url(info)
    save_download(chat_id, orig=url, platform="TikTok", music=audio,
                  hd=video_url, sd=sd if sd and sd != video_url else None)
    if audio:
        try:
            send_music_file(chat_id, audio, "🎵 Nhạc nền của video")
            sent.add("nhac")
            with LOCK:
                LAST_MUSIC[chat_id] = audio
        except Exception as e:  # noqa: BLE001 - nhạc lỗi thì vẫn còn video
            log(f"Gửi nhạc lỗi: {e}")
    return sent


# ---------------------------------------------------------------- Douyin
# Tải Douyin bằng trang chia sẻ iesdouyin (SSR): trang đã render sẵn dữ
# liệu video trong window._ROUTER_DATA nên không cần đăng nhập, không cần
# chữ ký X-Bogus/A-Bogus như API web của douyin.com (API đó đòi cookie
# tươi + chữ ký, yt-dlp cũng báo "Fresh cookies are needed").
# Lần đọc đầu tiên chỉ để lấy cookie khách ttwid (Douyin tự cấp), lần
# đọc sau mới có item_list. Giữ 1 cookie jar dùng chung để đỡ phải lấy
# lại cookie mỗi link (trang chia sẻ dễ bị giới hạn theo IP nếu gọi dồn).
DOUYIN_CJ = http.cookiejar.CookieJar()
DOUYIN_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(DOUYIN_CJ))
DOUYIN_REFERER = "https://www.douyin.com/"


def _douyin_find_item(obj):
    """Tìm item đầu tiên trong JSON _ROUTER_DATA (tìm đệ quy theo khóa
    item_list, vì tên khóa trang video/note có thể khác nhau)."""
    if isinstance(obj, dict):
        items = obj.get("item_list")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        for v in obj.values():
            found = _douyin_find_item(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _douyin_find_item(v)
            if found is not None:
                return found
    return None


def _douyin_parse_router_data(html):
    """Trích JSON window._ROUTER_DATA khỏi HTML trang chia sẻ."""
    idx = html.find("window._ROUTER_DATA")
    if idx < 0:
        return None
    brace = html.find("{", idx)
    if brace < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[brace:])
        return data
    except Exception as e:  # noqa: BLE001 - caller thử lại / báo lỗi gọn
        log(f"Đọc _ROUTER_DATA Douyin lỗi: {e}")
        return None


def resolve_douyin_id(url):
    """Từ link Douyin (link ngắn v.douyin.com, link video/note, link có
    modal_id) lấy (video_id, kind) với kind là 'video' hoặc 'note'."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    modal = (qs.get("modal_id") or [None])[0]
    if modal and str(modal).isdigit():
        return str(modal), "video"
    m = re.search(r"/(video|note)/(\d+)", parsed.path or "")
    if m:
        return m.group(2), m.group(1)
    # Link ngắn / link chia sẻ: đi theo redirect để lấy URL đầy đủ.
    req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
    with DOUYIN_OPENER.open(req, timeout=20) as r:
        final = r.geturl()
        r.read(1024)
    fparsed = urllib.parse.urlparse(final)
    fqs = urllib.parse.parse_qs(fparsed.query)
    modal = (fqs.get("modal_id") or [None])[0]
    if modal and str(modal).isdigit():
        return str(modal), "video"
    m = re.search(r"/(?:share/)?(video|note)/(\d+)", fparsed.path or "")
    if m:
        return m.group(2), m.group(1)
    m = re.search(r"/(\d{15,25})(?:/|$)", fparsed.path or "")
    if m:
        return m.group(1), "video"
    raise RuntimeError(f"không lấy được mã video Douyin từ link: {final[:120]}")


def fetch_douyin_item(video_id, kind="video"):
    """Đọc trang chia sẻ iesdouyin, trả dict item của video/bài ảnh.
    Thử cả share/video và share/note, mỗi trang tối đa 3 lần (lần đầu
    thường chỉ để lấy cookie ttwid nên chưa có dữ liệu)."""
    kinds = [kind, "note" if kind == "video" else "video"]
    last_err = None
    for k in kinds:
        share_url = f"https://www.iesdouyin.com/share/{k}/{video_id}/"
        for i in range(3):
            try:
                req = urllib.request.Request(
                    share_url, headers={"User-Agent": MOBILE_UA})
                with DOUYIN_OPENER.open(req, timeout=25) as r:
                    html = r.read().decode("utf-8", "ignore")
                data = _douyin_parse_router_data(html)
                item = _douyin_find_item(data) if data else None
                if item:
                    return item
                last_err = "trang chia sẻ chưa có dữ liệu video"
                log(f"Douyin {k} lần {i + 1} chưa có item (len={len(html)})")
            except Exception as e:  # noqa: BLE001 - thử lại / đổi loại trang
                last_err = e
                log(f"Đọc trang Douyin {k} lần {i + 1} lỗi: {e}")
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"không đọc được dữ liệu Douyin: {last_err}")


def douyin_cover_url(item):
    video = item.get("video") or {}
    for key in ("cover", "origin_cover", "dynamic_cover"):
        urls = (video.get(key) or {}).get("url_list") or []
        if urls:
            return urls[0]
    return None


def douyin_image_urls(item):
    """Link ảnh của bài đăng ảnh Douyin (tối đa 10, đã lọc trùng)."""
    candidates = []
    if isinstance(item.get("images"), list):
        candidates.extend(item["images"])
    info = item.get("image_post_info") or {}
    if isinstance(info.get("images"), list):
        candidates.extend(info["images"])
    out, seen = [], set()
    for entry in candidates:
        url = None
        if isinstance(entry, dict):
            urls = entry.get("url_list") or []
            if not urls:
                for sub in ("display_image", "thumbnail", "download_url"):
                    urls = (entry.get(sub) or {}).get("url_list") or []
                    if urls:
                        break
            if urls:
                url = urls[0]
        elif isinstance(entry, str) and entry.startswith("http"):
            url = entry
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out[:10]


def douyin_play_url(uri, ratio="720p"):
    return (f"https://aweme.snssdk.com/aweme/v1/play/"
            f"?video_id={uri}&ratio={ratio}&line=0")


def douyin_play_candidates(item):
    """Các link phát Douyin xếp tốt -> nhẹ: bản gốc (default), các mức
    bit_rate nếu có, rồi 720p/540p. Đã đổi playwm -> play (không logo)."""
    video = item.get("video") or {}
    play_addr = video.get("play_addr") or {}
    uri = play_addr.get("uri")
    cands, seen = [], set()

    def add(label, url):
        if url:
            url = url.replace("playwm", "play")
            if url not in seen:
                seen.add(url)
                cands.append((label, url))

    if uri:
        add("gốc", douyin_play_url(uri, "default"))
    rates = [b for b in (video.get("bit_rate") or []) if isinstance(b, dict)]
    rates.sort(key=lambda b: b.get("bit_rate") or 0, reverse=True)
    for b in rates:
        urls = (b.get("play_addr") or {}).get("url_list") or []
        if urls:
            add(f"{b.get('height') or '?'}p", urls[0])
    for u in play_addr.get("url_list") or []:
        add("720p", u)
    if uri:
        add("720p", douyin_play_url(uri, "720p"))
        add("540p", douyin_play_url(uri, "540p"))
    return cands


def probe_remote_size(url):
    """Dung lượng file từ xa (byte) qua request Range 1 byte, None nếu
    không đọc được. Dùng để chọn bản vừa giới hạn 45MB của Telegram."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": MOBILE_UA, "Referer": DOUYIN_REFERER,
            "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read(1)
            crange = r.headers.get("Content-Range") or ""
            if "/" in crange:
                total = crange.rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    return int(total)
            clen = r.headers.get("Content-Length")
            if clen and str(clen).isdigit():
                return int(clen)
    except Exception as e:  # noqa: BLE001 - không đọc được thì tải thử
        log(f"Đo dung lượng Douyin lỗi: {e}")
    return None


def download_douyin_file(url, max_bytes=120_000_000):
    """Tải file video Douyin về thư mục tạm, trả (đường dẫn, thư mục).
    CDN Douyin cần UA trình duyệt + Referer douyin.com."""
    tmpdir = tempfile.mkdtemp(prefix="timenow_douyin_")
    path = os.path.join(tmpdir, "video.mp4")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": MOBILE_UA, "Referer": DOUYIN_REFERER})
        total = 0
        with urllib.request.urlopen(req, timeout=180) as r, \
                open(path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("Video nặng hơn giới hạn tải")
                f.write(chunk)
        if total == 0:
            raise RuntimeError("file video Douyin rỗng")
        return path, tmpdir
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def extract_mp3_from_video(video_path, tmpdir):
    """Tách nhạc MP3 từ file video bằng ffmpeg. Trả (đường dẫn, tên file)
    hoặc (None, None) nếu không tách được (video không có tiếng...)."""
    ff = ffmpeg_exe()
    if not ff:
        return None, None
    out = os.path.join(tmpdir, "nhac.mp3")
    cmd = [ff, "-y", "-i", video_path, "-vn", "-c:a", "libmp3lame",
           "-b:a", "128k", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        return out, "nhac.mp3"
    log(f"Tách MP3 Douyin lỗi: {(r.stderr or '').strip()[-200:]}")
    out2 = os.path.join(tmpdir, "nhac.m4a")
    cmd2 = [ff, "-y", "-i", video_path, "-vn", "-c:a", "aac",
            "-b:a", "128k", out2]
    r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=180)
    if r2.returncode == 0 and os.path.exists(out2) and os.path.getsize(out2) > 0:
        return out2, "nhac.m4a"
    return None, None


def douyin_send_music_by_uri(chat_id, uri):
    """Tải lại bản 720p rồi tách MP3 gửi cho người dùng (nút lấy nhạc /
    nhắn 'nhac' cho link Douyin vừa tải). Trả True nếu gửi được."""
    tmpdir = None
    try:
        path, tmpdir = download_douyin_file(douyin_play_url(uri, "720p"))
        mp3, fname = extract_mp3_from_video(path, tmpdir)
        if not mp3:
            return False
        tg_multipart_audio(chat_id, mp3, fname, "🎵 Nhạc nền của video")
        return True
    except Exception as e:  # noqa: BLE001 - caller báo lỗi gọn
        log(f"Gửi lại nhạc Douyin lỗi: {e}")
        return False
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def douyin_send_sd_by_uri(chat_id, uri):
    """Tải bản thường 540p của Douyin rồi upload (nút Bản thường)."""
    tmpdir = None
    try:
        path, tmpdir = download_douyin_file(douyin_play_url(uri, "540p"))
        path = reencode_fit(path, tmpdir, 0)
        tg_multipart_video(chat_id, path, "video.mp4",
                           "📥 Bản thường (nhẹ hơn, tải nhanh hơn)")
        return True
    except Exception as e:  # noqa: BLE001 - caller báo lỗi gọn
        log(f"Tải bản thường Douyin lỗi: {e}")
        return False
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def handle_douyin(chat_id, url):
    send(chat_id, "⏳ Đang tải Douyin cho Huy đây, chờ mình vài giây nhé...")
    try:
        video_id, kind = resolve_douyin_id(url)
        item = fetch_douyin_item(video_id, kind)
    except Exception as e:  # noqa: BLE001 - báo lỗi gọn cho người dùng
        log(f"Tải Douyin thất bại: {e}")
        send_error(chat_id, "Không tải được link Douyin này, thử lại sau "
                            "vài phút nhé. (Chỉ tải được video công khai, "
                            "một số video bị khóa theo khu vực.)",
                   failed_url=url, platform="Douyin")
        return
    author = item.get("author") or {}
    nick = author.get("nickname") or "Douyin"
    unique = author.get("unique_id") or author.get("short_id") or ""
    title = (item.get("desc") or "").strip()
    video = item.get("video") or {}
    dur = int((video.get("duration") or 0) // 1000)
    stats = item.get("statistics") or {}
    caption = f"🎬 Douyin • @{unique} ({nick})\n{title}"
    if dur:
        caption += f"\n⏱ {dur // 60}:{dur % 60:02d}" if dur >= 60 else f"\n⏱ {dur}s"
        caption += (f" • ❤️ {fmt_num(stats.get('digg_count'))}"
                    f" • 💬 {fmt_num(stats.get('comment_count'))}")
    uri = (video.get("play_addr") or {}).get("uri")
    sent = set()
    # Bài đăng ảnh -> gửi từng ảnh.
    images = douyin_image_urls(item)
    if images and not uri:
        ok = False
        for i, img in enumerate(images):
            try:
                send_photo(chat_id, img, caption if i == 0 else "")
                ok = True
            except Exception as e:  # noqa: BLE001 - ảnh lỗi thì gửi ảnh khác
                log(f"Gửi ảnh Douyin {i} lỗi: {e}")
        if not ok:
            send_error(chat_id, "Không tải được bài ảnh Douyin này, thử "
                                "lại sau vài phút nhé.",
                       failed_url=url, platform="Douyin")
            return
        sent.add("anh")
        save_download(chat_id, orig=url, platform="Douyin", music=None,
                      douyin_uri=None, douyin_id=video_id, douyin_kind=kind)
        bump_stat("Douyin")
        record_download(chat_id, "Douyin")
        notify_admin_download(chat_id, "Douyin", url)
        send_done(chat_id, sent, url)
        return
    # Video -> ảnh bìa trước, rồi file video, rồi nhạc tách từ video.
    cover = douyin_cover_url(item)
    if cover:
        try:
            send_photo(chat_id, cover, caption)
            sent.add("anh")
        except Exception as e:  # noqa: BLE001 - bìa lỗi thì vẫn gửi video
            log(f"Gửi ảnh bìa Douyin lỗi: {e}")
    cands = douyin_play_candidates(item)
    if not cands:
        send_error(chat_id, "Không tải được link Douyin này, thử lại sau "
                            "vài phút nhé.",
                   failed_url=url, platform="Douyin")
        return
    # Chọn bản tốt nhất vừa 45MB; không đo được dung lượng thì tải thử
    # bản đầu tiên rồi nén lại nếu nặng quá.
    chosen = cands[0][1]
    for _label, cand in cands:
        size = probe_remote_size(cand)
        if size is not None and size <= MAX_VIDEO_BYTES:
            chosen = cand
            break
        if size is not None:
            chosen = cand  # vẫn nặng: giữ bản nhẹ hơn ở vòng sau / nén lại
    tmpdir = None
    video_sent = False
    try:
        path, tmpdir = download_douyin_file(chosen)
        path = reencode_fit(path, tmpdir, dur)
        tg_multipart_video(chat_id, path, "video.mp4",
                           "" if "anh" in sent else caption)
        video_sent = True
        sent.add("video")
        # Tách nhạc MP3 từ chính file vừa tải (Douyin không cho link nhạc
        # riêng trong dữ liệu SSR như TikWM).
        try:
            mp3, fname = extract_mp3_from_video(path, tmpdir)
            if mp3:
                tg_multipart_audio(chat_id, mp3, fname,
                                   "🎵 Nhạc nền của video")
                sent.add("nhac")
        except Exception as e:  # noqa: BLE001 - nhạc lỗi thì vẫn còn video
            log(f"Tách/gửi nhạc Douyin lỗi: {e}")
    except Exception as e:  # noqa: BLE001 - thử gửi bằng link dự phòng
        log(f"Tải/upload video Douyin lỗi: {e}")
        try:
            send_video(chat_id, chosen, "" if "anh" in sent else caption)
            video_sent = True
            sent.add("video")
        except Exception as e2:  # noqa: BLE001 - hết cách thì báo lỗi
            log(f"Gửi video Douyin bằng link lỗi: {e2}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if not video_sent:
        send_error(chat_id, "Không tải được video Douyin này (nặng quá "
                            "hoặc link lỗi), thử lại sau vài phút nhé.",
                   failed_url=url, platform="Douyin")
        return
    save_download(chat_id, orig=url, platform="Douyin", music=None,
                  douyin_uri=uri, douyin_id=video_id, douyin_kind=kind)
    bump_stat("Douyin")
    record_download(chat_id, "Douyin")
    notify_admin_download(chat_id, "Douyin", url)
    send_done(chat_id, sent, url)


def handle_generic(chat_id, url, platform):
    """Tải video YouTube / Instagram / Facebook / X: tải + gộp rồi upload file.
    (Các nền tảng này chỉ có luồng hình và tiếng riêng, không có sẵn file gộp.)"""
    send(chat_id, f"⏳ Đang tải {platform} cho Huy đây, chờ mình khoảng 1 phút nhé...")
    try:
        info = ytdlp_info(url)
    except Exception as e:  # noqa: BLE001 - báo lỗi gọn cho người dùng
        log(f"yt-dlp {platform} thất bại: {e}")
        send_error(chat_id, f"Không tải được link {platform} này, thử lại sau "
                            "vài phút nhé. (Chỉ tải được video công khai.)",
                   failed_url=url, platform=platform)
        return
    title = (info.get("title") or "").strip()
    uploader = info.get("uploader") or ""
    dur = info.get("duration") or 0
    caption = f"🎬 {platform} • @{uploader}\n{title}"
    if dur:
        caption += f"\n⏱ {dur // 60}:{dur % 60:02d}"
    sent = set()
    thumb = info.get("thumbnail")
    if thumb:
        try:
            send_photo(chat_id, thumb, caption)
            sent.add("anh")
        except Exception as e:  # noqa: BLE001 - bìa lỗi thì vẫn gửi video
            log(f"Gửi ảnh bìa lỗi: {e}")
    # Tải + gộp hình/tiếng thành 1 file mp4, thử 720p rồi 480p nếu nặng quá.
    path = tmpdir = None
    try:
        path, tmpdir = download_merged(url, 720)
    except Exception as e:  # noqa: BLE001 - thử 480p
        log(f"Tải {platform} 720p lỗi: {e}, thử 480p")
        try:
            path, tmpdir = download_merged(url, 480)
        except Exception as e2:  # noqa: BLE001 - báo lỗi gọn cho người dùng
            log(f"Tải {platform} 480p lỗi: {e2}")
            send_error(chat_id, f"Không tải được video {platform} này (nặng quá "
                                "hoặc link lỗi), thử link khác nhé.",
                       failed_url=url, platform=platform)
            return
    try:
        tg_multipart_video(chat_id, path, "video.mp4",
                           "" if "anh" in sent else caption)
        sent.add("video")
    except Exception as e:  # noqa: BLE001 - báo lỗi gọn cho người dùng
        log(f"Upload video {platform} lỗi: {e}")
        send_error(chat_id, f"Gửi video {platform} lỗi, thử lại sau nhé.",
                   failed_url=url, platform=platform)
        return
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    audio = best_audio_url(info)
    save_download(chat_id, orig=url, platform=platform, music=audio)
    if audio:
        try:
            send_music_file(chat_id, audio, "🎵 Nhạc nền của video")
            sent.add("nhac")
            with LOCK:
                LAST_MUSIC[chat_id] = audio
        except Exception as e:  # noqa: BLE001 - nhạc lỗi thì vẫn còn video
            log(f"Gửi nhạc lỗi: {e}")
    bump_stat(platform)
    record_download(chat_id, platform)
    notify_admin_download(chat_id, platform, url)
    send_done(chat_id, sent, url)


def send_done(chat_id, sent, orig_url):
    """Câu chốt sau khi tải xong: nói rõ đã gửi những gì + nút bấm."""
    parts = []
    if "anh" in sent:
        parts.append("ảnh")
    if "video" in sent:
        parts.append("video")
    if "nhac" in sent:
        parts.append("nhạc MP3")
    if not parts:
        return
    text = ("✅ Xong rồi Huy! Mình gửi " + ", ".join(parts) +
            " ở trên, thích cái nào thì tải cái đó nhé.")
    dl = LAST_DOWNLOAD.get(chat_id) or {}
    row = []
    if dl.get("music") or (dl.get("platform") == "Douyin"
                           and dl.get("douyin_uri")):
        row.append({"text": "🎵 Lấy lại nhạc", "callback_data": "dl:music"})
    # Nền tảng đa luồng (YouTube/IG/FB/X): nút bản thường tải lại bản 480p.
    # Douyin: nút bản thường tải lại bản 540p.
    if dl.get("sd") or dl.get("platform") in ("YouTube", "Instagram",
                                             "Facebook", "X", "Douyin"):
        row.append({"text": "📥 Bản thường", "callback_data": "dl:sd"})
    kb = {"inline_keyboard": []}
    if row:
        kb["inline_keyboard"].append(row)
    if orig_url:
        kb["inline_keyboard"].append(
            [{"text": "🔗 Mở link gốc", "url": orig_url}])
    send_markup(chat_id, text, kb)


def handle_broadcast(admin_id, text):
    """Gửi thông báo tới toàn bộ người dùng đã lưu (trừ admin).
    Người chặn bot (403) được đánh dấu để lần sau bỏ qua."""
    content = text.strip()
    if not content:
        send(admin_id, "Huy gõ: /thongbao <nội dung> — ví dụ:\n"
                       "/thongbao Bot vừa có tính năng mới, thử ngay nhé!")
        return
    with LOCK:
        targets = [int(k) for k, v in USERS.items()
                   if int(k) != admin_id and not v.get("blocked")]
    ok = fail = 0
    for cid in targets:
        try:
            send(cid, content)
            ok += 1
        except Exception as e:  # noqa: BLE001 - lỗi từng người thì đếm, không dừng
            fail += 1
            log(f"Gửi thông báo tới {cid} lỗi: {e}")
            if "403" in str(e) or "blocked" in str(e).lower():
                with LOCK:
                    if str(cid) in USERS:
                        USERS[str(cid)]["blocked"] = True
                        try:
                            save_users(USERS)
                        except Exception:
                            pass
        time.sleep(0.05)
    msg = f"📣 Đã gửi thông báo tới {ok}/{len(targets)} người dùng."
    if fail:
        msg += f" {fail} người gửi lỗi (có thể đã chặn bot)."
    send(admin_id, msg)


def handle_callback(chat_id, data):
    """Xử lý nút bấm dưới tin 'Xong rồi' và nút 'Cần hỗ trợ'."""
    if data == "support":
        # Gửi yêu cầu hỗ trợ tới admin kèm link lỗi (nếu có), rồi chờ người
        # dùng mô tả thêm ở tin nhắn tiếp theo để chuyển tiếp luôn.
        with LOCK:
            failed = LAST_FAILED.get(chat_id) or {}
            SUPPORT_PENDING[chat_id] = failed
        user = (USERS.get(str(chat_id)) or {})
        who = f"@{user['username']}" if user.get("username") else str(chat_id)
        if user.get("first_name"):
            who += f" ({user['first_name']})"
        ticket = (f"🆘 Yêu cầu hỗ trợ từ {who} (chat_id {chat_id})")
        if failed.get("url"):
            ticket += f"\n🔗 Link lỗi: {failed['url']}"
            if failed.get("platform"):
                ticket += f" ({failed['platform']})"
        ticket += ("\n\nTrả lời người này bằng:\n"
                   f"/traloi {chat_id} <nội dung>")
        try:
            send(ADMIN_CHAT_ID, ticket)
        except Exception as e:  # noqa: BLE001 - báo admin lỗi thì vẫn báo user
            log(f"Gửi yêu cầu hỗ trợ tới admin lỗi: {e}")
        send(chat_id, "Đã báo cho admin rồi nhé, bạn chờ chút nha. "
                      "Bạn mô tả thêm lỗi gặp phải ở tin nhắn tiếp theo, "
                      "mình chuyển tiếp luôn cho admin.")
        return
    dl = LAST_DOWNLOAD.get(chat_id) or {}
    if data == "dl:music":
        music = dl.get("music")
        if music:
            try:
                send_music_file(chat_id, music, "🎵 Nhạc nền TikTok")
            except Exception:  # noqa: BLE001 - gửi link thay cho file
                send(chat_id, f"Gửi file nhạc lỗi, Huy bấm link này để tải: {music}")
        elif dl.get("platform") == "Douyin" and dl.get("douyin_uri"):
            send(chat_id, "⏳ Đang tách nhạc Douyin, chờ mình chút nhé...")
            if not douyin_send_music_by_uri(chat_id, dl["douyin_uri"]):
                send(chat_id, "Tách nhạc Douyin lỗi, thử lại sau nhé.")
        else:
            send(chat_id, "Link vừa tải không có nhạc.")
    elif data == "dl:sd":
        sd = dl.get("sd")
        if sd:
            if dl.get("platform") == "TikTok":
                # Link CDN TikTok hay chặn Telegram tải trực tiếp (403),
                # tải về server rồi upload lại, nén nếu nặng hơn 45MB.
                try:
                    upload_tiktok_video(
                        chat_id, sd, "📥 Bản thường (nhẹ hơn, tải nhanh hơn)")
                except Exception as e:  # noqa: BLE001 - gửi bằng link như trước
                    log(f"Tải bản thường TikTok lỗi: {e}")
                    send_video(chat_id, sd,
                               "📥 Bản thường (nhẹ hơn, tải nhanh hơn)")
            else:
                send_video(chat_id, sd, "📥 Bản thường (nhẹ hơn, tải nhanh hơn)")
        elif dl.get("platform") == "Douyin" and dl.get("douyin_uri"):
            send(chat_id, "⏳ Đang tải bản thường Douyin, chờ mình chút nhé...")
            if not douyin_send_sd_by_uri(chat_id, dl["douyin_uri"]):
                send(chat_id, "Tải bản thường Douyin lỗi, thử lại sau nhé.")
        elif dl.get("platform") in ("YouTube", "Instagram", "Facebook", "X") \
                and dl.get("orig"):
            send(chat_id, "⏳ Đang tải bản thường, chờ mình chút nhé...")
            path = tmpdir = None
            try:
                path, tmpdir = download_merged(dl["orig"], 480)
                tg_multipart_video(chat_id, path, "video.mp4",
                                   "📥 Bản thường (480p)")
            except Exception as e:  # noqa: BLE001 - báo lỗi gọn
                log(f"Tải bản thường lỗi: {e}")
                send(chat_id, "Tải bản thường lỗi, thử lại sau nhé.")
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            send(chat_id, "Link vừa tải không có bản thường.")
    else:
        send(chat_id, "Nút này mình chưa hiểu, gửi lại link nhé.")


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
            sent = send_from_tikwm(chat_id, data, url)
        except Exception as e:  # noqa: BLE001 - fallback sang yt-dlp
            log(f"Gửi từ TikWM lỗi, chuyển sang yt-dlp: {e}")
            sent = set()
        # Bài đăng ảnh: gửi được ảnh là đủ. Video: phải gửi được video
        # thì mới xong — TikWM chỉ gửi được nhạc/bìa thì thử yt-dlp.
        is_photo = bool(data.get("images"))
        if "video" in sent or (is_photo and "anh" in sent):
            bump_stat("TikTok")
            record_download(chat_id, "TikTok")
            notify_admin_download(chat_id, "TikTok", url)
            send_done(chat_id, sent, url)
            return
        log("TikWM không gửi được video, chuyển sang yt-dlp")
    # Cách 2: yt-dlp / đọc HTML trang.
    try:
        sent = handle_with_ytdlp(chat_id, url)
    except Exception as e:  # noqa: BLE001 - báo lỗi gọn cho người dùng
        log(f"yt-dlp thất bại: {e}")
        send_error(chat_id, "Không tải được link này, thử lại sau vài phút nhé. "
                            "(Link vẫn công khai, do server tải đang bận.)",
                   failed_url=url, platform="TikTok")
        return
    bump_stat("TikTok")
    record_download(chat_id, "TikTok")
    notify_admin_download(chat_id, "TikTok", url)
    send_done(chat_id, sent, url)


def handle_update(update):
    # ---- Nút bấm inline dưới tin "Xong rồi" ----
    cq = update.get("callback_query")
    if cq:
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if chat_id:
            register_user(chat_id, cq.get("from"))
            try:
                tg("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            except Exception:  # noqa: BLE001 - bấm nút vẫn chạy tiếp
                pass
            handle_callback(chat_id, cq.get("data") or "")
        return

    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return
    register_user(chat_id, msg.get("from"))
    username = (msg.get("from") or {}).get("username", "")
    text = (msg.get("text") or "").strip()
    low = text.lower()

    # ---- /start ----
    if low.startswith("/start"):
        card = ("👋 Chào bạn, mình là bot TimeNow.\n"
                "Gửi link vào đây, mình gửi đủ cả ảnh bìa, video "
                "không logo và nhạc MP3 để bạn thích cái nào thì tải cái đó.\n\n"
                "📱 Tải được: TikTok, Douyin, YouTube, Instagram, Facebook, X\n"
                "📎 Gửi nhiều link trong 1 tin nhắn (tối đa 3) để tải hàng loạt\n"
                "🎵 Nhắn 'nhac' để gửi lại file nhạc của link vừa tải\n"
                "🆘 Gặp sự cố thì bấm nút 'Cần hỗ trợ' hoặc gõ /hotro\n"
                "ℹ️ /help xem hướng dẫn, /thongke xem số link đã tải")
        if chat_id == ADMIN_CHAT_ID:
            card += ("\n\nHuy cứ nhắn câu hỏi, mình trả lời như Muse nhé."
                     "\n📣 /thongbao <nội dung> gửi thông báo tới toàn bộ "
                     "người dùng, /danhsach xem số người dùng đã lưu.")
        send(chat_id, card)
        return

    # ---- /help ----
    if low.startswith("/help"):
        send(chat_id,
             "📖 Hướng dẫn dùng bot TimeNow\n\n"
             "📎 Gửi link video vào đây, bot tải về gửi lại cho bạn:\n"
             "• TikTok, Douyin — ảnh bìa + video không logo + nhạc MP3\n"
             "• YouTube, Instagram, Facebook, X — video + ảnh bìa + nhạc\n"
             "• Gửi nhiều link trong 1 tin nhắn (tối đa 3) để tải hàng loạt\n\n"
             "🔘 Sau khi tải xong, bấm nút dưới tin nhắn để:\n"
             "• Lấy lại file nhạc MP3\n"
             "• Tải bản thường (nhẹ hơn, nhanh hơn)\n"
             "• Mở link gốc\n\n"
             "🎵 Nhắn 'nhac' để gửi lại file nhạc của link vừa tải.\n"
             "📊 /thongke xem số link bot đã tải.\n"
             "🆘 Gặp sự cố khi tải: bấm nút 'Cần hỗ trợ' dưới tin báo lỗi, "
             "hoặc gõ /hotro để nhờ admin giúp.\n\n"
             "Chỉ tải được nội dung công khai thôi nhé.")
        return

    # ---- /hotro ----
    if low.startswith("/hotro"):
        with LOCK:
            SUPPORT_PENDING[chat_id] = LAST_FAILED.get(chat_id) or {}
        send_markup(chat_id,
                    "🆘 Cần hỗ trợ à? Bạn mô tả lỗi gặp phải ở tin nhắn tiếp "
                    "theo nhé, mình chuyển tiếp luôn cho admin.\n\n"
                    "Nhớ kèm link video bị lỗi (nếu có) để admin kiểm tra "
                    "nhanh hơn.",
                    SUPPORT_BUTTON)
        return

    # ---- /thongke ----
    if low.startswith("/thongke"):
        with LOCK:
            total = STATS["total"]
            parts = dict(STATS["platforms"])
        lines = [f"📊 Bot đã tải {total} link."]
        for name, n in sorted(parts.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {name}: {n}")
        lines.append("(Số liệu từ lúc server chạy lại.)")
        send(chat_id, "\n".join(lines))
        return

    # ---- /thongbao (chỉ Huy): gửi thông báo tới toàn bộ người dùng ----
    if low.startswith("/thongbao"):
        if chat_id != ADMIN_CHAT_ID:
            send(chat_id, "Lệnh này chỉ admin dùng được nhé.")
            return
        handle_broadcast(chat_id, text[len("/thongbao"):])
        return

    # ---- /danhsach (chỉ Huy): xem số người dùng đã lưu ----
    if low.startswith("/danhsach"):
        if chat_id != ADMIN_CHAT_ID:
            send(chat_id, "Lệnh này chỉ admin dùng được nhé.")
            return
        with LOCK:
            items = list(USERS.items())
        lines = [f"👥 Bot đã lưu {len(items)} người dùng."]
        for k, v in items[:20]:
            name = f"@{v['username']}" if v.get("username") else k
            if v.get("blocked"):
                name += " (đã chặn bot)"
            n = int(v.get("downloads") or 0)
            plat = v.get("last_platform") or ""
            last = fmt_ago(v.get("last_seen") or 0)
            if n:
                lines.append(f"• {name} — {n} lượt tải ({plat}), "
                             f"hoạt động {last}")
            else:
                lines.append(f"• {name} — chưa tải gì, hoạt động {last}")
        if len(items) > 20:
            lines.append(f"... và {len(items) - 20} người nữa.")
        send(chat_id, "\n".join(lines))
        return

    # ---- /traloi (chỉ Huy): trả lời người dùng đang cần hỗ trợ ----
    if low.startswith("/traloi"):
        if chat_id != ADMIN_CHAT_ID:
            send(chat_id, "Lệnh này chỉ admin dùng được nhé.")
            return
        parts = text.split(None, 2)
        if len(parts) < 3:
            send(chat_id, "Huy gõ: /traloi <chat_id> <nội dung> — ví dụ:\n"
                          "/traloi 123456789 Link này tải được rồi, bạn thử lại nhé!")
            return
        try:
            target = int(parts[1])
        except ValueError:
            send(chat_id, "chat_id phải là số nhé, Huy kiểm tra lại.")
            return
        try:
            send(target, f"💬 Admin TimeNow trả lời bạn:\n{parts[2]}")
            send(chat_id, f"Đã gửi trả lời tới {target}.")
        except Exception as e:  # noqa: BLE001 - người dùng chặn bot thì báo lại
            log(f"Trả lời hỗ trợ tới {target} lỗi: {e}")
            send(chat_id, f"Gửi tới {target} lỗi, có thể người này đã chặn bot.")
        return

    # ---- Lấy nhạc MP3 của video vừa tải ----
    if low in ("nhac", "mp3", "🎵", "nhạc"):
        with LOCK:
            music = LAST_MUSIC.get(chat_id)
            dl_nhac = dict(LAST_DOWNLOAD.get(chat_id) or {})
        if music:
            try:
                send_music_file(chat_id, music, "🎵 Nhạc nền TikTok")
            except Exception:  # noqa: BLE001 - gửi link thay cho file
                send(chat_id, f"Gửi file nhạc lỗi, Huy bấm link này để tải: {music}")
        elif dl_nhac.get("platform") == "Douyin" and dl_nhac.get("douyin_uri"):
            send(chat_id, "⏳ Đang tách nhạc Douyin, chờ mình chút nhé...")
            if not douyin_send_music_by_uri(chat_id, dl_nhac["douyin_uri"]):
                send(chat_id, "Tách nhạc Douyin lỗi, thử lại sau nhé.")
        else:
            send(chat_id, "Bạn chưa tải video nào, gửi link trước nhé.")
        return

    # ---- Có link trong tin nhắn (tối đa 3 link 1 lần) ----
    urls = [u.rstrip(").,") for u in URL_RE.findall(text)][:3]
    if urls:
        if len(urls) > 1:
            send(chat_id, f"Tìm thấy {len(urls)} link, mình tải từng cái nhé...")
        for url in urls:
            host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
            platform = platform_of(host)
            if platform == "TikTok":
                handle_tiktok(chat_id, url)
            elif platform == "Douyin":
                handle_douyin(chat_id, url)
            elif platform:
                handle_generic(chat_id, url, platform)
            else:
                send(chat_id, "Link này bot chưa tải được, hiện tải được "
                              "TikTok, Douyin, YouTube, Instagram, "
                              "Facebook, X nhé.")
        return

    # ---- Người dùng đang cần hỗ trợ: chuyển tiếp tin nhắn cho admin ----
    # (Chỉ áp dụng với người thường, không áp dụng với Huy vì Huy chat với
    # bot là để hỏi AI; và chỉ chuyển tin chữ, link thì vẫn đi luồng tải.)
    if chat_id != ADMIN_CHAT_ID and text and not urls:
        with LOCK:
            pending = SUPPORT_PENDING.pop(chat_id, None)
        if pending is not None:
            user = (USERS.get(str(chat_id)) or {})
            who = f"@{user['username']}" if user.get("username") else str(chat_id)
            if user.get("first_name"):
                who += f" ({user['first_name']})"
            fwd = (f"🆘 {who} (chat_id {chat_id}) mô tả thêm:\n{text}")
            if pending.get("url"):
                fwd += f"\n🔗 Link lỗi: {pending['url']}"
            fwd += (f"\n\nTrả lời bằng:\n/traloi {chat_id} <nội dung>")
            try:
                send(ADMIN_CHAT_ID, fwd)
                send(chat_id, "Đã chuyển mô tả của bạn cho admin rồi, "
                              "bạn chờ admin trả lời nhé.")
            except Exception as e:  # noqa: BLE001 - gửi admin lỗi thì báo lại
                log(f"Chuyển tiếp hỗ trợ lỗi: {e}")
                send(chat_id, "Gửi cho admin lỗi, bạn thử lại sau nhé.")
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
        send(chat_id, "Gửi link TikTok, Douyin, YouTube, Instagram, "
                      "Facebook hoặc X vào đây, mình tải video/ảnh/nhạc "
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
