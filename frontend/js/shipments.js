initNav('shipments');

async function load() {
  const q = encodeURIComponent(document.getElementById('search').value.trim());
  const status = document.getElementById('status-filter').value;
  const tbody = document.getElementById('shipments-body');
  const empty = document.getElementById('empty');
  empty.style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="10"><span class="spinner"></span> Loading…</td></tr>';
  try {
    const rows = await api(`/api/shipments?q=${q}&status=${status}`);
    if (!rows.length) {
      tbody.innerHTML = '';
      empty.textContent = 'No shipments found.';
      empty.style.display = '';
      return;
    }
    tbody.innerHTML = rows.map((s) => {
      const ref = s.shopify_order_name || s.backoffice_invoice_number || `#${s.id}`;
      const dest = s.destination || {};
      const shipTo = dest.company || dest.contact || '';
      const needsRetry = s.status === 'label_created' &&
        ((s.source === 'shopify' && !s.writeback_shopify_at) ||
         (s.source === 'backoffice' && !s.writeback_backoffice_at));
      return `<tr>
        <td>${esc(s.created_at)}</td>
        <td>${esc(s.source)}</td>
        <td><strong>${esc(ref)}</strong></td>
        <td class="wrap">${esc(shipTo)}</td>
        <td>${esc(s.courier_name || '')}</td>
        <td class="mono">${esc(s.tracking_number || '')}</td>
        <td>${money(s.shipping_cost)}</td>
        <td>${esc(s.created_by)}</td>
        <td><span class="status status-${esc(s.status)}" title="${esc(s.error_message || '')}">${esc(s.status.replace('_', ' '))}</span></td>
        <td>
          ${s.has_label ? `<a class="btn btn-text btn-small" href="/api/shipments/${s.id}/label" target="_blank">Label</a>` : ''}
          ${needsRetry ? `<button class="btn btn-text btn-small" onclick="retryWb(${s.id})">Retry writeback</button>` : ''}
          ${['label_created', 'fulfilled'].includes(s.status) ? `<button class="btn btn-danger btn-small" onclick="voidShipment(${s.id}, '${esc(ref)}')">Void</button>` : ''}
        </td>
      </tr>`;
    }).join('');
  } catch (err) {
    tbody.innerHTML = '';
    empty.textContent = err.message;
    empty.style.display = '';
  }
}

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

window.voidShipment = (id, ref) => {
  const backdrop = document.getElementById('modal-backdrop');
  document.getElementById('modal').innerHTML = `
    <h3>Void label</h3>
    <p>Void the label for <strong>${ref}</strong>? This cancels the shipment at Easyship.</p>
    <div class="actions">
      <button class="btn btn-text" id="m-cancel">Cancel</button>
      <button class="btn btn-danger" id="m-void">Void label</button>
    </div>`;
  backdrop.classList.add('show');
  document.getElementById('m-cancel').addEventListener('click', () => backdrop.classList.remove('show'));
  document.getElementById('m-void').addEventListener('click', async () => {
    try {
      await api(`/api/shipments/${id}/void`, { method: 'POST' });
      backdrop.classList.remove('show');
      snackbar('Label voided', 'success');
      load();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

document.getElementById('refresh').addEventListener('click', load);
document.getElementById('search').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') load();
});
document.getElementById('status-filter').addEventListener('change', load);

load();
