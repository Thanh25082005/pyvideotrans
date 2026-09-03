# Video Dubbing Studio — web app

Upload video → nhận dạng lời thoại → dịch → lồng tiếng bằng ngôn ngữ khác → tải video về.
Không host model nào: Loli 2.0 (ASR) + OpenAI (dịch) + Loly 3.5 (TTS), cộng ffmpeg trên máy.

```bash
./run.sh          # lần đầu tự tạo .venv và cài dependency
```

Mở http://127.0.0.1:8199 rồi điền API key ở mục *1 · Cấu hình API*.

Tài liệu đầy đủ: [../docs/webapp.md](../docs/webapp.md) ·
Cơ chế đồng bộ: [../docs/dong-bo.md](../docs/dong-bo.md) ·
Kiến trúc: [../docs/kien-truc.md](../docs/kien-truc.md)
