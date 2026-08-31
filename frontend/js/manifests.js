initNav('manifests');

let eligibleRows = [];
const selected = new Set();

function orderRef(s) {
  return s.shopify_order_name || s.backoffice_invoice_number || `#${s.id}`;
}

function updateToolbar() {
  const count = selected.size;
  document.getElementById('generate').disabled = count === 0;
  document.getElementById('selected-count').textContent =
    count ? `${count} of ${eligibleRows.length} selected` : (eligibleRows.length ? `${eligibleRows.length} parcels` : '');
  const all = document.getElementById('select-all');
  all.checked = eligibleRows.length > 0 && count === eligibleRows.length;
  all.indeterminate = count > 0 && count < eligibleRows.length;
}

function renderEligible(message) {
  const tbody = document.getElementById('eligible-body');
  const empty = document.getElementById('eligible-empty');
  if (!eligibleRows.length) {
    tbody.innerHTML = '';
    empty.textContent = message || 'No USPS parcels from today are waiting for a manifest.';
    empty.style.display = '';
    updateToolbar();
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = eligibleRows.map((s) => `
    <tr>
      <td><input type="checkbox" class="row-check" data-id="${s.id}" ${selected.has(s.id) ? 'checked' : ''}></td>
      <td><strong>${esc(orderRef(s))}</strong></td>
      <td class="col-narrow">${esc(s.created_by)}</td>
      <td class="num col-narrow">${s.box_total > 1 ? `${s.box_number}/${s.box_total}` : '1'}</td>
      <td class="ellip" title="${esc(s.courier_name || '')}">${esc(s.courier_name || '')}</td>
      <td class="mono">${esc(s.tracking_number || '')}</td>
      <td>${esc(s.label_created_at || s.created_at || '')}</td>
    </tr>`).join('');
  updateToolbar();
}

async function loadEligible() {
  const provider = window.activeProvider();
  const tbody = document.getElementById('eligible-body');
  const empty = document.getElementById('eligible-empty');
  empty.style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="7"><span class="spinner"></span> Loading…</td></tr>';
  if (!provider) {
    eligibleRows = [];
    selected.clear();
    renderEligible('No shipping provider is enabled — pick one in the sidebar.');
    return;
  }
  try {
    const res = await api(`/api/manifests/eligible?provider=${encodeURIComponent(provider)}`);
    eligibleRows = res.supported ? res.shipments : [];
    [...selected].forEach((id) => {
      if (!eligibleRows.some((s) => s.id === id)) selected.delete(id);
    });
    renderEligible(res.supported ? '' : 'This shipping provider does not support USPS manifests.');
  } catch (err) {
    eligibleRows = [];
    selected.clear();
    renderEligible(err.message);
  }
}

async function loadHistory() {
  const tbody = document.getElementById('history-body');
  const empty = document.getElementById('history-empty');
  empty.style.display = 'none';
  try {
    const rows = await api('/api/manifests');
    if (!rows.length) {
      tbody.innerHTML = '';
      empty.textContent = 'No manifests yet.';
      empty.style.display = '';
      return;
    }
    tbody.innerHTML = rows.map((m) => `
      <tr>
        <td>${esc(m.created_at || '')}</td>
        <td class="col-narrow">${esc(m.created_by || '')}</td>
        <td>${esc(m.provider_label || '')}</td>
        <td class="num col-narrow">${m.shipment_count || ''}</td>
        <td class="mono ellip" title="${esc(m.ref_number || m.provider_manifest_id || '')}">${esc(m.ref_number || m.provider_manifest_id || '')}</td>
        <td><span class="status status-${m.status === 'ready' ? 'fulfilled' : m.status === 'failed' ? 'error' : 'rated'}" title="${esc(m.error_message || '')}">${esc(m.status)}</span></td>
        <td class="actions">
          ${m.has_document ? `<a class="btn btn-text btn-small" href="/api/manifests/${m.id}/document" target="_blank">Open</a>` : ''}
          ${m.has_document ? `<button class="btn btn-text btn-small" onclick="printManifest(${m.id})" title="Print" aria-label="Print manifest">${ICON_PRINTER}</button>` : ''}
        </td>
      </tr>`).join('');
  } catch (err) {
    tbody.innerHTML = '';
    empty.textContent = err.message;
    empty.style.display = '';
  }
}

window.printManifest = (id) => {
  printPdfUrl(`/api/manifests/${id}/document`).catch((err) => snackbar(err.message, 'error'));
};

async function generate() {
  const ids = [...selected];
  const btn = document.getElementById('generate');
  const backdrop = document.getElementById('modal-backdrop');
  document.getElementById('modal').innerHTML = `
    <h3>Generate manifest</h3>
    <p>Create a USPS manifest for <strong>${ids.length}</strong> parcel${ids.length > 1 ? 's' : ''}?</p>
    <p class="text-secondary" style="margin-top:8px">USPS gets one barcode covering all of them — the driver scans it at pickup. Parcels on a manifest cannot be added to another one.</p>
    <div class="actions">
      <button class="btn btn-text" id="m-cancel">Cancel</button>
      <button class="btn btn-primary" id="m-confirm">Generate</button>
    </div>`;
  backdrop.classList.add('show');
  document.getElementById('m-cancel').addEventListener('click', () => backdrop.classList.remove('show'));
  document.getElementById('m-confirm').addEventListener('click', async () => {
    backdrop.classList.remove('show');
    btn.disabled = true;
    btn.textContent = 'Generating…';
    try {
      const res = await api('/api/manifests', {
        method: 'POST',
        body: { provider: window.activeProvider(), shipment_ids: ids },
      });
      const manifests = res.manifests || [];
      snackbar(`Manifest created — ${manifests.length > 1 ? manifests.length + ' documents' : 'sending to printer'}`, 'success');
      selected.clear();
      manifests.filter((m) => m.has_document).forEach((m) => window.printManifest(m.id));
    } catch (err) {
      snackbar(err.message, 'error');
    } finally {
      btn.textContent = 'Generate manifest';
      loadEligible();
      loadHistory();
    }
  });
}

document.getElementById('eligible-body').addEventListener('change', (e) => {
  const box = e.target.closest('.row-check');
  if (!box) return;
  const id = Number(box.dataset.id);
  if (box.checked) selected.add(id);
  else selected.delete(id);
  updateToolbar();
});

document.getElementById('select-all').addEventListener('change', (e) => {
  selected.clear();
  if (e.target.checked) eligibleRows.forEach((s) => selected.add(s.id));
  renderEligible();
});

document.getElementById('generate').addEventListener('click', generate);
document.getElementById('refresh-eligible').addEventListener('click', () => {
  loadEligible();
  loadHistory();
});
window.addEventListener('easyship:provider', () => {
  selected.clear();
  loadEligible();
});

loadEligible();
loadHistory();
