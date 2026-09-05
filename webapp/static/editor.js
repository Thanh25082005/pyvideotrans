'use strict';
/* Trình chỉnh sửa lồng tiếng.
 *
 * Timeline vẽ bằng canvas (đường bao sóng do server tính sẵn), còn mỗi câu là một
 * box DOM nên kéo/thả và bắt chuột đơn giản, chính xác.
 *
 * Phần nghe thử KHÔNG phát file dubbed.wav có sẵn: từng clip được lập lịch bằng
 * Web Audio tại đúng mốc của nó, nên vừa kéo xong là nghe được ngay bản mới mà
 * không phải render lại. Bản render cuối vẫn do ffmpeg dựng ở server.
 */

const $ = (id) => document.getElementById(id);

const state = {
  jobId: '',
  project: null,
  peaks: { original: null, clips: {} },
  view: { start: 0, pxPerMs: 0.02 },
  duration: 0,
  selected: null,
  playing: false,
  saveTimer: null,
  undo: [],
  drag: null,
  audio: { ctx: null, master: null, buffers: new Map(), sources: [], origin: 0, token: 0, volume: 1 },
  useSeparateAudio: false,
  renderPoll: null,
  // Box gen lại chèn trên dải audio gốc: [{id, start, end, status}]
  marks: [],
  markSeq: 0,
  activeMark: null,
  markDrag: null,
  laneDrag: null,
  pendingMarks: [],
  // 'pan' = kéo trượt timeline (mặc định) | 'box' = kéo để chèn box gen lại
  tool: 'pan',
  // Danh sách id câu đang hiện ở bảng sửa lời, để biết lúc nào phải dựng lại
  textKey: null,
  taskRunning: false,
  // Lần poll đầu tiên chỉ ghi nhận việc cũ, không báo toast/nạp lại
  firstPoll: true,
  lastTaskAt: 0,
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

const jsonPost = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});

let toastTimer = null;
function toast(message, kind = '') {
  const box = $('toast');
  box.textContent = message;
  box.className = `toast ${kind}`;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, kind === 'err' ? 6000 : 2600);
}

function fmt(ms) {
  ms = Math.max(0, Math.round(ms));
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
}

const esc = (text) => String(text ?? '').replace(/[&<>"]/g,
  (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const mediaUrl = (rel) => `/api/projects/${state.jobId}/media/${rel.split('/').map(encodeURIComponent).join('/')}`;
const segments = () => (state.project ? state.project.segments : []);
const effMs = (seg) => (seg.fit_ms > 0 ? seg.fit_ms : seg.clip_ms) || 0;
const boxMs = (seg) => effMs(seg) || Math.max(300, seg.orig_end_ms - seg.orig_start_ms);
const segById = (id) => segments().find((s) => s.id === id);

/* ------------------------------------------------------------------- nạp dữ liệu */
async function boot() {
  const params = new URLSearchParams(location.search);
  state.jobId = params.get('job') || '';
  if (!state.jobId) return showPicker();
  try {
    await loadProject();
  } catch (err) {
    toast(String(err.message || err), 'err');
    return showPicker();
  }
}

async function showPicker() {
  $('workspace').hidden = true;
  $('picker').hidden = false;
  const { projects } = await api('/api/projects');
  $('picker-list').innerHTML = projects.length
    ? projects.map((p) => `
        <a class="picker-item" href="/editor?job=${p.job_id}">
          <span class="name">${p.filename || p.job_id}</span>
          <span class="meta">${p.segments} câu · ${fmt(p.duration_ms)}
            ${p.rendered ? ' · đã ghép lại' : ''}<br />${new Date((p.created_at || 0) * 1000).toLocaleString()}</span>
        </a>`).join('')
    : '<p class="hint">Chưa có job nào chạy xong. Về <a href="/">trang chính</a> để tạo bản lồng tiếng trước.</p>';
}

async function loadProject() {
  const { project } = await api(`/api/projects/${state.jobId}`);
  state.project = project;
  state.duration = Math.max(project.duration_ms, ...segments().map((s) => s.start_ms + boxMs(s)), 1000);
  $('picker').hidden = true;
  $('workspace').hidden = false;
  $('project-title').textContent = project.filename || 'Chỉnh sửa lồng tiếng';
  $('project-sub').textContent =
    `${segments().length} câu · ${fmt(project.duration_ms)} · ${project.target_lang || ''}`;
  document.title = `Sửa: ${project.filename || state.jobId}`;

  const [original, clips] = await Promise.all([
    api(`/api/projects/${state.jobId}/peaks/original`).catch(() => null),
    api(`/api/projects/${state.jobId}/peaks/clips`).catch(() => ({})),
  ]);
  state.peaks.original = original;
  state.peaks.clips = clips || {};

  setupMedia();
  fillMixInputs();
  buildRegions();
  zoomFit();
  updateOutputLink();
  paintMarkPanel();
  fillRangeConfig();
  setTool('pan');
  pollRender();
}

function setupMedia() {
  const video = $('video');
  const media = state.project.media || {};
  const primary = media.source || media.novoice;
  if (!primary) return;
  video.src = mediaUrl(primary);
  video.addEventListener('error', () => {
    // Container gốc trình duyệt không đọc được (mkv, avi…) -> quay về bản mp4
    // không tiếng do pipeline tách, audio gốc phát bằng thẻ <audio> riêng.
    if (state.useSeparateAudio || !media.novoice) return;
    state.useSeparateAudio = true;
    video.src = mediaUrl(media.novoice);
    if (media.original_audio) {
      const audio = $('original-audio');
      audio.src = mediaUrl(media.original_audio);
      audio.volume = Number($('vol-original').value) / 100;
    }
    toast('Không phát được file gốc, đang dùng bản mp4 đã tách hình + audio gốc riêng');
  }, { once: false });
  video.volume = Number($('vol-original').value) / 100;
  video.addEventListener('play', onPlay);
  video.addEventListener('pause', onPause);
  video.addEventListener('seeked', onSeeked);
  video.addEventListener('loadedmetadata', () => {
    if (video.duration && Number.isFinite(video.duration)) {
      state.duration = Math.max(state.duration, video.duration * 1000);
      draw();
    }
  });
}

/* --------------------------------------------------------------------- toạ độ */
const lanesWidth = () => $('lanes').clientWidth || 1;
const msToX = (ms) => (ms - state.view.start) * state.view.pxPerMs;
const xToMs = (x) => state.view.start + x / state.view.pxPerMs;

function clampView() {
  const width = lanesWidth();
  const minPx = width / Math.max(state.duration, 1);
  state.view.pxPerMs = Math.min(Math.max(state.view.pxPerMs, minPx), 0.6);
  const span = width / state.view.pxPerMs;
  state.view.start = Math.min(Math.max(0, state.view.start), Math.max(0, state.duration - span));
}

function zoomFit() {
  state.view.start = 0;
  state.view.pxPerMs = lanesWidth() / Math.max(state.duration, 1);
  clampView();
  draw();
}

function zoomBy(factor, anchorX) {
  const anchorMs = xToMs(anchorX ?? lanesWidth() / 2);
  state.view.pxPerMs *= factor;
  clampView();
  state.view.start = anchorMs - (anchorX ?? lanesWidth() / 2) / state.view.pxPerMs;
  clampView();
  draw();
}

/* ---------------------------------------------------------------------- vẽ */
function sizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const width = lanesWidth();
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

/** Vẽ một mảng peaks trải đều trên khoảng [fromMs, toMs]. */
function paintPeaks(ctx, peaks, fromMs, toMs, width, height, color, alpha = 1) {
  if (!peaks || !peaks.length || toMs <= fromMs) return;
  const x0 = Math.max(0, Math.floor(msToX(fromMs)));
  const x1 = Math.min(width, Math.ceil(msToX(toMs)));
  if (x1 <= x0) return;
  const perMs = peaks.length / (toMs - fromMs);
  const mid = height / 2;
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.fillStyle = color;
  for (let x = x0; x < x1; x += 1) {
    const a = Math.max(0, Math.floor((xToMs(x) - fromMs) * perMs));
    const b = Math.min(peaks.length, Math.max(a + 1, Math.ceil((xToMs(x + 1) - fromMs) * perMs)));
    let peak = 0;
    for (let i = a; i < b; i += 1) if (peaks[i] > peak) peak = peaks[i];
    const h = Math.max(1, peak * (height - 10));
    ctx.fillRect(x, mid - h / 2, 1, h);
  }
  ctx.restore();
}

const RULER_STEPS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000,
  60000, 120000, 300000, 600000, 1800000];

function drawRuler() {
  const { ctx, width, height } = sizeCanvas($('ruler'));
  const step = RULER_STEPS.find((s) => s * state.view.pxPerMs >= 70) || RULER_STEPS.at(-1);
  ctx.fillStyle = '#171a23';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = '#2a2f3d';
  ctx.fillStyle = '#949bb0';
  ctx.font = '10px ui-monospace, Menlo, monospace';
  ctx.beginPath();
  const first = Math.floor(state.view.start / step) * step;
  for (let t = first; t <= xToMs(width); t += step) {
    const x = Math.round(msToX(t)) + 0.5;
    if (x < 0) continue;
    ctx.moveTo(x, height - 7);
    ctx.lineTo(x, height);
    ctx.fillText(fmt(t).replace(/\.000$/, ''), x + 3, height - 9);
  }
  ctx.stroke();
}

function drawOriginal() {
  const { ctx, width, height } = sizeCanvas($('wave-original'));
  // Khung giọng gốc của từng câu: mốc tham chiếu để biết câu lồng tiếng lệch đi đâu
  ctx.fillStyle = 'rgba(108,92,231,.14)';
  for (const seg of segments()) {
    const x = msToX(seg.orig_start_ms);
    const w = (seg.orig_end_ms - seg.orig_start_ms) * state.view.pxPerMs;
    if (x + w < 0 || x > width) continue;
    ctx.fillRect(x, 0, Math.max(1, w), height);
  }
  const data = state.peaks.original;
  if (data) paintPeaks(ctx, data.peaks, 0, data.duration_ms, width, height, '#7f8aa3');
}

function drawDub() {
  const { ctx, width, height } = sizeCanvas($('wave-dub'));
  for (const seg of segments()) {
    const peaks = state.peaks.clips[String(seg.id)];
    if (!peaks || !seg.clip) continue;
    const to = seg.start_ms + effMs(seg);
    if (msToX(to) < 0 || msToX(seg.start_ms) > width) continue;
    paintPeaks(ctx, peaks, seg.start_ms, to, width, height,
      seg.muted ? '#5b6172' : '#8b7cf0', seg.muted ? 0.5 : 1);
  }
}

function drawPlayhead() {
  const ms = currentMs();
  const x = msToX(ms);
  const head = $('playhead');
  head.style.display = x < 0 || x > lanesWidth() ? 'none' : 'block';
  head.style.left = `${x}px`;
  $('time-label').textContent = `${fmt(ms)} / ${fmt(state.duration)}`;
}

function recomputeDuration() {
  const video = $('video');
  let end = state.project ? state.project.duration_ms : 0;
  if (video.duration && Number.isFinite(video.duration)) end = Math.max(end, video.duration * 1000);
  for (const seg of segments()) end = Math.max(end, seg.start_ms + boxMs(seg));
  state.duration = Math.max(1000, end);
}

function draw() {
  recomputeDuration();
  clampView();
  drawRuler();
  drawOriginal();
  drawDub();
  layoutRegions();
  layoutMarks();
  drawPlayhead();
}

/* ------------------------------------------------------------------ box câu */
function buildRegions() {
  const host = $('regions');
  host.innerHTML = '';
  for (const seg of segments()) {
    const box = document.createElement('div');
    box.className = 'region';
    box.dataset.id = String(seg.id);
    box.innerHTML = '<div class="handle l"></div><div class="label"></div><div class="handle r"></div>';
    host.appendChild(box);
    seg._el = box;
  }
  layoutRegions();
}

function layoutRegions() {
  const width = lanesWidth();
  const list = segments();
  for (let i = 0; i < list.length; i += 1) {
    const seg = list[i];
    const box = seg._el;
    if (!box) continue;
    const x = msToX(seg.start_ms);
    const w = boxMs(seg) * state.view.pxPerMs;
    if (x + w < -50 || x > width + 50) { box.style.display = 'none'; continue; }
    box.style.display = 'block';
    box.style.left = `${x}px`;
    box.style.width = `${Math.max(4, w)}px`;
    const next = list[i + 1];
    const overlap = next && seg.start_ms + boxMs(seg) > next.start_ms + 1;
    box.className = 'region'
      + (state.selected === seg.id ? ' selected' : '')
      + (seg.muted ? ' muted' : '')
      + (seg.clip ? '' : ' missing')
      + (seg.fit_ms > 0 ? ' fitted' : '')
      + (overlap ? ' overlap' : '')
      + (w < 46 ? ' tiny' : '');
    box.querySelector('.label').textContent = `${seg.line}· ${seg.target_text || '(chưa có lời)'}`;
    box.title = `Câu ${seg.line} — ${fmt(seg.start_ms)} → ${fmt(seg.start_ms + boxMs(seg))}\n`
      + `Khung gốc: ${fmt(seg.orig_start_ms)} → ${fmt(seg.orig_end_ms)}\n${seg.target_text || ''}`;
  }
}

/* --------------------------------------------------------------- kéo & thả */
const SNAP_PX = 7;

function snapTargets(seg) {
  const points = [seg.orig_start_ms];
  for (const other of segments()) {
    if (other.id === seg.id) continue;
    points.push(other.start_ms, other.start_ms + boxMs(other), other.orig_start_ms, other.orig_end_ms);
  }
  return points;
}

function snap(value, targets, enabled) {
  if (!enabled) return value;
  const tol = SNAP_PX / state.view.pxPerMs;
  let best = value;
  let bestDelta = tol;
  for (const t of targets) {
    const delta = Math.abs(t - value);
    if (delta < bestDelta) { best = t; bestDelta = delta; }
  }
  return best;
}

$('regions').addEventListener('pointerdown', (event) => {
  const box = event.target.closest('.region');
  if (!box) return;
  const seg = segById(Number(box.dataset.id));
  if (!seg) return;
  event.preventDefault();
  select(seg.id);
  pushUndo();
  const handle = event.target.classList.contains('handle')
    ? (event.target.classList.contains('l') ? 'l' : 'r') : 'body';
  state.drag = {
    seg, handle, moved: false,
    grabMs: xToMs(event.clientX - $('lanes').getBoundingClientRect().left),
    startMs: seg.start_ms,
    lengthMs: boxMs(seg),
    targets: snapTargets(seg),
  };
  box.setPointerCapture(event.pointerId);
  box.classList.add('dragging');
});

$('regions').addEventListener('pointermove', (event) => {
  const drag = state.drag;
  if (!drag) return;
  const host = $('lanes').getBoundingClientRect();
  const ms = xToMs(event.clientX - host.left);
  const free = event.shiftKey;
  const seg = drag.seg;
  if (drag.handle === 'body') {
    const next = snap(Math.max(0, drag.startMs + (ms - drag.grabMs)), drag.targets, !free);
    if (next !== seg.start_ms) { seg.start_ms = Math.round(next); drag.moved = true; }
  } else if (drag.handle === 'r') {
    const end = snap(ms, drag.targets, !free);
    seg.fit_ms = Math.round(Math.max(200, end - seg.start_ms));
    drag.moved = true;
  } else {
    const start = snap(ms, drag.targets, !free);
    const end = drag.startMs + drag.lengthMs;
    seg.start_ms = Math.round(Math.max(0, Math.min(start, end - 200)));
    seg.fit_ms = Math.round(end - seg.start_ms);
    drag.moved = true;
  }
  draw();
  if (state.selected === seg.id) fillInspector(seg);
});

function endDrag(event) {
  const drag = state.drag;
  if (!drag) return;
  state.drag = null;
  drag.seg._el?.classList.remove('dragging');
  if (event && event.pointerId !== undefined) {
    try { drag.seg._el?.releasePointerCapture(event.pointerId); } catch { /* đã nhả */ }
  }
  if (!drag.moved) { state.undo.pop(); return; }
  queueSave();
  if (state.playing) rescheduleAudio();
}

$('regions').addEventListener('pointerup', endDrag);
$('regions').addEventListener('pointercancel', endDrag);

/* ------------------------------------- box gen lại trên dải audio gốc */
/* Box chỉ sống trong phiên: nó đánh dấu "khúc này hỏng, làm lại", không phải dữ
 * liệu của bản lồng tiếng, nên không ghi vào project.json.
 * Bấm một cái trên nền timeline vẫn là tua; chỉ KÉO trên dải Audio gốc mới đẻ box. */
const MARK_MIN_MS = 300;
const MARK_STATUS = { idle: '', running: ' · đang gen…', done: ' · đã gen', error: ' · lỗi' };
const laneX = (event) => event.clientX - $('lanes').getBoundingClientRect().left;
const markById = (id) => state.marks.find((mark) => mark.id === id);

/** Các câu chạm vào box - cùng luật với editor.segments_in_range ở server. */
function segsInMark(mark) {
  if (!mark) return [];
  return segments().filter((seg) => {
    let begin = seg.start_ms;
    let finish = begin + Math.max(effMs(seg), 1);
    if (!seg.clip) {
      begin = Math.min(begin, seg.orig_start_ms);
      finish = Math.max(finish, seg.orig_end_ms);
    }
    return finish > mark.start && begin < mark.end;
  });
}

function addMark(from, to) {
  const mark = {
    id: (state.markSeq += 1),
    start: Math.round(Math.max(0, Math.min(from, to))),
    end: Math.round(Math.min(state.duration, Math.max(from, to))),
    status: 'idle',
  };
  state.marks.push(mark);
  state.activeMark = mark.id;
  buildMarks();
  paintMarkPanel();
  return mark;
}

function removeMark(id) {
  state.marks = state.marks.filter((mark) => mark.id !== id);
  if (state.activeMark === id) state.activeMark = state.marks.length ? state.marks[0].id : null;
  buildMarks();
  paintMarkPanel();
}

function buildMarks() {
  const host = $('marks');
  host.innerHTML = '';
  for (const mark of state.marks) {
    const box = document.createElement('div');
    box.className = 'mark';
    box.dataset.id = String(mark.id);
    box.innerHTML = '<div class="m-handle l"></div><div class="m-label"></div>'
      + '<button class="m-del" type="button" title="Xoá box">×</button><div class="m-handle r"></div>';
    host.appendChild(box);
    mark._el = box;
  }
  layoutMarks();
}

function layoutMarks() {
  const width = lanesWidth();
  for (const mark of state.marks) {
    const box = mark._el;
    if (!box) continue;
    const x = msToX(mark.start);
    const w = (mark.end - mark.start) * state.view.pxPerMs;
    if (x + w < -60 || x > width + 60) { box.style.display = 'none'; continue; }
    box.style.display = 'block';
    box.style.left = `${x}px`;
    box.style.width = `${Math.max(6, w)}px`;
    const picked = segsInMark(mark);
    box.className = `mark ${mark.status}`
      + (picked.length || $('range-asr').checked ? '' : ' empty')
      + (state.activeMark === mark.id ? ' active' : '')
      + (w < 96 ? ' tiny' : '') + (w < 30 ? ' mini' : '');
    box.querySelector('.m-label').textContent =
      `${picked.length} câu · ${((mark.end - mark.start) / 1000).toFixed(1)}s`;
    box.title = `Gen lại ${fmt(mark.start)} → ${fmt(mark.end)} · ${picked.length} câu`
      + (picked.length ? `\n${picked.map((seg) => `${seg.line}· ${seg.target_text || ''}`).join('\n')}` : '');
  }
}

function paintMarkPanel() {
  const ordered = state.marks.slice().sort((a, b) => a.start - b.start);
  $('mark-list').innerHTML = ordered.length
    ? ordered.map((mark) => `<div class="mark-item ${mark.status}`
      + `${state.activeMark === mark.id ? ' active' : ''}" data-id="${mark.id}">`
      + `<button class="mark-pick" type="button"><span>${fmt(mark.start)} → ${fmt(mark.end)}</span>`
      + `<span class="meta">${segsInMark(mark).length} câu${MARK_STATUS[mark.status] || ''}</span></button>`
      + '<button class="mark-del" type="button" title="Xoá box">×</button></div>').join('')
    : '<p class="hint">Chưa có box nào.</p>';
  const active = markById(state.activeMark);
  // Bật ASR thì box rỗng lại chính là mục tiêu: nó đi dò thoại bị bỏ sót trong đó
  const asr = $('range-asr').checked;
  const ok = (mark) => !!mark && (asr || segsInMark(mark).length > 0);
  const usable = state.marks.filter(ok);
  $('btn-range-run').disabled = state.taskRunning || !ok(active);
  $('btn-range-all').disabled = state.taskRunning || !usable.length;
  $('btn-range-all').textContent = `Gen tất cả (${state.marks.length} box)`;

  // Nút bị khoá thì phải nói rõ vì sao, không để người dùng bấm vào chỗ chết
  const note = $('mark-note');
  const lines = new Set();
  usable.forEach((mark) => segsInMark(mark).forEach((seg) => lines.add(seg.id)));
  const empty = state.marks.length - state.marks.filter((mark) => segsInMark(mark).length).length;
  if (!state.marks.length) {
    note.textContent = '';
    note.className = 'hint';
  } else if (!asr && active && !segsInMark(active).length) {
    note.textContent = 'Box đang chọn rơi vào khoảng lặng, không trùm lên câu nào nên không gen được'
      + ' — kéo mép cho rộng ra, dời tới chỗ có box câu ở dải «Lồng tiếng»,'
      + ' hoặc bật «Nhận dạng lại (ASR)» để dò thoại bị bỏ sót trong đó.'
      + (usable.length ? ' Các box khác vẫn gen được bằng «Gen tất cả».' : '');
    note.className = 'hint err';
  } else if (!usable.length) {
    note.textContent = 'Chưa có box nào trùm lên câu nào để gen.';
    note.className = 'hint err';
  } else {
    note.textContent = `Sẽ gen lại ${lines.size} câu trong ${usable.length} box`
      + (asr && empty ? `, và dò lại thoại trong ${empty} box đang trống.`
        : asr ? ', kèm dò lại thoại ở những chỗ trống bên trong.' : '.');
    note.className = 'hint ok';
  }
  paintTextList();
  paintInsert();
}

/* ------------------------------------------------------- kéo & thả box */
$('marks').addEventListener('pointerdown', (event) => {
  const box = event.target.closest('.mark');
  if (!box) return;
  const mark = markById(Number(box.dataset.id));
  if (!mark) return;
  event.stopPropagation();  // đừng để nền lane tưởng là đang vẽ box mới
  // Nút xoá là nút: thoát TRƯỚC preventDefault, vì preventDefault trên pointerdown
  // chặn luôn sự kiện click sinh ra sau đó.
  if (event.target.classList.contains('m-del')) return;
  event.preventDefault();
  state.activeMark = mark.id;
  const handle = event.target.classList.contains('m-handle')
    ? (event.target.classList.contains('l') ? 'l' : 'r') : 'body';
  state.markDrag = { mark, handle, grabMs: xToMs(laneX(event)), start: mark.start, end: mark.end };
  box.setPointerCapture(event.pointerId);
  box.classList.add('dragging');
  layoutMarks();
  paintMarkPanel();
});

$('marks').addEventListener('pointermove', (event) => {
  const drag = state.markDrag;
  if (!drag) return;
  const ms = xToMs(laneX(event));
  const mark = drag.mark;
  if (drag.handle === 'body') {
    const width = drag.end - drag.start;
    mark.start = Math.round(Math.max(0, Math.min(drag.start + (ms - drag.grabMs), state.duration - width)));
    mark.end = mark.start + width;
  } else if (drag.handle === 'r') {
    mark.end = Math.round(Math.max(mark.start + MARK_MIN_MS, Math.min(ms, state.duration)));
  } else {
    mark.start = Math.round(Math.max(0, Math.min(ms, mark.end - MARK_MIN_MS)));
  }
  layoutMarks();
  paintMarkPanel();
});

function endMarkDrag(event) {
  const drag = state.markDrag;
  if (!drag) return;
  state.markDrag = null;
  drag.mark._el?.classList.remove('dragging');
  try { drag.mark._el?.releasePointerCapture(event.pointerId); } catch { /* đã nhả */ }
  // Đổi mốc rồi thì cái dấu "đã gen" không còn đúng nữa
  if (drag.mark.status === 'done' && (drag.mark.start !== drag.start || drag.mark.end !== drag.end)) {
    drag.mark.status = 'idle';
  }
  layoutMarks();
  paintMarkPanel();
}

$('marks').addEventListener('pointerup', endMarkDrag);
$('marks').addEventListener('pointercancel', endMarkDrag);

$('marks').addEventListener('click', (event) => {
  const box = event.target.closest('.mark');
  if (!box || !event.target.classList.contains('m-del')) return;
  event.stopPropagation();
  removeMark(Number(box.dataset.id));
});

$('mark-list').addEventListener('click', (event) => {
  const item = event.target.closest('.mark-item');
  const mark = item && markById(Number(item.dataset.id));
  if (!mark) return;
  if (event.target.closest('.mark-del')) { removeMark(mark.id); return; }
  state.activeMark = mark.id;
  seek(mark.start);
  layoutMarks();
  paintMarkPanel();
});

/* ------------------------------------------------- chế độ của con chuột
 * Mặc định kéo = trượt timeline. Chỉ khi mục «Gen lại cả một đoạn» đang mở thì
 * kéo trên dải Audio gốc mới là chèn box - mở mục khác là tự trả về kéo trượt.
 * Bấm một cái không kéo thì lúc nào cũng là tua, ở cả hai chế độ. */
function setTool(tool) {
  state.tool = tool;
  $('lanes').classList.toggle('tool-box', tool === 'box');
  const pill = $('tool-pill');
  pill.textContent = tool === 'box' ? 'chuột: chèn box' : 'chuột: kéo trượt';
  pill.className = tool === 'box' ? 'pill box' : 'pill';
}

$('lanes').addEventListener('pointerdown', (event) => {
  if (event.target.closest('.region') || event.target.closest('.mark')) return;
  const x = laneX(event);
  state.laneDrag = {
    anchor: xToMs(x), downX: x, moved: false, mark: null,
    viewStart: state.view.start,
    creating: state.tool === 'box' && !!event.target.closest('.orig-lane'),
  };
  $('lanes').setPointerCapture(event.pointerId);
});

$('lanes').addEventListener('pointermove', (event) => {
  const drag = state.laneDrag;
  if (!drag) return;
  const x = laneX(event);
  if (!drag.moved) {
    if (Math.abs(x - drag.downX) < 4) return;
    drag.moved = true;
    if (!drag.creating) $('lanes').classList.add('panning');
  }
  if (!drag.creating) {
    // Nội dung đi theo tay nên mốc đầu khung nhìn phải chạy ngược lại
    state.view.start = drag.viewStart - (x - drag.downX) / state.view.pxPerMs;
    clampView();
    draw();
    return;
  }
  const ms = xToMs(x);
  if (!drag.mark) {
    drag.mark = addMark(drag.anchor, ms);
    return;
  }
  drag.mark.start = Math.round(Math.max(0, Math.min(drag.anchor, ms)));
  drag.mark.end = Math.round(Math.min(state.duration, Math.max(drag.anchor, ms)));
  layoutMarks();
  paintMarkPanel();
});

function endLaneDrag(event) {
  const drag = state.laneDrag;
  if (!drag) return;
  state.laneDrag = null;
  $('lanes').classList.remove('panning');
  try { $('lanes').releasePointerCapture(event.pointerId); } catch { /* đã nhả */ }
  if (!drag.moved) { seek(drag.anchor); return; }
  // Tay run kéo được vài pixel thì bỏ, đừng để lại box tí hon
  if (drag.mark && drag.mark.end - drag.mark.start < MARK_MIN_MS) removeMark(drag.mark.id);
}

$('lanes').addEventListener('pointerup', endLaneDrag);
$('lanes').addEventListener('pointercancel', endLaneDrag);

/* Con lăn = phóng to/thu nhỏ quanh con trỏ; kéo chuột đã lo việc trượt rồi.
 * Shift+lăn (và lăn ngang của bàn di) vẫn trượt, cho ai quen tay.
 *
 * Hệ số tính theo độ lớn của delta chứ không cố định mỗi nấc một mức: bàn di cảm
 * ứng bắn ra rất nhiều delta nhỏ, dùng hệ số cố định thì zoom nhảy vọt. Hàm mũ
 * cho cảm giác đều nhau ở mọi mức phóng, và lăn lên rồi lăn xuống đúng bằng đó
 * thì về lại chỗ cũ. */
const ZOOM_PER_PX = 0.004;
const ZOOM_MAX_STEP = 120;

$('lanes').addEventListener('wheel', (event) => {
  event.preventDefault();
  const sideways = Math.abs(event.deltaX) > Math.abs(event.deltaY);
  if (event.shiftKey || sideways) {
    state.view.start += (sideways ? event.deltaX : event.deltaY) / state.view.pxPerMs;
    clampView();
    draw();
    return;
  }
  // deltaMode 1 = đếm theo dòng chứ không theo pixel, quy về cùng thang
  const delta = event.deltaY * (event.deltaMode === 1 ? 16 : 1);
  const step = Math.max(-ZOOM_MAX_STEP, Math.min(delta, ZOOM_MAX_STEP));
  zoomBy(Math.exp(-step * ZOOM_PER_PX), laneX(event));
}, { passive: false });

/* ------------------------------------------------------------------ phát thử */
function ensureAudio() {
  if (!state.audio.ctx) {
    state.audio.ctx = new (window.AudioContext || window.webkitAudioContext)();
    state.audio.master = state.audio.ctx.createGain();
    state.audio.master.gain.value = state.audio.volume;
    state.audio.master.connect(state.audio.ctx.destination);
  }
  if (state.audio.ctx.state === 'suspended') state.audio.ctx.resume();
  return state.audio.ctx;
}

function bufferFor(seg) {
  const url = mediaUrl(seg.clip);
  if (!state.audio.buffers.has(url)) {
    state.audio.buffers.set(url, fetch(url)
      .then((r) => r.arrayBuffer())
      .then((raw) => ensureAudio().decodeAudioData(raw)));
  }
  return state.audio.buffers.get(url);
}

function stopSources() {
  for (const src of state.audio.sources) {
    try { src.stop(); } catch { /* đã dừng */ }
  }
  state.audio.sources = [];
}

async function scheduleSegment(seg, token) {
  let buffer;
  try { buffer = await bufferFor(seg); } catch { return; }
  if (token !== state.audio.token || !state.playing) return;
  const ctx = state.audio.ctx;
  const when = state.audio.origin + seg.start_ms / 1000;
  // Câu đang phát dở thì vào giữa clip, không phải bỏ qua
  const offset = Math.max(0, ctx.currentTime - when);
  const rate = seg.fit_ms > 0 && seg.clip_ms > 0 && Math.abs(seg.fit_ms - seg.clip_ms) > 60
    ? seg.clip_ms / seg.fit_ms : 1;
  if (offset >= buffer.duration / rate) return;
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.playbackRate.value = rate;
  const gain = ctx.createGain();
  gain.gain.value = Math.max(0, seg.gain ?? 1);
  source.connect(gain).connect(state.audio.master);
  source.start(Math.max(ctx.currentTime, when), offset * rate);
  state.audio.sources.push(source);
}

function rescheduleAudio() {
  const ctx = ensureAudio();
  stopSources();
  state.audio.token += 1;
  const token = state.audio.token;
  const nowMs = currentMs();
  // Đệm một nhịp nhỏ để clip kịp decode trước khi tới lượt
  state.audio.origin = ctx.currentTime + 0.06 - nowMs / 1000;
  for (const seg of segments()) {
    if (seg.muted || !seg.clip) continue;
    if (seg.start_ms + effMs(seg) <= nowMs) continue;
    scheduleSegment(seg, token);
  }
}

const currentMs = () => ($('video').currentTime || 0) * 1000;

function onPlay() {
  state.playing = true;
  $('btn-play').textContent = '❚❚';
  if (state.useSeparateAudio) {
    const audio = $('original-audio');
    audio.currentTime = $('video').currentTime;
    audio.play().catch(() => {});
  }
  rescheduleAudio();
  requestAnimationFrame(tick);
}

function onPause() {
  state.playing = false;
  $('btn-play').textContent = '▶';
  stopSources();
  if (state.useSeparateAudio) $('original-audio').pause();
}

function onSeeked() {
  if (state.useSeparateAudio) $('original-audio').currentTime = $('video').currentTime;
  if (state.playing) rescheduleAudio();
  drawPlayhead();
}

function seek(ms) {
  const video = $('video');
  video.currentTime = Math.max(0, ms) / 1000;
  drawPlayhead();
}

function tick() {
  if (!state.playing) return;
  if (state.useSeparateAudio) {
    const audio = $('original-audio');
    const drift = audio.currentTime - $('video').currentTime;
    if (Math.abs(drift) > 0.15) audio.currentTime = $('video').currentTime;
  }
  if ($('follow').checked) {
    const x = msToX(currentMs());
    const width = lanesWidth();
    if (x < width * 0.1 || x > width * 0.9) {
      state.view.start = currentMs() - (width * 0.3) / state.view.pxPerMs;
      draw();
    }
  }
  drawPlayhead();
  requestAnimationFrame(tick);
}

function togglePlay() {
  const video = $('video');
  if (video.paused) { ensureAudio(); video.play().catch((err) => toast(String(err), 'err')); }
  else video.pause();
}

function playSegment(seg) {
  seek(Math.max(0, seg.start_ms - 150));
  if ($('video').paused) togglePlay();
}

/* -------------------------------------------------------------- inspector */
function select(id) {
  state.selected = id;
  const seg = segById(id);
  $('inspector-empty').hidden = !!seg;
  $('inspector-body').hidden = !seg;
  if (seg) fillInspector(seg);
  layoutRegions();
}

function fillInspector(seg) {
  $('seg-title').textContent = `Câu ${seg.line}`;
  $('seg-time').textContent = `${fmt(seg.start_ms)} → ${fmt(seg.start_ms + boxMs(seg))}`;
  $('seg-source').textContent = seg.source_text || '(không có transcript gốc)';
  if (document.activeElement !== $('seg-text')) $('seg-text').value = seg.target_text || '';
  $('seg-speed').value = seg.speed || 1;
  $('seg-speed-val').textContent = Number(seg.speed || 1).toFixed(2);
  $('seg-gain').value = Math.round((seg.gain ?? 1) * 100);
  $('seg-gain-val').textContent = `${Math.round((seg.gain ?? 1) * 100)}%`;
  $('seg-muted').checked = !!seg.muted;

  const drift = seg.start_ms - seg.orig_start_ms;
  const window_ms = seg.orig_end_ms - seg.orig_start_ms;
  const overrun = seg.start_ms + effMs(seg) - seg.orig_end_ms;
  $('seg-stats').innerHTML = [
    `Khung gốc <b>${fmt(seg.orig_start_ms)} → ${fmt(seg.orig_end_ms)}</b> (${(window_ms / 1000).toFixed(2)}s)`,
    `Lệch mốc gốc <b class="${Math.abs(drift) > 300 ? 'bad' : ''}">${(drift / 1000).toFixed(2)}s</b>`,
    `Clip dài <b>${(seg.clip_ms / 1000).toFixed(2)}s</b>`
      + (seg.fit_ms > 0 ? ` → ép về <b>${(seg.fit_ms / 1000).toFixed(2)}s</b> bằng atempo` : ''),
    `Đọc quá mốc gốc <b class="${overrun > 500 ? 'bad' : ''}">${(overrun / 1000).toFixed(2)}s</b>`,
    seg.clip ? '' : '<b class="bad">Câu này chưa có audio — bấm «Đọc lại câu này»</b>',
  ].filter(Boolean).map((line) => `<div>${line}</div>`).join('');
}

function pushUndo() {
  state.undo.push(JSON.stringify(segments().map(
    ({ id, start_ms, fit_ms, gain, muted, target_text }) => ({ id, start_ms, fit_ms, gain, muted, target_text }))));
  if (state.undo.length > 60) state.undo.shift();
}

function undo() {
  const snapshot = state.undo.pop();
  if (!snapshot) return toast('Không còn gì để hoàn tác');
  for (const patch of JSON.parse(snapshot)) Object.assign(segById(patch.id) || {}, patch);
  draw();
  if (state.selected) fillInspector(segById(state.selected));
  queueSave();
  if (state.playing) rescheduleAudio();
}

/* -------------------------------------------------------------------- lưu */
function queueSave() {
  $('save-state').textContent = 'chưa lưu';
  $('save-state').className = 'pill running';
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveNow, 700);
}

async function saveNow() {
  if (!state.project) return;
  clearTimeout(state.saveTimer);
  state.saveTimer = null;
  const payload = {
    mix: state.project.mix,
    tts: state.project.tts,
    translate: state.project.translate || {},
    segments: segments().map(({ id, start_ms, fit_ms, gain, muted, target_text }) =>
      ({ id, start_ms, fit_ms, gain, muted, target_text })),
  };
  try {
    await api(`/api/projects/${state.jobId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    $('save-state').textContent = 'đã lưu';
    $('save-state').className = 'pill done';
  } catch (err) {
    $('save-state').textContent = 'lưu lỗi';
    $('save-state').className = 'pill error';
    toast(`Không lưu được: ${err.message}`, 'err');
  }
}

/* ---------------------------------------------------------------- đọc lại */
async function regenerate(fit) {
  const seg = segById(state.selected);
  if (!seg) return;
  const buttons = [$('btn-regen'), $('btn-regen-fit')];
  buttons.forEach((b) => { b.disabled = true; });
  $('seg-msg').textContent = fit ? 'Đang đọc lại cho vừa khung…' : 'Đang đọc lại…';
  $('seg-msg').className = 'hint';
  try {
    await saveNow();
    const body = { text: $('seg-text').value, speed: Number($('seg-speed').value), fit };
    if (fit) body.fit_ms = seg.fit_ms > 0 ? seg.fit_ms : (seg.orig_end_ms - seg.orig_start_ms);
    const data = await jsonPost(`/api/projects/${state.jobId}/segments/${seg.id}/regen`, body);
    Object.assign(seg, data.segment);
    state.peaks.clips[String(seg.id)] = data.peaks;
    $('seg-msg').textContent = `Xong: clip mới dài ${(seg.clip_ms / 1000).toFixed(2)}s`
      + (data.requested_ms ? ` (đặt hàng ${(data.requested_ms / 1000).toFixed(2)}s)` : '');
    $('seg-msg').className = 'hint ok';
    draw();
    fillInspector(seg);
    if (state.playing) rescheduleAudio();
    playSegment(seg);
  } catch (err) {
    $('seg-msg').textContent = String(err.message || err);
    $('seg-msg').className = 'hint err';
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

/* ---------------------- sửa lời hàng loạt trong box + chèn câu thủ công */
/* Dựng lại danh sách chỉ khi tập câu đổi; còn lại chỉ đồng bộ giá trị, và không
 * bao giờ đụng vào ô đang gõ dở - nếu không con trỏ sẽ nhảy mỗi lần vẽ lại. */
function paintTextList() {
  const host = $('range-text-list');
  const picked = segsInMark(markById(state.activeMark));
  const key = picked.map((seg) => seg.id).join(',');
  if (key !== state.textKey) {
    state.textKey = key;
    host.innerHTML = picked.length
      ? picked.map((seg) => {
        const kind = seg.manual ? ' manual' : seg.discovered ? ' discovered' : '';
        return `<div class="text-row${kind}">`
          + `<div class="head"><b>Câu ${seg.line}</b><span class="mono">${fmt(seg.start_ms)}</span></div>`
          + `<div class="src">${esc(seg.source_text || '(không có transcript gốc)')}</div>`
          + `<textarea rows="2" data-id="${seg.id}"></textarea></div>`;
      }).join('')
      : '<p class="hint">Box đang chọn chưa có câu nào.</p>';
  }
  for (const area of host.querySelectorAll('textarea')) {
    if (area === document.activeElement) continue;
    const seg = segById(Number(area.dataset.id));
    if (seg && area.value !== (seg.target_text || '')) area.value = seg.target_text || '';
  }
}

$('range-text-list').addEventListener('input', (event) => {
  const area = event.target.closest('textarea');
  const seg = area && segById(Number(area.dataset.id));
  if (!seg) return;
  seg.target_text = area.value;
  layoutRegions();
  layoutMarks();
  if (state.selected === seg.id) $('seg-text').value = area.value;
  queueSave();
});

function paintInsert() {
  $('btn-insert').disabled = state.taskRunning || !markById(state.activeMark)
    || !$('insert-text').value.trim();
}

async function insertSegment() {
  const mark = markById(state.activeMark);
  const text = $('insert-text').value.trim();
  if (!mark || !text) return;
  $('btn-insert').disabled = true;
  $('insert-msg').textContent = 'Đang đọc câu mới…';
  $('insert-msg').className = 'hint';
  try {
    await saveNow();
    const data = await jsonPost(`/api/projects/${state.jobId}/segments`,
      { start_ms: mark.start, end_ms: mark.end, text });
    $('insert-text').value = '';
    await refreshSegments();
    select(data.segment.id);
    $('insert-msg').textContent = `Đã chèn câu ${data.segment.line}, clip dài `
      + `${(data.segment.clip_ms / 1000).toFixed(2)}s.`;
    $('insert-msg').className = 'hint ok';
    toast('Đã chèn câu mới', 'ok');
  } catch (err) {
    $('insert-msg').textContent = String(err.message || err);
    $('insert-msg').className = 'hint err';
    toast(String(err.message || err), 'err');
  } finally {
    // Câu mới làm đổi cả danh sách box lẫn bảng sửa lời, vẽ lại hết
    paintMarkPanel();
  }
}

/* ------------------------------ cấu hình giọng & dịch cho lần gen theo box */
/* Lưu thẳng vào project.json (mục tts + translate) nên mở lại phiên sau vẫn còn,
 * và bước gen ở server cứ đọc từ project ra dùng - không phải truyền kèm request. */
function ditLabel(steps) {
  const tag = steps <= 8 ? 'tiết kiệm' : steps <= 20 ? 'cân bằng'
    : steps <= 36 ? 'tốt' : 'cao nhất, chậm';
  return `${steps} — ${tag}, chi phí ×${(steps / 8).toFixed(2)}`;
}

function paintRangeConfig() {
  $('range-speed-val').textContent = Number($('range-speed').value).toFixed(2);
  $('range-dit-val').textContent = ditLabel(Number($('range-dit').value));
  $('range-instr-count').textContent = `${$('range-instruction').value.length} / 4000`;
}

function fillRangeConfig() {
  const tts = state.project.tts || {};
  const voice = tts.voice_id || '';
  const select = $('range-voice');
  // Voice đang dùng có thể không nằm trong danh sách chưa nạp: cứ thêm vào để không mất
  if (voice && !Array.from(select.options).some((option) => option.value === voice)) {
    select.insertAdjacentHTML('beforeend', `<option value="${voice}">${voice}</option>`);
  }
  select.value = voice;
  $('range-speed').value = tts.speed ?? 1;
  $('range-dit').value = tts.dit_steps ?? 16;
  $('range-instruction').value = (state.project.translate || {}).instruction || '';
  paintRangeConfig();
}

function readRangeConfig() {
  Object.assign(state.project.tts, {
    voice_id: $('range-voice').value,
    speed: Number($('range-speed').value),
    dit_steps: Number($('range-dit').value),
  });
  state.project.translate = {
    ...(state.project.translate || {}),
    instruction: $('range-instruction').value.slice(0, 4000),
  };
  paintRangeConfig();
  queueSave();
}

async function loadRangeVoices() {
  const button = $('range-load-voices');
  const select = $('range-voice');
  const keep = select.value;
  button.disabled = true;
  button.textContent = '…';
  try {
    const result = await api('/api/voices');
    select.innerHTML = ['<option value="">— Voice mặc định trong cấu hình —</option>']
      .concat((result.voices || []).map((v) => `<option value="${v.id}">${v.name}</option>`)).join('');
    if (keep && !Array.from(select.options).some((option) => option.value === keep)) {
      select.insertAdjacentHTML('beforeend', `<option value="${keep}">${keep}</option>`);
    }
    select.value = keep;
    $('range-cfg-msg').textContent = result.message || '';
    $('range-cfg-msg').className = result.ok ? 'hint ok' : 'hint err';
  } catch (err) {
    $('range-cfg-msg').textContent = String(err.message || err);
    $('range-cfg-msg').className = 'hint err';
  } finally {
    button.disabled = false;
    button.textContent = 'Nạp danh sách voice';
  }
}

/* --------------------------------------------------------- gen lại theo box */
async function runMarks(marks) {
  const asr = $('range-asr').checked;
  const usable = marks.filter((mark) => mark && (asr || segsInMark(mark).length));
  if (!usable.length) return toast('Box đang chọn không trùm lên câu nào', 'err');
  const lines = new Set();
  usable.forEach((mark) => segsInMark(mark).forEach((seg) => lines.add(seg.id)));
  const translate = $('range-translate').checked;
  const renderAfter = $('range-render').checked;
  const ok = confirm(
    `Gen lại ${lines.size} câu trong ${usable.length} box?\n`
    + (asr ? '· Nhận dạng lại (ASR) những chỗ trống trong box để tìm thoại bị sót\n' : '')
    + (translate ? '· Dịch lại bằng OpenAI (tốn token)'
      + ($('range-instruction').value.trim() ? ', theo chỉ thị dịch riêng đã đặt' : '') + '\n' : '')
    + '· Đọc lại toàn bộ câu đó bằng TTS\n'
    + (renderAfter ? '· Ghép lại video ngay sau đó' : '· Không ghép lại video'));
  if (!ok) return;
  $('btn-range-run').disabled = true;
  $('btn-range-all').disabled = true;
  try {
    await saveNow();
    await jsonPost(`/api/projects/${state.jobId}/range`, {
      ranges: usable.map((mark) => ({ start_ms: mark.start, end_ms: mark.end })),
      translate,
      asr,
      fit: $('range-fit').checked,
      render_after: renderAfter,
    });
    state.pendingMarks = usable.map((mark) => mark.id);
    usable.forEach((mark) => { mark.status = 'running'; });
    layoutMarks();
    $('range-msg').textContent = 'Đang gen lại…';
    $('range-msg').className = 'hint';
    toast(`Đang gen lại ${lines.size} câu trong ${usable.length} box…`);
    pollRender();
  } catch (err) {
    usable.forEach((mark) => { mark.status = 'idle'; });
    $('range-msg').textContent = String(err.message || err);
    $('range-msg').className = 'hint err';
    toast(String(err.message || err), 'err');
    layoutMarks();
    paintMarkPanel();
  }
}

/** Nạp lại câu + đường bao sóng sau khi server sinh clip mới. */
async function refreshSegments() {
  try {
    const [{ project }, clips] = await Promise.all([
      api(`/api/projects/${state.jobId}`),
      api(`/api/projects/${state.jobId}/peaks/clips`).catch(() => null),
    ]);
    if (clips) state.peaks.clips = clips;
    const byId = new Map(segments().map((seg) => [seg.id, seg]));
    // Giữ nguyên object câu đang có để không mất tham chiếu tới box DOM (_el);
    // câu mới (ASR dò ra hoặc tự chèn) thì lấy nguyên bản từ server.
    let added = false;
    const merged = project.segments.map((fresh) => {
      const seg = byId.get(fresh.id);
      if (!seg) { added = true; return fresh; }
      Object.assign(seg, fresh);
      return seg;
    });
    state.project.segments = merged;
    if (added || merged.length !== byId.size) buildRegions();
    state.textKey = null;
    state.project.output = project.output;
    updateOutputLink();
    draw();
    paintMarkPanel();
    const seg = segById(state.selected);
    if (seg) fillInspector(seg);
    if (state.playing) rescheduleAudio();
  } catch (err) {
    toast(`Không nạp lại được dữ liệu mới: ${err.message}`, 'err');
  }
}

/* ------------------------------------------------------------ trộn & render */
function fillMixInputs() {
  const mix = state.project.mix || {};
  $('mix-bg').value = Math.round((mix.background_volume ?? 0.9) * 100);
  $('mix-voice').value = Math.round((mix.original_voice_volume ?? 0) * 100);
  $('mix-dub').value = Math.round((mix.dubbed_volume ?? 1) * 100);
  $('mix-original').checked = mix.mix_original_audio !== false;
  $('mix-burn').checked = !!mix.burn_subtitle;
  $('mix-soft').checked = !!mix.soft_subtitle;
  paintMixLabels();
}

function paintMixLabels() {
  $('mix-bg-val').textContent = (Number($('mix-bg').value) / 100).toFixed(2);
  $('mix-voice-val').textContent = (Number($('mix-voice').value) / 100).toFixed(2);
  $('mix-dub-val').textContent = (Number($('mix-dub').value) / 100).toFixed(2);
}

function readMixInputs() {
  Object.assign(state.project.mix, {
    background_volume: Number($('mix-bg').value) / 100,
    original_voice_volume: Number($('mix-voice').value) / 100,
    dubbed_volume: Number($('mix-dub').value) / 100,
    mix_original_audio: $('mix-original').checked,
    burn_subtitle: $('mix-burn').checked,
    soft_subtitle: $('mix-soft').checked,
  });
  paintMixLabels();
  queueSave();
}

function updateOutputLink() {
  const output = (state.project && state.project.output) || {};
  const link = $('btn-download');
  link.hidden = !output.name;
  if (output.name) {
    link.href = `/api/projects/${state.jobId}/download`;
    link.textContent = `⬇ ${output.name}`;
  }
}

async function startRender() {
  $('btn-render').disabled = true;
  try {
    await saveNow();
    await jsonPost(`/api/projects/${state.jobId}/render`);
    toast('Đang ghép lại video…');
    pollRender();
  } catch (err) {
    toast(String(err.message || err), 'err');
    $('btn-render').disabled = false;
  }
}

/* Một job chỉ chạy một việc nặng tại một thời điểm nên chỉ cần một vòng poll:
 * status.kind cho biết đang ghép video hay đang gen lại một đoạn. */
async function pollRender() {
  clearTimeout(state.renderPoll);
  let status;
  try { status = await api(`/api/projects/${state.jobId}/render`); } catch { return; }
  const first = state.firstPoll;
  state.firstPoll = false;
  const isRange = status.kind === 'range';
  const box = isRange ? $('range-msg') : $('render-msg');
  state.taskRunning = !!status.running;
  $('render-bar').style.width = `${status.progress || 0}%`;
  $('btn-render').disabled = !!status.running;

  if (status.running) {
    box.textContent = `${status.stage || 'Đang chạy'}… ${status.progress || 0}%`;
    box.className = 'hint';
    if (isRange) $('range-panel').open = true;
    paintMarkPanel();
    state.renderPoll = setTimeout(pollRender, 1200);
    return;
  }

  // Việc vừa xong trong phiên này mới báo và nạp lại; việc của lần mở trước thì thôi
  const fresh = !first && !!status.started_at && status.started_at !== state.lastTaskAt;
  state.lastTaskAt = status.started_at || state.lastTaskAt;
  const what = isRange ? 'Gen lại đoạn' : 'Ghép lại';

  if (status.error) {
    box.textContent = status.error;
    box.className = 'hint err';
    if (fresh) toast(`${what} thất bại: ${status.error}`, 'err');
  } else if (status.output && status.output.name) {
    box.textContent = `Đã ghép xong: ${status.output.name}`;
    box.className = 'hint ok';
    state.project.output = status.output;
    updateOutputLink();
    if (fresh) toast('Đã ghép xong bản đã sửa', 'ok');
  } else if (isRange && status.output && status.output.range) {
    const info = status.output.range;
    box.textContent = `Đã gen lại ${info.redubbed}/${info.count} câu trong ${info.ranges} box`
      + (info.discovered ? `, thêm ${info.discovered} câu mới do ASR dò ra` : '')
      + '. Bấm «Ghép lại video» khi ưng.';
    box.className = 'hint ok';
    if (fresh) toast('Đã gen lại xong đoạn', 'ok');
  } else if (!isRange) {
    $('render-msg').textContent = 'Các số này chỉ áp dụng khi bấm «Ghép lại video».';
    $('render-msg').className = 'hint';
  }
  if (state.pendingMarks.length) {
    for (const id of state.pendingMarks) {
      const mark = markById(id);
      if (mark) mark.status = status.error ? 'error' : 'done';
    }
    state.pendingMarks = [];
  }
  if (fresh && isRange) await refreshSegments();
  layoutMarks();
  paintMarkPanel();
}

/* ------------------------------------------------------------------- sự kiện */
function wire() {
  $('btn-play').addEventListener('click', togglePlay);
  $('zoom-in').addEventListener('click', () => zoomBy(1.6));
  $('zoom-out').addEventListener('click', () => zoomBy(0.625));
  $('zoom-fit').addEventListener('click', zoomFit);
  $('vol-original').addEventListener('input', () => {
    const value = Number($('vol-original').value) / 100;
    $('video').volume = state.useSeparateAudio ? 0 : value;
    $('original-audio').volume = value;
  });
  $('vol-dubbed').addEventListener('input', () => {
    state.audio.volume = Number($('vol-dubbed').value) / 100;
    if (state.audio.master) state.audio.master.gain.value = state.audio.volume;
  });

  $('seg-text').addEventListener('input', () => {
    const seg = segById(state.selected);
    if (!seg) return;
    seg.target_text = $('seg-text').value;
    layoutRegions();
    layoutMarks();
    paintTextList();
    queueSave();
  });
  $('seg-speed').addEventListener('input', () => {
    $('seg-speed-val').textContent = Number($('seg-speed').value).toFixed(2);
  });
  $('seg-gain').addEventListener('input', () => {
    const seg = segById(state.selected);
    if (!seg) return;
    seg.gain = Number($('seg-gain').value) / 100;
    $('seg-gain-val').textContent = `${$('seg-gain').value}%`;
    queueSave();
    if (state.playing) rescheduleAudio();
  });
  $('seg-muted').addEventListener('change', () => {
    const seg = segById(state.selected);
    if (!seg) return;
    pushUndo();
    seg.muted = $('seg-muted').checked;
    draw();
    queueSave();
    if (state.playing) rescheduleAudio();
  });

  $('btn-regen').addEventListener('click', () => regenerate(false));
  $('btn-regen-fit').addEventListener('click', () => regenerate(true));
  $('btn-reset-pos').addEventListener('click', () => {
    const seg = segById(state.selected);
    if (!seg) return;
    pushUndo();
    seg.start_ms = seg.orig_start_ms;
    draw(); fillInspector(seg); queueSave();
    if (state.playing) rescheduleAudio();
  });
  $('btn-fit-atempo').addEventListener('click', () => {
    const seg = segById(state.selected);
    if (!seg) return;
    pushUndo();
    seg.fit_ms = Math.max(200, seg.orig_end_ms - seg.orig_start_ms);
    draw(); fillInspector(seg); queueSave();
    if (state.playing) rescheduleAudio();
  });
  $('btn-clear-fit').addEventListener('click', () => {
    const seg = segById(state.selected);
    if (!seg) return;
    pushUndo();
    seg.fit_ms = 0;
    draw(); fillInspector(seg); queueSave();
    if (state.playing) rescheduleAudio();
  });

  for (const id of ['mix-bg', 'mix-voice', 'mix-dub', 'mix-original', 'mix-burn', 'mix-soft']) {
    $(id).addEventListener('input', readMixInputs);
  }
  $('btn-range-run').addEventListener('click',
    () => runMarks([markById(state.activeMark)].filter(Boolean)));
  $('btn-range-all').addEventListener('click', () => runMarks(state.marks));
  // Bao cả khung giọng gốc lẫn box lồng tiếng, câu bị kéo lệch vẫn lọt vào box
  const markAround = (seg) => addMark(Math.min(seg.start_ms, seg.orig_start_ms),
    Math.max(seg.start_ms + boxMs(seg), seg.orig_end_ms));
  $('mark-add').addEventListener('click', () => {
    const at = currentMs();
    const seg = segments().find((s) => s.start_ms <= at && at < s.start_ms + boxMs(s));
    const mark = seg ? markAround(seg) : addMark(at, at + 8000);
    seek(mark.start);
    $('range-panel').open = true;
  });
  $('mark-from-seg').addEventListener('click', () => {
    const seg = segById(state.selected);
    if (!seg) return toast('Chưa chọn câu nào để lấy mốc');
    markAround(seg);
    $('range-panel').open = true;
  });
  $('range-asr').addEventListener('change', () => { layoutMarks(); paintMarkPanel(); });
  $('insert-text').addEventListener('input', paintInsert);
  $('btn-insert').addEventListener('click', insertSegment);
  for (const id of ['range-voice', 'range-speed', 'range-dit', 'range-instruction']) {
    $(id).addEventListener('input', readRangeConfig);
  }
  $('range-load-voices').addEventListener('click', loadRangeVoices);

  // Mở mục nào thì mục kia đóng lại, và chế độ chuột đi theo mục đang mở
  $('range-panel').addEventListener('toggle', () => {
    if ($('range-panel').open) $('mix-panel').open = false;
    setTool($('range-panel').open ? 'box' : 'pan');
  });
  $('mix-panel').addEventListener('toggle', () => {
    if ($('mix-panel').open) $('range-panel').open = false;
  });

  $('mark-clear').addEventListener('click', () => {
    if (!state.marks.length || !confirm('Xoá hết box đang có?')) return;
    state.marks = [];
    state.activeMark = null;
    buildMarks();
    paintMarkPanel();
  });

  $('btn-render').addEventListener('click', startRender);
  $('btn-delete').addEventListener('click', async () => {
    if (!confirm('Xoá toàn bộ dữ liệu chỉnh sửa của job này? Video đã tải về vẫn còn.')) return;
    await api(`/api/projects/${state.jobId}`, { method: 'DELETE' });
    location.href = '/editor';
  });

  $('regions').addEventListener('dblclick', (event) => {
    const box = event.target.closest('.region');
    if (box) playSegment(segById(Number(box.dataset.id)));
  });

  document.addEventListener('keydown', (event) => {
    const tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;
    if (event.key === ' ') { event.preventDefault(); togglePlay(); return; }
    if (event.key === 'Escape' && state.activeMark) {
      state.activeMark = null; layoutMarks(); paintMarkPanel(); return;
    }
    if (event.key === 'Delete' && state.activeMark) { removeMark(state.activeMark); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault(); undo(); return;
    }
    const seg = segById(state.selected);
    if (!seg) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      pushUndo();
      seg.start_ms = Math.max(0, seg.start_ms + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 100 : 10));
      draw(); fillInspector(seg); queueSave();
      if (state.playing) rescheduleAudio();
    }
  });

  window.addEventListener('beforeunload', (event) => {
    if (state.saveTimer) { saveNow(); event.preventDefault(); event.returnValue = ''; }
  });
  new ResizeObserver(() => { if (state.project) draw(); }).observe($('lanes'));
}

wire();
boot();
