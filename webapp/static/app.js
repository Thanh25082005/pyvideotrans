'use strict';

const $ = (id) => document.getElementById(id);
const state = { file: null, jobId: null, logIndex: 0, poller: null, ticker: null, baseElapsed: 0, baseAt: 0,
                words: [], wordsStamp: -1 };

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

/* ------------------------------------------------------------------ cấu hình */
function collectConfig() {
  return {
    stt: { base_url: $('stt-base').value.trim(), api_key: $('stt-key').value.trim() },
    openai: {
      base_url: $('openai-base').value.trim(),
      api_key: $('openai-key').value.trim(),
      model: $('openai-model').value.trim(),
      batch_size: Number($('openai-batch').value) || 20,
      instruction: $('openai-instruction').value.trim(),
    },
    tts: {
      base_url: $('tts-base').value.trim(),
      api_key: $('tts-key').value.trim(),
      voice_id: $('tts-voice').value.trim(),
    },
  };
}

function paintBadges(ready) {
  $('badge-stt').classList.toggle('on', !!ready.stt);
  $('badge-openai').classList.toggle('on', !!ready.openai);
  $('badge-tts').classList.toggle('on', !!ready.tts);
}

async function loadConfig() {
  const { config, ready } = await api('/api/config');
  $('stt-base').value = config.stt.base_url || '';
  $('stt-key').value = config.stt.api_key || '';
  $('openai-base').value = config.openai.base_url || '';
  $('openai-key').value = config.openai.api_key || '';
  $('openai-model').value = config.openai.model || '';
  $('openai-batch').value = config.openai.batch_size || 20;
  $('tts-base').value = config.tts.base_url || '';
  $('tts-key').value = config.tts.api_key || '';
  $('tts-voice').value = config.tts.voice_id || '';
  $('speed').value = config.tts.speed ?? 1;
  $('openai-instruction').value = config.openai.instruction || '';
  syncInstruction();
  $('dit-steps').value = config.tts.dit_steps ?? 16;
  syncSliders();
  $('max-speed').value = config.pipeline?.max_audio_speed ?? 1.6;
  paintBadges(ready);
}

async function saveConfig() {
  const message = $('config-msg');
  message.textContent = 'Đang lưu…';
  message.className = 'hint';
  try {
    const payload = collectConfig();
    payload.tts.speed = Number($('speed').value) || 1;
    payload.tts.dit_steps = Number($('dit-steps').value) || 16;
    payload.pipeline = { max_audio_speed: Number($('max-speed').value) || 1.6 };
    await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const { ready } = await api('/api/config');
    paintBadges(ready);
    message.textContent = 'Đã lưu cấu hình vào webapp/data/config.json';
    message.className = 'hint ok';
  } catch (error) {
    message.textContent = `Lỗi: ${error.message}`;
    message.className = 'hint err';
  }
}

async function testProvider(name) {
  const target = $(`${name}-msg`);
  target.textContent = 'Đang kiểm tra…';
  target.className = 'hint';
  try {
    const result = await api(`/api/config/test/${name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectConfig()),
    });
    target.textContent = result.message;
    target.className = result.ok ? 'hint ok' : 'hint err';
  } catch (error) {
    target.textContent = `Lỗi: ${error.message}`;
    target.className = 'hint err';
  }
}

/* --------------------------------------------------------------- ngôn ngữ */
async function loadLanguages() {
  const { source, target } = await api('/api/languages');
  $('source-lang').innerHTML = source
    .map((l) => `<option value="${l.code}">${l.label}</option>`).join('');
  $('target-lang').innerHTML = target
    .map((l) => `<option value="${l.code}">${l.label}</option>`).join('');
  $('source-lang').value = 'auto';
  $('target-lang').value = 'vi';
}

async function loadVoices() {
  const button = $('load-voices');
  button.disabled = true;
  button.textContent = '…';
  try {
    const result = await api('/api/voices');
    const options = ['<option value="">— Dùng voice mặc định trong cấu hình —</option>']
      .concat((result.voices || []).map((v) => `<option value="${v.id}">${v.name}</option>`));
    $('voice-select').innerHTML = options.join('');
    $('tts-msg').textContent = result.message || '';
    $('tts-msg').className = result.ok ? 'hint ok' : 'hint err';
  } catch (error) {
    $('tts-msg').textContent = `Lỗi: ${error.message}`;
    $('tts-msg').className = 'hint err';
  } finally {
    button.disabled = false;
    button.textContent = 'Tải';
  }
}

/* ------------------------------------------------------------------- file */
function setFile(file) {
  state.file = file;
  if (!file) return;
  $('dz-title').textContent = file.name;
  $('dz-sub').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB — bấm để đổi file khác`;
  $('start-btn').disabled = false;
}

function initDropzone() {
  const zone = $('dropzone');
  const input = $('file-input');
  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => setFile(input.files[0]));
  ['dragenter', 'dragover'].forEach((type) => zone.addEventListener(type, (event) => {
    event.preventDefault();
    zone.classList.add('drag');
  }));
  ['dragleave', 'drop'].forEach((type) => zone.addEventListener(type, (event) => {
    event.preventDefault();
    zone.classList.remove('drag');
  }));
  zone.addEventListener('drop', (event) => {
    const file = event.dataTransfer.files[0];
    if (file) setFile(file);
  });
}

/* -------------------------------------------------------------------- job */
function formatClock(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(total / 60)).padStart(2, '0');
  const ss = String(total % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

function appendLogs(entries) {
  const box = $('job-log');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  for (const entry of entries) {
    const line = document.createElement('div');
    const time = new Date(entry.t * 1000).toLocaleTimeString('vi-VN', { hour12: false });
    line.className = entry.level;
    line.innerHTML = `<span class="t">${time}</span>`;
    line.appendChild(document.createTextNode(entry.msg));
    box.appendChild(line);
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function startTicker() {
  stopTicker();
  state.ticker = setInterval(() => {
    const seconds = state.baseElapsed + (Date.now() - state.baseAt) / 1000;
    $('job-timer').textContent = formatClock(seconds);
  }, 500);
}
function stopTicker() { if (state.ticker) clearInterval(state.ticker); state.ticker = null; }

async function startJob() {
  if (!state.file) return;
  const form = new FormData();
  form.append('file', state.file);
  form.append('source_lang', $('source-lang').value);
  form.append('target_lang', $('target-lang').value);
  form.append('voice_id', $('voice-select').value);
  form.append('speed', $('speed').value || '1');
  form.append('dit_steps', $('dit-steps').value || '16');
  form.append('background_volume', bgVolumeParam());
  form.append('background_source', $('background-source').value);
  form.append('original_voice_volume', String(Number($('voice-volume').value) / 100));
  form.append('instruction', $('job-instruction').value.trim());
  form.append('dubbed_volume', String(Number($('dubbed-volume').value) / 100));
  form.append('max_audio_speed', $('max-speed').value || '1.6');
  form.append('voice_autorate', $('voice-autorate').checked);
  form.append('mix_original_audio', $('mix-original-audio').checked);
  form.append('resynth', $('resynth').checked);
  form.append('burn_subtitle', $('burn-sub').checked);
  form.append('soft_subtitle', $('soft-sub').checked);
  form.append('clone_voice', $('clone-voice').checked);

  const startMsg = $('start-msg');
  $('start-btn').disabled = true;
  startMsg.textContent = 'Đang tải file lên…';
  startMsg.className = 'hint';

  try {
    const { job_id: jobId } = await api('/api/jobs', { method: 'POST', body: form });
    state.jobId = jobId;
    state.logIndex = 0;
    state.baseElapsed = 0;
    state.baseAt = Date.now();
    $('job-log').innerHTML = '';
    $('progress-card').hidden = false;
    $('result-card').hidden = true;
    resetWords();
    $('cancel-btn').disabled = false;
    startMsg.textContent = `Job ${jobId} đang chạy`;
    $('progress-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
    startTicker();
    poll();
    state.poller = setInterval(poll, 900);
  } catch (error) {
    startMsg.textContent = `Lỗi: ${error.message}`;
    startMsg.className = 'hint err';
    $('start-btn').disabled = false;
  }
}

async function poll() {
  if (!state.jobId) return;
  let snapshot;
  try {
    snapshot = await api(`/api/jobs/${state.jobId}?log_from=${state.logIndex}`);
  } catch { return; }

  state.logIndex = snapshot.log_total;
  appendLogs(snapshot.logs || []);
  pollWords();
  state.baseElapsed = snapshot.elapsed;
  state.baseAt = Date.now();

  $('job-bar').style.width = `${snapshot.progress}%`;
  $('job-stage').textContent = snapshot.stage;
  const statusLabels = { queued: 'Đang chờ', running: 'Đang chạy', done: 'Hoàn tất', error: 'Lỗi', cancelled: 'Đã dừng' };
  const pill = $('job-status');
  pill.textContent = statusLabels[snapshot.status] || snapshot.status;
  pill.className = `pill ${snapshot.status}`;

  if (['done', 'error', 'cancelled'].includes(snapshot.status)) {
    clearInterval(state.poller);
    state.poller = null;
    stopTicker();
    $('job-timer').textContent = formatClock(snapshot.elapsed);
    $('start-btn').disabled = false;
    $('cancel-btn').disabled = true;
    pollWords(true);
    if (snapshot.status === 'done') showResult(snapshot);
    else {
      $('start-msg').textContent = snapshot.error || 'Tiến trình đã dừng';
      $('start-msg').className = 'hint err';
    }
  }
}


// dit_steps: quota Loly tính theo bội số 8 bước, nên 32 bước tốn gấp 4 lần 8 bước
function ditLabel(steps) {
  const cost = (steps / 8).toFixed(2);
  let tag = 'cân bằng';
  if (steps <= 8) tag = 'tiết kiệm';
  else if (steps <= 20) tag = 'cân bằng';
  else if (steps <= 36) tag = 'tốt';
  else tag = 'cao nhất, chậm';
  return `${steps} — ${tag}, chi phí ×${cost}`;
}

// Slider nền có một nấc "mặc định" nằm dưới 0 để phân biệt với "tắt hẳn tiếng nền"
function bgVolumeParam() {
  const raw = Number($('background-volume').value);
  return raw < 0 ? '-1' : String(raw / 100);
}

// Đếm ký tự để người dùng biết mình sắp chạm trần 4000
function syncInstruction() {
  const n = $('openai-instruction').value.trim().length;
  const el = $('instruction-count');
  el.textContent = `${n} / 4000`;
  el.className = n > 4000 ? 'val err' : 'val';
}

function syncSliders() {
  const dit = Number($('dit-steps').value) || 16;
  $('dit-steps-val').textContent = ditLabel(dit);
  const bg = Number($('background-volume').value);
  $('background-volume-val').textContent = bg < 0 ? 'mặc định' : (bg === 0 ? 'tắt' : `${bg}%`);
  const voice = Number($('voice-volume').value);
  $('voice-volume-val').textContent = voice === 0 ? 'tắt' : `${voice}%`;
  $('dubbed-volume-val').textContent = `${Number($('dubbed-volume').value)}%`;
}

function formatTokens(result) {
  const usage = result.token_usage;
  if (!usage) return '';
  const n = (x) => Number(x || 0).toLocaleString('vi-VN');
  const rows = [
    `<div><span>Tổng token</span><b>${n(usage.total_tokens)}</b></div>`,
    `<div><span>Gửi lên (input)</span><b>${n(usage.prompt_tokens)}</b></div>`,
    `<div><span>Nhận về (output)</span><b>${n(usage.completion_tokens)}</b></div>`,
  ];
  if (usage.cached_tokens) {
    const share = Math.round((usage.cached_tokens / Math.max(1, usage.prompt_tokens)) * 100);
    rows.push(`<div><span>Trong đó vào cache (rẻ hơn)</span><b>${n(usage.cached_tokens)} · ${share}%</b></div>`);
  }
  if (usage.reasoning_tokens) {
    rows.push(`<div><span>Token suy luận</span><b>${n(usage.reasoning_tokens)}</b></div>`);
  }
  rows.push(`<div><span>Số request</span><b>${n(usage.calls)}</b></div>`);
  let warn = '';
  if (usage.missing_usage) {
    warn = `<p class="hint err">⚠ ${n(usage.missing_usage)} request trả về không kèm trường
      <code>usage</code> nên token của chúng KHÔNG nằm trong con số trên — số thực tế cao hơn.
      Thường gặp khi <code>openai.base_url</code> trỏ vào proxy tương thích thay vì api.openai.com.</p>`;
  }

  const cost = result.token_cost;
  let money;
  if (cost) {
    money = `<div class="cost"><span>Ước tính chi phí · ${cost.model}</span><b>$${Number(cost.total_usd).toFixed(4)}</b></div>`
      + `<p class="hint">Số token là con số OpenAI trả về nên chính xác tuyệt đối. Tiền tính theo bảng giá
         <code>openai.pricing</code> trong cấu hình — hãy đối chiếu bảng giá hiện hành rồi sửa lại nếu lệch.</p>`;
  } else {
    money = `<p class="hint">Model này chưa có trong bảng giá <code>openai.pricing</code> nên chỉ hiện số token,
             không quy ra tiền.</p>`;
  }
  return `<div class="tokens"><h3>Token OpenAI đã dùng</h3>${rows.join('')}${warn}${money}</div>`;
}

async function showResult(snapshot) {
  const result = snapshot.result || {};
  const base = `/api/jobs/${snapshot.id}/file`;
  $('result-card').hidden = false;
  $('start-msg').textContent = `Xong sau ${formatClock(snapshot.elapsed)} — ${result.lines || 0} câu thoại`;
  $('start-msg').className = 'hint ok';

  const video = $('result-video');
  if (result.video) {
    video.hidden = false;
    video.src = `${base}/video`;
  } else {
    video.hidden = true;
  }

  const links = [`<a class="dl main edit" href="/editor?job=${snapshot.id}">✎ Chỉnh sửa &amp; ghép lại</a>`];
  if (result.video) links.push(`<a class="dl main" href="${base}/video?download=1">⬇ Tải video đã lồng tiếng</a>`);
  if (result.audio) links.push(`<a class="dl main" href="${base}/audio?download=1">⬇ Tải audio đã lồng tiếng</a>`);
  if (result.dubbed_audio) links.push(`<a class="dl" href="${base}/dubbed_audio?download=1">Track lồng tiếng (wav)</a>`);
  if (result.source_srt) links.push(`<a class="dl" href="${base}/source_srt?download=1">Phụ đề gốc (.srt)</a>`);
  if (result.target_srt) links.push(`<a class="dl" href="${base}/target_srt?download=1">Phụ đề dịch (.srt)</a>`);
  if (result.word_timestamps) links.push(`<a class="dl" href="${base}/word_timestamps?download=1">Word timestamps (.json)</a>`);
  if (result.align_log) links.push(`<a class="dl" href="${base}/align_log?download=1">Nhật ký căn chỉnh (.log)</a>`);
  $('download-row').innerHTML = links.join('');
  $('token-usage').innerHTML = formatTokens(result);

  for (const [kind, node] of [['source_srt', 'srt-source'], ['target_srt', 'srt-target']]) {
    try {
      const data = await api(`/api/jobs/${snapshot.id}/subtitles/${kind}`);
      $(node).textContent = data.text || '(trống)';
    } catch { $(node).textContent = ''; }
  }
  $('result-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ------------------------------------------------- word-level timestamps */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function clockOf(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const ms = Math.max(0, Math.round(seconds * 1000));
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:`
    + `${pad(Math.floor(ms / 1000) % 60)}.${pad(ms % 1000, 3)}`;
}

const signed = (value) => `${value >= 0 ? '+' : ''}${Number(value).toFixed(3)}s`;
// Trễ quá ngưỡng này thì coi như chèn lệch, tô màu cảnh báo
const DRIFT_WARN = 0.15;

function resetWords() {
  state.words = [];
  state.wordsStamp = -1;
  $('words-card').hidden = true;
  $('words-list').innerHTML = '';
  $('words-summary').textContent = '';
}

// "KÉO-DÃN-ĐÃ-NẮN" là ghi chú đã xử lý xong, không tính là dòng đáng ngờ
const INFO_FLAGS = new Set(['KÉO-DÃN-ĐÃ-NẮN', 'ĐỨNG-LẺ-XA-CỤM']);

function isBadSegment(seg) {
  if ((seg.warnings || []).some((w) => !INFO_FLAGS.has(w))) return true;
  const placed = seg.placed;
  if (placed && placed.silent) return true;
  return !!(placed && typeof placed.drift === 'number' && placed.drift > DRIFT_WARN);
}

async function pollWords(force = false) {
  if (!state.jobId) return;
  let data;
  try { data = await api(`/api/jobs/${state.jobId}/words`); } catch { return; }
  const segments = data.segments || [];
  if (!segments.length) return;

  // Mốc chèn được gắn ở bước căn chỉnh, sau khi số dòng đã đứng yên - đếm cả hai
  const placed = segments.filter((seg) => seg.placed).length;
  const stamp = segments.length * 100000 + placed;
  state.words = segments;
  $('words-card').hidden = false;

  const words = segments.reduce((total, seg) => total + (seg.words || []).length, 0);
  const bad = segments.filter(isBadSegment).length;
  $('words-summary').textContent = [
    data.model || 'forced aligner',
    `${segments.length} dòng`,
    `${words} từ`,
    bad ? `${bad} dòng đáng ngờ` : 'không có dòng đáng ngờ',
  ].join(' · ');

  if (force || stamp !== state.wordsStamp) {
    state.wordsStamp = stamp;
    renderWords();
  }
}

function segmentHtml(seg) {
  const vad = seg.vad || {};
  const head = [`<span class="wseg-line">dòng ${seg.line}</span>`];
  if (seg.split_of) head.push(`<span class="k">tách từ khối ${seg.split_of}</span>`);
  head.push(`<span><span class="k">VAD</span> ${clockOf(vad.start)} → ${clockOf(vad.end)}</span>`);
  if (seg.aligned) {
    head.push(`<span><span class="k">ALIGN</span> ${clockOf(seg.aligned.start)} → ${clockOf(seg.aligned.end)}`
      + ` <span class="k">Δ</span> ${signed(seg.shift.start)} / ${signed(seg.shift.end)}</span>`);
  } else {
    head.push('<span class="k">giữ mốc VAD</span>');
  }
  const placed = seg.placed;
  if (placed && placed.silent) {
    head.push('<span class="delta late"><span class="k">CHÈN</span> không có audio</span>');
  } else if (placed) {
    const cls = placed.drift > DRIFT_WARN ? 'late' : 'ok';
    head.push(`<span><span class="k">CHÈN</span> ${clockOf(placed.start)} → ${clockOf(placed.end)}`
      + ` <span class="delta ${cls}">lệch ${signed(placed.drift)}</span></span>`);
  }

  const warnings = (seg.warnings || []).length
    ? `<p class="wseg-warn">!! ${escapeHtml(seg.warnings.join('; '))}</p>` : '';
  const chips = (seg.words || []).map((word) => {
    const flags = word.flags || [];
    const was = word.fixed_from;
    // Từ đã nắn hiện thêm mốc gốc mà aligner trả về, để đối chiếu
    const before = was
      ? `<span class="fixed">aligner: ${Number(was.rel_start).toFixed(2)}–${Number(was.rel_end).toFixed(2)}</span>`
      : '';
    const cls = was ? ' fixed' : (flags.length ? ' flag' : '');
    return `<span class="wchip${cls}" title="${escapeHtml(flags.join(', '))}">`
      + `<b>${escapeHtml(word.word)}</b>`
      + `<span>rel ${Number(word.rel_start).toFixed(2)}–${Number(word.rel_end).toFixed(2)}`
      + ` · ${clockOf(word.start)}</span>${before}</span>`;
  }).join('');

  return `<div class="wseg${isBadSegment(seg) ? ' bad' : ''}">`
    + `<div class="wseg-head">${head.join('')}</div>`
    + `<div class="wseg-text">${escapeHtml(seg.text || '')}</div>${warnings}`
    + `<div class="wchips">${chips || '<span class="k">(aligner không trả từ nào)</span>'}</div></div>`;
}

function renderWords() {
  const query = $('words-filter').value.trim().toLowerCase();
  const onlyBad = $('words-only-bad').checked;
  const list = $('words-list');
  const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 40;

  const html = state.words.filter((seg) => {
    if (onlyBad && !isBadSegment(seg)) return false;
    if (!query) return true;
    return String(seg.line) === query
      || (seg.text || '').toLowerCase().includes(query)
      || (seg.words || []).some((word) => (word.word || '').toLowerCase().includes(query));
  }).map(segmentHtml).join('');

  list.innerHTML = html || '<div class="words-empty">Không có dòng nào khớp bộ lọc.</div>';
  if ($('words-auto').checked && atBottom) list.scrollTop = list.scrollHeight;
}

async function cancelJob() {
  if (!state.jobId) return;
  $('cancel-btn').disabled = true;
  try { await api(`/api/jobs/${state.jobId}/cancel`, { method: 'POST' }); } catch { /* bỏ qua */ }
}

/* ------------------------------------------------------------------- init */
function init() {
  initDropzone();
  $('save-config').addEventListener('click', saveConfig);
  $('start-btn').addEventListener('click', startJob);
  $('cancel-btn').addEventListener('click', cancelJob);
  $('words-filter').addEventListener('input', renderWords);
  $('words-only-bad').addEventListener('change', renderWords);
  $('load-voices').addEventListener('click', loadVoices);
  for (const id of ['dit-steps', 'background-volume', 'voice-volume', 'dubbed-volume']) {
    $(id).addEventListener('input', syncSliders);
  }
  $('openai-instruction').addEventListener('input', syncInstruction);
  syncSliders();
  syncInstruction();
  document.querySelectorAll('[data-test]').forEach((button) =>
    button.addEventListener('click', () => testProvider(button.dataset.test)));
  document.querySelectorAll('[data-eye]').forEach((button) =>
    button.addEventListener('click', () => {
      const input = $(button.dataset.eye);
      const shown = input.type === 'text';
      input.type = shown ? 'password' : 'text';
      button.textContent = shown ? 'Hiện' : 'Ẩn';
    }));
  $('config-toggle').addEventListener('click', () => {
    const body = $('config-body');
    body.hidden = !body.hidden;
    $('config-toggle').textContent = body.hidden ? 'Mở rộng' : 'Thu gọn';
  });
  $('voice-select').addEventListener('change', () => {
    if ($('voice-select').value) $('clone-voice').checked = false;
  });

  loadConfig().catch((error) => { $('config-msg').textContent = error.message; });
  loadLanguages().catch(() => { /* giữ nguyên select rỗng */ });
}

document.addEventListener('DOMContentLoaded', init);
