const STORES_KEY = 'easyship.picklistStores';
const RULES_KEY = 'easyship.picklistRules';
const CUSTOMER_KEY = 'easyship.picklistCustomer';
const REP_KEY = 'easyship.picklistRep';

let stores = [];
let selectedStores = new Set();
let orders = [];
let rules = [];
let activeRules = new Set();
const excluded = new Set();

initNav('picklists');

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
function openModal(html, wide = false) {
  const modal = document.getElementById('modal');
  modal.innerHTML = html;
  modal.classList.toggle('wide', wide);
  document.getElementById('modal-backdrop').classList.add('show');
}
function closeModal() {
  document.getElementById('modal-backdrop').classList.remove('show');
}
window.closeModal = closeModal;
document.getElementById('modal-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modal-backdrop') closeModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
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
  el.innerHTML = '<span class="pl-chip-label">Stores</span>' + stores.map((s) =>
    `<button class="chip ${selectedStores.has(s.id) ? 'active' : ''}" data-store="${s.id}">${esc(s.name)}</button>`).join('');
  el.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const id = Number(chip.dataset.store);
      if (selectedStores.has(id)) selectedStores.delete(id);
      else selectedStores.add(id);
      remember(STORES_KEY, [...selectedStores]);
      renderStores();
      loadOrders();
    });
  });
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
  tbody.innerHTML = '<tr><td colspan="9"><span class="spinner"></span> Loading eligible orders…</td></tr>';
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
  const savedRules = recall(RULES_KEY, []);
  activeRules = new Set(savedRules.filter((id) => rules.some((r) => r.id === id)));
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

function renderRules() {
  const el = document.getElementById('pl-rules');
  el.style.display = rules.length ? '' : 'none';
  el.innerHTML = '<span class="pl-chip-label">Rules</span>' + rules.map((r) => `
    <button class="chip ${activeRules.has(r.id) ? 'active' : ''}" data-rule="${esc(r.id)}"
      title="${r.action === 'keep' ? 'Show only matching orders' : 'Hide matching orders'}">
      ${r.action === 'keep' ? 'Only' : 'Hide'}: ${esc(r.name)}</button>`).join('');
  el.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const id = chip.dataset.rule;
      if (activeRules.has(id)) activeRules.delete(id);
      else activeRules.add(id);
      remember(RULES_KEY, [...activeRules]);
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
  const on = rules.filter((r) => activeRules.has(r.id));
  if (on.some((r) => r.action === 'hide' && o.rule_ids.includes(r.id))) return false;
  const keeps = on.filter((r) => r.action === 'keep');
  if (keeps.length && !keeps.some((r) => o.rule_ids.includes(r.id))) return false;
  const has = (tags, needle) => tags.some((t) => t.toLowerCase().includes(needle));
  if (qf.tag && !has(o.tags, qf.tag)) return false;
  if (qf.ctag && !has(o.customer_tags, qf.ctag)) return false;
  if (qf.states.length && !qf.states.includes((o.ship_state || '').toUpperCase())) return false;
  if (qf.search && !`${o.name} ${o.customer}`.toLowerCase().includes(qf.search)) return false;
  return true;
}

function visibleOrders() {
  const qf = quickFilters();
  return orders.filter((o) => passesFilters(o, qf));
}

function includedOrders() {
  return visibleOrders().filter((o) => !excluded.has(orderKey(o)));
}

function riskChip(level) {
  if (!level || level === 'NONE') return '';
  const tone = { HIGH: 'err', MEDIUM: 'warn', LOW: 'ok' }[level] || '';
  const label = level === 'PENDING' ? 'Pending' : level.charAt(0) + level.slice(1).toLowerCase();
  return `<span class="chip static ${tone}">${esc(label)}</span>`;
}

function tagsHtml(tags) {
  return tags.length ? `<div class="order-tags">${tags.map((t) => `<span class="tag-chip">${esc(t)}</span>`).join('')}</div>` : '';
}

function render(emptyMessage) {
  const tbody = document.getElementById('pl-orders');
  const empty = document.getElementById('pl-empty');
  const visible = visibleOrders();
  const shown = visible.filter((o) => !excluded.has(orderKey(o)));
  if (!shown.length) {
    tbody.innerHTML = '';
    empty.textContent = emptyMessage || (orders.length ? 'No orders match the filters.' : 'No eligible orders.');
    empty.style.display = '';
  } else {
    empty.style.display = 'none';
    tbody.innerHTML = shown.map((o) => `<tr>
      <td><button class="link-btn" data-fraud="${esc(orderKey(o))}">${esc(o.name)}</button></td>
      <td>${esc(storeName(o.store_id))}</td>
      <td>${esc(o.created_at)}</td>
      <td class="ellip" title="${esc(o.customer)}">${esc(o.customer)}${tagsHtml(o.customer_tags)}</td>
      <td>${esc([o.ship_city, o.ship_state].filter(Boolean).join(', '))}</td>
      <td class="num">${o.units}</td>
      <td class="wrap">${tagsHtml(o.tags)}</td>
      <td>${riskChip(o.risk_level)}</td>
      <td><button class="btn btn-text btn-small" data-exclude="${esc(orderKey(o))}" title="Leave this order off the pick list">✕ Exclude</button></td>
    </tr>`).join('');
  }
  renderExcluded(visible);
  renderSummary();
}

function renderExcluded(visible) {
  const box = document.getElementById('pl-excluded');
  const out = visible.filter((o) => excluded.has(orderKey(o)));
  box.style.display = out.length ? '' : 'none';
  document.getElementById('pl-excluded-summary').textContent = `Excluded by you (${out.length})`;
  document.getElementById('pl-excluded-list').innerHTML = out.map((o) => `
    <div class="pl-excluded-row">
      <strong>${esc(o.name)}</strong> <span class="text-secondary">${esc(storeName(o.store_id))} · ${esc(o.customer)} · ${o.units} units</span>
      <button class="btn btn-text btn-small" data-restore="${esc(orderKey(o))}">Restore</button>
    </div>`).join('');
}

function renderSummary() {
  const inc = includedOrders();
  const units = inc.reduce((n, o) => n + o.units, 0);
  const products = new Set(inc.flatMap((o) => o.lines.map((l) => l.barcode || `?${l.title}`))).size;
  document.getElementById('pl-summary').textContent = inc.length
    ? `${inc.length} order${inc.length === 1 ? '' : 's'} · ${units} unit${units === 1 ? '' : 's'} · ${products} product${products === 1 ? '' : 's'}`
    : 'No orders selected';
  document.getElementById('pl-generate').disabled = !inc.length;
}

document.getElementById('pl-orders').addEventListener('click', (e) => {
  const ex = e.target.closest('[data-exclude]');
  if (ex) {
    excluded.add(ex.dataset.exclude);
    render();
    return;
  }
  const fraud = e.target.closest('[data-fraud]');
  if (fraud) showFraud(orders.find((o) => orderKey(o) === fraud.dataset.fraud));
});
document.getElementById('pl-excluded-list').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-restore]');
  if (!btn) return;
  excluded.delete(btn.dataset.restore);
  render();
});
['qf-tag', 'qf-ctag', 'qf-state', 'qf-search'].forEach((id) =>
  document.getElementById(id).addEventListener('input', () => render()));
document.getElementById('qf-clear').addEventListener('click', () => {
  ['qf-tag', 'qf-ctag', 'qf-state', 'qf-search'].forEach((id) => { document.getElementById(id).value = ''; });
  render();
});
document.getElementById('pl-refresh').addEventListener('click', loadOrders);

/* ---------- Fraud window ---------- */
const RECOMMENDATION_TEXT = {
  ACCEPT: 'You can fulfill this order',
  INVESTIGATE: 'Investigate this order before fulfilling it',
  CANCEL: 'Consider canceling this order',
};
const CARDHOLDER_TEXT = {
  MATCH: ['ok', 'Name on card matches the order'],
  LAST_NAME_ONLY: ['warn', 'Only the last name on the card matches the order'],
  MISMATCH: ['err', 'Name on card does not match the billing or shipping name'],
};
const SENTIMENT_TONE = { NEGATIVE: 'err', POSITIVE: 'ok' };

function addressHtml(title, a) {
  if (!a) return `<div class="pl-fraud-col"><h4>${title}</h4><span class="text-secondary">—</span></div>`;
  return `<div class="pl-fraud-col"><h4>${title}</h4>
    ${[a.name, a.address1, a.address2, [a.city, a.state, a.zip].filter(Boolean).join(', '), a.country, a.phone]
      .filter(Boolean).map((l) => `<div>${esc(l)}</div>`).join('')}</div>`;
}

function riskHtml(risk) {
  if (!risk) return '<p class="text-secondary">Shopify has no risk evaluation for this order.</p>';
  const level = risk.level && risk.level !== 'PENDING'
    ? `<div class="pl-risk-level ${({ HIGH: 'err', MEDIUM: 'warn', LOW: 'ok' })[risk.level] || ''}">${esc(risk.level.charAt(0) + risk.level.slice(1).toLowerCase())} risk</div>` : '';
  const advice = risk.level === 'PENDING' ? 'Risk analysis in progress' : RECOMMENDATION_TEXT[risk.recommendation] || '';
  const many = risk.assessments.length > 1;
  const assessments = risk.assessments.map((a) => `
    ${many ? `<div class="pl-risk-provider">${esc(a.provider || 'Shopify')}: ${esc(a.level ? `${a.level.toLowerCase()} risk` : 'no risk level')}</div>` : ''}
    ${a.facts.length ? `<ul class="pl-risk-facts">${a.facts.map((f) =>
      `<li class="${SENTIMENT_TONE[f.sentiment] || ''}">${esc(f.description)}</li>`).join('')}</ul>` : ''}`).join('');
  let cardholder = '';
  if (risk.cardholder) {
    const [tone, text] = CARDHOLDER_TEXT[risk.cardholder.status];
    const c = risk.cardholder;
    cardholder = `<div class="pl-risk-card ${tone}">${text}</div>
      <div class="hint">${c.card_names.length > 1 ? 'Names on cards' : 'Name on card'}: ${esc(c.card_names.join(', '))}
        · Billing: ${esc(c.billing_name || '—')} · Shipping: ${esc(c.shipping_name || '—')}</div>`;
  }
  const ip = risk.ip ? `<div class="hint mt-16">IP address: ${esc(risk.ip)}${risk.ip_location
    ? `<br>Location of IP address used to place the order is ${esc(risk.ip_location)}` : ''}</div>` : '';
  return `${level}${advice ? `<div class="text-secondary mb-16">${esc(advice)}</div>` : ''}${assessments}${cardholder}${ip}`;
}

async function showFraud(o) {
  if (!o) return;
  openModal(`<h3>Order ${esc(o.name)}</h3><p><span class="spinner"></span> Loading fraud data…</p>`, true);
  let d;
  try {
    d = await api(`/api/picklists/orders/risk?store_id=${o.store_id}&order_id=${encodeURIComponent(o.id)}`);
  } catch (err) {
    openModal(`<h3>Order ${esc(o.name)}</h3><p class="error-note">${esc(err.message)}</p>
      <div class="actions"><button class="btn btn-text" onclick="closeModal()">Close</button></div>`, true);
    return;
  }
  const c = d.customer || {};
  openModal(`
    <h3>Order ${esc(d.name)}</h3>
    <div class="hint mb-16">${esc(storeName(o.store_id))} · ${esc(d.created_at)} · ${esc(d.financial_status || '')} · ${esc(d.fulfillment_status || '')}</div>
    <div class="pl-fraud-stats">
      <div><span class="hint">Total</span><strong>${money(d.total)}</strong></div>
      <div><span class="hint">Items</span><strong>${d.items ?? ''}</strong></div>
      <div><span class="hint">Shipping</span><strong>${esc(d.shipping_method || '—')}</strong></div>
    </div>
    ${tagsHtml(d.tags)}
    <div class="pl-fraud-cols">
      <div class="pl-fraud-col"><h4>Customer</h4>
        <div>${esc(c.name || '—')}</div>
        ${d.email ? `<div>${esc(d.email)}</div>` : ''}${d.phone ? `<div>${esc(d.phone)}</div>` : ''}
        ${c.orders_count != null ? `<div class="hint">${c.orders_count} order${c.orders_count === 1 ? '' : 's'}${c.since ? ` · since ${esc(c.since)}` : ''}</div>` : ''}
        ${c.amount_spent != null ? `<div class="hint">Spent ${money(c.amount_spent)}</div>` : ''}
        ${tagsHtml(c.tags || [])}
      </div>
      ${addressHtml('Ship to', d.shipping_address)}
      ${addressHtml('Bill to', d.billing_address)}
    </div>
    <div class="pl-risk">
      <div class="pl-risk-head">Order risk evaluation</div>
      ${riskHtml(d.risk)}
    </div>
    <div class="actions">
      <button class="btn btn-danger" id="m-fraud-exclude">Exclude from pick list</button>
      <button class="btn btn-text" onclick="closeModal()">Close</button>
    </div>`, true);
  document.getElementById('m-fraud-exclude').addEventListener('click', () => {
    excluded.add(orderKey(o));
    closeModal();
    render();
  });
}

/* ---------- Generate ---------- */
document.getElementById('pl-generate').addEventListener('click', openGenerate);

async function openGenerate() {
  const picked = includedOrders();
  if (!picked.length) return;
  const body = { orders: picked.map((o) => ({ store_id: o.store_id, order_id: o.id })) };
  openModal('<h3>New pick list</h3><p><span class="spinner"></span> Checking orders and BackOffice products…</p>', true);
  let preview;
  let reps;
  try {
    [preview, reps] = await Promise.all([
      api('/api/picklists/preview', { method: 'POST', body }),
      api('/api/picklists/sales-reps'),
    ]);
  } catch (err) {
    openModal(`<h3>New pick list</h3><p class="error-note">${esc(err.message)}</p>
      <div class="actions"><button class="btn btn-text" onclick="closeModal()">Close</button></div>`, true);
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
    </div>`, true);
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
  openModal(`<h3>Pick list #${id}</h3><p><span class="spinner"></span> Loading…</p>`, true);
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
    <div class="actions"><button class="btn btn-text" onclick="closeModal()">Close</button></div>`, true);
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
