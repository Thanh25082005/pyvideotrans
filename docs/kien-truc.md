# Kiến trúc và luồng xử lý

## Nguyên tắc

Không host bất kỳ model STT/TTS/LLM nào. Thứ duy nhất chạy tại máy là **ffmpeg** và một bộ
**VAD năng lượng** viết bằng numpy (~150 dòng) để cắt câu — bắt buộc phải có vì API nhận dạng
chỉ trả về text, không trả mốc thời gian.

## Web app (`webapp/`) — phần chính

```
upload video
     │
     ▼
[1] Chuẩn bị        ffprobe → tách audio 16kHz mono + tách video không tiếng (novoice.mp4)
     │
     ▼
[2] VAD             cắt audio thành các đoạn thoại, mốc thời gian lấy từ vị trí đoạn cắt
     │
     ▼
[3] Nhận dạng       gửi song song từng đoạn tới Loli 2.0 → source.srt
     │
     ▼
[4] Dịch            OpenAI dịch theo lô, mỗi dòng kèm ngân sách thời gian → target.srt
     │
     ▼
[5] Lồng tiếng      Loly 3.5 sinh từng câu, tốc độ đọc được tính riêng cho mỗi câu
     │
     ▼
[6] Căn chỉnh       đặt từng câu về đúng mốc gốc, tự bắt nhịp khi trễ (xem docs/dong-bo.md)
     │
     ▼
[7] Ghép            kéo dài video nếu lồng tiếng dài hơn, rồi mux hình + tiếng (+ phụ đề)
     │
     ▼
video kết quả + track wav + 2 file SRT
```

### Bản đồ module

| File | Vai trò |
|---|---|
| [`webapp/server.py`](../webapp/server.py) | FastAPI: route cấu hình, upload, poll trạng thái, tải file |
| [`webapp/core/pipeline.py`](../webapp/core/pipeline.py) | Bảy bước ở trên, lớp `Pipeline` và `RateEstimator` |
| [`webapp/core/jobs.py`](../webapp/core/jobs.py) | Chạy job trong thread riêng, gom log và tiến độ |
| [`webapp/core/vad.py`](../webapp/core/vad.py) | Cắt câu theo năng lượng tín hiệu |
| [`webapp/core/stt_loli.py`](../webapp/core/stt_loli.py) | Client Loli 2.0 |
| [`webapp/core/translate_openai.py`](../webapp/core/translate_openai.py) | Dịch qua Chat Completions, prompt cho lồng tiếng |
| [`webapp/core/tts_loly.py`](../webapp/core/tts_loly.py) | Client Loly 3.5 (sinh giọng, liệt kê voice, nhân bản giọng) |
| [`webapp/core/ffmpeg.py`](../webapp/core/ffmpeg.py) | Bọc ffmpeg/ffprobe: probe, tách, atempo, mux, tpad |
| [`webapp/core/srt.py`](../webapp/core/srt.py) | Sinh/đọc SRT, gộp câu ngắn, khớp lại theo mốc thời gian |
| [`webapp/core/langs.py`](../webapp/core/langs.py) | Bảng ngôn ngữ dùng chung cho cả ba API |
| [`webapp/core/settings.py`](../webapp/core/settings.py) | Đọc/ghi `webapp/data/config.json` |
| [`webapp/static/`](../webapp/static/) | Giao diện: HTML + CSS + JS thuần, không framework |

### Tại sao cần VAD

Loli 2.0 trả về `{"text": ..., "language": ...}` — **không có timestamp**. Mà lồng tiếng thì bắt
buộc phải biết câu nào nói ở giây thứ mấy. Cách giải: cắt audio thành từng đoạn thoại trước, gửi
riêng từng đoạn, rồi lấy chính vị trí đoạn cắt làm mốc thời gian của câu.

Đây cũng là cách pyVideoTrans xử lý các API không có timestamp (`BaseRecogn.cut_audio`), chỉ khác
là bản gốc dùng model ten-vad/silero còn ở đây là VAD năng lượng thuần numpy:

1. Chia khung 20ms, tính RMS theo dB.
2. Ngưỡng thích ứng theo nền nhiễu (`percentile 20`) và đỉnh (`percentile 95`), có trễ đóng/mở
   (hysteresis) để không cắt vụn giữa câu.
3. Nối các mảnh cách nhau dưới `min_silence_ms` **trước**, lọc mảnh ngắn **sau** — làm ngược lại
   sẽ mất phụ âm tắc và nuốt mất nửa câu.
4. Chia đoạn quá dài tại điểm năng lượng thấp nhất trong cửa sổ cho phép.
5. Gộp đoạn quá ngắn vào hàng xóm, nhưng chỉ khi hai đoạn thực sự gần nhau (≤ 2s).

## App gốc (`videotrans/`)

Luồng của pyVideoTrans, giữ nguyên cấu trúc mixin trong [`videotrans/task/trans_create.py`](../videotrans/task/trans_create.py):

```
prepare → recogn → diariz → trans → dubbing → align → assembling → task_done
```

Mỗi bước là một mixin trong `videotrans/task/_stage_*.py`, nối với nhau bằng hàng đợi ở
`videotrans/task/job.py`. Web app tái hiện đúng luồng này nhưng gọn hơn nhiều và chạy tuần tự
trong một thread thay vì qua hàng đợi Qt.

Chi tiết phần đã tỉa: [desktop.md](desktop.md).
