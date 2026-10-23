const STORES_KEY = 'easyship.picklistStores';
const RULES_KEY = 'easyship.picklistRules';
const CUSTOMER_KEY = 'easyship.picklistCustomer';
const REP_KEY = 'easyship.picklistRep';
const SORT_KEY = 'easyship.picklistSort';

let stores = [];
let selectedStores = new Set();
let orders = [];
let rules = [];
// Rule id → how its chip is switched on: 'add' (orders matching any Add rule
// are shown), 'narrow' (orders must also match it) or 'hide'.
let ruleModes = new Map();
const excluded = new Set();
const checked = new Set();
let lastChecked = null; // anchor for Shift+click ranges
// The table shows either the orders going on the pick list or the ones
// excluded from it; both views work the same way.
let view = 'pick';

initNav('picklists');
document.body.classList.add('pl-page');

/* Notices sit just above the pick-list footer wherever it is on screen, so
   they never cover Generate. */
const baseSnackbar = snackbar;
snackbar = (message, type) => {
  baseSnackbar(message, type);
  const footer = document.querySelector('.pl-footer').getBoundingClientRect();
  const onScreen = footer.height && footer.top > 0 && footer.top < innerHeight;
  document.getElementById('snackbar').style.bottom = `${onScreen ? innerHeight - footer.top + 12 : 22}px`;
};

/* Per-computer memory; private windows can refuse storage. */
function remember(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* not kept */ }
}
function recall(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}

const orderKey = (o) => `${o.store_id}|${o.id}`;
const storeName = (id) => (stores.find((s) => s.id === id) || {}).name || `Store ${id}`;

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    document.getElementById('tab-new').style.display = tab.dataset.tab === 'new' ? '' : 'none';
    document.getElementById('tab-history').style.display = tab.dataset.tab === 'history' ? '' : 'none';
    if (tab.dataset.tab === 'history') loadHistory();
  });
});

/* ---------- Modal ---------- */
function openModal(html, variant = '') {
  const modal = document.getElementById('modal');
  modal.innerHTML = html;
  modal.className = `modal ${variant}`.trim();
  document.getElementById('modal-backdrop').classList.add('show');
}
function closeModal() {
  document.getElementById('modal-backdrop').classList.remove('show');
  modalOrderKey = null;
}
window.closeModal = closeModal;
document.getElementById('modal-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modal-backdrop') closeModal();
});
document.addEventListener('keydown', (e) => {
  const modalOpen = document.getElementById('modal-backdrop').classList.contains('show');
  if (e.target instanceof Element && e.target.closest('input, select, textarea')) return; // typing, not a shortcut
  if (e.key === 'Escape' && !modalOpen && checked.size) clearChecked();
  if (e.key === 'Escape') closeModal();
  if (!modalOrderKey) return;
  if (e.key === 'ArrowLeft') stepOrder(-1);
  if (e.key === 'ArrowRight') stepOrder(1);
  // E = the window's Exclude (or, in the Excluded view, Restore) button; a held key fires once.
  if ((e.key === 'e' || e.key === 'E') && !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    moveAndAdvance(modalOrderKey);
  }
});

/* ---------- Stores ---------- */
async function loadStores() {
  const el = document.getElementById('pl-stores');
  try {
    stores = (await api('/api/shopify-stores')).filter((s) => s.is_active);
  } catch {
    stores = [];
  }
  if (!stores.length) {
    el.innerHTML = '<span class="text-secondary">No Shopify stores configured.</span>';
    return;
  }
  const saved = recall(STORES_KEY, null);
  selectedStores = new Set((saved || stores.map((s) => s.id)).filter((id) => stores.some((s) => s.id === id)));
  renderStores();
  loadOrders();
}

function renderStores() {
  const el = document.getElementById('pl-stores');
  const allOn = stores.every((s) => selectedStores.has(s.id));
  el.innerHTML = '<span class="pl-chip-label">Stores</span>' + stores.map((s) =>
    `<button class="chip ${selectedStores.has(s.id) ? 'active' : ''}" data-store="${s.id}">${esc(s.name)}</button>`).join('')
    + (stores.length > 1 ? `<button class="btn btn-text btn-small pl-all-stores" id="pl-all-stores">${allOn ? 'Clear all' : 'Select all'}</button>` : '');
  const apply = (next) => {
    selectedStores = next;
    remember(STORES_KEY, [...selectedStores]);
    renderStores();
    loadOrders();
  };
  el.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const id = Number(chip.dataset.store);
      const next = new Set(selectedStores);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      apply(next);
    });
  });
  const all = document.getElementById('pl-all-stores');
  if (all) all.addEventListener('click', () => apply(new Set(allOn ? [] : stores.map((s) => s.id))));
}

/* ---------- Orders ---------- */
let loadSeq = 0;

async function loadOrders() {
  const seq = ++loadSeq;
  const tbody = document.getElementById('pl-orders');
  document.getElementById('pl-errors').innerHTML = '';
  if (!selectedStores.size) {
    orders = [];
    render('Pick at least one store.');
    return;
  }
  document.getElementById('pl-empty').style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="12"><span class="spinner"></span> Loading eligible orders…</td></tr>';
  let res;
  try {
    res = await api(`/api/picklists/orders?store_ids=${[...selectedStores].join(',')}`);
  } catch (err) {
    if (seq !== loadSeq) return;
    orders = [];
    showLoadErrors([{ error: err.message }]);
    render('Orders could not be loaded.');
    return;
  }
  if (seq !== loadSeq) return; // a newer store selection is loading
  document.getElementById('pl-window').textContent = `· orders from the last ${res.days} days`;
  orders = res.orders;
  rules = res.rules;
  const known = new Set(orders.map(orderKey));
  [...excluded].forEach((k) => { if (!known.has(k)) excluded.delete(k); });
  ruleModes = restoreRuleModes(recall(RULES_KEY, {}));
  showLoadErrors(res.errors);
  renderRules();
  render();
}

/* A store Shopify could not answer for (after its retries) is called out by
   name; the orders of the other stores stay usable. */
function showLoadErrors(errors) {
  const el = document.getElementById('pl-errors');
  el.innerHTML = errors.map((e) => `
    <div class="rule-banner err">
      <span><strong>${e.store_id ? `Couldn’t load orders from ${esc(e.store_name || storeName(e.store_id))}` : 'Couldn’t load orders'}</strong>
        — ${esc(e.error)}${e.store_id && orders.length ? ' Orders from the other stores are shown.' : ''}</span>
      <button class="btn btn-outlined btn-small pl-retry">Retry</button>
    </div>`).join('');
  el.querySelectorAll('.pl-retry').forEach((b) => b.addEventListener('click', loadOrders));
  if (errors.length) {
    const names = errors.map((e) => (e.store_id ? e.store_name || storeName(e.store_id) : null)).filter(Boolean);
    snackbar(names.length ? `Couldn’t load orders from ${names.join(', ')}` : 'Couldn’t load orders', 'error');
  }
}

/* "Show only" rules cycle off → Add → Narrow → off; "hide" rules toggle. */
const RULE_MODE = {
  add: { icon: '<path d="M12 5v14"/><path d="M5 12h14"/>', tip: 'Adding: orders matching this rule are shown (along with any other Add rule). Click to narrow instead.' },
  narrow: { icon: '<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>', tip: 'Narrowing: only orders that also match this rule stay. Click to switch off.' },
  hide: { icon: '<path d="M5 12h14"/>', tip: 'Hiding: orders matching this rule are taken out. Click to switch off.' },
};

function nextRuleMode(rule, mode) {
  if (rule.action === 'hide') return mode ? null : 'hide';
  if (!mode) return 'add';
  return mode === 'add' ? 'narrow' : null;
}

/* Saved per computer as {id: mode}; an older save (a plain list of ids) means
   every listed rule was on in its only mode. */
function restoreRuleModes(saved) {
  const modes = new Map();
  const entries = Array.isArray(saved) ? saved.map((id) => [id, null]) : Object.entries(saved || {});
  entries.forEach(([id, mode]) => {
    const rule = rules.find((r) => r.id === id);
    if (!rule) return;
    const fallback = rule.action === 'hide' ? 'hide' : 'add';
    modes.set(id, rule.action === 'hide' ? 'hide' : mode === 'narrow' ? 'narrow' : fallback);
  });
  return modes;
}

function renderRules() {
  const el = document.getElementById('pl-rules');
  el.style.display = rules.length ? '' : 'none';
  const offTip = (r) => (r.action === 'hide' ? 'Click to hide orders matching this rule'
    : 'Click to add orders matching this rule; click again to narrow to them');
  el.innerHTML = '<span class="pl-chip-label">Rules</span>' + rules.map((r) => {
    const mode = ruleModes.get(r.id);
    return `<button class="chip pl-rule-chip ${mode ? `mode-${mode}` : ''}" data-rule="${esc(r.id)}"
      title="${esc(mode ? RULE_MODE[mode].tip : offTip(r))}" aria-pressed="${mode ? 'true' : 'false'}">
      ${mode ? `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${RULE_MODE[mode].icon}</svg>` : ''}${esc(r.name)}</button>`;
  }).join('') + `<span class="pl-rule-legend">
      ${['add', 'narrow'].map((m) =>
        `<span class="mode-${m}"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${RULE_MODE[m].icon}</svg>${{ add: 'Add', narrow: 'Narrow' }[m]}</span>`).join('')}
      <span class="hint">click a rule again to change</span></span>`;
  el.querySelectorAll('.pl-rule-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const rule = rules.find((r) => r.id === chip.dataset.rule);
      const next = nextRuleMode(rule, ruleModes.get(rule.id));
      if (next) ruleModes.set(rule.id, next);
      else ruleModes.delete(rule.id);
      remember(RULES_KEY, Object.fromEntries(ruleModes));
      renderRules();
      render();
    });
  });
}

function quickFilters() {
  const val = (id) => document.getElementById(id).value.trim().toLowerCase();
  return {
    tag: val('qf-tag'),
    ctag: val('qf-ctag'),
    states: val('qf-state').split(',').map((s) => s.trim().toUpperCase()).filter(Boolean),
    search: val('qf-search'),
  };
}

/* Hide rules drop what they match; when any keep rule is on, an order must
   match at least one of them. Quick filters narrow further. */
function passesFilters(o, qf) {
  const inMode = (mode) => [...ruleModes].filter(([, m]) => m === mode).map(([id]) => id);
  const matches = (id) => o.rule_ids.includes(id);
  if (inMode('hide').some(matches)) return false;
  const adds = inMode('add');
  if (adds.length && !adds.some(matches)) return false;
  if (!inMode('narrow').every(matches)) return false;
  const has = (tags, needle) => tags.some((t) => t.toLowerCase().includes(needle));
  if (qf.tag && !has(o.tags, qf.tag)) return false;
  if (qf.ctag && !has(o.customer_tags, qf.ctag)) return false;
  if (qf.states.length && !qf.states.includes((o.ship_state || '').toUpperCase())) return false;
  if (qf.search && !`${o.name} ${o.customer}`.toLowerCase().includes(qf.search)) return false;
  return true;
}

/* Column sort, remembered per computer; the order window walks the same order. */
const SORT_VALUE = {
  name: (o) => o.name,
  store: (o) => storeName(o.store_id),
  created_at: (o) => o.created_at,
  customer: (o) => o.customer,
  address: (o) => o.ship_address1,
  state: (o) => o.ship_state,
  shipping: (o) => o.shipping_method,
  total: (o) => (o.total == null || o.total === '' ? null : Number(o.total)),
  tags: (o) => o.tags.join(', '),
};
let sort = recall(SORT_KEY, { key: 'created_at', dir: 1 });
if (!SORT_VALUE[sort.key]) sort = { key: 'created_at', dir: 1 };
const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });

function compareOrders(a, b) {
  const va = SORT_VALUE[sort.key](a);
  const vb = SORT_VALUE[sort.key](b);
  const diff = typeof va === 'number' && typeof vb === 'number' ? va - vb : collator.compare(String(va ?? ''), String(vb ?? ''));
  // Blank values sink to the bottom either way; ties fall back to the order number.
  if (!va && va !== 0 && (vb || vb === 0)) return 1;
  if (!vb && vb !== 0 && (va || va === 0)) return -1;
  return diff * sort.dir || collator.compare(a.name, b.name);
}

function renderSortHeads() {
  document.querySelectorAll('#pl-head th.sortable').forEach((th) => {
    th.classList.toggle('asc', th.dataset.sort === sort.key && sort.dir === 1);
    th.classList.toggle('desc', th.dataset.sort === sort.key && sort.dir === -1);
  });
}

document.querySelectorAll('#pl-head th.sortable').forEach((th) => {
  th.addEventListener('click', () => {
    sort = th.dataset.sort === sort.key ? { key: sort.key, dir: -sort.dir } : { key: th.dataset.sort, dir: 1 };
    remember(SORT_KEY, sort);
    render();
  });
});

function visibleOrders() {
  const qf = quickFilters();
  return orders.filter((o) => passesFilters(o, qf)).sort(compareOrders);
}

function includedOrders() {
  return visibleOrders().filter((o) => !excluded.has(orderKey(o)));
}

/* The rows of the current view, in list order. */
function listedOrders() {
  return visibleOrders().filter((o) => excluded.has(orderKey(o)) === (view === 'excluded'));
}

/* Exclude in the To pick view, restore in the Excluded view. */
function moveOrders(keys) {
  keys.forEach((k) => (view === 'pick' ? excluded.add(k) : excluded.delete(k)));
}

function setView(next) {
  view = next;
  checked.clear();
  lastChecked = null;
  document.querySelectorAll('.pl-view').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  render();
}
document.querySelectorAll('.pl-view').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));

function tagsHtml(tags) {
  return tags.length ? `<div class="order-tags">${tags.map((t) => `<span class="tag-chip">${esc(t)}</span>`).join('')}</div>` : '';
}

function shipAddressHtml(o) {
  const street = [o.ship_address1, o.ship_address2].filter(Boolean).join(', ');
  const place = [o.ship_city, o.ship_zip].filter(Boolean).join(' ');
  if (!street && !place) return '<span class="text-secondary">—</span>';
  return `<div class="ellip-line" title="${esc(street)}">${esc(street)}</div><div class="text-secondary">${esc(place)}</div>`;
}

function render(emptyMessage) {
  const tbody = document.getElementById('pl-orders');
  const empty = document.getElementById('pl-empty');
  const visible = visibleOrders();
  const shown = listedOrders();
  const excludedCount = visible.filter((o) => excluded.has(orderKey(o))).length;
  document.getElementById('pl-count-pick').textContent = visible.length - excludedCount;
  document.getElementById('pl-count-excluded').textContent = excludedCount;
  const action = view === 'pick'
    ? '<button class="btn btn-text btn-small pl-exclude" data-move="KEY" title="Leave this order off the pick list">Exclude</button>'
    : '<button class="btn btn-text btn-small pl-restore" data-move="KEY" title="Put this order back on the pick list">Restore</button>';
  if (!shown.length) {
    tbody.innerHTML = '';
    empty.textContent = emptyMessage
      || (view === 'excluded' ? 'No excluded orders.' : orders.length ? 'No orders match the filters.' : 'No eligible orders.');
    empty.style.display = '';
  } else {
    empty.style.display = 'none';
    tbody.innerHTML = shown.map((o, i) => `<tr class="${checked.has(orderKey(o)) ? 'selected' : ''}">
      <td class="pl-check"><input type="checkbox" data-check="${esc(orderKey(o))}" ${checked.has(orderKey(o)) ? 'checked' : ''} aria-label="Select ${esc(o.name)}"></td>
      <td class="pl-num">${i + 1}</td>
      <td><button class="link-btn" data-fraud="${esc(orderKey(o))}" title="Open order details">${esc(o.name)}</button></td>
      <td><span class="pl-store">${esc(storeName(o.store_id))}</span></td>
      <td class="pl-date">${esc(shortDate(o.created_at))}</td>
      <td class="pl-customer"><div class="ellip-line" title="${esc(o.customer)}">${esc(o.customer)}</div>${tagsHtml(o.customer_tags)}</td>
      <td class="pl-address">${shipAddressHtml(o)}</td>
      <td>${o.ship_state ? `<span class="pl-state">${esc(o.ship_state)}</span>` : ''}</td>
      <td class="pl-shipping" title="${esc(o.shipping_method)}">${o.shipping_method ? `<div class="pl-clamp">${esc(o.shipping_method)}</div>` : '<span class="text-secondary">—</span>'}</td>
      <td class="num pl-total">${money(o.total)}</td>
      <td class="wrap pl-tags">${tagsHtml(o.tags)}</td>
      <td class="pl-actions">${action.replace('KEY', esc(orderKey(o)))}</td>
    </tr>`).join('');
  }
  // Only rows on screen can stay selected: filtering or excluding drops the rest.
  const onScreen = new Set(shown.map(orderKey));
  [...checked].forEach((k) => { if (!onScreen.has(k)) checked.delete(k); });
  renderSortHeads();
  renderSummary();
  renderSelection(shown.length);
}

function renderSelection(rowCount) {
  const all = document.getElementById('pl-check-all');
  all.checked = rowCount > 0 && checked.size === rowCount;
  all.indeterminate = checked.size > 0 && checked.size < rowCount;
  all.disabled = !rowCount;
  document.getElementById('pl-bulk').style.display = checked.size ? '' : 'none';
  document.getElementById('pl-bulk-count').textContent = `${checked.size} selected`;
  document.getElementById('pl-bulk-action').textContent = view === 'pick' ? 'Exclude selected' : 'Restore selected';
  document.getElementById('pl-bulk-action').className = `btn btn-small ${view === 'pick' ? 'btn-danger' : 'btn-outlined'}`;
}

/* Shift extends from the last row clicked to this one, in list order. */
function toggleChecked(key, on, range) {
  const keys = listedOrders().map(orderKey);
  const from = keys.indexOf(lastChecked);
  const to = keys.indexOf(key);
  const targets = range && from >= 0 && to >= 0
    ? keys.slice(Math.min(from, to), Math.max(from, to) + 1) : [key];
  targets.forEach((k) => (on ? checked.add(k) : checked.delete(k)));
  lastChecked = key;
  render();
}

function clearChecked() {
  checked.clear();
  lastChecked = null;
  render();
}

function renderSummary() {
  const inc = includedOrders();
  const units = inc.reduce((n, o) => n + o.units, 0);
  const products = new Set(inc.flatMap((o) => o.lines.map((l) => l.barcode || `?${l.title}`))).size;
  document.getElementById('pl-summary').textContent = inc.length
    ? `On the pick list: ${inc.length} order${inc.length === 1 ? '' : 's'} · ${units} unit${units === 1 ? '' : 's'} · ${products} product${products === 1 ? '' : 's'}`
    : 'Nothing on the pick list';
  document.getElementById('pl-generate').disabled = !inc.length;
}

document.getElementById('pl-orders').addEventListener('click', (e) => {
  const move = e.target.closest('[data-move]');
  if (move) {
    moveOrders([move.dataset.move]);
    render();
    return;
  }
  const fraud = e.target.closest('[data-fraud]');
  if (fraud) {
    showOrder(fraud.dataset.fraud);
    return;
  }
  // The checkbox, or a click anywhere else on the row, selects it.
  let box = e.target.closest('[data-check]');
  if (!box && !e.target.closest('button, a, input')) {
    box = e.target.closest('tr') && e.target.closest('tr').querySelector('[data-check]');
    if (box) box.checked = !box.checked;
  }
  if (box) toggleChecked(box.dataset.check, box.checked, e.shiftKey);
});
// Shift+click selects rows, not text.
document.getElementById('pl-orders').addEventListener('mousedown', (e) => {
  if (e.shiftKey) e.preventDefault();
});
document.getElementById('pl-check-all').addEventListener('change', (e) => {
  listedOrders().forEach((o) => (e.target.checked ? checked.add(orderKey(o)) : checked.delete(orderKey(o))));
  lastChecked = null;
  render();
});
document.getElementById('pl-bulk-action').addEventListener('click', () => {
  const n = checked.size;
  const plural = `${n} order${n === 1 ? '' : 's'}`;
  moveOrders([...checked]);
  clearChecked();
  snackbar(view === 'pick' ? `${plural} excluded — see the Excluded tab` : `${plural} back on the pick list`);
});
document.getElementById('pl-bulk-clear').addEventListener('click', clearChecked);
['qf-tag', 'qf-ctag', 'qf-state', 'qf-search'].forEach((id) =>
  document.getElementById(id).addEventListener('input', () => render()));
document.getElementById('qf-clear').addEventListener('click', () => {
  ['qf-tag', 'qf-ctag', 'qf-state', 'qf-search'].forEach((id) => { document.getElementById(id).value = ''; });
  render();
});
document.getElementById('pl-refresh').addEventListener('click', loadOrders);

/* ---------- Order window ----------
   Walks the orders as listed (filters and exclusions applied): Previous /
   Next or ← →, and Exclude moves straight on to the next order. Details are
   cached per order and the next one is fetched ahead. */
let modalOrderKey = null;
const orderDetails = new Map();

const RECOMMENDATION_TEXT = {
  ACCEPT: 'You can fulfill this order',
  INVESTIGATE: 'Investigate this order before fulfilling it',
  CANCEL: 'Consider canceling this order',
};
const LEVEL_TONE = { HIGH: 'err', MEDIUM: 'warn', LOW: 'ok' };
const CARDHOLDER_TEXT = {
  MATCH: ['ok', 'Name on card matches the order'],
  LAST_NAME_ONLY: ['warn', 'Only the last name on the card matches'],
  MISMATCH: ['err', 'Name on card does not match the billing or shipping name'],
};
const SENTIMENT_TONE = { NEGATIVE: 'err', POSITIVE: 'ok' };
const svg = (paths) => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
const ICONS = {
  err: svg('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
  warn: svg('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
  ok: svg('<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>'),
  info: svg('<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>'),
  card: svg('<rect width="20" height="14" x="2" y="5" rx="2"/><path d="M2 10h20"/>'),
  globe: svg('<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>'),
  prev: svg('<path d="m15 18-6-6 6-6"/>'),
  next: svg('<path d="m9 18 6-6-6-6"/>'),
  close: svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
  box: svg('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>'),
};

const titleCase = (v) => (v ? v.charAt(0) + v.slice(1).toLowerCase().replace(/_/g, ' ') : '');

function shortDate(iso) {
  if (!iso) return '';
  const d = new Date(`${iso}T12:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function fetchOrderDetail(o) {
  const key = orderKey(o);
  if (!orderDetails.has(key)) {
    const pending = api(`/api/picklists/orders/risk?store_id=${o.store_id}&order_id=${encodeURIComponent(o.id)}`);
    pending.catch(() => orderDetails.delete(key)); // a failed load is tried again next time
    orderDetails.set(key, pending);
  }
  return orderDetails.get(key);
}

function sameAddress(a, b) {
  const fields = ['address1', 'address2', 'city', 'state', 'zip', 'country'];
  return !!a && !!b && fields.every((f) => (a[f] || '').trim().toLowerCase() === (b[f] || '').trim().toLowerCase());
}

function addressLines(a) {
  return [a.name, a.address1, a.address2, [[a.city, a.state].filter(Boolean).join(', '), a.zip].filter(Boolean).join(' '),
    a.country === 'US' ? '' : a.country, a.phone].filter(Boolean).map((l) => `<div>${esc(l)}</div>`).join('');
}

function omCard(label, body) {
  return `<div class="om-card"><div class="om-label">${label}</div>${body}</div>`;
}

function riskPanel(risk) {
  if (!risk) {
    return `<div class="om-risk"><div class="om-label">Order risk evaluation</div>
      <div class="text-secondary">Shopify has no risk evaluation for this order.</div></div>`;
  }
  const tone = LEVEL_TONE[risk.level] || '';
  const pill = risk.level && risk.level !== 'PENDING'
    ? `<span class="om-pill ${tone}">${esc(titleCase(risk.level))} risk</span>`
    : '<span class="om-pill">Analysis pending</span>';
  const advice = risk.level === 'PENDING' ? 'Risk analysis in progress' : RECOMMENDATION_TEXT[risk.recommendation] || '';
  const many = risk.assessments.length > 1;
  const assessments = risk.assessments.map((a) => `
    ${many ? `<div class="om-provider">${esc(a.provider || 'Shopify')} · ${esc(a.level ? `${titleCase(a.level)} risk` : 'No risk level')}</div>` : ''}
    ${a.facts.length ? `<ul class="om-facts">${a.facts.map((f) => {
      const t = SENTIMENT_TONE[f.sentiment] || 'info';
      return `<li class="${t}">${ICONS[t]}<span>${esc(f.description)}</span></li>`;
    }).join('')}</ul>` : ''}`).join('');
  const checks = [];
  if (risk.cardholder) {
    const c = risk.cardholder;
    const [t, text] = CARDHOLDER_TEXT[c.status];
    checks.push(`<div class="om-check ${t}">${ICONS.card}<div><div class="om-check-title">${text}</div>
      <div class="hint">${c.card_names.length > 1 ? 'Cards' : 'Card'}: ${esc(c.card_names.join(', '))} · Billing: ${esc(c.billing_name || '—')} · Shipping: ${esc(c.shipping_name || '—')}</div></div></div>`);
  }
  if (risk.ip) {
    checks.push(`<div class="om-check">${ICONS.globe}<div><div class="om-check-title">IP ${esc(risk.ip)}</div>
      <div class="hint">${risk.ip_location ? `Placed from ${esc(risk.ip_location)}` : 'Location unknown'}</div></div></div>`);
  }
  return `<div class="om-risk ${tone}">
    <div class="om-risk-top"><span class="om-label">Order risk evaluation</span>${pill}${advice ? `<span class="om-advice">${esc(advice)}</span>` : ''}</div>
    ${assessments}
    ${checks.length ? `<div class="om-checks">${checks.join('')}</div>` : ''}
  </div>`;
}

function orderBody(d) {
  const c = d.customer || {};
  const stats = [
    ['Total', money(d.total)],
    ['Items', d.items ?? '—'],
    ['Shipping', esc(d.shipping_method || '—')],
    ['Customer orders', c.orders_count ?? '—'],
  ];
  const customer = `<div class="om-strong">${esc(c.name || '—')}</div>
    ${d.email ? `<div>${esc(d.email)}</div>` : ''}${d.phone ? `<div>${esc(d.phone)}</div>` : ''}
    <div class="hint">${[c.since ? `Customer since ${esc(shortDate(c.since))}` : '', c.amount_spent != null ? `spent ${money(c.amount_spent)}` : ''].filter(Boolean).join(' · ')}</div>
    ${tagsHtml(c.tags || [])}`;
  const ship = d.shipping_address ? addressLines(d.shipping_address) : '<span class="text-secondary">—</span>';
  const bill = !d.billing_address ? '<span class="text-secondary">—</span>'
    : sameAddress(d.billing_address, d.shipping_address)
      ? `<div class="om-strong">${esc(d.billing_address.name || '')}</div><div class="text-secondary">Same as ship to</div>`
      : addressLines(d.billing_address);
  return `
    <div class="om-main">
      <div class="om-stats">${stats.map(([l, v]) => `<div class="om-stat"><div class="om-label">${l}</div><div class="om-value">${v}</div></div>`).join('')}</div>
      <div class="om-cards">${omCard('Customer', customer)}${omCard('Ship to', ship)}${omCard('Bill to', bill)}</div>
      ${riskPanel(d.risk)}
    </div>
    ${productsPanel(d.products || [])}`;
}

/* The order's products beside the details; a long list scrolls on its own
   so the window keeps its height. */
function productsPanel(products) {
  const units = products.reduce((n, p) => n + p.qty, 0);
  const rows = products.map((p) => `
    <li class="om-product">
      ${p.image ? `<img class="om-thumb" src="${esc(p.image)}" alt="" loading="lazy">` : `<span class="om-thumb om-thumb-empty">${ICONS.box}</span>`}
      <div class="om-product-info">
        <div class="om-product-title" title="${esc(p.title)}">${esc(p.title)}</div>
        ${p.variant ? `<div class="hint">${esc(p.variant)}</div>` : ''}
        <div class="om-product-codes">${[p.sku && `SKU ${esc(p.sku)}`, p.barcode ? esc(p.barcode) : p.ships && '<span class="om-nobarcode">No barcode</span>']
          .filter(Boolean).join(' · ')}</div>
        ${!p.ships ? '<div class="hint">No shipping needed</div>' : p.to_ship < p.qty ? `<div class="hint">${p.to_ship} left to ship</div>` : ''}
      </div>
      <div class="om-product-qty"><strong>×${p.qty}</strong><span class="hint">${money(p.price)}</span></div>
    </li>`).join('');
  return `<aside class="om-products">
    <div class="om-products-inner">
      <div class="om-products-head"><span class="om-label">Products</span><span class="hint">${products.length} item${products.length === 1 ? '' : 's'} · ${units} unit${units === 1 ? '' : 's'}</span></div>
      ${products.length ? `<ul class="om-product-list">${rows}</ul>` : '<div class="om-products-empty text-secondary">No products</div>'}
    </div>
  </aside>`;
}

async function showOrder(key) {
  const list = listedOrders();
  const idx = list.findIndex((o) => orderKey(o) === key);
  if (idx < 0) {
    closeModal();
    return;
  }
  const o = list[idx];
  modalOrderKey = key;
  openModal(`
    <div class="om-head">
      <div>
        <div class="om-title">Order ${esc(o.name)}<span id="om-status"></span></div>
        <div class="om-meta">${esc(storeName(o.store_id))} · ${esc(shortDate(o.created_at))} · ${esc(o.customer)} · ${o.units} unit${o.units === 1 ? '' : 's'}
          ${o.tags.length ? tagsHtml(o.tags) : ''}</div>
      </div>
      <button class="om-close" onclick="closeModal()" aria-label="Close">${ICONS.close}</button>
    </div>
    <div class="om-body" id="om-body"><div class="om-loading"><span class="spinner"></span> Loading order details…</div></div>
    <div class="om-foot">
      <button class="btn btn-outlined btn-small" id="om-prev" ${idx === 0 ? 'disabled' : ''}>${ICONS.prev} Previous</button>
      <span class="om-pos">${idx + 1} of ${list.length}</span>
      <button class="btn btn-outlined btn-small" id="om-next" ${idx === list.length - 1 ? 'disabled' : ''}>Next ${ICONS.next}</button>
      <span class="om-keys" aria-label="Keyboard shortcuts">
        <span><kbd>←</kbd><kbd>→</kbd> Previous / Next</span>
        <span><kbd>E</kbd> ${view === 'pick' ? 'Exclude' : 'Restore'}</span>
        <span><kbd>Esc</kbd> Close</span>
      </span>
      <span class="om-spacer"></span>
      ${view === 'pick'
        ? '<button class="btn btn-danger" id="om-move" title="Shortcut: E">Exclude from pick list <kbd>E</kbd></button>'
        : '<button class="btn btn-outlined" id="om-move" title="Shortcut: E">Restore to pick list <kbd>E</kbd></button>'}
    </div>`, 'order');
  document.getElementById('om-prev').addEventListener('click', () => stepOrder(-1));
  document.getElementById('om-next').addEventListener('click', () => stepOrder(1));
  document.getElementById('om-move').addEventListener('click', () => moveAndAdvance(key));
  const current = fetchOrderDetail(o);
  if (list[idx + 1]) fetchOrderDetail(list[idx + 1]).catch(() => {}); // ready before Next / Exclude

  let html;
  try {
    const d = await current;
    html = orderBody(d);
    if (modalOrderKey === key) {
      document.getElementById('om-status').innerHTML = [d.financial_status, d.fulfillment_status]
        .filter(Boolean).map((v) => `<span class="om-chip">${esc(titleCase(v))}</span>`).join('');
    }
  } catch (err) {
    html = `<div class="om-loading"><span class="error-note">${esc(err.message)}</span>
      <button class="btn btn-outlined btn-small" id="om-retry">Retry</button></div>`;
  }
  if (modalOrderKey !== key) return; // moved on while this was loading
  document.getElementById('om-body').innerHTML = html;
  const retry = document.getElementById('om-retry');
  if (retry) retry.addEventListener('click', () => showOrder(key));
}

function stepOrder(delta) {
  const list = listedOrders();
  const idx = list.findIndex((o) => orderKey(o) === modalOrderKey);
  const target = list[idx + delta];
  if (target) showOrder(orderKey(target));
}

/* Exclude (or, in the Excluded view, restore) and go straight to the next order. */
function moveAndAdvance(key) {
  const before = listedOrders();
  const idx = before.findIndex((o) => orderKey(o) === key);
  const name = before[idx] ? before[idx].name : '';
  moveOrders([key]);
  render();
  const after = listedOrders();
  snackbar(view === 'pick' ? `${name} excluded` : `${name} back on the pick list`);
  if (!after.length) {
    closeModal();
    return;
  }
  showOrder(orderKey(after[Math.min(idx, after.length - 1)]));
}

/* ---------- Generate ---------- */
document.getElementById('pl-generate').addEventListener('click', openGenerate);

async function openGenerate() {
  const picked = includedOrders();
  if (!picked.length) return;
  const body = { orders: picked.map((o) => ({ store_id: o.store_id, order_id: o.id })) };
  openModal('<h3>New pick list</h3><p><span class="spinner"></span> Checking orders and BackOffice products…</p>', 'wide');
  let preview;
  let reps;
  try {
    [preview, reps] = await Promise.all([
      api('/api/picklists/preview', { method: 'POST', body }),
      api('/api/picklists/sales-reps'),
    ]);
  } catch (err) {
    openModal(`<h3>New pick list</h3><p class="error-note">${esc(err.message)}</p>
      <div class="actions"><button class="btn btn-text" onclick="closeModal()">Close</button></div>`, 'wide');
    return;
  }
  const t = preview.totals;
  const lastCustomer = recall(CUSTOMER_KEY, null);
  const lastRep = recall(REP_KEY, null);
  openModal(`
    <h3>New pick list</h3>
    ${preview.excluded.length ? `<div class="rule-banner warn pl-stack mb-16">
      <strong>${preview.excluded.length} order${preview.excluded.length === 1 ? '' : 's'} left out automatically</strong>
      <ul class="pl-left-out">${preview.excluded.map((e) => `<li><strong>${esc(e.name)}</strong>${e.store_id ? ` <span class="text-secondary">${esc(storeName(e.store_id))}</span>` : ''} — ${e.reasons.map(esc).join('; ')}</li>`).join('')}</ul>
    </div>` : ''}
    ${t.orders ? `
    <div class="hint mb-16">${t.orders} order${t.orders === 1 ? '' : 's'} · ${t.units} units · ${t.lines} product${t.lines === 1 ? '' : 's'}</div>
    <div class="pl-preview-lines">
      <table>
        <thead><tr><th>Barcode</th><th>Product</th><th>SKU</th><th class="num">Qty</th></tr></thead>
        <tbody>${preview.lines.map((g) => `<tr>
          <td>${esc(g.barcode)}</td><td class="ellip" style="max-width:320px" title="${esc(g.orders.join(', '))}">${esc(g.description)}</td>
          <td>${esc(g.sku)}</td><td class="num"><strong>${g.qty}</strong></td></tr>`).join('')}</tbody>
      </table>
    </div>
    <div class="row mt-16">
      <div class="field">
        <label>BackOffice customer</label>
        <input id="m-cust-search" placeholder="Search business name or account #" autocomplete="off">
        <select id="m-cust" size="5"></select>
      </div>
      <div class="field fixed" style="min-width:220px">
        <label>Sales rep</label>
        <select id="m-rep"><option value="">— Choose —</option>${reps.map((r) =>
          `<option value="${r.id}" ${lastRep === r.id ? 'selected' : ''}>${esc(r.name)}</option>`).join('')}</select>
      </div>
    </div>` : '<p>Nothing left to put on a pick list.</p>'}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      ${t.orders ? '<button class="btn btn-primary" id="m-create" disabled>Create pick list</button>' : ''}
    </div>`, 'wide');
  if (!t.orders) return;

  const custSel = document.getElementById('m-cust');
  const repSel = document.getElementById('m-rep');
  const createBtn = document.getElementById('m-create');
  const sync = () => { createBtn.disabled = !custSel.value || !repSel.value; };
  let custSeq = 0;
  const searchCustomers = async (q) => {
    const seq = ++custSeq;
    let list = [];
    try { list = await api(`/api/picklists/customers?q=${encodeURIComponent(q)}`); } catch (err) { snackbar(err.message, 'error'); }
    if (seq !== custSeq) return;
    if (lastCustomer && !q && !list.some((c) => c.id === lastCustomer.id)) list.unshift(lastCustomer);
    const keep = custSel.value;
    custSel.innerHTML = list.map((c) => `<option value="${c.id}" data-name="${esc(c.name)}">${esc(c.name)}${c.account_no ? ` · ${esc(c.account_no)}` : ''}${c.state ? ` · ${esc([c.city, c.state].filter(Boolean).join(', '))}` : ''}</option>`).join('');
    const want = keep || (lastCustomer ? String(lastCustomer.id) : '');
    if ([...custSel.options].some((op) => op.value === want)) custSel.value = want;
    sync();
  };
  let timer;
  document.getElementById('m-cust-search').addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => searchCustomers(e.target.value.trim()), 300);
  });
  custSel.addEventListener('change', sync);
  repSel.addEventListener('change', sync);
  searchCustomers('');

  createBtn.addEventListener('click', async () => {
    const opt = custSel.selectedOptions[0];
    const customer = { id: Number(custSel.value), name: opt ? opt.dataset.name : '' };
    createBtn.disabled = true;
    createBtn.innerHTML = '<span class="spinner"></span> Creating…';
    try {
      const res = await api('/api/picklists', {
        method: 'POST',
        body: { ...body, customer_id: customer.id, customer_name: customer.name, sales_rep_id: Number(repSel.value) },
      });
      remember(CUSTOMER_KEY, customer);
      remember(REP_KEY, Number(repSel.value));
      openModal(`
        <h3>Pick list #${res.id} created</h3>
        <p>BackOffice quotation <strong>${esc(res.quotation_number)}</strong> — ${res.totals.orders} orders, ${res.totals.units} units, ${res.totals.lines} products.</p>
        ${res.excluded.length ? `<p class="hint mt-16">Left out: ${res.excluded.map((e) => esc(e.name)).join(', ')}</p>` : ''}
        <div class="actions"><button class="btn btn-primary" onclick="closeModal()">Done</button></div>`);
      loadOrders();
    } catch (err) {
      const names = err.data && err.data.orders ? ` (${err.data.orders.join(', ')})` : '';
      snackbar(err.message + names, 'error');
      createBtn.disabled = false;
      createBtn.textContent = 'Create pick list';
      if (err.status === 409) loadOrders();
    }
  });
}

/* ---------- History ---------- */
const STATUS_CHIP = {
  active: '<span class="chip static ok">Active</span>',
  cancelled: '<span class="chip static">Cancelled</span>',
  creating: '<span class="chip static warn">Incomplete</span>',
};

async function loadHistory() {
  const tbody = document.getElementById('pl-history');
  tbody.innerHTML = '<tr><td colspan="11"><span class="spinner"></span> Loading…</td></tr>';
  let rows;
  try {
    rows = await api('/api/picklists');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="11" class="text-secondary">${esc(err.message)}</td></tr>`;
    return;
  }
  document.getElementById('pl-history-empty').style.display = rows.length ? 'none' : '';
  tbody.innerHTML = rows.map((p) => `<tr>
    <td><strong>${p.id}</strong></td>
    <td>${esc(p.created_at)}</td>
    <td>${esc(p.created_by || '')}</td>
    <td>${esc(p.quotation_number || '—')}</td>
    <td class="ellip" title="${esc(p.customer_name || '')}">${esc(p.customer_name || '')}</td>
    <td>${esc(p.sales_rep_name || '')}</td>
    <td class="num">${p.order_count}</td>
    <td class="num">${p.unit_count}</td>
    <td class="num">${p.line_count}</td>
    <td>${STATUS_CHIP[p.status] || esc(p.status)}${p.cancelled_at ? `<div class="hint">${esc(p.cancelled_at)}</div>` : ''}</td>
    <td>
      <button class="btn btn-text btn-small" data-view="${p.id}">View</button>
      ${p.status !== 'cancelled' ? `<button class="btn btn-danger btn-small" data-cancel="${p.id}" data-number="${esc(p.quotation_number || '')}">Cancel</button>` : ''}
    </td>
  </tr>`).join('');
}

document.getElementById('pl-history').addEventListener('click', (e) => {
  const view = e.target.closest('[data-view]');
  if (view) viewPicklist(Number(view.dataset.view));
  const cancel = e.target.closest('[data-cancel]');
  if (cancel) confirmCancel(Number(cancel.dataset.cancel), cancel.dataset.number);
});

async function viewPicklist(id) {
  openModal(`<h3>Pick list #${id}</h3><p><span class="spinner"></span> Loading…</p>`, 'wide');
  let p;
  try {
    p = await api(`/api/picklists/${id}`);
  } catch (err) {
    snackbar(err.message, 'error');
    closeModal();
    return;
  }
  openModal(`
    <h3>Pick list #${p.id} ${STATUS_CHIP[p.status] || ''}</h3>
    <div class="hint mb-16">Quotation ${esc(p.quotation_number || '—')} · ${esc(p.customer_name || '')} · ${esc(p.sales_rep_name || '')} · ${esc(p.created_at)} by ${esc(p.created_by || '')}</div>
    <h4 class="mb-8">Products (${p.lines.length})</h4>
    <div class="pl-preview-lines mb-16"><table>
      <thead><tr><th>Barcode</th><th>Product</th><th>SKU</th><th class="num">Qty</th></tr></thead>
      <tbody>${p.lines.map((l) => `<tr><td>${esc(l.barcode)}</td><td class="ellip" style="max-width:320px">${esc(l.description || '')}</td><td>${esc(l.sku || '')}</td><td class="num"><strong>${l.qty}</strong></td></tr>`).join('')}</tbody>
    </table></div>
    <h4 class="mb-8">Orders (${p.orders.length})</h4>
    <div class="pl-preview-lines"><table>
      <thead><tr><th>Order</th><th>Store</th><th>Customer</th><th>State</th><th class="num">Units</th></tr></thead>
      <tbody>${p.orders.map((o) => `<tr><td>${esc(o.order_name)}</td><td>${esc(o.store_name || '')}</td><td>${esc(o.customer_name || '')}</td><td>${esc(o.ship_state || '')}</td><td class="num">${o.units}</td></tr>`).join('')}</tbody>
    </table></div>
    <div class="actions"><button class="btn btn-text" onclick="closeModal()">Close</button></div>`, 'wide');
}

function confirmCancel(id, number) {
  openModal(`
    <h3>Cancel pick list #${id}?</h3>
    <p>${number ? `Quotation <strong>${esc(number)}</strong> and its lines will be <strong>deleted from BackOffice</strong>. ` : ''}Its orders become available for a new pick list again.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Keep it</button>
      <button class="btn btn-danger" id="m-cancel-confirm">Cancel pick list</button>
    </div>`);
  document.getElementById('m-cancel-confirm').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      const res = await api(`/api/picklists/${id}/cancel`, { method: 'POST' });
      closeModal();
      snackbar(res.quotation_deleted ? `Pick list #${id} cancelled — quotation deleted` : `Pick list #${id} cancelled`, 'success');
      loadHistory();
      loadOrders();
    } catch (err) {
      e.target.disabled = false;
      snackbar(err.message, 'error');
    }
  });
}

loadStores();
