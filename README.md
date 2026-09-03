# Video Dubbing Studio

Công cụ lồng tiếng video bằng API: **upload video → nhận dạng lời thoại → dịch → lồng tiếng bằng ngôn ngữ khác → tải video về**.

Repo này là bản rút gọn của [pyVideoTrans](https://github.com/jianchang512/pyvideotrans), giữ lại đúng phần cần cho dự án và
**bỏ toàn bộ model chạy tại máy**. Mọi khối AI đều gọi API bên ngoài.

## Hai phần trong repo

| Phần | Vai trò | Chạy bằng |
|---|---|---|
| **[`webapp/`](webapp/)** | Web UI chính của dự án: upload video, xem log tiến trình, tải kết quả. Dùng Loli 2.0 (ASR) + OpenAI (dịch) + Loly 3.5 (TTS). | `./webapp/run.sh` |
| `videotrans/`, `sp.py`, `webui.py`, `cli.py` | App gốc của pyVideoTrans (giao diện desktop, WebUI Gradio, CLI) đã được tỉa còn các kênh API thuần. | `uv run sp.py` |

Phần đang được phát triển là `webapp/`. Phần còn lại giữ nguyên để tham chiếu và tái sử dụng.

## Bắt đầu nhanh

```bash
./webapp/run.sh          # lần đầu tự tạo .venv và cài dependency
```

Mở **http://127.0.0.1:8199**, điền API key ở mục *1 · Cấu hình API*, rồi kéo thả video vào.

Yêu cầu: **ffmpeg** trên máy, Python 3.12 (hoặc 3.10+). Phía Python chỉ cần fastapi, uvicorn, httpx, numpy.

## Tài liệu

| Tài liệu | Nội dung |
|---|---|
| [docs/webapp.md](docs/webapp.md) | Hướng dẫn đầy đủ web app: cấu hình, từng tuỳ chọn, kết quả, xử lý sự cố |
| [docs/kien-truc.md](docs/kien-truc.md) | Kiến trúc và luồng xử lý, bản đồ module |
| [docs/dong-bo.md](docs/dong-bo.md) | Cơ chế khớp lồng tiếng với hình: ước lượng tốc độ đọc, đọc lại, atempo, con trỏ tự bắt nhịp |
| [docs/desktop.md](docs/desktop.md) | App desktop/WebUI/CLI sau khi tỉa: còn kênh nào, cấu hình ra sao |
| [docs/phat-trien.md](docs/phat-trien.md) | Môi trường phát triển, cách kiểm tra, tình trạng test |
| [.claude/api-stt.md](.claude/api-stt.md) · [.claude/api-tts.md](.claude/api-tts.md) | Contract API của Loli 2.0 và Loly 3.5 |

## Giấy phép

GPL v3 — kế thừa từ pyVideoTrans. Xem [LICENSE](LICENSE).
