# Loli 2.0 — Public Speech-to-Text API

Tài liệu này là public contract cho file transcription, NDJSON progress và realtime WebSocket của OriAgent Voice. Nó không mô tả upstream model, database, secret nội bộ hoặc quy trình vận hành.

## 1. Base URL và endpoint

```text
https://studio.evomlabs.com
```

| Method | Path | Kết quả |
|---|---|---|
| `POST` | `/api/v1/stt/transcriptions` | Transcribe file, trả JSON |
| `POST` | `/api/v1/stt/transcriptions?stream=true` | Transcribe file, trả NDJSON |
| `WS` | `/api/public/v1/stt/ws?token=...` | Realtime transcription |
| `GET` | `/api/public/v1/auth/check` | Kiểm tra STT key |

> **Needs verification — direct gateway exposure:** route REST `/api/v1/stt/transcriptions` có owner Next.js được track. Hai path `/api/public/v1/auth/check` và `/api/public/v1/stt/ws` thuộc STT gateway; repository chỉ có reverse-proxy example rời và không chứng minh chúng đang được map trên public origin ở mọi deployment. Operator phải xác minh edge route/TLS; nếu direct gateway không được expose, dùng origin do operator cung cấp thay vì hard-code ví dụ bên dưới.

Public branding trong response là:

```json
{
  "provider": "loli-asr",
  "model": "Loli 2.0"
}
```

## 2. Xác thực

STT user key có dạng `stt_sk_live_*` và cần scope `stt.transcribe`.

**Một tài khoản giữ được nhiều key STT (từ 2026-08-11).** Mỗi key cấp trong Studio có mô tả, **hạn dùng** (`expiresAt`) và **trần ký tự** (`creditLimit`) riêng, giống hai họ key TTS; thu hồi một key không ảnh hưởng các key còn lại. Key hết hạn trả `403 INVALID_API_KEY`; key hết trần trả `403 KEY_CREDIT_EXCEEDED` **trước khi** audio được đọc hay gửi đi.

Trần của key STT tính bằng **số ký tự transcript trả về** (không phải độ dài audio, không phải số request), và được cộng vào sau khi transcribe xong — với NDJSON là `text` của event `final`. Route Next.js `/api/v1/stt/transcriptions` kiểm quota STT account trước khi đọc upload, rồi settle số ký tự transcript sau khi model xong. Direct STT REST/WebSocket cũng settle final transcript qua callback nội bộ của control plane; nếu gateway chưa có `STT_USAGE_CALLBACK_URL` + `STT_USAGE_CALLBACK_SECRET` thì direct `stt_sk_live_*` bị fail closed thay vì chạy không meter. Vì độ dài chỉ biết sau processing, request cuối có thể vượt qua phần còn lại của trần key/quota; request sau bị chặn. Trần key thuộc vòng đời key và không reset theo tháng.

File transcription dùng Bearer header:

```http
Authorization: Bearer stt_sk_live_YOUR_KEY
```

`POST /api/v1/stt/transcriptions` (route Next.js) cũng chấp nhận account key `vc_ak_live_*`, với điều kiện key đó bật cả công tắc **STT** và **Audio upload** trong Studio; tắt một trong hai thì request trả `403 PERMISSION_DENIED`. Realtime WebSocket và `auth/check` thuộc STT gateway và **chỉ** nhận `stt_sk_live_*` (hoặc master key của operator), không nhận account key.

Realtime WebSocket dùng query parameter vì browser WebSocket API không hỗ trợ custom Authorization header:

```text
wss://studio.evomlabs.com/api/public/v1/stt/ws?token=stt_sk_live_YOUR_KEY
```

`auth/check` chấp nhận một trong hai dạng:

```http
X-API-Key: stt_sk_live_YOUR_KEY
```

```http
Authorization: Bearer stt_sk_live_YOUR_KEY
```

Key hợp lệ trả:

```json
{"status":"ok","api_key":"valid"}
```

Key là secret. Query token có thể xuất hiện trong proxy/browser logs; chỉ mở WebSocket qua TLS, tránh ghi URL đầy đủ vào log và rotate key nếu nghi bị lộ.

## 3. Language modes

Từ 2026-08-11, mode phủ **toàn bộ 30 ngôn ngữ model hỗ trợ**. Trước đó chỉ có 8 mode (en/vi/ko/ja và ba mode song ngữ) và `auto` khoá cứng ở English + Vietnamese: model vẫn nghe được tiếng Thái hay tiếng Nga, nhưng gateway coi transcript viết bằng chữ viết ngoài mode là model drift và **bỏ trắng**. Nay `auto` cho phép mọi ngôn ngữ và không lọc gì.

**`auto`** — model tự nhận diện, không giới hạn ngôn ngữ nào.

**Mode đơn ngôn ngữ** — mã ngôn ngữ được gửi thẳng xuống model làm gợi ý giải mã, và transcript viết bằng chữ viết khác sẽ bị loại:

| Mã | Ngôn ngữ | Mã | Ngôn ngữ | Mã | Ngôn ngữ |
|---|---|---|---|---|---|
| `vi` | Vietnamese | `hi` | Hindi | `fr` | French |
| `en` | English | `ar` | Arabic | `es` | Spanish |
| `zh` | Chinese | `fa` | Persian | `pt` | Portuguese |
| `yue` | Cantonese | `ru` | Russian | `it` | Italian |
| `ja` | Japanese | `mk` | Macedonian | `nl` | Dutch |
| `ko` | Korean | `el` | Greek | `pl` | Polish |
| `th` | Thai | `tr` | Turkish | `cs` | Czech |
| `id` | Indonesian | `de` | German | `ro` | Romanian |
| `ms` | Malay | `hu` | Hungarian | `sv` | Swedish |
| `tl` | Filipino | `da` | Danish | `fi` | Finnish |

Filipino dùng mã `tl` (không phải `fil`) vì đó là mã upstream chấp nhận.

**Mode song ngữ** — giữ nguyên từ trước, hẹp hơn `auto`: `en-vi`, `ko-en`, `ja-en`.

`language` trong response là nhãn **suy ra**, không phải kết quả nhận diện của model: mode đơn ngôn ngữ trả về đúng mã đó, còn `auto` đoán theo chữ viết của transcript. Chữ viết chỉ phân biệt được hệ chữ chứ không phân biệt được các ngôn ngữ dùng chung một hệ chữ, nên một transcript tiếng Pháp trong mode `auto` sẽ được gắn nhãn `en`. Cần nhãn chính xác thì chọn hẳn ngôn ngữ thay vì để `auto`. Transcript rỗng trả `unknown`.

## 4. File transcription — JSON

```http
POST /api/v1/stt/transcriptions
Authorization: Bearer stt_sk_live_YOUR_KEY
Content-Type: multipart/form-data
```

### Form fields

| Field | Kiểu | Bắt buộc | Mặc định/giới hạn |
|---|---|---:|---|
| `audio` | file | Có | Tối đa 100 MiB; `file` cũng được chấp nhận như alias |
| `language` | string | Không | `auto`; một trong các mode ở §3 |

Container được nhận diện hiện tại:

- WAV, 16-bit PCM;
- MP3;
- WebM;
- OGG;
- MP4/M4A.

Thời lượng sau decode phải từ khoảng `0,2` giây đến `1.800` giây. Encoded formats phụ thuộc khả năng decode của deployment; client không nên chỉ dựa vào filename extension.

```bash
curl -X POST "https://studio.evomlabs.com/api/v1/stt/transcriptions" \
  -H "Authorization: Bearer stt_sk_live_YOUR_KEY" \
  -F "audio=@recording.wav" \
  -F "language=auto"
```

Response thành công không dùng `{ok,data}` envelope:

```json
{
  "text": "Nội dung đã phiên âm...",
  "language": "vi",
  "duration_ms": 1234,
  "provider": "loli-asr",
  "model": "Loli 2.0"
}
```

`duration_ms` là processing latency, không phải độ dài audio.

## 5. File transcription — NDJSON

```http
POST /api/v1/stt/transcriptions?stream=true
Authorization: Bearer stt_sk_live_YOUR_KEY
Content-Type: multipart/form-data
```

Form fields giống endpoint JSON. Response có `Content-Type: application/x-ndjson`; mỗi dòng là một JSON object hoàn chỉnh.

```bash
curl -N -X POST "https://studio.evomlabs.com/api/v1/stt/transcriptions?stream=true" \
  -H "Authorization: Bearer stt_sk_live_YOUR_KEY" \
  -F "audio=@meeting.mp3" \
  -F "language=vi"
```

Thứ tự bình thường:

```json
{"type":"started","duration_seconds":42.5,"chunks_total":2,"provider":"loli-asr","model":"Loli 2.0"}
{"type":"chunk","index":1,"chunks_total":2,"text":"...","combined_text":"...","language":"vi"}
{"type":"chunk","index":2,"chunks_total":2,"text":"...","combined_text":"...","language":"vi"}
{"type":"final","text":"...","language":"vi","is_final":true,"provider":"loli-asr","model":"Loli 2.0","latency_ms":5230}
```

Audio dài được chia thành chunk mục tiêu tối đa khoảng 25 giây, ưu tiên điểm cắt yên lặng gần boundary. Client phải dựa vào `type` và `chunks_total`, không suy ra số chunk từ duration.

Nếu pipeline lỗi sau khi response stream đã bắt đầu, dòng cuối có thể là:

```json
{
  "type": "error",
  "error": {
    "code": "stt_upstream_error",
    "message": "Error description"
  }
}
```

Khi dùng NDJSON, luôn xử lý cả `final` và `error`.

## 6. Realtime WebSocket

Kết nối:

```text
wss://studio.evomlabs.com/api/public/v1/stt/ws?token=stt_sk_live_YOUR_KEY&language=auto
```

Sau khi socket mở, gửi message `start` trước binary audio:

```json
{
  "type": "start",
  "mode": "auto",
  "language": "auto",
  "sample_rate": 48000,
  "format": "pcm_s16le"
}
```

| Field | Mặc định | Ý nghĩa |
|---|---|---|
| `type` | — | Dùng `start` |
| `mode` | `auto` | Chỉ `auto` hoặc `manual` |
| `language` | Query value hoặc `auto` | Một trong các language mode ở §3 |
| `sample_rate` | `16000` | Integer từ `8000` đến `192000`: sample rate của binary input |
| `format` | `pcm_s16le` | Chỉ `pcm_s16le`: PCM signed 16-bit little-endian, mono |
| `session_id` | Server tự tạo | Optional client-provided identifier |

Gửi binary PCM16 mono liên tục. Server resample về sample rate trong event `started`. `mode="auto"` dùng energy VAD: server tự tìm speech onset/silence, phát `partial` và chốt `segment`. Khi dừng, gửi:

```json
{"type":"stop"}
```

Server phát `final`, sau đó `closed`. Đóng socket mà không gửi `stop` sẽ hủy session và không đảm bảo có final transcript.

### Event `started`

```json
{
  "type": "started",
  "session_id": "session_id",
  "mode": "auto",
  "language": "auto",
  "sample_rate": 16000,
  "provider": "loli-asr",
  "model": "Loli 2.0"
}
```

### Event `partial` và `segment`

```json
{
  "type": "partial",
  "session_id": "session_id",
  "index": 0,
  "text": "live window text",
  "committed_text": "",
  "live_text": "live utterance text",
  "combined_text": "live utterance text",
  "is_final": false,
  "language": "vi",
  "latency_ms": 210
}
```

```json
{
  "type": "segment",
  "session_id": "session_id",
  "index": 0,
  "text": "committed utterance",
  "committed_text": "committed utterance",
  "live_text": "",
  "combined_text": "committed utterance",
  "is_final": true,
  "language": "vi",
  "latency_ms": 350
}
```

- `partial` có thể bị thay thế bởi partial mới cho cùng utterance.
- `segment` chốt một utterance tại silence boundary.
- Dùng `combined_text` để hiển thị toàn bộ transcript hiện thời.

### Manual realtime mode

Manual mode dành cho Push-to-Talk hoặc client đã có VAD riêng. Start bằng `"mode":"manual"`; server **không** tự phát `partial` và không tự cắt silence. Binary audio sau lần `commit` trước được giữ trong buffer cho đến khi client gửi:

```json
{"type":"commit"}
```

`commit` cần ít nhất `0,2` giây audio; nếu ngắn hơn server gửi recoverable event `error` với `code: "bad_request"` và **giữ buffer**, nên client có thể gửi thêm PCM rồi commit lại. Commit thành công phát một event `segment` với cùng schema ở trên, `is_final: true`, `live_text: ""`; `index` tăng từ 0. Khi gửi `stop`, server tự commit phần buffer cuối nếu đủ `0,2` giây, bỏ fragment ngắn hơn, rồi phát `final` và `closed`. Gửi `commit` khi không có audio cũng trả event `error` recoverable.

### Event `final` và `closed`

```json
{
  "type": "final",
  "session_id": "session_id",
  "text": "Toàn bộ transcript",
  "language": "vi",
  "is_final": true,
  "provider": "loli-asr",
  "model": "Loli 2.0",
  "latency_ms": 4100
}
```

```json
{"type":"closed","session_id":"session_id","reason":"stopped"}
```

### Event lỗi

```json
{
  "type": "error",
  "session_id": "session_id-or-null",
  "message": "Error description",
  "code": "bad_request"
}
```

Key sai/thiếu phát error code `unauthorized` và socket đóng với code `1008`. Realtime session dùng cùng giới hạn audio tối đa 1.800 giây.

### Ví dụ JavaScript tối thiểu

```javascript
const ws = new WebSocket(
  "wss://studio.evomlabs.com/api/public/v1/stt/ws" +
  "?token=stt_sk_live_YOUR_KEY&language=auto"
);

ws.addEventListener("open", () => {
  ws.send(JSON.stringify({
    type: "start",
    language: "auto",
    sample_rate: 48000,
    format: "pcm_s16le",
  }));
});

ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.type === "partial" || message.type === "segment") {
    renderTranscript(message.combined_text);
  }
  if (message.type === "final") {
    renderTranscript(message.text);
  }
  if (message.type === "error") {
    console.error(message.code, message.message);
  }
});

// Gửi các frame PCM16 bằng ws.send(arrayBuffer).
function stopTranscription() {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "stop" }));
  }
}
```

## 7. Error contract

Lỗi xảy ra tại public proxy trước khi gọi transcription dùng:

```json
{
  "ok": false,
  "error": {
    "code": "BAD_REQUEST",
    "message": "Error description"
  }
}
```

Lỗi do STT service trả về có thể không có field `ok`:

```json
{
  "error": {
    "code": "bad_request",
    "message": "Error description"
  }
}
```

Client hiện phải chấp nhận cả hai envelope.

| HTTP | Code thường gặp | Ý nghĩa |
|---:|---|---|
| 400 | `BAD_REQUEST`, `bad_request` | Multipart, audio, language hoặc duration không hợp lệ |
| 401 | `INVALID_API_KEY`, `unauthorized` | Key thiếu, sai hoặc revoked |
| 403 | `INVALID_API_KEY`, `FORBIDDEN` | Key expired hoặc thiếu scope |
| 403 | `KEY_CREDIT_EXCEEDED` | Key đã dùng hết trần ký tự riêng của nó |
| 403 | `QUOTA_EXCEEDED` | Account không còn quota STT để bắt đầu request |
| 413 | `FILE_TOO_LARGE` | Upload vượt 100 MiB |
| 415 | `unsupported_audio_format` | Container/encoding không được hỗ trợ |
| 502 | `stt_upstream_error` | Transcription engine lỗi |
| 503 | `SERVICE_NOT_CONFIGURED`, `STT_UNAVAILABLE`, `model_not_ready` | Dịch vụ chưa cấu hình, không truy cập được hoặc chưa sẵn sàng |
| 500 | `INTERNAL_ERROR`, `internal_error` | Lỗi hệ thống |

Với NDJSON và WebSocket, lỗi có thể xuất hiện trong stream sau khi HTTP/WebSocket handshake đã thành công; luôn kiểm tra event `type: "error"`.

## 8. Tài liệu liên quan

- [TTS public API](./tts-api.md)
- [System overview](../overview.md)
- [Internal route ownership](../specs/route-ownership.md) — dành cho maintainer, không phải public integration contract
