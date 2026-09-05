# Video Dubbing Studio — web app

Upload video → nhận dạng lời thoại → dịch → lồng tiếng bằng ngôn ngữ khác → tải video về.
Loli 2.0 tạo transcript; Qwen3-ForcedAligner-0.6B chạy local để căn timestamp từng từ;
OpenAI dịch và Loly 3.5 sinh giọng, cộng ffmpeg trên máy.

```bash
./install_aligner.sh  # chạy một lần để cài runtime Qwen local
./run.sh          # lần đầu tự tạo .venv và cài dependency
```

Mở http://127.0.0.1:8199 rồi điền API key ở mục *1 · Cấu hình API*.

Job xong mà có đoạn hỏng thì mở **✎ Chỉnh sửa & ghép lại** (hoặc http://127.0.0.1:8199/editor): kéo lại mốc từng câu trên timeline, đọc lại đúng câu sai rồi ghép lại video — không phải chạy lại cả job.
Hỏng cả một khúc thì kéo ngang trên dải **Audio gốc** để chèn một box gen lại — chèn bao nhiêu box cũng được — rồi bấm *Gen tất cả*: nó dịch lại và đọc lại toàn bộ câu trong các box đó rồi ghép lại video luôn; transcript gốc và mốc từng câu giữ nguyên.
Khúc im lặng đáng ngờ thì bật thêm *Nhận dạng lại (ASR) chỗ trống trong box* — nó dò lại thoại mà lần chạy đầu bỏ sót và chèn thành câu mới.
Bản dịch tệ thì sửa thẳng ở *Lời trong box* rồi gen lại với ô «Dịch lại lời» tắt; thiếu hẳn một câu thì *Chèn câu thủ công vào box*, gõ lời là nó đọc luôn.

Tài liệu đầy đủ: [../docs/webapp.md](../docs/webapp.md) ·
Cơ chế đồng bộ: [../docs/dong-bo.md](../docs/dong-bo.md) ·
Kiến trúc: [../docs/kien-truc.md](../docs/kien-truc.md)
