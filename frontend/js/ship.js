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
let providerLabels = {};
let preferredService = ''; // courier service a tag rule asked for
let preferredServiceId = ''; // Auto Mode preset service id (when no tag rule names one)

const navReady = initNav('scan');
init();

async function init() {
  const [settings, boxes, providers] = await Promise.all([
    api('/api/settings/client').catch(() => clientSettings),
    api('/api/boxes').catch(() => []),
    api('/api/providers/enabled').catch(() => []),
  ]);
  clientSettings = settings;
  savedBoxes = boxes;
  providerLabels = Object.fromEntries(providers.map((p) => [p.name, p.label]));
  addParcelRow();
  const loaded = await prefill();
  applyPlaceholderEmail();
  focusNextField();
  if (params.get('auto') === '1') Auto.start(loaded);
}

function applyPlaceholderEmail() {
  const el = document.getElementById('d-email');
  if (!el.value.trim() && clientSettings.placeholder_email) {
    el.value = clientSettings.placeholder_email;
  }
}

const REQUIRED_DESTINATION_IDS = ['d-address1', 'd-city', 'd-state', 'd-zip'];

function firstMissingDestinationField() {
  return REQUIRED_DESTINATION_IDS.map((id) => document.getElementById(id))
    .find((el) => !el.value.trim()) || null;
}

function focusNextField() {
  // Land the cursor where the user must type next to get a label out.
  const missing = firstMissingDestinationField();
  if (missing) { missing.focus(); return; }
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
      showOrderSummary(`Shopify order <strong>${esc(o.name)}</strong> — ${esc(o.customer || '')}`,
        orderExtras(o));
      applyTagRules(o.tag_rules);
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
        `BackOffice invoice <strong>${esc(inv.invoice_number)}</strong> — ${esc(inv.business_name || '')}`
      );
      seedParcels(inv.no_boxes, inv.total_weight);
      if ((inv.tracking_no || '').trim() && !params.get('reship_ack')) {
        await confirmReship(`Invoice ${inv.invoice_number}`, [inv.tracking_no.trim()],
          'New tracking numbers are added to the invoice Notes in BackOffice — the existing tracking number is kept.');
      }
    }
  } catch (err) {
    snackbar(`Could not load order: ${err.message}`, 'error');
    return false;
  }
  return true;
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

function showOrderSummary(html, extrasHtml = '') {
  const el = document.getElementById('order-summary');
  el.style.display = '';
  el.innerHTML = `<h2>${html}</h2>${extrasHtml}`;
}

/* Tags, the customer/staff note, and what the tag rules decided — shown right
   under the order heading so the packer sees them before rating. */
function orderExtras(o) {
  const parts = [];
  if ((o.tags || []).length) {
    parts.push(`<div class="order-tags">${o.tags.map((t) => `<span class="tag-chip">${esc(t)}</span>`).join('')}</div>`);
  }
  if (o.note) {
    parts.push(`<div class="order-note"><strong>Note:</strong> ${esc(o.note)}</div>`);
  }
  const rules = o.tag_rules || {};
  if ((rules.matched || []).length) {
    const what = [];
    if (rules.signature === 'adult') what.push('Adult signature (21+) required');
    else if (rules.signature === 'signature') what.push('Signature required');
    if (rules.service) what.push(`Preferred service: ${esc(rules.service)}`);
    const tags = rules.matched.map((r) => `<span class="tag-chip">${esc(r.tag)}</span>`).join('');
    parts.push(`<div class="rule-banner">${tags} <span>${what.join(' · ') || 'Tag rule matched'}</span></div>`);
  }
  return parts.length ? `<div class="order-extras">${parts.join('')}</div>` : '';
}

function applyTagRules(rules) {
  if (!rules) return;
  preferredService = rules.service || '';
  const sel = document.getElementById('signature');
  if (sel && rules.signature) sel.value = rules.signature;
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
      <label>L × W × H (in)</label>
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
      syncRemoveButtons();
    }
  });
  div.querySelector('.p-weight').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (Auto.weightEntered()) return; // Auto Mode continues its own chain
      getRates();
    } else if (e.key !== 'Tab') {
      Auto.weightTyped();
    }
  });
  document.getElementById('parcel-list').appendChild(div);
  syncRemoveButtons();
}

// The remove button is a no-op with a single box, so hide it then — it also
// frees the horizontal room that otherwise crowds the last dimension field.
function syncRemoveButtons() {
  const rows = document.querySelectorAll('.parcel-row');
  rows.forEach((row) => {
    row.querySelector('.remove-parcel').style.display = rows.length > 1 ? '' : 'none';
  });
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
document.getElementById('get-rates').addEventListener('click', () => {
  if (Auto.weightEntered()) return; // Auto Mode continues its own chain
  getRates();
});

/* Rate the shipment as entered. Resolves true when rates rendered, false on
   any error (already shown to the user). Shared by the button and Auto Mode. */
async function getRates() {
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
        provider: (window.activeProvider && window.activeProvider()) || '',
        destination: collectDestination(),
        parcels: collectParcels(),
        items: orderItems,
        options: { signature: document.getElementById('signature').value },
        preferred_service: preferredService,
        preferred_service_id: preferredServiceId,
      },
    });
    localShipmentId = res.shipment_id;
    groupId = res.group_id;
    boxCount = res.box_count || 1;
    rates = res.rates;
    renderRates();
    renderRateWarnings(res.warnings || []);
    document.getElementById('panel-rates').style.display = '';
    document.getElementById('panel-rates').scrollIntoView({ behavior: 'smooth' });
    return true;
  } catch (err) {
    snackbar(err.message, 'error');
    setStep(2);
    return false;
  } finally {
    btn.disabled = false;
    spinner.style.display = 'none';
  }
}

function providerLabel(name) {
  if (!name) return '';
  return providerLabels[name] || name.charAt(0).toUpperCase() + name.slice(1);
}

function renderRates() {
  const list = document.getElementById('rate-list');
  selectedRate = null;
  document.getElementById('buy-label').disabled = true;
  // Only tag rates with their provider when more than one is quoting.
  const multiProvider = new Set(rates.map((r) => r.provider)).size > 1;
  list.innerHTML = rates.map((r, i) => `
    <div class="rate-row${r.preferred ? ' preferred' : ''}" data-idx="${i}" tabindex="0" role="radio" aria-checked="false">
      <span class="rate-radio"></span>
      <span class="rate-info">
        <span class="rate-courier">${esc(r.courier_name)}${r.preferred ? (r.preferred_by === 'preset' ? ' <span class="chip static warn rate-best" title="Your Auto Mode preset service">Auto preset</span>' : ' <span class="chip static warn rate-best" title="Chosen by an order tag rule">Tag rule</span>') : ''}${r.value_for_money_rank === 1 ? ' <span class="chip static ok rate-best">Best value</span>' : ''}${multiProvider && r.provider ? ` <span class="chip static rate-provider">${esc(providerLabel(r.provider))}</span>` : ''}</span>
        <span class="rate-days">${r.min_delivery_time ?? '?'}–${r.max_delivery_time ?? '?'} business days</span>
      </span>
      <span class="rate-price">${money(r.total_charge)} <small>${esc(r.currency || 'USD')}</small></span>
    </div>`).join('');
  const select = (row) => {
    list.querySelectorAll('.rate-row').forEach((c) => {
      c.classList.remove('selected');
      c.setAttribute('aria-checked', 'false');
    });
    row.classList.add('selected');
    row.setAttribute('aria-checked', 'true');
    selectedRate = rates[Number(row.dataset.idx)];
    const buyBtn = document.getElementById('buy-label');
    buyBtn.disabled = false;
    buyBtn.focus();
  };
  list.querySelectorAll('.rate-row').forEach((row) => {
    row.addEventListener('click', () => select(row));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(row); }
    });
  });
  // A tag rule's preferred service is pre-selected (the packer can still pick
  // another rate); otherwise land on the first rate for keyboard selection.
  const preferred = list.querySelector('.rate-row.preferred');
  if (preferred) select(preferred);
  else {
    const first = list.querySelector('.rate-row');
    if (first) first.focus();
  }
}

function renderRateWarnings(warnings) {
  let el = document.getElementById('rate-warnings');
  if (!el) {
    el = document.createElement('div');
    el.id = 'rate-warnings';
    el.className = 'rate-warnings';
    document.getElementById('rate-list').before(el);
  }
  el.innerHTML = warnings.map((w) => `<div class="rule-banner warn">⚠ ${esc(w)}</div>`).join('');
  el.style.display = warnings.length ? '' : 'none';
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

document.getElementById('buy-label').addEventListener('click', () => { buyLabel(); });

/* Buy the selected rate and follow the purchase to its end. Resolves
   {state: 'done'|'retry'|'error'|'failed', message} — 'failed' when the buy
   request itself was rejected. Shared by the button and Auto Mode. */
async function buyLabel() {
  if (!selectedRate || !groupId) return { state: 'failed', message: 'No rate selected' };
  const btn = document.getElementById('buy-label');
  const spinner = document.getElementById('buy-spinner');
  btn.disabled = true;
  spinner.style.display = '';
  try {
    await api(`/api/shipments/group/${groupId}/buy`, {
      method: 'POST',
      body: {
        provider: selectedRate.provider,
        courier_service_id: selectedRate.courier_service_id,
        rate: selectedRate,
      },
    });
  } catch (err) {
    snackbar(err.message, 'error');
    btn.disabled = false;
    spinner.style.display = 'none';
    return { state: 'failed', message: err.message };
  }
  return new Promise((resolve) => {
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
        resolve({ state: 'done', message: '' });
      } else if (progress.state === 'retry' || progress.state === 'error') {
        clearInterval(buyPollTimer);
        spinner.style.display = 'none';
        const message = progress.message || 'Label purchase did not complete';
        snackbar(message, 'error');
        if (progress.state === 'retry') btn.disabled = false;
        resolve({ state: progress.state, message });
      }
    }, 1500);
  });
}

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
  // Keep the address + rate columns visible; the result fills the wide middle
  // column under the boxes, where there's the most space.
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
  Auto.onResult(s, printFailed);
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

/* ============================================================ Auto Mode
   Scan page sets ?auto=1 when the station has Auto Mode on with a preset
   (localStorage easyship.autoPreset). From there: preset box → wait for a
   stable scale weight → rate → buy the preset/tag-rule service → print.
   Every step that isn't certain drops back to the manual form. */
const Auto = (() => {
  const SETTLE_MS = 800; // a stable reading must hold this long before we trust it
  let stage = 'idle'; // idle|weigh|rating|choose|buying|done|cancelled|failed
  let unsubscribe = null;
  let settleTimer = null;
  let lastLb = null;
  let preset = null;
  let printOutcome = null; // set by showResult, which can run before the buy promise settles

  const banner = () => document.getElementById('auto-banner');
  const weightField = () => document.querySelector('.p-weight');

  function setStage(next, title, detail = '', tone = '') {
    stage = next;
    const el = banner();
    el.style.display = '';
    el.className = `card auto-banner${tone ? ' ' + tone : ''}`;
    document.getElementById('auto-stage').textContent = title;
    document.getElementById('auto-detail').textContent = detail;
    document.getElementById('auto-cancel').style.display =
      ['weigh', 'rating', 'choose'].includes(next) ? '' : 'none';
  }

  function setDetail(detail) {
    document.getElementById('auto-detail').textContent = detail;
  }

  function stopScale() {
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
    clearTimeout(settleTimer);
    settleTimer = null;
    lastLb = null;
  }

  function fail(message) {
    stopScale();
    setStage('failed', 'Auto Mode stopped', message, 'err');
    snackbar(message, 'error');
    focusNextField();
  }

  function loadPreset(provider) {
    try {
      const all = JSON.parse(localStorage.getItem('easyship.autoPreset') || '{}');
      const svc = (all.byProvider || {})[provider];
      if (!all.box_id || !svc || !(svc.service_id || svc.service_name)) return null;
      return { box_id: Number(all.box_id), ...svc };
    } catch {
      return null;
    }
  }

  async function start(orderLoaded) {
    await navReady; // the provider must be resolved — never auto-rate against every provider
    const provider = (window.activeProvider && window.activeProvider()) || '';
    preset = loadPreset(provider);
    if (!orderLoaded) return fail('Order could not be loaded — finish manually');
    if (!provider) return fail('No shipping provider selected — finish manually');
    if (!preset) return fail('No Auto Mode preset for this provider on this station — set it on the Scan page');
    if (firstMissingDestinationField()) return fail('Destination is incomplete — finish manually');
    const rows = document.querySelectorAll('.parcel-row');
    if (rows.length !== 1) return fail('Multi-box order — ship manually');
    const box = savedBoxes.find((b) => b.id === preset.box_id);
    if (!box) return fail('Auto Mode preset box no longer exists — finish manually');

    const boxSelect = rows[0].querySelector('.p-box');
    boxSelect.value = String(box.id);
    boxSelect.dispatchEvent(new Event('change'));
    if (preferredService) {
      // A tag rule named a service — it outranks the station preset.
      preferredServiceId = '';
    } else if (preset.service_id) {
      preferredServiceId = String(preset.service_id);
    } else {
      preferredService = preset.service_name;
    }
    weigh();
  }

  function weigh() {
    const field = weightField();
    if (field) { field.focus(); if (field.value) field.select(); }
    const connected = window.Scale && window.Scale.connected;
    setStage('weigh', 'Auto Mode — waiting for weight',
      connected ? 'Place the package on the scale, or type the weight and press Enter'
        : 'Scale not connected — connect it, or type the weight and press Enter', 'warn');
    if (window.Scale && window.Scale.subscribe) unsubscribe = window.Scale.subscribe(onReading);
  }

  function onReading({ status, lb }) {
    if (stage !== 'weigh') return;
    if (status === 4 && lb > 0) {
      const v = lb.toFixed(1);
      if (v !== lastLb) {
        lastLb = v;
        clearTimeout(settleTimer);
        settleTimer = setTimeout(() => {
          const field = weightField();
          if (!field) return;
          field.value = lastLb;
          field.dispatchEvent(new Event('input', { bubbles: true }));
          proceedFromWeight();
        }, SETTLE_MS);
      }
      return;
    }
    clearTimeout(settleTimer);
    settleTimer = null;
    lastLb = null;
    if (status === 3) setDetail('Weighing…');
    else if (status === 5) setDetail('Scale below zero — re-zero it');
    else if (status === -1) setDetail('Scale unplugged — reconnect it, or type the weight and press Enter');
    else if (status >= 6) setDetail('Scale fault — type the weight and press Enter');
    else setDetail('Place the package on the scale, or type the weight and press Enter');
  }

  function weightTyped() {
    if (stage !== 'weigh' || !unsubscribe) return;
    stopScale();
    setDetail('Manual weight — press Enter to continue');
  }

  function weightEntered() {
    if (stage !== 'weigh') return false;
    proceedFromWeight();
    return true;
  }

  async function proceedFromWeight() {
    if (stage !== 'weigh') return;
    const field = weightField();
    const lb = parseFloat(field && field.value);
    if (!(lb > 0)) { setDetail('Weight must be greater than 0'); return; }
    stopScale();
    setStage('rating', 'Auto Mode — getting rates', `Using ${lb.toFixed(1)} lb · ${preset.service_name || 'preset service'}`);
    const ok = await getRates();
    if (stage !== 'rating') return; // cancelled while the request was in flight
    if (!ok) return fail('Could not get rates — finish manually');
    if (!selectedRate || !selectedRate.preferred) {
      setStage('choose', 'Auto Mode paused', 'Preset service not offered for this shipment — choose a rate and print', 'warn');
      return;
    }
    setStage('buying', 'Auto Mode — buying label',
      `${selectedRate.courier_name} · ${money(selectedRate.total_charge)}`);
    const result = await buyLabel();
    if (result.state === 'done') {
      setStage('done', 'Auto Mode — label bought', 'Printing…', 'ok');
      applyPrintOutcome();
    } else {
      fail(result.message || 'Label purchase did not complete');
    }
  }

  function onResult(s, printFailed) {
    printOutcome = { printFailed };
    if (stage === 'done') applyPrintOutcome();
  }

  function applyPrintOutcome() {
    if (!printOutcome) return;
    if (printOutcome.printFailed) setStage('done', 'Auto Mode — label bought', 'Print failed — use Print again', 'err');
    else setDetail(clientSettings.print_mode === 'browser' ? 'Confirm the print dialog' : 'Printed — returning to Scan');
  }

  function cancel() {
    if (!['weigh', 'rating', 'choose'].includes(stage)) return;
    stopScale();
    stage = 'cancelled';
    banner().style.display = 'none';
    snackbar('Auto Mode off for this order — continue manually');
    focusNextField();
  }

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') cancel(); });
  const cancelBtn = document.getElementById('auto-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', cancel);

  return { start, weightEntered, weightTyped, onResult, cancel, get stage() { return stage; } };
})();
