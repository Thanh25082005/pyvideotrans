'use strict';

const $ = (id) => document.getElementById(id);
const state = { file: null, jobId: null, logIndex: 0, poller: null, ticker: null, baseElapsed: 0, baseAt: 0 };

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
  $('dit-steps').value = config.tts.dit_steps ?? 16;
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
  form.append('max_audio_speed', $('max-speed').value || '1.6');
  form.append('voice_autorate', $('voice-autorate').checked);
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
    if (snapshot.status === 'done') showResult(snapshot);
    else {
      $('start-msg').textContent = snapshot.error || 'Tiến trình đã dừng';
      $('start-msg').className = 'hint err';
    }
  }
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

  const links = [];
  if (result.video) links.push(`<a class="dl main" href="${base}/video?download=1">⬇ Tải video đã lồng tiếng</a>`);
  if (result.audio) links.push(`<a class="dl main" href="${base}/audio?download=1">⬇ Tải audio đã lồng tiếng</a>`);
  if (result.dubbed_audio) links.push(`<a class="dl" href="${base}/dubbed_audio?download=1">Track lồng tiếng (wav)</a>`);
  if (result.source_srt) links.push(`<a class="dl" href="${base}/source_srt?download=1">Phụ đề gốc (.srt)</a>`);
  if (result.target_srt) links.push(`<a class="dl" href="${base}/target_srt?download=1">Phụ đề dịch (.srt)</a>`);
  $('download-row').innerHTML = links.join('');

  for (const [kind, node] of [['source_srt', 'srt-source'], ['target_srt', 'srt-target']]) {
    try {
      const data = await api(`/api/jobs/${snapshot.id}/subtitles/${kind}`);
      $(node).textContent = data.text || '(trống)';
    } catch { $(node).textContent = ''; }
  }
  $('result-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
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
  $('load-voices').addEventListener('click', loadVoices);
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
