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
let wrong = [];
let canSeeAll = false;
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

function storedPageSize() {
  try {
    const n = Number(localStorage.getItem('verifications.pageSize'));
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
  if (document.querySelector(`th.sortable[data-sort="${CSS.escape(sort || '')}"]`)) {
    state.sort = sort;
    state.dir = p.get('dir') === 'asc' ? 'asc' : 'desc';
  }
  state.page = Math.max(1, Number(p.get('page')) || 1);
  state.size = storedPageSize();
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

function rangeDates() {
  switch (state.range) {
    case 'today': return { from: centralDay() };
    case 'yesterday': return { from: centralDay(1), to: centralDay(1) };
    case '7': return { from: centralDay(6) };
    case '30': return { from: centralDay(29) };
    case 'custom': return { from: state.from, to: state.to };
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
  load();
  if (canSeeAll) loadWrong();
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

function optionLabel(key, value) {
  const o = options[key].find((x) => x.value === value);
  return o ? o.label : value;
}

function syncControls() {
  document.querySelectorAll('.vh-fbtn').forEach((btn) => {
    const key = btn.dataset.filter;
    const set = state.filters[key];
    btn.classList.toggle('active', set.size > 0);
    if (!set.size) {
      btn.innerHTML = `<svg class="vh-fplus" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>${FILTER_LABEL[key]}`;
      return;
    }
    const value = set.size === 1 ? esc(optionLabel(key, [...set][0])) : `${set.size} selected`;
    btn.innerHTML = `${FILTER_LABEL[key]}<span class="vh-fsep"></span><b>${value}</b>`
      + `<span class="vh-fx" role="button" aria-label="Clear ${FILTER_LABEL[key]}" title="Clear">×</span>`;
  });
  document.querySelectorAll('#vh-range button').forEach((b) => {
    b.classList.toggle('active', b.dataset.range === state.range);
    b.setAttribute('aria-pressed', b.dataset.range === state.range);
  });
  document.getElementById('vh-custom').hidden = !canSeeAll || state.range !== 'custom';
  document.getElementById('vh-from').value = state.from;
  document.getElementById('vh-to').value = state.to;
  document.getElementById('vh-clear').hidden = !hasFilters();
  document.querySelectorAll('th.sortable').forEach((th) => {
    th.classList.toggle('asc', th.dataset.sort === state.sort && state.dir === 'asc');
    th.classList.toggle('desc', th.dataset.sort === state.sort && state.dir === 'desc');
    th.setAttribute('aria-sort', th.dataset.sort !== state.sort ? 'none' : state.dir === 'asc' ? 'ascending' : 'descending');
  });
  const sortSel = document.getElementById('vh-sort-m');
  const sortValue = `${state.sort}:${state.dir}`;
  if (![...sortSel.options].some((o) => o.value === sortValue)) {
    sortSel.querySelector('[data-custom]')?.remove();
    sortSel.insertAdjacentHTML('beforeend', `<option data-custom value="${esc(sortValue)}">${esc(document.querySelector(`th[data-sort="${state.sort}"]`).textContent)} (${state.dir === 'asc' ? '↑' : '↓'})</option>`);
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

function renderMenu(menu, key) {
  const set = state.filters[key];
  let group = null;
  const items = options[key].map((o) => {
    const head = o.group && o.group !== group ? `<div class="vh-menu-group">${esc(o.group)}</div>` : '';
    group = o.group || group;
    return `${head}<button type="button" class="filter-menu-item${set.has(o.value) ? ' checked' : ''}" data-v="${esc(o.value)}"><span class="fm-check"></span><span>${esc(o.label)}</span></button>`;
  }).join('');
  const search = options[key].length > 8 ? '<input type="search" class="vh-menu-search" placeholder="Find…" aria-label="Find">' : '';
  menu.innerHTML = `${search}<div class="vh-menu-items">${items || '<div class="vh-menu-group">Nothing yet</div>'}</div>
    ${set.size ? '<div class="row-menu-sep"></div><button type="button" class="filter-menu-item vh-menu-clear" data-clear="1">Clear</button>' : ''}`;
}

function openFilterMenu(btn) {
  const key = btn.dataset.filter;
  if (openMenu && openMenu.btn === btn) { closeMenu(); return; }
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'row-menu filter-menu vh-menu';
  menu.setAttribute('role', 'menu');
  renderMenu(menu, key);
  menu.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const item = ev.target.closest('.filter-menu-item');
    if (!item) return;
    const set = state.filters[key];
    if (item.dataset.clear) set.clear();
    else if (set.has(item.dataset.v)) set.delete(item.dataset.v);
    else set.add(item.dataset.v);
    const find = menu.querySelector('.vh-menu-search');
    const term = find ? find.value : '';
    renderMenu(menu, key);
    const again = menu.querySelector('.vh-menu-search');
    if (again) { again.value = term; filterMenuItems(menu, term); }
    refetch();
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
  if (e.target.closest('.vh-fx')) {
    closeMenu();
    state.filters[btn.dataset.filter].clear();
    refetch();
    return;
  }
  openFilterMenu(btn);
});
document.addEventListener('click', (e) => {
  if (openMenu && !openMenu.menu.contains(e.target)) closeMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && openMenu) { openMenu.btn.focus(); closeMenu(); }
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

document.querySelectorAll('th.sortable').forEach((th) => {
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

async function loadWrong() {
  try {
    wrong = await api(`/api/verify/wrong-barcodes${document.getElementById('vw-show-fixed').checked ? '?fixed=1' : ''}`);
  } catch (err) {
    snackbar(err.message, 'error');
    return;
  }
  const open = wrong.filter((w) => !w.fixed_at).length;
  document.getElementById('vh-wrong-count').textContent = open ? `(${open})` : '';
  document.getElementById('vw-body').innerHTML = wrong.map((w) => `<tr>
    <td>${w.fixed_at ? '' : `<input type="checkbox" class="vw-pick" value="${w.id}">`}</td>
    <td class="mono">${esc(w.barcode)}</td>
    <td>${esc(w.order_name)}</td>
    <td>${esc(w.source === 'shopify' ? (w.store_name || 'Shopify') : (w.source_db || 'BackOffice'))}</td>
    <td>${esc(w.username || '')}</td>
    <td>${esc(ct(w.created_at))}</td>
    <td class="text-secondary">${w.fixed_at ? `${esc(w.fixed_by_name || '')} · ${esc(ct(w.fixed_at))}` : ''}</td>
  </tr>`).join('');
  const empty = document.getElementById('vw-empty');
  empty.style.display = wrong.length ? 'none' : '';
  empty.textContent = 'No wrong barcodes to look at.';
  document.getElementById('vw-all').checked = false;
  updateFixButton();
}

function updateFixButton() {
  document.getElementById('vw-fix').disabled = !document.querySelectorAll('.vw-pick:checked').length;
}

document.getElementById('vw-body').addEventListener('change', updateFixButton);
document.getElementById('vw-all').addEventListener('change', (e) => {
  document.querySelectorAll('.vw-pick').forEach((cb) => { cb.checked = e.target.checked; });
  updateFixButton();
});
document.getElementById('vw-show-fixed').addEventListener('change', loadWrong);
document.getElementById('vw-fix').addEventListener('click', async () => {
  const ids = [...document.querySelectorAll('.vw-pick:checked')].map((cb) => Number(cb.value));
  try {
    await api('/api/verify/wrong-barcodes/fix', { method: 'POST', body: { ids } });
    snackbar(`Marked ${ids.length} fixed`, 'success');
    loadWrong();
  } catch (err) {
    snackbar(err.message, 'error');
  }
});

/* ---------- tabs ---------- */

document.getElementById('vh-tabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (!tab) return;
  document.querySelectorAll('#vh-tabs .tab').forEach((b) => b.classList.toggle('active', b === tab));
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === tab.dataset.tab));
});
