# Bot tải video TimeNow cho @timenowvn_bot

Gửi link TikTok vào bot, bot tải qua API TikWM rồi gửi lại file video
không logo (hoặc từng ảnh nếu là bài đăng ảnh). Nhắn "nhac" sau khi tải
xong để lấy file nhạc MP3. Nếu TikWM bị chặn (Render dùng IP dùng chung
hay bị giới hạn), bot tự chuyển sang cách 2: tải thẳng từ TikTok bằng
yt-dlp (video) hoặc đọc HTML trang TikTok để trích ảnh (bài đăng ảnh).
Huy nhắn câu hỏi thì bot trả lời như Muse.

## File trong thư mục này
- `webhook_server.py` — server Python chuẩn + yt-dlp (cài ở buildCommand trong render.yaml)

## Cách triển khai (chọn 1)

### Cách dễ nhất: Render.com (miễn phí)
1. Đẩy file này lên GitHub repo (public hoặc private đều được).
2. Vào render.com → New → Web Service → chọn repo → Runtime Python.
3. Start Command: `python3 webhook_server.py`
4. Thêm biến môi trường:
   - `TELEGRAM_BOT_TOKEN` = token bot (lấy từ @BotFather, KHÔNG ghi vào code)
   - `AI_API_KEY` = key AI. Rẻ nhất: Groq (groq.com, miễn phí) — cả hai đều tương thích OpenAI, chỉ cần đổi `AI_API_BASE` và `AI_MODEL`:
     - Groq: `AI_API_BASE=https://api.groq.com/openai/v1`, `AI_MODEL=llama-3.3-70b-versatile`
     - Gemini: `AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai`, `AI_MODEL=gemini-2.0-flash`
     - OpenAI: mặc định, `AI_MODEL=gpt-4o-mini` (trả phí theo dùng)
5. Deploy xong Render cho 1 địa chỉ https, ví dụ `https://timenowvn-bot.onrender.com`
6. Đặt webhook (chạy 1 lần):
   `curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://timenowvn-bot.onrender.com/webhook"`
7. Xong. Gửi link TikTok vào bot là nhận lại video trong vài giây.

Lưu ý Render miễn phí: server ngủ sau 15 phút không có ai nhắn, tin đầu tiên sau lúc ngủ sẽ chậm ~30 giây để server tỉnh, các tin sau nhanh lại. Muốn không ngủ thì dùng gói trả phí (~7 USD/tháng) hoặc VPS.

### Cách ổn định nhất: VPS rẻ (~5 USD/tháng, ví dụ DigitalOcean / Vultr / Hetzner)
1. Thuê VPS Ubuntu, cài Python 3.
2. Copy file lên VPS, chạy:
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
- Hiện bot mới tải được TikTok. YouTube / Instagram / Facebook sẽ thêm sau.
- Shop bán hàng đã gỡ khỏi bot ngày 2026-11-11 theo ý Huy.
