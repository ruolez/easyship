const STORES_KEY = 'easyship.picklistStores';
const RULES_KEY = 'easyship.picklistRules';
const CUSTOMER_KEY = 'easyship.picklistCustomer';
const REP_KEY = 'easyship.picklistRep';
const SORT_KEY = 'easyship.picklistSort';
const PRESET_KEY = 'easyship.picklistPreset';

let stores = [];
let selectedStores = new Set();
let orders = [];
let rules = [];
// Rule id → how its chip is switched on: 'add' (orders matching any Add rule
// are shown), 'narrow' (orders must also match it) or 'hide'.
let ruleModes = new Map();
// Presets combine rules into one filter; while one is on it decides alone and the rule chips pause.
let presets = [];
let activePreset = null;
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
  if (e.key === '/' && !modalOpen && !e.ctrlKey && !e.metaKey && !e.altKey
    && document.getElementById('tab-new').style.display !== 'none') {
    e.preventDefault();
    document.getElementById('qf-search').focus();
    return;
  }
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

/* A store pill shows how many eligible orders it brought in, once loaded. */
function renderStores(counts) {
  const el = document.getElementById('pl-stores');
  const allOn = stores.every((s) => selectedStores.has(s.id));
  const tick = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
  el.innerHTML = stores.map((s) => {
    const on = selectedStores.has(s.id);
    const n = on && counts ? counts.get(s.id) || 0 : null;
    return `<button class="pl-store-pill ${on ? 'on' : ''}" data-store="${s.id}" aria-pressed="${on}">
      <span class="pl-tick">${on ? tick : ''}</span>${esc(s.name)}${n != null ? `<span class="pl-store-n">${n}</span>` : ''}</button>`;
  }).join('')
    + (stores.length > 1 ? `<button class="link-btn pl-all-stores" id="pl-all-stores">${allOn ? 'Clear all' : 'Select all'}</button>` : '');
  const apply = (next) => {
    selectedStores = next;
    remember(STORES_KEY, [...selectedStores]);
    renderStores();
    loadOrders();
  };
  el.querySelectorAll('.pl-store-pill').forEach((chip) => {
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
  const refresh = document.getElementById('pl-refresh');
  if (!selectedStores.size) {
    orders = [];
    render('Pick at least one store.');
    return;
  }
  document.getElementById('pl-empty').style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="12"><span class="spinner"></span> Loading eligible orders…</td></tr>';
  refresh.classList.add('loading');
  let res;
  try {
    res = await api(`/api/picklists/orders?store_ids=${[...selectedStores].join(',')}`);
  } catch (err) {
    if (seq !== loadSeq) return;
    refresh.classList.remove('loading');
    orders = [];
    showLoadErrors([{ error: err.message }]);
    render('Orders could not be loaded.');
    return;
  }
  if (seq !== loadSeq) return; // a newer store selection is loading
  refresh.classList.remove('loading');
  const updated = document.getElementById('pl-updated');
  updated.textContent = `Last ${res.days} days · Updated ${new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`;
  updated.title = `Paid, unfulfilled orders created in the last ${res.days} days`;
  orders = res.orders;
  const counts = new Map();
  orders.forEach((o) => counts.set(o.store_id, (counts.get(o.store_id) || 0) + 1));
  renderStores(counts);
  rules = res.rules;
  presets = res.presets || [];
  const savedPreset = recall(PRESET_KEY, null);
  activePreset = presets.some((p) => p.id === savedPreset) ? savedPreset : null;
  const known = new Set(orders.map(orderKey));
  [...excluded].forEach((k) => { if (!known.has(k)) excluded.delete(k); });
  ruleModes = restoreRuleModes(recall(RULES_KEY, {}));
  showLoadErrors(res.errors);
  renderPresets();
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

/* ---------- Presets ---------- */
const PRESET_ICON = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/></svg>';

/* (rule AND/OR rule …) AND/OR (…); an item can be negated. */
function presetMatches(preset, o) {
  const item = (it) => o.rule_ids.includes(it.rule) !== it.negate;
  const group = (g) => (g.join === 'or' ? g.items.some(item) : g.items.every(item));
  return preset.join === 'or' ? preset.groups.some(group) : preset.groups.every(group);
}

const ruleName = (id) => (rules.find((r) => r.id === id) || {}).name || 'a deleted rule';

/* "Wholesale or Big orders, and not Same address" — the preset as a sentence of rule tokens. */
function presetExpressionHtml(preset) {
  const join = (j) => `<span class="pl-pjoin">${j === 'or' ? 'or' : 'and'}</span>`;
  const groups = preset.groups.map((g) => {
    const items = g.items.map((it) => `${it.negate ? '<span class="pl-pnot">not</span>' : ''}<span class="pl-ptoken">${esc(ruleName(it.rule))}</span>`);
    const body = items.join(join(g.join));
    return preset.groups.length > 1 && g.items.length > 1 ? `<span class="pl-pgroup">${body}</span>` : body;
  });
  return groups.join(join(preset.join));
}

function setPreset(id) {
  activePreset = id;
  remember(PRESET_KEY, id);
  renderPresets();
  renderRules();
  render();
}

function renderPresets() {
  document.getElementById('pl-presets-row').style.display = presets.length ? '' : 'none';
  const el = document.getElementById('pl-presets');
  el.innerHTML = presets.map((p) => {
    const on = p.id === activePreset;
    return `<button class="chip pl-preset-chip${on ? ' on' : ''}" data-preset="${esc(p.id)}" aria-pressed="${on}"
      title="${on ? 'Click to switch this preset off' : 'Show only the orders this preset matches'}">${PRESET_ICON}${esc(p.name)}</button>`;
  }).join('');
  const preset = presets.find((p) => p.id === activePreset);
  const desc = document.getElementById('pl-preset-desc');
  desc.hidden = !preset;
  desc.innerHTML = preset ? `<span class="pl-pdesc-lead">Showing orders that match</span> ${presetExpressionHtml(preset)}` : '';
  el.querySelectorAll('.pl-preset-chip').forEach((chip) => {
    chip.addEventListener('click', () => setPreset(chip.dataset.preset === activePreset ? null : chip.dataset.preset));
  });
}

function renderRules() {
  const el = document.getElementById('pl-rules');
  document.getElementById('pl-rules-row').style.display = rules.length ? '' : 'none';
  const pausedBy = presets.find((p) => p.id === activePreset);
  document.getElementById('pl-rules-row').classList.toggle('paused', !!pausedBy);
  const offTip = (r) => (r.action === 'hide' ? 'Click to hide orders matching this rule'
    : 'Click to add orders matching this rule; click again to narrow to them');
  el.innerHTML = rules.map((r) => {
    const mode = ruleModes.get(r.id);
    return `<button class="chip pl-rule-chip ${mode ? `mode-${mode}` : ''}" data-rule="${esc(r.id)}"
      title="${esc(mode ? RULE_MODE[mode].tip : offTip(r))}" aria-pressed="${mode ? 'true' : 'false'}">
      ${mode ? `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${RULE_MODE[mode].icon}</svg>` : ''}${esc(r.name)}</button>`;
  }).join('');
  document.getElementById('pl-rule-legend').innerHTML = `
      ${['add', 'narrow', 'hide'].filter((m) => m !== 'hide' || rules.some((r) => r.action === 'hide')).map((m) =>
        `<span class="mode-${m}"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${RULE_MODE[m].icon}</svg>${{ add: 'Add', narrow: 'Narrow', hide: 'Hide' }[m]}</span>`).join('')}
      <span class="hint">${pausedBy ? `paused while <strong>${esc(pausedBy.name)}</strong> is on` : 'click a rule again to change'}</span>`;
  el.querySelectorAll('.pl-rule-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const rule = rules.find((r) => r.id === chip.dataset.rule);
      if (activePreset) {
        setPreset(null);
        snackbar('Preset switched off — the rule chips work again');
      }
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
  const preset = presets.find((p) => p.id === activePreset);
  if (preset) {
    if (!presetMatches(preset, o)) return false;
  } else {
    const inMode = (mode) => [...ruleModes].filter(([, m]) => m === mode).map(([id]) => id);
    const matches = (id) => o.rule_ids.includes(id);
    if (inMode('hide').some(matches)) return false;
    const adds = inMode('add');
    if (adds.length && !adds.some(matches)) return false;
    if (!inMode('narrow').every(matches)) return false;
  }
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
    const unmatched = !emptyMessage && orders.length && !visible.length;
    empty.innerHTML = esc(emptyMessage
      || (unmatched ? 'No orders match the rules and filters.'
        : view === 'excluded' ? 'No excluded orders.' : orders.length ? 'Every order is excluded.' : 'No eligible orders.'))
      + (unmatched ? '<div><button class="btn btn-outlined btn-small pl-empty-reset" id="pl-empty-reset">Show all orders</button></div>' : '');
    if (unmatched) document.getElementById('pl-empty-reset').addEventListener('click', resetFilters);
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
  renderFilterState(visible.length);
}

/* Says how many orders the rules and filters are holding back, with one
   click to show them all again. */
function renderFilterState(visibleCount) {
  const qf = quickFilters();
  QF_IDS.forEach((id) => {
    const input = document.getElementById(id);
    input.parentElement.classList.toggle('filled', !!input.value.trim());
  });
  document.getElementById('qf-clear').hidden = !(qf.tag || qf.ctag || qf.states.length);
  const filtered = activePreset || ruleModes.size || qf.tag || qf.ctag || qf.states.length || qf.search;
  const el = document.getElementById('pl-showing');
  el.innerHTML = filtered && orders.length
    ? `Showing <strong>${visibleCount}</strong> of ${orders.length} orders<button class="link-btn" id="pl-reset">Show all</button>`
    : '';
  const reset = document.getElementById('pl-reset');
  if (reset) reset.addEventListener('click', resetFilters);
}

function resetFilters() {
  QF_IDS.forEach((id) => { document.getElementById(id).value = ''; });
  ruleModes.clear();
  remember(RULES_KEY, {});
  activePreset = null;
  remember(PRESET_KEY, null);
  renderPresets();
  renderRules();
  render();
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
const QF_IDS = ['qf-tag', 'qf-ctag', 'qf-state', 'qf-search'];
QF_IDS.forEach((id) => document.getElementById(id).addEventListener('input', () => render()));
document.getElementById('qf-clear').addEventListener('click', () => {
  ['qf-tag', 'qf-ctag', 'qf-state'].forEach((id) => { document.getElementById(id).value = ''; });
  render();
});
document.getElementById('qf-search').addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && e.target.value) {
    e.target.value = '';
    render();
  }
});
document.getElementById('pl-refresh').addEventListener('click', loadOrders);

/* ---------- Order window ----------
   Walks the orders as listed (filters and exclusions applied): Previous /
   Next or ← →, and Exclude moves straight on to the next order. Details are
   cached per order and the next one is fetched ahead. */
let modalOrderKey = null;
let modalIndex = 0;
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
  search: svg('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>'),
  check: svg('<path d="M20 6 9 17l-5-5"/>'),
  copy: svg('<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>'),
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
      <div class="om-cardholder">${c.card_names.length > 1 ? 'Cards' : 'Card'}: <strong>${esc(c.card_names.join(', '))}</strong></div>
      <div class="hint">Billing: ${esc(c.billing_name || '—')} · Shipping: ${esc(c.shipping_name || '—')}</div></div></div>`);
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
    <span id="om-ctags">${tagsHtml(c.tags || [])}</span>`;
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
      ${profitPanel(d.profit)}
    </div>
    ${productsPanel(d.products || [])}`;
}

/* Profitability, as in Shopify Automation's order window: revenue − product
   cost − the shipping EasyShip's past labels suggest, against the profit
   check's thresholds. */
function estimateText(est) {
  if (est.cost == null) {
    if (!est.weight_lb) return 'No estimate — the products have no weight in Shopify';
    return `No estimate — no EasyShip labels to ${esc(est.state || 'this state')} near ${est.weight_lb} lb in the last 90 days`;
  }
  return `Average of ${plural(est.samples, 'EasyShip label')} to ${esc(est.state)} within ±${est.tolerance_lb} lb (${est.weight_lb} lb of products)`;
}

function profitPanel(p) {
  if (!p) return '';
  const head = (extra = '') => `<div class="om-profit-top"><span class="om-label">Profitability</span>${extra}</div>`;
  if (!p.available) {
    return `<div class="om-profit">${head()}<div class="text-secondary">Profit can't be worked out: ${esc(p.reason_text || 'unknown reason')}.</div></div>`;
  }
  const est = p.shipping_estimate || {};
  const tone = p.below_threshold || p.profit < 0 ? 'err' : p.includes_shipping ? 'ok' : '';
  const margin = p.margin_pct != null ? ` · ${pct(p.margin_pct)} margin` : '';
  const pill = `<span class="om-pill ${tone}">${signedMoney(p.profit)}${margin}${p.includes_shipping ? '' : ' before shipping'}</span>`;
  const row = (label, value, cls = '', note = '') =>
    `<div class="profit-row${cls ? ` ${cls}` : ''}"><span>${label}${note ? ` <span class="hint">(${note})</span>` : ''}</span><span class="num">${value}</span></div>`;
  const missing = p.missing_cost.length;
  const t = p.thresholds || {};
  const limits = [t.min_amount != null ? `profit ≥ ${money(t.min_amount)}` : '', t.min_margin_pct != null ? `margin ≥ ${t.min_margin_pct}%` : '']
    .filter(Boolean).join(' · ');
  let check = '';
  if (limits && !p.includes_shipping) check = `<div class="hint">Profit check (${limits}) needs a shipping estimate</div>`;
  else if (limits && p.reasons.length) check = `<ul class="profit-reasons">${p.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`;
  else if (limits) check = `<div class="om-profit-ok">${ICONS.ok}<span>Clears the profit check (${limits})</span></div>`;
  return `<div class="om-profit ${tone}">
    ${head(pill)}
    <div class="om-profit-grid">
      <div class="profit-table">
        ${row('Products (after discounts)', money(p.items_subtotal))}
        ${row('+ Shipping paid', money(p.shipping_paid))}
        ${row('− Product cost', money(p.items_cost), '', missing ? `${missing} without cost, at 90% of price` : '')}
        ${row('− Est. shipping', est.cost == null ? '—' : money(est.cost))}
        ${row(p.includes_shipping ? '= Profit' : '= Profit before shipping', signedMoney(p.profit), `total${tone === 'err' ? ' bad' : ''}`)}
      </div>
      <div class="om-profit-side">
        <div class="om-label">Estimated shipping</div>
        <div class="om-profit-ship">${est.cost == null ? '—' : money(est.cost)}</div>
        <div class="hint">${estimateText(est)}</div>
        ${check}
      </div>
    </div>
  </div>`;
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
        ${p.cost != null
          ? `<div class="om-product-cost">Cost ${money(p.cost)}${p.margin_pct != null ? ` · <span class="${p.margin_pct < 0 ? 'neg' : p.margin_pct < 15 ? 'low' : 'good'}">${pct(p.margin_pct)} margin</span>` : ''}</div>`
          : p.ships ? '<div class="om-product-cost low">No cost in Shopify</div>' : ''}
      </div>
      <div class="om-product-qty"><strong>×${p.qty}</strong><span class="hint">${money(p.price)}</span>${p.price_original ? `<s class="hint">${money(p.price_original)}</s>` : ''}</div>
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
  modalIndex = idx;
  openModal(`
    <div class="om-head">
      <div>
        <div class="om-title">Order ${esc(o.name)}<span id="om-status"></span></div>
        <div class="om-meta">${esc(storeName(o.store_id))} · ${esc(shortDate(o.created_at))} · ${esc(o.customer)} · ${o.units} unit${o.units === 1 ? '' : 's'}
          <span id="om-tags">${tagsHtml(o.tags)}</span></div>
      </div>
      <button class="om-close" onclick="closeModal()" aria-label="Close">${ICONS.close}</button>
    </div>
    <div class="om-body" id="om-body"><div class="om-loading"><span class="spinner"></span> Loading order details…</div></div>
    <div class="om-foot">
      <button class="btn btn-outlined btn-small" id="om-prev" ${idx === 0 ? 'disabled' : ''}>${ICONS.prev} Previous</button>
      <span class="om-pos">${idx + 1} of ${list.length}</span>
      <button class="btn btn-outlined btn-small" id="om-next" ${idx === list.length - 1 ? 'disabled' : ''}>Next ${ICONS.next}</button>
      <span class="om-keys" title="← → Previous / Next · E ${view === 'pick' ? 'Exclude' : 'Restore'} · Esc Close" aria-label="Keyboard shortcuts: arrow keys for previous and next, E to ${view === 'pick' ? 'exclude' : 'restore'}, Escape to close">
        <span><kbd>←</kbd><kbd>→</kbd></span><span><kbd>E</kbd></span><span><kbd>Esc</kbd></span>
      </span>
      <span class="om-spacer"></span>
      ${tagBoxHtml('order', o.tags)}
      ${tagBoxHtml('customer', o.customer_tags)}
      ${view === 'pick'
        ? '<button class="btn btn-danger" id="om-move" title="Shortcut: E">Exclude from pick list <kbd>E</kbd></button>'
        : '<button class="btn btn-outlined" id="om-move" title="Shortcut: E">Restore to pick list <kbd>E</kbd></button>'}
    </div>`, 'order');
  document.getElementById('om-prev').addEventListener('click', () => stepOrder(-1));
  document.getElementById('om-next').addEventListener('click', () => stepOrder(1));
  document.getElementById('om-move').addEventListener('click', () => moveAndAdvance(key));
  document.querySelectorAll('#modal .tagbox').forEach((box) => wireTagBox(box, key));
  const current = fetchOrderDetail(o);
  if (list[idx + 1]) fetchOrderDetail(list[idx + 1]).catch(() => {}); // ready before Next / Exclude

  let html;
  try {
    const d = await current;
    html = orderBody(d);
    if (modalOrderKey === key && !d.customer) {
      document.querySelector('#modal .tagbox[data-kind="customer"]').outerHTML = tagBoxHtml('customer', [], true);
    }
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

/* Where the open order sits; a tag change can drop it out of the listing,
   and Previous / Next then go from the gap it left. */
function modalPosition(list, delta = 1) {
  const idx = list.findIndex((o) => orderKey(o) === modalOrderKey);
  if (idx >= 0) return idx;
  return delta > 0 ? modalIndex - 1 : modalIndex;
}

function stepOrder(delta) {
  const list = listedOrders();
  const target = list[modalPosition(list, delta) + delta];
  if (target) showOrder(orderKey(target));
}

/* Exclude (or, in the Excluded view, restore) and go straight to the next order. */
function moveAndAdvance(key) {
  const before = listedOrders();
  const idx = Math.max(modalPosition(before), 0);
  const name = (orders.find((o) => orderKey(o) === key) || {}).name || '';
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

/* ---------- Tag fields in the order window ----------
   Like Shopify's: the current tags as chips (× removes one), typing suggests
   the store's tags, Enter / comma / a click adds. Every change is saved to
   Shopify straight away, one at a time per field, and the order comes back
   with the rules it now matches. */
const shopTags = new Map(); // `${store}|${kind}` → Promise<string[]>
const TAG_SUGGESTIONS = 8;

function loadShopTags(storeId, kind) {
  const key = `${storeId}|${kind}`;
  if (!shopTags.has(key)) {
    const pending = api(`/api/picklists/tags?store_id=${storeId}&kind=${kind}`).then((r) => r.tags);
    pending.catch(() => shopTags.delete(key)); // tried again on the next focus
    shopTags.set(key, pending);
  }
  return shopTags.get(key);
}

function rememberShopTag(storeId, kind, tag) {
  const key = `${storeId}|${kind}`;
  if (shopTags.has(key)) {
    shopTags.set(key, shopTags.get(key).then((all) => (all.some((t) => t.toLowerCase() === tag.toLowerCase()) ? all : [tag, ...all])));
  }
}

const tagChipHtml = (t, pending = false) => `<span class="tagbox-chip${pending ? ' pending' : ''}" data-tag="${attr(t)}">${esc(t)}<button type="button" class="tagbox-x" aria-label="Remove ${attr(t)}" title="Remove">${ICONS.close}</button></span>`;

function tagBoxHtml(kind, tags, disabled = false) {
  const label = kind === 'order' ? 'Order tags' : 'Customer tags';
  return `<div class="tagbox${disabled ? ' disabled' : ''}" data-kind="${kind}">
    <div class="tagbox-field" title="${disabled ? 'This order has no customer' : label}">
      <span class="tagbox-label">${label}</span>
      ${(tags || []).map((t) => tagChipHtml(t)).join('')}
      <input class="tagbox-input" placeholder="${disabled ? 'No customer' : 'Add…'}" ${disabled ? 'disabled' : ''}
        autocomplete="off" spellcheck="false" aria-label="Add ${kind} tag" aria-autocomplete="list">
    </div>
    <ul class="tagbox-menu" role="listbox" hidden></ul>
  </div>`;
}

/* Typed text first as "Add …" when it is new, then the store's tags that
   start with it, then those that contain it (Shopify's popularity order). */
function tagOptions(all, have, typed) {
  const needle = typed.toLowerCase();
  const free = all.filter((t) => !have.has(t.toLowerCase()));
  const starts = free.filter((t) => t.toLowerCase().startsWith(needle));
  const contains = needle ? free.filter((t) => !t.toLowerCase().startsWith(needle) && t.toLowerCase().includes(needle)) : [];
  const options = [...starts, ...contains].slice(0, TAG_SUGGESTIONS).map((tag) => ({ tag }));
  const known = have.has(needle) || all.some((t) => t.toLowerCase() === needle);
  return typed && !known ? [{ tag: typed, isNew: true }, ...options] : options;
}

function highlightTag(tag, typed) {
  const at = typed ? tag.toLowerCase().indexOf(typed.toLowerCase()) : -1;
  if (at < 0) return esc(tag);
  return `${esc(tag.slice(0, at))}<mark>${esc(tag.slice(at, at + typed.length))}</mark>${esc(tag.slice(at + typed.length))}`;
}

function wireTagBox(box, key) {
  const kind = box.dataset.kind;
  const input = box.querySelector('.tagbox-input');
  const menu = box.querySelector('.tagbox-menu');
  const order = () => orders.find((o) => orderKey(o) === key);
  const saved = () => (kind === 'order' ? order().tags : order().customer_tags) || [];
  const shown = () => [...box.querySelectorAll('.tagbox-chip')].map((c) => c.dataset.tag);
  let options = [];
  let active = 0;
  let queue = Promise.resolve();
  let saving = 0;
  const unsaved = new Set();

  function showChips(tags) {
    box.querySelectorAll('.tagbox-chip').forEach((c) => c.remove());
    input.insertAdjacentHTML('beforebegin', tags.map((t) => tagChipHtml(t, unsaved.has(t))).join(''));
  }

  function closeMenu() {
    menu.hidden = true;
    options = [];
  }

  async function openMenu() {
    const typed = input.value.trim();
    let all = [];
    try { all = await loadShopTags(order().store_id, kind); } catch { /* typing still adds */ }
    if (document.activeElement !== input || input.value.trim() !== typed) return; // moved on meanwhile
    options = tagOptions(all, new Set(shown().map((t) => t.toLowerCase())), typed);
    active = 0;
    if (!options.length) {
      closeMenu();
      return;
    }
    menu.innerHTML = options.map((opt, i) => `<li class="tagbox-opt${i === active ? ' active' : ''}" role="option" data-i="${i}">
      ${opt.isNew ? `<span class="hint">Add</span> “${esc(opt.tag)}”` : highlightTag(opt.tag, typed)}</li>`).join('');
    menu.hidden = false;
  }

  function markActive() {
    menu.querySelectorAll('.tagbox-opt').forEach((li, i) => li.classList.toggle('active', i === active));
    const li = menu.children[active];
    if (li) li.scrollIntoView({ block: 'nearest' });
  }

  function save(change, optimistic) {
    (change.add || []).forEach((t) => unsaved.add(t));
    showChips(optimistic);
    saving += 1;
    queue = queue.then(async () => {
      try {
        const o = order();
        const res = await api('/api/picklists/orders/tags', {
          method: 'POST', body: { store_id: o.store_id, order_id: o.id, kind, ...change },
        });
        (change.add || []).forEach((t) => rememberShopTag(o.store_id, kind, t));
        applyOrderUpdate(res.order);
      } catch (err) {
        snackbar(err.message, 'error');
      }
      (change.add || []).forEach((t) => unsaved.delete(t));
      saving -= 1;
      // Redraw from Shopify's answer once nothing else is queued, so later chips don't flicker.
      if (!saving && modalOrderKey === key && box.isConnected) showChips(saved());
    });
  }

  function addTag(raw) {
    const have = new Set(shown().map((t) => t.toLowerCase()));
    const tags = raw.split(',').map((t) => t.trim()).filter((t) => t && !have.has(t.toLowerCase()));
    input.value = '';
    closeMenu();
    if (tags.length) save({ add: tags }, [...shown(), ...tags]);
  }

  if (input.disabled) return;
  input.addEventListener('focus', openMenu);
  input.addEventListener('input', () => {
    if (input.value.includes(',')) addTag(input.value);
    else openMenu();
  });
  input.addEventListener('blur', closeMenu);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!options.length) return;
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      markActive();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const pick = options[active];
      if (pick && !menu.hidden) addTag(pick.tag);
      else if (input.value.trim()) addTag(input.value);
    } else if (e.key === 'Escape') {
      if (!menu.hidden) closeMenu();
      else input.blur();
    }
  });
  menu.addEventListener('mousedown', (e) => {
    const li = e.target.closest('.tagbox-opt');
    e.preventDefault(); // keep the input focused
    if (li) addTag(options[Number(li.dataset.i)].tag);
  });
  box.querySelector('.tagbox-field').addEventListener('click', (e) => {
    const x = e.target.closest('.tagbox-x');
    if (!x) {
      input.focus();
      return;
    }
    const tag = x.closest('.tagbox-chip').dataset.tag;
    save({ remove: [tag] }, shown().filter((t) => t !== tag));
  });
}

/* A saved tag change: the list, the cached details and the open window all
   take the order as Shopify now has it. */
function applyOrderUpdate(updated) {
  const key = orderKey(updated);
  const i = orders.findIndex((o) => orderKey(o) === key);
  if (i < 0) return;
  orders[i] = { ...orders[i], ...updated };
  const detail = orderDetails.get(key);
  if (detail) {
    orderDetails.set(key, detail.then((d) => ({
      ...d, tags: updated.tags, customer: d.customer && { ...d.customer, tags: updated.customer_tags },
    })));
  }
  render();
  if (modalOrderKey !== key) return;
  document.getElementById('om-tags').innerHTML = tagsHtml(updated.tags);
  const ctags = document.getElementById('om-ctags');
  if (ctags) ctags.innerHTML = tagsHtml(updated.customer_tags);
  if (!listedOrders().some((o) => orderKey(o) === key)) {
    snackbar(`${updated.name} no longer fits the rules and filters in use — it leaves this list when you move on`);
  }
}

/* ---------- Generate ---------- */
document.getElementById('pl-generate').addEventListener('click', openGenerate);

const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
const attr = (v) => esc(v).replace(/"/g, '&quot;');
const genShown = () => document.getElementById('modal-backdrop').classList.contains('show') && !!document.getElementById('gen-body');

function customerMeta(c) {
  const where = [c.city, c.state].filter(Boolean).join(', ');
  return [c.account_no ? `Account ${esc(c.account_no)}` : '', esc(where)].filter(Boolean).join(' · ') || `Customer #${c.id}`;
}

function genFrame(body, foot) {
  return `
    <div class="om-head">
      <div>
        <div class="om-title">New pick list</div>
        <div class="om-meta">Check the products, choose who the BackOffice quotation is for, then create it.</div>
      </div>
      <button class="om-close" onclick="closeModal()" aria-label="Close">${ICONS.close}</button>
    </div>
    <div class="gen-body" id="gen-body">${body}</div>
    <div class="om-foot">${foot}</div>`;
}

const GEN_CANCEL = '<span class="om-spacer"></span><button class="btn btn-text" onclick="closeModal()">Cancel</button>';

function leftOutHtml(left, open) {
  if (!left.length) return '';
  return `<details class="gen-leftout" ${open || left.length <= 3 ? 'open' : ''}>
    <summary>${ICONS.warn}<span>${plural(left.length, 'order')} left out automatically</span>
      <span class="hint">They won't be on this pick list</span>${ICONS.next}</summary>
    <ul>${left.map((e) => `<li><strong>${esc(e.name)}</strong>${e.store_id ? `<span class="pl-store">${esc(storeName(e.store_id))}</span>` : ''}
      <span>${e.reasons.map(esc).join('; ')}</span></li>`).join('')}</ul>
  </details>`;
}

function linesTable(lines) {
  const rows = lines.map((g, i) => `<tr data-i="${i}">
    <td class="num gen-idx">${i + 1}</td>
    <td class="gen-product"><div class="gen-product-name" title="${attr(g.title && g.title !== g.description ? `Shopify: ${g.title}` : g.description)}">${esc(g.description)}</div>
      ${g.sku ? `<div class="hint">SKU ${esc(g.sku)}</div>` : ''}</td>
    <td class="gen-code">${esc(g.barcode)}</td>
    <td class="num" title="${attr(g.orders.join(', '))}">${g.orders.length}</td>
    <td class="num"><span class="gen-qty">${g.qty.toLocaleString()}</span></td></tr>`).join('');
  return `<div class="gen-lines"><table>
    <thead><tr><th class="num">#</th><th>Product</th><th class="gen-code">Barcode</th><th class="num" title="Orders that need it">Orders</th><th class="num">Qty</th></tr></thead>
    <tbody id="gen-lines">${rows}</tbody></table>
    <div class="gen-msg" id="gen-lines-none" hidden>No products match the filter.</div></div>`;
}

async function openGenerate() {
  const picked = includedOrders();
  if (!picked.length) return;
  const body = { orders: picked.map((o) => ({ store_id: o.store_id, order_id: o.id })) };
  openModal(genFrame(`<div class="om-loading"><span class="spinner"></span> Checking ${plural(picked.length, 'order')} with Shopify and BackOffice…</div>`, GEN_CANCEL), 'gen');
  let preview;
  let reps;
  try {
    [preview, reps] = await Promise.all([
      api('/api/picklists/preview', { method: 'POST', body }),
      api('/api/picklists/sales-reps'),
    ]);
  } catch (err) {
    if (!genShown()) return;
    document.getElementById('gen-body').innerHTML = `<div class="om-loading gen-failed">
      <div class="error-note">${esc(err.message)}</div><button class="btn btn-outlined btn-small" id="gen-retry">Try again</button></div>`;
    document.getElementById('gen-retry').addEventListener('click', openGenerate);
    return;
  }
  if (!genShown()) return; // closed while checking
  const t = preview.totals;
  const left = preview.excluded;
  if (!t.orders) {
    openModal(genFrame(`<div class="gen-empty">${ICONS.warn}<div><strong>None of these orders can go on a pick list</strong>
      <div class="hint">Fix the reasons below, then try again.</div></div></div>${leftOutHtml(left, true)}`, GEN_CANCEL), 'gen');
    return;
  }

  const lastCustomer = recall(CUSTOMER_KEY, null);
  const lastRep = recall(REP_KEY, null);
  const stats = [['Orders', t.orders], ['Units', t.units], ['Products', t.lines], ['Left out', left.length]];
  const repPick = reps.length === 1 ? reps[0].id : lastRep;
  openModal(genFrame(`
    <div class="om-stats">${stats.map(([label, n], i) => `<div class="om-stat"><div class="om-label">${label}</div>
      <div class="om-value ${i === 3 && n ? 'gen-warn' : ''}">${n.toLocaleString()}</div></div>`).join('')}</div>
    ${leftOutHtml(left, false)}
    <div class="gen-grid">
      <section class="gen-panel">
        <div class="gen-panel-head">
          <span class="om-label">Products to pick</span>
          <span class="hint" id="gen-lines-count">${plural(t.lines, 'product')}</span>
          ${t.lines > 8 ? `<label class="gen-search gen-filter">${ICONS.search}<input id="gen-lines-filter" placeholder="Filter products" autocomplete="off" aria-label="Filter products"></label>` : ''}
        </div>
        ${linesTable(preview.lines)}
      </section>
      <section class="gen-panel gen-side">
        <div class="gen-panel-head"><span class="om-label">BackOffice quotation</span></div>
        <div class="gen-side-body">
          <div class="gen-step">
            <div class="gen-step-label"><span class="gen-step-num" id="gen-step-cust">1</span>Customer</div>
            <div id="gen-cust"></div>
          </div>
          <div class="gen-step">
            <label class="gen-step-label" for="gen-rep"><span class="gen-step-num" id="gen-step-rep">2</span>Sales rep</label>
            <div class="field"><select id="gen-rep"><option value="">Choose a sales rep…</option>${reps.map((r) =>
              `<option value="${r.id}" ${repPick === r.id ? 'selected' : ''}>${esc(r.name)}</option>`).join('')}</select></div>
          </div>
          <div class="gen-note">${ICONS.info}<span>Creates one quotation with ${plural(t.lines, 'line')}. Cancelling the pick list deletes it again.</span></div>
        </div>
      </section>
    </div>
    <div id="gen-error"></div>`,
  `<span class="gen-status" id="gen-status"></span><span class="om-spacer"></span>
    <button class="btn btn-text" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" id="gen-create" disabled>Create pick list</button>`), 'gen');

  const filter = document.getElementById('gen-lines-filter');
  if (filter) {
    const haystack = preview.lines.map((g) => [g.description, g.title, g.sku, g.barcode].join(' ').toLowerCase());
    filter.addEventListener('input', () => {
      const q = filter.value.trim().toLowerCase();
      let shown = 0;
      document.querySelectorAll('#gen-lines tr').forEach((tr) => {
        const hit = !q || haystack[tr.dataset.i].includes(q);
        tr.hidden = !hit;
        if (hit) shown += 1;
      });
      document.getElementById('gen-lines-count').textContent = q ? `${shown} of ${plural(t.lines, 'product')}` : plural(t.lines, 'product');
      document.getElementById('gen-lines-none').hidden = shown > 0;
    });
  }

  const repSel = document.getElementById('gen-rep');
  const createBtn = document.getElementById('gen-create');
  const custBox = document.getElementById('gen-cust');
  let customer = lastCustomer;
  let picking = !customer;
  let busy = false;

  const markStep = (id, done, n) => {
    const el = document.getElementById(id);
    el.classList.toggle('done', done);
    el.innerHTML = done ? ICONS.check : n;
  };
  const sync = () => {
    const custOk = !!customer && !picking;
    const repOk = !!repSel.value;
    createBtn.disabled = busy || !custOk || !repOk;
    markStep('gen-step-cust', custOk, 1);
    markStep('gen-step-rep', repOk, 2);
    const status = document.getElementById('gen-status');
    status.classList.toggle('ready', custOk && repOk);
    status.innerHTML = !custOk ? 'Choose a customer to continue'
      : !repOk ? 'Choose a sales rep to continue'
        : `${ICONS.ok}<span>${plural(t.orders, 'order')} · ${plural(t.units, 'unit')} for <strong>${esc(customer.name)}</strong></span>`;
  };

  const showChosen = () => {
    picking = false;
    custBox.innerHTML = `<div class="gen-chosen">
      <div class="gen-chosen-info"><div class="om-strong">${esc(customer.name)}</div>
        <div class="hint">${customerMeta(customer)}${lastCustomer && customer.id === lastCustomer.id ? ' · last used' : ''}</div></div>
      <button class="btn btn-outlined btn-small" id="gen-cust-change">Change</button></div>`;
    document.getElementById('gen-cust-change').addEventListener('click', showPicker);
    sync();
  };

  let results = [];
  let active = -1;
  let custSeq = 0;
  let timer;
  const choose = (c) => {
    customer = c;
    showChosen();
    if (!repSel.value) repSel.focus();
  };
  const renderResults = () => {
    const list = document.getElementById('gen-cust-list');
    if (!list) return;
    list.innerHTML = results.length
      ? results.map((c, i) => `<button type="button" class="gen-cust-opt ${i === active ? 'active' : ''}" data-i="${i}" role="option" aria-selected="${i === active}">
          <span class="om-strong">${esc(c.name)}</span><span class="hint">${customerMeta(c)}</span></button>`).join('')
      : '<div class="gen-msg">No customers match.</div>';
    const on = list.querySelector('.active');
    if (on) on.scrollIntoView({ block: 'nearest' });
  };
  const searchCustomers = async (q) => {
    const seq = ++custSeq;
    let list;
    try {
      list = await api(`/api/picklists/customers?q=${encodeURIComponent(q)}`);
    } catch (err) {
      if (seq !== custSeq) return;
      const box = document.getElementById('gen-cust-list');
      if (box) box.innerHTML = `<div class="gen-msg error">${esc(err.message)}</div>`;
      return;
    }
    if (seq !== custSeq) return;
    if (lastCustomer && !q && !list.some((c) => c.id === lastCustomer.id)) list.unshift(lastCustomer);
    results = list;
    const current = customer ? results.findIndex((c) => c.id === customer.id) : -1;
    active = q ? 0 : current;
    renderResults();
  };
  function showPicker() {
    picking = true;
    custBox.innerHTML = `<label class="gen-search">${ICONS.search}<input id="gen-cust-q" placeholder="Search business name or account #" autocomplete="off" aria-label="Search customers"></label>
      <div class="gen-cust-list" id="gen-cust-list" role="listbox"><div class="gen-msg"><span class="spinner"></span> Loading customers…</div></div>
      ${customer ? `<button type="button" class="link-btn gen-keep" id="gen-cust-keep">Keep ${esc(customer.name)}</button>` : ''}`;
    const input = document.getElementById('gen-cust-q');
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => searchCustomers(input.value.trim()), 250);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!results.length) return;
        active = Math.max(0, Math.min(results.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
        renderResults();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const c = results[active] || (results.length === 1 ? results[0] : null);
        if (c) choose(c);
      } else if (e.key === 'Escape') {
        if (customer) showChosen();
        else closeModal();
      }
    });
    document.getElementById('gen-cust-list').addEventListener('click', (e) => {
      const opt = e.target.closest('.gen-cust-opt');
      if (opt) choose(results[Number(opt.dataset.i)]);
    });
    const keep = document.getElementById('gen-cust-keep');
    if (keep) keep.addEventListener('click', showChosen);
    input.focus();
    sync();
    searchCustomers('');
  }

  repSel.addEventListener('change', sync);
  if (picking) showPicker();
  else showChosen();

  createBtn.addEventListener('click', async () => {
    const repId = Number(repSel.value);
    const repName = repSel.selectedOptions[0].textContent;
    const errBox = document.getElementById('gen-error');
    errBox.innerHTML = '';
    busy = true;
    sync();
    createBtn.innerHTML = '<span class="spinner"></span> Creating…';
    try {
      const res = await api('/api/picklists', {
        method: 'POST',
        body: { ...body, customer_id: customer.id, customer_name: customer.name, sales_rep_id: repId },
      });
      remember(CUSTOMER_KEY, customer);
      remember(REP_KEY, repId);
      loadOrders();
      showCreated(res, customer.name, repName);
    } catch (err) {
      busy = false;
      createBtn.textContent = 'Create pick list';
      sync();
      const names = err.data && err.data.orders ? ` (${err.data.orders.join(', ')})` : '';
      errBox.innerHTML = `<div class="error-note">${esc(err.message + names)}</div>`;
      errBox.scrollIntoView({ block: 'nearest' });
      if (err.status === 409) loadOrders();
    }
  });
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const area = document.createElement('textarea');
  area.value = text;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  const ok = document.execCommand('copy');
  area.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error('Copy failed'));
}

function showCreated(res, customerName, repName) {
  const t = res.totals;
  openModal(`
    <div class="gen-done">
      <div class="gen-done-icon">${ICONS.check}</div>
      <div class="gen-done-title">Pick list #${res.id} created</div>
      <div class="text-secondary">${esc(customerName)} · ${esc(repName)}</div>
      <div class="gen-done-number">
        <div class="om-label">BackOffice quotation</div>
        <div class="gen-done-row"><span class="gen-qnum">${esc(res.quotation_number)}</span>
          <button class="btn btn-outlined btn-small" id="gen-copy">${ICONS.copy} Copy</button></div>
      </div>
      <div class="gen-done-stats">${plural(t.orders, 'order')} · ${plural(t.units, 'unit')} · ${plural(t.lines, 'product')}</div>
      ${res.excluded.length ? `<div class="hint">Left out: ${res.excluded.map((e) => esc(e.name)).join(', ')}</div>` : ''}
      ${taggingHtml(res.tagging, res.id)}
    </div>
    <div class="om-foot"><span class="om-spacer"></span>
      <button class="btn btn-outlined" id="gen-view">View pick list</button>
      <button class="btn btn-primary" id="gen-done" onclick="closeModal()">Done</button>
    </div>`, 'gen gen-small');
  document.getElementById('gen-done').focus();
  document.getElementById('gen-view').addEventListener('click', () => viewPicklist(res.id));
  const copy = document.getElementById('gen-copy');
  copy.addEventListener('click', () => {
    copyText(res.quotation_number)
      .then(() => { copy.innerHTML = `${ICONS.check} Copied`; })
      .catch(() => snackbar('Could not copy — select the number instead', 'error'));
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
    <td>${STATUS_CHIP[p.status] || esc(p.status)}${p.cancelled_at ? `<div class="hint">${esc(p.cancelled_at)}</div>` : ''}
      ${p.tag_failed ? `<div><span class="chip static err" title="Open the pick list to see why and retry">${p.status === 'cancelled' ? 'Tag left on' : 'Tag missing on'} ${plural(p.tag_failed, 'order')}</span></div>` : ''}</td>
    <td>
      <button class="btn btn-text btn-small" data-view="${p.id}">View</button>
      ${p.status !== 'cancelled' ? `<button class="btn btn-danger btn-small" data-cancel="${p.id}" data-number="${attr(p.quotation_number || '')}" data-tag="${attr(p.shopify_tag || '')}">Cancel</button>` : ''}
    </td>
  </tr>`).join('');
}

document.getElementById('pl-history').addEventListener('click', (e) => {
  const view = e.target.closest('[data-view]');
  if (view) viewPicklist(Number(view.dataset.view));
  const cancel = e.target.closest('[data-cancel]');
  if (cancel) confirmCancel(Number(cancel.dataset.cancel), cancel.dataset.number, cancel.dataset.tag);
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
    <h3 data-detail="${p.id}">Pick list #${p.id} ${STATUS_CHIP[p.status] || ''}</h3>
    <div class="hint mb-16">Quotation ${esc(p.quotation_number || '—')} · ${esc(p.customer_name || '')} · ${esc(p.sales_rep_name || '')} · ${esc(p.created_at)} by ${esc(p.created_by || '')}${p.shopify_tag ? ` · Shopify tag <strong>${esc(p.shopify_tag)}</strong>` : ''}</div>
    ${p.tag_failed ? `<div class="mb-16">${taggingHtml(detailTagging(p), p.id)}</div>` : ''}
    <h4 class="mb-8">Products (${p.lines.length})</h4>
    <div class="pl-preview-lines mb-16"><table>
      <thead><tr><th>Barcode</th><th>Product</th><th>SKU</th><th class="num">Qty</th></tr></thead>
      <tbody>${p.lines.map((l) => `<tr><td>${esc(l.barcode)}</td><td class="ellip" style="max-width:320px">${esc(l.description || '')}</td><td>${esc(l.sku || '')}</td><td class="num"><strong>${l.qty}</strong></td></tr>`).join('')}</tbody>
    </table></div>
    <h4 class="mb-8">Orders (${p.orders.length})</h4>
    <div class="pl-preview-lines"><table>
      <thead><tr><th>Order</th><th>Store</th><th>Customer</th><th>State</th><th class="num">Units</th>${p.shopify_tag ? '<th>Shopify tag</th>' : ''}</tr></thead>
      <tbody>${p.orders.map((o) => `<tr><td>${esc(o.order_name)}</td><td>${esc(o.store_name || '')}</td><td>${esc(o.customer_name || '')}</td><td>${esc(o.ship_state || '')}</td><td class="num">${o.units}</td>${p.shopify_tag ? `<td>${tagStateHtml(o)}</td>` : ''}</tr>`).join('')}</tbody>
    </table></div>
    <div class="actions"><button class="btn btn-text" onclick="closeModal()">Close</button></div>`, 'wide');
}

function confirmCancel(id, number, tag) {
  openModal(`
    <h3>Cancel pick list #${id}?</h3>
    <p>${number ? `Quotation <strong>${esc(number)}</strong> and its lines will be <strong>deleted from BackOffice</strong>. ` : ''}${tag ? `The tag <strong>${esc(tag)}</strong> is removed from its Shopify orders. ` : ''}Its orders become available for a new pick list again.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Keep it</button>
      <button class="btn btn-danger" id="m-cancel-confirm">Cancel pick list</button>
    </div>`);
  document.getElementById('m-cancel-confirm').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      const res = await api(`/api/picklists/${id}/cancel`, { method: 'POST' });
      loadHistory();
      loadOrders();
      const t = res.tagging;
      const done = `Pick list #${id} cancelled${res.quotation_deleted ? ' — quotation deleted' : ''}`;
      if (t && (t.error || t.failed.length)) {
        // The cancel itself went through; what is left is the tag on some orders.
        openModal(`<h3>${esc(done)}</h3>${taggingHtml(t, id)}
          <div class="actions"><button class="btn btn-primary" onclick="closeModal()">Close</button></div>`);
        return;
      }
      closeModal();
      snackbar(t ? `${done}, tag removed from ${plural(t.done, 'order')}` : done, 'success');
    } catch (err) {
      e.target.disabled = false;
      snackbar(err.message, 'error');
    }
  });
}

/* ---------- Shopify tag "picklist <quotation number>" ----------
   Added to every order when a pick list is created, removed when it is
   cancelled. Orders Shopify refused are listed with the reason and a Retry. */
const TAG_STATE = {
  tagged: ['ok', 'Tagged'],
  pending: ['warn', 'Waiting'],
  failed: ['err', 'Not tagged'],
  removed: ['', 'Removed'],
  remove_failed: ['err', 'Still tagged'],
};

function tagStateHtml(o) {
  if (!o.tag_state) return '<span class="text-secondary">—</span>';
  const [tone, text] = TAG_STATE[o.tag_state] || ['', o.tag_state];
  return `<span class="chip static ${tone}"${o.tag_error ? ` title="${attr(o.tag_error)}"` : ''}>${text}</span>`
    + (o.tag_error ? `<div class="hint">${esc(o.tag_error)}</div>` : '');
}

/* The detail view's orders as a tag pass result, for the failure panel. */
function detailTagging(p) {
  const bad = p.orders.filter((o) => o.tag_state === 'failed' || o.tag_state === 'remove_failed');
  return {
    tag: p.shopify_tag, mode: p.status === 'cancelled' ? 'remove' : 'add', done: 0, error: null,
    failed: bad.map((o) => ({ order_name: o.order_name, store_name: o.store_name, error: o.tag_error || 'Unknown error' })),
  };
}

function taggingHtml(t, picklistId, retried = false) {
  if (!t) return '';
  const adding = t.mode === 'add';
  if (!t.error && !t.failed.length) {
    // A retry only touches the orders that were left, so it speaks for the whole list.
    const text = retried
      ? `Tag <strong>${esc(t.tag)}</strong> ${adding ? 'is now on' : 'is now off'} every order`
      : adding
        ? `Tagged ${plural(t.done, 'Shopify order')} <strong>${esc(t.tag)}</strong>`
        : `Tag <strong>${esc(t.tag)}</strong> removed from ${plural(t.done, 'Shopify order')}`;
    return `<div class="pl-tagging ok">${ICONS.ok}<span>${text}</span></div>`;
  }
  const head = t.error
    ? esc(t.error)
    : `Tag <strong>${esc(t.tag)}</strong> couldn't be ${adding ? 'added to' : 'removed from'} ${plural(t.failed.length, 'order')}${t.done ? ` · ${t.done} done` : ''}`;
  return `<div class="pl-tagging err">${ICONS.warn}<div class="pl-tagging-body">
    <div class="pl-tagging-head">${head}</div>
    ${t.failed.length ? `<ul>${t.failed.map((f) => `<li><strong>${esc(f.order_name)}</strong>${f.store_name ? ` · ${esc(f.store_name)}` : ''} — ${esc(f.error)}</li>`).join('')}</ul>` : ''}
    <div class="pl-tagging-foot"><button class="btn btn-outlined btn-small" data-tag-retry="${picklistId}">Retry</button>
      <span class="hint">${adding ? 'The pick list and quotation are fine; only the tag is missing.' : 'The pick list is cancelled; only the tag is left on.'}</span></div>
  </div></div>`;
}

document.getElementById('modal').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-tag-retry]');
  if (!btn) return;
  const id = Number(btn.dataset.tagRetry);
  const panel = btn.closest('.pl-tagging');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Retrying…';
  try {
    const res = await api(`/api/picklists/${id}/tags/retry`, { method: 'POST' });
    if (document.querySelector('#modal [data-detail]')) viewPicklist(id); // the order rows show the new states
    else panel.outerHTML = taggingHtml(res, id, true);
    if (document.getElementById('tab-history').style.display !== 'none') loadHistory();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Retry';
    snackbar(err.message, 'error');
  }
});

loadStores();
