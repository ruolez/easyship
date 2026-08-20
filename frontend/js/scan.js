initNav('scan').then(() => {
  // The Orders page is admin-only; packers only see the manual-shipment link.
  const isAdmin = window.currentUser && window.currentUser.role === 'admin';
  document.getElementById('orders-link').style.display = isAdmin ? '' : 'none';
  document.getElementById('orders-link-alt').style.display = isAdmin ? 'none' : '';
  initAutoMode(); // needs the resolved provider from the nav
});

const sourceSelect = document.getElementById('source-select');
const numberInput = document.getElementById('order-number');
const statusEl = document.getElementById('lookup-status');

/* Auto-detect is the store select's first option rather than a separate
   toggle — one control tells the whole story. */
const AUTO_SOURCE = 'auto';
const isAutoDetect = () => sourceSelect.value === AUTO_SOURCE;

/* Active sources with their configured prefixes, for auto-detect. */
let sources = [];

async function loadSources() {
  const [dbs, stores] = await Promise.all([
    api('/api/backoffice-dbs').catch(() => []),
    api('/api/shopify-stores').catch(() => []),
  ]);
  const groups = [];
  const activeDbs = dbs.filter((d) => d.is_active);
  const activeStores = stores.filter((s) => s.is_active);
  sources = [
    ...activeDbs.map((d) => ({ kind: 'backoffice', id: d.id, name: d.name, prefix: (d.prefix || '').trim() })),
    ...activeStores.map((s) => ({ kind: 'shopify', id: s.id, name: s.name, prefix: (s.prefix || '').trim() })),
  ];
  if (activeDbs.length) {
    groups.push(`<optgroup label="BackOffice">${activeDbs
      .map((d) => `<option value="backoffice:${d.id}">${esc(d.name)}</option>`)
      .join('')}</optgroup>`);
  }
  if (activeStores.length) {
    groups.push(`<optgroup label="Shopify">${activeStores
      .map((s) => `<option value="shopify:${s.id}">${esc(s.name)}</option>`)
      .join('')}</optgroup>`);
  }
  if (!groups.length) {
    sourceSelect.innerHTML = '<option value="">No sources — see Settings</option>';
    sourceSelect.disabled = true;
    return;
  }
  sourceSelect.innerHTML = `<option value="${AUTO_SOURCE}">Auto-detect (order prefix)</option>` + groups.join('');
  const last = localStorage.getItem('easyship.lastSource');
  if (localStorage.getItem('easyship.autoDetect') === '1') {
    sourceSelect.value = AUTO_SOURCE;
  } else if (last && sourceSelect.querySelector(`option[value="${last}"]`)) {
    sourceSelect.value = last;
  }
  numberInput.focus();
}

/* Resolve a scanned number to a source via configured prefixes.
   Longest matching prefix wins. Returns the source, or {error} when none or
   several sources tie at the longest match. */
function detectSource(number) {
  const matches = sources.filter((s) => s.prefix && number.startsWith(s.prefix));
  if (!matches.length) return { error: 'none' };
  const maxLen = Math.max(...matches.map((s) => s.prefix.length));
  const best = matches.filter((s) => s.prefix.length === maxLen);
  if (best.length > 1) return { error: 'ambiguous' };
  return { source: best[0] };
}

/* Warning modal before leaving the scan page. Resolves true on Continue
   anyway, false on Cancel (stay on the scan page). */
function confirmProceed(title, message, detailsHtml = '') {
  return new Promise((resolve) => {
    const backdrop = document.getElementById('modal-backdrop');
    document.getElementById('modal').innerHTML = `
      <h3>${esc(title)}</h3>
      <p>${esc(message)}</p>
      ${detailsHtml}
      <div class="actions">
        <button class="btn btn-text" id="m-cancel">Cancel</button>
        <button class="btn btn-primary" id="m-continue">Continue anyway</button>
      </div>`;
    const close = (ok) => { backdrop.classList.remove('show'); resolve(ok); };
    backdrop.classList.add('show');
    document.getElementById('m-cancel').addEventListener('click', () => close(false));
    document.getElementById('m-continue').addEventListener('click', () => close(true));
    // Focus Cancel: scanners send Enter, which must not auto-continue.
    document.getElementById('m-cancel').focus();
  });
}

async function verificationGate(kind, id, number) {
  let res;
  try {
    const qs = kind === 'backoffice'
      ? `source=backoffice&db_id=${id}&number=${encodeURIComponent(number)}`
      : `source=shopify&number=${encodeURIComponent(number)}`;
    res = await api(`/api/shipper/check?${qs}`);
  } catch {
    res = { status: 'unavailable' };
  }
  if (res.status === 'verified' || res.status === 'not_configured') return true;
  statusEl.innerHTML = '';
  if (res.status === 'unavailable') {
    return confirmProceed('Verification unavailable',
      'Could not confirm verification — the Shipper database is unreachable.');
  }
  return confirmProceed('Order not verified',
    'This order has not been verified in Shipper.');
}

/* Warn when the order already has tracking (previously shipped). */
async function reshipGate(what, numbers, note) {
  if (!numbers.length) return true;
  statusEl.innerHTML = '';
  return confirmProceed('Order already processed', `${what} already has tracking:`,
    `<p class="mono" style="margin-top:8px">${numbers.map(esc).join('<br>')}</p>
     <p class="text-secondary" style="margin-top:8px">${esc(note)}</p>`);
}

/* ---------- Auto Mode (per station) ----------
   Toggle + presets live in localStorage: the ship page reads the preset when
   the URL carries ?auto=1. Service presets are per provider because service
   ids differ between platforms. */
const AUTO_MODE_KEY = 'easyship.autoMode';
const AUTO_PRESET_KEY = 'easyship.autoPreset';
const autoToggle = document.getElementById('auto-mode');
const autoSummary = document.getElementById('auto-summary');
let autoServices = [];
let autoHasCatalog = true;
let autoBoxes = [];
let autoEditorOpen = false;

function readAutoPreset() {
  try {
    const p = JSON.parse(localStorage.getItem(AUTO_PRESET_KEY) || '{}');
    return { box_id: p.box_id || '', byProvider: p.byProvider || {} };
  } catch {
    return { box_id: '', byProvider: {} };
  }
}

function writeAutoPreset(update) {
  const p = readAutoPreset();
  const provider = window.activeProvider ? window.activeProvider() : '';
  if ('box_id' in update) p.box_id = update.box_id;
  if (update.service && provider) p.byProvider[provider] = update.service;
  localStorage.setItem(AUTO_PRESET_KEY, JSON.stringify(p));
}

function currentServicePreset() {
  const provider = window.activeProvider ? window.activeProvider() : '';
  return readAutoPreset().byProvider[provider] || null;
}

function autoReady() {
  if (!autoToggle.checked) return false;
  const p = readAutoPreset();
  const svc = currentServicePreset();
  return Boolean(p.box_id && svc && (svc.service_id || svc.service_name));
}

async function initAutoMode() {
  autoToggle.checked = localStorage.getItem(AUTO_MODE_KEY) === '1';
  autoToggle.addEventListener('change', async () => {
    localStorage.setItem(AUTO_MODE_KEY, autoToggle.checked ? '1' : '0');
    // Opening straight into the editor only when something still needs choosing.
    autoEditorOpen = autoToggle.checked && !autoReady();
    await renderAutoPreset();
    numberInput.focus();
  });
  autoSummary.addEventListener('click', () => {
    autoEditorOpen = !autoEditorOpen;
    renderAutoPreset();
  });
  window.addEventListener('easyship:provider', () => renderAutoPreset());
  document.getElementById('auto-carrier').addEventListener('change', () => {
    fillAutoServices();
    saveAutoService();
  });
  document.getElementById('auto-service').addEventListener('change', saveAutoService);
  document.getElementById('auto-service-name').addEventListener('input', saveAutoService);
  document.getElementById('auto-box').addEventListener('change', () => {
    writeAutoPreset({ box_id: document.getElementById('auto-box').value });
    renderAutoStatus();
  });
  await renderAutoPreset();
}

async function renderAutoPreset() {
  const wrap = document.getElementById('auto-preset');
  wrap.style.display = autoToggle.checked && autoEditorOpen ? '' : 'none';
  autoSummary.setAttribute('aria-expanded', String(autoToggle.checked && autoEditorOpen));
  if (!autoToggle.checked) { renderAutoStatus(); return; }
  const provider = window.activeProvider ? window.activeProvider() : '';
  const status = document.getElementById('auto-status');
  status.textContent = 'Loading services…';
  const [boxes, svc] = await Promise.all([
    api('/api/boxes').catch(() => []),
    provider ? api(`/api/providers/${encodeURIComponent(provider)}/services/available`).catch(() => null) : null,
  ]);
  autoBoxes = boxes;
  autoServices = (svc && svc.services) || [];
  autoHasCatalog = svc ? svc.has_catalog : false;

  const preset = readAutoPreset();
  const boxSel = document.getElementById('auto-box');
  boxSel.innerHTML = '<option value="">Choose a box…</option>'
    + autoBoxes.map((b) => `<option value="${b.id}">${b.length}×${b.width}×${b.height} in</option>`).join('');
  if (autoBoxes.some((b) => String(b.id) === String(preset.box_id))) boxSel.value = String(preset.box_id);

  const catalogFields = [document.getElementById('auto-carrier-field'), document.getElementById('auto-service-field')];
  const nameField = document.getElementById('auto-service-name-field');
  catalogFields.forEach((el) => { el.style.display = autoHasCatalog ? '' : 'none'; });
  nameField.style.display = autoHasCatalog ? 'none' : '';
  const current = currentServicePreset() || {};
  if (autoHasCatalog) {
    const carriers = [...new Set(autoServices.map((s) => s.umbrella_name || 'Other'))].sort();
    const carrierSel = document.getElementById('auto-carrier');
    carrierSel.innerHTML = '<option value="">Choose a carrier…</option>'
      + carriers.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    if (carriers.includes(current.carrier)) carrierSel.value = current.carrier;
    fillAutoServices();
    const serviceSel = document.getElementById('auto-service');
    if (autoServices.some((s) => String(s.id) === String(current.service_id))) serviceSel.value = String(current.service_id);
  } else {
    document.getElementById('auto-service-name').value = current.service_name || '';
  }
  renderAutoStatus();
}

function fillAutoServices() {
  const carrier = document.getElementById('auto-carrier').value;
  const serviceSel = document.getElementById('auto-service');
  const list = autoServices.filter((s) => !carrier || (s.umbrella_name || 'Other') === carrier);
  serviceSel.innerHTML = '<option value="">Choose a service…</option>'
    + list.map((s) => `<option value="${esc(String(s.id))}">${esc(s.name)}</option>`).join('');
}

function saveAutoService() {
  if (autoHasCatalog) {
    const id = document.getElementById('auto-service').value;
    const svc = autoServices.find((s) => String(s.id) === id);
    writeAutoPreset({ service: {
      carrier: document.getElementById('auto-carrier').value,
      service_id: svc ? String(svc.id) : '',
      service_name: svc ? svc.name : '',
    } });
  } else {
    writeAutoPreset({ service: { carrier: '', service_id: '', service_name: document.getElementById('auto-service-name').value.trim() } });
  }
  renderAutoStatus();
}

function renderAutoStatus() {
  const status = document.getElementById('auto-status');
  const svc = currentServicePreset();
  const box = autoBoxes.find((b) => String(b.id) === String(readAutoPreset().box_id));
  if (!autoToggle.checked) {
    autoSummary.style.display = 'none';
    return;
  }
  autoSummary.style.display = '';
  if (!window.activeProvider || !window.activeProvider()) {
    autoSummary.className = 'station-summary err';
    autoSummary.textContent = 'Select a provider';
    status.innerHTML = '<span class="chip static err">Select a shipping provider in the sidebar</span>';
  } else if (autoReady()) {
    autoSummary.className = 'station-summary ok';
    autoSummary.textContent = `${svc.service_name || 'service'} · ${box ? `${box.length}×${box.width}×${box.height} in` : 'box'}`;
    status.innerHTML = '<span class="chip static ok">✓ Ready</span>'
      + '<span class="auto-hint">Scanned orders are weighed, rated, bought and printed — Esc on the Ship page switches back to manual.</span>';
  } else {
    autoSummary.className = 'station-summary warn';
    autoSummary.textContent = 'Set service & box';
    status.innerHTML = '<span class="chip static warn">Choose a service and a box</span>'
      + '<span class="auto-hint">Auto Mode stays off until both are set.</span>';
  }
}

function stayOnScan() {
  statusEl.innerHTML = '';
  numberInput.select();
  numberInput.focus();
}

async function lookup() {
  const number = numberInput.value.trim();
  if (!number) { numberInput.focus(); return; }

  let kind, id;
  if (isAutoDetect()) {
    const res = detectSource(number);
    if (res.error === 'none') {
      statusEl.innerHTML = '<span class="chip static err">✕ No store matches this order number</span>';
      numberInput.select();
      numberInput.focus();
      return;
    }
    if (res.error === 'ambiguous') {
      statusEl.innerHTML = '<span class="chip static err">✕ Multiple stores match this prefix</span>';
      numberInput.select();
      numberInput.focus();
      return;
    }
    ({ kind, id } = res.source);
    statusEl.innerHTML = `<span class="chip static ok">Detected: ${esc(res.source.name)}</span> <span class="spinner"></span>`;
  } else {
    const source = sourceSelect.value;
    if (!source) { snackbar('Configure a BackOffice database or Shopify store in Settings first', 'error'); return; }
    [kind, id] = source.split(':');
    statusEl.innerHTML = '<span class="spinner"></span>';
  }
  try {
    if (kind === 'backoffice') {
      const inv = await api(`/api/backoffice/${id}/lookup?number=${encodeURIComponent(number)}`);
      const tracking = (inv.tracking_no || '').trim();
      if (!(await reshipGate(`Invoice ${inv.invoice_number}`, tracking ? [tracking] : [],
        'New tracking numbers are added to the invoice Notes in BackOffice — the existing tracking number is kept.'))) {
        stayOnScan();
        return;
      }
      if (!(await verificationGate('backoffice', id, number))) {
        stayOnScan();
        return;
      }
      location.href = `/ship.html?source=backoffice&db_id=${id}&invoice_id=${inv.invoice_id}&reship_ack=1${autoReady() ? '&auto=1' : ''}`;
    } else {
      const order = await api(`/api/shopify/lookup?store_id=${id}&number=${encodeURIComponent(number)}`);
      if (!(await reshipGate(`Shopify order ${order.name}`, order.existing_tracking || [],
        "New tracking numbers are added to the order's existing fulfillment — it stays fulfilled."))) {
        stayOnScan();
        return;
      }
      if (!(await verificationGate('shopify', id, order.name || number))) {
        stayOnScan();
        return;
      }
      location.href = `/ship.html?source=shopify&store_id=${id}&order_id=${encodeURIComponent(order.id)}&reship_ack=1${autoReady() ? '&auto=1' : ''}`;
    }
  } catch (err) {
    statusEl.innerHTML = `<span class="chip static err">✕ ${esc(err.message)}</span>`;
    numberInput.select();
    numberInput.focus();
  }
}

numberInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); lookup(); }
});
document.getElementById('lookup-btn').addEventListener('click', lookup);
sourceSelect.addEventListener('change', () => {
  localStorage.setItem('easyship.autoDetect', isAutoDetect() ? '1' : '0');
  if (!isAutoDetect() && sourceSelect.value) localStorage.setItem('easyship.lastSource', sourceSelect.value);
  statusEl.innerHTML = '';
  numberInput.focus();
});

loadSources();
