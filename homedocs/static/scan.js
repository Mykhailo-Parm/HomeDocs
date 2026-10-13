'use strict';
// Мобильный сканер: фото → поиск листа на сервере → правка углов → «скан» → PDF во «Входящие»
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ls = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* недоступно */ } },
};
const FILTERS = ['auto', 'bw', 'gray', 'color'];
const FULL = [[0, 0], [1, 0], [1, 1], [0, 1]];
const MAX_SIDE = 3200;

let lang = ['en', 'uk', 'de'].includes(ls.get('hd_lang')) ? ls.get('hd_lang') : 'en';
let pages = [];        // {id, corners, detected, found, filter, rotate, v}
let lastSaved = null;  // итог последнего сохранения

function t(key, vars = {}) {
  const s = (I18N[lang] && I18N[lang][key]) ?? I18N.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');
}
function applyI18n() {
  document.documentElement.lang = lang;
  $$('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  $$('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  $$('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  $$('.langSel').forEach(s => { s.value = lang; });
}
async function api(url, opts = {}) {
  const headers = { 'X-HomeDocs': '1', ...(opts.headers || {}) };
  if (opts.body && !(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const r = await fetch(url, { ...opts, headers, credentials: 'same-origin' });
  if (r.status === 401) { location.href = '/?next=/scan.html'; throw new Error('login'); }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(typeof d.detail === 'string' ? d.detail : r.statusText || String(r.status));
  }
  return r.json();
}
function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 8000 : 4000);
}
function busy(text) {
  $('#busy').hidden = !text;
  $('#busyText').textContent = text || '';
}
function savePages() { ls.set('hd_scan_pages', JSON.stringify(pages)); }

// ---------- список страниц ----------

function render() {
  $('#pages').innerHTML = (lastSaved ? `<div class="s-done" style="grid-column:1/-1">${lastSaved}</div>` : '')
    + pages.map((p, i) => `<div class="s-page" data-i="${i}">
      <img src="/api/scanner/pages/${p.id}/result?v=${p.v}" alt="">
      <span class="n">${i + 1}</span>
      ${p.found ? '' : `<span class="warn">${esc(t('s_check_corners'))}</span>`}</div>`).join('');
  $('#hint').hidden = pages.length > 0;
  $('#foot').hidden = pages.length === 0;
  $('#saveBtn').textContent = t('s_save', { n: pages.length });
}
$('#pages').addEventListener('click', e => {
  const el = e.target.closest('.s-page');
  if (el) openEditor(+el.dataset.i);
});

// ---------- съёмка и загрузка ----------

async function shrink(file) {
  // уменьшаем на телефоне: быстрее загрузка, поворот по EXIF применяет браузер
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
    const k = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close && bmp.close();
    return await new Promise(res => c.toBlob(b => res(b || file), 'image/jpeg', 0.9));
  } catch (e) {
    return file;  // старый браузер — отправляем как есть, сервер справится
  }
}
async function addFiles(files) {
  files = [...files];
  for (let i = 0; i < files.length; i++) {
    busy(t('s_processing', { n: i + 1, total: files.length }));
    try {
      const fd = new FormData();
      fd.append('file', await shrink(files[i]), 'page.jpg');
      const r = await api('/api/scanner/pages', { method: 'POST', body: fd });
      pages.push({ id: r.id, corners: r.corners, detected: r.corners, found: r.found, filter: 'auto', rotate: 0, v: 1 });
      lastSaved = null;
      savePages();
    } catch (e) { if (e.message !== 'login') toast(t('error', { msg: e.message }), 'bad'); }
  }
  busy('');
  render();
  const last = pages[pages.length - 1];
  if (files.length === 1 && last && !last.found) openEditor(pages.length - 1);  // лист не нашёлся — сразу поправить
}
$('#camera').addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });
$('#gallery').addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });

// ---------- сохранение ----------

$('#saveBtn').addEventListener('click', async () => {
  busy(t('s_saving'));
  try {
    const r = await api('/api/scanner/finish', { method: 'POST', body: JSON.stringify({ pages: pages.map(p => p.id), name: $('#docName').value.trim(), private: $('#privScan').checked }) });
    const d = r.doc;
    lastSaved = `<b>✅ ${d && d.private ? '🔒 ' : ''}${esc(t('s_saved', { name: r.saved }))}</b>`
      + (d && d.status === 'analyzed' ? `<div>${esc(d.title || '')}</div><div class="muted small">${esc([d.person, d.sender, d.doc_date].filter(Boolean).join(' · '))}</div>` : '')
      + (r.error ? `<div class="muted small">${esc(r.error)}</div>` : '')
      + `<div style="margin-top:8px"><a href="/">${esc(t('s_open_archive'))}</a></div>`;
    pages = []; savePages();
    $('#docName').value = '';
    $('#privScan').checked = false;
  } catch (e) { if (e.message !== 'login') toast(t('error', { msg: e.message }), 'bad'); }
  busy('');
  render();
});

// ---------- редактор: углы с лупой, фильтр, поворот ----------

const ed = { i: -1, p: null, img: null, drag: -1, fit: null, pointer: null };
const canvas = $('#canvas');
const ctx = canvas.getContext('2d');

async function openEditor(i) {
  ed.i = i;
  ed.p = { ...pages[i], corners: pages[i].corners.map(c => [...c]) };
  $('#editor').hidden = false;
  renderFilters();
  ed.img = new Image();
  ed.img.onload = () => { layout(); draw(); };
  ed.img.src = `/api/scanner/pages/${ed.p.id}/original`;
}
function closeEditor() { $('#editor').hidden = true; ed.img = null; }

function renderFilters() {
  $('#filters').innerHTML = FILTERS.map(f => `<button data-f="${f}" class="${ed.p.filter === f ? 'on' : ''}">${esc(t('s_f_' + f))}</button>`).join('');
  $('#edRotate').lastElementChild.textContent = t('s_rotate') + (ed.p.rotate ? ` ${ed.p.rotate * 90}°` : '');
}
$('#filters').addEventListener('click', e => {
  const b = e.target.closest('[data-f]');
  if (b) { ed.p.filter = b.dataset.f; renderFilters(); }
});

function layout() {
  const box = $('#stage').getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  canvas.width = box.width * dpr; canvas.height = box.height * dpr;
  const pad = 24 * dpr, iw = ed.img.naturalWidth, ih = ed.img.naturalHeight;
  const k = Math.min((canvas.width - 2 * pad) / iw, (canvas.height - 2 * pad) / ih);
  ed.fit = { k, x: (canvas.width - iw * k) / 2, y: (canvas.height - ih * k) / 2, w: iw * k, h: ih * k, dpr };
}
const toCanvas = ([u, v]) => [ed.fit.x + u * ed.fit.w, ed.fit.y + v * ed.fit.h];

function draw() {
  if (!ed.img || !ed.fit) return;
  const f = ed.fit, pts = ed.p.corners.map(toCanvas);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(ed.img, f.x, f.y, f.w, f.h);
  // затемняем всё вне листа
  ctx.save();
  ctx.fillStyle = 'rgba(0,0,0,.5)';
  ctx.beginPath();
  ctx.rect(f.x, f.y, f.w, f.h);
  ctx.moveTo(...pts[0]); pts.slice(1).forEach(p => ctx.lineTo(...p)); ctx.closePath();
  ctx.fill('evenodd');
  ctx.restore();
  ctx.strokeStyle = '#4c8dff'; ctx.lineWidth = 3 * f.dpr;
  ctx.beginPath(); ctx.moveTo(...pts[0]); pts.slice(1).forEach(p => ctx.lineTo(...p)); ctx.closePath(); ctx.stroke();
  pts.forEach(([x, y], i) => {
    ctx.beginPath(); ctx.arc(x, y, (i === ed.drag ? 16 : 13) * f.dpr, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(76,141,255,.35)'; ctx.fill();
    ctx.lineWidth = 3 * f.dpr; ctx.strokeStyle = '#fff'; ctx.stroke();
  });
  if (ed.drag >= 0) loupe(pts[ed.drag]);
}
function loupe([x, y]) {
  // лупа: увеличенный участок под пальцем, чтобы точно попасть в угол листа
  const f = ed.fit, r = 60 * f.dpr, zoom = 2.5;
  const cx = x < canvas.width / 2 ? canvas.width - r - 12 * f.dpr : r + 12 * f.dpr, cy = r + 12 * f.dpr;
  const sx = (x - f.x) / f.k, sy = (y - f.y) / f.k, sr = r / (f.k * zoom);
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.closePath();
  ctx.fillStyle = '#000'; ctx.fill(); ctx.clip();
  ctx.drawImage(ed.img, sx - sr, sy - sr, sr * 2, sr * 2, cx - r, cy - r, r * 2, r * 2);
  ctx.strokeStyle = '#4c8dff'; ctx.lineWidth = 2 * f.dpr;
  ctx.beginPath(); ctx.moveTo(cx - 10 * f.dpr, cy); ctx.lineTo(cx + 10 * f.dpr, cy); ctx.moveTo(cx, cy - 10 * f.dpr); ctx.lineTo(cx, cy + 10 * f.dpr); ctx.stroke();
  ctx.restore();
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.lineWidth = 3 * f.dpr; ctx.strokeStyle = '#fff'; ctx.stroke();
}

function pointerPos(e) {
  const b = canvas.getBoundingClientRect(), dpr = ed.fit.dpr;
  return [(e.clientX - b.left) * dpr, (e.clientY - b.top) * dpr];
}
canvas.addEventListener('pointerdown', e => {
  if (!ed.fit) return;
  const [x, y] = pointerPos(e);
  let best = -1, bestD = 48 * ed.fit.dpr;
  ed.p.corners.map(toCanvas).forEach(([cx, cy], i) => { const d = Math.hypot(cx - x, cy - y); if (d < bestD) { best = i; bestD = d; } });
  if (best < 0) return;
  ed.drag = best;
  canvas.setPointerCapture(e.pointerId);
  draw();
});
canvas.addEventListener('pointermove', e => {
  if (ed.drag < 0) return;
  const [x, y] = pointerPos(e), f = ed.fit;
  ed.p.corners[ed.drag] = [Math.min(1, Math.max(0, (x - f.x) / f.w)), Math.min(1, Math.max(0, (y - f.y) / f.h))];
  draw();
});
['pointerup', 'pointercancel'].forEach(ev => canvas.addEventListener(ev, () => { ed.drag = -1; draw(); }));
window.addEventListener('resize', () => { if (!$('#editor').hidden && ed.img) { layout(); draw(); } });

$('#edRotate').addEventListener('click', () => { ed.p.rotate = (ed.p.rotate + 1) % 4; renderFilters(); });
$('#edFull').addEventListener('click', () => { ed.p.corners = FULL.map(c => [...c]); draw(); });
$('#edAuto').addEventListener('click', () => { ed.p.corners = ed.p.detected.map(c => [...c]); draw(); });
$('#edCancel').addEventListener('click', closeEditor);
$('#edDelete').addEventListener('click', async () => {
  if (!confirm(t('s_delete_page'))) return;
  await api(`/api/scanner/pages/${ed.p.id}`, { method: 'DELETE' }).catch(() => {});
  pages.splice(ed.i, 1); savePages();
  closeEditor(); render();
});
$('#edDone').addEventListener('click', async () => {
  busy(t('s_applying'));
  try {
    await api(`/api/scanner/pages/${ed.p.id}/render`, { method: 'POST', body: JSON.stringify({ corners: ed.p.corners, filter: ed.p.filter, rotate: ed.p.rotate }) });
    pages[ed.i] = { ...ed.p, found: true, v: ed.p.v + 1 };
    savePages();
    closeEditor();
  } catch (e) { if (e.message !== 'login') toast(t('error', { msg: e.message }), 'bad'); }
  busy('');
  render();
});

// ---------- язык и старт ----------

$$('.langSel').forEach(s => s.addEventListener('change', () => {
  lang = s.value; ls.set('hd_lang', lang);
  api('/api/me', { method: 'PATCH', body: JSON.stringify({ lang }) }).catch(() => {});
  applyI18n(); render(); if (!$('#editor').hidden) renderFilters();
}));

(async function boot() {
  applyI18n();
  const st = await api('/api/auth/state').catch(() => null);
  if (!st || !st.user) { location.href = '/?next=/scan.html'; return; }
  if (['en', 'uk', 'de'].includes(st.user.lang)) { lang = st.user.lang; ls.set('hd_lang', lang); applyI18n(); }
  try {
    // незаконченные страницы после перезагрузки (на сервере живут сутки)
    pages = JSON.parse(ls.get('hd_scan_pages') || '[]').filter(p => p && /^[0-9a-f]{32}$/.test(p.id));
  } catch (e) { pages = []; }
  render();
})();
