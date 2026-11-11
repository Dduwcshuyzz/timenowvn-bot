#!/usr/bin/env python3
"""Webhook server cho bot Telegram @timenowvn_bot — trả lời trong vài giây.

Cách hoạt động: Telegram đẩy tin nhắn tới server ngay khi khách nhắn
(webhook), server xử lý lệnh shop ngay tại chỗ và chỉ gọi AI khi cần
trả lời câu hỏi. Không còn chờ lịch kiểm tra 1 phút như bản cron.

Chạy:  python3 webhook_server.py   (mặc định cổng 8080)
Biến môi trường bắt buộc:
  TELEGRAM_BOT_TOKEN  - token bot lấy từ @BotFather
  AI_API_KEY          - key AI (OpenAI / Gemini / Groq... tương thích OpenAI)
Tùy chọn:
  AI_API_BASE - mặc định https://api.openai.com/v1
  AI_MODEL    - mặc định gpt-4o-mini
  ADMIN_CHAT_ID - mặc định 7223454879 (Huy)
  PORT        - mặc định 8080
  SHOP_DIR    - thư mục chứa shop.json, shop_stock.json (mặc định cùng thư mục file này)

Sau khi server chạy có địa chỉ https công khai, đặt webhook:
  curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<địa-chỉ-của-bạn>/webhook"
Xóa webhook (quay về polling): setWebhook?url=
"""
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
SHOP_DIR = os.environ.get("SHOP_DIR", BASE)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AI_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE = os.environ.get("AI_API_BASE", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "7223454879"))
PORT = int(os.environ.get("PORT", "8080"))
LOCK = threading.Lock()

SHOP_FILE = os.path.join(SHOP_DIR, "shop.json")
STOCK_FILE = os.path.join(SHOP_DIR, "shop_stock.json")

SYSTEM_PROMPT = (
    "Bạn là Muse, trợ lý của Huy (Le Duc Huy, CEO VietMess.Com, "
    "làm web TimeNow.vn tải video, kênh TikTok/YouTube 'Chuyện lạ / Bí ẩn 60 giây', "
    "app học tiếng Đức A1-C2, game Sinh Tồn Hà Nội). Trả lời bằng tiếng Việt, "
    "ngắn gọn nhất có thể, đúng giọng Muse, không bịa thông tin chưa kiểm chứng."
)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def tg(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def send(chat_id, text):
    # Telegram giới hạn 4096 ký tự / tin
    for i in range(0, len(text), 4000):
        tg("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4000]})


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


def fmt_money(n):
    return f"{n:,}".replace(",", ".")


def personal_card(chat_id, username, balance, extra=""):
    return (f"👤 THÔNG TIN CÁ NHÂN\n"
            f"UID: {chat_id}\n"
            f"Username: @{username}\n"
            f"Số dư: {fmt_money(balance)}đ\n"
            f"Thông báo gia hạn: Bật{extra}")


def handle_update(update):
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return
    username = (msg.get("from") or {}).get("username", "")
    text = (msg.get("text") or "").strip()
    low = text.lower()
    has_photo = bool(msg.get("photo"))

    with LOCK:
        shop = load_json(SHOP_FILE, {"products": [], "orders": [],
                                     "balances": {}, "topups": [],
                                     "payment": {}})
        stock = load_json(STOCK_FILE, {})
        balance = shop.get("balances", {}).get(str(chat_id), 0)
        payment = shop.get("payment", {})
        bank = payment.get("bank", {})
        momo = payment.get("momo", {})

        def save():
            save_json(SHOP_FILE, shop)
            save_json(STOCK_FILE, stock)

        def product(pid):
            for p in shop.get("products", []):
                if str(p.get("id")) == str(pid):
                    return p
            return None

        def deliver(order, buyer_id, buyer_name):
            """Lấy 1 tài khoản khỏi kho và giao cho người mua. Trả True nếu giao được."""
            pid = str(order["product_id"])
            items = stock.get(pid, [])
            if not items:
                return False
            acc = items.pop(0)
            p = product(pid)
            if p:
                p["stock"] = max(0, int(p.get("stock", 0)) - 1)
            order["status"] = "da_giao"
            save()
            lines = [f"🎉 Giao hàng đơn DH{order['order_id']}: {order['name']}"]
            for k, label in [("username", "Tài khoản"), ("password", "Mật khẩu"),
                             ("mail", "Mail"), ("mail_password", "Mật khẩu mail"),
                             ("twofa", "Mã 2FA")]:
                if acc.get(k):
                    lines.append(f"{label}: {acc[k]}")
            send(buyer_id, "\n".join(lines))
            send(ADMIN_CHAT_ID,
                 f"Đã giao đơn DH{order['order_id']} cho @{buyer_name}: {order['name']}")
            return True

        # ---- /start, /taikhoan ----
        if low.startswith("/start"):
            card = personal_card(chat_id, username, balance)
            card += "\n\nNhắn /shop để xem hàng, /nap để nạp tiền."
            if chat_id == ADMIN_CHAT_ID:
                card += "\nHuy cứ nhắn câu hỏi, mình trả lời như Muse nhé."
            send(chat_id, card)
            return
        if low.startswith("/taikhoan"):
            n = sum(1 for o in shop.get("orders", [])
                    if str(o.get("buyer_chat_id")) == str(chat_id))
            send(chat_id, personal_card(chat_id, username, balance,
                                        f"\nSố đơn đã mua: {n}"))
            return
        # ---- /lichsu ----
        if low.startswith("/lichsu"):
            mine = [o for o in shop.get("orders", [])
                    if str(o.get("buyer_chat_id")) == str(chat_id)]
            if not mine:
                send(chat_id, "Bạn chưa mua gì.")
            else:
                st = {"cho_thanh_toan": "Chờ thanh toán", "cho_duyet": "Chờ duyệt",
                      "da_giao": "Đã giao"}
                lines = [f"DH{o['order_id']} - {o['name']} - "
                         f"{fmt_money(o['price'])}đ - {st.get(o['status'], o['status'])}"
                         for o in mine[-10:]]
                send(chat_id, "\n".join(lines))
            return
        # ---- /nap ----
        if low.startswith("/nap"):
            send(chat_id,
                 f"Nạp tiền vào số dư:\n"
                 f"Techcombank {bank.get('account_number', '')} - "
                 f"{bank.get('account_holder', '')}\n"
                 f"Momo {momo.get('phone', '')}\n"
                 f"Nội dung chuyển khoản: NAP{chat_id}\n"
                 f"Chuyển xong nhắn 'da nap <số tiền>' để shop cộng tiền vào số dư.")
            return
        if low.startswith("da nap"):
            parts = low.replace("da nap", "").strip().replace(".", "").replace(",", "")
            try:
                amount = int("".join(ch for ch in parts if ch.isdigit()))
            except ValueError:
                amount = 0
            if amount <= 0:
                send(chat_id, "Huy ghi số tiền chưa rõ, nhắn lại kiểu: da nap 100000")
                return
            shop.setdefault("topups", []).append(
                {"chat_id": chat_id, "username": username,
                 "amount": amount, "status": "cho_duyet"})
            save()
            send(chat_id, f"Đã nhận yêu cầu nạp {fmt_money(amount)}đ, chờ shop duyệt.")
            send(ADMIN_CHAT_ID,
                 f"Nạp tiền mới: @{username} (chat {chat_id}) nạp {fmt_money(amount)}đ. "
                 f"Duyệt bằng /congtien {chat_id} {amount}.")
            return
        if low.startswith("/congtien") and chat_id == ADMIN_CHAT_ID:
            parts = text.split()
            if len(parts) >= 3:
                try:
                    target, amount = int(parts[1]), int(parts[2])
                except ValueError:
                    send(chat_id, "Sai cú pháp. Dùng: /congtien <chat_id> <số tiền>")
                    return
                shop.setdefault("balances", {})
                shop["balances"][str(target)] = \
                    shop["balances"].get(str(target), 0) + amount
                for t in shop.get("topups", []):
                    if str(t.get("chat_id")) == str(target) and \
                            t.get("status") == "cho_duyet":
                        t["status"] = "da_duyet"
                save()
                bal = shop["balances"][str(target)]
                send(chat_id, f"Đã cộng {fmt_money(amount)}đ cho chat {target}. "
                              f"Số dư mới: {fmt_money(bal)}đ.")
                send(target, f"Đã cộng {fmt_money(amount)}đ vào số dư của bạn. "
                             f"Số dư hiện tại: {fmt_money(bal)}đ.")
            return
        # ---- /shop ----
        if low == "/shop" or low == "shop":
            lines = ["🛒 Cửa hàng đồ số:"]
            for p in shop.get("products", []):
                lines.append(f"{p['id']}. {p['name']} - "
                             f"{fmt_money(p['price'])}đ - còn {p.get('stock', 0)}")
            lines.append("Mua: nhắn /mua 1 hoặc /mua 2")
            send(chat_id, "\n".join(lines))
            return
        # ---- /mua ----
        if low.startswith("/mua") or low.startswith("mua "):
            parts = text.split()
            pid = parts[1] if len(parts) > 1 else ""
            p = product(pid)
            if not p or int(p.get("stock", 0)) <= 0:
                send(chat_id, "Món này hết hàng hoặc không tồn tại.")
                return
            price = int(p["price"])
            orders = shop.setdefault("orders", [])
            oid = max([o.get("order_id", 0) for o in orders], default=0) + 1
            if balance >= price:
                shop["balances"][str(chat_id)] = balance - price
                order = {"order_id": oid, "buyer_chat_id": chat_id,
                         "buyer_username": username, "product_id": p["id"],
                         "name": p["name"], "price": price, "status": "cho_duyet"}
                orders.append(order)
                if not deliver(order, chat_id, username):
                    shop["balances"][str(chat_id)] = balance  # hoàn tiền
                    save()
                    send(chat_id, "Shop đang chuẩn bị hàng, sẽ giao ngay cho bạn.")
                    send(ADMIN_CHAT_ID,
                         f"Kho tài khoản món {p['name']} đang trống, Huy nạp thêm nhé.")
                return
            order = {"order_id": oid, "buyer_chat_id": chat_id,
                     "buyer_username": username, "product_id": p["id"],
                     "name": p["name"], "price": price, "status": "cho_thanh_toan"}
            orders.append(order)
            save()
            send(chat_id,
                 f"Đơn DH{oid}: {p['name']} - {fmt_money(price)}đ\n"
                 f"Techcombank {bank.get('account_number', '')} - "
                 f"{bank.get('account_holder', '')}\n"
                 f"Momo {momo.get('phone', '')}\n"
                 f"Nội dung chuyển khoản: DH{oid}\n"
                 f"Chuyển xong nhắn 'da chuyen' để shop giao hàng.")
            return
        # ---- da chuyen / photo bill ----
        pending = [o for o in shop.get("orders", [])
                   if str(o.get("buyer_chat_id")) == str(chat_id)
                   and o.get("status") == "cho_thanh_toan"]
        if low in ("da chuyen", "đã chuyển", "da thanh toan") or \
                (has_photo and pending):
            if not pending:
                send(chat_id, "Bạn chưa có đơn nào đang chờ thanh toán.")
                return
            order = pending[-1]
            order["status"] = "cho_duyet"
            save()
            send(chat_id, "Shop đã nhận, đang chờ duyệt, thanh toán xong sẽ giao hàng ngay.")
            send(ADMIN_CHAT_ID,
                 f"Đơn mới DH{order['order_id']}: @{username} (chat {chat_id}) "
                 f"mua {order['name']} {fmt_money(order['price'])}đ, khách báo đã chuyển. "
                 f"Duyệt bằng /duyet {order['order_id']} để bot tự giao hàng.")
            return
        # ---- /donhang ----
        if low.startswith("/donhang") or low == "don hang":
            mine = [o for o in shop.get("orders", [])
                    if str(o.get("buyer_chat_id")) == str(chat_id)]
            if not mine:
                send(chat_id, "Bạn chưa có đơn nào.")
            else:
                o = mine[-1]
                st = {"cho_thanh_toan": "Chờ thanh toán", "cho_duyet": "Chờ duyệt",
                      "da_giao": "Đã giao"}
                send(chat_id, f"DH{o['order_id']} - {o['name']} - "
                             f"{fmt_money(o['price'])}đ - {st.get(o['status'], o['status'])}")
            return
        # ---- /duyet (chỉ Huy) ----
        if low.startswith("/duyet") and chat_id == ADMIN_CHAT_ID:
            parts = text.split()
            oid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            order = next((o for o in shop.get("orders", [])
                          if o.get("order_id") == oid), None)
            if not order:
                send(chat_id, f"Không có đơn DH{oid}.")
                return
            if order.get("status") != "cho_duyet":
                send(chat_id, f"Đơn DH{oid} khách chưa báo đã chuyển.")
                return
            if not deliver(order, order["buyer_chat_id"],
                           order.get("buyer_username", "")):
                send(chat_id, "Kho tài khoản món này đang trống, chưa giao được — "
                              "Huy gửi danh sách tài khoản cho Muse để nạp kho.")
                send(order["buyer_chat_id"],
                     "Shop đang chuẩn bị hàng, sẽ giao ngay cho bạn.")
            return
        # ---- Hỏi đáp Muse (chỉ Huy) ----
        if chat_id == ADMIN_CHAT_ID and text:
            if not AI_KEY:
                send(chat_id, "Bot chưa gắn key AI nên chưa trả lời được câu hỏi này. "
                              "Huy gắn AI_API_KEY vào server webhook nhé.")
                return
            try:
                send(chat_id, call_ai(text))
            except Exception as e:
                send(chat_id, f"AI đang lỗi, Huy thử lại sau nhé. ({e})")
            return
        # ---- Khách lạ ----
        if text:
            send(chat_id, "Chào bạn, đây là shop đồ số. Nhắn /shop để xem hàng và mua nhé.")


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
