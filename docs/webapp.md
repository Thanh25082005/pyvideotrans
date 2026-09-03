# Web app — hướng dẫn đầy đủ

## Cài đặt và chạy

```bash
./webapp/run.sh                    # lần đầu tự tạo webapp/.venv và cài dependency
PORT=9000 ./webapp/run.sh          # đổi cổng
HOST=0.0.0.0 ./webapp/run.sh       # cho máy khác trong LAN truy cập
```

Mặc định: **http://127.0.0.1:8199**

Yêu cầu duy nhất ngoài Python là **ffmpeg** (`sudo apt install ffmpeg`). Dependency Python chỉ có
fastapi, uvicorn, python-multipart, httpx, numpy — không torch, không model.

Chạy tay không qua script:

```bash
webapp/.venv/bin/python webapp/server.py --host 127.0.0.1 --port 8199
```

## 1 · Cấu hình API

Điền trực tiếp trên giao diện, bấm **Kiểm tra key** cho từng dịch vụ rồi **Lưu cấu hình**.
Giá trị ghi vào `webapp/data/config.json` (chmod 600, đã nằm trong `.gitignore`).

| Ô | Ghi chú |
|---|---|
| **STT API key** | `stt_sk_live_*`, hoặc account key `vc_ak_live_*` có bật cả công tắc STT và Audio upload |
| **OpenAI API key** | Dùng cho bước dịch. Ngôn ngữ nguồn trùng ngôn ngữ đích thì bỏ qua bước này và không cần key |
| **TTS API key** | `vc_sk_live_*` (đã gắn sẵn một voice) **hoặc** `vc_ak_live_*` — loại này **bắt buộc** điền `voice_id` |
| **Voice ID** | Nút *Tải* cạnh ô voice gọi `GET /api/v1/keys` để liệt kê voice mà key với tới được (cần quyền key management) |

Nút *Kiểm tra key* gọi thật lên dịch vụ: STT dùng `/api/public/v1/auth/check` (nếu deployment không
mở route này thì tự chuyển sang gửi 0,5 giây im lặng để xác thực), TTS dùng `GET /api/v1/usage` và
báo luôn số ký tự còn lại, OpenAI gửi một câu hỏi 1 token.

## 2 · Chọn video và ngôn ngữ

| Tuỳ chọn | Ý nghĩa |
|---|---|
| **Ngôn ngữ trong video** | 34 lựa chọn theo §3 của Loli 2.0: `auto`, 30 ngôn ngữ đơn, 3 mode song ngữ. Chọn đúng ngôn ngữ cho kết quả ổn định hơn `auto` |
| **Dịch sang** | 30 ngôn ngữ đích |
| **Voice lồng tiếng** | Để trống = dùng voice trong cấu hình |
| **Tốc độ đọc nền** | 0,5–1,5. Là điểm xuất phát, từng câu vẫn được điều chỉnh riêng |

Định dạng nhận: mp4, mkv, mov, avi, webm, flv, ts, m4v, wmv, mpg, mpeg và các file audio mp3, wav,
m4a, aac, flac, ogg, opus. File chỉ có audio sẽ cho ra `.m4a` thay vì video.

### Tuỳ chọn nâng cao

| Tuỳ chọn | Mặc định | Ý nghĩa |
|---|---|---|
| **Chất lượng giọng (dit_steps)** | 16 (×2.0) | Số bước khuếch tán. Quota TTS = `số ký tự × (dit_steps / 8)`. Mốc 8 rẻ nhất nhưng nhịp đọc thất thường, làm khâu đồng bộ khó hơn; 32 cho chất lượng cao nhất |
| **Kéo giãn tối đa bằng atempo** | 1.6 | Trần của bước vá cuối. Hạ về 1.2–1.3 nếu thà lệch tiếng còn hơn nghe méo |
| **Tự khớp tốc độ với khung thời gian** | bật | Tắt = khoá cứng ở tốc độ nền, không chỉnh gì thêm |
| **Đọc lại câu còn tràn** | bật | Câu vẫn dài hơn khung sau lần sinh đầu sẽ được đọc lại một lần với tốc độ đã hiệu chỉnh. Tốn thêm quota nhưng thường chỉ vài câu đầu |
| **Nhân bản giọng gốc** | tắt | Cắt đoạn thoại dài nhất (≤ 20 giây) và gọi `POST /api/v1/voices` với `consent=true`. Cần account key `vc_ak_live_*`. Chỉ dùng khi bạn có quyền với giọng nói trong video |
| **Ghi phụ đề lên hình** | tắt | Hardsub — phải encode lại video |
| **Nhúng phụ đề mềm** | tắt | Softsub, người xem bật/tắt được, không encode lại |

## 3 · Tiến trình

Thanh tiến độ, đồng hồ đếm và log realtime (poll mỗi 0,9 giây). Log ghi rõ từng bước: số đoạn VAD
tìm được, tiến độ nhận dạng, tốc độ đọc đo được của voice, câu nào phải đọc lại, và thống kê đồng bộ
cuối cùng. Nút **Dừng** huỷ job ngay, các worker đang chạy thoát ở lần kiểm tra kế tiếp.

## 4 · Kết quả

Mỗi job tạo thư mục `webapp/data/jobs/<id>/output/`:

| File | Nội dung |
|---|---|
| `<tên>-<lang>.mp4` | Video đã lồng tiếng (hoặc `.m4a` nếu đầu vào chỉ có audio) |
| `<tên>-<lang>-dubbed.wav` | Track lồng tiếng riêng, 48kHz mono |
| `source.srt` | Phụ đề gốc theo mốc thời gian VAD |
| `target.srt` | Phụ đề đã dịch, mốc thời gian **thật** của bản lồng tiếng |

App giữ 30 job gần nhất rồi tự xoá job cũ. Thư mục `cache/` của job bị xoá ngay khi job xong.

## Giới hạn cần biết

- Một đoạn gửi ASR tối đa 100 MiB và 1.800 giây — VAD đã cắt ngắn hơn nhiều (mặc định ≤ 18 giây).
- Một lần gọi TTS tối đa 5.000 ký tự; câu phụ đề luôn ngắn hơn nhiều.
- Chưa tách nhạc nền khỏi lời thoại: video nhiều nhạc nền sẽ mất phần nền ở bản lồng tiếng.
- Chưa phân biệt nhiều người nói (diarization) — cả video dùng chung một giọng.

## Xử lý sự cố

| Hiện tượng | Nguyên nhân thường gặp |
|---|---|
| `Chưa cấu hình API key cho: ...` | Chưa bấm *Lưu cấu hình*, hoặc ngôn ngữ nguồn `auto` khác ngôn ngữ đích nên vẫn cần OpenAI key |
| `[403] VOICE_ID_REQUIRED` | Đang dùng account key `vc_ak_live_*` mà chưa điền voice_id |
| `[403] KEY_CREDIT_EXCEEDED` | Key đã hết trần ký tự riêng của nó, không liên quan quota tài khoản |
| `Nhận dạng không ra nội dung nào` | Sai ngôn ngữ nguồn, hoặc audio quá nhỏ/nhiễu. Thử để `auto` |
| Lồng tiếng lệch nhiều | Đọc dòng log `Đồng bộ: X/Y câu vào đúng mốc gốc...` — xem [dong-bo.md](dong-bo.md) để biết cách đọc con số này |
| Giọng đọc nhanh như máy | Hạ *Kéo giãn tối đa bằng atempo* xuống 1.2, hoặc đặt *Tốc độ đọc nền* 0,95 để còn biên độ |

## API HTTP

Server cũng dùng được như API. Các route chính:

```
GET  /api/config                     đọc cấu hình + trạng thái key
POST /api/config                     lưu cấu hình (JSON, ghi đè một phần)
POST /api/config/test/{stt|tts|openai}   kiểm tra key
GET  /api/voices                     liệt kê voice của TTS key
GET  /api/languages                  danh sách ngôn ngữ nguồn/đích
POST /api/jobs                       multipart: file + tuỳ chọn → {"job_id": "..."}
GET  /api/jobs/{id}?log_from=N       trạng thái, tiến độ, elapsed, log từ dòng N
POST /api/jobs/{id}/cancel           dừng job
GET  /api/jobs/{id}/file/{kind}      tải kết quả: video|audio|dubbed_audio|source_srt|target_srt
GET  /api/jobs/{id}/subtitles/{kind} đọc nội dung SRT dạng text
```

`GET /api/jobs/{id}/file/video` hỗ trợ HTTP Range nên trình duyệt tua được video ngay trên trang.
