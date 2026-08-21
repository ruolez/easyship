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
    document.getElementById('user-filter').innerHTML =
      '<option value="">All</option>' +
      users.map((u) => `<option value="${esc(u)}">${esc(u)}</option>`).join('');
  } catch { /* filter stays open */ }
}

/* ---------- Client-side sorting & column filters over the fetched page ---------- */
let allRows = [];
let sortKey = null;
let sortDir = 1;

const SORT_VALUE = {
  ref: (s) => (s.shopify_order_name || s.backoffice_invoice_number || `#${s.id}`).toLowerCase(),
  user: (s) => (s.created_by || '').toLowerCase(),
  store: (s) => (s.service_name || '').toLowerCase(),
  address: (s) => formatAddress(s.destination).toLowerCase(),
  boxes: (s) => s.box_total || 1,
  weight: (s) => s.total_weight_lb ?? -1,
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
  const store = document.getElementById('store-filter').value;
  const carrier = document.getElementById('carrier-filter').value;
  const service = document.getElementById('service-filter').value;
  let rows = allRows.filter((s) =>
    (!store || s.service_name === store)
    && (!carrier || s.courier_umbrella_name === carrier)
    && (!service || s.courier_name === service));
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
    status: document.getElementById('status-filter').value,
    user: document.getElementById('user-filter').value,
    from: document.getElementById('date-from').value,
    to: document.getElementById('date-to').value,
  });
  const tbody = document.getElementById('parcels-body');
  const empty = document.getElementById('empty');
  empty.style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="13"><span class="spinner"></span> Loading…</td></tr>';
  try {
    allRows = await api(`/api/shipments?${params}`);
    const uniq = (vals) => [...new Set(vals.filter(Boolean))].sort();
    fillOptions('store-filter', uniq(allRows.map((s) => s.service_name)));
    fillOptions('carrier-filter', uniq(allRows.map((s) => s.courier_umbrella_name)));
    fillOptions('service-filter', uniq(allRows.map((s) => s.courier_name)));
    render();
  } catch (err) {
    allRows = [];
    tbody.innerHTML = '';
    empty.textContent = err.message;
    empty.style.display = '';
  }
}

function render() {
  const tbody = document.getElementById('parcels-body');
  const empty = document.getElementById('empty');
  const rows = visibleRows();
  if (!rows.length) {
    tbody.innerHTML = '';
    empty.textContent = 'No parcels found.';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map((s) => {
      const ref = s.shopify_order_name || s.backoffice_invoice_number || `#${s.id}`;
      const needsRetry = s.status === 'label_created' && s.box_number === 1 &&
        ((s.source === 'shopify' && !s.writeback_shopify_at) ||
         (s.source === 'backoffice' && !s.writeback_backoffice_at));
      const boxesCell = s.box_total > 1
        ? `<span class="chip static ${['label_created', 'fulfilled'].includes(s.status) ? 'ok' : 'warn'}">${s.box_number}/${s.box_total}</span>`
        : '1';
      const canResume = ['rated', 'error'].includes(s.status) && s.courier_service_id
        && s.provider_shipment_id && s.group_id;
      const numbers = (s.tracking_numbers || []).length ? s.tracking_numbers : (s.tracking_number ? [s.tracking_number] : []);
      const trackingCell = numbers.length
        ? `<span class="copy-wrap"><span class="mono">${esc(numbers[0])}</span>${numbers.length > 1 ? `<span class="chip static warn">+${numbers.length - 1}</span>` : ''}<button class="copy-btn" data-copy="${esc(numbers.join('\n'))}" title="Copy tracking number${numbers.length > 1 ? 's' : ''}" aria-label="Copy tracking">${COPY_ICON}</button></span>`
        : '';
      return `<tr>
        <td><strong>${copyable(ref, 'order number')}</strong></td>
        <td class="col-narrow">${esc(s.created_by)}</td>
        <td class="ellip store" title="${esc(s.service_name)}">${esc(s.service_name)}</td>
        <td class="ellip address" title="${esc(formatAddress(s.destination))}">${esc(formatAddress(s.destination))}</td>
        <td class="num col-narrow">${boxesCell}</td>
        <td class="num col-narrow">${s.total_weight_lb ?? ''}</td>
        <td class="ellip service" title="${esc(s.courier_name || '')}">${esc(s.courier_name || '')}${signatureChip(s.options)}</td>
        <td>${esc(s.courier_umbrella_name || '')}</td>
        <td class="num">${money(s.shipping_cost)}</td>
        <td title="${esc(numbers.join(', '))}">${trackingCell}</td>
        <td><span class="status status-${esc(s.status)}" title="${esc(s.error_message || '')}">${esc(s.status.replace('_', ' '))}</span></td>
        <td class="created">${esc(s.created_at)}</td>
        <td class="actions">
          ${canResume ? `<button class="btn btn-text btn-small" onclick="resumeBuy('${esc(s.group_id)}')">Resume labels</button>` : ''}
          ${s.has_label ? `<a class="btn btn-text btn-small" href="/api/shipments/${s.id}/label" target="_blank">Label</a>` : ''}
          ${s.has_label ? `<button class="btn btn-text btn-small" onclick="reprint(${s.id})" title="Send to printer" aria-label="Send to printer">${ICON_PRINTER}</button>` : ''}
          ${needsRetry ? `<button class="btn btn-text btn-small" onclick="retryWb(${s.id})">Retry writeback</button>` : ''}
          ${['label_created', 'fulfilled'].includes(s.status) ? `<button class="btn btn-danger btn-small" onclick="voidShipment(${s.id}, '${esc(ref)}', '${esc(s.source)}', ${s.box_total})">Undo</button>` : ''}
          ${s.status === 'voided' && s.error_message ? `<button class="btn btn-danger btn-small" onclick="retryUndo(${s.id})">Retry undo</button>` : ''}
        </td>
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

window.retryWb = async (id) => {
  try {
    const res = await api(`/api/shipments/${id}/writeback`, { method: 'POST' });
    const wb = res.writebacks || {};
    const failed = Object.values(wb).some((v) => String(v).startsWith('error'));
    snackbar(failed ? Object.entries(wb).map(([k, v]) => `${k}: ${v}`).join('; ') : 'Writeback complete', failed ? 'error' : 'success');
    load();
  } catch (err) {
    snackbar(err.message, 'error');
  }
};

const PROVIDER_LABELS = { easyship: 'Easyship', shippo: 'GoShippo', easypost: 'EasyPost', shipstation: 'ShipStation' };
function providerLabel(id) {
  const row = allRows.find((r) => r.id === id);
  const name = (row && row.provider) || '';
  return PROVIDER_LABELS[name] || (name ? name.charAt(0).toUpperCase() + name.slice(1) : 'the shipping provider');
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
['status-filter', 'user-filter', 'date-from', 'date-to'].forEach((id) => {
  document.getElementById(id).addEventListener('change', load);
});
['store-filter', 'carrier-filter', 'service-filter'].forEach((id) => {
  document.getElementById(id).addEventListener('change', render);
});

loadUsers();
load();
