'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ls = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* недоступно */ } },
};
const LANGS = ['en', 'uk', 'de'];
const LOCALE = { en: 'en-GB', uk: 'uk-UA', de: 'de-DE' };
const THEMES = ['system', 'light', 'dark'];
const THEME_ICON = { system: '🖥', light: '☀', dark: '🌙' };

let lang = LANGS.includes(ls.get('hd_lang')) ? ls.get('hd_lang') : 'en';
const S = {
  user: null, meta: null, people: [], docs: [], stats: null, cur: null, sel: null,
  checked: new Set(), sort: 'added_at', order: 'desc', preview: false, authSetup: false, tab: 'types',
  f: { q: '', person: '', doc_type: '', doc_status: '', status: '', due: '' },
};

// ---------- общие помощники ----------

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
  if (r.status === 401 && !url.startsWith('/api/auth/')) {
    S.user = null;
    showAuth(false);
    throw new Error(t('session_expired'));
  }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(typeof d.detail === 'string' ? d.detail : r.statusText || String(r.status));
  }
  return r.json();
}
const send = (method, data) => ({ method, body: JSON.stringify(data) });

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 9000 : 5000);
}
const fail = e => toast(t('error', { msg: e.message }), 'bad');

function labelsOf(kind) { return (S.meta && S.meta[kind === 'type' ? 'types' : 'statuses']) || []; }
function lname(kind, key) {
  const l = labelsOf(kind).find(x => x.key === key);
  return l ? (l.names[lang] || l.names.en || key) : (key || '');
}

function fmtDate(iso) {
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return esc(iso || '');
  const d = new Date(iso + 'T00:00:00');
  return isNaN(d) ? esc(iso) : esc(d.toLocaleDateString(LOCALE[lang], { day: '2-digit', month: '2-digit', year: 'numeric' }));
}
function fmtAmount(d) {
  if (d.amount == null) return '';
  try {
    return esc(new Intl.NumberFormat(LOCALE[lang], d.currency ? { style: 'currency', currency: d.currency } : { minimumFractionDigits: 2 }).format(d.amount));
  } catch (e) { return esc(d.amount + ' ' + (d.currency || '')); }
}
function daysUntil(iso) {
  const now = new Date(); now.setHours(0, 0, 0, 0);
  return Math.round((new Date(iso + 'T00:00:00') - now) / 86400000);
}
function dueHtml(d) {
  if (!d.due_date) return '';
  const open = d.doc_status === 'to_pay' || d.doc_status === 'action';
  const n = daysUntil(d.due_date);
  if (!open || isNaN(n)) return `<span class="muted">${fmtDate(d.due_date)}</span>`;
  if (n < 0) return `<span class="due-bad" title="${esc(t('overdue'))}">${fmtDate(d.due_date)} ⚠</span>`;
  const hint = n === 0 ? t('today') : t('in_days', { n });
  return `<span class="${n <= 7 ? 'due-warn' : ''}" title="${esc(hint)}">${fmtDate(d.due_date)}</span>`;
}

// ---------- тема и язык ----------

function applyTheme(th) {
  if (!THEMES.includes(th)) th = 'system';
  if (th === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = th;
  ls.set('hd_theme', th);
  $$('.themeBtn').forEach(b => { b.textContent = THEME_ICON[th]; b.title = `${t('theme')}: ${t('theme_' + th)}`; });
}
function currentTheme() { return document.documentElement.dataset.theme || 'system'; }

$$('.themeBtn').forEach(b => b.addEventListener('click', () => {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  applyTheme(next);
  if (S.user) api('/api/me', send('PATCH', { theme: next })).catch(fail);
}));

$$('.langSel').forEach(s => s.addEventListener('change', () => setLang(s.value)));
function setLang(l) {
  if (!LANGS.includes(l)) return;
  lang = l;
  ls.set('hd_lang', l);
  applyI18n();
  applyTheme(currentTheme());
  if (!S.user) { renderAuth(); return; }
  api('/api/me', send('PATCH', { lang: l })).then(u => { S.user = u; }).catch(fail);
  renderFilters(); renderStats(); renderTable(); renderDetail();
  $('#provider').textContent = S.meta ? t('ai_provider', { p: S.meta.provider }) : '';
  if ($('#settings').open) renderSettings();
}

// ---------- вход ----------

function showAuth(setup) {
  S.authSetup = setup;
  $('#app').hidden = true;
  $('#auth').hidden = false;
  if ($('#settings').open) $('#settings').close();
  renderAuth();
  $('#aUser').focus();
}
function renderAuth() {
  $('#aPass2Wrap').hidden = !S.authSetup;
  $('#aPass2').required = S.authSetup;
  $('#authHint').textContent = t(S.authSetup ? 'setup_hint' : 'login_hint');
  $('#authBtn').textContent = t(S.authSetup ? 'create_account' : 'login');
  $('#aPass').autocomplete = S.authSetup ? 'new-password' : 'current-password';
}

$('#authForm').addEventListener('submit', async e => {
  e.preventDefault();
  $('#authErr').textContent = '';
  const username = $('#aUser').value.trim(), password = $('#aPass').value;
  if (S.authSetup && password !== $('#aPass2').value) { $('#authErr').textContent = t('passwords_mismatch'); return; }
  $('#authBtn').disabled = true;
  try {
    const user = await api('/api/auth/' + (S.authSetup ? 'setup' : 'login'), send('POST', { username, password }));
    $('#aPass').value = ''; $('#aPass2').value = '';
    await enter(user, S.authSetup);
  } catch (err) {
    $('#authErr').textContent = err.message;
  } finally {
    $('#authBtn').disabled = false;
  }
});

async function enter(user, pushPrefs) {
  S.user = user;
  if (pushPrefs) S.user = await api('/api/me', send('PATCH', { lang, theme: currentTheme() }));
  lang = S.user.lang; ls.set('hd_lang', lang);
  applyTheme(S.user.theme);
  applyI18n();
  $('#auth').hidden = true;
  $('#app').hidden = false;
  $('#who').textContent = S.user.username;
  try { Object.assign(S.f, JSON.parse(ls.get('hd_filters') || '{}')); } catch (e) { /* сброшенные фильтры */ }
  $('#q').value = S.f.q || '';
  await Promise.all([loadMeta(), loadPeople()]);
  renderFilters();
  await refresh();
}

$('#logoutBtn').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
  S.user = null; S.cur = null; S.sel = null;
  showAuth(false);
});

// ---------- данные ----------

async function loadMeta() {
  S.meta = await api('/api/meta');
  $('#provider').textContent = t('ai_provider', { p: S.meta.provider });
}
async function loadPeople() {
  S.people = await api('/api/people');
  $('#peopleList').innerHTML = S.people.map(p => `<option value="${esc(p.name)}">`).join('');
}
async function loadDocs() {
  const p = new URLSearchParams({ ...S.f, sort: S.sort, order: S.order });
  S.docs = await api('/api/documents?' + p);
  const ids = new Set(S.docs.map(d => d.id));
  S.checked.forEach(id => { if (!ids.has(id)) S.checked.delete(id); });
  renderTable();
}
async function loadStats() { S.stats = await api('/api/stats'); renderStats(); }
async function refresh() { await Promise.all([loadDocs(), loadStats()]).catch(fail); }
function saveFilters() { ls.set('hd_filters', JSON.stringify(S.f)); }

// ---------- фильтры и статистика ----------

function options(list, cur, first) {
  return first + list.map(([v, label]) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(label)}</option>`).join('');
}
function renderFilters() {
  $('#fPerson').innerHTML = options(S.people.map(p => [p.name, p.name]), S.f.person, `<option value="">${esc(t('all_people'))}</option>`);
  $('#fType').innerHTML = options(labelsOf('type').map(l => [l.key, lname('type', l.key)]), S.f.doc_type, `<option value="">${esc(t('all_types'))}</option>`);
  $('#fDocStatus').innerHTML = options([...labelsOf('status').map(l => [l.key, lname('status', l.key)]), ['none', t('no_doc_status')]],
    S.f.doc_status, `<option value="">${esc(t('any_doc_status'))}</option>`);
  $('#fStatus').innerHTML = options(['analyzed', 'approved', 'error'].map(s => [s, t('st_' + s)]), S.f.status, `<option value="">${esc(t('any_state'))}</option>`);
}
const filterSel = { '#fPerson': 'person', '#fType': 'doc_type', '#fDocStatus': 'doc_status', '#fStatus': 'status' };
Object.entries(filterSel).forEach(([sel, key]) => $(sel).addEventListener('change', () => {
  S.f[key] = $(sel).value; S.f.due = ''; saveFilters(); renderStats(); loadDocs().catch(fail);
}));
let qTimer;
$('#q').addEventListener('input', () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { S.f.q = $('#q').value.trim(); saveFilters(); loadDocs().catch(fail); }, 250);
});
function resetFilters() {
  S.f = { q: '', person: '', doc_type: '', doc_status: '', status: '', due: '' };
  $('#q').value = ''; saveFilters(); renderFilters(); renderStats(); loadDocs().catch(fail);
}
$('#resetFilters').addEventListener('click', resetFilters);

const CHIPS = [
  { key: 'total', label: 'stat_total', f: {}, active: f => !f.status && !f.doc_status && !f.due },
  { key: 'review', label: 'stat_review', f: { status: 'analyzed' }, active: f => f.status === 'analyzed' && !f.doc_status, cls: 'warn' },
  { key: 'to_pay', label: 'stat_to_pay', f: { doc_status: 'to_pay' }, active: f => f.doc_status === 'to_pay' && !f.due, cls: 'warn' },
  { key: 'overdue', label: 'stat_overdue', f: { due: 'overdue' }, active: f => f.due === 'overdue', cls: 'bad' },
  { key: 'errors', label: 'stat_errors', f: { status: 'error' }, active: f => f.status === 'error', cls: 'bad' },
];
function renderStats() {
  if (!S.stats) return;
  $('#stats').innerHTML = CHIPS.map((c, i) => {
    const n = S.stats[c.key];
    if (n === 0 && ['overdue', 'errors'].includes(c.key)) return '';
    let extra = '';
    if (c.key === 'to_pay' && S.stats.to_pay_sum) {
      extra = Object.entries(S.stats.to_pay_sum).map(([cur, sum]) => fmtAmount({ amount: sum, currency: cur })).join(' + ');
      extra = extra ? ` <span class="muted">· ${extra}</span>` : '';
    }
    return `<button class="chip ${n ? c.cls || '' : ''} ${c.active(S.f) ? 'active' : ''}" data-chip="${i}">${esc(t(c.label))}<b>${n}</b>${extra}</button>`;
  }).join('');
}
$('#stats').addEventListener('click', e => {
  const b = e.target.closest('[data-chip]');
  if (!b) return;
  const c = CHIPS[+b.dataset.chip];
  Object.assign(S.f, { person: S.f.person, doc_type: S.f.doc_type, status: '', doc_status: '', due: '' }, c.f);
  saveFilters(); renderFilters(); renderStats(); loadDocs().catch(fail);
});

$('#exportBtn').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = '/api/export.csv?' + new URLSearchParams(S.f);
  a.download = 'homedocs.csv';
  document.body.append(a); a.click(); a.remove();
});

// ---------- таблица ----------

function renderTable() {
  $$('th[data-s]').forEach(th => {
    th.textContent = t(th.dataset.i18n);
    if (th.dataset.s === S.sort) th.insertAdjacentHTML('beforeend', ` <span class="arrow">${S.order === 'asc' ? '▲' : '▼'}</span>`);
  });
  const filtered = Object.values(S.f).some(Boolean);
  $('#rows').innerHTML = S.docs.map(d => `<tr class="row ${d.id === S.sel ? 'sel' : ''}" data-id="${d.id}">
    <td class="ck"><input type="checkbox" data-check="${d.id}" ${S.checked.has(d.id) ? 'checked' : ''} ${d.status !== 'analyzed' ? 'disabled' : ''}></td>
    <td><div class="t">${esc(d.title || d.filename)}</div><div class="muted small">${esc(d.sender || '')}</div></td>
    <td>${esc(d.person || '—')}</td>
    <td>${d.doc_type ? `<span class="pill">${esc(lname('type', d.doc_type))}</span>` : ''}</td>
    <td class="nowrap">${fmtDate(d.doc_date)}</td>
    <td class="nowrap num">${fmtAmount(d)}</td>
    <td class="nowrap">${dueHtml(d)}</td>
    <td>${d.doc_status ? `<span class="pill ds-${esc(d.doc_status)}">${esc(lname('status', d.doc_status))}</span>` : ''}</td>
    <td class="nowrap st-${esc(d.status)}">${esc(t('st_' + d.status))}</td></tr>`).join('')
    || `<tr><td colspan="9" class="empty">${esc(t(filtered ? 'empty_filtered' : 'empty'))}</td></tr>`;
  renderBulk();
}
function renderBulk() {
  $('#bulk').hidden = S.checked.size === 0;
  $('#bulkCount').textContent = t('selected', { n: S.checked.size });
  const reviewable = S.docs.filter(d => d.status === 'analyzed');
  $('#checkAll').checked = reviewable.length > 0 && reviewable.every(d => S.checked.has(d.id));
  $('#checkAll').disabled = reviewable.length === 0;
}

$('#rows').addEventListener('click', e => {
  const cb = e.target.closest('[data-check]');
  if (cb) {
    const id = +cb.dataset.check;
    cb.checked ? S.checked.add(id) : S.checked.delete(id);
    renderBulk();
    return;
  }
  const tr = e.target.closest('tr.row');
  if (tr) show(+tr.dataset.id);
});
$('#checkAll').addEventListener('change', () => {
  S.docs.filter(d => d.status === 'analyzed').forEach(d => $('#checkAll').checked ? S.checked.add(d.id) : S.checked.delete(d.id));
  renderTable();
});
$('#bulkClear').addEventListener('click', () => { S.checked.clear(); renderTable(); });
$('#bulkApprove').addEventListener('click', async () => {
  $('#bulkApprove').disabled = true;
  try {
    const r = await api('/api/documents/approve', send('POST', { ids: [...S.checked] }));
    toast(t('approved_n', { n: r.approved }), 'ok');
    S.checked.clear();
    await refresh();
    if (S.sel) await show(S.sel);
  } catch (e) { fail(e); }
  $('#bulkApprove').disabled = false;
});
$$('th[data-s]').forEach(th => th.addEventListener('click', () => {
  S.order = S.sort === th.dataset.s && S.order === 'desc' ? 'asc' : 'desc';
  S.sort = th.dataset.s;
  loadDocs().catch(fail);
}));

// ---------- панель документа ----------

async function show(id) {
  try {
    S.cur = await api('/api/documents/' + id);
    S.sel = id;
  } catch (e) { fail(e); return; }
  renderDetail();
  $$('#rows tr.row').forEach(tr => tr.classList.toggle('sel', +tr.dataset.id === id));
}
function closeDetail() {
  S.cur = null; S.sel = null;
  renderDetail();
  $$('#rows tr.sel').forEach(tr => tr.classList.remove('sel'));
}

function labelOptions(kind, cur, allowNone) {
  const opts = labelsOf(kind).map(l => `<option value="${esc(l.key)}" ${l.key === cur ? 'selected' : ''}>${esc(lname(kind, l.key))}${l.origin === 'ai' ? ' ✨' : ''}</option>`).join('');
  return (allowNone ? `<option value="">${esc(t('none'))}</option>` : '') + opts
    + `<option value="__new">${esc(t(kind === 'type' ? 'new_type_opt' : 'new_status_opt'))}</option>`;
}

function previewHtml(d) {
  const url = `/api/documents/${d.id}/file`;
  const ext = (d.filename.split('.').pop() || '').toLowerCase();
  if (['png', 'jpg', 'jpeg', 'webp'].includes(ext)) return `<img class="preview" src="${url}" alt="">`;
  return `<iframe class="preview" src="${url}" title="${esc(d.filename)}"></iframe>`;
}

function renderDetail() {
  const d = S.cur;
  $('#detail').hidden = !d;
  $('#main').classList.toggle('with-detail', !!d);
  if (!d) return;
  const v = x => esc(x ?? '');
  $('#detail').innerHTML = `
    <div class="d-head">
      <div><b>${esc(d.filename)}</b> <span class="small st-${esc(d.status)}">· ${esc(t('st_' + d.status))}</span></div>
      <button class="icon" data-act="close" title="${esc(t('close'))}" aria-label="${esc(t('close'))}">✕</button>
    </div>
    ${d.summary ? `<p class="muted" style="margin:6px 0">${esc(d.summary)}</p>` : ''}
    ${d.error ? `<div class="alert bad">${esc(d.error)}</div>` : ''}
    ${d.confidence != null && d.confidence < 0.6 && d.status === 'analyzed' ? `<div class="alert warn">${esc(t('low_conf'))}</div>` : ''}
    ${S.preview ? previewHtml(d) : ''}
    <form id="dForm" class="fields" autocomplete="off">
      <label class="full">${esc(t('f_filename'))}<input name="suggested_name" value="${v(d.suggested_name)}"></label>
      <label class="full">${esc(t('f_title'))}<span class="inrow"><input name="title" value="${v(d.title)}"><button type="button" class="small" data-act="translit" title="${esc(t('to_latin_title'))}">${esc(t('to_latin'))}</button></span></label>
      <label>${esc(t('f_person'))}<input name="person" list="peopleList" value="${v(d.person)}"></label>
      <label>${esc(t('f_sender'))}<input name="sender" value="${v(d.sender)}"></label>
      <label>${esc(t('f_type'))}<select name="doc_type" data-kind="type">${labelOptions('type', d.doc_type, false)}</select></label>
      <label>${esc(t('f_doc_status'))}<select name="doc_status" data-kind="status">${labelOptions('status', d.doc_status, true)}</select></label>
      <label>${esc(t('f_date'))}<input type="date" name="doc_date" value="${v(d.doc_date)}"></label>
      <label>${esc(t('f_due'))}<input type="date" name="due_date" value="${v(d.due_date)}"></label>
      <label>${esc(t('f_amount'))}<input type="number" step="0.01" name="amount" value="${v(d.amount)}"></label>
      <label>${esc(t('f_currency'))}<input name="currency" maxlength="3" value="${v(d.currency)}"></label>
      <label class="full">${esc(t('f_tags'))}<input name="tags" value="${v((d.tags || []).join(', '))}"></label>
      <label class="full">${esc(t('f_summary'))}<textarea name="summary" rows="3">${v(d.summary)}</textarea></label>
    </form>
    ${d.archive_path ? `<div class="muted small" style="margin-top:8px;word-break:break-all">${esc(t('archived_at', { path: d.archive_path }))}</div>` : ''}
    <div class="actions">
      <button class="primary" data-act="approve" ${d.status === 'approved' ? 'disabled' : ''}>${esc(t('approve'))}</button>
      <button data-act="save">${esc(t('save'))}</button>
      <button data-act="preview">${esc(t(S.preview ? 'hide_preview' : 'preview'))}</button>
      <button data-act="open">${esc(t('open_file'))}</button>
      <button data-act="download">${esc(t('download'))}</button>
      <button data-act="reanalyze">${esc(t('reanalyze'))}</button>
      <button class="danger" data-act="remove">${esc(t('remove'))}</button>
    </div>`;
}

function formData() {
  const f = $('#dForm');
  const g = n => f.elements[n].value.trim();
  const amount = g('amount');
  return {
    suggested_name: g('suggested_name'), title: g('title'), person: g('person'), sender: g('sender'),
    doc_type: g('doc_type'), doc_status: g('doc_status') || null, doc_date: g('doc_date') || null,
    due_date: g('due_date') || null, amount: amount === '' ? null : Number(amount), currency: g('currency').toUpperCase() || null,
    tags: g('tags').split(',').map(s => s.trim()).filter(Boolean), summary: g('summary'),
  };
}
async function save(quiet) {
  S.cur = await api('/api/documents/' + S.cur.id, send('PATCH', formData()));
  if (!quiet) toast(t('saved'), 'ok');
}
function nextToReview(afterId) {
  const i = S.docs.findIndex(d => d.id === afterId);
  return [...S.docs.slice(i + 1), ...S.docs.slice(0, Math.max(i, 0))].find(d => d.status === 'analyzed' && d.id !== afterId);
}
async function approveCurrent() {
  const id = S.cur.id;
  await save(true);
  const r = await api(`/api/documents/${id}/approve`, { method: 'POST' });
  toast(t('filed_to', { path: r.archive_path }), 'ok');
  await Promise.all([refresh(), loadPeople()]);
  const next = nextToReview(id);
  await show(next ? next.id : id);
}

$('#detail').addEventListener('click', async e => {
  const b = e.target.closest('[data-act]');
  if (!b || !S.cur) return;
  const id = S.cur.id, act = b.dataset.act;
  try {
    if (act === 'close') closeDetail();
    else if (act === 'save') { await save(); await Promise.all([loadDocs(), loadStats(), loadPeople()]); renderDetail(); }
    else if (act === 'approve') { b.disabled = true; await approveCurrent(); }
    else if (act === 'preview') { S.preview = !S.preview; ls.set('hd_preview', S.preview ? '1' : ''); renderDetail(); }
    else if (act === 'open') window.open(`/api/documents/${id}/file`, '_blank', 'noopener');
    else if (act === 'download') { const a = document.createElement('a'); a.href = `/api/documents/${id}/file?download=1`; document.body.append(a); a.click(); a.remove(); }
    else if (act === 'translit') {
      const inp = $('#dForm').elements.title;
      inp.value = (await api('/api/translit?' + new URLSearchParams({ text: inp.value }))).text;
    } else if (act === 'reanalyze') {
      b.disabled = true; toast(t('analysing'));
      const r = await api(`/api/documents/${id}/reanalyze`, { method: 'POST' });
      if (r.new_types.length || r.new_statuses.length) await loadMeta();
      announceNew(r);
      await refresh(); await show(id);
    } else if (act === 'remove') {
      if (!confirm(t('confirm_remove'))) return;
      await api('/api/documents/' + id, { method: 'DELETE' });
      toast(t('removed'));
      closeDetail(); await refresh();
    }
  } catch (err) { fail(err); b.disabled = false; }
});

$('#detail').addEventListener('change', async e => {
  const sel = e.target.closest('select[data-kind]');
  if (!sel || sel.value !== '__new') return;
  const kind = sel.dataset.kind;
  const name = (prompt(t(kind === 'type' ? 'prompt_new_type' : 'prompt_new_status')) || '').trim();
  const prev = kind === 'type' ? S.cur.doc_type : (S.cur.doc_status || '');
  if (!name) { sel.value = prev; return; }
  try {
    const r = await api('/api/labels/' + kind, send('POST', { names: { [lang]: name } }));
    S.meta = { ...S.meta, types: r.types, statuses: r.statuses };
    sel.innerHTML = labelOptions(kind, r.key, kind === 'status');
    sel.value = r.key;
    renderFilters();
  } catch (err) { fail(err); sel.value = prev; }
});

function announceNew(r) {
  const names = (kind, keys) => keys.map(k => lname(kind, k)).join(', ');
  if (r.new_people && r.new_people.length) toast(t('new_people', { list: r.new_people.join(', ') }));
  if (r.new_types && r.new_types.length) toast(t('new_types', { list: names('type', r.new_types) }));
  if (r.new_statuses && r.new_statuses.length) toast(t('new_statuses', { list: names('status', r.new_statuses) }));
}

// ---------- сканирование и загрузка ----------

async function scan() {
  const btn = $('#scan');
  btn.disabled = true;
  toast(t('scanning'));
  try {
    const r = await api('/api/scan', { method: 'POST' });
    if (r.new_types.length || r.new_statuses.length) await loadMeta();
    toast(t('scan_result', r), r.errors ? 'bad' : 'ok');
    announceNew(r);
    await Promise.all([loadPeople(), refresh()]);
    renderFilters();
  } catch (e) { fail(e); }
  btn.disabled = false;
}
$('#scan').addEventListener('click', scan);

async function upload(files) {
  files = [...files];
  if (!files.length) return;
  toast(t('uploading', { n: files.length }));
  let ok = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append('file', f);
    try { await api('/api/upload', { method: 'POST', body: fd }); ok++; } catch (e) { toast(`${f.name}: ${e.message}`, 'bad'); }
  }
  if (ok) { toast(t('uploaded', { n: ok })); await scan(); }
}
$('#uploadBtn').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', () => { upload($('#fileInput').files); $('#fileInput').value = ''; });

let dragDepth = 0;
const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
window.addEventListener('dragenter', e => { if (!S.user || !hasFiles(e)) return; dragDepth++; $('#drop').hidden = false; });
window.addEventListener('dragleave', e => { if (!hasFiles(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('#drop').hidden = true; });
window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener('drop', e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $('#drop').hidden = true;
  if (S.user) upload(e.dataTransfer.files);
});

// ---------- клавиатура ----------

document.addEventListener('keydown', e => {
  if (!S.user || $('#settings').open) return;
  const typing = e.target.matches('input, textarea, select');
  if (e.key === 'Escape') {
    if (typing) e.target.blur();
    else if (S.cur) closeDetail();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && S.cur && e.key.toLowerCase() === 's') {
    e.preventDefault();
    save().then(() => Promise.all([loadDocs(), loadStats()])).catch(fail);
    return;
  }
  if ((e.ctrlKey || e.metaKey) && S.cur && e.key === 'Enter') {
    e.preventDefault();
    if (S.cur.status !== 'approved') approveCurrent().catch(fail);
    return;
  }
  if (typing) return;
  if (e.key === '/') { e.preventDefault(); $('#q').focus(); return; }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!S.docs.length) return;
    e.preventDefault();
    const i = S.docs.findIndex(d => d.id === S.sel);
    const j = i < 0 ? 0 : Math.min(S.docs.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)));
    show(S.docs[j].id).then(() => { const tr = $(`#rows tr[data-id="${S.docs[j].id}"]`); if (tr) tr.scrollIntoView({ block: 'nearest' }); });
  }
});

// ---------- настройки ----------

const TABS = ['types', 'statuses', 'people', 'account', 'users'];
$('#settingsBtn').addEventListener('click', () => { renderSettings(); $('#settings').showModal(); });
$('#settingsClose').addEventListener('click', () => $('#settings').close());
$('#settings').addEventListener('close', async () => {
  await Promise.all([loadMeta(), loadPeople()]).catch(fail);
  renderFilters(); renderDetail(); refresh();
});
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('[data-tab]');
  if (b) { S.tab = b.dataset.tab; renderSettings(); }
});

async function renderSettings() {
  const tabs = TABS.filter(x => x !== 'users' || S.user.is_admin);
  if (!tabs.includes(S.tab)) S.tab = 'types';
  const title = { types: 's_types', statuses: 's_statuses', people: 's_people', account: 's_account', users: 's_users' };
  $('#tabs').innerHTML = tabs.map(x => `<button data-tab="${x}" class="${x === S.tab ? 'on' : ''}">${esc(t(title[x]))}</button>`).join('');
  const body = $('#settingsBody');
  try {
    if (S.tab === 'types' || S.tab === 'statuses') { await loadMeta(); body.innerHTML = labelsHtml(S.tab === 'types' ? 'type' : 'status'); }
    else if (S.tab === 'people') { await loadPeople(); body.innerHTML = peopleHtml(); }
    else if (S.tab === 'account') body.innerHTML = accountHtml();
    else if (S.tab === 'users') body.innerHTML = usersHtml(await api('/api/users'));
  } catch (e) { fail(e); }
}

function labelsHtml(kind) {
  const rows = labelsOf(kind).map(l => `<tr data-key="${esc(l.key)}">
    ${LANGS.map(x => `<td><input data-l="${x}" value="${esc(l.names[x] || '')}"></td>`).join('')}
    <td class="nowrap">${l.count}${l.origin === 'ai' ? ` <span class="ai" title="${esc(t('ai_label_hint'))}">${esc(t('ai_badge'))}</span>` : ''}</td>
    <td class="nowrap"><button class="small" data-sact="label-save">${esc(t('save'))}</button>
      ${kind === 'type' && l.key === 'other' ? '' : `<button class="small danger" data-sact="label-del">${esc(t('delete'))}</button>`}</td></tr>`).join('');
  return `<p class="muted small" style="margin-top:0">${esc(t('labels_hint'))}</p>
    <div class="addrow" data-kind="${kind}">
      ${LANGS.map(x => `<input data-new="${x}" placeholder="${esc(t('name_' + x))}">`).join('')}
      <button class="primary" data-sact="label-add">${esc(t('add'))}</button>
    </div>
    <div class="table-wrap"><table data-kind="${kind}">
      <thead><tr>${LANGS.map(x => `<th>${esc(t('name_' + x))}</th>`).join('')}<th>${esc(t('docs'))}</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

function peopleHtml() {
  const rows = S.people.map(p => `<tr data-pid="${p.id}">
    <td><input data-f="name" value="${esc(p.name)}"></td>
    <td><input data-f="aliases" value="${esc(p.aliases.join(', '))}"></td>
    <td>${p.documents}</td>
    <td class="nowrap"><button class="small" data-sact="person-save">${esc(t('save'))}</button>
      <select class="small" data-f="merge"><option value="">${esc(t('merge_into'))}</option>${S.people.filter(o => o.id !== p.id).map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join('')}</select></td></tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr><th>${esc(t('name'))}</th><th>${esc(t('aliases'))}</th><th>${esc(t('docs'))}</th><th></th></tr></thead>
    <tbody>${rows || `<tr><td colspan="4" class="empty">—</td></tr>`}</tbody></table></div>`;
}

function accountHtml() {
  return `<p class="muted">${esc(t('logged_in_as', { name: S.user.username }))}</p>
    <form class="form-col" id="pwForm">
      <b>${esc(t('change_password'))}</b>
      <input type="text" autocomplete="username" value="${esc(S.user.username)}" hidden>
      <input type="password" name="old" autocomplete="current-password" placeholder="${esc(t('current_password'))}" required>
      <input type="password" name="new" autocomplete="new-password" placeholder="${esc(t('new_password'))}" minlength="8" required>
      <input type="password" name="new2" autocomplete="new-password" placeholder="${esc(t('password_repeat'))}" minlength="8" required>
      <button class="primary" type="submit">${esc(t('change_password'))}</button>
    </form>`;
}

function usersHtml(users) {
  const rows = users.map(u => `<tr><td>${esc(u.username)}${u.id === S.user.id ? ` <span class="muted">(${esc(t('you'))})</span>` : ''}</td>
    <td>${u.is_admin ? esc(t('admin')) : ''}</td>
    <td>${u.id === S.user.id ? '' : `<button class="small danger" data-sact="user-del" data-uid="${u.id}" data-name="${esc(u.username)}">${esc(t('delete'))}</button>`}</td></tr>`).join('');
  return `<p class="muted small" style="margin-top:0">${esc(t('users_hint'))}</p>
    <form class="addrow" id="userForm">
      <input name="username" placeholder="${esc(t('username'))}" required maxlength="40" autocomplete="off">
      <input name="password" type="password" placeholder="${esc(t('new_password'))}" minlength="8" required autocomplete="new-password">
      <label class="nowrap"><input type="checkbox" name="is_admin"> ${esc(t('admin'))}</label>
      <button class="primary" type="submit">${esc(t('add_user'))}</button>
    </form>
    <div class="table-wrap"><table><tbody>${rows}</tbody></table></div>`;
}

$('#settingsBody').addEventListener('click', async e => {
  const b = e.target.closest('[data-sact]');
  if (!b) return;
  const act = b.dataset.sact;
  try {
    if (act === 'label-add') {
      const row = b.closest('.addrow'), kind = row.dataset.kind;
      const names = Object.fromEntries($$('[data-new]', row).map(i => [i.dataset.new, i.value.trim()]).filter(([, v]) => v));
      if (!Object.keys(names).length) return;
      const r = await api('/api/labels/' + kind, send('POST', { names }));
      S.meta = { ...S.meta, types: r.types, statuses: r.statuses };
      renderSettings();
    } else if (act === 'label-save' || act === 'label-del') {
      const tr = b.closest('tr'), kind = b.closest('table').dataset.kind, key = tr.dataset.key;
      if (act === 'label-save') {
        const names = Object.fromEntries($$('[data-l]', tr).map(i => [i.dataset.l, i.value]));
        S.meta = await api(`/api/labels/${kind}/${encodeURIComponent(key)}`, send('PATCH', { names }));
        toast(t('saved'), 'ok');
      } else {
        const msg = kind === 'type' ? t('confirm_delete_type', { name: lname(kind, key), target: lname('type', 'other') })
          : t('confirm_delete_status', { name: lname(kind, key) });
        if (!confirm(msg)) return;
        S.meta = await api(`/api/labels/${kind}/${encodeURIComponent(key)}`, { method: 'DELETE' });
      }
      renderSettings();
    } else if (act === 'person-save') {
      const tr = b.closest('tr'), pid = tr.dataset.pid;
      S.people = await api('/api/people/' + pid, send('PATCH', {
        name: $('[data-f="name"]', tr).value, aliases: $('[data-f="aliases"]', tr).value.split(',').map(s => s.trim()).filter(Boolean),
      }));
      toast(t('saved'), 'ok');
      renderSettings();
    } else if (act === 'user-del') {
      if (!confirm(t('confirm_delete_user', { name: b.dataset.name }))) return;
      $('#settingsBody').innerHTML = usersHtml(await api('/api/users/' + b.dataset.uid, { method: 'DELETE' }));
    }
  } catch (err) { fail(err); }
});

$('#settingsBody').addEventListener('change', async e => {
  const sel = e.target.closest('[data-f="merge"]');
  if (!sel || !sel.value) return;
  const tr = sel.closest('tr');
  const a = $('[data-f="name"]', tr).value, b = sel.options[sel.selectedIndex].text;
  if (!confirm(t('confirm_merge', { a, b }))) { sel.value = ''; return; }
  try {
    S.people = await api('/api/people/' + tr.dataset.pid, send('PATCH', { merge_into: +sel.value }));
    renderSettings();
  } catch (err) { fail(err); }
});

$('#settingsBody').addEventListener('submit', async e => {
  e.preventDefault();
  const f = e.target;
  try {
    if (f.id === 'pwForm') {
      if (f.elements.new.value !== f.elements.new2.value) { toast(t('passwords_mismatch'), 'bad'); return; }
      await api('/api/me/password', send('POST', { old: f.elements.old.value, new: f.elements.new.value }));
      f.reset();
      toast(t('password_changed'), 'ok');
    } else if (f.id === 'userForm') {
      const users = await api('/api/users', send('POST', {
        username: f.elements.username.value, password: f.elements.password.value, is_admin: f.elements.is_admin.checked,
      }));
      $('#settingsBody').innerHTML = usersHtml(users);
    }
  } catch (err) { fail(err); }
});

// ---------- старт ----------

(async function boot() {
  S.preview = ls.get('hd_preview') === '1';
  applyI18n();
  applyTheme(ls.get('hd_theme') || 'system');
  try {
    const st = await api('/api/auth/state');
    if (st.user) await enter(st.user, false);
    else showAuth(st.setup_needed);
  } catch (e) {
    showAuth(false);
    $('#authErr').textContent = e.message;
  }
})();
