const STORES_KEY = 'easyship.picklistStores';
const RULES_KEY = 'easyship.picklistRules';
const CUSTOMER_KEY = 'easyship.picklistCustomer';
const REP_KEY = 'easyship.picklistRep';
const SORT_KEY = 'easyship.picklistSort';
const PRESET_KEY = 'easyship.picklistPreset';
const EXCLUDED_KEY = 'easyship.picklistExcluded';
// From this many days old an order's age is called out in the list.
const AGE_WARN_DAYS = 3;

let stores = [];
let selectedStores = new Set();
let orders = [];
let rules = [];
// Rule id → how it is switched on: 'add' (Show: orders matching any Show rule
// stay), 'narrow' (Require: orders must also match it) or 'hide'.
let ruleModes = new Map();
// Presets are saved views: while one is on it decides alone and the rules pause.
let presets = [];
let activePreset = null;
// Filter key → the chosen values, lower-cased; an order must carry one of them.
const facets = new Map();
let hiddenTags = new Set();
// Search and Needs review only change what the table shows; the view, rules,
// filters and exclusions decide what goes on the pick list.
let reviewOnly = false;
const excluded = new Set(recall(EXCLUDED_KEY, []));
const checked = new Set();
let lastChecked = null; // anchor for Shift+click ranges
let cursorKey = null; // the row J / K / X / E work on
let lastMove = null; // what Undo puts back
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
const STORE_COLORS = ['#2563eb', '#d97706', '#16a34a', '#9333ea', '#db2777', '#0891b2', '#65a30d', '#dc2626'];
const storeColor = (id) => {
  const i = stores.findIndex((s) => s.id === id);
  return i < 0 ? 'var(--outline-strong)' : STORE_COLORS[i % STORE_COLORS.length];
};

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

/* ---------- Keyboard ----------
   In the list: J / K move, X selects, Enter opens, E excludes (or restores),
   ⌘Z / Ctrl+Z undoes, / finds. In the order window: ← → and E. */
document.addEventListener('keydown', (e) => {
  const modalOpen = document.getElementById('modal-backdrop').classList.contains('show');
  if (e.key === 'Escape' && pop) {
    closePop();
    return;
  }
  if (e.target instanceof Element && e.target.closest('input, select, textarea')) return; // typing, not a shortcut
  const listing = !modalOpen && document.getElementById('tab-new').style.display !== 'none';
  const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === 'z' && lastMove && document.getElementById('tab-new').style.display !== 'none') {
    e.preventDefault();
    undoMove();
    return;
  }
  if (listing && plain) {
    const key = e.key.toLowerCase();
    if (e.key === '/') {
      e.preventDefault();
      document.getElementById('qf-search').focus();
      return;
    }
    if (key === 'j' || key === 'k') {
      e.preventDefault();
      moveCursor(key === 'j' ? 1 : -1);
      return;
    }
    // Enter on a focused button or link presses it, not the keyboard row.
    const pressing = key === 'enter' && e.target instanceof Element && e.target.closest('button, a');
    if (cursorKey && !pressing && (key === 'x' || key === 'enter' || key === 'o' || (key === 'e' && !e.repeat))) {
      const row = document.querySelector(`#pl-orders [data-check="${CSS.escape(cursorKey)}"]`);
      if (!row) return;
      e.preventDefault();
      if (key === 'x') toggleChecked(cursorKey, !checked.has(cursorKey), e.shiftKey);
      else if (key === 'e') moveRows([cursorKey]);
      else showOrder(cursorKey);
      return;
    }
  }
  if (e.key === 'Escape' && !modalOpen && checked.size) clearChecked();
  if (e.key === 'Escape') closeModal();
  if (!modalOrderKey) return;
  if (e.key === 'ArrowLeft') stepOrder(-1);
  if (e.key === 'ArrowRight') stepOrder(1);
  // E = the window's Exclude (or, in the Excluded view, Restore) button; a held key fires once.
  if ((e.key === 'e' || e.key === 'E') && !e.repeat && plain) {
    e.preventDefault();
    moveAndAdvance(modalOrderKey);
  }
});

function moveCursor(delta) {
  const keys = listedOrders().map(orderKey);
  if (!keys.length) return;
  const at = keys.indexOf(cursorKey);
  cursorKey = keys[at < 0 ? (delta > 0 ? 0 : keys.length - 1) : Math.max(0, Math.min(keys.length - 1, at + delta))];
  render();
  const row = document.querySelector('#pl-orders tr.cursor');
  if (row) row.scrollIntoView({ block: 'nearest' });
}

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
    return `<button class="pl-store-pill ${on ? 'on' : ''}" data-store="${s.id}" aria-pressed="${on}" style="--dot:${storeColor(s.id)}">
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
let backofficeError = null;

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
  hiddenTags = new Set((res.hidden_tags || []).map((t) => t.toLowerCase()));
  backofficeError = res.backoffice_error || null;
  const savedPreset = recall(PRESET_KEY, null);
  activePreset = presets.some((p) => p.id === savedPreset) ? savedPreset : null;
  forgetGoneExclusions(res.errors);
  ruleModes = restoreRuleModes(recall(RULES_KEY, {}));
  showLoadErrors(res.errors);
  render();
}

/* Exclusions are kept per computer; one is forgotten once its order has left
   the list (fulfilled, put on a pick list) — but only for stores that loaded. */
function forgetGoneExclusions(errors) {
  if (errors.some((e) => !e.store_id)) return;
  const failed = new Set(errors.map((e) => e.store_id));
  const known = new Set(orders.map(orderKey));
  [...excluded].forEach((k) => {
    const store = Number(k.split('|')[0]);
    if (selectedStores.has(store) && !failed.has(store) && !known.has(k)) excluded.delete(k);
  });
  remember(EXCLUDED_KEY, [...excluded]);
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
    </div>`).join('')
    + (backofficeError && orders.length ? `<div class="rule-banner warn"><span><strong>Barcodes weren’t checked against BackOffice</strong>
      — ${esc(backofficeError)}. Products BackOffice doesn’t know only show up at Generate.</span></div>` : '');
  el.querySelectorAll('.pl-retry').forEach((b) => b.addEventListener('click', loadOrders));
  if (errors.length) {
    const names = errors.map((e) => (e.store_id ? e.store_name || storeName(e.store_id) : null)).filter(Boolean);
    snackbar(names.length ? `Couldn’t load orders from ${names.join(', ')}` : 'Couldn’t load orders', 'error');
  }
}

/* Saved per computer as {id: mode}; an older save (a plain list of ids) means
   every listed rule was on in its only mode. */
function restoreRuleModes(saved) {
  const modes = new Map();
  const entries = Array.isArray(saved) ? saved.map((id) => [id, null]) : Object.entries(saved || {});
  entries.forEach(([id, mode]) => {
    const rule = rules.find((r) => r.id === id);
    if (!rule) return;
    modes.set(id, rule.action === 'hide' ? 'hide' : mode === 'narrow' ? 'narrow' : 'add');
  });
  return modes;
}

/* ---------- Views (presets) ---------- */
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
  render();
}

/* "All orders" (the rules in use) plus one tab per preset, each with how many
   orders it would put on the pick list. */
function renderViewTabs() {
  const row = document.getElementById('pl-viewtabs-row');
  row.hidden = !presets.length;
  if (!presets.length) return;
  const count = (id) => orders.filter((o) => inScope(o, id) && !excluded.has(orderKey(o))).length;
  document.getElementById('pl-viewtabs').innerHTML = [{ id: null, name: 'All orders' }, ...presets].map((t) => {
    const on = t.id === activePreset;
    return `<button class="pl-vtab${on ? ' on' : ''}" role="tab" aria-selected="${on}" data-preset="${attr(t.id || '')}">
      ${t.id ? PRESET_ICON : ''}${esc(t.name)}<span class="pl-vtab-n">${count(t.id)}</span></button>`;
  }).join('');
  const preset = presets.find((p) => p.id === activePreset);
  const desc = document.getElementById('pl-preset-desc');
  desc.hidden = !preset;
  desc.innerHTML = preset ? `<span class="pl-pdesc-lead">Orders that match</span> ${presetExpressionHtml(preset)}` : '';
}
document.getElementById('pl-viewtabs').addEventListener('click', (e) => {
  const tab = e.target.closest('[data-preset]');
  if (tab) setPreset(tab.dataset.preset || null);
});

/* ---------- Filters ---------- */
function shippingLabel(method) {
  const short = (method || '').replace(/\s*[([].*$/, '').replace(/\s+shipping$/i, '').trim();
  return short || method || '';
}
const FAST_SHIPPING = /expedit|express|priority|overnight|next[\s-]?day|2[\s-]?day|two[\s-]?day|rush/i;

const FACETS = [
  { key: 'state', label: 'State', values: (o) => (o.ship_state ? [o.ship_state.toUpperCase()] : []) },
  { key: 'tag', label: 'Order tag', values: (o) => o.tags },
  { key: 'ctag', label: 'Customer tag', values: (o) => o.customer_tags },
  { key: 'shipping', label: 'Shipping', values: (o) => (o.shipping_method ? [shippingLabel(o.shipping_method)] : []) },
];
const facetDef = (key) => FACETS.find((f) => f.key === key);

function rulesPass(o, presetId) {
  const preset = presets.find((p) => p.id === presetId);
  if (preset) return presetMatches(preset, o);
  const inMode = (mode) => [...ruleModes].filter(([, m]) => m === mode).map(([id]) => id);
  const matches = (id) => o.rule_ids.includes(id);
  if (inMode('hide').some(matches)) return false;
  const shows = inMode('add');
  if (shows.length && !shows.some(matches)) return false;
  return inMode('narrow').every(matches);
}

/* Within a filter any chosen value will do; every filter in use must pass. */
function facetsPass(o, skip) {
  for (const [key, chosen] of facets) {
    if (key === skip || !chosen.size) continue;
    if (!facetDef(key).values(o).some((v) => chosen.has(v.toLowerCase()))) return false;
  }
  return true;
}

const inScope = (o, presetId = activePreset) => rulesPass(o, presetId) && facetsPass(o);
const searchText = () => document.getElementById('qf-search').value.trim().toLowerCase();
const facetsInUse = () => [...facets.values()].some((s) => s.size);

/* A filter's values with how many orders each would leave, given everything
   else in use; chosen values stay listed even when nothing has them now. */
function facetValues(def) {
  const labels = new Map();
  const counts = new Map();
  orders.forEach((o) => {
    const keys = new Set();
    def.values(o).forEach((v) => {
      const k = v.toLowerCase();
      if (!labels.has(k)) labels.set(k, v);
      keys.add(k);
    });
    if (rulesPass(o, activePreset) && facetsPass(o, def.key)) keys.forEach((k) => counts.set(k, (counts.get(k) || 0) + 1));
  });
  (facets.get(def.key) || new Set()).forEach((k) => { if (!counts.has(k)) counts.set(k, 0); });
  return [...counts].map(([key, n]) => ({ key, n, label: labels.get(key) || key }))
    .sort((a, b) => b.n - a.n || collator.compare(a.label, b.label));
}

function facetLabel(def, key) {
  for (const o of orders) {
    const hit = def.values(o).find((v) => v.toLowerCase() === key);
    if (hit) return hit;
  }
  return key;
}

const CHEVRON = '<svg class="ico pl-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const FILTER_ICON = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/></svg>';
const RULE_LABEL = { add: 'Show', narrow: 'Require', hide: 'Hide' };
const shortList = (labels) => (labels.length > 2 ? `${labels.slice(0, 2).join(', ')} +${labels.length - 2}` : labels.join(', '));

/* One bar: a button per filter, which turns into "State: TX, CA ×" once in use. */
function renderFilterBar() {
  const bar = document.getElementById('pl-filterbar');
  const button = (key, label, value, title, extra = '') => {
    const open = pop && pop.key === key;
    if (!value) {
      return `<button class="pl-fbtn${extra}" data-pop="${key}" aria-haspopup="true" aria-expanded="${open}">${label}${CHEVRON}</button>`;
    }
    return `<span class="pl-fbtn on${extra}"><button class="pl-fbtn-main" data-pop="${key}" aria-haspopup="true" aria-expanded="${open}" title="${attr(title)}">
      ${label}: <strong>${esc(value)}</strong>${CHEVRON}</button><button class="pl-fbtn-x" data-clear="${key}" aria-label="Clear ${attr(label)}" title="Clear">×</button></span>`;
  };
  const parts = FACETS.map((def) => {
    const labels = [...(facets.get(def.key) || [])].map((k) => facetLabel(def, k));
    return button(def.key, def.label, shortList(labels), `${def.label}: ${labels.join(', ')}`);
  });
  if (rules.length) {
    const on = rules.filter((r) => ruleModes.has(r.id));
    const names = on.map((r) => (ruleModes.get(r.id) === 'hide' ? `not ${r.name}` : r.name));
    const title = on.map((r) => `${RULE_LABEL[ruleModes.get(r.id)]}: ${r.name}`).join(' · ');
    const paused = activePreset && on.length;
    parts.push(button('rules', 'Rules', paused ? `${on.length} paused` : shortList(names),
      paused ? `Paused while a saved view is on — ${title}` : title, paused ? ' paused' : ''));
  }
  const any = facetsInUse() || ruleModes.size;
  bar.innerHTML = `<span class="pl-fbar-icon" aria-hidden="true">${FILTER_ICON}</span>${parts.join('')}`
    + (any ? '<button class="link-btn pl-clear-filters" id="pl-clear-filters">Clear filters</button>' : '');
}

document.getElementById('pl-filterbar').addEventListener('click', (e) => {
  const clear = e.target.closest('[data-clear]');
  if (clear) {
    if (clear.dataset.clear === 'rules') setRuleModes(new Map());
    else facets.delete(clear.dataset.clear);
    if (pop && pop.key === clear.dataset.clear) closePop();
    render();
    return;
  }
  const opener = e.target.closest('[data-pop]');
  if (opener) {
    if (pop && pop.key === opener.dataset.pop) closePop();
    else openPop(opener.dataset.pop, opener);
    return;
  }
  if (e.target.closest('#pl-clear-filters')) {
    facets.clear();
    setRuleModes(new Map());
    closePop();
    render();
  }
});

function setRuleModes(next) {
  ruleModes = next;
  remember(RULES_KEY, Object.fromEntries(ruleModes));
}

/* ---------- Filter menus ---------- */
let pop = null; // { key, query }

function openPop(key, anchor) {
  pop = { key, query: '' };
  const el = document.getElementById('pl-pop');
  el.hidden = false;
  el.innerHTML = key === 'rules' ? rulesPopHtml() : facetPopHtml(facetDef(key));
  renderPopList();
  const card = document.getElementById('pl-filters').getBoundingClientRect();
  const at = anchor.getBoundingClientRect();
  const left = Math.max(8, Math.min(at.left - card.left, card.width - el.offsetWidth - 8));
  el.style.left = `${left}px`;
  el.style.top = `${at.bottom - card.top + 6}px`;
  renderFilterBar();
  const search = document.getElementById('pl-pop-q');
  (search || el.querySelector('button')).focus();
}

function closePop() {
  if (!pop) return;
  pop = null;
  document.getElementById('pl-pop').hidden = true;
  renderFilterBar();
}

document.addEventListener('mousedown', (e) => {
  if (pop && !e.target.closest('#pl-pop, [data-pop]')) closePop();
});

function facetPopHtml(def) {
  const many = facetValues(def).length > 8;
  return `<div class="pl-pop-head"><strong>${def.label}</strong><button class="link-btn" data-pop-clear hidden>Clear</button></div>
    ${many ? `<label class="pl-pop-search">${ICONS.search}<input id="pl-pop-q" placeholder="Search ${attr(def.label.toLowerCase())}s" autocomplete="off" aria-label="Search ${attr(def.label)}"></label>` : ''}
    <div class="pl-pop-list" id="pl-pop-list" role="group" aria-label="${attr(def.label)}"></div>`;
}

function rulesPopHtml() {
  return `<div class="pl-pop-head"><strong>Rules</strong><button class="link-btn" data-pop-clear hidden>Clear</button></div>
    <div class="pl-pop-note" id="pl-pop-note" hidden></div>
    <div class="pl-pop-list pl-pop-rules" id="pl-pop-list"></div>
    <div class="pl-pop-foot"><strong>Show</strong> orders matching any Show rule · <strong>Require</strong> a match ·
      <strong>Hide</strong> takes matches out</div>`;
}

/* Redraws the open menu's options in place, so its search box keeps focus. */
function renderPopList() {
  if (!pop) return;
  const list = document.getElementById('pl-pop-list');
  const clear = document.querySelector('#pl-pop [data-pop-clear]');
  if (pop.key === 'rules') {
    clear.hidden = !ruleModes.size;
    const note = document.getElementById('pl-pop-note');
    const preset = presets.find((p) => p.id === activePreset);
    note.hidden = !preset;
    note.innerHTML = preset ? `The <strong>${esc(preset.name)}</strong> view decides on its own; choosing a rule switches to All orders.` : '';
    list.innerHTML = rules.map((r) => {
      const current = ruleModes.get(r.id) || '';
      const modes = r.action === 'hide' ? ['', 'hide'] : ['', 'add', 'narrow'];
      return `<div class="pl-rule-row"><span class="pl-rule-name" title="${attr(r.name)}">${esc(r.name)}</span>
        <div class="pl-seg" role="radiogroup" aria-label="${attr(r.name)}">${modes.map((m) => `<button type="button" role="radio"
          aria-checked="${m === current}" class="${m === current ? 'on' : ''}${m ? ` m-${m}` : ''}" data-rule="${attr(r.id)}" data-mode="${m}">${m ? RULE_LABEL[m] : 'Off'}</button>`).join('')}</div></div>`;
    }).join('');
    return;
  }
  const def = facetDef(pop.key);
  const chosen = facets.get(def.key) || new Set();
  clear.hidden = !chosen.size;
  const q = pop.query.toLowerCase();
  const values = facetValues(def).filter((v) => !q || v.label.toLowerCase().includes(q));
  const tick = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
  list.innerHTML = values.length ? values.map((v) => {
    const on = chosen.has(v.key);
    return `<button type="button" class="pl-pop-opt${on ? ' on' : ''}${v.n ? '' : ' none'}" role="menuitemcheckbox" aria-checked="${on}" data-value="${attr(v.key)}">
      <span class="pl-box">${on ? tick : ''}</span><span class="pl-pop-name">${esc(v.label)}</span><span class="pl-pop-n">${v.n}</span></button>`;
  }).join('') : `<div class="pl-pop-empty">${q ? 'Nothing matches' : 'No values in these orders'}</div>`;
}

document.getElementById('pl-pop').addEventListener('input', (e) => {
  if (e.target.id !== 'pl-pop-q') return;
  pop.query = e.target.value.trim();
  renderPopList();
});
document.getElementById('pl-pop').addEventListener('click', (e) => {
  if (e.target.closest('[data-pop-clear]')) {
    if (pop.key === 'rules') setRuleModes(new Map());
    else facets.delete(pop.key);
    render();
    return;
  }
  const opt = e.target.closest('[data-value]');
  if (opt) {
    const chosen = new Set(facets.get(pop.key) || []);
    if (chosen.has(opt.dataset.value)) chosen.delete(opt.dataset.value);
    else chosen.add(opt.dataset.value);
    facets.set(pop.key, chosen);
    render();
    return;
  }
  const seg = e.target.closest('[data-rule]');
  if (seg) {
    if (activePreset) {
      activePreset = null;
      remember(PRESET_KEY, null);
      snackbar('Switched to All orders — the rules work again');
    }
    const next = new Map(ruleModes);
    if (seg.dataset.mode) next.set(seg.dataset.rule, seg.dataset.mode);
    else next.delete(seg.dataset.rule);
    setRuleModes(next);
    render();
  }
});

/* ---------- Flags ----------
   What to look at before an order goes on a pick list. */
const PICKLIST_TAG = /^picklist\s+\d+$/i;
const FLAG_ICONS = {
  block: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/></svg>',
  risk: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>',
  tag: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12.59 2.59A2 2 0 0 0 11.17 2H4a2 2 0 0 0-2 2v7.17a2 2 0 0 0 .59 1.42l8.7 8.7a2.43 2.43 0 0 0 3.42 0l6.58-6.58a2.43 2.43 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/></svg>',
};

const ISSUE_SHORT = { no_barcode: 'No barcode', unknown_barcode: 'Not in BackOffice' };

/* Each flag: a label, a short reason that fits the Needs review column, and the full detail. */
function orderFlags(o) {
  const flags = [];
  if (o.issues && o.issues.length) {
    const first = o.issues[0];
    const title = first.text.split(/ has no barcode| — barcode /)[0];
    flags.push({ tone: 'err', icon: 'block', label: 'Will be left out at Generate',
      short: `${ISSUE_SHORT[first.kind] || 'Left out'}: ${title}${o.issues.length > 1 ? ` +${o.issues.length - 1}` : ''}`,
      detail: o.issues.map((i) => i.text).join('; ') });
  }
  if (o.risk_level === 'HIGH' || o.risk_level === 'MEDIUM') {
    const advice = RECOMMENDATION_TEXT[o.risk_recommendation] || 'Shopify flagged this order';
    flags.push({ tone: o.risk_level === 'HIGH' ? 'err' : 'warn', icon: 'risk', label: `${titleCase(o.risk_level)} risk`,
      short: `${titleCase(o.risk_level)} risk`, detail: advice });
  }
  const tag = o.tags.find((t) => PICKLIST_TAG.test(t.trim()));
  if (tag) {
    flags.push({ tone: 'warn', icon: 'tag', label: 'Pick list tag', short: `Tagged “${tag}”`,
      detail: `Tagged “${tag}” — it may already be on a pick list` });
  }
  return flags;
}

/* Each flag spelled out: short in the Needs review column, in full in the order window. */
function flagReasonsHtml(o, full = false) {
  return orderFlags(o).map((f) => `<span class="pl-why ${f.tone}" title="${attr(`${f.label}: ${f.detail}`)}">${FLAG_ICONS[f.icon]}${
    full ? `<strong>${esc(f.label)}</strong><span class="pl-why-detail">${esc(f.detail)}</span>` : `<strong class="pl-why-short">${esc(f.short)}</strong>`}</span>`).join('');
}

function flagsHtml(o) {
  return orderFlags(o).map((f) => {
    const text = attr(`${f.label}: ${f.detail}`);
    return `<span class="pl-flag ${f.tone}" role="img" title="${text}" aria-label="${text}">${FLAG_ICONS[f.icon]}</span>`;
  }).join('');
}

document.getElementById('pl-review').addEventListener('click', () => {
  reviewOnly = !reviewOnly;
  render();
});

/* ---------- Sorting ---------- */
/* Column sort, remembered per computer; the order window walks the same order. */
const visibleTags = (tags) => tags.filter((t) => !hiddenTags.has(t.toLowerCase()));
const SORT_VALUE = {
  name: (o) => o.name,
  store: (o) => storeName(o.store_id),
  created_at: (o) => o.created_at,
  customer: (o) => o.customer,
  ship_to: (o) => [o.ship_state, o.ship_city].filter(Boolean).join(' '),
  shipping: (o) => shippingLabel(o.shipping_method),
  units: (o) => o.units,
  total: (o) => (o.total == null || o.total === '' ? null : Number(o.total)),
  tags: (o) => visibleTags(o.tags).join(', '),
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
    const on = th.dataset.sort === sort.key;
    th.classList.toggle('asc', on && sort.dir === 1);
    th.classList.toggle('desc', on && sort.dir === -1);
    th.setAttribute('aria-sort', on ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none');
  });
}

document.querySelectorAll('#pl-head th.sortable').forEach((th) => {
  th.addEventListener('click', () => {
    sort = th.dataset.sort === sort.key ? { key: sort.key, dir: -sort.dir } : { key: th.dataset.sort, dir: 1 };
    remember(SORT_KEY, sort);
    render();
  });
});

/* ---------- Lists ---------- */
/* Orders the view, rules and filters keep, in list order. */
function scopedOrders() {
  return orders.filter((o) => inScope(o)).sort(compareOrders);
}

/* What Generate puts on the pick list: search and Needs review play no part. */
function pickedOrders() {
  return scopedOrders().filter((o) => !excluded.has(orderKey(o)));
}

const inCurrentView = (o) => excluded.has(orderKey(o)) === (view === 'excluded');

function lensPass(o, search) {
  if (search && !`${o.name} ${o.customer}`.toLowerCase().includes(search)) return false;
  return !reviewOnly || orderFlags(o).length > 0;
}

/* The rows of the current view, in list order. */
function listedOrders() {
  const search = searchText();
  return scopedOrders().filter((o) => inCurrentView(o) && lensPass(o, search));
}

/* Exclude in the To pick view, restore in the Excluded view; remembered per
   computer, and Undo puts the last move back. */
function moveOrders(keys) {
  const toExcluded = view === 'pick';
  const moved = keys.filter((k) => excluded.has(k) !== toExcluded);
  moved.forEach((k) => (toExcluded ? excluded.add(k) : excluded.delete(k)));
  remember(EXCLUDED_KEY, [...excluded]);
  lastMove = { keys: moved, toExcluded };
  return moved;
}

function undoMove() {
  if (!lastMove) return;
  const { keys, toExcluded } = lastMove;
  keys.forEach((k) => (toExcluded ? excluded.delete(k) : excluded.add(k)));
  remember(EXCLUDED_KEY, [...excluded]);
  lastMove = null;
  render();
  snackbar(`${plural(keys.length, 'order')} ${toExcluded ? 'back on the pick list' : 'excluded again'}`);
}

function moveSnackbar(message) {
  snackbar(message);
  const el = document.getElementById('snackbar');
  el.insertAdjacentHTML('beforeend', '<button type="button" class="snackbar-action">Undo</button>');
  el.querySelector('.snackbar-action').addEventListener('click', undoMove);
}

/* Moves rows from the list itself; the keyboard row moves on to the next one. */
function moveRows(keys) {
  const before = listedOrders().map(orderKey);
  const at = before.indexOf(cursorKey);
  const moved = moveOrders(keys);
  if (!moved.length) return;
  const after = listedOrders().map(orderKey);
  if (cursorKey && !after.includes(cursorKey)) cursorKey = after[Math.min(Math.max(at, 0), after.length - 1)] || null;
  const name = moved.length === 1 ? (orders.find((o) => orderKey(o) === moved[0]) || {}).name : plural(moved.length, 'order');
  render();
  moveSnackbar(view === 'pick' ? `${name} excluded` : `${name} back on the pick list`);
}

function setView(next) {
  view = next;
  checked.clear();
  lastChecked = null;
  cursorKey = null;
  document.querySelectorAll('.pl-view').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  render();
}
document.querySelectorAll('.pl-view').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));

/* ---------- Rows ---------- */
function tagsHtml(tags) {
  return tags.length ? `<div class="order-tags">${tags.map((t) => `<span class="tag-chip">${esc(t)}</span>`).join('')}</div>` : '';
}

/* The first few tags on one line and "+N" for the rest; hidden tags are left out. */
function compactTags(tags, max) {
  const shown = visibleTags(tags);
  if (!shown.length) return '';
  const rest = shown.slice(max);
  return `<span class="pl-tagline">${shown.slice(0, max).map((t) => `<span class="tag-chip${PICKLIST_TAG.test(t.trim()) ? ' pl-tag-pick' : ''}" title="${attr(t)}">${esc(t)}</span>`).join('')}${
    rest.length ? `<span class="tag-chip pl-tag-more" title="${attr(rest.join(', '))}">+${rest.length}</span>` : ''}</span>`;
}

function ageDays(iso) {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((today - d) / 86400000);
}

function placedHtml(o) {
  const days = ageDays(o.created_at);
  if (days == null) return esc(o.created_at || '');
  const d = new Date(`${o.created_at}T12:00:00`);
  const label = days === 0 ? 'Today' : days === 1 ? 'Yesterday'
    : d.toLocaleDateString('en-US', d.getFullYear() === new Date().getFullYear()
      ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
  const title = `${shortDate(o.created_at)}${days > 1 ? ` · ${days} days ago` : ''}`;
  return `<span title="${attr(title)}">${label}</span>${days >= AGE_WARN_DAYS ? `<span class="pl-age" title="${days} days old">${days}d</span>` : ''}`;
}

function shipToHtml(o) {
  const street = [o.ship_address1, o.ship_address2].filter(Boolean).join(', ');
  const city = esc(o.ship_city || '');
  const state = o.ship_state ? `<strong>${esc(o.ship_state)}</strong>` : '';
  const place = [[city, state].filter(Boolean).join(', '), esc(o.ship_zip || '')].filter(Boolean).join(' ');
  if (!street && !place) return '<span class="text-secondary">—</span>';
  const country = o.ship_country && o.ship_country !== 'US' ? ` · ${esc(o.ship_country)}` : '';
  const full = [street, [o.ship_city, o.ship_state].filter(Boolean).join(', '), o.ship_zip, o.ship_country].filter(Boolean).join(', ');
  return `<div class="pl-cellflex"><span class="pl-place" title="${attr(full)}">${place || esc(street)}${country}</span>${
    o.po_box ? '<span class="pl-pobox" title="PO Box — only USPS, or a service that hands off to USPS, can deliver">PO Box</span>' : ''}</div>`;
}

function shippingHtml(o) {
  if (!o.shipping_method) return '<span class="text-secondary">—</span>';
  return `<span class="pl-ship${FAST_SHIPPING.test(o.shipping_method) ? ' fast' : ''}" title="${attr(o.shipping_method)}">${esc(shippingLabel(o.shipping_method))}</span>`;
}

function rowHtml(o, i, action) {
  const key = orderKey(o);
  const products = o.lines.length;
  return `<tr class="${checked.has(key) ? 'selected' : ''}${key === cursorKey ? ' cursor' : ''}" data-key="${attr(key)}">
    <td class="pl-check"><input type="checkbox" data-check="${attr(key)}" ${checked.has(key) ? 'checked' : ''} aria-label="Select ${attr(o.name)}"></td>
    <td class="pl-num">${i + 1}</td>
    <td class="pl-order"><button class="link-btn" data-fraud="${attr(key)}" title="Open order details">${esc(o.name)}</button>${flagsHtml(o)}</td>
    ${reviewOnly ? `<td class="pl-whycell"><div class="pl-cellflex">${flagReasonsHtml(o)}</div></td>` : ''}
    <td class="pl-storecell"><span class="pl-dot" style="--dot:${storeColor(o.store_id)}"></span>${esc(storeName(o.store_id))}</td>
    <td class="pl-date">${placedHtml(o)}</td>
    <td class="pl-customer"><div class="pl-cellflex"><span class="pl-cust-name" title="${attr(o.customer)}">${esc(o.customer)}</span>${compactTags(o.customer_tags, 1)}</div></td>
    <td class="pl-shipto">${shipToHtml(o)}</td>
    <td class="pl-shipping">${shippingHtml(o)}</td>
    <td class="num pl-units" title="${attr(`${plural(o.units, 'unit')} of ${plural(products, 'product')}`)}">${o.units}</td>
    <td class="num pl-total">${money(o.total)}</td>
    <td class="pl-tags"><div class="pl-cellflex">${compactTags(o.tags, 2)}</div></td>
    <td class="pl-actions">${action.replace('KEY', attr(key))}</td>
  </tr>`;
}

function render(emptyMessage) {
  const tbody = document.getElementById('pl-orders');
  const empty = document.getElementById('pl-empty');
  const scoped = scopedOrders();
  const picked = scoped.filter((o) => !excluded.has(orderKey(o)));
  const inView = scoped.filter(inCurrentView);
  const search = searchText();
  const shown = inView.filter((o) => lensPass(o, search));
  document.getElementById('pl-count-pick').textContent = picked.length;
  document.getElementById('pl-count-excluded').textContent = scoped.length - picked.length;
  const reviewCount = inView.filter((o) => orderFlags(o).length).length;
  const review = document.getElementById('pl-review');
  review.classList.toggle('on', reviewOnly);
  review.classList.toggle('has', reviewCount > 0);
  review.setAttribute('aria-pressed', reviewOnly);
  review.disabled = !reviewCount && !reviewOnly;
  document.getElementById('pl-count-review').textContent = reviewCount;
  document.getElementById('pl-col-why').hidden = !reviewOnly;
  const action = view === 'pick'
    ? '<button class="btn btn-text btn-small pl-exclude" data-move="KEY" title="Leave this order off the pick list (E)">Exclude</button>'
    : '<button class="btn btn-text btn-small pl-restore" data-move="KEY" title="Put this order back on the pick list (E)">Restore</button>';
  if (cursorKey && !shown.some((o) => orderKey(o) === cursorKey)) cursorKey = null;
  if (!shown.length) {
    tbody.innerHTML = '';
    let message = emptyMessage;
    let reset = '';
    if (!message) {
      if (!orders.length) message = 'No eligible orders.';
      else if (!scoped.length) {
        message = 'No orders match the view and filters.';
        reset = '<button class="btn btn-outlined btn-small" data-empty="all">Show all orders</button>';
      } else if (!inView.length) message = view === 'excluded' ? 'No excluded orders.' : 'Every order is excluded.';
      else {
        message = search ? `No orders match “${search}”.` : 'Nothing needs review.';
        reset = '<button class="btn btn-outlined btn-small" data-empty="lens">Show every order</button>';
      }
    }
    empty.innerHTML = `${esc(message)}${reset ? `<div class="pl-empty-reset">${reset}</div>` : ''}`;
    empty.style.display = '';
  } else {
    empty.style.display = 'none';
    tbody.innerHTML = shown.map((o, i) => rowHtml(o, i, action)).join('');
  }
  // Only rows on screen can stay selected: filtering or excluding drops the rest.
  const onScreen = new Set(shown.map(orderKey));
  [...checked].forEach((k) => { if (!onScreen.has(k)) checked.delete(k); });
  renderSortHeads();
  renderSummary(picked, scoped.length, search || reviewOnly);
  renderSelection(shown.length);
  renderShowing(shown.length, scoped.length !== orders.length || search || reviewOnly);
  renderFilterBar();
  renderViewTabs();
  renderPopList();
}

document.getElementById('pl-empty').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-empty]');
  if (!btn) return;
  if (btn.dataset.empty === 'all') resetFilters();
  else clearLens();
});

/* How many rows are on screen against every eligible order, with one click
   to show them all again. */
function renderShowing(count, narrowed) {
  const el = document.getElementById('pl-showing');
  el.innerHTML = narrowed && orders.length
    ? `Showing <strong>${count}</strong> of ${orders.length}<button class="link-btn" id="pl-reset">Show all</button>`
    : '';
  const reset = document.getElementById('pl-reset');
  if (reset) reset.addEventListener('click', resetFilters);
}

function clearLens() {
  document.getElementById('qf-search').value = '';
  document.querySelector('.pl-search').classList.remove('filled');
  reviewOnly = false;
  render();
}

function resetFilters() {
  facets.clear();
  setRuleModes(new Map());
  activePreset = null;
  remember(PRESET_KEY, null);
  closePop();
  clearLens();
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
  cursorKey = key;
  render();
}

function clearChecked() {
  checked.clear();
  lastChecked = null;
  render();
}

/* The pick list as Generate would make it; the note says what else is in play. */
function renderSummary(picked, scopedCount, lens) {
  const units = picked.reduce((n, o) => n + o.units, 0);
  const products = new Set(picked.flatMap((o) => o.lines.map((l) => l.barcode || `?${l.title}`))).size;
  document.getElementById('pl-summary').textContent = picked.length
    ? `On the pick list: ${plural(picked.length, 'order')} · ${plural(units, 'unit')} · ${plural(products, 'product')}`
    : 'Nothing on the pick list';
  const excludedCount = scopedCount - picked.length;
  const flagged = picked.filter((o) => o.issues && o.issues.length).length;
  document.getElementById('pl-summary-note').textContent = [
    scopedCount !== orders.length ? `${scopedCount} of ${orders.length} eligible orders match the view and filters` : '',
    excludedCount ? `${excludedCount} excluded` : '',
    flagged ? `${plural(flagged, 'order')} will be left out` : '',
    lens ? 'search and Needs review don’t change it' : '',
  ].filter(Boolean).join(' · ');
  document.getElementById('pl-generate').disabled = !picked.length;
}

document.getElementById('pl-orders').addEventListener('click', (e) => {
  const move = e.target.closest('[data-move]');
  if (move) {
    cursorKey = move.dataset.move;
    moveRows([move.dataset.move]);
    return;
  }
  const fraud = e.target.closest('[data-fraud]');
  if (fraud) {
    cursorKey = fraud.dataset.fraud;
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
  const keys = [...checked];
  checked.clear();
  lastChecked = null;
  moveRows(keys);
});
document.getElementById('pl-bulk-clear').addEventListener('click', clearChecked);
const searchBox = document.getElementById('qf-search');
searchBox.addEventListener('input', () => {
  searchBox.parentElement.classList.toggle('filled', !!searchBox.value);
  render();
});
searchBox.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && searchBox.value) {
    e.stopPropagation();
    clearLens();
  } else if (e.key === 'Enter' || e.key === 'ArrowDown') {
    // Straight from finding to the keyboard row.
    e.preventDefault();
    searchBox.blur();
    cursorKey = null;
    moveCursor(1);
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

function orderBody(d, o) {
  const flags = o ? orderFlags(o) : [];
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
      ${flags.length ? `<div class="om-review"><span class="om-label">Needs review</span><div class="om-review-list">${flagReasonsHtml(o, true)}</div></div>` : ''}
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
    html = orderBody(d, o);
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
  moveSnackbar(view === 'pick' ? `${name} excluded` : `${name} back on the pick list`);
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
    snackbar(`${updated.name} no longer fits the view and filters in use — it leaves this list when you move on`);
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
  const picked = pickedOrders();
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
