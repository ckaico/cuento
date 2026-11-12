// Cuento editor. One page, no build step.
// State lives in the daemon's project file; this page renders it and sends
// edit operations back. Claude edits arrive over the event stream.
'use strict';

const FPS = 24000 / 1001;
const project = new URLSearchParams(location.search).get('project');
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

const S = {
  doc: null, rev: 0, items: [], total: 0, media: {}, renders: {},
  playhead: 0, rate: 0, sel: null, marks: { in: null, out: null },
  ppf: 1, fit: true, layerMode: 'rendered', curComment: null, curLayer: null, kHeld: false,
  diff: false, only: new Map(), chat: null,
  tool: 'select', multi: new Set(), snap: true,
};

const tc = (f) => { f = Math.max(0, Math.floor(f)); const s = Math.floor(f / 24); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}:${String(f % 24).padStart(2, '0')}`; };

async function api(path, body) {
  const r = await fetch(`/api/p/${encodeURIComponent(project)}/${path}`, body === undefined ? {} :
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

let toastTimer;
function toast(msg, err) {
  const t = $('toast'); t.textContent = msg; t.className = err ? 'err' : ''; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, err ? 5000 : 1800);
}

// ------------------------------------------------------------------ state

function index(doc) {
  let t = 0;
  S.items = doc.seq.map((it, i) => {
    const dur = it.type === 'gap' ? it.dur : it.out - it.in;
    const o = { ...it, start: t, dur, index: i }; t += dur; return o;
  });
  S.total = t;
}

function itemAt(frame) {
  if (!S.items.length) return null;
  const f = Math.max(0, Math.min(Math.floor(frame), S.total - 1));
  let lo = 0, hi = S.items.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (S.items[mid].start <= f) lo = mid; else hi = mid - 1; }
  return S.items[lo];
}

let loading = null;
async function refresh(force) {
  if (loading) { await loading; if (!force) return; }
  loading = (async () => {
    const st = await api('state');
    const docChanged = st.rev !== S.rev || !S.doc;
    S.doc = st.doc; S.rev = st.rev; S.media = st.media; S.renders = st.renders;
    $('b-undo').disabled = !st.can_undo; $('b-redo').disabled = !st.can_redo;
    if (docChanged) { index(st.doc); computeDiff(); renderVersions(); }
    if (!S.booted) {
      S.booted = true;
      S.playhead = Math.min(st.ui.playhead || 0, S.total);
      if (st.ui.marks) S.marks = st.ui.marks;
      $('pname').textContent = st.name; document.title = `Cuento · ${st.name}`;
    }
    setupMedia();
    if (docChanged) {
      S.playhead = Math.min(S.playhead, S.total);
      renderTimeline(); renderTranscript();
      if (S.rate !== 0) { const r = S.rate; S.rate = 0; pauseAll(); setRate(r); } else show(S.playhead);
    }
    renderComments(); renderLayerList(); renderLayerBars(); syncLayers(); updatePlayhead();
    $('status').textContent = `rev ${S.rev}`;
  })();
  try { await loading; } finally { loading = null; }
}

async function op(name, args) {
  try {
    const r = await api('op', { op: name, args });
    await refresh(true);
    return r.result;
  } catch (e) { toast(e.message, true); return null; }
}

async function history(kind) {
  try { const r = await api(kind, {}); if (!r.result.ok) toast(`Nothing to ${kind}`); await refresh(true); }
  catch (e) { toast(e.message, true); }
}

let uiTimer;
function saveUi() {
  clearTimeout(uiTimer);
  uiTimer = setTimeout(() => api('ui', { ui: { playhead: Math.floor(S.playhead), marks: S.marks } }).catch(() => {}), 400);
}

function listen() {
  const es = new EventSource(`/api/p/${encodeURIComponent(project)}/events`);
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === 'ui' && ev.ui.playhead != null) { setRate(0); seek(ev.ui.playhead); return; }
    if (ev.type === 'chat') { loadChat(); return; }
    if (ev.type === 'doc' && ev.actor !== 'ui') toast(`${ev.actor}: ${ev.op.replace('_', ' ')}`);
    refresh(true);
  };
}

// ------------------------------------------------------------------ media

const pool = {};      // source id -> two <video>, so the next clip is cued before the cut
const camAudio = {};  // source id -> <audio> of the camera mic (A1), when extracted
const peaks = {};     // `${sid}.${track}` -> Uint8Array, 100 values per second
let cur = { el: null, item: null };
let pre = { el: null, id: null };

function setupMedia() {
  let waiting = [];
  for (const [sid, m] of Object.entries(S.media)) {
    if (!m.ready) { waiting.push(sid); continue; }
    if (!pool[sid]) {
      pool[sid] = [0, 1].map(() => {
        const v = document.createElement('video');
        v.src = `/media/${project}/${sid}.mp4`; v.preload = 'auto'; v.playsInline = true; v.dataset.sid = sid;
        v.addEventListener('seeked', () => { if (v._want != null) { const t = v._want; v._want = null; v.currentTime = t; } });
        $('videos').appendChild(v); return v;
      });
      loadPeaks(sid, 'a2');
    }
    if (m.cam_audio && !camAudio[sid]) {
      const a = new Audio(`/media/${project}/${sid}.cam.m4a`); a.preload = 'auto'; camAudio[sid] = a;
      loadPeaks(sid, 'a1');
    }
  }
  const used = new Set(S.items.filter((i) => i.type === 'block').map((i) => i.source));
  waiting = waiting.filter((s) => used.has(s));
  $('notice').classList.toggle('on', waiting.length > 0);
  if (waiting.length) {
    $('notice').textContent = `Proxy for ${waiting.join(', ')} is not readable yet (still encoding or offline). The timeline is editable; picture appears when the file is ready.`;
    clearTimeout(setupMedia.t); setupMedia.t = setTimeout(() => refresh(true), 8000);
  }
}

async function loadPeaks(sid, track) {
  const key = `${sid}.${track}`;
  if (peaks[key]) return;
  try {
    const r = await fetch(`/peaks/${project}/${sid}?track=${track}`);
    if (!r.ok) throw new Error();
    peaks[key] = new Uint8Array(await r.arrayBuffer());
    drawWaves();
  } catch { setTimeout(() => loadPeaks(sid, track), 10000); }
}

function seekTo(v, t) {
  if (v.seeking) { v._want = t; return; }
  if (Math.abs(v.currentTime - t) > 0.002) v.currentTime = t;
}

function activate(v) {
  for (const pair of Object.values(pool)) for (const x of pair) x.classList.toggle('on', x === v);
}

function pauseAll() {
  for (const pair of Object.values(pool)) for (const x of pair) x.pause();
  for (const a of Object.values(camAudio)) a.pause();
}

// Paused display of one timeline frame.
function show(frame) {
  const it = itemAt(frame);
  if (!it || it.type === 'gap' || !pool[it.source]) { activate(null); cur = { el: null, item: it }; return; }
  const v = cur.el && cur.el.dataset.sid === it.source ? cur.el : pool[it.source][0];
  const f = Math.min(Math.floor(frame), it.start + it.dur - 1);
  seekTo(v, (it.in + (f - it.start) + 0.5) / FPS);
  activate(v); cur = { el: v, item: it };
}

function showSource(sid, frame) {
  if (!pool[sid]) return;
  const v = cur.el && cur.el.dataset.sid === sid ? cur.el : pool[sid][0];
  seekTo(v, (frame + 0.5) / FPS); activate(v);
}

function seek(frame) {
  S.playhead = Math.max(0, Math.min(frame, S.total));
  if (S.rate > 0) startForward(); else show(S.playhead);
  updatePlayhead(); syncLayers(); saveUi(); followPlayhead();
}

function audible(it) { return S.rate >= 0.5 && S.rate <= 2; }

function startClip(v, it, t) {
  if (t != null && Math.abs(v.currentTime - t) > 0.02) v.currentTime = t;
  const cam = it.audio === 'cam' && camAudio[it.source];
  v.playbackRate = S.rate; v.volume = +$('vol').value; v.muted = !audible(it) || !!cam;
  v.play().catch(() => {});
  for (const a of Object.values(camAudio)) a.pause();
  if (cam && audible(it)) {
    // camera mic replaces the lav for this clip; the two are never summed
    cam.currentTime = t != null ? t : v.currentTime; cam.playbackRate = S.rate; cam.volume = +$('vol').value;
    cam.play().catch(() => {});
  }
  activate(v); cur = { el: v, item: it }; cueNext(it);
}

function startForward() {
  pauseAll();
  if (S.playhead >= S.total) { setRate(0); return; }
  const it = itemAt(S.playhead);
  if (it.type === 'gap' || !pool[it.source]) { activate(null); cur = { el: null, item: it }; return; }
  const v = cur.el && cur.el.dataset.sid === it.source ? cur.el : pool[it.source][0];
  startClip(v, it, (it.in + (S.playhead - it.start)) / FPS + 0.0005);
}

const continues = (a, b) => b && b.type === 'block' && a.type === 'block' && b.source === a.source && b.in === a.out && (b.audio || 'lav') === (a.audio || 'lav');

// Park the idle <video> of the next clip's source on its first frame, so the cut is instant.
function cueNext(it) {
  let n = S.items[it.index + 1];
  while (continues(it, n)) { it = n; n = S.items[it.index + 1]; }
  if (!n || n.type !== 'block' || !pool[n.source]) { pre = { el: null, id: null }; return; }
  const v = pool[n.source].find((x) => x !== cur.el);
  v.pause(); v._want = null; v.currentTime = n.in / FPS + 0.0005;
  pre = { el: v, id: n.id };
}

function advance(it) {
  const next = S.items[it.index + 1];
  if (!next) { S.playhead = S.total; setRate(0); return; }
  S.playhead = next.start;
  if (cur.el && continues(it, next)) { cur.item = next; return; }   // same media runs on through a split
  if (cur.el) cur.el.pause();
  if (next.type === 'gap' || !pool[next.source]) { for (const a of Object.values(camAudio)) a.pause(); activate(null); cur = { el: null, item: next }; return; }
  if (pre.id === next.id) startClip(pre.el, next, null);
  else startClip(pool[next.source].find((x) => x !== cur.el), next, next.in / FPS + 0.0005);
}

let last = 0, ticking = false;
function tick(now) {
  const dt = Math.min(0.1, (now - last) / 1000); last = now;
  if (S.rate > 0) {
    const it = cur.item;
    if (!it) { setRate(0); }
    else {
      if (cur.el) { if (!cur.el.seeking) S.playhead = Math.max(it.start, it.start + cur.el.currentTime * FPS - it.in); }
      else S.playhead += S.rate * dt * FPS;
      if (S.playhead >= it.start + it.dur - 0.02) advance(it);
    }
  } else if (S.rate < 0) {
    S.playhead += S.rate * dt * FPS;
    if (S.playhead <= 0) { S.playhead = 0; setRate(0); } else show(S.playhead);
  }
  updatePlayhead(); syncLayers(); followPlayhead();
  if (S.rate !== 0) requestAnimationFrame(tick); else ticking = false;
}

function setRate(r) {
  S.rate = r;
  $('rate').textContent = r === 0 || r === 1 ? '' : `${r > 0 ? '▶' : '◀'} ${Math.abs(r)}×`;
  $('t-play').querySelector('use').setAttribute('href', r === 0 ? '#i-play' : '#i-pause');
  if (r === 0) {
    pauseAll(); S.playhead = Math.min(S.total, Math.round(S.playhead));
    show(S.playhead); updatePlayhead(); syncLayers(); saveUi(); return;
  }
  if (r > 0) startForward(); else pauseAll();
  if (S.rate !== 0 && !ticking) { ticking = true; last = performance.now(); requestAnimationFrame(tick); }
}

function step(n) { setRate(0); seek(Math.round(S.playhead) + n); }

function edits() { return [...S.items.map((i) => i.start), S.total]; }
function jumpEdit(dir) {
  const p = Math.round(S.playhead), e = edits();
  const target = dir > 0 ? e.find((x) => x > p) : [...e].reverse().find((x) => x < p);
  if (target != null) { setRate(0); seek(target); }
}

// ------------------------------------------------------------------ layers in the viewer
// Each visible layer is composited in the browser over the proxy: its rendered
// alpha WebM when the render for its content hash exists, else the live page.

const views = new Map(); // key -> { node, kind, id }

function layerViewKey(l) {
  const r = S.renders[l.id] || {};
  const kind = S.layerMode === 'rendered' && r.ready ? 'video' : 'live';
  return { key: `${l.id}:${r.hash}:${kind}`, kind, hash: r.hash };
}

function syncLayers() {
  if (!S.doc) return;
  const p = S.playhead, near = 3 * FPS, keep = new Set();
  const order = { background: 1, behind_person: 2, above_person: 3 };
  for (const l of S.doc.layers) {
    if (p < l.start - near || p >= l.start + l.dur + near) continue;
    const { key, kind, hash } = layerViewKey(l);
    keep.add(key);
    let view = views.get(key);
    if (!view) {
      const node = kind === 'video' ? document.createElement('video') : document.createElement('iframe');
      if (kind === 'video') { node.muted = true; node.playsInline = true; node.preload = 'auto'; node.src = `/render/${project}/${hash}.webm`; }
      else {
        // a layer is code: run it in an opaque origin so it cannot reach the editor or the API
        node.setAttribute('sandbox', 'allow-scripts'); node.setAttribute('scrolling', 'no');
        node.addEventListener('load', syncLayers);
        node.src = `/layer/${project}/${l.id}.html?h=${hash}`;
      }
      // Until person mattes are available, all layers render in front of the picture.
      node.style.zIndex = order[l.z] || 3;
      $('layers').appendChild(node);
      view = { node, kind }; views.set(key, view); fitLayers();
    }
    const f = Math.floor(p) - l.start, on = f >= 0 && f < l.dur;
    view.node.classList.toggle('on', on);
    const t = (Math.max(0, Math.min(f, l.dur - 1)) + 0.5) / FPS;
    if (kind === 'live') { view.node.contentWindow.postMessage({ cuentoSeek: t - 0.5 / FPS }, '*'); continue; }
    const v = view.node;
    if (on && S.rate > 0 && S.rate <= 2) {
      const want = (p - l.start) / FPS;
      v.playbackRate = S.rate;
      if (v.paused) { v.currentTime = want; v.play().catch(() => {}); }
      else if (Math.abs(v.currentTime - want) > 0.12) v.currentTime = want;
    } else { if (!v.paused) v.pause(); seekTo(v, t); }
  }
  for (const [key, view] of views) if (!keep.has(key)) { view.node.remove(); views.delete(key); }
}

function fitLayers() {
  if (!$('stage')) return;   // the project list has no stage
  const k = $('stage').clientWidth / 1920;
  for (const v of views.values()) if (v.kind === 'live') v.node.style.transform = `scale(${k})`;
}
new ResizeObserver(() => { fitLayers(); sizeCanvases(); }).observe($('stage'));

// ------------------------------------------------------------------ timeline

const scroller = $('tl-scroll'), inner = $('tl-inner');
const x = (f) => f * S.ppf;
const hue = (s) => { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };

// one-shot: fit on load and on \, then hold the zoom so edits don't rescale the timeline
function fitZoom() { S.fit = false; S.ppf = Math.max(0.005, (scroller.clientWidth - 24) / Math.max(1, S.total)); }

function setZoom(ppf, anchorFrame) {
  S.fit = false;
  const a = anchorFrame ?? S.playhead, off = x(a) - scroller.scrollLeft;
  S.ppf = Math.max(0.005, Math.min(40, ppf));
  renderTimeline();
  scroller.scrollLeft = x(a) - off;
  drawAll();
}

function renderTimeline() {
  if (S.fit) fitZoom();
  inner.style.width = `${x(S.total) + 240}px`;
  const v1 = $('lane-v1'), secs = $('lane-sections');
  v1.querySelectorAll('.block,.gap').forEach((n) => n.remove());
  secs.textContent = '';
  let run = null;
  for (const it of S.items) {
    const n = el('div', it.type === 'gap' ? 'gap' : 'block');
    n.style.left = `${x(it.start)}px`; n.style.width = `${Math.max(2, x(it.dur) - 1)}px`;
    n.dataset.id = it.id;
    if (it.id === S.sel || S.multi.has(it.id)) n.classList.add('sel');
    if (S.diff && S.only.has(it.id)) n.classList.add(S.only.get(it.id));
    if (it.type === 'block') {
      n.title = `${it.section || ''}\n${it.text || ''}\n${it.source} ${it.in}–${it.out} · ${tc(it.dur)} · audio: ${it.audio || 'lav'}`;
      const w = x(it.dur), ready = S.media[it.source] && S.media[it.source].ready;
      if (w > 40 && ready) {
        const th = el('div', 'thumb'); th.style.backgroundImage = `url(/thumb/${project}/${it.source}/${it.in}.jpg)`; n.appendChild(th);
        if (w < 150) th.style.width = '100%';
      }
      if (w >= 150 || (w > 40 && !ready)) { const txt = el('div', 'txt', it.text || ''); if (!ready) txt.style.left = '6px'; n.appendChild(txt); }
      n.appendChild(el('div', 'h l')); n.appendChild(el('div', 'h r'));
      const sec = it.section || '';
      if (run && run.sec === sec) run.end = it.start + it.dur; else { run = { sec, start: it.start, end: it.start + it.dur }; (renderTimeline.runs ||= []).push(run); }
    } else run = null;
    v1.appendChild(n);
  }
  for (const r of renderTimeline.runs || []) {
    if (!r.sec) continue;
    const b = el('div', 'secbar', r.sec); b.title = r.sec;
    b.style.left = `${x(r.start)}px`; b.style.width = `${Math.max(2, x(r.end - r.start) - 2)}px`;
    b.style.background = `hsl(${hue(r.sec)} 45% 62%)`;
    secs.appendChild(b);
  }
  renderTimeline.runs = [];
  renderLayerBars(); renderCommentMarks(); renderFx(); renderMarks(); updatePlayhead(); sizeCanvases();
}

function renderLayerBars() {
  if (!S.doc) return;
  const lane = $('lane-layers');
  lane.querySelectorAll('.lbar').forEach((n) => n.remove());
  const rows = [];
  for (const l of [...S.doc.layers].sort((a, b) => a.start - b.start)) {
    let row = rows.findIndex((end) => end <= l.start);
    if (row < 0) { row = rows.length; rows.push(0); }
    rows[row] = l.start + l.dur;
    const r = S.renders[l.id] || {};
    const b = el('div', `lbar${r.ready ? '' : ' pending'}${l.id === S.curLayer ? ' cur' : ''}`, l.name);
    b.title = `${l.name} · ${tc(l.start)}–${tc(l.start + l.dur)} · ${l.z} · ${r.ready ? 'rendered' : r.status || 'queued'}`;
    b.style.left = `${x(l.start)}px`; b.style.width = `${Math.max(8, x(l.dur))}px`; b.style.top = `${2 + row * 18}px`;
    b.dataset.id = l.id;
    lane.appendChild(b);
  }
  lane.style.height = `${Math.max(1, rows.length) * 18 + 4}px`;
}

function renderCommentMarks() {
  const lane = $('lane-comments');
  lane.querySelectorAll('.cm').forEach((n) => n.remove());
  for (const c of S.doc.comments) {
    const m = el('div', `cm ${c.status}${c.id === S.curComment ? ' cur' : ''}`);
    m.style.left = `${x(c.start)}px`; m.style.width = `${Math.max(9, x(c.dur))}px`;
    m.title = c.text; m.dataset.id = c.id;
    lane.appendChild(m);
  }
}

function renderFx() {
  document.querySelectorAll('.fx').forEach((n) => n.remove());
  for (const fx of S.doc.audio_fx) {
    const n = el('div', 'fx', fx.chain.map((c) => c.type || 'fx').join(' + '));
    n.style.left = `${x(fx.start)}px`; n.style.width = `${Math.max(4, x(fx.dur))}px`;
    n.title = JSON.stringify(fx.chain);
    $(`lane-${fx.track}`).appendChild(n);
  }
}

function renderMarks() {
  const m = $('marks'), a = S.marks.in, b = S.marks.out;
  m.hidden = a == null && b == null;
  if (m.hidden) return;
  const s = a ?? b, e = b ?? a;
  m.style.left = `${x(Math.min(s, e))}px`; m.style.width = `${Math.max(1, x(Math.abs(e - s)))}px`;
}

function markRange() {
  const { in: a, out: b } = S.marks;
  return a != null && b != null && b > a ? [a, b] : null;
}

let lastItemId = null;
function updatePlayhead() {
  $('playhead').style.transform = `translateX(${x(S.playhead)}px)`;
  const f = Math.floor(S.playhead);
  $('tc').textContent = `${tc(f)}`;
  const it = itemAt(f);
  if (it && it.id !== lastItemId) {
    lastItemId = it.id;
    document.querySelectorAll('.line.cur').forEach((n) => n.classList.remove('cur'));
    const line = document.querySelector(`.line[data-id="${it.id}"]`);
    if (line) { line.classList.add('cur'); if ($('transcript').classList.contains('on')) line.scrollIntoView({ block: 'nearest' }); }
  }
  $('clipinfo').textContent = it && it.type === 'block'
    ? `f${f} / ${S.total} · ${it.source} @ ${it.in + (f - it.start)} · clip ${it.index + 1}/${S.items.length}` : `f${f} / ${S.total}`;
}

function followPlayhead() {
  if (S.rate === 0) return;
  const px = x(S.playhead), w = scroller.clientWidth;
  if (px > scroller.scrollLeft + w - 30) scroller.scrollLeft = px - 60;
  else if (px < scroller.scrollLeft) scroller.scrollLeft = Math.max(0, px - w + 60);
}

function revealPlayhead() {
  const px = x(S.playhead), w = scroller.clientWidth;
  if (px < scroller.scrollLeft + 20 || px > scroller.scrollLeft + w - 20) scroller.scrollLeft = Math.max(0, px - w / 3);
}

// ---- canvases (ruler and waveforms cover only the visible window)

function sizeCanvases() {
  if (!$('timeline')) return;   // the project list has no timeline
  const dpr = devicePixelRatio || 1, w = scroller.clientWidth;
  for (const c of [$('ruler'), ...document.querySelectorAll('.lane.audio canvas')]) {
    const h = c.id === 'ruler' ? 22 : 40;
    c.style.width = `${w}px`; c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  }
  drawAll();
}

function drawAll() { drawRuler(); drawWaves(); }

function drawRuler() {
  const c = $('ruler'), g = c.getContext('2d'), dpr = devicePixelRatio || 1, w = c.width / dpr, left = scroller.scrollLeft;
  g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, 22);
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const stepS = steps.find((s) => s * 24 * S.ppf >= 70) || 600;
  g.fillStyle = '#8d9199'; g.strokeStyle = '#4a4e55'; g.font = '10px -apple-system, sans-serif'; g.textBaseline = 'top';
  for (let s = Math.floor(left / S.ppf / 24 / stepS) * stepS; ; s += stepS) {
    const px = s * 24 * S.ppf - left;
    if (px > w) break;
    if (px < -60) continue;
    g.beginPath(); g.moveTo(px + 0.5, 12); g.lineTo(px + 0.5, 22); g.stroke();
    g.fillText(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`, px + 3, 2);
  }
}

function drawWaves() {
  if (!S.doc) return;
  const dpr = devicePixelRatio || 1, left = scroller.scrollLeft;
  for (const track of ['a2', 'a1']) {
    const lane = $(`lane-${track}`);
    if (lane.hidden) continue;
    const c = lane.querySelector('canvas'), g = c.getContext('2d'), w = c.width / dpr, H = 40;
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, H);
    const f0 = left / S.ppf, f1 = (left + w) / S.ppf;
    for (const it of S.items) {
      if (it.start + it.dur < f0 || it.start > f1 || it.type !== 'block') continue;
      const pk = peaks[`${it.source}.${track}`];
      const heard = (it.audio || 'lav') === (track === 'a2' ? 'lav' : 'cam');
      const x0 = Math.max(0, x(it.start) - left), x1 = Math.min(w, x(it.start + it.dur) - left - 1);
      g.fillStyle = heard ? '#18221d' : '#16181b'; g.fillRect(x0, 1, Math.max(1, x1 - x0), H - 2);
      if (!pk) continue;
      g.fillStyle = heard ? '#4ccb86' : '#4c5a55';
      for (let px = Math.floor(x0); px < x1; px++) {
        const fa = (px + left) / S.ppf - it.start + it.in, fb = fa + 1 / S.ppf;
        const a = Math.floor(fa / FPS * 100), b = Math.max(a + 1, Math.ceil(fb / FPS * 100));
        let m = 0; for (let i = a; i < b && i < pk.length; i++) if (pk[i] > m) m = pk[i];
        const h = Math.max(1, Math.sqrt(m / 255) * (H - 4));
        g.fillRect(px, (H - h) / 2, 1, h);
      }
    }
  }
}

scroller.addEventListener('scroll', drawAll);
addEventListener('resize', sizeCanvases);

scroller.addEventListener('wheel', (e) => {
  if (e.metaKey || e.ctrlKey) {
    e.preventDefault();
    const r = inner.getBoundingClientRect();
    setZoom(S.ppf * Math.exp(-e.deltaY * 0.01), (e.clientX - r.left) / S.ppf);
  } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); scroller.scrollLeft += e.deltaY; }
}, { passive: false });

// ---- pointer: scrub, select, trim, move layer

const tip = el('div'); tip.id = 'tip'; tip.hidden = true; document.body.appendChild(tip);
const frameAtX = (clientX) => Math.max(0, Math.min(S.total, Math.round((clientX - inner.getBoundingClientRect().left) / S.ppf)));

function drag(move, up) {
  const m = (e) => move(e), u = (e) => { removeEventListener('mousemove', m); removeEventListener('mouseup', u); tip.hidden = true; up && up(e); };
  addEventListener('mousemove', m); addEventListener('mouseup', u);
}

inner.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  const t = e.target;
  if (t.classList.contains('h')) return S.tool === 'trim' ? startRoll(e, t) : startTrim(e, t);
  if (t.classList.contains('lbar')) return startLayerMove(e, t);
  if (S.tool === 'trim' && t.closest('.block')) return e.shiftKey ? startSlide(e, t.closest('.block')) : startSlip(e, t.closest('.block'));
  if (t.classList.contains('cm')) { focusComment(t.dataset.id, true); return; }
  if (S.tool === 'razor' && t.closest('.block')) {
    e.preventDefault();
    const f = frameAtX(e.clientX);   // read before setRate, which can scroll the timeline
    setRate(0);
    splitAtPlayhead(f);
    return;
  }
  const blockNode = t.closest('.block,.gap');
  if (blockNode) {
    const id = blockNode.dataset.id;
    if (markRange()) clearMarks();   // clicking a clip drops stale I/O marks
    if (e.metaKey || e.ctrlKey) {     // ⌘-click adds or removes one clip
      if (!S.multi.size && S.sel) S.multi.add(S.sel);
      if (S.multi.has(id)) S.multi.delete(id); else S.multi.add(id);
      S.sel = S.multi.has(id) ? id : [...S.multi].pop() || null;
      return markSel();
    }
    if (e.shiftKey && S.sel) {        // ⇧-click selects every item between
      const a = S.items.findIndex((i) => i.id === S.sel), b = S.items.findIndex((i) => i.id === id);
      S.multi = new Set(S.items.slice(Math.min(a, b), Math.max(a, b) + 1).map((i) => i.id));
      return markSel();
    }
    S.sel = id; S.multi.clear(); markSel();
    return startClipMove(e, blockNode);
  }
  setRate(0); seek(snap(frameAtX(e.clientX)));
  drag((ev) => seek(snap(frameAtX(ev.clientX))), () => snapLine.remove());
});

// Razor (C): click a clip to split it at that frame; V goes back to select.
const razorLine = el('div'); razorLine.id = 'razorline';
function setTool(tool) {
  S.tool = tool;
  for (const t of ['razor', 'trim']) { document.body.classList.toggle(t, tool === t); $(`b-${t}`).classList.toggle('on', tool === t); }
  if (tool !== 'razor') razorLine.remove();
}
inner.addEventListener('mousemove', (e) => {
  if (S.tool !== 'razor') return;
  if (!e.target.closest('.block')) { razorLine.remove(); return; }
  if (!razorLine.parentNode) inner.appendChild(razorLine);
  razorLine.style.left = `${x(frameAtX(e.clientX))}px`;
});
inner.addEventListener('mouseleave', () => razorLine.remove());

inner.addEventListener('dblclick', (e) => {
  const lane = e.target.closest('.lane.audio');
  if (lane) {
    const it = itemAt(frameAtX(e.clientX));
    if (!it || it.type !== 'block') return;
    const want = lane.id === 'lane-a1' ? 'cam' : 'lav';
    if (want === 'cam' && !(S.media[it.source] || {}).cam_audio) return toast('Camera audio is not extracted for this source yet (cuento cam-audio)', true);
    op('clip_set', { id: it.id, audio: want });
    return;
  }
  const b = e.target.closest('.block');
  if (b) { const it = S.items.find((i) => i.id === b.dataset.id); setRate(0); seek(it.start); }
});

// Drag a clip to a new place in the sequence; a click without movement only selects.
function startClipMove(e, node) {
  e.preventDefault();
  const it = S.items.find((i) => i.id === node.dataset.id), x0 = e.clientX;
  let moving = false, to = null;
  const mark = el('div'); mark.id = 'dropmark';
  drag((ev) => {
    if (!moving && Math.abs(ev.clientX - x0) < 5) return;
    if (!moving) { moving = true; setRate(0); node.classList.add('moving'); inner.appendChild(mark); }
    const f = frameAtX(ev.clientX), hit = S.items.find((i) => f < i.start + i.dur / 2);
    to = hit ? hit.index : S.items.length;
    mark.style.left = `${x(hit ? hit.start : S.total)}px`;
    tip.hidden = false; tip.textContent = hit ? `before ${hit.section || 'clip'} · ${tc(hit.start)}` : 'to the end';
    tip.style.left = `${ev.clientX + 12}px`; tip.style.top = `${ev.clientY - 28}px`;
  }, async () => {
    mark.remove(); node.classList.remove('moving');
    if (!moving || to == null) return;
    const index = to > it.index ? to - 1 : to;
    if (index !== it.index) await op('clip_move', { id: it.id, index });
    show(S.playhead);
  });
}

function markSel() {
  document.querySelectorAll('.block,.gap').forEach((n) => n.classList.toggle('sel', n.dataset.id === S.sel || S.multi.has(n.dataset.id)));
}
const selectedIds = () => (S.multi.size ? [...S.multi] : S.sel ? [S.sel] : []);

// Snapping (N): edits, playhead, marks and comment starts pull within 8 px.
const snapLine = el('div'); snapLine.id = 'snapline';
function snap(f, skip = []) {
  snapLine.remove();
  if (!S.snap) return f;
  const pts = [...edits(), Math.round(S.playhead), S.marks.in, S.marks.out, ...S.doc.comments.map((c) => c.start)];
  let best = f, bd = 8 / S.ppf;
  for (const p of pts) if (p != null && !skip.includes(p) && Math.abs(p - f) < bd) { best = p; bd = Math.abs(p - f); }
  if (best !== f || pts.includes(f)) { snapLine.style.left = `${x(best)}px`; inner.appendChild(snapLine); }
  return best;
}
function toggleSnap() {
  S.snap = !S.snap; $('b-snap').classList.toggle('on', S.snap); toast(S.snap ? 'Snapping on' : 'Snapping off');
  try { localStorage.setItem('cuento.snap', S.snap ? '1' : '0'); } catch (e) { /* storage blocked */ }
}
const srcFrames = (it) => (it.type === 'block' && (S.doc.sources[it.source] || {}).frames) || Infinity;
const nodeOf = (it) => inner.querySelector(`[data-id="${it.id}"]`);
function place(it, start, dur) { const n = nodeOf(it); if (n) { n.style.left = `${x(start)}px`; n.style.width = `${Math.max(2, x(dur) - 1)}px`; } }
function trimTip(ev, text) { tip.hidden = false; tip.textContent = text; tip.style.left = `${ev.clientX + 12}px`; tip.style.top = `${ev.clientY - 28}px`; }
const sgn = (d) => `${d >= 0 ? '+' : ''}${d}f`;

// Trim tool (T). Edge: roll the edit. Body: slip the source. ⇧ + body: slide the clip. Length never changes.
function startRoll(e, handle) {
  e.preventDefault(); setRate(0);
  const node = handle.parentNode, i = S.items.findIndex((q) => q.id === node.dataset.id);
  const [A, B] = handle.classList.contains('l') ? [S.items[i - 1], S.items[i]] : [S.items[i], S.items[i + 1]];
  if (!A || !B) { toast('Roll needs a clip on both sides of the edit'); return; }
  const edit = A.start + A.dur, x0 = e.clientX;
  const lo = Math.max(1 - A.dur, B.type === 'block' ? -B.in : -Infinity);
  const hi = Math.min(B.dur - 1, A.type === 'block' ? srcFrames(A) - A.out : Infinity);
  let d = 0;
  drag((ev) => {
    d = Math.max(lo, Math.min(hi, snap(edit + Math.round((ev.clientX - x0) / S.ppf), [edit]) - edit));
    place(A, A.start, A.dur + d); place(B, B.start + d, B.dur - d);
    if (A.type === 'block') showSource(A.source, A.out + d - 1);
    trimTip(ev, `roll ${sgn(d)}`);
  }, async () => {
    snapLine.remove();
    if (d) await op('clip_roll', { id: A.id, delta: d }); else renderTimeline();
    show(S.playhead);
  });
}

function startSlip(e, node) {
  e.preventDefault(); setRate(0);
  const it = S.items.find((q) => q.id === node.dataset.id), x0 = e.clientX;
  S.sel = it.id; S.multi.clear(); markSel();
  let d = 0;
  drag((ev) => {
    d = Math.max(-it.in, Math.min(srcFrames(it) - it.out, Math.round((x0 - ev.clientX) / S.ppf)));
    showSource(it.source, it.in + d);
    trimTip(ev, `slip ${sgn(d)} · ${tc(it.in + d)}–${tc(it.out + d)}`);
  }, async () => {
    if (d) await op('clip_slip', { id: it.id, delta: d });
    show(S.playhead);
  });
}

function startSlide(e, node) {
  e.preventDefault(); setRate(0);
  const i = S.items.findIndex((q) => q.id === node.dataset.id), it = S.items[i], P = S.items[i - 1], N = S.items[i + 1];
  if (!P || !N) { toast('Slide needs an item on both sides'); return; }
  S.sel = it.id; S.multi.clear(); markSel();
  const x0 = e.clientX;
  const lo = Math.max(1 - P.dur, N.type === 'block' ? -N.in : -Infinity);
  const hi = Math.min(N.dur - 1, P.type === 'block' ? srcFrames(P) - P.out : Infinity);
  let d = 0;
  drag((ev) => {
    d = Math.max(lo, Math.min(hi, snap(it.start + Math.round((ev.clientX - x0) / S.ppf), [it.start]) - it.start));
    place(P, P.start, P.dur + d); place(it, it.start + d, it.dur); place(N, N.start + d, N.dur - d);
    trimTip(ev, `slide ${sgn(d)}`);
  }, async () => {
    snapLine.remove();
    if (d) await op('clip_slide', { id: it.id, delta: d }); else renderTimeline();
    show(S.playhead);
  });
}

function startTrim(e, handle) {
  e.preventDefault(); setRate(0);
  const node = handle.parentNode, it = S.items.find((i) => i.id === node.dataset.id);
  const left = handle.classList.contains('l'), x0 = e.clientX, max = (S.doc.sources[it.source] || {}).frames || Infinity;
  let nin = it.in, nout = it.out;
  S.sel = it.id; node.classList.add('trimming');
  drag((ev) => {
    let d = Math.round((ev.clientX - x0) / S.ppf);
    const edge = left ? it.start : it.start + it.dur;   // where the dragged edge sits now
    d = snap(edge + d, [edge]) - edge;
    if (left) nin = Math.max(0, Math.min(it.out - 1, it.in + d)); else nout = Math.max(it.in + 1, Math.min(max, it.out + d));
    // show the frame the edit will land on while dragging
    showSource(it.source, left ? nin : nout - 1);
    node.style.left = `${x(it.start + (nin - it.in))}px`; node.style.width = `${Math.max(2, x(nout - nin) - 1)}px`;
    const delta = left ? nin - it.in : nout - it.out;
    tip.hidden = false; tip.textContent = `${delta >= 0 ? '+' : ''}${delta}f · ${tc(nout - nin)}`;
    tip.style.left = `${ev.clientX + 12}px`; tip.style.top = `${ev.clientY - 28}px`;
  }, async () => {
    snapLine.remove(); node.classList.remove('trimming');
    if (nin !== it.in || nout !== it.out) await op('clip_trim', left ? { id: it.id, in: nin } : { id: it.id, out: nout });
    else renderTimeline();
    show(S.playhead);
  });
}

function startLayerMove(e, node) {
  e.preventDefault();
  const l = S.doc.layers.find((q) => q.id === node.dataset.id), x0 = e.clientX;
  let start = l.start;
  S.curLayer = l.id; showTab('layerlist'); renderLayerList();
  drag((ev) => {
    start = Math.max(0, snap(l.start + Math.round((ev.clientX - x0) / S.ppf), [l.start]));
    node.style.left = `${x(start)}px`;
  }, () => { snapLine.remove(); if (start !== l.start) op('layer_update', { id: l.id, start }); else renderLayerBars(); });
}

// ------------------------------------------------------------------ versions
// A project keeps more than one cut of the same footage. Switching lands on the
// same source moment when the other cut has it, so two versions can be compared
// in place. Diff marks what only this version has.

function versionNames() { return [S.doc.version || 'A', ...Object.keys(S.doc.versions || {})].sort(); }
function otherVersion() { const n = versionNames(); return n.length < 2 ? null : n[(n.indexOf(S.doc.version || 'A') + 1) % n.length]; }
function seqOf(name) { return name === (S.doc.version || 'A') ? S.doc.seq : S.doc.versions[name].seq; }
const seqDur = (seq) => seq.reduce((t, it) => t + (it.type === 'gap' ? it.dur : it.out - it.in), 0);

function computeDiff() {
  S.only = new Map(); S.diffInfo = null;
  const other = otherVersion();
  if (!other) return;
  const there = {};
  for (const it of seqOf(other)) if (it.type === 'block') (there[it.source] ||= []).push([it.in, it.out]);
  let frames = 0;
  for (const it of S.items) {
    if (it.type !== 'block') continue;
    // parked ranges of one source can overlap after edits; count each frame once
    const cuts = (there[it.source] || []).map(([a, b]) => [Math.max(a, it.in), Math.min(b, it.out)]).filter(([a, b]) => b > a).sort((p, q) => p[0] - q[0]);
    let shared = 0, end = it.in;
    for (const [a, b] of cuts) { if (b > end) { shared += b - Math.max(a, end); end = b; } }
    if (shared < it.dur) { S.only.set(it.id, shared === 0 ? 'only' : 'part'); frames += it.dur - shared; }
  }
  S.diffInfo = { other, frames, clips: S.only.size };
}

function renderVersions() {
  const box = $('versions'); box.textContent = '';
  const names = versionNames();
  for (const name of names) {
    const b = el('button', name === (S.doc.version || 'A') ? 'on' : null, name);
    b.appendChild(el('small', null, tc(seqDur(seqOf(name)))));
    b.onclick = () => { b.blur(); switchVersion(name); };
    box.appendChild(b);
  }
  if (names.length > 1) {
    const d = el('button', S.diff ? 'on' : null, 'Diff');
    d.title = 'Mark what only this version has (D)';
    d.onclick = () => { d.blur(); toggleDiff(); };
    box.appendChild(d);
    if (S.diff && S.diffInfo) box.appendChild(el('span', 'diffinfo', S.diffInfo.clips ? `${S.diffInfo.clips} clips, ${tc(S.diffInfo.frames)} not in ${S.diffInfo.other}` : `nothing ${S.diffInfo.other} lacks`));
  }
}

function toggleDiff() { S.diff = !S.diff; renderVersions(); renderTimeline(); }

async function switchVersion(name) {
  if (name === (S.doc.version || 'A')) return;
  const it = itemAt(S.playhead);
  const at = it && it.type === 'block' ? { sid: it.source, f: it.in + Math.floor(S.playhead) - it.start } : null;
  setRate(0);
  if (!await op('version_switch', { name })) return;
  if (at) {
    const same = S.items.filter((i) => i.type === 'block' && i.source === at.sid);
    const hit = same.find((i) => i.in <= at.f && at.f < i.out);
    const near = hit || same.sort((a, b) => Math.abs(a.in - at.f) - Math.abs(b.in - at.f))[0];
    if (near) seek(hit ? near.start + at.f - near.in : near.start);
  }
  revealPlayhead();
  toast(`Version ${name}`);
}

function cycleVersion() { const o = otherVersion(); if (o) switchVersion(o); else toast('This project has one version'); }

// ------------------------------------------------------------------ chat
// Messages go to a headless Claude Code pass run by the daemon. Edit is one
// continuing conversation that changes the timeline; Story is a read-only pass
// whose report is handed to the edit conversation with the next message.

async function loadChat() {
  try { S.chat = await (await fetch(`/api/p/${encodeURIComponent(project)}/chat`)).json(); renderChat(); } catch { /* daemon restarting */ }
}

function renderChat() {
  const c = S.chat, log = $('chatlog'), sel = $('chatmode');
  if (!c) return;
  if (!sel.options.length) for (const [key, m] of Object.entries(c.modes)) { const o = el('option', null, `${m.label} · ${m.model}`); o.value = key; sel.appendChild(o); }
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = '';
  if (!c.messages.length) log.appendChild(el('div', 'empty', 'Talk to the edit here: "tighten the intro", "work the comments", "make the short version open on the pirate line". Switch the menu to Story for a read-only pass that looks for the best story across your versions; its findings go to the edit chat with your next message.'));
  for (const m of c.messages) {
    const n = el('div', `msg ${m.role === 'user' ? 'user' : m.mode} ${m.status || ''}`);
    const who = el('div', 'who');
    who.appendChild(el('span', null, m.role === 'user' ? `You → ${c.modes[m.mode] ? c.modes[m.mode].label : m.mode}` : `${c.modes[m.mode] ? c.modes[m.mode].label : m.mode} · ${m.model}`));
    if (m.status === 'running') who.appendChild(el('span', 'pill rendering', 'working'));
    if (m.status === 'error') who.appendChild(el('span', 'pill error', 'failed'));
    if (m.role === 'pass' && m.mode === 'story' && m.status === 'done') who.appendChild(el('span', `pill ${m.fed ? 'resolved' : 'open'}`, m.fed ? 'sent to edit' : 'goes to edit next'));
    n.append(who, el('div', 'body', m.status === 'running' ? 'Reading the timeline…' : m.text));
    log.appendChild(n);
  }
  if (stick) log.scrollTop = log.scrollHeight;
  $('chatbusy').textContent = c.busy ? `${c.modes[c.busy].label} pass running…` : '';
  $('chattext').disabled = !!c.busy;
}

async function sendChat() {
  const text = $('chattext').value.trim(), mode = $('chatmode').value;
  if (!text && mode !== 'story') return;
  try { await api('chat', { mode, text }); $('chattext').value = ''; } catch (e) { toast(e.message, true); }
  loadChat();
}
$('chattext').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  if (e.key === 'Escape') $('chattext').blur();
});
$('chatform').addEventListener('submit', (e) => e.preventDefault());
$('chatmode').addEventListener('change', () => { $('chattext').placeholder = $('chatmode').value === 'story' ? 'Optional: what to look for. Enter runs the story pass.' : 'Tell the edit pass what to do. Enter to send.'; });

// ------------------------------------------------------------------ rail

function showTab(name) {
  document.querySelectorAll('#rail nav button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.id === name));
}
document.querySelectorAll('#rail nav button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));

const TARGETS = { v1: 'picture', a1: 'A1 camera', a2: 'A2 lav' };

function renderComments() {
  const box = $('comments');
  if (box.querySelector('textarea')) return;   // don't clobber an edit in progress
  const top = box.scrollTop;
  box.textContent = '';
  const list = [...S.doc.comments].sort((a, b) => a.start - b.start);
  const open = list.filter((c) => c.status === 'open').length;
  $('n-open').textContent = open ? `(${open})` : '';
  if (!list.length) box.appendChild(el('div', 'empty', 'No comments yet. Park the playhead (or mark a range with I and O) and press M.'));
  for (const c of list) {
    const n = el('div', `c ${c.status}${c.id === S.curComment ? ' cur' : ''}`); n.dataset.id = c.id;
    const meta = el('div', 'meta');
    meta.append(el('span', null, `${tc(c.start)} – ${tc(c.start + c.dur)}`), el('span', null, TARGETS[c.target] || c.target), el('span', `pill ${c.status}`, c.status));
    const text = el('div', 'text', c.text);
    n.append(meta, text);
    for (const r of c.thread) { const rp = el('div', 'reply'); rp.append(el('b', null, `${r.by}: `), document.createTextNode(r.text)); n.appendChild(rp); }
    if (c.layer_id) {
      const l = S.doc.layers.find((q) => q.id === c.layer_id), r = S.renders[c.layer_id] || {};
      if (l) n.appendChild(el('div', 'chip', `◆ layer: ${l.name} · ${r.ready ? 'rendered' : r.status || 'queued'}`));
    }
    const acts = el('div', 'acts');
    const edit = el('button', null, 'Edit'), res = el('button', null, c.status === 'open' ? 'Resolve' : 'Reopen'), del = el('button', null, 'Delete');
    edit.onclick = (e) => { e.stopPropagation(); editComment(n, text, c); };
    res.onclick = (e) => { e.stopPropagation(); op(c.status === 'open' ? 'comment_resolve' : 'comment_reopen', c.status === 'open' ? { id: c.id, by: 'user' } : { id: c.id }); };
    del.onclick = (e) => { e.stopPropagation(); op('comment_delete', { id: c.id }); };
    acts.append(edit, res, del); n.appendChild(acts);
    n.onclick = () => focusComment(c.id, true);
    text.ondblclick = (e) => { e.stopPropagation(); editComment(n, text, c); };
    box.appendChild(n);
  }
  box.scrollTop = top;
}

function editComment(node, textNode, c) {
  const ta = el('textarea'); ta.value = c.text; ta.rows = 3;
  textNode.replaceWith(ta); ta.focus();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const v = ta.value.trim(); ta.remove();
    if (save && v && v !== c.text) await op('comment_edit', { id: c.id, text: v }); else renderComments();
  };
  ta.onclick = (e) => e.stopPropagation();
  ta.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
  ta.onblur = () => finish(true);
}

function focusComment(id, jump) {
  const c = S.doc.comments.find((q) => q.id === id);
  if (!c) return;
  S.curComment = id; showTab('comments');
  if (jump) { setRate(0); seek(c.start); revealPlayhead(); }
  renderComments(); renderCommentMarks();
  const n = document.querySelector(`.c[data-id="${id}"]`); if (n) n.scrollIntoView({ block: 'nearest' });
}

function stepComment(dir) {
  const list = [...S.doc.comments].sort((a, b) => a.start - b.start), p = Math.floor(S.playhead);
  const c = dir > 0 ? list.find((q) => q.start > p) : [...list].reverse().find((q) => q.start < p);
  if (c) focusComment(c.id, true); else toast(dir > 0 ? 'No later comment' : 'No earlier comment');
}

function renderTranscript() {
  const box = $('tlist'), q = $('tsearch').value.trim().toLowerCase();
  box.textContent = ''; lastItemId = null;
  let sec = null;
  for (const it of S.items) {
    if (it.type !== 'block' || (q && !(it.text || '').toLowerCase().includes(q))) continue;
    if (it.section !== sec) { sec = it.section; box.appendChild(el('div', 'sec', sec || '—')); }
    const line = el('div', 'line'); line.dataset.id = it.id;
    line.append(el('small', null, tc(it.start)), document.createTextNode(it.text || '(no transcript)'));
    line.onclick = () => { setRate(0); seek(it.start); revealPlayhead(); };
    box.appendChild(line);
  }
}
$('tsearch').addEventListener('input', renderTranscript);

function renderLayerList() {
  const box = $('layerlist'); box.textContent = '';
  if (!S.doc.layers.length) box.appendChild(el('div', 'empty', 'No layers yet. Leave a comment like "pirate scroll pops up here" and ask Claude to work the comments: each one becomes its own alpha layer.'));
  for (const l of [...S.doc.layers].sort((a, b) => a.start - b.start)) {
    const r = S.renders[l.id] || {}, st = r.ready ? 'ready' : r.status || 'queued';
    const n = el('div', `lay${l.id === S.curLayer ? ' cur' : ''}`);
    const meta = el('div', 'meta');
    meta.append(el('span', null, `${tc(l.start)} – ${tc(l.start + l.dur)}`), el('span', null, l.z.replace('_', ' ')), el('span', `pill ${st}`, st === 'ready' ? 'rendered' : st));
    n.append(meta, el('div', null, l.name));
    if (r.error) n.appendChild(el('div', 'hist', r.error));
    if (l.z !== 'above_person') n.appendChild(el('div', 'hist', 'Draws over the picture until the person matte exists (M3).'));
    for (const h of l.prompt_history.slice(-3)) n.appendChild(el('div', 'hist', `“${h.text}”`));
    const acts = el('div', 'acts'), del = el('button', null, 'Delete');
    del.onclick = (e) => { e.stopPropagation(); op('layer_delete', { id: l.id }); };
    acts.appendChild(del); n.appendChild(acts);
    n.onclick = () => { S.curLayer = l.id; setRate(0); seek(l.start); revealPlayhead(); renderLayerList(); renderLayerBars(); };
    box.appendChild(n);
  }
}

// ------------------------------------------------------------------ comment box (M)

let cboxRange = null;
function openCommentBox() {
  setRate(0);
  const p = Math.floor(S.playhead), r = markRange(), it = itemAt(p);
  cboxRange = r ? [r[0], r[1] - r[0]] : [p, it ? Math.max(1, it.start + it.dur - p) : 1];
  $('cbox-range').textContent = `${tc(cboxRange[0])} – ${tc(cboxRange[0] + cboxRange[1])}${r ? ' (marked range)' : ' (playhead to end of clip)'}`;
  $('cbox').hidden = false; $('cbox-text').value = ''; $('cbox-text').focus();
}
function closeCommentBox() { $('cbox').hidden = true; $('cbox-text').blur(); }
$('cbox-text').addEventListener('keydown', async (e) => {
  if (e.key === 'Escape') { e.preventDefault(); closeCommentBox(); }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const text = $('cbox-text').value.trim();
    if (!text) return closeCommentBox();
    const r = await op('comment_add', { start: cboxRange[0], dur: cboxRange[1], text, target: $('cbox-target').value });
    closeCommentBox();
    if (r) focusComment(r.id, false);
  }
});
$('cbox').addEventListener('submit', (e) => e.preventDefault());

// ------------------------------------------------------------------ edit commands

// A selected clip wins over I/O marks: the highlighted thing is what gets deleted.
async function removeSelection(ripple) {
  const ids = selectedIds(), r = markRange();
  if (ids.length > 1) {
    setRate(0);
    const first = Math.min(...ids.map((id) => (S.items.find((i) => i.id === id) || {}).start ?? S.total));
    if (await op('clip_delete', { ids, ripple })) { S.sel = null; S.multi.clear(); clearMarks(); if (ripple) seek(Math.min(first, S.total)); }
    return;
  }
  const it = S.sel && S.items.find((i) => i.id === S.sel);
  if (it) {
    setRate(0);
    if (await op('clip_delete', { ids: [it.id], ripple })) { S.sel = null; clearMarks(); if (ripple) seek(Math.min(it.start, S.total)); }
  } else if (r) {
    setRate(0);
    if (await op('range_delete', { start: r[0], end: r[1], ripple })) { clearMarks(); if (ripple) seek(r[0]); }
  } else toast('Select a clip or mark a range (I / O) first');
}

function clearMarks() { S.marks = { in: null, out: null }; renderMarks(); saveUi(); }

// ⌘K: cut at the playhead and select the piece that ends there, so Delete removes it.
async function splitAtPlayhead(frame = Math.round(S.playhead)) {
  if (!(await op('clip_split', { frame }))) return;
  const left = S.items.find((i) => i.start + i.dur === frame);
  S.sel = left ? left.id : null; clearMarks(); renderTimeline(); seek(frame);
}

async function rippleTrim(side) {
  const p = Math.round(S.playhead), wasPlaying = S.rate;
  setRate(0);
  const r = await op('ripple_trim_to', { frame: p, side });
  if (r && r.id) seek(r.playhead); else if (r) toast('Playhead is on an edit: nothing to trim');
  if (wasPlaying > 0) setRate(wasPlaying);
}

function setMark(which, value) { S.marks[which] = value; renderMarks(); saveUi(); }

// ------------------------------------------------------------------ keys

function shuttle(dir) {
  if (S.kHeld) return;
  const same = Math.sign(S.rate) === dir && Math.abs(S.rate) >= 1;
  setRate(dir * (same ? Math.min(8, Math.abs(S.rate) * 2) : 1));
}

addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (!$('keys').hidden) { $('keys').hidden = true; if (e.key === 'Escape' || e.key === '?') return; }
  if (typing) return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key, cmd = e.metaKey || e.ctrlKey;
  let hit = true;
  if (cmd && k === 'k') splitAtPlayhead();
  else if (cmd && k === 'z') history(e.shiftKey ? 'redo' : 'undo');
  else if (cmd && e.shiftKey && k === 'm') stepComment(-1);
  else if (cmd && e.shiftKey && k === 'x') { S.marks = { in: null, out: null }; renderMarks(); saveUi(); }
  else if (cmd || e.altKey && k !== 'Delete' && k !== 'Backspace') hit = false;
  else if (k === 'l' || k === 'j') {
    const dir = k === 'l' ? 1 : -1;
    if (S.kHeld) { if (e.repeat) { if (S.rate !== dir * 0.25) setRate(dir * 0.25); } else step(dir); }
    else if (!e.repeat) shuttle(dir);
  }
  else if (k === 'k') { S.kHeld = true; if (S.rate !== 0) setRate(0); }
  else if (k === ' ') setRate(S.rate === 0 ? 1 : 0);
  else if (k === 'ArrowLeft') step(e.shiftKey ? -5 : -1);
  else if (k === 'ArrowRight') step(e.shiftKey ? 5 : 1);
  else if (k === 'ArrowUp') { jumpEdit(-1); revealPlayhead(); }
  else if (k === 'ArrowDown') { jumpEdit(1); revealPlayhead(); }
  else if (k === 'Home') { setRate(0); seek(0); revealPlayhead(); }
  else if (k === 'End') { setRate(0); seek(S.total); revealPlayhead(); }
  else if (k === 'q') rippleTrim('prev');
  else if (k === 'w') rippleTrim('next');
  else if (k === 'i') setMark('in', Math.round(S.playhead));
  else if (k === 'o') setMark('out', Math.round(S.playhead));
  else if (k === 'x') { const it = itemAt(S.playhead); if (it) { S.marks = { in: it.start, out: it.start + it.dur }; renderMarks(); saveUi(); } }
  else if (k === 'Delete' || k === 'Backspace') removeSelection(!(e.shiftKey || e.altKey));
  else if (k === 'm') { if (e.shiftKey) stepComment(1); else openCommentBox(); }
  else if (k === 'Tab') cycleVersion();
  else if (k === 'd') toggleDiff();
  else if (k === '=' || k === '+') setZoom(S.ppf * 1.5);
  else if (k === '-') setZoom(S.ppf / 1.5);
  else if (k === '\\') { S.fit = true; renderTimeline(); scroller.scrollLeft = 0; }
  else if (!cmd && k === 'c') setTool(S.tool === 'razor' ? 'select' : 'razor');
  else if (!cmd && k === 'v') setTool('select');
  else if (!cmd && k === 't') setTool(S.tool === 'trim' ? 'select' : 'trim');
  else if (!cmd && k === 'n') toggleSnap();
  else if (cmd && k === 'a') { S.multi = new Set(S.items.map((i) => i.id)); S.sel = S.items.length ? S.items[0].id : null; markSel(); }
  else if (k === 'Escape') { setTool('select'); S.marks = { in: null, out: null }; S.sel = null; S.multi.clear(); renderMarks(); renderTimeline(); saveUi(); }
  else if (k === '?') $('keys').hidden = false;
  else hit = false;
  if (hit) e.preventDefault();
});

addEventListener('keyup', (e) => {
  const k = e.key.toLowerCase();
  if (k === 'k') { S.kHeld = false; if (Math.abs(S.rate) === 0.25) setRate(0); }
  if ((k === 'l' || k === 'j') && Math.abs(S.rate) === 0.25) setRate(0);
});
addEventListener('blur', () => { S.kHeld = false; });

$('b-split').onclick = () => splitAtPlayhead();
$('b-razor').onclick = () => setTool(S.tool === 'razor' ? 'select' : 'razor');
$('b-trim').onclick = () => setTool(S.tool === 'trim' ? 'select' : 'trim');
$('b-snap').onclick = () => toggleSnap();
try { if (localStorage.getItem('cuento.snap') === '0') { S.snap = false; $('b-snap').classList.remove('on'); } } catch (e) { /* storage blocked */ }
$('b-undo').onclick = () => history('undo');
$('b-redo').onclick = () => history('redo');
$('b-keys').onclick = () => { $('keys').hidden = false; };
$('keys').onclick = () => { $('keys').hidden = true; };
$('b-a1').onclick = () => { const l = $('lane-a1'); l.hidden = !l.hidden; $('b-a1').classList.toggle('on', !l.hidden); sizeCanvases(); };
$('b-layers').onclick = () => {
  S.layerMode = S.layerMode === 'rendered' ? 'live' : 'rendered';
  $('b-layers').classList.toggle('on', S.layerMode === 'live');
  $('b-layers').title = S.layerMode === 'live' ? 'Layers: live from source. Click for rendered alpha files.' : 'Layers: preview from rendered alpha files. Click for live from source.';
  syncLayers();
};
$('vol').oninput = () => { const v = +$('vol').value; if (cur.el) cur.el.volume = v; for (const a of Object.values(camAudio)) a.volume = v; };
for (const b of document.querySelectorAll('header button, #transport button')) b.addEventListener('mouseup', () => b.blur());

// Transport under the viewer.
$('t-play').onclick = () => setRate(S.rate === 0 ? 1 : 0);
$('t-back').onclick = () => step(-1);
$('t-fwd').onclick = () => step(1);
$('t-prev').onclick = () => { setRate(0); jumpEdit(-1); };
$('t-next').onclick = () => { setRate(0); jumpEdit(1); };

// Inspector on the right can be hidden; the choice is remembered per browser.
function setInspector(on) {
  document.body.classList.toggle('noinsp', !on); $('b-side').classList.toggle('on', on);
  try { localStorage.setItem('cuento.inspector', on ? '1' : '0'); } catch (e) { /* storage blocked */ }
  requestAnimationFrame(sizeCanvases);
}
$('b-side').onclick = () => setInspector(document.body.classList.contains('noinsp'));
try { if (localStorage.getItem('cuento.inspector') === '0') setInspector(false); } catch (e) { /* storage blocked */ }

// No browser context menu over the editor surfaces.
for (const id of ['viewer', 'timeline']) $(id).addEventListener('contextmenu', (e) => e.preventDefault());

// ------------------------------------------------------------------ boot

async function boot() {
  if (!project) {
    const r = await (await fetch('/api/projects')).json();
    const box = el('div', 'projects'); box.appendChild(el('h2', null, 'Cuento projects'));
    for (const name of Object.keys(r.projects)) { const a = el('a', null, name); a.href = `/?project=${encodeURIComponent(name)}`; box.appendChild(a); }
    if (!Object.keys(r.projects).length) box.appendChild(el('div', 'empty', 'No projects yet. Create one with: cuento import-edl'));
    document.querySelector('main').replaceWith(box); $('timeline').remove(); document.body.classList.add('list');
    return;
  }
  try { await refresh(true); } catch (e) { toast(e.message, true); return; }
  listen();
  loadChat();
  revealPlayhead();
}
boot();
