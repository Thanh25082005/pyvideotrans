# Kiến trúc và luồng xử lý

## Nguyên tắc

Không host model STT/TTS/LLM. Webapp host thêm **Qwen3-ForcedAligner-0.6B** cục bộ để căn
timestamp từng từ; ngoài ra dùng **ffmpeg** và một bộ **VAD năng lượng** viết bằng numpy để cắt câu — bắt buộc phải có vì API nhận dạng
chỉ trả về text, không trả mốc thời gian.

## Web app (`webapp/`) — phần chính

```
upload video
     │
     ▼
[1] Chuẩn bị        ffprobe → tách audio 16kHz mono + tách video không tiếng (novoice.mp4)
     │
     ▼
[1b] TÁCH VOCAL     Demucs: giọng / nhạc nền (bỏ qua nếu service không chạy)
[2] VAD             cắt audio thành các đoạn thoại thô (chạy trên stem giọng)
[2b] Forced align   Loli transcript + audio → Qwen word-level timestamps
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
| [`webapp/core/translate_openai.py`](../webapp/core/translate_openai.py) | Dịch qua Chat Completions, prompt cho lồng tiếng, chỉ thị của người dùng, đếm token thật |
| [`webapp/core/tts_loly.py`](../webapp/core/tts_loly.py) | Client Loly 3.5 (sinh giọng, liệt kê voice, nhân bản giọng) |
| [`webapp/core/ffmpeg.py`](../webapp/core/ffmpeg.py) | Bọc ffmpeg/ffprobe: probe, tách, atempo, mux, tpad |
| [`webapp/core/srt.py`](../webapp/core/srt.py) | Sinh/đọc SRT, gộp câu ngắn, khớp lại theo mốc thời gian |
| [`webapp/core/langs.py`](../webapp/core/langs.py) | Bảng ngôn ngữ dùng chung cho cả ba API |
| [`webapp/core/separate.py`](../webapp/core/separate.py) | Client tách giọng/nhạc nền (Hybrid Demucs qua aligner service) |
| [`webapp/core/editor.py`](../webapp/core/editor.py) | Chỉnh sửa sau lồng tiếng: chụp project, đọc lại từng câu hoặc gen lại cả một đoạn (dịch lại + đọc lại), dựng lại track theo mốc tuyệt đối rồi ghép lại video |
| [`webapp/core/settings.py`](../webapp/core/settings.py) | Đọc/ghi `webapp/data/config.json` |
| [`webapp/static/`](../webapp/static/) | Giao diện: HTML + CSS + JS thuần, không framework |

### Tại sao cần VAD

Loli 2.0 trả về `{"text": ..., "language": ...}` — **không có timestamp**. Mà lồng tiếng thì bắt
buộc phải biết câu nào nói ở giây thứ mấy. Cách giải: cắt audio thành từng đoạn thoại trước, gửi
riêng từng đoạn, rồi lấy chính vị trí đoạn cắt làm mốc thời gian của câu.

Đây cũng là cách pyVideoTrans xử lý các API không có timestamp (`BaseRecogn.cut_audio`), chỉ khác
là bản gốc dùng model ten-vad/silero còn ở đây là VAD phân tích phổ thuần numpy (không thêm
dependency nào ngoài numpy đã có):

1. STFT 32ms/bước 10ms, gom thành 24 dải log, tính luôn cepstrum để có cao độ và độ hài.
2. **Trừ nền nhạc**: ước lượng sàn năng lượng từng dải trên cửa sổ trượt 3 giây (`percentile 12`,
   làm mượt bằng min-3-block) rồi chỉ giữ phần vượt lên trên sàn. Nhạc nền đều đều chìm vào sàn,
   giọng nói nhô lên — nhờ vậy biên câu bám lúc người nói mở/ngậm miệng chứ không bám lúc nhạc to.
3. Ngưỡng thích ứng theo nền nhiễu (`percentile 20`) và đỉnh (`percentile 95`), có trễ đóng/mở
   (hysteresis) để không cắt vụn giữa câu.
4. Nối các mảnh cách nhau dưới `min_silence_ms` **trước**, lọc mảnh ngắn **sau** — làm ngược lại
   sẽ mất phụ âm tắc và nuốt mất nửa câu.
5. **Lọc nhạc nền**: chấm điểm "giống giọng nói" 0..1 cho từng cửa sổ 1,2s (bước 200ms), rồi cắt
   bỏ vùng chỉ có nhạc — xem mục dưới.
6. Chia đoạn quá dài tại điểm năng lượng thấp nhất trong cửa sổ cho phép.
7. Gộp đoạn quá ngắn vào hàng xóm, nhưng chỉ khi hai đoạn thực sự gần nhau (≤ 2s).

### Phân biệt nhạc nền với giọng nói

Ngưỡng năng lượng thuần không phân biệt được nhạc với tiếng người: nhạc chuyển cảnh to bằng thoại
thì cũng bị gửi vào ASR, và ASR **không trả về rỗng mà bịa chữ** — thường là một tiếng ậm ừ kèm
ngôn ngữ đoán sai (video tiếng Anh mà ra `嗯。` / `language: Chinese`).

Đã thử hai cách, chỉ một cách hiệu quả.

**Cách không hiệu quả — chấm điểm bằng đặc trưng phổ.** Tính `dyn` (biên độ dao động dB), `spread`
(độ tản cao độ), `mod4` (điều biến 4Hz — nhịp âm tiết), `harm_ratio` trên từng cửa sổ 1,2s rồi gộp
thành điểm 0..1. Trên audio tổng hợp thì tách bạch rất đẹp (nhạc 0,05–0,20 / thoại 0,75–0,99),
**nhưng đo trên phim thật thì hai phân bố chồng hoàn toàn lên nhau**:

| Đoạn (phim hoạt hình có nhạc nền) | điểm |
|---|---|
| Nhạc + hiệu ứng, ASR bịa ra `嗯。` | 0,66 |
| Nhạc, ASR bịa ra `啊！` | 0,70 |
| Nhạc, ASR bịa ra `嗯，哎，嗯。` | 0,87 |
| **Thoại thật** "Is this?" | 0,86 |
| **Thoại thật** "Who are you, and how did you find me?" | 0,65 |

Câu thoại thật còn thấp điểm hơn tiếng động bịa. Không ngưỡng nào cắt được. Lý do: nhạc phim có
dàn nhạc + hiệu ứng nên `dyn` đạt 13–31 (bằng thoại) chứ không phải 1–3 như nốt ngân tổng hợp.
Code vẫn còn trong `core/vad.py` để soi bằng `python -m core.vad`, nhưng **mặc định tắt**
(`pipeline.music_filter: false`).

**Cách hiệu quả — tách vocal.** [`webapp/core/separate.py`](../webapp/core/separate.py) gọi sang
aligner service, chạy Hybrid Demucs (đi kèm `torchaudio`, không phải cài thêm package) để tách
audio thành stem giọng và stem nhạc nền. Đo trên cùng đoạn phim đó:

| Đoạn | vocals − nhạc |
|---|---|
| Chỗ chỉ có nhạc | **−45 dB** (stem giọng gần như câm) |
| Thoại thật | **+9 đến +26 dB** |

Kết quả trên phim 5,4 phút: VAD chạy trên stem giọng loại được **58 giây** audio chỉ-có-nhạc so với
chạy trên audio gốc (238,5s → 180,4s), mà không mất câu thoại nào. Tách hết 6,5 giây trên RTX 5060.

Tách vocal còn sửa luôn một lỗi khác của bước ghép cuối: trước đây để giữ nhạc nền thì phải trộn
nguyên audio gốc ở volume 0,35, tức là **giọng gốc cũng chồng lên giọng lồng tiếng**. Có stem nhạc
nền rồi thì trộn stem đó ở volume 0,9 — nhạc và hiệu ứng nguyên vẹn, không còn giọng gốc.

Tham số ở mục `separate` trong `config.json`: `enabled`, `base_url`, `timeout`,
`accompaniment_volume`. Service không chạy thì pipeline tự quay về đường cũ kèm cảnh báo.

### Lọc chữ ASR bịa ra

Tách vocal không diệt hết: tiếng hét, tiếng thở gấp của nhân vật *là* giọng người nên vẫn lọt vào
ASR và vẫn ra `啊！`. [`srt.drop_hallucinations`](../webapp/core/srt.py) dọn nốt bằng hai luật, cả
hai đều đòi **khác hệ chữ với phần còn lại của video** nên tiếng ậm ừ có thật trong đúng ngôn ngữ
nguồn vẫn được giữ:

1. Dòng chỉ gồm tiếng ậm ừ (`嗯 啊 唉 哎…` / `uh um ah oh mm…`) và viết bằng hệ chữ khác đa số.
2. Từ 3 dòng liên tiếp trở lên nội dung giống hệt nhau và ngắn — kiểu lặp vô hạn của ASR.

Chạy trên phụ đề thật: bắt đúng 4 dòng bịa trên tổng 85 dòng, không đụng 81 dòng còn lại.

### Chỉ thị dịch và thứ tự ưu tiên trong prompt

Prompt dịch ([`translate_openai.py`](../webapp/core/translate_openai.py)) có 10 quy tắc. Quy tắc 10
nhận khối `<USER_INSTRUCTIONS>` — văn bản tự do người dùng viết để điều khiển xưng hô, giọng văn,
từ giữ nguyên. Vì là văn bản tự do nên thứ tự ưu tiên phải nói thẳng trong prompt:

```
chỉ thị người dùng  ĐÈ ĐƯỢC   quy tắc 3 (diễn đạt cho lồng tiếng), 7 (giọng văn)
chỉ thị người dùng  KHÔNG ĐÈ  quy tắc 1 (cặp ngôn ngữ), 2 (giữ thuật ngữ Anh),
                              5 (ánh xạ 1-1 theo [n]), 6 (mảnh câu), 9 (chỉ xuất
                              dòng dịch), và khối FORMAT
```

Nhờ vậy một chỉ thị viết sai — kể cả cố tình viết "bỏ qua mọi quy tắc" — cũng chỉ đổi văn phong chứ
không làm lệch số dòng hay phá định dạng `[n]`, tức là không làm hỏng khâu ghép phụ đề phía sau.
Prompt còn nói rõ khối đó là *dữ liệu, không phải nhiệm vụ mới*, nên model không trả lời câu hỏi hay
thêm bình luận vì nó. Thẻ đóng giả bị vô hiệu hoá, và chỉ thị bị cắt ở 4000 ký tự để không đẩy
`<TRANSCRIPT>` ra khỏi cửa sổ ngữ cảnh.

Chỉ thị **không** áp cho `split_sentences`: bước chấm câu chỉ được thêm dấu, không được đổi chữ.

## App gốc (`videotrans/`)

Luồng của pyVideoTrans, giữ nguyên cấu trúc mixin trong [`videotrans/task/trans_create.py`](../videotrans/task/trans_create.py):

```
prepare → recogn → diariz → trans → dubbing → align → assembling → task_done
```

Mỗi bước là một mixin trong `videotrans/task/_stage_*.py`, nối với nhau bằng hàng đợi ở
`videotrans/task/job.py`. Web app tái hiện đúng luồng này nhưng gọn hơn nhiều và chạy tuần tự
trong một thread thay vì qua hàng đợi Qt.

Chi tiết phần đã tỉa: [desktop.md](desktop.md).
