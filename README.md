# Webhook siêu nhanh cho @timenowvn_bot

Bản cron hiện tại kiểm tra tin nhắn mỗi 1 phút nên trả lời chậm 20–40 giây.
Bản webhook này: Telegram đẩy tin tới server ngay khi khách nhắn → trả lời trong 2–5 giây.

## File trong thư mục này
- `webhook_server.py` — server, chỉ dùng Python chuẩn, không cần cài thêm gì
- `shop.json` — sản phẩm, thanh toán, đơn hàng, số dư (bản sao lúc làm, server sẽ tự cập nhật)
- `shop_stock.json` — kho tài khoản demo (bản sao, thay bằng tài khoản thật trước khi bán thật)

## Cách triển khai (chọn 1)

### Cách dễ nhất: Render.com (miễn phí)
1. Đẩy 3 file này lên GitHub repo mới (public hoặc private đều được).
2. Vào render.com → New → Web Service → chọn repo → Runtime Python.
3. Start Command: `python3 webhook_server.py`
4. Thêm biến môi trường:
   - `TELEGRAM_BOT_TOKEN` = token bot (lấy từ @BotFather, KHÔNG ghi vào code)
   - `AI_API_KEY` = key AI. Rẻ nhất: Groq (groq.com, miễn phí) hoặc Google Gemini (aistudio.google.com, miễn phí) — cả hai đều tương thích OpenAI, chỉ cần đổi `AI_API_BASE` và `AI_MODEL`:
     - Groq: `AI_API_BASE=https://api.groq.com/openai/v1`, `AI_MODEL=llama-3.3-70b-versatile`
     - Gemini: `AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai`, `AI_MODEL=gemini-2.0-flash`
     - OpenAI: mặc định, `AI_MODEL=gpt-4o-mini` (trả phí theo dùng)
5. Deploy xong Render cho 1 địa chỉ https, ví dụ `https://timenowvn-bot.onrender.com`
6. Đặt webhook (chạy 1 lần):
   `curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://timenowvn-bot.onrender.com/webhook"`
7. Xong. Nhắn vào bot là trả lời trong vài giây.

Lưu ý Render miễn phí: server ngủ sau 15 phút không có ai nhắn, tin đầu tiên sau lúc ngủ sẽ chậm ~30 giây để server tỉnh, các tin sau nhanh lại. Muốn không ngủ thì dùng gói trả phí (~7 USD/tháng) hoặc VPS.

### Cách ổn định nhất: VPS rẻ (~5 USD/tháng, ví dụ DigitalOcean / Vultr / Hetzner)
1. Thuê VPS Ubuntu, cài Python 3.
2. Copy 3 file lên VPS, chạy:
   ```
   TELEGRAM_BOT_TOKEN=xxx AI_API_KEY=yyy AI_API_BASE=... AI_MODEL=... nohup python3 webhook_server.py &
   ```
3. Dùng Caddy hoặc nginx để có https cho tên miền của Huy, rồi đặt webhook như trên.
4. Server chạy 24/7, không ngủ, lúc nào cũng 2–5 giây.

## Quay về bản cũ
Muốn về lại bản cron 1 phút thì xóa webhook:
`curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url="`
rồi cron telegram-muse-poll sẽ nhận tin lại như trước.

## Lưu ý
- Token bot và key AI chỉ để trong biến môi trường, không ghi vào code, không gửi trong chat.
- Bot webhook sẽ không nhớ hết context Muse ở đây (VietMess, TimeNow...) trừ dòng mô tả ngắn trong SYSTEM_PROMPT của file. Muốn bot nhớ nhiều hơn thì sửa SYSTEM_PROMPT.
- Đơn hàng / số dư / kho tài khoản lưu trong shop.json và shop_stock.json trên server, không đồng bộ về máy Muse này. Trước khi chuyển sang webhook, copy 2 file mới nhất từ máy Muse sang server.
