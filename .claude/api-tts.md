# Loly 3.5 — Public Text-to-Speech API

Tài liệu này là public contract cho voice enrollment, batch TTS, streaming TTS và quota của OriAgent Voice. Nó chỉ mô tả hành vi mà ứng dụng bên ngoài cần biết; chi tiết database, secret nội bộ và vận hành không thuộc phạm vi này.

## 1. Base URL và endpoint

Base URL HTTP:

```text
https://studio.evomlabs.com
```

| Method | Path | Credential | Chức năng |
|---|---|---|---|
| `POST` | `/api/v1/voices` | Account key `vc_ak_live_*` | Tạo voice clone và cấp voice key |
| `DELETE` | `/api/v1/voices/{id}` | Account key `vc_ak_live_*` | Xoá một voice của chính account |
| `POST` | `/api/v1/tts/generate` | Voice key `vc_sk_live_*` hoặc account key `vc_ak_live_*` | Sinh một audio hoàn chỉnh |
| `POST` | `/api/v1/tts/bytes` | Voice key `vc_sk_live_*` hoặc account key `vc_ak_live_*` | Sinh audio hoàn chỉnh và trả binary bytes |
| `POST` | `/api/v1/tts/sse` | Voice key `vc_sk_live_*` hoặc account key `vc_ak_live_*` | Stream PCM16 dần qua Server-Sent Events |
| `POST` | `/api/v1/tts/stream-token` | Voice key `vc_sk_live_*` hoặc account key `vc_ak_live_*` | Cấp token WebSocket ngắn hạn |
| `WS` | `/ws/tts/stream?token=...` | Stream token | Stream PCM16 theo frame |
| `GET` | `/api/v1/keys` | Account key `vc_ak_live_*` có quyền key management | Liệt kê metadata API key và usage còn lại |
| `GET` | `/api/v1/usage` | Voice key `vc_sk_live_*` | Đọc quota của account |

> **Verified / Needs verification — public WebSocket origin:** stream-token tạo `ws_url` từ `APP_URL` (fallback public origin) tại [`web/src/app/api/v1/tts/stream-token/route.ts`](../../web/src/app/api/v1/tts/stream-token/route.ts), nên production phải đặt `APP_URL=https://studio.evomlabs.com`. Repository không có full edge configuration cho `/ws/tts/stream`; TLS và WebSocket proxy của `wss://studio.evomlabs.com` vẫn là **Needs verification**. Client phải dùng `ws_url` trong response.

## 2. Credential và xác thực

Hai loại key có quyền khác nhau và không thay thế cho nhau:

| Key | Scope | Dùng cho |
|---|---|---|
| `vc_ak_live_*` | `voices.write` + permission switch của key | Voice enrollment/delete, TTS `generate`/`bytes`/`sse`/`stream-token`, STT batch và `GET /api/v1/keys` theo từng công tắc |
| `vc_sk_live_*` | `tts.generate`, `tts.stream`, `usage.read` | Generate, stream token và usage |

`vc_sk_live_*` gắn với đúng một voice. Client không gửi `voice_id` trong request generate hoặc streaming; nếu có gửi thì phải trùng voice của key, sai voice trả `403 VOICE_NOT_ALLOWED`.

### 2.1. Account key trên endpoint TTS

`vc_ak_live_*` không gắn voice nào, nên request generate và stream-token **bắt buộc có `voice_id`**. Thiếu field này trả `400 VOICE_ID_REQUIRED`.

Quyền của account key do chủ key bật/tắt trong Studio (API Key → key admin) và được kiểm tra trước khi trừ quota hay gọi backend:

| Công tắc | Chặn cái gì khi tắt |
|---|---|
| TTS | `POST /api/v1/tts/generate`, `/bytes`, `/sse`, `/stream-token` → `403 PERMISSION_DENIED` |
| STT | `POST /api/v1/stt/transcriptions` → `403 PERMISSION_DENIED` |
| Quản lý kho audio | Mọi route đưa audio vào hoặc ra khỏi thư viện: `POST /api/v1/voices`, `POST /api/v1/stt/transcriptions` và `DELETE /api/v1/voices/{id}` → `403 PERMISSION_DENIED` |
| API key usage | `GET /api/v1/keys` → `403 PERMISSION_DENIED` |
| Phạm vi voice | `all` = voice của account + voice dùng chung (preset); `mine` = chỉ voice của account; `custom` = chỉ các voice trong allow-list. Áp dụng cho mọi transport TTS (`generate`, `bytes`, `sse`, `stream-token`) lẫn delete. Ngoài phạm vi trả `403 VOICE_NOT_ALLOWED` |

Voice của account khác luôn nằm ngoài tầm với, kể cả khi id đó có trong allow-list. `403 VOICE_NOT_ALLOWED` dùng chung cho "voice không tồn tại" và "không được phép", nên không thể dò id voice của người khác.

Key admin vẫn không có quyền quản lý vòng đời credential: không có public route để rotate, revoke hoặc lộ secret của key khác. Xóa voice là ngoại lệ có chủ đích ở §4.1 và chỉ được phép khi bật **Quản lý kho audio** cùng phạm vi voice tương ứng.

REST request dùng Bearer header:

```http
Authorization: Bearer vc_sk_live_YOUR_KEY
```

Giữ key ở secret store hoặc backend tin cậy. Không commit key, đưa vào URL, log, client bundle hoặc mã nguồn công khai.

## 3. REST response envelope

Thành công:

```json
{
  "ok": true,
  "data": {}
}
```

Lỗi:

```json
{
  "ok": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable message"
  }
}
```

Riêng validation của voice enrollment có thể thêm `error.details[]`.

## 4. Tạo voice clone

```http
POST /api/v1/voices
Authorization: Bearer vc_ak_live_YOUR_KEY
Content-Type: multipart/form-data
```

### Form fields

| Field | Kiểu | Bắt buộc | Contract |
|---|---|---:|---|
| `file` | file | Có | Audio tối đa 10 MiB |
| `name` | string | Có | Không rỗng, tối đa 64 ký tự |
| `consent` | string | Có | Phải là `true`, xác nhận speaker đã cho phép clone voice |
| `description` | string | Không | Ghi chú cho voice |
| `gender` | string | Không | `male`, `female` hoặc `other`; mặc định hiện tại là `female` |

`consent` chỉ chấp nhận string chính xác `true` (không phân biệt hoa/thường); mọi giá trị khác, kể cả field thiếu, bị từ chối. `name` sau trim phải dài `1–64` ký tự. `description` là string optional và được trim; route không công bố giới hạn độ dài riêng cho field này.

Khi validation fail, server trả `400 VALIDATION_FAILED` và có thể liệt kê nhiều lỗi cùng lúc trong `error.details`:

| `details[].field` | `details[].code` có thể có | Khi nào |
|---|---|---|
| `file` | `MISSING`, `NOT_A_FILE`, `INVALID_TYPE`, `FILE_TOO_LARGE` | Thiếu upload, gửi text thay file, format không hợp lệ, hoặc vượt 10 MiB. `FILE_TOO_LARGE` kèm `limit_mb: 10`. |
| `name` | `MISSING`, `TOO_LONG` | Rỗng sau trim hoặc dài hơn 64. `TOO_LONG` kèm `max_length: 64`. |
| `consent` | `REQUIRED` | Không xác nhận speaker đã cho phép clone. |
| `gender` | `INVALID` | Không phải `male`, `female`, `other`. |

File được chấp nhận khi MIME bắt đầu bằng `audio/` hoặc extension thuộc danh sách:

```text
m4a mp3 wav wave aac ogg oga opus flac weba aiff aif aifc caf wma amr mp4 m4b
```

Ví dụ:

```bash
curl -X POST "https://studio.evomlabs.com/api/v1/voices" \
  -H "Authorization: Bearer vc_ak_live_YOUR_KEY" \
  -F "file=@sample.wav" \
  -F "name=Authorized sample voice" \
  -F "gender=female" \
  -F "consent=true"
```

### Response tạo mới

HTTP `201`:

```json
{
  "ok": true,
  "data": {
    "voice_id": "voice_id",
    "name": "Authorized sample voice",
    "status": "ready",
    "deduped": false,
    "gender": "female",
    "description": "",
    "file_name": "sample.wav",
    "file_size": 482310,
    "created_at": "2026-08-03T00:00:00.000Z",
    "key_status": "created",
    "api_key": {
      "key": "vc_sk_live_NEW_KEY",
      "last_four": "ABCD",
      "scopes": ["tts.generate", "tts.stream", "usage.read"]
    }
  }
}
```

Public endpoint trả plaintext voice key khi `key_status` là `created`. Hãy lưu key trước khi bỏ response.

### Dedupe và retry

Audio được dedupe trong phạm vi account. Upload lại cùng nội dung trả HTTP `200` với `deduped: true`:

| Trạng thái voice hiện có | `key_status` | `api_key` |
|---|---|---|
| Chưa có voice key active | `created` | Có key mới |
| Đã có voice key active | `exists` | `null` |

Endpoint không trả lại secret của một key đang tồn tại. Với account thông thường, public enrollment có giới hạn 20 voice. Rate limit mặc định của account key là 6 enrollment/phút; response `429` có header `Retry-After`.

## 4.1. Xoá voice

```http
DELETE /api/v1/voices/{id}
Authorization: Bearer vc_ak_live_YOUR_KEY
```

Yêu cầu account key bật công tắc **Quản lý kho audio** — cùng công tắc với enrollment. Tắt công tắc thì route trả `403 PERMISSION_DENIED`, kể cả khi voice thuộc account.

Chỉ xoá được voice do chính account tạo và nằm trong phạm vi voice của key. Voice dùng chung (preset), voice của account khác, id không tồn tại và voice ngoài allow-list đều trả cùng một lỗi `403 VOICE_NOT_ALLOWED` — cố tình gộp để không dò được id voice của người khác.

```bash
curl -X DELETE "https://studio.evomlabs.com/api/v1/voices/VOICE_ID" \
  -H "Authorization: Bearer vc_ak_live_YOUR_KEY"
```

HTTP `200`:

```json
{
  "ok": true,
  "data": { "voice_id": "voice_id", "name": "Authorized sample voice", "deleted": true }
}
```

Thao tác không hoàn tác được: audio và feature trên R2 bị xoá, và voice key `vc_sk_live_*` gắn với voice đó bị xoá theo (cascade). Lịch sử generate vẫn giữ nguyên. Rate limit 30 lượt xoá/phút cho mỗi account key; response `429` có header `Retry-After`.

## 5. Batch generation

```http
POST /api/v1/tts/generate
Authorization: Bearer vc_sk_live_YOUR_KEY
Content-Type: application/json
```

### Request body

| Field | Kiểu | Bắt buộc | Mặc định và hành vi hiện tại |
|---|---|---:|---|
| `text` | string | Có | Không rỗng; tối đa 5.000 ký tự |
| `voice_id` | string | Chỉ với account key | Bắt buộc khi dùng `vc_ak_live_*`. Với `vc_sk_live_*` thì bỏ trống; nếu gửi thì phải trùng voice của key |
| `language` | string | Không | `auto`; chỉ `auto`, một code hoặc tên ngôn ngữ trong [catalog đầy đủ 646 ngôn ngữ](./tts-languages.md). Tên không phân biệt hoa/thường và được chuẩn hoá thành code. |
| `format` | string | Không | `mp3`; chỉ `mp3` hoặc `wav`. Container/MIME trả về khớp giá trị này. |
| `speed` | number | Không | `1.0`; clamp vào `0.5–1.5` |
| `cfg_value` | finite number | Không | `2.0`; chỉ `0.0–4.0`, gồm cả `0` |
| `dit_steps` | integer | Không | `10`; chỉ `0–64`, gồm cả `0` |
| `do_normalize` | boolean | Không | `false` |
| `denoise` | boolean | Không | `false` trên route public batch |
| `preprocess_prompt` | boolean | Không | `true` |
| `postprocess_output` | boolean | Không | `true` |
| `control_instruction` | string | Không | Chuỗi rỗng; không dùng để chọn voice khác với voice gắn vào key |
| `use_prompt_text` | boolean | Không | `false`; advanced compatibility field |
| `prompt_text` | string | Không | Chuỗi rỗng; advanced compatibility field |

Quota được trừ trước, theo công thức `số ký tự × (dit_steps / 8)`, làm tròn lên. Số ký tự tính cả whitespace có trong string gửi lên.

Số bước nằm trong giá vì chi phí GPU tuyến tính theo nó: vòng lặp sinh audio chạy đúng `dit_steps` lượt forward trên toàn bộ sequence, mỗi lượt tốn như nhau. Mốc chuẩn là 8 bước — ở đúng mức đó credit bằng số ký tự. Mặc định của endpoint này là `dit_steps: 10`, tức hệ số `1.25`; đặt `dit_steps: 32` là hệ số `4`.

`dit_steps: 0` vẫn bị tính bằng 1 bước, vì backend luôn chạy tối thiểu một bước.

Nếu key có trần ký tự riêng (đặt trong Studio), trần đó được trừ **trước** quota tài khoản: key hết credit trả `403 KEY_CREDIT_EXCEEDED` mà không tiêu quota của tài khoản. Trần này tính theo vòng đời của key, không reset hàng tháng.

```bash
curl -X POST "https://studio.evomlabs.com/api/v1/tts/generate" \
  -H "Authorization: Bearer vc_sk_live_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Xin chào, đây là một bản thử giọng.",
    "language": "vi",
    "format": "wav",
    "speed": 1.0,
    "cfg_value": 2.0,
    "dit_steps": 10
  }'
```

### Response

```json
{
  "ok": true,
  "data": {
    "success": true,
    "request_id": "generation_id",
    "voice_id": "voice_id",
    "audio_url": "https://cdn.example/audio.wav",
    "text": "Xin chào, đây là một bản thử giọng.",
    "duration": null,
    "status": "completed",
    "chars_deducted": 44,
    "format": "wav",
    "sample_rate": 24000
  }
}
```

`chars_deducted` là **số credit đã trừ**, không phải độ dài text: ở ví dụ trên là `ceil(35 × 10/8) = 44`. Hai con số này chỉ trùng nhau khi `dit_steps` đúng bằng 8. Dùng field này để đối soát số dư thay vì tự đếm ký tự.

`POST /generate` giữ response URL/R2 để tương thích. Dùng `/bytes` nếu caller cần audio bytes ngay trong response; dùng `/sse` hoặc WebSocket nếu cần frame PCM16 sớm hơn.

## 6. Text-to-Speech bytes

```http
POST /api/v1/tts/bytes
Authorization: Bearer vc_sk_live_YOUR_KEY
Content-Type: application/json
```

Body có đúng toàn bộ field ở [§5](#5-batch-generation), gồm `voice_id` khi dùng account key. Đây là transport request/response: server hoàn tất generation rồi trả trực tiếp file, không ghi thêm bản durable vào R2 và không trả JSON success envelope.

Response `200` luôn là binary audio:

| Header | Giá trị |
|---|---|
| `Content-Type` | `audio/wav` khi `format="wav"`; `audio/mpeg` khi `format="mp3"` |
| `Content-Disposition` | `attachment; filename="oriagent.wav"` hoặc `oriagent.mp3` |
| `X-OriAgent-Format` | `wav` hoặc `mp3` |
| `X-OriAgent-Sample-Rate` | integer Hz do model trả; chỉ có khi backend cung cấp |
| `X-OriAgent-Chars-Deducted` | integer bằng `text.length` |
| `Cache-Control` | `private, no-store` |

Ví dụ ghi bytes ra file:

```bash
curl -X POST "https://studio.evomlabs.com/api/v1/tts/bytes" \
  -H "Authorization: Bearer vc_sk_live_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text":"Xin chào","language":"vi","format":"wav"}' \
  --output hello.wav
```

Lỗi vẫn dùng REST error envelope ở §3, với `Content-Type: application/json`; đừng cố decode body lỗi như audio.

## 7. Text-to-Speech SSE

```http
POST /api/v1/tts/sse
Authorization: Bearer vc_sk_live_YOUR_KEY
Accept: text/event-stream
Content-Type: application/json
```

Body dùng cùng field với §5. `format` vẫn được validate là `wav` hoặc `mp3` để các transport có cùng contract, nhưng SSE **luôn** phát PCM16 (`pcm_s16le`) vì chỉ dạng raw frame mới có thể đưa audio sớm qua text-only SSE. SSE không trả file URL; client ghép các `audio` event rồi tự phát PCM hoặc đóng gói WAV.

Response thành công có `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache, no-store`, `X-Accel-Buffering: no` và `X-OriAgent-Chars-Deducted`. Vì request là `POST`, browser client dùng `fetch()` + `ReadableStream`; `EventSource` chỉ hỗ trợ GET nên không dùng được cho endpoint này.

Thứ tự event hợp lệ:

```text
event: start
data: {"sample_rate":24000,"channels":1,"format":"pcm_s16le","encoding":"base64"}

event: audio
data: {"sequence":1,"audio":"<base64 PCM16 little-endian>"}

event: done
data: {"chunks":12,"duration_ms":5230,"elapsed_ms":6100}
```

| Event | Số lần | Data | Xử lý client |
|---|---:|---|---|
| `start` | 1 | `sample_rate` integer, `channels: 1`, `format: "pcm_s16le"`, `encoding: "base64"` | Lưu metadata; không hard-code sample rate. |
| `audio` | 1+ | `sequence` tăng từ 1; `audio` base64 của một frame PCM16 little-endian mono | Decode base64 theo thứ tự `sequence`, phát hoặc nối byte. |
| `done` | 1 | `chunks`, `duration_ms`, `elapsed_ms` là integer không âm | Không còn audio event. |
| `error` | 0 hoặc 1 | `code: "GENERATION_FAILED"`, `message` | Stream đã bắt đầu nhưng generation lỗi; không có `done`. |

Quota bị trừ trước khi mở stream, theo cùng công thức `số ký tự × (dit_steps / 8)` như batch. Client hủy fetch có thể dừng nhận data nhưng không hoàn quota đã dành.

## 8. Cấp stream token

```http
POST /api/v1/tts/stream-token
Authorization: Bearer vc_sk_live_YOUR_KEY
Content-Type: application/json
```

Request:

```json
{
  "text_length": 200
}
```

`text_length` phải là JSON integer dương (`1–10.000`), không nhận string, decimal, `NaN` hoặc `Infinity`. Gửi giá trị bằng đúng độ dài text dự kiến. Quota bị trừ ngay theo giá trị này và hiện không được hoàn lại nếu token không được dùng hoặc stream bị hủy.

Khi xác thực bằng `vc_ak_live_*`, thêm `voice_id` vào body giống endpoint generate.

Response:

```json
{
  "ok": true,
  "data": {
    "stream_token": "SHORT_LIVED_TOKEN",
    "ws_url": "wss://studio.evomlabs.com/ws/tts/stream",
    "max_length": 200,
    "chars_deducted": 200,
    "expires_in": 60
  }
}
```

Token hết hạn sau 60 giây. Source hiện chỉ kiểm tra chữ ký và expiry; không có enforcement one-time-use được công bố. Mở WebSocket ngay sau khi nhận token và không log token hoặc URL chứa token.

## 9. WebSocket streaming

Kết nối bằng URL do endpoint stream-token trả về:

```text
<ws_url>?token=<stream_token>
```

Message đầu tiên từ client:

```json
{
  "type": "start",
  "text": "Xin chào từ OriAgent",
  "language": "vi",
  "speed": 1.0,
  "control_instruction": "",
  "cfg_value": 2.0,
  "dit_steps": 8,
  "use_prompt_text": false,
  "prompt_text": "",
  "do_normalize": false,
  "denoise": true,
  "preprocess_prompt": true,
  "postprocess_output": true
}
```

| Field | Mặc định/giới hạn |
|---|---|
| `type` | Phải là `start` |
| `text` | Không vượt `max_length` trong token |
| `language` | `auto`; `auto`, code hoặc tên trong [TTS language catalog](./tts-languages.md) |
| `speed` | `1.0`, clamp `0.5–1.5` |
| `control_instruction` | Chuỗi rỗng; bị bỏ qua khi stream dùng cloned-voice prompt |
| `cfg_value` | `2.0`, khoảng `0.0–4.0` |
| `dit_steps` | `8`, khoảng `0–64` |
| `use_prompt_text` | `false`; advanced compatibility field |
| `prompt_text` | Chuỗi rỗng; advanced compatibility field |
| `do_normalize` | `false` |
| `denoise` | `true` |
| `preprocess_prompt` | `true` |
| `postprocess_output` | `true` |

Voice được khóa trong stream token; client không được đổi voice trong message `start`.

### Server events

Metadata mở đầu:

```json
{
  "type": "start",
  "mode": "fast",
  "sample_rate": 24000,
  "format": "pcm16",
  "channels": 1
}
```

Sau metadata là nhiều binary frame PCM16 little-endian, mono, ở `sample_rate` do event cung cấp. Kích thước mục tiêu hiện tại là khoảng 200 ms/frame; client không nên hard-code số byte cho một frame.

Hoàn tất:

```json
{
  "type": "done",
  "mode": "fast",
  "audio_url": "/api/tts/file/output.wav",
  "chunks": 12,
  "ttfb_ms": 480,
  "duration_ms": 5230
}
```

`audio_url` có thể là relative path. Resolve nó với public origin nếu client cần file hoàn chỉnh.

Lỗi hoặc hủy:

```json
{"type":"error","message":"Error description"}
{"type":"cancelled"}
```

Token thiếu/sai/hết hạn tạo event `error` rồi server đóng socket với code `4401`. Client phải phân biệt binary frame với JSON text frame. Đóng socket để hủy generation.

## 10. Usage và quota

```http
GET /api/v1/usage
Authorization: Bearer vc_sk_live_YOUR_KEY
```

```json
{
  "ok": true,
  "data": {
    "environment": "live",
    "limit": 500000,
    "used": 1234,
    "remaining": 498766,
    "reset_date": "2026-09-01T00:00:00.000Z",
    "reset_interval": "month",
    "reset_interval_count": 1,
    "unlimited": false,
    "key": {
      "kind": "tts",
      "name": "Landing page voice",
      "last_four": "9f2c",
      "credit_limit": 200,
      "credit_used": 12,
      "credit_remaining": 188,
      "credit_source": "key",
      "spendable": 188
    }
  }
}
```

`key` mô tả trần vòng đời của chính key đang gọi: `credit_limit` `null` nghĩa là key không có trần riêng (`credit_remaining` cũng `null`, `credit_source: "account"`). `spendable` là số nhỏ hơn giữa trần key và `remaining` của tài khoản - đó mới là số caller thực sự tiêu được kế tiếp, vì mỗi lần tiêu trừ CẢ HAI counter.

### Admin key: chỉ đọc trần của chính nó

```http
GET /api/v1/usage
Authorization: Bearer vc_ak_live_YOUR_KEY
```

```json
{
  "ok": true,
  "data": {
    "environment": "live",
    "key": {
      "kind": "admin",
      "name": "Contractor key",
      "last_four": "4a71",
      "credit_limit": 200,
      "credit_used": 12,
      "credit_remaining": 188,
      "credit_source": "key"
    }
  }
}
```

Account key đọc được trần của chính nó và **không** đọc được quota tài khoản: không có `limit`, `used`, `remaining`, `unlimited` hay `reset_*`, và cũng không có `spendable` - `spendable` là `min(trần key, remaining tài khoản)` nên khi tài khoản chặt hơn nó sẽ lộ đúng số dư tài khoản.

Hệ quả: admin key không đặt trần riêng nhận `credit_limit: null` và `credit_remaining: null` với `credit_source: "account"`. Nó tiêu vào quota tài khoản, và con số đó đúng là thứ credential này không được đọc. Không yêu cầu permission nào ngoài một account key còn hiệu lực, vì response không mô tả gì ngoài chính credential đang gọi.

- Batch và SSE trừ quota theo `số ký tự × (dit_steps / 8)`, làm tròn lên.
- Streaming qua WebSocket trừ quota khi cấp token theo `text_length`, không nhân hệ số: `/stream-token` không nhận `dit_steps` và WS luôn chạy ở mốc chuẩn 8 bước, nên hệ số của nó luôn bằng 1.
- Reset được áp dụng lazy khi quota được đọc hoặc trừ; dùng `reset_date` từ response thay vì tự suy đoán timezone.
- `reset_interval` + `reset_interval_count` là chu kỳ cấp lại của gói: đơn vị (`never` | `hour` | `day` | `week` | `month`) và số đơn vị mỗi chu kỳ - `"hour"` + `6` nghĩa là mỗi 6 giờ. Admin đặt được cho từng gói nên đừng giả định luôn là hàng tháng.
- `reset_interval: "never"` nghĩa là gói KHÔNG cấp lại credit: đã tiêu là mất cho tới khi admin đổi gói hoặc đổi chu kỳ. Khi đó **`reset_date` là `null`** và `reset_interval_count` luôn là `1`. Client phải xử lý `null` ở đây - `new Date(null)` cho ra 1970 chứ không báo lỗi, nên một client cũ sẽ âm thầm hiển thị sai ngày thay vì crash. Đây cũng là giá trị `null` duy nhất `reset_date` có thể nhận.
- `unlimited: true` (gói Max) nghĩa là `limit`/`remaining` chỉ là số canh chừng, không phải hạn mức thật - đừng hiển thị chúng như một con số hạn mức.

## 11. API Key Admin: danh sách key và usage

```http
GET /api/v1/keys
Authorization: Bearer vc_ak_live_YOUR_KEY
```

Endpoint này yêu cầu account key còn active, chưa hết hạn, và permission `keyManagement: true`. Nó chỉ trả metadata và usage — **không bao giờ** trả plaintext, ciphertext hay hash của bất kỳ key nào.

Response chỉ chứa những key mà **chính key đang gọi** với tới được, không phải toàn bộ account:

| Nhóm | Key được liệt kê |
|---|---|
| `keys.admin` | Chỉ chính key đang gọi. Admin key khác trên cùng account **không** xuất hiện — permission, trần credit và `lastUsedAt` của một credential khác không phải thứ key này được đọc. |
| `keys.tts` | Chỉ key gắn với voice mà key đang gọi được phép nói, theo đúng `voiceScope` mà route TTS áp dụng. `endpoints.tts: false` → array rỗng. |
| `keys.stt` | Chỉ khi `endpoints.stt: true`, ngược lại array rỗng. STT key không có ràng buộc theo voice nên công tắc endpoint là toàn bộ điều kiện. |

Hệ quả: hai admin key trên cùng một account nhìn thấy hai response khác nhau, và không key nào liệt kê được key còn lại.

Response `200`:

```json
{
  "ok": true,
  "data": {
    "keys": {"admin": [], "tts": [], "stt": []}
  }
}
```

Endpoint này **không** trả quota tài khoản. Nó từng có `account_usage` (tts/stt `limit`/`used`/`remaining` kèm lịch reset); field đó đã bị bỏ theo cùng một quy tắc với `GET /api/v1/usage`: account key đọc được trần của các key, không đọc được số dư của tài khoản. Quota tài khoản chỉ còn đọc được bằng voice key qua `GET /api/v1/usage`, hoặc trong Studio khi đăng nhập.

`keys.admin`, `keys.tts`, `keys.stt` luôn là array (có thể rỗng); `keys.admin` có đúng một phần tử là chính key đang gọi. Mọi object key đều có `id`, `name`, `prefix`, `lastFour`, `scopes` (array string), `isActive`, `usageCount`, `rateLimit`, `expiresAt`, `revokedAt`, `lastUsedAt`, `createdAt`, và `usage`:

| Field trong `usage` | Kiểu | Ý nghĩa |
|---|---|---|
| `credit_limit` | positive integer hoặc `null` | Trần ký tự vòng đời của chính key; `null` = key không có trần riêng. |
| `credit_used` | integer ≥ 0 | Số ký tự key đã dùng trong vòng đời. |
| `credit_remaining` | integer ≥ 0 hoặc `null` | `max(credit_limit - credit_used, 0)`; `null` khi không có trần riêng. |

`keys.admin[*]` còn có `permissions`, với các enum/boolean chính xác sau: `endpoints.tts` boolean, `endpoints.stt` boolean, `audioUpload` boolean, `keyManagement` boolean, `voiceScope` là một trong `all` | `mine` | `custom`, và `voiceIds` là array tối đa 200 id (không rỗng khi `voiceScope="custom"`). `keys.tts[*]` có `environment: "live"` và `voice: { id, name } | null`. Không có field riêng thêm cho `keys.stt[*]`.

`usage` trong từng key là trần vòng đời của key, không phải quota tài khoản. Hai loại trần cùng áp dụng - mỗi lần tiêu trừ cả trần key lẫn quota tài khoản - nên giá trị caller thực sự còn dùng được là giá trị nhỏ hơn trong các giới hạn hữu hạn liên quan. Từ endpoint này thì nửa còn lại (quota tài khoản) không đọc được.

## 12. Mã lỗi chính

| Code | HTTP thường gặp | Ý nghĩa |
|---|---:|---|
| `INVALID_API_KEY` | 401/403 | Key thiếu, sai, inactive, revoked, expired, sai loại hoặc thiếu scope |
| `PERMISSION_DENIED` | 403 | Account key hợp lệ nhưng công tắc TTS/STT/audio upload đang tắt |
| `VOICE_ID_REQUIRED` | 400 | Dùng account key mà không gửi `voice_id` |
| `VOICE_NOT_ALLOWED` | 403 | Voice không tồn tại, ngoài phạm vi của account key, hoặc khác voice gắn vào voice key |
| `ACCOUNT_DISABLED` | 403 | Account của enrollment key bị vô hiệu hóa |
| `VOICE_NOT_FOUND` | 404 | Voice gắn với voice key không còn tồn tại |
| `VALIDATION_FAILED` | 400 | Voice enrollment có field sai; xem `error.details[]` |
| `VOICE_LIMIT_REACHED` | 403 | Account đã đạt giới hạn public enrollment |
| `RATE_LIMITED` | 429 | Enrollment quá nhanh; đọc `Retry-After` |
| `ENROLLMENT_FAILED` | 503 | Không thể hoàn tất voice enrollment |
| `BAD_REQUEST` | 400 | Thiếu hoặc sai request field |
| `TEXT_TOO_LONG` | 400 | Batch text vượt 5.000 ký tự |
| `INVALID_LANGUAGE` | 400 | `language` không có trong [TTS language catalog](./tts-languages.md) |
| `INVALID_FORMAT` | 400 | `format` khác `wav` hoặc `mp3` |
| `KEY_CREDIT_EXCEEDED` | 403 | Key đã tiêu hết trần ký tự riêng của nó |
| `QUOTA_EXCEEDED` | 403 | Không đủ quota của tài khoản |
| `VOICE_FEATURE_UNAVAILABLE` | 503 | Không tải được feature/reference của voice; request không được gửi sang model |
| `BACKEND_ERROR` | 4xx/5xx | TTS generation backend từ chối hoặc lỗi |
| `NOT_FOUND` | 404 | Không tìm thấy quota/resource |
| `INTERNAL_ERROR` | 500 | Lỗi hệ thống |

Với WebSocket, lỗi đi qua JSON event thay vì REST envelope.

## 13. Tài liệu liên quan

- [STT public API](./stt-api.md)
- [TTS language catalog — đầy đủ mọi giá trị `language`](./tts-languages.md)
- [System overview](../overview.md)
- [Internal route ownership](../specs/route-ownership.md) — dành cho maintainer, không phải public integration contract
