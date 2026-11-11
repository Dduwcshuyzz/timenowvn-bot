# Bot tải video TimeNow cho @timenowvn_bot

Gửi link TikTok / YouTube / Instagram / Facebook / X vào bot, bot gửi đủ
cả ảnh bìa, video không logo và nhạc MP3 một lần để thích cái nào thì tải
cái đó (bài đăng ảnh TikTok thì gửi từng ảnh + nhạc). Gửi nhiều link trong
1 tin nhắn (tối đa 3) để tải hàng loạt. Sau khi tải xong có nút bấm: lấy lại
nhạc, tải bản thường (480p), mở link gốc. Nhắn "nhac" để gửi lại file nhạc
của link vừa tải. /help xem hướng dẫn, /thongke xem số link đã tải.
Huy nhắn câu hỏi thì bot trả lời như Muse. Bot tải TikTok qua API TikWM
trước; nếu TikWM chặn IP server (đã gặp trên Render, lỗi 403) thì tự chuyển
sang yt-dlp (video), nếu yt-dlp cũng bị TikTok chặn thì lấy link video từ
HTML trang TikTok (playAddr). YouTube / Instagram / Facebook / X tải bằng
yt-dlp rồi gộp hình + tiếng bằng ffmpeg (gói imageio-ffmpeg) vì các nền
tảng này chỉ có luồng hình và tiếng riêng, không có sẵn file gộp.
File nhạc bot tải về rồi upload lại dạng MP3, vì gửi thẳng bằng link
hay bị Telegram nhận nhầm thành file video .mp4 (link nhạc TikTok
không có đuôi .mp3 và CDN trả sai Content-Type).

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
- Chỉ tải được nội dung công khai. TikTok trên server Render đôi lúc bị chặn (TikWM 403, yt-dlp chặn) nên có link phải gửi lại lần 2 mới tải được.
- Shop bán hàng đã gỡ khỏi bot ngày 2026-11-11 theo ý Huy.
