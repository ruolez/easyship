initNav('scan');

const sourceSelect = document.getElementById('source-select');
const numberInput = document.getElementById('order-number');
const statusEl = document.getElementById('lookup-status');
const autoDetectToggle = document.getElementById('auto-detect');

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
    sourceSelect.innerHTML = '<option value="">No sources configured — see Settings</option>';
    return;
  }
  sourceSelect.innerHTML = groups.join('');
  const last = localStorage.getItem('easyship.lastSource');
  if (last && sourceSelect.querySelector(`option[value="${last}"]`)) {
    sourceSelect.value = last;
  }
  applyAutoDetect();
  numberInput.focus();
}

/* Reflect the auto-detect toggle: dim the manual dropdown when detection is on. */
function applyAutoDetect() {
  sourceSelect.disabled = autoDetectToggle.checked;
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

function stayOnScan() {
  statusEl.innerHTML = '';
  numberInput.select();
  numberInput.focus();
}

async function lookup() {
  const number = numberInput.value.trim();
  if (!number) { numberInput.focus(); return; }

  let kind, id;
  if (autoDetectToggle.checked) {
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
    sourceSelect.value = `${kind}:${id}`;
    statusEl.innerHTML = `<span class="chip static ok">Detected: ${esc(res.source.name)}</span> <span class="spinner"></span>`;
  } else {
    const source = sourceSelect.value;
    if (!source) { snackbar('Configure a BackOffice database or Shopify store in Settings first', 'error'); return; }
    localStorage.setItem('easyship.lastSource', source);
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
      location.href = `/ship.html?source=backoffice&db_id=${id}&invoice_id=${inv.invoice_id}&reship_ack=1`;
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
      location.href = `/ship.html?source=shopify&store_id=${id}&order_id=${encodeURIComponent(order.id)}&reship_ack=1`;
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
sourceSelect.addEventListener('change', () => numberInput.focus());

autoDetectToggle.checked = localStorage.getItem('easyship.autoDetect') === '1';
autoDetectToggle.addEventListener('change', () => {
  localStorage.setItem('easyship.autoDetect', autoDetectToggle.checked ? '1' : '0');
  applyAutoDetect();
  statusEl.innerHTML = '';
  numberInput.focus();
});

loadSources();
