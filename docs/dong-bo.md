# Cơ chế khớp lồng tiếng với hình

Bản dịch gần như không bao giờ dài đúng bằng câu gốc. Tài liệu này mô tả cách web app xử lý
chênh lệch đó, và vì sao cách làm khác với `videotrans/task/_rate.py` của bản gốc.

## Vấn đề: độ trễ cộng dồn

`_rate.py` ghép lồng tiếng theo kiểu tuần tự: con trỏ thời gian cộng `max(độ dài lồng tiếng, khung
thời gian)`. Chỉ cần một câu dịch dài hơn khung là **mọi câu sau đó trễ vĩnh viễn**, càng về cuối
video càng lệch, không có cơ chế nào kéo lại.

Đo trên video 60 giây, 8 câu, TTS mô phỏng sai số ±18%:

| | Lệch lớn nhất | Lệch trung bình | Câu vào đúng mốc |
|---|---|---|---|
| Cộng dồn (cách của `_rate.py`) | 17,09s | 5,33s | 3/8 |
| Cách hiện tại | 2,54s | 0,32s | 7/8 |

## Ba lớp xử lý

### Lớp 1 — tốc độ nền

Ô *Tốc độ đọc nền* trên giao diện, đi thẳng vào field `speed` của request TTS. Loly 3.5 chỉ nhận
0,5–1,5 nên giá trị được kẹp lại trong khoảng đó.

### Lớp 2 — đặt tốc độ riêng cho từng câu, **trước** khi gọi API

`RateEstimator` trong [`webapp/core/pipeline.py`](../webapp/core/pipeline.py) đo tốc độ đọc thật của
voice — số ký tự đọc được trong một giây, quy về `speed = 1.0`:

```
dur = chars / (cps × speed)      →      cps = chars / (dur × speed)
```

Giá trị `cps` được cập nhật bằng trung bình động (EMA, hệ số 0,3) sau mỗi câu sinh xong, kể cả câu
đã đổi tốc độ — nhờ công thức trên nên số đo ở speed nào cũng quy được về cùng một thang.

Hai câu đầu sinh tuần tự để có số liệu, các câu sau chạy song song. Với mỗi câu mới:

```
predicted = chars / cps / base_speed
nếu predicted > khung + 200ms:
    speed = clamp(base_speed × predicted / khung, base_speed, 1.5)
```

Giọng đọc nhanh do chính model sinh ra nghe tự nhiên hơn hẳn so với kéo giãn tín hiệu sau khi sinh.

### Lớp 3 — vá phần còn lại

1. **Đọc lại**: câu sinh xong vẫn tràn quá 200ms và tốc độ chưa kịch trần 1,5 → gọi TTS lần hai với
   tốc độ đã hiệu chỉnh. Tắt được bằng *Đọc lại câu còn tràn*.
2. **atempo**: vẫn tràn (đã kịch 1,5) → kéo giãn tín hiệu bằng ffmpeg, tối đa theo *Kéo giãn tối đa*
   (mặc định 1,6). Đây là bước cuối vì nó làm giọng méo nhất.

Nhờ lớp 2, số lần đọc lại giảm rất nhanh. Thử nghiệm với TTS luôn đọc chậm hơn dự đoán 25%: chỉ câu
đầu tiên phải đọc lại, 7 câu sau đặt đúng tốc độ ngay lần gọi đầu — **9 lượt TTS cho 8 câu**.

## Con trỏ tự bắt nhịp

Khi ghép track cuối, mỗi câu luôn cố phát đúng mốc thời gian gốc của nó:

```python
if cursor < target_start:          # đang sớm → chèn im lặng chờ
    timeline.append(silence(target_start - cursor))
    cursor = target_start
# đang trễ → phát ngay, không chèn gì, để đuổi kịp
timeline.append(samples)
cursor += dub_ms
```

Hệ quả: một câu tràn chỉ đẩy trễ **những câu sát ngay sau nó**; gặp khoảng lặng đủ dài là hết lệch.
Khác hẳn cách cộng dồn, nơi độ trễ đi theo tới cuối video.

## Khung thời gian của một câu

Khung = từ lúc câu bắt đầu tới lúc câu kế tiếp bắt đầu, trừ đi 120ms làm nhịp thở
(`gap_reserve_ms`). Tận dụng cả khoảng lặng sau câu giúp giảm mạnh nhu cầu tăng tốc.

Sai lệch dưới 200ms (`fit_tolerance_ms`) thì bỏ qua — ép tốc độ vì vài chục mili giây chỉ làm giọng
xấu đi mà không ai nhận ra khác biệt.

## Nén ngay từ khâu dịch

Câu dịch dài gấp ba khung thời gian thì không tốc độ nào cứu được. Vì vậy mỗi dòng gửi cho OpenAI
kèm ngân sách thời gian:

```
[3|2.4s] văn bản gốc của dòng 3
```

Prompt nói rõ: giọng TTS đọc khoảng 14 ký tự/giây với chữ Latin và 5 với chữ Hán/Nhật/Thái, câu nào
không vừa thì cắt tính từ, đại từ và các cụm khách sáo cho tới khi vừa. Bộ đọc kết quả chấp nhận cả
`[3]` lẫn `[3|2.4s]` nên model trả về dạng nào cũng ghép lại đúng.

## Đọc log đồng bộ

Cuối bước căn chỉnh, log in ra:

```
Đồng bộ: 7/8 câu vào đúng mốc gốc, lệch trung bình 0.32s, lệch lớn nhất 2.54s,
         2 câu phải kéo giãn thêm bằng atempo
```

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| Nhiều câu không vào đúng mốc, lệch lớn | Bản dịch dài hơn khung nhiều | Prompt đã ép nén; thử model mạnh hơn, hoặc chọn ngôn ngữ đích súc tích hơn |
| Nhiều câu phải atempo | Trần tốc độ native (1,5) không đủ | Tăng *Kéo giãn tối đa*, chấp nhận giọng méo hơn |
| Lệch trung bình gần 0 nhưng nghe vẫn sai chỗ | VAD cắt câu sai | Chỉnh `min_speech_ms` / `min_silence_ms` trong `webapp/data/config.json` |
