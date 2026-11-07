/* Verification history: every order verified (or started) on the Verify
   page, with its scans; and the wrong-barcode queue (admins and Verify
   override users). Packers see only their own orders of today. */
const STATUS_LABEL = { verified: 'Verified', in_progress: 'In progress', reset: 'Started over' };
const EVENT_LABEL = {
  start: 'Opened', scan: 'Scan', count: 'Typed count', check_all: 'Check all', confirm: 'Confirmed (no barcode)',
  force_line: 'Marked without scanning', force_order: 'Whole order marked without scanning', undo: 'Undo',
  reset: 'Started over', complete: 'Order verified', lines_changed: 'Order changed', reopened: 'Needs verifying again',
};
const SCAN_LABEL = {
  ok: '', line_done: 'item complete', order_done: 'order complete', wrong: 'not on the order',
  over: 'already complete', case_over: 'case bigger than what was left',
};
const STATUS_OPTIONS = [
  { value: 'verified', label: 'Verified' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'reset', label: 'Started over' },
];
const FLAG_OPTIONS = [
  { value: 'forced', label: 'Marked without scanning' },
  { value: 'errors', label: 'Has wrong scans' },
  { value: 'writeback_failed', label: 'Writeback failed' },
];
const RANGES = ['today', 'yesterday', '7', '30', 'all', 'custom'];
const PAGE_SIZES = [25, 50, 100, 200];
const DESC_FIRST = new Set(['units', 'errors', 'verified', 'started']);
const FILTER_LABEL = { status: 'Status', source: 'Source', user: 'User', flags: 'Issues' };

let rows = [];
let canSeeAll = false;
let activeTab = 'history';
let filterTimer = null;
let requestSeq = 0;
const options = { status: STATUS_OPTIONS, source: [], user: [], flags: FLAG_OPTIONS };

/* Everything the list shows is one state object, mirrored into the URL so a
   refresh, Back or a shared link shows the same page. */
const state = {
  q: '',
  range: 'all',
  from: '',
  to: '',
  filters: { status: new Set(), source: new Set(), user: new Set(), flags: new Set() },
  sort: 'started',
  dir: 'desc',
  page: 1,
  size: 50,
  total: 0,
};

function storedPageSize(key = 'verifications.pageSize') {
  try {
    const n = Number(localStorage.getItem(key));
    return PAGE_SIZES.includes(n) ? n : 50;
  } catch {
    return 50;
  }
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = p.get('q') || '';
  state.range = RANGES.includes(p.get('range')) ? p.get('range') : 'all';
  state.from = p.get('from') || '';
  state.to = p.get('to') || '';
  Object.keys(state.filters).forEach((key) => {
    state.filters[key] = new Set((p.get(key) || '').split(',').filter(Boolean));
  });
  const sort = p.get('sort');
  if (document.querySelector(`[data-panel="history"] th.sortable[data-sort="${CSS.escape(sort || '')}"]`)) {
    state.sort = sort;
    state.dir = p.get('dir') === 'asc' ? 'asc' : 'desc';
  }
  state.page = Math.max(1, Number(p.get('page')) || 1);
  state.size = storedPageSize();
  readWrongUrl(p);
  activeTab = p.get('tab') === 'wrong' ? 'wrong' : 'history';
}

function writeUrl() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  if (state.range !== 'all') p.set('range', state.range);
  if (state.range === 'custom') {
    if (state.from) p.set('from', state.from);
    if (state.to) p.set('to', state.to);
  }
  Object.entries(state.filters).forEach(([key, set]) => { if (set.size) p.set(key, [...set].join(',')); });
  if (state.sort !== 'started' || state.dir !== 'desc') { p.set('sort', state.sort); p.set('dir', state.dir); }
  if (state.page > 1) p.set('page', state.page);
  if (canSeeAll) writeWrongUrl(p);
  if (activeTab === 'wrong') p.set('tab', 'wrong');
  const qs = p.toString();
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}

/* Dates are Central, like every timestamp on the page. */
function centralDay(offsetDays = 0) {
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Chicago' }).format(new Date());
  const d = new Date(`${today}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() - offsetDays);
  return d.toISOString().slice(0, 10);
}

function rangeDates(s = state) {
  switch (s.range) {
    case 'today': return { from: centralDay() };
    case 'yesterday': return { from: centralDay(1), to: centralDay(1) };
    case '7': return { from: centralDay(6) };
    case '30': return { from: centralDay(29) };
    case 'custom': return { from: s.from, to: s.to };
    default: return {};
  }
}

function apiParams() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  if (canSeeAll) {
    const { from, to } = rangeDates();
    if (from) p.set('from', from);
    if (to) p.set('to', to);
  }
  Object.entries(state.filters).forEach(([key, set]) => { if (set.size) p.set(key, [...set].join(',')); });
  p.set('sort', state.sort);
  p.set('dir', state.dir);
  p.set('limit', state.size);
  p.set('offset', (state.page - 1) * state.size);
  return p;
}

function hasFilters() {
  return Boolean(state.q) || (canSeeAll && state.range !== 'all')
    || Object.values(state.filters).some((set) => set.size);
}

init();

async function init() {
  await initNav('verify');
  readUrl();
  try {
    const o = await api('/api/verify/history/options');
    canSeeAll = o.can_see_all;
    options.source = o.sources;
    options.user = o.users.map((u) => ({ value: String(u.id), label: u.username }));
  } catch (err) {
    snackbar(err.message, 'error');
  }
  if (!canSeeAll) {
    state.filters.user.clear();
    state.range = 'all';
  }
  document.querySelector('.vh-fbtn[data-filter="user"]').hidden = !canSeeAll;
  document.getElementById('vh-range').hidden = !canSeeAll;
  document.getElementById('vh-wrong-tab').hidden = !canSeeAll;
  document.getElementById('vh-sub').textContent = canSeeAll
    ? 'Every order scanned on the Verify page'
    : 'Your orders verified today';
  document.getElementById('vh-q').value = state.q;
  document.getElementById('vh-page-size').value = String(state.size);
  syncControls();
  if (canSeeAll) {
    document.getElementById('vw-q').value = wstate.q;
    document.getElementById('vw-page-size').value = String(wstate.size);
    syncWrongControls();
    loadWrong();
  } else {
    activeTab = 'history';
  }
  showTab(activeTab);
  load();
}

/* Any change to what is looked at starts over from page 1; load() alone
   keeps the page (pager, Refresh after a retry). */
function refetch() {
  state.page = 1;
  syncControls();
  load();
}

async function load() {
  writeUrl();
  const seq = ++requestSeq;
  document.querySelector('.vh-card').classList.add('loading');
  let data;
  try {
    data = await api(`/api/verify/history?${apiParams()}`);
  } catch (err) {
    if (seq === requestSeq) {
      document.querySelector('.vh-card').classList.remove('loading');
      snackbar(err.message, 'error');
    }
    return;
  }
  if (seq !== requestSeq) return;
  document.querySelector('.vh-card').classList.remove('loading');
  rows = data.rows;
  state.total = data.total;
  state.page = Math.floor(data.offset / state.size) + 1;
  writeUrl();
  renderRows();
  renderStats(data.stats || {});
  renderPager(data.offset);
}

function ct(iso) {
  return iso ? new Date(iso).toLocaleString('en-US', {
    timeZone: 'America/Chicago', month: '2-digit', day: '2-digit', year: 'numeric', hour: 'numeric', minute: '2-digit',
  }) : '';
}

/* ---------- controls ---------- */

function paintFilterButton(btn, label, set, opts) {
  btn.classList.toggle('active', set.size > 0);
  if (!set.size) {
    btn.innerHTML = `<svg class="vh-fplus" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>${label}`;
    return;
  }
  const only = opts.find((x) => x.value === [...set][0]);
  const value = set.size === 1 ? esc(only ? only.label : [...set][0]) : `${set.size} selected`;
  btn.innerHTML = `${label}<span class="vh-fsep"></span><b>${value}</b>`
    + `<span class="vh-fx" role="button" aria-label="Clear ${label}" title="Clear">×</span>`;
}

function syncControls() {
  document.querySelectorAll('[data-panel="history"] .vh-fbtn').forEach((btn) => {
    const key = btn.dataset.filter;
    paintFilterButton(btn, FILTER_LABEL[key], state.filters[key], options[key]);
  });
  document.querySelectorAll('#vh-range button').forEach((b) => {
    b.classList.toggle('active', b.dataset.range === state.range);
    b.setAttribute('aria-pressed', b.dataset.range === state.range);
  });
  document.getElementById('vh-custom').hidden = !canSeeAll || state.range !== 'custom';
  document.getElementById('vh-from').value = state.from;
  document.getElementById('vh-to').value = state.to;
  document.getElementById('vh-clear').hidden = !hasFilters();
  document.querySelectorAll('[data-panel="history"] th.sortable').forEach((th) => {
    th.classList.toggle('asc', th.dataset.sort === state.sort && state.dir === 'asc');
    th.classList.toggle('desc', th.dataset.sort === state.sort && state.dir === 'desc');
    th.setAttribute('aria-sort', th.dataset.sort !== state.sort ? 'none' : state.dir === 'asc' ? 'ascending' : 'descending');
  });
  const sortSel = document.getElementById('vh-sort-m');
  const sortValue = `${state.sort}:${state.dir}`;
  if (![...sortSel.options].some((o) => o.value === sortValue)) {
    sortSel.querySelector('[data-custom]')?.remove();
    sortSel.insertAdjacentHTML('beforeend', `<option data-custom value="${esc(sortValue)}">${esc(document.querySelector(`[data-panel="history"] th[data-sort="${state.sort}"]`).textContent)} (${state.dir === 'asc' ? '↑' : '↓'})</option>`);
  }
  sortSel.value = sortValue;
}

let openMenu = null;
function closeMenu() {
  if (!openMenu) return;
  openMenu.menu.remove();
  openMenu.btn.classList.remove('open');
  openMenu = null;
}

function renderMenu(menu, set, opts) {
  let group = null;
  const items = opts.map((o) => {
    const head = o.group && o.group !== group ? `<div class="vh-menu-group">${esc(o.group)}</div>` : '';
    group = o.group || group;
    return `${head}<button type="button" class="filter-menu-item${set.has(o.value) ? ' checked' : ''}" data-v="${esc(o.value)}"><span class="fm-check"></span><span>${esc(o.label)}</span></button>`;
  }).join('');
  const search = opts.length > 8 ? '<input type="search" class="vh-menu-search" placeholder="Find…" aria-label="Find">' : '';
  menu.innerHTML = `${search}<div class="vh-menu-items">${items || '<div class="vh-menu-group">Nothing yet</div>'}</div>
    ${set.size ? '<div class="row-menu-sep"></div><button type="button" class="filter-menu-item vh-menu-clear" data-clear="1">Clear</button>' : ''}`;
}

function openFilterMenu(btn, set, opts, onChange) {
  if (openMenu && openMenu.btn === btn) { closeMenu(); return; }
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'row-menu filter-menu vh-menu';
  menu.setAttribute('role', 'menu');
  renderMenu(menu, set, opts);
  menu.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const item = ev.target.closest('.filter-menu-item');
    if (!item) return;
    if (item.dataset.clear) set.clear();
    else if (set.has(item.dataset.v)) set.delete(item.dataset.v);
    else set.add(item.dataset.v);
    const find = menu.querySelector('.vh-menu-search');
    const term = find ? find.value : '';
    renderMenu(menu, set, opts);
    const again = menu.querySelector('.vh-menu-search');
    if (again) { again.value = term; filterMenuItems(menu, term); }
    onChange();
  });
  menu.addEventListener('input', (ev) => {
    if (ev.target.classList.contains('vh-menu-search')) filterMenuItems(menu, ev.target.value);
  });
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8))}px`;
  let top = r.bottom + 4;
  if (top + menu.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - menu.offsetHeight - 4);
  menu.style.top = `${top}px`;
  btn.classList.add('open');
  openMenu = { btn, menu };
  menu.querySelector('.vh-menu-search')?.focus();
}

function filterMenuItems(menu, term) {
  const t = term.trim().toLowerCase();
  menu.querySelectorAll('.vh-menu-items .filter-menu-item').forEach((item) => {
    item.hidden = Boolean(t) && !item.textContent.toLowerCase().includes(t);
  });
}

document.querySelector('.vh-filterbar').addEventListener('click', (e) => {
  const btn = e.target.closest('.vh-fbtn');
  if (!btn) return;
  e.stopPropagation();
  const set = state.filters[btn.dataset.filter];
  if (e.target.closest('.vh-fx')) {
    closeMenu();
    set.clear();
    refetch();
    return;
  }
  openFilterMenu(btn, set, options[btn.dataset.filter], refetch);
});
document.addEventListener('click', (e) => {
  if (openMenu && !openMenu.menu.contains(e.target)) closeMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && openMenu) { openMenu.btn.focus(); closeMenu(); }
  else if (e.key === 'Escape' && activeTab === 'wrong' && selected.size
    && !document.getElementById('modal-backdrop').classList.contains('show')) clearSelection();
  else if (e.key === '/' && !e.target.closest('input, select, textarea')) {
    e.preventDefault();
    document.getElementById(activeTab === 'wrong' ? 'vw-q' : 'vh-q').focus();
  }
});
window.addEventListener('resize', closeMenu);
window.addEventListener('scroll', (e) => {
  if (openMenu && !openMenu.menu.contains(e.target)) closeMenu();
}, true);

document.getElementById('vh-range').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-range]');
  if (!b) return;
  state.range = b.dataset.range;
  if (state.range === 'custom' && !state.from && !state.to) {
    state.from = centralDay(6);
    state.to = centralDay();
  }
  refetch();
});
['vh-from', 'vh-to'].forEach((id) => {
  document.getElementById(id).addEventListener('change', (e) => {
    state[id === 'vh-from' ? 'from' : 'to'] = e.target.value;
    refetch();
  });
});
document.getElementById('vh-q').addEventListener('input', (e) => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    refetch();
  }, 300);
});
document.getElementById('vh-clear').addEventListener('click', clearFilters);

function clearFilters() {
  state.q = '';
  document.getElementById('vh-q').value = '';
  state.range = 'all';
  Object.values(state.filters).forEach((set) => set.clear());
  refetch();
}

document.querySelectorAll('[data-panel="history"] th.sortable').forEach((th) => {
  th.tabIndex = 0;
  const sort = () => {
    const key = th.dataset.sort;
    if (state.sort === key) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
    else { state.sort = key; state.dir = DESC_FIRST.has(key) ? 'desc' : 'asc'; }
    refetch();
  };
  th.addEventListener('click', sort);
  th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
});
document.getElementById('vh-sort-m').addEventListener('change', (e) => {
  [state.sort, state.dir] = e.target.value.split(':');
  refetch();
});

document.getElementById('vh-page-size').addEventListener('change', (e) => {
  state.size = Number(e.target.value);
  try { localStorage.setItem('verifications.pageSize', String(state.size)); } catch { /* per-browser only */ }
  refetch();
});
const lastPage = () => Math.max(1, Math.ceil(state.total / state.size));
const goPage = (n) => {
  const next = Math.min(Math.max(1, n), lastPage());
  if (next === state.page) return;
  state.page = next;
  load();
  document.querySelector('.vh-card').scrollIntoView({ block: 'start', behavior: 'smooth' });
};
document.getElementById('vh-first').addEventListener('click', () => goPage(1));
document.getElementById('vh-prev').addEventListener('click', () => goPage(state.page - 1));
document.getElementById('vh-next').addEventListener('click', () => goPage(state.page + 1));
document.getElementById('vh-last').addEventListener('click', () => goPage(lastPage()));

/* ---------- render ---------- */

function statusPill(status) {
  return `<span class="vh-pill ${status}">${STATUS_LABEL[status] || esc(status)}</span>`;
}

/* The stat cards count every match, not just this page, and double as
   one-click filters. */
const STAT_CARDS = [
  { key: 'verified', label: 'Verified', filter: 'status', value: 'verified' },
  { key: 'in_progress', label: 'In progress', filter: 'status', value: 'in_progress' },
  { key: 'forced', label: 'Marked without scanning', filter: 'flags', value: 'forced' },
  { key: 'errors', label: 'Wrong scans', filter: 'flags', value: 'errors' },
];

function renderStats(stats) {
  document.getElementById('vh-stats').innerHTML = STAT_CARDS.map((c) => {
    const n = stats[c.key] || 0;
    const on = state.filters[c.filter].has(c.value);
    return `<button type="button" class="vh-stat ${c.key}${n ? '' : ' zero'}${on ? ' on' : ''}" data-stat="${c.key}" aria-pressed="${on}"
      title="${on ? 'Remove this filter' : 'Show only these'}"><b>${n.toLocaleString()}</b><span>${c.label}</span></button>`;
  }).join('');
}

document.getElementById('vh-stats').addEventListener('click', (e) => {
  const card = e.target.closest('.vh-stat');
  if (!card) return;
  const c = STAT_CARDS.find((x) => x.key === card.dataset.stat);
  const set = state.filters[c.filter];
  if (set.has(c.value)) set.delete(c.value);
  else {
    if (c.filter === 'status') set.clear();
    set.add(c.value);
  }
  refetch();
});

function writebackCell(r) {
  const wbState = r.source === 'shopify' ? r.shopify_tag_state : r.dbadmin_state;
  const label = r.source === 'shopify' ? 'Shopify tag' : 'DB_Admin';
  if (wbState === 'done') return `<span class="chip static ok">✓ ${label}</span>`;
  if (wbState === 'failed') return `<span class="chip static err">✕ ${label}</span>`;
  if (wbState === 'pending') return `<span class="chip static">${label}…</span>`;
  return '';
}

function renderRows() {
  const body = document.getElementById('vh-body');
  const empty = document.getElementById('vh-empty');
  body.innerHTML = rows.map((r) => `<tr class="vh-row" data-id="${r.id}" tabindex="0">
    <td class="vh-c-order"><b>${esc(r.order_name)}</b></td>
    <td class="vh-c-source">${esc(r.source === 'shopify' ? (r.store_name || 'Shopify') : (r.source_db || 'BackOffice'))}</td>
    <td class="vh-c-customer">${esc(r.customer || '')}</td>
    <td class="vh-c-status">${statusPill(r.status)}${r.forced ? ' <span class="vf-forced-tag">marked</span>' : ''}</td>
    <td class="num vh-c-units" data-label="Units"><span class="vh-units"><span class="vh-units-bar" style="--pct:${r.units_required ? Math.round((r.units_verified / r.units_required) * 100) : 0}%"></span>${r.units_verified} / ${r.units_required}</span></td>
    <td class="num vh-c-errors${r.errors ? ' err' : ''}" data-label="Wrong scans">${r.errors || ''}</td>
    <td data-label="Verified by">${esc(r.verified_by_name || '')}</td>
    <td data-label="Verified">${esc(ct(r.verified_at))}</td>
    <td class="text-secondary" data-label="Started">${esc(r.started_by_name || '')} · ${esc(ct(r.started_at))}</td>
    <td class="vh-c-wb">${writebackCell(r)}</td>
  </tr>`).join('');
  empty.style.display = rows.length ? 'none' : '';
  empty.innerHTML = hasFilters()
    ? 'No verifications match these filters. <button type="button" class="btn btn-text btn-small" id="vh-empty-clear">Clear filters</button>'
    : 'No verifications yet.';
  document.getElementById('vh-empty-clear')?.addEventListener('click', clearFilters);
  const n = state.total;
  document.getElementById('vh-count').textContent = `${n.toLocaleString()} order${n === 1 ? '' : 's'}`;
}

function renderPager(offset) {
  const pager = document.getElementById('vh-pager');
  pager.style.display = state.total ? '' : 'none';
  if (!state.total) return;
  const first = offset + 1;
  const last = Math.min(offset + rows.length, state.total);
  document.getElementById('vh-range-text').textContent =
    `${first.toLocaleString()}–${last.toLocaleString()} of ${state.total.toLocaleString()}`;
  const atStart = state.page <= 1;
  const atEnd = offset + state.size >= state.total;
  document.getElementById('vh-first').disabled = atStart;
  document.getElementById('vh-prev').disabled = atStart;
  document.getElementById('vh-next').disabled = atEnd;
  document.getElementById('vh-last').disabled = atEnd;
}

const bodyEl = document.getElementById('vh-body');
bodyEl.addEventListener('click', (e) => {
  const tr = e.target.closest('.vh-row');
  if (tr) openDetail(Number(tr.dataset.id));
});
bodyEl.addEventListener('keydown', (e) => {
  const tr = e.target.closest('.vh-row');
  if (tr && e.key === 'Enter') openDetail(Number(tr.dataset.id));
});

/* ---------- detail ---------- */

function eventText(e) {
  if (e.kind === 'scan') {
    const what = e.result in SCAN_LABEL ? SCAN_LABEL[e.result] : e.result;
    const product = e.title ? ` — ${e.title}` : '';
    return `${e.barcode}${product}${e.units > 1 ? ` (+${e.units})` : ''}${what ? ` · ${what}` : ''}`;
  }
  if (['count', 'check_all', 'confirm', 'force_line'].includes(e.kind)) {
    return `${EVENT_LABEL[e.kind]} — ${e.title || ''}: ${e.qty_before} → ${e.qty_after}`;
  }
  if (e.kind === 'lines_changed' && e.detail && e.detail.changes) {
    return `${EVENT_LABEL[e.kind]}: ${e.detail.changes.map((c) => `${c.title} ${c.from} → ${c.to}`).join(', ')}`;
  }
  if (e.kind === 'undo') return `Undo — ${e.title || ''} ${e.units}`;
  return EVENT_LABEL[e.kind] || e.kind;
}

async function openDetail(id) {
  const backdrop = document.getElementById('modal-backdrop');
  const modal = document.getElementById('modal');
  modal.innerHTML = '<div class="spinner"></div>';
  backdrop.classList.add('show');
  let state;
  let events;
  try {
    [state, events] = await Promise.all([api(`/api/verify/${id}`), api(`/api/verify/${id}/events`)]);
  } catch (err) {
    modal.innerHTML = `<h3>Could not load</h3><p>${esc(err.message)}</p><div class="actions"><button class="btn btn-text" id="m-close">Close</button></div>`;
    document.getElementById('m-close').addEventListener('click', () => backdrop.classList.remove('show'));
    return;
  }
  const wb = state.writebacks;
  const wbState = state.source === 'shopify' ? wb.shopify : wb.dbadmin;
  modal.innerHTML = `
    <h3>${esc(state.order_name)} <span class="text-secondary" style="font-weight:400">· ${esc(state.source_name || '')}${state.customer ? ` · ${esc(state.customer)}` : ''}</span></h3>
    <p class="mb-16">${statusPill(state.status)}
      ${state.verified_by ? ` by ${esc(state.verified_by)} · ${esc(ct(state.verified_at))}` : ''}
      <span class="vh-detail-units">${state.progress.units_verified} / ${state.progress.units_required} units</span>
      ${wbState && wbState.state === 'failed' ? `<br><span class="chip static err">✕ ${state.source === 'shopify' ? 'Shopify tag' : 'DB_Admin Checker'} failed</span> <span class="text-secondary">${esc(wbState.error || '')}</span> <button class="btn btn-text btn-small" id="m-retry">Retry</button>` : ''}</p>
    <div class="table-wrap vh-lines-wrap"><table class="striped compact vh-lines">
      <thead><tr><th>Item</th><th>Barcode</th><th class="num">Verified</th></tr></thead>
      <tbody>${state.lines.map((l) => `<tr>
        <td>${esc(l.title)}${l.forced ? ' <span class="vf-forced-tag">marked</span>' : ''}</td>
        <td class="${l.barcode && !l.skip_reason ? 'mono' : 'text-secondary'}">${esc(l.skip_reason || l.barcode || 'no barcode')}</td>
        <td class="num">${l.skip_reason ? 'skipped' : `${l.qty_verified} / ${l.qty_required}`}</td></tr>`).join('')}</tbody>
    </table></div>
    <ul class="vh-events">${events.map((e) => `<li class="${e.kind === 'scan' && ['wrong', 'over', 'case_over'].includes(e.result) ? 'err' : ''}${e.undone_at ? ' undone' : ''}">
      <span class="text-secondary">${esc(ct(e.created_at))}</span>
      <span>${esc(e.username || '')} — ${esc(eventText(e))}</span></li>`).join('')}</ul>
    <div class="actions"><button class="btn btn-text" id="m-close">Close</button></div>`;
  document.getElementById('m-close').addEventListener('click', () => backdrop.classList.remove('show'));
  const retry = document.getElementById('m-retry');
  if (retry) {
    retry.addEventListener('click', async () => {
      try {
        await api(`/api/verify/${id}/writebacks/retry`, { method: 'POST' });
        openDetail(id);
        load();
      } catch (err) {
        snackbar(err.message, 'error');
      }
    });
  }
}

document.getElementById('modal-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modal-backdrop') e.target.classList.remove('show');
});

/* ---------- wrong barcodes ---------- */

/* Its own list state, kept in the URL next to the Orders one with a `w`
   prefix. "By barcode" groups repeat scans of one product (leading zeros
   ignored) so fixing the product clears every scan of it at once. */
const WRONG_STATUSES = ['open', 'fixed', 'all'];
const WRONG_RANGES = ['today', '7', '30', 'all', 'custom'];
const WRONG_COLUMNS = {
  scans: [
    { key: 'barcode', label: 'Scanned barcode' },
    { key: 'order', label: 'Order' },
    { key: 'source', label: 'Source' },
    { key: 'packer', label: 'Packer' },
    { key: 'when', label: 'Scanned (CT)', desc: true },
    { key: 'fixed', label: 'Status', desc: true },
  ],
  barcodes: [
    { key: 'barcode', label: 'Barcode' },
    { key: 'scans', label: 'Scans', desc: true, num: true },
    { key: 'orders', label: 'Orders', desc: true },
    { key: 'packers', label: 'Packers', unsorted: true },
    { key: 'first', label: 'First seen', desc: true },
    { key: 'last', label: 'Last seen', desc: true },
  ],
};
const WRONG_DEFAULT_SORT = { scans: 'when', barcodes: 'last' };
const WRONG_FILTER_LABEL = { source: 'Source', user: 'Packer' };
const ICON_COPY = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';

const wstate = {
  q: '',
  status: 'open',
  range: 'all',
  from: '',
  to: '',
  view: 'scans',
  filters: { source: new Set(), user: new Set() },
  sort: 'when',
  dir: 'desc',
  page: 1,
  size: 50,
  total: 0,
};
let wrongRows = [];
let wrongSeq = 0;
let wrongTimer = null;
const selected = new Map();

function readWrongUrl(p) {
  wstate.q = p.get('wq') || '';
  wstate.status = WRONG_STATUSES.includes(p.get('wstatus')) ? p.get('wstatus') : 'open';
  wstate.range = WRONG_RANGES.includes(p.get('wrange')) ? p.get('wrange') : 'all';
  wstate.from = p.get('wfrom') || '';
  wstate.to = p.get('wto') || '';
  wstate.view = p.get('wview') === 'barcodes' ? 'barcodes' : 'scans';
  Object.keys(wstate.filters).forEach((key) => {
    wstate.filters[key] = new Set((p.get(`w${key}`) || '').split(',').filter(Boolean));
  });
  const col = WRONG_COLUMNS[wstate.view].find((c) => c.key === p.get('wsort') && !c.unsorted);
  wstate.sort = col ? col.key : WRONG_DEFAULT_SORT[wstate.view];
  wstate.dir = col ? (p.get('wdir') === 'asc' ? 'asc' : 'desc') : 'desc';
  wstate.page = Math.max(1, Number(p.get('wpage')) || 1);
  wstate.size = storedPageSize('verifications.wrong.pageSize');
}

function writeWrongUrl(p) {
  if (wstate.q) p.set('wq', wstate.q);
  if (wstate.status !== 'open') p.set('wstatus', wstate.status);
  if (wstate.range !== 'all') p.set('wrange', wstate.range);
  if (wstate.range === 'custom') {
    if (wstate.from) p.set('wfrom', wstate.from);
    if (wstate.to) p.set('wto', wstate.to);
  }
  if (wstate.view !== 'scans') p.set('wview', wstate.view);
  Object.entries(wstate.filters).forEach(([key, set]) => { if (set.size) p.set(`w${key}`, [...set].join(',')); });
  if (wstate.sort !== WRONG_DEFAULT_SORT[wstate.view] || wstate.dir !== 'desc') {
    p.set('wsort', wstate.sort);
    p.set('wdir', wstate.dir);
  }
  if (wstate.page > 1) p.set('wpage', wstate.page);
}

function wrongApiParams() {
  const p = new URLSearchParams();
  if (wstate.q) p.set('q', wstate.q);
  p.set('status', wstate.status);
  p.set('view', wstate.view);
  const { from, to } = rangeDates(wstate);
  if (from) p.set('from', from);
  if (to) p.set('to', to);
  Object.entries(wstate.filters).forEach(([key, set]) => { if (set.size) p.set(key, [...set].join(',')); });
  p.set('sort', wstate.sort);
  p.set('dir', wstate.dir);
  p.set('limit', wstate.size);
  p.set('offset', (wstate.page - 1) * wstate.size);
  return p;
}

function hasWrongFilters() {
  return Boolean(wstate.q) || wstate.range !== 'all' || Object.values(wstate.filters).some((set) => set.size);
}

function refetchWrong() {
  wstate.page = 1;
  syncWrongControls();
  loadWrong();
}

async function loadWrong() {
  writeUrl();
  const seq = ++wrongSeq;
  const card = document.querySelector('.vw-card');
  card.classList.add('loading');
  let data;
  try {
    data = await api(`/api/verify/wrong-barcodes?${wrongApiParams()}`);
  } catch (err) {
    if (seq === wrongSeq) {
      card.classList.remove('loading');
      snackbar(err.message, 'error');
    }
    return;
  }
  if (seq !== wrongSeq) return;
  card.classList.remove('loading');
  wrongRows = data.rows;
  wstate.total = data.total;
  wstate.page = Math.floor(data.offset / wstate.size) + 1;
  writeUrl();
  clearSelection();
  const n = data.open_all;
  document.getElementById('vh-wrong-count').innerHTML = n ? `<span class="vw-tab-n">${n.toLocaleString()}</span>` : '';
  renderWrongStats(data.stats || {});
  renderWrongRows();
  renderWrongPager(data.offset);
}

/* ---------- wrong barcodes: controls ---------- */

function syncWrongControls() {
  document.querySelectorAll('#vw-filterbar .vh-fbtn').forEach((btn) => {
    const key = btn.dataset.filter;
    paintFilterButton(btn, WRONG_FILTER_LABEL[key], wstate.filters[key], options[key]);
  });
  [['#vw-status button', 'status'], ['#vw-range button', 'range'], ['#vw-view button', 'view']].forEach(([sel, key]) => {
    document.querySelectorAll(sel).forEach((b) => {
      const on = b.dataset[key] === wstate[key];
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on);
    });
  });
  document.getElementById('vw-custom').hidden = wstate.range !== 'custom';
  document.getElementById('vw-from').value = wstate.from;
  document.getElementById('vw-to').value = wstate.to;
  document.getElementById('vw-clear').hidden = !hasWrongFilters();
  renderWrongHead();
  const sortSel = document.getElementById('vw-sort-m');
  sortSel.innerHTML = WRONG_COLUMNS[wstate.view].filter((c) => !c.unsorted).flatMap((c) => {
    const first = c.desc ? 'desc' : 'asc';
    const second = c.desc ? 'asc' : 'desc';
    return [first, second].map((dir) => `<option value="${c.key}:${dir}">${esc(c.label)} ${dir === 'asc' ? '↑' : '↓'}</option>`);
  }).join('');
  sortSel.value = `${wstate.sort}:${wstate.dir}`;
}

function renderWrongHead() {
  const cols = WRONG_COLUMNS[wstate.view];
  document.getElementById('vw-head').innerHTML = `<tr>
    <th class="vw-c-pick"><input type="checkbox" id="vw-all" aria-label="Select every row on this page"></th>
    ${cols.map((c) => {
      const cls = [c.num ? 'num' : '', c.unsorted ? '' : 'sortable',
        c.key === wstate.sort ? wstate.dir : ''].filter(Boolean).join(' ');
      const aria = c.unsorted ? '' : ` aria-sort="${c.key !== wstate.sort ? 'none' : wstate.dir === 'asc' ? 'ascending' : 'descending'}" tabindex="0"`;
      return `<th class="${cls}" data-sort="${c.key}"${aria}>${esc(c.label)}</th>`;
    }).join('')}
    <th class="vw-c-act"><span class="sr-only">Actions</span></th></tr>`;
}

function sortWrongBy(key) {
  const col = WRONG_COLUMNS[wstate.view].find((c) => c.key === key);
  if (!col || col.unsorted) return;
  if (wstate.sort === key) wstate.dir = wstate.dir === 'asc' ? 'desc' : 'asc';
  else { wstate.sort = key; wstate.dir = col.desc ? 'desc' : 'asc'; }
  refetchWrong();
}

const vwHead = document.getElementById('vw-head');
vwHead.addEventListener('click', (e) => {
  const th = e.target.closest('th.sortable');
  if (th) sortWrongBy(th.dataset.sort);
});
vwHead.addEventListener('keydown', (e) => {
  const th = e.target.closest('th.sortable');
  if (th && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); sortWrongBy(th.dataset.sort); }
});
vwHead.addEventListener('change', (e) => {
  if (e.target.id !== 'vw-all') return;
  wrongRows.forEach((r) => {
    if (e.target.checked) selected.set(rowKey(r), r);
    else selected.delete(rowKey(r));
  });
  document.querySelectorAll('.vw-pick').forEach((cb) => { cb.checked = e.target.checked; });
  updateSelection();
});
document.getElementById('vw-sort-m').addEventListener('change', (e) => {
  [wstate.sort, wstate.dir] = e.target.value.split(':');
  refetchWrong();
});

document.getElementById('vw-filterbar').addEventListener('click', (e) => {
  const viewBtn = e.target.closest('#vw-view button');
  if (viewBtn) {
    if (viewBtn.dataset.view === wstate.view) return;
    wstate.view = viewBtn.dataset.view;
    wstate.sort = WRONG_DEFAULT_SORT[wstate.view];
    wstate.dir = 'desc';
    refetchWrong();
    return;
  }
  const btn = e.target.closest('.vh-fbtn');
  if (!btn) return;
  e.stopPropagation();
  const set = wstate.filters[btn.dataset.filter];
  if (e.target.closest('.vh-fx')) {
    closeMenu();
    set.clear();
    refetchWrong();
    return;
  }
  openFilterMenu(btn, set, options[btn.dataset.filter], refetchWrong);
});
document.getElementById('vw-status').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-status]');
  if (!b || b.dataset.status === wstate.status) return;
  wstate.status = b.dataset.status;
  refetchWrong();
});
document.getElementById('vw-range').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-range]');
  if (!b) return;
  wstate.range = b.dataset.range;
  if (wstate.range === 'custom' && !wstate.from && !wstate.to) {
    wstate.from = centralDay(6);
    wstate.to = centralDay();
  }
  refetchWrong();
});
['vw-from', 'vw-to'].forEach((id) => {
  document.getElementById(id).addEventListener('change', (e) => {
    wstate[id === 'vw-from' ? 'from' : 'to'] = e.target.value;
    refetchWrong();
  });
});
document.getElementById('vw-q').addEventListener('input', (e) => {
  clearTimeout(wrongTimer);
  wrongTimer = setTimeout(() => {
    wstate.q = e.target.value.trim();
    refetchWrong();
  }, 300);
});
document.getElementById('vw-clear').addEventListener('click', clearWrongFilters);

function clearWrongFilters() {
  wstate.q = '';
  document.getElementById('vw-q').value = '';
  wstate.range = 'all';
  Object.values(wstate.filters).forEach((set) => set.clear());
  refetchWrong();
}

document.getElementById('vw-page-size').addEventListener('change', (e) => {
  wstate.size = Number(e.target.value);
  try { localStorage.setItem('verifications.wrong.pageSize', String(wstate.size)); } catch { /* per-browser only */ }
  refetchWrong();
});
const lastWrongPage = () => Math.max(1, Math.ceil(wstate.total / wstate.size));
const goWrongPage = (n) => {
  const next = Math.min(Math.max(1, n), lastWrongPage());
  if (next === wstate.page) return;
  wstate.page = next;
  loadWrong();
  document.querySelector('.vw-card').scrollIntoView({ block: 'start', behavior: 'smooth' });
};
document.getElementById('vw-first').addEventListener('click', () => goWrongPage(1));
document.getElementById('vw-prev').addEventListener('click', () => goWrongPage(wstate.page - 1));
document.getElementById('vw-next').addEventListener('click', () => goWrongPage(wstate.page + 1));
document.getElementById('vw-last').addEventListener('click', () => goWrongPage(lastWrongPage()));

/* ---------- wrong barcodes: render ---------- */

const WRONG_STAT_CARDS = [
  { key: 'open', label: 'Open scans', status: 'open' },
  { key: 'barcodes', label: 'Barcodes to fix', view: 'barcodes' },
  { key: 'orders', label: 'Orders affected' },
  { key: 'fixed', label: 'Fixed', status: 'fixed' },
];

function renderWrongStats(stats) {
  document.getElementById('vw-stats').innerHTML = WRONG_STAT_CARDS.map((c) => {
    const n = stats[c.key] || 0;
    const on = (c.status && wstate.status === c.status) || (c.view && wstate.view === c.view && wstate.status === 'open');
    const tone = n ? '' : ' zero';
    if (!c.status && !c.view) {
      return `<div class="vh-stat vw-stat-${c.key}${tone} static"><b>${n.toLocaleString()}</b><span>${c.label}</span></div>`;
    }
    const hint = c.status ? `Show ${c.label.toLowerCase()}` : 'Group open scans by barcode';
    return `<button type="button" class="vh-stat vw-stat-${c.key}${tone}${on ? ' on' : ''}" data-stat="${c.key}"
      aria-pressed="${on}" title="${hint}"><b>${n.toLocaleString()}</b><span>${c.label}</span></button>`;
  }).join('');
}

document.getElementById('vw-stats').addEventListener('click', (e) => {
  const card = e.target.closest('button.vh-stat');
  if (!card) return;
  const c = WRONG_STAT_CARDS.find((x) => x.key === card.dataset.stat);
  if (c.status) wstate.status = wstate.status === c.status ? 'all' : c.status;
  if (c.view) {
    wstate.status = 'open';
    if (wstate.view !== 'barcodes') {
      wstate.view = 'barcodes';
      wstate.sort = WRONG_DEFAULT_SORT.barcodes;
      wstate.dir = 'desc';
    }
  }
  refetchWrong();
});

function rowKey(r) {
  return wstate.view === 'barcodes' ? `k:${r.key}` : `e:${r.id}`;
}

function rowCounts(r) {
  if (wstate.view === 'barcodes') return { open: r.open, fixed: r.scans - r.open, ids: r.ids };
  return { open: r.fixed_at ? 0 : 1, fixed: r.fixed_at ? 1 : 0, ids: [r.id] };
}

function sourceName(r) {
  return r.source === 'shopify' ? (r.store_name || 'Shopify') : (r.source_db || 'BackOffice');
}

function shortCt(iso) {
  return iso ? new Date(iso).toLocaleString('en-US', {
    timeZone: 'America/Chicago', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }) : '';
}

function codeCell(barcode) {
  return `<button type="button" class="vw-code" data-copy="${esc(barcode)}" title="Copy barcode">
    <span>${esc(barcode)}</span>${ICON_COPY}</button>`;
}

function actionCell(r) {
  const { open } = rowCounts(r);
  if (open) {
    return `<button type="button" class="btn btn-text btn-small vw-act" data-act="fix">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>Mark fixed</button>`;
  }
  return '<button type="button" class="btn btn-text btn-small vw-act vw-act-muted" data-act="reopen">Reopen</button>';
}

function scanRow(r, i) {
  const status = r.fixed_at
    ? `<span class="vh-pill verified">Fixed</span><span class="vw-sub">${esc(r.fixed_by_name || '')} · ${esc(shortCt(r.fixed_at))}</span>`
    : '<span class="vh-pill vw-open">Open</span>';
  return `<tr class="vh-row vw-row${r.fixed_at ? ' vw-fixed' : ''}" data-i="${i}">
    <td class="vw-c-pick"><input type="checkbox" class="vw-pick" aria-label="Select ${esc(r.barcode)}"${selected.has(rowKey(r)) ? ' checked' : ''}></td>
    <td class="vw-c-code">${codeCell(r.barcode)}</td>
    <td class="vw-c-order"><button type="button" class="vw-order" data-vid="${r.verification_id}" title="Open this verification">${esc(r.order_name)}</button></td>
    <td class="vw-c-source">${esc(sourceName(r))}</td>
    <td data-label="Packer">${esc(r.username || '')}</td>
    <td data-label="Scanned">${esc(ct(r.created_at))}</td>
    <td class="vw-c-status">${status}</td>
    <td class="vw-c-act">${actionCell(r)}</td>
  </tr>`;
}

function groupRow(r, i) {
  const orders = r.order_list.slice(0, 3).map((o) =>
    `<button type="button" class="vw-order vw-order-chip" data-vid="${o.verification_id}" title="Open this verification">${esc(o.name)}</button>`).join('');
  const more = r.orders > 3 ? `<span class="vw-more">+${r.orders - 3}</span>` : '';
  let tally = '';
  if (!r.open) tally = '<span class="vh-pill verified">Fixed</span>';
  else if (r.open < r.scans) tally = `<span class="vw-sub">${r.open} open</span>`;
  return `<tr class="vh-row vw-row${r.open ? '' : ' vw-fixed'}" data-i="${i}">
    <td class="vw-c-pick"><input type="checkbox" class="vw-pick" aria-label="Select ${esc(r.barcode)}"${selected.has(rowKey(r)) ? ' checked' : ''}></td>
    <td class="vw-c-code">${codeCell(r.barcode)}</td>
    <td class="num vw-c-scans" data-label="Scans"><span class="vw-scans${r.scans > 1 ? ' many' : ''}">${r.scans}</span>${tally}</td>
    <td class="vw-c-orders"><span class="vw-orders">${orders}${more}</span></td>
    <td class="vw-c-packers" data-label="Packers">${esc((r.packers || []).join(', '))}</td>
    <td data-label="First seen">${esc(ct(r.first_at))}</td>
    <td data-label="Last seen">${esc(ct(r.last_at))}</td>
    <td class="vw-c-act">${actionCell(r)}</td>
  </tr>`;
}

function renderWrongRows() {
  const body = document.getElementById('vw-body');
  const grouped = wstate.view === 'barcodes';
  body.innerHTML = wrongRows.map(grouped ? groupRow : scanRow).join('');
  document.querySelector('.vw-table').classList.toggle('vw-grouped', grouped);
  const empty = document.getElementById('vw-empty');
  empty.style.display = wrongRows.length ? 'none' : '';
  if (hasWrongFilters()) {
    empty.innerHTML = 'Nothing matches these filters. <button type="button" class="btn btn-text btn-small" id="vw-empty-clear">Clear filters</button>';
    document.getElementById('vw-empty-clear').addEventListener('click', clearWrongFilters);
  } else if (wstate.status === 'open') {
    empty.innerHTML = '<span class="vw-done">✓</span> Nothing to fix — every wrong scan has been dealt with.';
  } else {
    empty.textContent = wstate.status === 'fixed' ? 'Nothing marked fixed yet.' : 'No wrong scans yet.';
  }
  const n = wstate.total;
  const noun = grouped ? 'barcode' : 'scan';
  document.getElementById('vw-count').textContent = `${n.toLocaleString()} ${noun}${n === 1 ? '' : 's'}`;
  updateSelection();
}

function renderWrongPager(offset) {
  const pager = document.getElementById('vw-pager');
  pager.style.display = wstate.total ? '' : 'none';
  if (!wstate.total) return;
  const last = Math.min(offset + wrongRows.length, wstate.total);
  document.getElementById('vw-range-text').textContent =
    `${(offset + 1).toLocaleString()}–${last.toLocaleString()} of ${wstate.total.toLocaleString()}`;
  const atStart = wstate.page <= 1;
  const atEnd = offset + wstate.size >= wstate.total;
  document.getElementById('vw-first').disabled = atStart;
  document.getElementById('vw-prev').disabled = atStart;
  document.getElementById('vw-next').disabled = atEnd;
  document.getElementById('vw-last').disabled = atEnd;
}

/* ---------- wrong barcodes: selection and actions ---------- */

function clearSelection() {
  selected.clear();
  document.querySelectorAll('.vw-pick').forEach((cb) => { cb.checked = false; });
  updateSelection();
}

function updateSelection() {
  const picked = [...selected.values()].map(rowCounts);
  const open = picked.reduce((n, c) => n + c.open, 0);
  const fixed = picked.reduce((n, c) => n + c.fixed, 0);
  const bar = document.getElementById('vw-bulk');
  bar.hidden = !selected.size || activeTab !== 'wrong';
  const scans = open + fixed;
  document.getElementById('vw-bulk-n').textContent = wstate.view === 'barcodes'
    ? `${selected.size} barcode${selected.size === 1 ? '' : 's'} · ${scans} scan${scans === 1 ? '' : 's'}`
    : `${selected.size} selected`;
  document.getElementById('vw-fix').hidden = !open;
  document.getElementById('vw-reopen').hidden = !fixed;
  const all = document.getElementById('vw-all');
  if (all) {
    const onPage = wrongRows.filter((r) => selected.has(rowKey(r))).length;
    all.checked = Boolean(wrongRows.length) && onPage === wrongRows.length;
    all.indeterminate = onPage > 0 && onPage < wrongRows.length;
  }
  document.querySelectorAll('#vw-body .vw-row').forEach((tr) => {
    const r = wrongRows[Number(tr.dataset.i)];
    tr.classList.toggle('selected', Boolean(r) && selected.has(rowKey(r)));
  });
}

function undoSnackbar(message, undo) {
  snackbar(message, 'success');
  const el = document.getElementById('snackbar');
  clearTimeout(snackbarTimer);
  snackbarTimer = setTimeout(() => { el.className = ''; }, 8000);
  el.insertAdjacentHTML('beforeend', '<button type="button" class="snackbar-action">Undo</button>');
  el.querySelector('.snackbar-action').addEventListener('click', () => {
    el.className = '';
    undo();
  }, { once: true });
}

async function markWrong(action, picked) {
  const want = action === 'fix' ? 'open' : 'fixed';
  const counts = picked.map(rowCounts);
  const n = counts.reduce((sum, c) => sum + c[want], 0);
  const ids = counts.flatMap((c) => c.ids);
  if (!n) return;
  try {
    await api(`/api/verify/wrong-barcodes/${action}`, { method: 'POST', body: { ids } });
  } catch (err) {
    snackbar(err.message, 'error');
    return;
  }
  const what = picked.length === 1 && wstate.view === 'barcodes'
    ? `${n} scan${n === 1 ? '' : 's'} of ${picked[0].barcode}`
    : `${n} scan${n === 1 ? '' : 's'}`;
  const done = action === 'fix' ? `Marked ${what} fixed` : `Reopened ${what}`;
  undoSnackbar(done, async () => {
    try {
      await api(`/api/verify/wrong-barcodes/${action === 'fix' ? 'reopen' : 'fix'}`, { method: 'POST', body: { ids } });
    } catch (err) {
      snackbar(err.message, 'error');
    }
    loadWrong();
  });
  loadWrong();
}

const vwBody = document.getElementById('vw-body');
vwBody.addEventListener('change', (e) => {
  if (!e.target.classList.contains('vw-pick')) return;
  const r = wrongRows[Number(e.target.closest('tr').dataset.i)];
  if (e.target.checked) selected.set(rowKey(r), r);
  else selected.delete(rowKey(r));
  updateSelection();
});
vwBody.addEventListener('click', async (e) => {
  const tr = e.target.closest('tr');
  if (!tr) return;
  const r = wrongRows[Number(tr.dataset.i)];
  const order = e.target.closest('.vw-order');
  if (order) { openDetail(Number(order.dataset.vid)); return; }
  const act = e.target.closest('.vw-act');
  if (act) { markWrong(act.dataset.act, [r]); return; }
  const copy = e.target.closest('.vw-code');
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      snackbar(`Copied ${copy.dataset.copy}`);
    } catch {
      snackbar('Could not copy — select the barcode instead', 'error');
    }
    return;
  }
  if (e.target.closest('input, button, a')) return;
  const cb = tr.querySelector('.vw-pick');
  if (window.getSelection().toString()) return;
  cb.checked = !cb.checked;
  cb.dispatchEvent(new Event('change', { bubbles: true }));
});
document.getElementById('vw-fix').addEventListener('click', () => markWrong('fix', [...selected.values()]));
document.getElementById('vw-reopen').addEventListener('click', () => markWrong('reopen', [...selected.values()]));
document.getElementById('vw-bulk-clear').addEventListener('click', clearSelection);

/* ---------- tabs ---------- */

function showTab(tab) {
  activeTab = tab;
  document.querySelectorAll('#vh-tabs .tab').forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on);
  });
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === tab));
  updateSelection();
}

document.getElementById('vh-tabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (!tab || tab.dataset.tab === activeTab) return;
  closeMenu();
  showTab(tab.dataset.tab);
  writeUrl();
});
