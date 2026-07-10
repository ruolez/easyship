const params = new URLSearchParams(location.search);
const source = params.get('source') || 'manual';
let orderContext = { source };
let localShipmentId = null;
let rates = [];
let selectedRate = null;
let orderItems = [];
let clientSettings = { placeholder_email: '', print_mode: 'browser' };
let savedBoxes = [];

initNav('scan');
init();

async function init() {
  [clientSettings, savedBoxes] = await Promise.all([
    api('/api/settings/client').catch(() => clientSettings),
    api('/api/boxes').catch(() => []),
  ]);
  addParcelRow();
  await prefill();
  applyPlaceholderEmail();
  focusNextField();
}

function applyPlaceholderEmail() {
  const el = document.getElementById('d-email');
  if (!el.value.trim() && clientSettings.placeholder_email) {
    el.value = clientSettings.placeholder_email;
  }
}

function focusNextField() {
  // Land the cursor where the user must type next to get a label out.
  const required = ['d-address1', 'd-city', 'd-state', 'd-zip'];
  for (const id of required) {
    const el = document.getElementById(id);
    if (!el.value.trim()) { el.focus(); return; }
  }
  // Always land in the weight field — even when a weight was auto-calculated
  // (e.g. from Shopify items), the packer confirms the real total on the scale.
  // The prefilled value is selected so typing replaces it, Enter accepts it.
  const weights = [...document.querySelectorAll('.p-weight')];
  const target = weights.find((w) => !w.value) || weights[0];
  if (target) {
    target.focus();
    if (target.value) target.select();
    return;
  }
  document.getElementById('get-rates').focus();
}

function setStep(n) {
  for (let i = 1; i <= 4; i++) {
    const el = document.getElementById(`step-${i}`);
    el.classList.toggle('active', i === n);
    el.classList.toggle('done', i < n);
  }
}

/* ---------- Prefill from order source ---------- */
async function prefill() {
  try {
    if (source === 'shopify') {
      const storeId = params.get('store_id');
      const orderId = params.get('order_id');
      const o = await api(`/api/shopify/orders/${encodeURIComponent(orderId)}?store_id=${storeId}`);
      orderContext = {
        source, store_id: Number(storeId), order_id: o.id, order_name: o.name,
      };
      fillDestination(o.destination);
      orderItems = o.items || [];
      showOrderSummary(`Shopify order <strong>${esc(o.name)}</strong> — ${esc(o.customer || '')}`, orderItems);
      if (o.total_weight_lb) {
        document.querySelector('.p-weight').value = o.total_weight_lb;
      }
    } else if (source === 'backoffice') {
      const invoiceId = params.get('invoice_id');
      const dbId = Number(params.get('db_id'));
      const inv = await api(`/api/backoffice/${dbId}/invoices/${invoiceId}`);
      orderContext = {
        source, db_id: dbId, invoice_id: inv.invoice_id, invoice_number: inv.invoice_number,
      };
      fillDestination(inv.destination);
      orderItems = inv.items || [];
      showOrderSummary(
        `BackOffice invoice <strong>${esc(inv.invoice_number)}</strong> — ${esc(inv.business_name || '')}`,
        orderItems
      );
      seedParcels(inv.no_boxes, inv.total_weight);
    }
  } catch (err) {
    snackbar(`Could not load order: ${err.message}`, 'error');
  }
}

function fillDestination(d) {
  if (!d) return;
  const map = {
    'd-company': d.company, 'd-contact': d.contact, 'd-address1': d.address1,
    'd-address2': d.address2, 'd-city': d.city, 'd-state': d.state,
    'd-zip': d.zip, 'd-phone': d.phone, 'd-email': d.email,
  };
  Object.entries(map).forEach(([id, v]) => {
    if (v) document.getElementById(id).value = v;
  });
}

function showOrderSummary(html, items) {
  const el = document.getElementById('order-summary');
  el.style.display = '';
  let itemsHtml = '';
  if (items && items.length) {
    itemsHtml = `<div class="table-wrap mt-16"><table>
      <thead><tr><th>Item</th><th>SKU</th><th>Qty</th><th>Value</th></tr></thead>
      <tbody>${items.map((i) => `<tr>
        <td class="wrap">${esc(i.description)}</td><td class="mono">${esc(i.sku || '')}</td>
        <td>${i.quantity}</td><td>${money(i.value)}</td>
      </tr>`).join('')}</tbody></table></div>`;
  }
  el.innerHTML = `<h2>${html}</h2>${itemsHtml}`;
}

/* ---------- Parcels ---------- */
function addParcelRow(weight = '', length = '', width = '', height = '') {
  const div = document.createElement('div');
  div.className = 'parcel-row';
  const n = document.querySelectorAll('.parcel-row').length + 1;
  const boxOptions = ['<option value="">Custom size</option>']
    .concat(savedBoxes.map((b) =>
      `<option value="${b.id}">${esc(b.name)} — ${b.length}×${b.width}×${b.height}</option>`))
    .join('');
  div.innerHTML = `
    <div class="parcel-num">${n}</div>
    <div class="field"><label>Box</label><select class="p-box">${boxOptions}</select></div>
    <div class="field parcel-weight"><label>Weight (lb)</label><input class="p-weight" type="number" step="0.1" min="0.1" value="${weight}" placeholder="0.0"></div>
    <div class="field">
      <label>Dimensions — L × W × H (in)</label>
      <div class="dims-group">
        <input class="p-length" type="number" step="0.1" value="${length}" placeholder="L">
        <span>×</span>
        <input class="p-width" type="number" step="0.1" value="${width}" placeholder="W">
        <span>×</span>
        <input class="p-height" type="number" step="0.1" value="${height}" placeholder="H">
      </div>
    </div>
    <button class="remove-parcel" title="Remove this box">✕</button>`;
  const boxSelect = div.querySelector('.p-box');
  const applyBox = () => {
    const box = savedBoxes.find((b) => b.id === Number(boxSelect.value));
    if (!box) return;
    div.querySelector('.p-length').value = box.length;
    div.querySelector('.p-width').value = box.width;
    div.querySelector('.p-height').value = box.height;
    localStorage.setItem('easyship.lastBox', box.id);
  };
  boxSelect.addEventListener('change', () => {
    applyBox();
    div.querySelector('.p-weight').focus();
  });
  // Preselect the last-used box so scan flow only needs a weight
  if (!length && !width && !height) {
    const lastBox = Number(localStorage.getItem('easyship.lastBox'));
    if (lastBox && savedBoxes.some((b) => b.id === lastBox)) {
      boxSelect.value = lastBox;
      applyBox();
    }
  }
  ['.p-length', '.p-width', '.p-height'].forEach((sel) => {
    div.querySelector(sel).addEventListener('input', () => { boxSelect.value = ''; });
  });
  div.querySelector('.remove-parcel').addEventListener('click', () => {
    if (document.querySelectorAll('.parcel-row').length > 1) {
      div.remove();
      document.querySelectorAll('.parcel-row .parcel-num')
        .forEach((el, i) => { el.textContent = i + 1; });
    }
  });
  div.querySelector('.p-weight').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      document.getElementById('get-rates').click();
    }
  });
  document.getElementById('parcel-list').appendChild(div);
}

function seedParcels(noBoxes, totalWeight) {
  const boxes = Math.max(Number(noBoxes) || 1, 1);
  const per = totalWeight ? (Number(totalWeight) / boxes).toFixed(1) : '';
  document.getElementById('parcel-list').innerHTML = '';
  for (let i = 0; i < boxes; i++) addParcelRow(per);
}

document.getElementById('add-parcel').addEventListener('click', () => {
  addParcelRow();
  const rows = document.querySelectorAll('.parcel-row');
  rows[rows.length - 1].querySelector('.p-weight').focus();
});

function collectParcels() {
  return [...document.querySelectorAll('.parcel-row')].map((row) => ({
    weight: row.querySelector('.p-weight').value,
    length: row.querySelector('.p-length').value,
    width: row.querySelector('.p-width').value,
    height: row.querySelector('.p-height').value,
  }));
}

function collectDestination() {
  const val = (id) => document.getElementById(id).value.trim();
  return {
    company: val('d-company'), contact: val('d-contact'),
    address1: val('d-address1'), address2: val('d-address2'),
    city: val('d-city'), state: val('d-state').toUpperCase(), zip: val('d-zip'),
    phone: val('d-phone'), email: val('d-email'), country: 'US',
  };
}

/* ---------- Rates ---------- */
document.getElementById('get-rates').addEventListener('click', async () => {
  const btn = document.getElementById('get-rates');
  const spinner = document.getElementById('rates-spinner');
  btn.disabled = true;
  spinner.style.display = '';
  setStep(3);
  try {
    const res = await api('/api/shipments/rates', {
      method: 'POST',
      body: {
        ...orderContext,
        destination: collectDestination(),
        parcels: collectParcels(),
        items: orderItems,
      },
    });
    localShipmentId = res.shipment_id;
    rates = res.rates;
    renderRates();
    document.getElementById('panel-rates').style.display = '';
    document.getElementById('panel-rates').scrollIntoView({ behavior: 'smooth' });
  } catch (err) {
    snackbar(err.message, 'error');
    setStep(2);
  } finally {
    btn.disabled = false;
    spinner.style.display = 'none';
  }
});

function renderRates() {
  const list = document.getElementById('rate-list');
  selectedRate = null;
  document.getElementById('buy-label').disabled = true;
  list.innerHTML = rates.map((r, i) => `
    <div class="rate-card" data-idx="${i}" tabindex="0">
      <div class="courier">${esc(r.courier_name)}</div>
      <div class="price">${money(r.total_charge)} <span class="text-secondary" style="font-size:13px">${esc(r.currency || 'USD')}</span></div>
      <div class="meta">${r.min_delivery_time ?? '?'}–${r.max_delivery_time ?? '?'} business days</div>
      ${r.value_for_money_rank === 1 ? '<div class="chip static ok mt-16" style="margin-top:8px">Best value</div>' : ''}
    </div>`).join('');
  const select = (card) => {
    list.querySelectorAll('.rate-card').forEach((c) => c.classList.remove('selected'));
    card.classList.add('selected');
    selectedRate = rates[Number(card.dataset.idx)];
    const buyBtn = document.getElementById('buy-label');
    buyBtn.disabled = false;
    buyBtn.focus();
  };
  list.querySelectorAll('.rate-card').forEach((card) => {
    card.addEventListener('click', () => select(card));
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(card); }
    });
  });
  const first = list.querySelector('.rate-card');
  if (first) first.focus();
}

/* ---------- Buy ---------- */
document.getElementById('buy-label').addEventListener('click', async () => {
  if (!selectedRate || !localShipmentId) return;
  const btn = document.getElementById('buy-label');
  const spinner = document.getElementById('buy-spinner');
  btn.disabled = true;
  spinner.style.display = '';
  try {
    const res = await api(`/api/shipments/${localShipmentId}/buy`, {
      method: 'POST',
      body: { courier_service_id: selectedRate.courier_service_id, rate: selectedRate },
    });
    setStep(4);
    showResult(res);
  } catch (err) {
    snackbar(err.message, 'error');
    btn.disabled = false;
  } finally {
    spinner.style.display = 'none';
  }
});

function showResult(s) {
  ['panel-destination', 'panel-parcels', 'panel-rates', 'rates-actions'].forEach((id) => {
    document.getElementById(id).style.display = 'none';
  });
  const panel = document.getElementById('panel-result');
  panel.style.display = '';
  document.getElementById('r-tracking').textContent = s.tracking_number || '(pending)';
  document.getElementById('r-courier').textContent = s.courier_name || '';
  document.getElementById('r-cost').textContent = money(s.shipping_cost);

  const wb = document.getElementById('r-writebacks');
  const chips = [];
  if (s.source === 'shopify') {
    chips.push(s.writeback_shopify_at
      ? '<span class="chip static ok">✓ Shopify fulfilled</span>'
      : `<span class="chip static err">✕ Shopify update failed</span> <button class="btn btn-text btn-small" onclick="retryWriteback()">Retry</button>`);
  }
  if (s.source === 'backoffice') {
    chips.push(s.writeback_backoffice_at
      ? '<span class="chip static ok">✓ BackOffice updated</span>'
      : `<span class="chip static err">✕ BackOffice update failed</span> <button class="btn btn-text btn-small" onclick="retryWriteback()">Retry</button>`);
  }
  if (s.printed === 'ok') {
    chips.push('<span class="chip static ok">🖨 Sent to printer</span>');
  } else if (s.printed && s.printed.startsWith('error')) {
    chips.push(`<span class="chip static err">🖨 ${esc(s.printed)}</span> <button class="btn btn-text btn-small" onclick="printAgain(${s.id})">Print again</button>`);
  }
  if (s.error_message) chips.push(`<div class="text-secondary mt-16">${esc(s.error_message)}</div>`);
  wb.innerHTML = chips.join(' ');

  if (s.has_label) {
    const url = `/api/shipments/${s.id}/label`;
    document.getElementById('r-download').href = url;
    const preview = document.getElementById('r-preview');
    preview.src = url;
    preview.style.display = '';
    if (s.printed == null && clientSettings.print_mode === 'browser') {
      // Local printer: pop the browser print dialog as soon as the PDF loads
      preview.addEventListener('load', () => {
        try {
          preview.contentWindow.focus();
          preview.contentWindow.print();
        } catch { window.open(url, '_blank'); }
      }, { once: true });
    }
  } else {
    document.getElementById('r-download').style.display = 'none';
  }
  const nextBtn = document.getElementById('r-next');
  if (nextBtn) nextBtn.focus();
  panel.scrollIntoView({ behavior: 'smooth' });
}

window.printAgain = async (id) => {
  try {
    await api(`/api/shipments/${id}/print`, { method: 'POST' });
    snackbar('Sent to printer', 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  }
};

window.retryWriteback = async () => {
  try {
    const res = await api(`/api/shipments/${localShipmentId}/writeback`, { method: 'POST' });
    showResult(res);
    snackbar('Writeback retried', 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  }
};

/* Step highlighting on focus */
document.getElementById('panel-destination').addEventListener('focusin', () => setStep(1));
document.getElementById('panel-parcels').addEventListener('focusin', () => setStep(2));
