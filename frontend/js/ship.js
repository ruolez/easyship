const params = new URLSearchParams(location.search);
const source = params.get('source') || 'manual';
let orderContext = { source };
let localShipmentId = null;
let groupId = null;
let boxCount = 1;
let rates = [];
let selectedRate = null;
let orderItems = [];
let clientSettings = { placeholder_email: '', print_mode: 'browser', countdown_seconds: 5 };
let savedBoxes = [];
let lastLabelUrl = null;

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
      if ((o.existing_tracking || []).length && !params.get('reship_ack')) {
        await confirmReship(`Shopify order ${o.name}`, o.existing_tracking,
          "New tracking numbers are added to the order's existing fulfillment — it stays fulfilled.");
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
      if ((inv.tracking_no || '').trim() && !params.get('reship_ack')) {
        await confirmReship(`Invoice ${inv.invoice_number}`, [inv.tracking_no.trim()],
          'New tracking numbers are added to the invoice Notes in BackOffice — the existing tracking number is kept.');
      }
    }
  } catch (err) {
    snackbar(`Could not load order: ${err.message}`, 'error');
  }
}

/* Warn that this order was already processed. Continue resolves and the flow
   proceeds; Cancel goes back to the scan page. */
function confirmReship(what, numbers, note) {
  return new Promise((resolve) => {
    const backdrop = document.getElementById('modal-backdrop');
    document.getElementById('modal').innerHTML = `
      <h3>Order already processed</h3>
      <p><strong>${esc(what)}</strong> already has tracking:</p>
      <p class="mono" style="margin-top:8px">${numbers.map(esc).join('<br>')}</p>
      <p class="text-secondary" style="margin-top:8px">${esc(note)}</p>
      <div class="actions">
        <button class="btn btn-text" id="m-cancel">Cancel</button>
        <button class="btn btn-primary" id="m-continue">Continue</button>
      </div>`;
    backdrop.classList.add('show');
    document.getElementById('m-cancel').addEventListener('click', () => { location.href = '/index.html'; });
    document.getElementById('m-continue').addEventListener('click', () => {
      backdrop.classList.remove('show');
      resolve();
    });
    document.getElementById('m-continue').focus();
  });
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
    groupId = res.group_id;
    boxCount = res.box_count || 1;
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
function renderBuyProgress(progress) {
  const el = document.getElementById('buy-progress');
  if (!progress || !progress.boxes) { el.style.display = 'none'; return; }
  el.style.display = '';
  const statusLabel = {
    purchasing: '<span class="spinner"></span> purchasing…',
    generating: '<span class="spinner"></span> generating label…',
    ready: '✓ label ready',
    failed: '✕ failed',
  };
  el.innerHTML = progress.boxes.map((b) => `
    <div class="row mb-16" style="align-items:center;gap:10px">
      <div class="fixed"><span class="parcel-num" style="display:inline-flex;width:26px;height:26px;border-radius:50%;background:var(--primary-light);color:var(--primary);font-weight:650;font-size:12.5px;align-items:center;justify-content:center">${b.box}</span></div>
      <div class="fixed" style="min-width:170px">${b.status === 'ready' ? '<span class="chip static ok">✓ label ready</span>' : b.status === 'failed' ? '<span class="chip static err">✕ failed</span>' : `<span class="text-secondary">${statusLabel[b.status] || b.status}</span>`}</div>
      <div class="fixed mono" style="font-size:13px">${b.tracking ? esc(b.tracking) : ''}</div>
      ${b.error ? `<div class="fixed" style="font-size:12px;color:var(--error);max-width:420px;white-space:normal">${esc(b.error)}</div>` : ''}
    </div>`).join('');
  const statusEl = document.getElementById('buy-status');
  if (progress.state === 'finalizing') statusEl.textContent = progress.message || 'Saving label…';
  else {
    const done = progress.boxes.filter((b) => b.status === 'ready').length;
    statusEl.textContent = `Generating labels… ${done}/${progress.boxes.length} ready`;
  }
}

let buyPollTimer = null;

document.getElementById('buy-label').addEventListener('click', async () => {
  if (!selectedRate || !groupId) return;
  const btn = document.getElementById('buy-label');
  const spinner = document.getElementById('buy-spinner');
  btn.disabled = true;
  spinner.style.display = '';
  try {
    await api(`/api/shipments/group/${groupId}/buy`, {
      method: 'POST',
      body: { courier_service_id: selectedRate.courier_service_id, rate: selectedRate },
    });
  } catch (err) {
    snackbar(err.message, 'error');
    btn.disabled = false;
    spinner.style.display = 'none';
    return;
  }
  buyPollTimer = setInterval(async () => {
    let g;
    try {
      g = await api(`/api/shipments/group/${groupId}`);
    } catch { return; } // transient poll failure — keep polling
    const progress = g.progress || {};
    renderBuyProgress(progress);
    const allDone = g.shipments.every((r) => ['label_created', 'fulfilled'].includes(r.status));
    if (progress.state === 'done' && allDone) {
      clearInterval(buyPollTimer);
      spinner.style.display = 'none';
      setStep(4);
      showGroupResult(g);
    } else if (progress.state === 'retry' || progress.state === 'error') {
      clearInterval(buyPollTimer);
      spinner.style.display = 'none';
      snackbar(progress.message || 'Label purchase did not complete', 'error');
      if (progress.state === 'retry') btn.disabled = false;
    }
  }, 1500);
});

function showGroupResult(g) {
  const primary = g.shipments[0];
  const progress = g.progress || {};
  const numbers = g.shipments.map((r) => r.tracking_number).filter(Boolean);
  const cost = g.shipments.reduce((sum, r) => sum + (r.shipping_cost || 0), 0);
  showResult({
    ...primary,
    printed: progress.printed,
    shipping_cost: cost || primary.shipping_cost,
    tracking_list: numbers,
    label_url: `/api/shipments/group/${g.group_id}/label`,
    has_label: g.shipments.some((r) => r.has_label),
  });
}

function showResult(s) {
  // Keep the address + boxes columns visible; the result fills the right column.
  const panel = document.getElementById('panel-result');
  panel.style.display = '';
  const numbers = s.tracking_list && s.tracking_list.length
    ? s.tracking_list
    : (s.tracking_number ? [s.tracking_number] : []);
  document.getElementById('r-tracking').innerHTML = numbers.length
    ? numbers.map((n, i) => `<div>${numbers.length > 1 ? `<span class="text-secondary">Box ${i + 1}:</span> ` : ''}${esc(n)}</div>`).join('')
    : '(pending)';
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
    const url = s.label_url || `/api/shipments/${s.id}/label`;
    lastLabelUrl = url;
    document.getElementById('r-download').href = url;
    if (clientSettings.print_mode === 'browserprint') {
      if (s.printed == null) sendToZebra(url); // ZPL has no visual preview; Open label still works
    } else if (s.printed == null && clientSettings.print_mode === 'browser') {
      // Local printer: pop the browser print dialog with every page (one per box)
      printPdfUrl(url).catch((err) => { cancelAutoAdvance(); snackbar(err.message, 'error'); });
    }
  } else {
    document.getElementById('r-download').style.display = 'none';
  }
  const nextBtn = document.getElementById('r-next');
  if (nextBtn) nextBtn.focus();
  panel.scrollIntoView({ behavior: 'smooth' });
  // Auto-advance to the next order once a label prints cleanly. A print error
  // pauses the countdown so the packer can reprint first.
  const printFailed = typeof s.printed === 'string' && s.printed.startsWith('error');
  if (s.has_label && !printFailed) startAutoAdvance();
}

/* ---------- Auto-advance countdown ---------- */
let advanceTimer = null;

function startAutoAdvance() {
  const seconds = parseInt(clientSettings.countdown_seconds, 10);
  if (!Number.isFinite(seconds) || seconds <= 0) return; // 0 = disabled
  cancelAutoAdvance();
  const btn = document.getElementById('r-next');
  let remaining = seconds;
  const render = () => { if (btn) btn.textContent = `Next order (${remaining}s)`; };
  render();
  advanceTimer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearInterval(advanceTimer);
      advanceTimer = null;
      location.href = '/index.html';
      return;
    }
    render();
  }, 1000);
}

function cancelAutoAdvance() {
  if (advanceTimer !== null) {
    clearInterval(advanceTimer);
    advanceTimer = null;
  }
  const btn = document.getElementById('r-next');
  if (btn) btn.textContent = 'Next order (Enter)';
}

async function sendToZebra(url) {
  const wb = document.getElementById('r-writebacks');
  try {
    await ZebraPrint.printLabelUrl(url);
    wb.innerHTML += ' <span class="chip static ok">🖨 Sent to printer</span>';
    snackbar('Label sent to Zebra printer', 'success');
  } catch (err) {
    cancelAutoAdvance(); // print failed — let the packer reprint before advancing
    wb.innerHTML += ` <span class="chip static err">🖨 ${esc(err.message)}</span>
      <button class="btn btn-text btn-small" onclick="printAgain()">Print again</button>`;
    snackbar(err.message, 'error');
  }
}

window.printAgain = async (id) => {
  cancelAutoAdvance(); // packer is acting on this order — don't yank them away
  if (clientSettings.print_mode === 'browserprint') {
    const url = lastLabelUrl || (id ? `/api/shipments/${id}/label` : null);
    if (!url) return;
    try {
      await ZebraPrint.printLabelUrl(url);
      snackbar('Label sent to Zebra printer', 'success');
    } catch (err) {
      snackbar(err.message, 'error');
    }
    return;
  }
  try {
    await api(`/api/shipments/${id}/print`, { method: 'POST' });
    snackbar('Sent to printer', 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  }
};

window.retryWriteback = async () => {
  cancelAutoAdvance(); // packer is acting on this order — don't yank them away
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
