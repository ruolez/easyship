initNav('parcels');

let clientSettings = { print_mode: 'browser' };
api('/api/settings/client').then((s) => { clientSettings = s; }).catch(() => {});

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
  service: (s) => (s.service_name || '').toLowerCase(),
  address: (s) => formatAddress(s.destination).toLowerCase(),
  boxes: (s) => s.box_total || 1,
  weight: (s) => s.total_weight_lb ?? -1,
  courier: (s) => (s.courier_name || '').toLowerCase(),
  carrier: (s) => (s.courier_umbrella_name || '').toLowerCase(),
  cost: (s) => s.shipping_cost ?? -1,
  tracking: (s) => s.tracking_number || '',
  status: (s) => s.status || '',
  shipped: (s) => s.label_created_at || '',
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
  tbody.innerHTML = '<tr><td colspan="14"><span class="spinner"></span> Loading…</td></tr>';
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
        && s.easyship_shipment_id && s.group_id;
      return `<tr>
        <td><strong>${esc(ref)}</strong></td>
        <td>${esc(s.created_by)}</td>
        <td class="ellip" style="max-width:150px" title="${esc(s.service_name)}">${esc(s.service_name)}</td>
        <td class="ellip" style="max-width:260px" title="${esc(formatAddress(s.destination))}">${esc(formatAddress(s.destination))}</td>
        <td class="num">${boxesCell}</td>
        <td class="num">${s.total_weight_lb ?? ''}</td>
        <td class="ellip" style="max-width:170px" title="${esc(s.courier_name || '')}">${esc(s.courier_name || '')}</td>
        <td>${esc(s.courier_umbrella_name || '')}</td>
        <td class="num">${money(s.shipping_cost)}</td>
        <td class="mono" title="${esc((s.tracking_numbers || []).join(', '))}">${esc(s.tracking_number || '')}${(s.tracking_numbers || []).length > 1 ? ` <span class="chip static warn">+${s.tracking_numbers.length - 1}</span>` : ''}</td>
        <td><span class="status status-${esc(s.status)}" title="${esc(s.error_message || '')}">${esc(s.status.replace('_', ' '))}</span></td>
        <td>${esc((s.label_created_at || '').split(' ')[0] || '')}</td>
        <td>${esc(s.created_at)}</td>
        <td style="white-space:nowrap">
          ${canResume ? `<button class="btn btn-text btn-small" onclick="resumeBuy('${esc(s.group_id)}')">Resume labels</button>` : ''}
          ${s.has_label ? `<a class="btn btn-text btn-small" href="/api/shipments/${s.id}/label" target="_blank">Label</a>` : ''}
          ${s.has_label ? `<button class="btn btn-text btn-small" onclick="reprint(${s.id})" title="Send to printer">🖨</button>` : ''}
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

window.reprint = async (id) => {
  try {
    if (clientSettings.print_mode === 'browserprint') {
      await ZebraPrint.printLabelUrl(`/api/shipments/${id}/label`);
      snackbar('Label sent to Zebra printer', 'success');
    } else {
      await api(`/api/shipments/${id}/print`, { method: 'POST' });
      snackbar('Sent to printer', 'success');
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

async function callVoid(id) {
  const res = await api(`/api/shipments/${id}/void`, { method: 'POST' });
  if (res.ok) {
    const details = Object.entries(res.undo || {}).map(([k, v]) => `${k}: ${v}`).join('; ');
    snackbar(details ? `Label voided — ${details}` : 'Label voided', 'success');
  } else {
    snackbar(`Label voided at Easyship, but: ${(res.errors || []).join('; ')} — use Retry undo`, 'error');
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
    <p class="text-secondary" style="margin-top:8px">The label is cancelled at Easyship (UPS/USPS), and ${undoNote}.${boxNote}</p>
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
