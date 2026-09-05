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
| **Chất lượng giọng (dit_steps)** | 16 (×2.0) | Thanh trượt **1–64**, chỉnh tự do chứ không còn 4 mốc cố định. Quota TTS = `số ký tự × (dit_steps / 8)`, nhãn bên cạnh hiện luôn hệ số chi phí khi kéo. Mốc 8 rẻ nhất nhưng nhịp đọc thất thường, làm khâu đồng bộ khó hơn; trên 32 gần như không nghe ra khác biệt mà quota tăng tuyến tính |
| **Kéo giãn tối đa bằng atempo** | 1.6 | Trần của bước vá cuối. Hạ về 1.2–1.3 nếu thà lệch tiếng còn hơn nghe méo |
| **Tự khớp tốc độ với khung thời gian** | bật | Tắt = khoá cứng ở tốc độ nền, không chỉnh gì thêm |
| **Đọc lại câu còn tràn** | bật | Câu vẫn dài hơn khung sau lần sinh đầu sẽ được đọc lại một lần với tốc độ đã hiệu chỉnh. Tốn thêm quota nhưng thường chỉ vài câu đầu |
| **Nhân bản giọng gốc** | tắt | Cắt đoạn thoại dài nhất (≤ 20 giây) và gọi `POST /api/v1/voices` với `consent=true`. Cần account key `vc_ak_live_*`. Chỉ dùng khi bạn có quyền với giọng nói trong video |
| **Nguồn tiếng nền** | Tự động | «Audio gốc nguyên bản» = đúng file đầu vào, không qua Demucs, chỉ đi qua một filter `volume` — đã kiểm bit-exact. Đổi lại giọng gốc còn nguyên nên chồng lên giọng lồng tiếng. «Tự động» dùng stem nhạc đã tách nếu có |
| **Âm lượng nền** | mặc định | Âm lượng của tiếng nền trộn vào bản lồng tiếng. Nấc «mặc định» dùng giá trị cấu hình (0.9 khi đã tách được nhạc nền, 0.35 khi phải trộn nguyên audio gốc); nấc 0 = tắt hẳn tiếng nền; tối đa 200% |
| **Âm lượng giọng gốc** | 0 (tắt) | Đường riêng cho giọng nhân vật gốc, tách khỏi nhạc nền nhờ stem Demucs. 0 = lồng tiếng thường (thay hẳn giọng); >0 = giọng gốc phát **cùng lúc** với giọng TTS, kiểu thuyết minh chồng tiếng. Chỉ chạy khi nguồn nền là «nhạc nền đã tách» |
| **Âm lượng lồng tiếng** | 100% | Âm lượng của track giọng đọc. Áp dụng cả khi không trộn nền (lúc đó dùng filter `volume` riêng thay vì `amix`) |
| **Ghi phụ đề lên hình** | tắt | Hardsub — phải encode lại video |
| **Nhúng phụ đề mềm** | tắt | Softsub, người xem bật/tắt được, không encode lại |

## Giữ nguyên audio gốc

Trước đây bản ghép cuối làm biến đổi audio gốc ở ba chỗ, đều đã sửa:

| Vấn đề | Trước | Nay |
|---|---|---|
| `amix` mặc định `normalize=1`, chia biên độ cho số input | âm lượng 1.0 thực ra bị hạ **−5,8 dB** | `normalize=0`, đặt 1.0 là đúng 1.0 |
| `mix_audio` ép `-ac 1 -ar 48000` | phim **stereo 44,1kHz → mono 48kHz** | giữ nguyên số kênh và sample rate của file nền |
| Tiếng nền lấy từ stem Demucs | không còn là audio gốc | thêm lựa chọn **Nguồn tiếng nền** |

Kiểm chứng trên chính file phim, trộn với một track lồng tiếng im lặng ở âm lượng 1.0:

```
audio gốc          : 2 kênh @ 44100 Hz
sau khi trộn       : 2 kênh @ 44100 Hz
so từng mẫu trên 2.646.017 mẫu: lệch tối đa = 0/32767  -> bit-exact
```

Thang âm lượng cũng chính xác: đặt 0.2 / 0.35 / 0.5 đo ra đúng 0.2000 / 0.3500 / 0.5000, phần dư
tối đa 1 LSB do làm tròn về int16.

### Ba đường tiếng độc lập

Demucs tách ra hai stem nhưng trước đây chỉ dùng một (nhạc nền), stem giọng bị bỏ sau khi ASR xong.
Nay giữ lại cả hai nên bản trộn cuối có **ba đường chỉnh riêng**:

```
nhạc nền đã tách  × âm lượng nền        (0–200%, hoặc «mặc định»)
giọng nhân vật gốc × âm lượng giọng gốc  (0–200%, mặc định 0 = tắt)
giọng TTS lồng vào × âm lượng lồng tiếng (0–200%, mặc định 100%)
```

Nhờ vậy làm được cả hai kiểu:

- **Lồng tiếng thường** — giọng gốc để 0, giọng TTS thay hẳn giọng nhân vật.
- **Thuyết minh chồng tiếng** — giọng gốc 20–40%, nghe được cả giọng diễn viên lẫn giọng đọc, kiểu
  phim tài liệu hay bản tin nước ngoài.

`mix_tracks` bỏ hẳn track có âm lượng 0 ra khỏi filter thay vì nhân 0 — đỡ một lần giải mã và tránh
`duration=longest` bị kéo dài bởi một track câm. Kiểm chứng phép cộng: trộn `nhạc×0.9 + giọng×0.3 +
giọng×1.0` cho ra kết quả lệch tối đa 1/32767 so với `nhạc×0.9 + giọng×1.3`.

Thanh «âm lượng giọng gốc» không áp dụng khi nguồn nền là «audio gốc nguyên bản», vì giọng gốc đã
nằm sẵn trong đó — cộng stem vào nữa sẽ nghe đúp tiếng. Pipeline cảnh báo thay vì im lặng làm sai.

### Vì sao limiter mặc định tắt

Bỏ chuẩn hoá của `amix` thì âm lượng đúng nghĩa, nhưng nền và lồng tiếng cùng to có thể vượt trần.
Cách thường dùng là chèn `alimiter`. Đo thực tế thì **alimiter không trong suốt**: so hai bản có và
không limiter ở cùng âm lượng 0,35 (còn rất xa ngưỡng clip), **86,6% số mẫu bị lệch**, lệch tối đa
9236/32767 — nó có trễ lookahead ~10ms và động vào cả phần chưa chạm ngưỡng. Tức là nó phá đúng cái
"giữ nguyên" mà ta muốn.

Nên `separate.mix_limiter` **mặc định `false`**. Thay vào đó pipeline đếm số mẫu chạm trần rồi cảnh
báo để bạn tự hạ âm lượng:

```
[warn] Có 1,884 mẫu (0.071%) chạm trần biên độ - nghe có thể rè.
       Hạ âm lượng nền (đang 4.00) hoặc âm lượng lồng tiếng (đang 4.00),
       hoặc bật separate.mix_limiter.
```

Ngưỡng cảnh báo là 0,001% số mẫu — vài chục mẫu chạm trần trong một phim dài thì không nghe ra,
báo cũng chỉ tổ nhiễu.

**Lưu ý còn lại:** nguồn nhiều hơn 2 kênh (5.1) vẫn bị hạ xuống stereo, vì giọng lồng tiếng là mono
nên không có cách đặt nó vào một layout 5.1 mà không đoán bừa. Mono và stereo thì giữ đúng.

## Đếm token OpenAI

Sau khi dịch xong, log job và khối kết quả hiện số token đã dùng. Con số lấy thẳng từ trường
`usage` trong response của OpenAI — **không ước lượng bằng cách đếm ký tự** — nên đúng bằng con số
OpenAI dùng để tính tiền:

```
OpenAI đã dùng 62,790 token (vào 50,310, ra 12,480, trong đó 31,000 token vào được cache
= 62% giá rẻ) qua 2 request | ước tính 0.0127 USD theo bảng giá cho gpt-4o-mini
```

Cách OpenAI đếm, đã xử lý đúng trong `estimate_cost`: `cached_tokens` nằm **bên trong**
`prompt_tokens` chứ không cộng thêm và được tính giá rẻ hơn, nên phần trả giá đầy đủ là
`prompt − cached`. `reasoning_tokens` nằm trong `completion_tokens` và tính giá như output thường.

`align-debug.log` ghi thêm bảng tách theo mục đích gọi (dịch / chấm câu) và cách ra thành tiền.

**Về phần tiền:** số token thì luôn chính xác, còn tiền chỉ đúng khi bảng giá `openai.pricing`
trong `config.json` khớp bảng giá hiện hành. OpenAI đổi giá và thêm model liên tục, nên hãy đối
chiếu <https://openai.com/api/pricing> rồi sửa lại. Đơn vị trong bảng là **USD cho 1 triệu token**:

```json
"pricing": {
  "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60}
}
```

Model không có trong bảng thì chỉ hiện số token, không quy ra tiền — thà thiếu còn hơn hiện số bịa.

## Trần thời lượng: không đọc quá lúc giọng gốc dứt

Khung thời gian cho mỗi câu lấy **cái chặt hơn** trong hai ràng buộc:

1. tới lúc câu sau bắt đầu, chừa `gap_reserve_ms`;
2. **thời lượng giọng gốc của chính câu đó**, cộng dung sai `max_overrun_ms`.

Ràng buộc thứ hai mới là cái quyết định. Trước đây chỉ có ràng buộc thứ nhất, nên một câu gốc dài
3,3 giây nhưng phía sau có 3 giây im lặng sẽ được cấp khung 6,6 giây — câu lồng tiếng đọc tràn hết
khoảng lặng đó, vẫn "vừa khung" nhưng tiếng chạy dài quá hình:

```
gốc   00:01:22,320 --> 00:01:25,600   Who are you, and how did you find me?
dịch  00:01:22,320 --> 00:01:28,770   Bạn là ai, và làm thế nào bạn tìm thấy tôi?
```

Khoảng lặng sau một câu là lúc nhân vật đã ngậm miệng, không phải chỗ để mượn. Với ràng buộc mới,
khung của câu trên là `3,280 + 0,500 = 3,780s`, tức lồng tiếng dứt chậm nhất ở `00:01:26,100`.

Trần này được ép ở ba chỗ nối tiếp nhau:

1. **Prompt dịch** nhận khung mới trong marker `[n|3.8s]`, nên câu dịch được viết ngắn ngay từ đầu.
2. **TTS** nhận thẳng tham số `duration` bằng khung đó; sai số còn lại được binary search trên chính
   `duration` (`binary_iterations` lượt) chứ không phải chỉnh `speed` — đây là cơ chế chính xác hơn.
3. **atempo** ở khâu căn chỉnh. Bình thường chỉ ép tới `max_audio_speed` cho giọng khỏi méo, nhưng
   nếu buông ở đó mà câu vẫn vượt mốc giọng gốc thì ép tiếp tới `hard_max_audio_speed` — lệch hình
   khó nghe hơn là giọng nhanh.

Câu nào vẫn lọt qua cả ba lớp đều được ghi log cảnh báo, gắn `<< VƯỢT MỐC GỐC` trong
`align-debug.log`, và đếm vào dòng tổng kết. Cột `gốc dứt` trong bảng timeline cho biết mốc trần của
từng câu.

| Khoá `pipeline` | Mặc định | Ý nghĩa |
|---|---|---|
| `max_overrun_ms` | `500` | Dung sai cho phép đọc quá mốc giọng gốc |
| `hard_max_audio_speed` | `2.5` | Trần atempo khi buộc phải ép để không tràn |
| `max_audio_speed` | `1.6` | Trần atempo thông thường |

## Nắn từ bị aligner kéo dãn

Aligner buộc phải phủ kín clip bằng đúng những từ trong transcript. Gặp đoạn đầu clip là nhạc nền
hay tiếng động, nó dán từ đầu tiên lên toàn bộ đoạn đó:

```
Who   rel 0.56 - 6.16   (5,6 giây cho một từ 3 chữ cái)
are   rel 6.24 - 6.48
you   rel 6.48 - 6.80
```

Mốc bắt đầu của câu lấy theo `Who` nên phụ đề và tiếng lồng xuất phát sớm hơn giọng thật gần 6 giây,
trong khi cả ba từ thật ra nằm chụm ở cuối clip.

Cách phát hiện: lấy **trung vị giây/ký tự** của mọi từ đã căn trong video — trung vị nên vài từ dị
thường không kéo lệch được ngưỡng — rồi so từng từ với độ dài đáng lẽ phải có. Một từ bị coi là kéo
dãn khi thoả **cả hai**: dài hơn `stretch_min_ms` **và** gấp hơn `stretch_factor` lần độ dài ước
tính. Từ đó được co lại về phía có hàng xóm đứng sát:

```
Who có hàng xóm sát ở phía cuối (0,08s tới `are`) -> neo vào cuối
-> rel 5.92 - 6.16, câu xuất phát 00:01:14.800 thay vì 00:01:09.440
```

Chỉ mốc thời gian bị sửa, chữ giữ nguyên. Mỗi chỗ nắn đều được ghi log, gắn cờ `KÉO-DÃN-ĐÃ-NẮN`, và
hiện trên UI bằng thẻ viền xanh kèm mốc gốc bị gạch ngang.

Còn một dạng bất thường nữa **chỉ được báo, không tự sửa**: cờ `ĐỨNG-LẺ-XA-CỤM` cho từ đầu hoặc cuối
cách cụm còn lại một khoảng lớn bất thường. Chỉ nhìn mốc thời gian thì không phân biệt được aligner
khớp nhầm vào tiếng động hay người nói thật rồi ngừng một nhịp, nên chỗ này để mắt người quyết định.

| Khoá `aligner` | Mặc định | Ý nghĩa |
|---|---|---|
| `fix_stretched_words` | `true` | Bật nắn từ bị kéo dãn |
| `stretch_factor` | `4.0` | Gấp bao nhiêu lần độ dài ước tính thì coi là bất thường |
| `stretch_min_ms` | `800` | Sàn tuyệt đối, ngắn hơn thì không đụng tới |

## Tách câu theo mốc từng từ

Loli trả về nguyên một khối cho mỗi đoạn VAD, ví dụ một dòng dài 16,6 giây:

> But what choice do I have? Okay, Flynn Rider, I'm prepared to offer you a deal. Deal? Look this way. Do you know what these are? You mean the lantern thing they do for the princess?

Đọc liền một mạch như vậy là nguồn gốc lệch tiếng: câu cuối đáng lẽ vang lên ở giây thứ 14 của
khối nhưng lại được đọc ngay sau câu trước. Bước này cắt khối đó thành từng câu rồi lấy mốc **thật**
của mỗi câu từ danh sách từ mà Qwen3 Forced Aligner đã căn:

```
And(0.16) … satchel(1.60)  ->  "And you'll give me back my satchel."  00:04:35.15
I(2.08) … promise(2.40)    ->  "I promise."                          00:04:37.07
```

Dấu chấm trong transcript là tín hiệu chính, khớp từ theo số ký tự đã chuẩn hoá nên aligner tách
`you'll` thành `you` + `ll` cũng không lệch. Mảnh ngắn hơn `min_piece_ms` được nhập lại vào câu
trước để phụ đề không bị vụn. Không đủ dữ liệu để cắt an toàn thì giữ nguyên dòng.

Với dòng dài mà ASR không trả về dấu kết câu nào, `llm_assist` nhờ OpenAI chấm câu hộ. Kết quả bị
từ chối nếu model đổi dù một chữ — lúc đó mốc từ không còn khớp được nữa.

| Khoá `resegment` | Mặc định | Ý nghĩa |
|---|---|---|
| `enabled` | `true` | Bật tách câu |
| `min_piece_ms` | `400` | Mảnh ngắn hơn ngưỡng này thì nhập lại vào câu trước |
| `margin_ms` | `40` | Biên chừa hai đầu để không cắt mất phụ âm |
| `llm_assist` | `true` | Nhờ LLM chấm câu cho dòng không có dấu |
| `llm_min_ms` | `6000` | Chỉ nhờ LLM với dòng dài hơn ngưỡng này |

Trên UI, dòng sinh ra từ bước này có nhãn *tách từ khối N* ở đầu dòng trong bảng word-level.
Chi tiết đầy đủ nằm ở khối `TÁCH CÂU THEO MỐC TỪ` trong `align-debug.log`.

## Dịch có ngữ cảnh

Mỗi lô dịch được gửi kèm **nguyên transcript nguồn của video** trong khối `<TRANSCRIPT>`, nên model
biết ai đang nói với ai, đại từ chỉ vào đâu, thuật ngữ nào đã dùng ở đoạn trước — thay vì đọc trơ
một câu rồi đoán. Transcript dài hơn `context_chars` thì chỉ gửi cửa sổ nới đều hai phía quanh lô
đang dịch. Model chỉ được phép dịch những dòng nằm trong `<INPUT>`.

Prompt nói thẳng cặp ngôn ngữ (`Translate from English into Vietnamese`). Ngôn ngữ nguồn để `auto`
thì lấy ngôn ngữ mà ASR nhận ra ở nhiều dòng nhất.

**Thuật ngữ tiếng Anh được giữ nguyên xi** — quy tắc này được đặt cao hơn mọi quy tắc khác trong
prompt. Không dịch, không phiên âm, không diễn giải, không thêm chú thích trong ngoặc, áp dụng cho:
thuật ngữ kỹ thuật/chuyên ngành tiếng Anh, tên sản phẩm - hãng - app - thư viện, viết tắt, tên model,
số phiên bản, tên file, đường dẫn, URL, định danh code, và tên riêng viết bằng chữ Latin.

| Khoá `openai` | Mặc định | Ý nghĩa |
|---|---|---|
| `context_chars` | `12000` | Trần ký tự cho khối `<TRANSCRIPT>`; đặt `0` để tắt ngữ cảnh |
| `keep_terms` | `[]` | Danh sách thuật ngữ bắt buộc giữ nguyên, thêm vào các nhóm ghi sẵn ở trên |
| `instruction` | `""` | Chỉ thị dịch tự do do bạn viết — xem mục dưới |

## Chỉ thị dịch của riêng bạn

Prompt dựng sẵn không thể biết video của bạn cần xưng hô thế nào, tên nhân vật nào phải giữ nguyên,
giọng văn nên trang trọng hay suồng sã. Hộp **Chỉ thị dịch** trong «Cấu hình → Dịch — OpenAI» để
bạn nói thẳng những điều đó, và nó đi kèm **mọi lô dịch**.

Nó hợp nhất với những thứ prompt vốn đã lo (thuật ngữ tiếng Anh, ánh xạ 1-1, ngân sách thời gian),
chứ không thay thế.

### Viết gì vào đó

Cụ thể và ở dạng mệnh lệnh thì model theo tốt hơn nhiều so với văn mô tả chung chung:

```
- Xưng hô: Rapunzel gọi Flynn là "anh", Flynn gọi Rapunzel là "em".
  Mẹ Gothel gọi Rapunzel là "con", Rapunzel gọi lại là "mẹ".
- Giữ nguyên không dịch: Rapunzel, Flynn Ryder, Corona, Gothel.
- Giọng văn: tự nhiên như phim chiếu rạp, tránh từ Hán Việt nặng nề.
- Không dùng "bạn" — nhân vật trong phim không xưng hô kiểu đó.
- "Tower" dịch là "tháp", không phải "toà tháp".
```

Những nhóm chỉ thị hay dùng:

| Nhóm | Ví dụ |
|---|---|
| **Xưng hô** | ai gọi ai là gì, vai vế, thân hay sơ — thứ tiếng Việt bắt buộc phải quyết mà tiếng Anh không có |
| **Từ giữ nguyên** | tên riêng, tên thương hiệu, thuật ngữ trong ngành của bạn |
| **Từ cấm dùng** | những cách dịch bạn thấy sai hoặc chối tai |
| **Giọng văn** | trang trọng / đời thường / trẻ trung, phim hay tài liệu hay bản tin |
| **Quy ước riêng** | đơn vị đo, cách đọc số, cách xử lý tiếng lóng |

### Hai cấp: chung và riêng từng video

| Nơi đặt | Phạm vi |
|---|---|
| Cấu hình → Dịch — OpenAI | Chỉ thị **chung**, áp cho mọi video |
| Tuỳ chọn → «Chỉ thị dịch cho riêng video này» | **Ghi đè** chỉ thị chung cho đúng lần chạy đó |

Ô riêng để trống thì dùng ô chung. Log job ghi rõ đang áp cái nào:

```
Áp dụng chỉ thị dịch riêng cho video này (74 ký tự): Chỉ thị RIÊNG VIDEO: giữ nguyên Rapunzel…
```

Toàn văn chỉ thị được chép vào `align-debug.log` để về sau còn đối chiếu bản dịch với chỉ thị đã dùng.

### Chỉ thị không phá được cấu trúc đầu ra

Chỉ thị là văn bản tự do, nên về nguyên tắc nó có thể chứa câu kiểu «bỏ qua mọi quy tắc trên». Prompt
được dựng để điều đó không làm hỏng phụ đề. Chỉ thị nằm trong khối `<USER_INSTRUCTIONS>` riêng, và
quy tắc 10 nói rõ thứ tự ưu tiên:

- **Chỉ thị ĐÈ được** quy tắc 3 (cách diễn đạt cho lồng tiếng) và quy tắc 7 (giọng văn) — đúng phần
  bạn cần điều khiển.
- **Chỉ thị KHÔNG đè được** quy tắc 1 (cặp ngôn ngữ), 2 (giữ nguyên thuật ngữ tiếng Anh), 5 (ánh xạ
  1-1 theo `[n]`), 6 (mảnh câu giữ nguyên là mảnh câu), 9 (chỉ xuất dòng dịch) và khối FORMAT.

Prompt cũng nói thẳng rằng khối đó là **dữ liệu, không phải một nhiệm vụ mới**, nên model không trả
lời câu hỏi hay thêm bình luận vì chỉ thị. Ngoài ra:

- Thẻ đóng `</USER_INSTRUCTIONS>` nếu bạn lỡ gõ vào sẽ bị vô hiệu hoá, chỉ thị không thoát ra ngoài
  khối của nó.
- Trần **4000 ký tự**; dài hơn bị cắt, vì phần thừa chỉ đẩy `<TRANSCRIPT>` ra khỏi cửa sổ ngữ cảnh
  chứ không giúp dịch tốt hơn. UI đếm ký tự ngay dưới hộp.

Đã kiểm chứng: chạy thật với chỉ thị chung và chỉ thị riêng, 15 dòng vào ra đúng 15 dòng, ánh xạ
`[n]` nguyên vẹn trong cả hai trường hợp.

### Chỉ thị không áp cho bước chấm câu

`split_sentences` (khôi phục dấu câu cho transcript ASR) **không** nhận chỉ thị. Bước đó chỉ được
thêm dấu câu chứ không được đổi một chữ nào, nên trộn chỉ thị về văn phong vào đấy chỉ tổ hỏng việc.

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
| `source.srt` | Phụ đề gốc theo mốc Qwen forced alignment (fallback VAD) |
| `word-timestamps.json` | Timestamp từng từ/ký tự từ Qwen3-ForcedAligner-0.6B, kèm `segments[]` (mốc VAD, mốc aligner, độ dịch, cờ cảnh báo từng từ) |
| `align-debug.log` | Nhật ký căn chỉnh dạng text: từng từ (mốc tương đối trong clip + mốc tuyệt đối trên video) và bảng chèn timeline lồng tiếng |
| `target.srt` | Phụ đề đã dịch, mốc thời gian **thật** của bản lồng tiếng |

### Bảng word-level timestamps trên UI

Thẻ **4 · Word-level timestamps** hiện ngay khi bước căn từ bắt đầu chạy (không phải đợi job xong) và
đổ thêm dòng theo tiến độ. Mỗi dòng thoại hiện ba mốc để so trực tiếp với nhau:

- `VAD` — mốc cắt câu ban đầu.
- `ALIGN` — mốc sau khi forced alignment, kèm `Δ` bắt đầu/kết thúc so với VAD.
- `CHÈN` — vị trí đặt câu lồng tiếng thực tế, kèm độ lệch (vàng khi trễ > 0,15s).

Bên dưới là từng từ dạng thẻ: `rel` là mốc trong clip, còn mốc tuyệt đối trên video ở dòng thứ hai.
Từ bị gắn cờ có viền đỏ, cả dòng cũng được tô đỏ. Ô lọc nhận số dòng, chữ trong câu hoặc một từ cụ thể;
tick *Chỉ dòng đáng ngờ* để lọc ra đúng những dòng có cờ hoặc bị chèn trễ.

Dữ liệu lấy từ `GET /api/jobs/<id>/words`.

### Soi lỗi chèn lệch bằng `align-debug.log`

File gồm hai phần:

1. **WORD-LEVEL TIMESTAMPS** — mỗi câu in mốc VAD, mốc aligner, `Δbắt đầu/Δkết thúc`, rồi từng từ ở
   cả hai hệ quy chiếu: `rel` là mốc trong clip gửi cho aligner, `abs` là mốc trên timeline video gốc
   (`abs = start_ms của câu + rel`). Từ bất thường được đánh cờ ngay tại dòng:
   - `VƯỢT-CLIP` — aligner trả mốc dài hơn clip, thường do transcript lệch với audio → mốc câu bị kéo sai.
   - `CHỒNG-TỪ-TRƯỚC` — từ bắt đầu trước khi từ trước kết thúc.
   - `ĐỘ-DÀI<=0` — từ có độ dài âm hoặc bằng 0, bị loại khỏi `word-timestamps.json`.
2. **TIMELINE CHÈN LỒNG TIẾNG** — mỗi câu in mốc đích, mốc chèn thực tế, độ lệch, thời lượng dub so với
   khung thời gian cho phép, kèm mốc từ đầu tiên của giọng gốc để đối chiếu. Câu bị đẩy trễ có cờ `TRỄ`.

So hai phần với nhau là ra ngay nguyên nhân: lệch từ phần 1 nghĩa là forced alignment sai mốc, còn
phần 1 đúng mà phần 2 trễ nghĩa là câu lồng tiếng quá dài, bị tràn sang câu sau.

Cấu hình trong `webapp/data/config.json` mục `aligner`:

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `debug_log` | `true` | Ghi `align-debug.log` |
| `log_words_to_ui` | `false` | Đẩy thêm timestamp từng từ vào luồng log job dạng text (rất dài; bảng ở thẻ 4 không cần bật khoá này) |
| `overshoot_warn_ms` | `150` | Ngưỡng gắn cờ `VƯỢT-CLIP` |

App giữ 30 job gần nhất rồi tự xoá job cũ. Thư mục `cache/` của job bị xoá ngay khi job xong.

## 5 · Chỉnh sửa và ghép lại

Job chạy xong thường vẫn có vài câu lệch mốc hoặc đọc sai. Trình chỉnh sửa cho sửa tay đúng những
câu đó rồi ghép lại video, **không phải chạy lại ASR và dịch**.

Mở bằng nút **✎ Chỉnh sửa & ghép lại** trong thẻ *5 · Kết quả*, hoặc vào thẳng
`http://127.0.0.1:8199/editor` để chọn trong danh sách job đã chạy xong.

### Bố cục

| Vùng | Nội dung |
|---|---|
| Trên trái | Video gốc (có tiếng gốc) + thanh điều khiển: phát, âm lượng gốc/lồng tiếng riêng, zoom |
| Trên phải | Bảng chi tiết của câu đang chọn: transcript gốc, lời dịch, tốc độ, âm lượng, các nút đọc lại |
| Dưới | Timeline hai đường: **audio gốc** (vùng tím = khung giọng gốc của từng câu) và **lồng tiếng** (mỗi câu là một box kéo được) |

### Thao tác

| Thao tác | Kết quả |
|---|---|
| Kéo thân box | Dời câu sang mốc khác. Tự hít vào mốc gốc và mép các câu bên cạnh; giữ **Shift** để kéo tự do |
| Kéo mép trái/phải | Đặt độ dài mong muốn (`fit_ms`). Box đổi sang viền vàng: lúc render sẽ ép bằng atempo |
| Bấm vào box | Chọn câu, mở bảng chi tiết |
| Bấm đúp | Phát từ đầu câu đó |
| Bấm nền timeline | Tua tới đó |
| **Kéo trên nền timeline** | Trượt timeline qua lại. Khi mục *Gen lại cả một đoạn* đang mở thì kéo trên dải **Audio gốc** là chèn box (xem bên dưới) |
| **Space** | Phát / dừng |
| **← →** | Dời câu đang chọn 10ms (Shift: 100ms) |
| **Lăn chuột** | Phóng to / thu nhỏ quanh con trỏ. Mốc thời gian nằm dưới con trỏ đứng yên, nên nhắm vào chỗ cần soi rồi lăn là tới |
| **Shift + lăn** | Trượt ngang (lăn ngang của bàn di cảm ứng cũng vậy) |
| **− / + / Vừa màn hình** | Ba nút zoom ở thanh điều khiển, cho ai không dùng con lăn |
| **Ctrl + Z** | Hoàn tác (60 bước gần nhất) |

Mọi thay đổi tự lưu vào `edit/project.json` sau 0,7 giây, trạng thái hiện ở góc trên phải.

### Đọc lại một câu

| Nút | Việc nó làm |
|---|---|
| **Đọc lại câu này** | Gọi TTS một lần với lời và tốc độ đang để trong bảng. Dùng khi câu dịch sai hoặc giọng vấp |
| **Đọc lại vừa khung** | Như trên nhưng gửi kèm `duration` = độ dài khung, để Loly tự đọc cho vừa. Chính xác hơn kéo atempo nhiều |
| **Ép vừa khung (atempo)** | Không gọi TTS, không tốn quota: đánh dấu để lúc render kéo giãn cho vừa khung gốc |
| **Về mốc gốc** | Trả câu về đúng mốc giọng gốc |

Chỉ một request TTS cho mỗi lần bấm, tính đúng theo `số ký tự × (dit_steps / 8)` như lúc chạy job.
Câu mà pipeline không sinh được audio (viền đỏ) cũng đọc lại được ở đây — chỗ đó trong bản đã giao
đang là khoảng lặng.

Clip cũ **không bị xoá**: mỗi lần đọc lại ghi ra `seg0007_v2.wav`, `_v3.wav`… nên đổi ý vẫn còn bản trước.

### Gen lại cả một đoạn

Sửa từng câu một chỉ hợp khi hỏng lác đác. Cả một khúc dịch sai giọng văn hoặc đọc hỏng thì
**chèn một box lên dải «Audio gốc»** rồi bấm gen.

#### Chuột làm gì tuỳ theo mục nào đang mở

Bình thường kéo chuột trên timeline là **trượt qua lại**. Chỉ khi mở mục *✂ Gen lại cả một đoạn*
thì kéo trên dải **Audio gốc** mới thành **chèn box**; đóng mục đó lại, hoặc mở *Trộn âm & ghép lại*
(hai mục tự đóng lẫn nhau), là chuột quay về kéo trượt. Chế độ hiện tại luôn hiện ở thẻ cạnh nút
*Cuộn theo*: «chuột: kéo trượt» hay «chuột: chèn box», và ở chế độ chèn thì dải Audio gốc được viền
hổ phách. Kể cả đang ở chế độ chèn, kéo ở dải *Lồng tiếng* vẫn là trượt, và **bấm một cái không kéo
thì lúc nào cũng là tua**.

Box là một hộp kéo được đúng như box câu ở dải Lồng tiếng, chỉ khác là nó nằm trên dải audio gốc và
mang màu hổ phách:

| Thao tác | Kết quả |
|---|---|
| **Kéo ngang trên dải Audio gốc** (chế độ chèn) | Chèn box mới, vừa kéo vừa định độ rộng |
| **＋ Chèn box** | Chèn box ôm trọn câu đang phát ở vị trí con trỏ, không có thì lấy 8 giây |
| Kéo thân box | Dời sang khúc khác, giữ nguyên độ rộng |
| Kéo mép trái/phải | Nới rộng hay thu hẹp |
| Bấm **×** trên box (hoặc trong danh sách) | Xoá box đó |
| **Delete** / **Esc** | Xoá box đang chọn / bỏ chọn |

Nhãn trên box và danh sách bên phải luôn hiện box đó trùm lên **bao nhiêu câu** — đó chính là những
câu sẽ bị làm lại. Bấm một cái trên nền timeline vẫn là *tua*, chỉ khi kéo mới đẻ ra box.

Chèn bao nhiêu box cũng được. Box **không** ghi vào `project.json`: nó là chỗ đánh dấu "khúc này
hỏng" trong lúc làm việc, tải lại trang là mất.

#### Gen như thế nào

**Gen box đang chọn** làm một box; **Gen tất cả** làm hết mọi box trong **một lượt chạy duy nhất** —
câu nằm trong hai box chồng nhau chỉ tính một lần, và cả video chỉ ghép lại một lần ở cuối. Thứ tự:

1. **Dịch lại** `source_text` của mọi câu trong các box bằng OpenAI. Transcript của **cả video** vẫn
   được gửi kèm làm ngữ cảnh (`context_items`), nên xưng hô và mạch truyện không đứt khỏi phần xung
   quanh. Mỗi câu được nói rõ ngân sách bao nhiêu giây, cùng công thức với `Pipeline._budgets`.
2. **Đọc lại** toàn bộ câu đó bằng Loly, chạy song song theo `pipeline.tts_concurrency`.
   Bật *Đọc vừa khung* thì gửi kèm `duration` = khung giọng gốc của từng câu.
3. **Ghép lại video** luôn, nếu để tick ô cuối.

| Ô chọn | Ý nghĩa |
|---|---|
| **Nhận dạng lại (ASR) chỗ trống trong box** | Dò thoại bị bỏ sót ở những chỗ trong box chưa có câu nào — xem mục dưới. Mặc định tắt |
| **Dịch lại lời từ transcript gốc** | Tắt đi thì giữ nguyên lời đang có, chỉ đọc lại bằng TTS — không tốn token OpenAI |
| **Đọc vừa khung gốc của từng câu** | Gửi `duration` cho Loly thay vì kéo atempo lúc render |
| **Ghép lại video ngay khi gen xong** | Tắt đi thì nghe thử trên timeline trước, tự bấm *Ghép lại video* sau |

#### Dò lại thoại bị bỏ sót trong khoảng lặng

Đôi khi cả một khúc không có câu nào không phải vì im lặng, mà vì lần chạy đầu **bỏ sót thoại**.
Bật **Nhận dạng lại (ASR) chỗ trống trong box** để dò lại đúng những chỗ đó.

Nó chỉ đụng vào phần **chưa câu nào chiếm** bên trong box (lỗ ngắn hơn 0,7s thì bỏ qua), nên câu
sẵn có giữ nguyên transcript và mốc — dò lại không bao giờ phá thứ đang đúng. Với mỗi lỗ: cắt audio
từ stem giọng đã tách (`vocals.flac`, đúng thứ pipeline đưa cho ASR lần đầu), chạy VAD, gọi Loli,
rồi những câu tìm được đi tiếp vào bước dịch và đọc như mọi câu khác. Câu mới được chèn đúng vị trí
thời gian, `line` của cả project được đánh số lại cho liền mạch, `id` của câu cũ giữ nguyên.

**VAD ở bước này chạy tham số nới rộng**, vì chạy lại đúng tham số cũ thì cũng vứt đi y như lần đầu:
hạ `min_speech_ms` 1200 → 400 (câu chen một hai tiếng mới lọt) và tắt bộ lọc nhạc.

Nới ra thì đổi lại tiếng động ngắn cũng vào tới ASR, mà Loly gặp đoạn không có tiếng người thì không
trả về rỗng — nó đoán bừa, điển hình là một chữ Hán lạc vào video tiếng Anh. Nên dòng dò được bị
chấm **dựa trên transcript sẵn có của cả video** chứ không tự chấm lẫn nhau: khác hệ chữ với phần
còn lại là bỏ, rồi trộn vào transcript thật để `srt.drop_hallucinations` soi tiếp. Không có câu nào
sống sót thì job báo lỗi rõ ràng chứ không lặng lẽ chèn rác.

Câu mới **không bị ép đọc vừa khung**, kể cả khi ô *Đọc vừa khung* đang bật: VAD cắt theo năng lượng
nên khung của nó thường dài hơn lời thật rất nhiều (đuôi nhạc, tiếng động), ép cho vừa sẽ ra giọng
kéo lê — một tiếng «Ai vậy?» bị giãn thành 7 giây.

#### Sửa lời trong box, và tự gõ câu mới

Hai mục con nữa trong bảng gen:

**Lời trong box — sửa tay trước khi đọc lại** liệt kê mọi câu box đang trùm, kèm transcript gốc để
đối chiếu và một ô sửa lời dịch. Sửa xong thì gen với ô *Dịch lại lời* **tắt** — nó đọc đúng lời bạn
vừa gõ chứ không dịch lại. Đó cũng là vòng lặp để chữa bản dịch tệ: gen có dịch (tắt ghép video) →
đọc lại kết quả ở đây → sửa chỗ sai → gen lại chỉ TTS → ghép video. Ô đang gõ dở không bao giờ bị
ghi đè khi giao diện vẽ lại, và nhãn box câu trên timeline đổi theo ngay từng chữ.

**Chèn câu thủ công vào box** cho gõ thẳng lời rồi đọc luôn, không cần ASR cũng chẳng cần dịch — một
request TTS là xong. Câu mới đặt ở đầu box đang chọn, đọc với giọng/tốc độ/`dit_steps` đang cấu hình.

| Đặc điểm câu tự gõ | Vì sao |
|---|---|
| `source_text` để rỗng | Bước *Dịch lại* chỉ đụng vào câu có transcript gốc, nên lời bạn gõ **không bao giờ bị dịch đè lên** |
| Không ép đọc vừa khung | Khung ở đây do bạn kéo tay chứ không phải mốc giọng gốc, ép cho vừa chỉ làm giọng méo |
| Đánh dấu `manual` | Hàng của nó trong bảng sửa lời có nhãn *tự gõ*; câu do ASR dò ra thì nhãn *ASR dò ra* |

Chèn xong `line` của cả project được đánh số lại theo thứ tự thời gian, `id` câu cũ giữ nguyên.
Muốn bỏ thì chọn câu đó rồi *Tắt tiếng câu này*, hoặc sửa lời thành nội dung khác và đọc lại.

#### Cấu hình giọng & dịch cho lần gen

Mục con *Cấu hình giọng & dịch cho lần gen này* mang đúng những tuỳ chọn của lúc tạo job mà còn ảnh
hưởng tới việc gen lại:

| Tuỳ chọn | Ghi chú |
|---|---|
| **Voice lồng tiếng** | Bấm *Nạp danh sách voice* để lấy từ tài khoản TTS. Voice đang dùng luôn được giữ trong danh sách kể cả khi chưa nạp |
| **Tốc độ đọc** | 0.5–1.5, gửi thẳng cho Loly |
| **Chất lượng giọng (dit_steps)** | 1–64, kèm luôn hệ số chi phí `dit_steps / 8` như ở trang chính |
| **Chỉ thị dịch** | **Dùng chung cho mọi box** trong lần gen. Để trống thì lấy chỉ thị chung trong *Cài đặt* của trang chính |

Cả bốn được lưu vào `project.json` (mục `tts` và `translate`) nên mở lại phiên sau vẫn còn, và
chính là thứ bước gen đọc ra để chạy — không phải nhập lại mỗi lần.

Box đổi màu theo trạng thái: hổ phách = chưa gen, viền đứt = đang gen, **xanh = đã gen**, đỏ = lỗi.
Kéo lại mốc một box đã gen thì nó quay về *chưa gen*, vì phạm vi đã khác.

Những thứ **không** đổi: transcript gốc, mốc `start_ms` của từng câu, và cách cắt câu. Đây là chỗ
khác với chạy lại job — đoạn mới vẫn ngồi đúng cái lỗ mà đoạn cũ đang chiếm, phần còn lại của video
không bị xê dịch. Câu nào bạn đã kéo tay rồi thì vẫn nằm nguyên chỗ bạn đặt.

Tiến độ hiện chung ở thanh của mục *Trộn âm* (`GET /api/projects/<id>/render`): mỗi job chỉ chạy
một việc nặng tại một thời điểm, gen box và ghép video không giẫm chân nhau. Lô nào dịch hỏng thì
câu đó giữ nguyên lời cũ và được ghi rõ trong log, không bị thay bằng câu tiếng gốc.

### Nghe thử và bản render khác nhau chỗ nào

Phần nghe thử trên trình duyệt lập lịch từng clip bằng Web Audio nên kéo xong là nghe được ngay
mà không phải chờ ghép. Đổi lại, câu bị ép khung được nghe bằng cách đổi `playbackRate` — **giọng
sẽ cao/thấp đi một chút**. Bản render cuối do ffmpeg dựng bằng `atempo`, giữ nguyên cao độ.

Bấm **Ghép lại video** để ffmpeg dựng bản thật: overlay từng câu vào đúng mốc tuyệt đối của nó
(không dồn toa như lúc chạy pipeline), trộn nhạc nền + giọng gốc theo các thanh trong mục *Trộn âm*,
rồi mux với phần hình (video stream chỉ copy nên nhanh). Kết quả ra
`output/<tên>-<lang>-edit.mp4`, tách khỏi bản gốc chứ không đè lên.

### Dữ liệu giữ lại trên đĩa

Bình thường `cache/` bị xoá ngay khi job xong. Để sửa được, những thứ cần thiết được chuyển sang
`webapp/data/jobs/<id>/edit/` trước lúc đó:

| File | Dùng để |
|---|---|
| `project.json` | Toàn bộ trạng thái chỉnh sửa. Đọc thẳng từ đĩa nên restart server vẫn sửa tiếp được |
| `clips/segNNNN*.wav` | Audio từng câu — thứ được kéo qua kéo lại trên timeline |
| `accompaniment.flac`, `vocals.flac` | Hai stem của Demucs, để trộn lại. FLAC không mất mát mà nhẹ hơn wav một nửa |
| `novoice.mp4`, `original.m4a` | Phần hình để mux lại và audio gốc để trình duyệt phát |
| `peaks/*.json` | Đường bao sóng tính sẵn ở server, UI không phải tải cả file wav về |

Cỡ chừng 1,5–2× file gốc mỗi job. Job có thư mục `edit/` sẽ **không** bị dọn khi vượt hạn mức 30 job
trong lịch sử — xoá bằng nút *Xoá dữ liệu chỉnh sửa* trong mục *Trộn âm*.

## Giới hạn cần biết

- Một đoạn gửi ASR tối đa 100 MiB và 1.800 giây — VAD đã cắt ngắn hơn nhiều (mặc định ≤ 18 giây).
- Forced aligner hỗ trợ Chinese, English, Cantonese, French, German, Italian, Japanese, Korean,
  Portuguese, Russian và Spanish. Ngôn ngữ khác tự fallback về timestamp VAD.
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

GET    /api/projects                     danh sách job có dữ liệu chỉnh sửa (đọc từ đĩa)
GET    /api/projects/{id}                toàn bộ project.json
PUT    /api/projects/{id}                lưu {mix, tts, segments[]} - chỉ nhận các trường cho phép sửa
DELETE /api/projects/{id}                xoá thư mục edit/ của job
GET    /api/projects/{id}/peaks/{original|clips}   đường bao sóng dựng sẵn
GET    /api/projects/{id}/media/{path}   phát/tải file trong job (có Range, chặn path traversal)
POST   /api/projects/{id}/segments/{n}/regen  đọc lại một câu: {text, speed, fit, fit_ms, voice_id, dit_steps}
POST   /api/projects/{id}/render         ghép lại video, chạy nền
GET    /api/projects/{id}/render         tiến độ render
GET    /api/projects/{id}/download       tải bản đã ghép lại
```

`GET /api/jobs/{id}/file/video` hỗ trợ HTTP Range nên trình duyệt tua được video ngay trên trang.
