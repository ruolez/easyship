initNav('parcels');

let clientSettings = { print_mode: 'browser' };
api('/api/settings/client').then((s) => { clientSettings = s; }).catch(() => {});

const COPY_ICON = '<svg viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';

function copyable(text, label) {
  if (!text) return '';
  return `<span class="copy-wrap">${esc(text)}<button class="copy-btn" data-copy="${esc(text)}" title="Copy ${label}" aria-label="Copy ${label}">${COPY_ICON}</button></span>`;
}

// navigator.clipboard only exists on secure origins (https / localhost); the
// app is usually reached over plain http on the LAN, so fall back to the
// selection-based copy command there.
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch { /* fall through to execCommand */ }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  if (await copyText(btn.dataset.copy)) {
    btn.classList.add('copied');
    setTimeout(() => btn.classList.remove('copied'), 1200);
    snackbar('Copied', 'success');
  } else {
    snackbar('Copy failed — select the text and copy manually', 'error');
  }
});

function signatureChip(options) {
  const sig = (options || {}).signature;
  if (sig === 'adult') return ' <span class="chip static warn" title="Adult signature (21+) required">21+</span>';
  if (sig === 'signature') return ' <span class="chip static warn" title="Signature required">✍</span>';
  return '';
}

function boxSize(s) {
  const p = (s.parcels || [])[0] || {};
  const dims = [p.length, p.width, p.height].map(Number);
  if (dims.some((d) => !d || d <= 0)) return '';
  return dims.map((d) => String(d)).join('×');
}

function boxVolume(s) {
  const p = (s.parcels || [])[0] || {};
  return (Number(p.length) || 0) * (Number(p.width) || 0) * (Number(p.height) || 0);
}

function formatAddress(d) {
  if (!d) return '';
  const parts = [
    d.contact, d.company && d.company !== d.contact ? d.company : null,
    d.address1, d.address2, d.city,
    [d.state, d.zip].filter(Boolean).join(' '),
  ];
  return parts.filter(Boolean).join(', ');
}

async function loadUsers() {
  try {
    const users = await api('/api/shipments/creators');
    userFilter.setOptions(users);
  } catch { /* filter stays open */ }
}

async function loadAccounts() {
  try {
    const accounts = await api('/api/shipments/providers');
    accountFilter.setOptions(accounts);
  } catch { /* filter stays open */ }
}

/* ---------- Multi-select column filters: a compact button in the filter row
   recalls a checkbox popover; empty selection means "All". ---------- */
const FM_CARET = '<svg class="fm-caret" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>';

let openFilter = null;
function closeFilterMenu() {
  if (!openFilter) return;
  openFilter.menu.remove();
  openFilter.btn.classList.remove('open');
  openFilter = null;
}

function multiFilter(id, { options = [], onChange }) {
  const btn = document.getElementById(id);
  const normalize = (list) => list.map((o) => (typeof o === 'string' ? { value: o, label: o } : o));
  let opts = normalize(options);
  const selected = new Set();

  function updateBtn() {
    let text = 'All';
    if (selected.size === 1) {
      const v = selected.values().next().value;
      text = (opts.find((o) => o.value === v) || { label: v }).label;
    } else if (selected.size > 1) text = `${selected.size} selected`;
    btn.innerHTML = `<span class="fm-label">${esc(text)}</span>${FM_CARET}`;
    btn.classList.toggle('filtered', selected.size > 0);
  }

  function renderMenu(menu) {
    menu.innerHTML = `
      <button class="filter-menu-item${selected.size ? '' : ' checked'}" data-all="1"><span class="fm-check"></span><span>All</span></button>
      <div class="row-menu-sep"></div>
      ${opts.map((o) => `<button class="filter-menu-item${selected.has(o.value) ? ' checked' : ''}" data-v="${esc(o.value)}"><span class="fm-check"></span><span>${esc(o.label)}</span></button>`).join('')}`;
  }

  btn.addEventListener('click', () => {
    closeRowMenu();
    if (openFilter && openFilter.btn === btn) { closeFilterMenu(); return; }
    closeFilterMenu();
    const menu = document.createElement('div');
    menu.className = 'row-menu filter-menu';
    menu.setAttribute('role', 'menu');
    renderMenu(menu);
    menu.addEventListener('click', (ev) => {
      // The re-render below detaches ev.target, so the document-level
      // outside-click check would no longer see it inside the menu.
      ev.stopPropagation();
      const item = ev.target.closest('.filter-menu-item');
      if (!item) return;
      if (item.dataset.all) selected.clear();
      else if (selected.has(item.dataset.v)) selected.delete(item.dataset.v);
      else selected.add(item.dataset.v);
      renderMenu(menu);
      updateBtn();
      onChange();
    });
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    const left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8));
    let top = r.bottom + 4;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = r.top - menu.offsetHeight - 4;
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    btn.classList.add('open');
    openFilter = { btn, menu };
  });

  updateBtn();
  return {
    get values() { return selected; },
    setOptions(list) {
      opts = normalize(list);
      [...selected].forEach((v) => { if (!opts.some((o) => o.value === v)) selected.delete(v); });
      updateBtn();
    },
  };
}

const statusFilter = multiFilter('status-filter', {
  options: [
    { value: 'fulfilled', label: 'Fulfilled' },
    { value: 'label_created', label: 'Label created' },
    { value: 'rated', label: 'Rated (no label)' },
    { value: 'draft', label: 'Draft' },
    { value: 'voided', label: 'Voided' },
    { value: 'error', label: 'Error' },
  ],
  onChange: () => load(),
});
const userFilter = multiFilter('user-filter', { onChange: () => load() });
const accountFilter = multiFilter('account-filter', { onChange: () => load() });
const storeFilter = multiFilter('store-filter', { onChange: () => render() });
const serviceFilter = multiFilter('service-filter', { onChange: () => render() });
const carrierFilter = multiFilter('carrier-filter', { onChange: () => render() });

/* ---------- Client-side sorting & column filters over the fetched page ---------- */
let allRows = [];
let renderedRows = [];
let sortKey = null;
let sortDir = 1;

const SORT_VALUE = {
  ref: (s) => (s.shopify_order_name || s.backoffice_invoice_number || `#${s.id}`).toLowerCase(),
  user: (s) => (s.created_by || '').toLowerCase(),
  store: (s) => (s.service_name || '').toLowerCase(),
  address: (s) => formatAddress(s.destination).toLowerCase(),
  boxes: (s) => s.box_total || 1,
  size: (s) => boxVolume(s) || -1,
  weight: (s) => s.total_weight_lb ?? -1,
  account: (s) => (s.provider_label || '').toLowerCase(),
  courier: (s) => (s.courier_name || '').toLowerCase(),
  carrier: (s) => (s.courier_umbrella_name || '').toLowerCase(),
  cost: (s) => s.shipping_cost ?? -1,
  tracking: (s) => s.tracking_number || '',
  status: (s) => s.status || '',
  created: (s) => s.created_at || '',
};

document.querySelectorAll('th.sortable').forEach((th) => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (sortKey === key) sortDir = -sortDir;
    else { sortKey = key; sortDir = 1; }
    document.querySelectorAll('th.sortable').forEach((h) => h.classList.remove('asc', 'desc'));
    th.classList.add(sortDir === 1 ? 'asc' : 'desc');
    render();
  });
});

function fillOptions(id, values) {
  const el = document.getElementById(id);
  const current = el.value;
  el.innerHTML = '<option value="">All</option>'
    + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  if (values.includes(current)) el.value = current;
}

function visibleRows() {
  const stores = storeFilter.values;
  const carriers = carrierFilter.values;
  const services = serviceFilter.values;
  const size = document.getElementById('size-filter').value;
  let rows = allRows.filter((s) =>
    (!stores.size || stores.has(s.service_name))
    && (!carriers.size || carriers.has(s.courier_umbrella_name))
    && (!services.size || services.has(s.courier_name))
    && (!size || boxSize(s) === size));
  if (sortKey) {
    const val = SORT_VALUE[sortKey];
    rows = [...rows].sort((a, b) => {
      const va = val(a); const vb = val(b);
      return ((va > vb) - (va < vb)) * sortDir;
    });
  }
  return rows;
}

async function load() {
  const params = new URLSearchParams({
    q: document.getElementById('search').value.trim(),
    status: [...statusFilter.values].join(','),
    user: [...userFilter.values].join(','),
    provider: [...accountFilter.values].join(','),
    from: document.getElementById('date-from').value,
    to: document.getElementById('date-to').value,
  });
  const tbody = document.getElementById('parcels-body');
  const empty = document.getElementById('empty');
  empty.style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="16"><span class="spinner"></span> Loading…</td></tr>';
  try {
    allRows = await api(`/api/shipments?${params}`);
    const uniq = (vals) => [...new Set(vals.filter(Boolean))].sort();
    storeFilter.setOptions(uniq(allRows.map((s) => s.service_name)));
    carrierFilter.setOptions(uniq(allRows.map((s) => s.courier_umbrella_name)));
    serviceFilter.setOptions(uniq(allRows.map((s) => s.courier_name)));
    const sizes = [...new Map(allRows.filter(boxSize).map((s) => [boxSize(s), boxVolume(s)]))]
      .sort((a, b) => a[1] - b[1]).map(([label]) => label);
    fillOptions('size-filter', sizes);
    render();
  } catch (err) {
    allRows = [];
    renderTotals([]);
    tbody.innerHTML = '';
    empty.textContent = err.message;
    empty.style.display = '';
  }
}

/* Filter-aware totals: a summary line above the table and a totals row under
   it, so the packer sees how many parcels/shipments the current view covers
   and what the shipping cost added up to. */
function renderTotals(rows) {
  const bar = document.getElementById('parcels-summary');
  const foot = document.getElementById('parcels-foot');
  if (!rows.length) {
    bar.style.display = 'none';
    foot.innerHTML = '';
    return;
  }
  const cost = rows.reduce((sum, s) => sum + (s.shipping_cost || 0), 0);
  const shipments = new Set(rows.map((s) => s.group_id || `#${s.id}`)).size;
  bar.style.display = '';
  bar.innerHTML = `<span><strong>${shipments}</strong> shipment${shipments === 1 ? '' : 's'}</span>
    <span><strong>${rows.length}</strong> parcel${rows.length === 1 ? '' : 's'}</span>
    <span>Shipping total <strong>${money(cost)}</strong></span>`;
  foot.innerHTML = `<tr>
    <td class="pin-num"></td>
    <td class="pin-ref">Total</td>
    <td colspan="4">${shipments} shipment${shipments === 1 ? '' : 's'}, ${rows.length} parcel${rows.length === 1 ? '' : 's'}</td>
    <td class="col-size"></td>
    <td colspan="4"></td>
    <td class="num">${money(cost)}</td>
    <td colspan="3"></td>
    <td class="actions"></td></tr>`;
}

/* ---------- Row actions: a slim pinned "⋯" column recalls a popover menu,
   so the wide button pane no longer eats table width. ---------- */
const MENU_ICONS = {
  resume: '<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>',
  label: '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  print: '<svg viewBox="0 0 24 24"><path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
  undo: '<svg viewBox="0 0 24 24"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>',
};
const ICON_KEBAB = '<svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg>';

function rowActions(s) {
  const ref = s.shopify_order_name || s.backoffice_invoice_number || `#${s.id}`;
  // Sending tracking covers the whole group, so offer it on every box.
  const hasLabel = s.status === 'label_created';
  const needsShopifyPush = hasLabel && s.source === 'shopify' && !s.writeback_shopify_at;
  const needsRetry = hasLabel && s.source === 'backoffice' && !s.writeback_backoffice_at;
  const canResume = ['rated', 'error'].includes(s.status) && s.courier_service_id
    && s.provider_shipment_id && s.group_id;
  const items = [];
  if (canResume) items.push({ label: 'Resume labels', icon: MENU_ICONS.resume, run: () => resumeBuy(s.group_id) });
  if (s.has_label) items.push({ label: 'View label', icon: MENU_ICONS.label, href: `/api/shipments/${s.id}/label` });
  if (s.has_label) items.push({ label: 'Print label', icon: MENU_ICONS.print, run: () => reprint(s.id) });
  if (needsShopifyPush) items.push({ label: 'Send to Shopify', icon: MENU_ICONS.retry, run: () => sendToShopify(s) });
  if (needsRetry) items.push({ label: 'Retry writeback', icon: MENU_ICONS.retry, run: () => retryWb(s.id) });
  if (['label_created', 'fulfilled'].includes(s.status)) {
    items.push({ label: 'Undo shipment', icon: MENU_ICONS.undo, danger: true, run: () => voidShipment(s.id, ref, s.source, s.box_total) });
  }
  if (s.status === 'voided' && s.error_message) {
    items.push({ label: 'Retry undo', icon: MENU_ICONS.undo, danger: true, run: () => retryUndo(s.id) });
  }
  return items;
}

let openMenu = null;
function closeRowMenu() {
  if (!openMenu) return;
  openMenu.menu.remove();
  openMenu.btn.classList.remove('open');
  openMenu = null;
}

function openRowMenu(btn) {
  const items = rowActions(renderedRows[Number(btn.dataset.row)]);
  const menu = document.createElement('div');
  menu.className = 'row-menu';
  menu.setAttribute('role', 'menu');
  menu.innerHTML = items.map((it, i) => {
    const sep = it.danger && i > 0 && !items[i - 1].danger ? '<div class="row-menu-sep"></div>' : '';
    const cls = `row-menu-item${it.danger ? ' danger' : ''}`;
    const inner = `${it.icon}<span>${esc(it.label)}</span>`;
    return sep + (it.href
      ? `<a class="${cls}" role="menuitem" href="${it.href}" target="_blank" data-i="${i}">${inner}</a>`
      : `<button class="${cls}" role="menuitem" data-i="${i}">${inner}</button>`);
  }).join('');
  menu.addEventListener('click', (ev) => {
    const item = ev.target.closest('.row-menu-item');
    if (!item) return;
    const act = items[Number(item.dataset.i)];
    closeRowMenu();
    if (act.run) act.run();
  });
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8));
  let top = r.bottom + 4;
  if (top + menu.offsetHeight > window.innerHeight - 8) top = r.top - menu.offsetHeight - 4;
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  btn.classList.add('open');
  openMenu = { btn, menu };
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.filter-multi') && !e.target.closest('.filter-menu')) closeFilterMenu();
  const btn = e.target.closest('.row-menu-btn');
  if (!btn) {
    if (!e.target.closest('.row-menu')) closeRowMenu();
    return;
  }
  const reopen = !openMenu || openMenu.btn !== btn;
  closeRowMenu();
  if (reopen) openRowMenu(btn);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeRowMenu(); closeFilterMenu(); }
});
document.addEventListener('scroll', (e) => {
  closeRowMenu();
  if (openFilter && !openFilter.menu.contains(e.target)) closeFilterMenu();
}, true);
window.addEventListener('resize', () => { closeRowMenu(); closeFilterMenu(); });

function render() {
  const tbody = document.getElementById('parcels-body');
  const empty = document.getElementById('empty');
  const rows = visibleRows();
  closeRowMenu();
  renderedRows = rows;
  renderTotals(rows);
  if (!rows.length) {
    tbody.innerHTML = '';
    empty.textContent = 'No parcels found.';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map((s, rowIndex) => {
      const boxesCell = s.box_total > 1
        ? `<span class="chip static ${['label_created', 'fulfilled'].includes(s.status) ? 'ok' : 'warn'}">${s.box_number}/${s.box_total}</span>`
        : '1';
      const numbers = (s.tracking_numbers || []).length ? s.tracking_numbers : (s.tracking_number ? [s.tracking_number] : []);
      const trackingCell = numbers.length
        ? `<span class="copy-wrap"><span class="mono">${esc(numbers[0])}</span>${numbers.length > 1 ? `<span class="chip static warn">+${numbers.length - 1}</span>` : ''}<button class="copy-btn" data-copy="${esc(numbers.join('\n'))}" title="Copy tracking number${numbers.length > 1 ? 's' : ''}" aria-label="Copy tracking">${COPY_ICON}</button></span>`
        : '';
      const ref = s.shopify_order_name || s.backoffice_invoice_number || `#${s.id}`;
      return `<tr>
        <td class="num col-narrow pin-num text-secondary">${rowIndex + 1}</td>
        <td class="pin-ref"><strong>${copyable(ref, 'order number')}</strong></td>
        <td class="col-narrow">${esc(s.created_by)}</td>
        <td class="ellip store" title="${esc(s.service_name)}">${esc(s.service_name)}</td>
        <td class="ellip address" title="${esc(formatAddress(s.destination))}">${esc(formatAddress(s.destination))}</td>
        <td class="num col-narrow">${boxesCell}</td>
        <td class="col-narrow col-size">${esc(boxSize(s))}</td>
        <td class="num col-narrow">${s.total_weight_lb ?? ''}</td>
        <td class="ellip account" title="${esc(s.provider_label || '')}">${esc(s.provider_label || '')}</td>
        <td class="ellip service" title="${esc(s.courier_name || '')}">${esc(s.courier_name || '')}${signatureChip(s.options)}</td>
        <td>${esc(s.courier_umbrella_name || '')}</td>
        <td class="num">${money(s.shipping_cost)}</td>
        <td title="${esc(numbers.join(', '))}">${trackingCell}</td>
        <td><span class="status status-${esc(s.status)}" title="${esc(s.error_message || '')}">${esc(s.status.replace('_', ' '))}</span></td>
        <td class="created">${esc(s.created_at)}</td>
        <td class="actions">${rowActions(s).length
          ? `<button class="row-menu-btn" data-row="${rowIndex}" title="Actions" aria-label="Row actions" aria-haspopup="menu">${ICON_KEBAB}</button>`
          : ''}</td>
      </tr>`;
  }).join('');
}

window.resumeBuy = async (gid) => {
  try {
    await api(`/api/shipments/group/${gid}/buy`, { method: 'POST', body: {} });
  } catch (err) {
    snackbar(err.message, 'error');
    return;
  }
  snackbar('Resuming label purchase…');
  const timer = setInterval(async () => {
    let g;
    try { g = await api(`/api/shipments/group/${gid}`); } catch { return; }
    const st = (g.progress || {}).state;
    const boxes = (g.progress || {}).boxes || [];
    const ready = boxes.filter((b) => b.status === 'ready').length;
    if (st === 'buying') snackbar(`Purchasing labels… ${ready}/${boxes.length} ready`);
    if (st === 'done') {
      clearInterval(timer);
      snackbar('All labels purchased', 'success');
      load();
    } else if (st === 'retry' || st === 'error') {
      clearInterval(timer);
      snackbar((g.progress || {}).message || 'Purchase did not complete', 'error');
      load();
    }
  }, 2000);
};

function printDialog(url) {
  printPdfUrl(url).catch((err) => snackbar(err.message, 'error'));
}

window.reprint = async (id) => {
  try {
    if (clientSettings.print_mode === 'browserprint') {
      await ZebraPrint.printLabelUrl(`/api/shipments/${id}/label`);
      snackbar('Label sent to Zebra printer', 'success');
    } else if (clientSettings.print_mode === 'network') {
      await api(`/api/shipments/${id}/print`, { method: 'POST' });
      snackbar('Sent to printer', 'success');
    } else {
      printDialog(`/api/shipments/${id}/label`);
    }
  } catch (err) {
    snackbar(err.message, 'error');
  }
};

/* Push the group's tracking to its Shopify order. A shipment bought while
   Shopify was unreachable may carry only the scanned number (the backend
   resolves it) or, for older rows, no store at all — then ask for both. */
window.sendToShopify = (s) => {
  if (s.shopify_store_id && (s.shopify_order_id || s.shopify_order_name)) {
    retryWb(s.id);
    return;
  }
  linkShopifyOrder(s);
};

async function linkShopifyOrder(s) {
  let stores = [];
  try {
    stores = (await api('/api/shopify-stores')).filter((st) => st.is_active || st.id === s.shopify_store_id);
  } catch (err) {
    snackbar(err.message, 'error');
    return;
  }
  if (!stores.length) { snackbar('No Shopify stores are configured', 'error'); return; }
  const backdrop = document.getElementById('modal-backdrop');
  const boxNote = s.box_total > 1 ? ` All ${s.box_total} boxes are linked together.` : '';
  document.getElementById('modal').innerHTML = `
    <h3>Send to Shopify</h3>
    <p>This label has no Shopify order attached. Pick the store and enter the order number; the tracking is sent right away.${boxNote}</p>
    <div class="field mb-16" style="margin-top:12px">
      <label for="m-store">Store</label>
      <select id="m-store">${stores.map((st) => `<option value="${st.id}"${st.id === s.shopify_store_id ? ' selected' : ''}>${esc(st.name)}</option>`).join('')}</select>
    </div>
    <div class="field">
      <label for="m-number">Order number</label>
      <input id="m-number" type="text" autocomplete="off" placeholder="#1234" value="${esc(s.shopify_order_name || '')}">
    </div>
    <div class="actions">
      <button class="btn btn-text" id="m-cancel">Cancel</button>
      <button class="btn btn-primary" id="m-send">Send to Shopify</button>
    </div>`;
  backdrop.classList.add('show');
  const number = document.getElementById('m-number');
  number.focus();
  document.getElementById('m-cancel').addEventListener('click', () => backdrop.classList.remove('show'));
  const submit = async () => {
    const orderNumber = number.value.trim();
    if (!orderNumber) { number.focus(); return; }
    const btn = document.getElementById('m-send');
    btn.disabled = true;
    try {
      const res = await api(`/api/shipments/${s.id}/shopify-link`, {
        method: 'POST',
        body: { store_id: Number(document.getElementById('m-store').value), order_number: orderNumber },
      });
      backdrop.classList.remove('show');
      reportWriteback(res.writebacks || {});
      load();
    } catch (err) {
      btn.disabled = false;
      snackbar(err.message, 'error');
    }
  };
  document.getElementById('m-send').addEventListener('click', submit);
  number.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') submit(); });
}

function reportWriteback(wb) {
  const failed = Object.values(wb).some((v) => String(v).startsWith('error'));
  snackbar(failed ? Object.entries(wb).map(([k, v]) => `${k}: ${v}`).join('; ') : 'Sent to Shopify', failed ? 'error' : 'success');
}

window.retryWb = async (id) => {
  try {
    const res = await api(`/api/shipments/${id}/writeback`, { method: 'POST' });
    reportWriteback(res.writebacks || {});
    load();
  } catch (err) {
    snackbar(err.message, 'error');
  }
};

function providerLabel(id) {
  const row = allRows.find((r) => r.id === id);
  return (row && (row.provider_label || row.provider)) || 'the shipping provider';
}

async function callVoid(id) {
  const res = await api(`/api/shipments/${id}/void`, { method: 'POST' });
  if (res.ok) {
    const details = Object.entries(res.undo || {}).map(([k, v]) => `${k}: ${v}`).join('; ');
    snackbar(details ? `Label voided — ${details}` : 'Label voided', 'success');
  } else {
    snackbar(`Label voided at ${providerLabel(id)}, but: ${(res.errors || []).join('; ')} — use Retry undo`, 'error');
  }
  load();
}

window.voidShipment = (id, ref, source, boxTotal) => {
  const undoNote = source === 'shopify'
    ? 'the Shopify fulfillment is cancelled (tracking removed from the order)'
    : source === 'backoffice'
      ? 'the tracking number and shipping cost are cleared from the BackOffice invoice'
      : 'no order updates to undo';
  const boxNote = boxTotal > 1
    ? ` All ${boxTotal} boxes of this order are undone together.`
    : '';
  const backdrop = document.getElementById('modal-backdrop');
  document.getElementById('modal').innerHTML = `
    <h3>Undo shipment</h3>
    <p>Undo <strong>${ref}</strong>?</p>
    <p class="text-secondary" style="margin-top:8px">The label is cancelled at ${esc(providerLabel(id))}, and ${undoNote}.${boxNote}</p>
    <div class="actions">
      <button class="btn btn-text" id="m-cancel">Cancel</button>
      <button class="btn btn-danger" id="m-void">Undo shipment</button>
    </div>`;
  backdrop.classList.add('show');
  document.getElementById('m-cancel').addEventListener('click', () => backdrop.classList.remove('show'));
  document.getElementById('m-void').addEventListener('click', async () => {
    document.getElementById('m-void').disabled = true;
    try {
      backdrop.classList.remove('show');
      await callVoid(id);
    } catch (err) {
      backdrop.classList.remove('show');
      snackbar(err.message, 'error');
    }
  });
};

window.retryUndo = async (id) => {
  try {
    await callVoid(id);
  } catch (err) {
    snackbar(err.message, 'error');
  }
};

document.getElementById('refresh').addEventListener('click', load);
document.getElementById('search').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') load();
});
['date-from', 'date-to'].forEach((id) => {
  document.getElementById(id).addEventListener('change', load);
});
document.getElementById('size-filter').addEventListener('change', render);

const showSize = document.getElementById('show-size');
showSize.checked = localStorage.getItem('parcels.showSize') === '1';
function applySizeColumn() {
  document.querySelector('.parcels-table').classList.toggle('show-size', showSize.checked);
}
showSize.addEventListener('change', () => {
  localStorage.setItem('parcels.showSize', showSize.checked ? '1' : '0');
  const sizeFilter = document.getElementById('size-filter');
  if (!showSize.checked && sizeFilter.value) {
    sizeFilter.value = '';
    render();
  }
  applySizeColumn();
});
applySizeColumn();

/* Edge shadows on the pinned columns, shown only when content is actually
   hidden in that direction. */
const parcelsWrap = document.querySelector('.parcels-wrap');
function updateScrollShadows() {
  const max = parcelsWrap.scrollWidth - parcelsWrap.clientWidth;
  parcelsWrap.classList.toggle('shadow-start', parcelsWrap.scrollLeft > 0);
  parcelsWrap.classList.toggle('shadow-end', parcelsWrap.scrollLeft < max - 1);
}
parcelsWrap.addEventListener('scroll', updateScrollShadows, { passive: true });
new ResizeObserver(updateScrollShadows).observe(parcelsWrap);
new ResizeObserver(updateScrollShadows).observe(parcelsWrap.querySelector('table'));
updateScrollShadows();

loadUsers();
loadAccounts();
load();
