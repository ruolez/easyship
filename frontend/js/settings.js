// Non-provider settings. Provider fields (tokens, mode, enabled, custom
// fields) are appended after the provider cards render, so new platforms need
// no edits here.
const BASE_SETTING_IDS = [
  'origin_company', 'origin_contact', 'origin_address1', 'origin_address2',
  'origin_city', 'origin_state', 'origin_zip', 'origin_phone', 'origin_email',
  'placeholder_email', 'print_mode', 'printer_host', 'printer_port',
  'label_timeout_seconds', 'countdown_seconds',
  'shipper_host', 'shipper_port', 'shipper_db', 'shipper_user', 'shipper_password',
];
let SETTING_IDS = [...BASE_SETTING_IDS];

initNav('settings').then(async () => {
  const isAdmin = window.currentUser && window.currentUser.role === 'admin';
  initTabs(isAdmin);
  if (!isAdmin) return;
  await renderProviders();
  await loadSettings();
  updatePrintModeUI();
  watchDirty();
  await loadStores();
  await loadDbs();
  await loadBoxes();
  await loadUsers();
});

/* ---------- Tabs ---------- */
const ADMIN_TABS = ['providers', 'shipping', 'printing', 'boxes', 'integrations', 'users'];

function initTabs(isAdmin) {
  const buttons = [...document.querySelectorAll('#settings-tabs .tab')];
  buttons.forEach((b) => {
    if (!isAdmin && ADMIN_TABS.includes(b.dataset.tab)) b.style.display = 'none';
    b.addEventListener('click', () => showTab(b.dataset.tab));
  });
  const allowed = buttons.map((b) => b.dataset.tab)
    .filter((t) => isAdmin || !ADMIN_TABS.includes(t));
  const fromHash = location.hash.replace('#', '');
  showTab(allowed.includes(fromHash) ? fromHash : allowed[0]);
}

function showTab(tab) {
  document.querySelectorAll('#settings-tabs .tab')
    .forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel')
    .forEach((p) => p.classList.toggle('active', p.dataset.panel === tab));
  if (location.hash !== `#${tab}`) history.replaceState(null, '', `#${tab}`);
}

/* ---------- Unsaved-changes save bar ---------- */
let dirty = false;

function setDirty(v) {
  dirty = v;
  document.getElementById('savebar').classList.toggle('show', v);
}

function watchDirty() {
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('input', () => setDirty(true));
    el.addEventListener('change', () => setDirty(true));
  });
}

window.addEventListener('beforeunload', (e) => {
  if (dirty) e.preventDefault();
});

document.getElementById('discard-settings').addEventListener('click', async () => {
  await loadSettings();
  updatePrintModeUI();
  setDirty(false);
});

/* ---------- Print mode: show only what the selected mode needs ---------- */
const PRINT_HINTS = {
  browser: 'After purchase the label opens in this browser’s print dialog — works with any printer, one click per label.',
  network: 'The server sends labels straight to a thermal printer on the network — no dialog. Set your Easyship dashboard → Printing Options to ZPL, 4x6.',
  browserprint: 'Labels print silently through the Zebra Browser Print app on each packing station. The app must be running with the Zebra set as its default printer; the first print asks to accept this website inside the app. Requires Easyship dashboard → Printing Options set to ZPL, 4x6. Test from the packing station, not the server.',
};

function updatePrintModeUI() {
  const mode = document.getElementById('print_mode').value || 'browser';
  document.getElementById('print-network').style.display = mode === 'network' ? '' : 'none';
  document.getElementById('print-browserprint').style.display = mode === 'browserprint' ? '' : 'none';
  document.getElementById('print-hint').textContent = PRINT_HINTS[mode] || '';
}

document.getElementById('print_mode').addEventListener('change', updatePrintModeUI);

/* ---------- Box sizes ---------- */
async function loadBoxes() {
  const boxes = await api('/api/boxes');
  const tbody = document.getElementById('boxes-body');
  tbody.innerHTML = boxes.length
    ? boxes.map((b) => `<tr>
        <td><strong>${esc(b.name)}</strong></td>
        <td class="mono">${b.length} × ${b.width} × ${b.height}</td>
        <td><button class="btn btn-danger btn-small" onclick="deleteBox(${b.id})">Delete</button></td>
      </tr>`).join('')
    : '<tr><td colspan="3" class="text-secondary">No box sizes yet.</td></tr>';
}

document.getElementById('add-box').addEventListener('click', async () => {
  try {
    await api('/api/boxes', {
      method: 'POST',
      body: {
        name: document.getElementById('box-name').value,
        length: document.getElementById('box-length').value,
        width: document.getElementById('box-width').value,
        height: document.getElementById('box-height').value,
      },
    });
    ['box-name', 'box-length', 'box-width', 'box-height'].forEach((id) => {
      document.getElementById(id).value = '';
    });
    snackbar('Box added', 'success');
    loadBoxes();
  } catch (err) {
    snackbar(err.message, 'error');
  }
});

window.deleteBox = async (id) => {
  await api(`/api/boxes/${id}`, { method: 'DELETE' });
  loadBoxes();
};

/* ---------- Shipping providers (rendered from descriptors) ---------- */
async function renderProviders() {
  const container = document.getElementById('providers-container');
  let list;
  try {
    list = await api('/api/providers');
  } catch (err) {
    container.innerHTML = `<div class="card"><p class="text-secondary">Could not load providers: ${esc(err.message)}</p></div>`;
    return;
  }
  container.innerHTML = list.map(providerCardHtml).join('');
  // Register every provider field as a persistable setting.
  list.forEach((p) => {
    SETTING_IDS.push(p.enabled_key);
    if (p.modes && p.modes.length) SETTING_IDS.push(p.mode_key);
    p.fields.forEach((f) => SETTING_IDS.push(f.key));
  });
  for (const p of list) {
    wireProviderCard(p);
    for (const f of p.fields) {
      if (f.type === 'select' && f.options_endpoint) await loadFieldOptions(f);
    }
    if (p.supports && p.supports.service_exclusions) loadServices(p);
  }
  wireProviderPicker(list);
}

/* Show one provider's config card at a time, chosen from the picker dropdown. */
function wireProviderPicker(list) {
  const picker = document.getElementById('provider-config-select');
  if (!picker || !list.length) return;
  picker.innerHTML = list.map((p) => `<option value="${esc(p.name)}">${esc(p.label)}</option>`).join('');
  const showOnly = (name) => {
    list.forEach((p) => {
      document.querySelectorAll(`[data-provider="${p.name}"], [data-services="${p.name}"]`)
        .forEach((el) => { el.style.display = p.name === name ? '' : 'none'; });
    });
  };
  picker.value = list[0].name;
  showOnly(list[0].name);
  picker.addEventListener('change', () => showOnly(picker.value));
}

function fieldHtml(f) {
  const hint = f.hint ? `<span class="hint">${esc(f.hint)}</span>` : '';
  if (f.type === 'secret') {
    return `<div class="field"><label>${esc(f.label)}</label>
      <input type="password" id="${esc(f.key)}" autocomplete="off">${hint}</div>`;
  }
  if (f.type === 'select') {
    // Inline options render immediately; otherwise loadFieldOptions fills from options_endpoint.
    const opts = (f.options || [])
      .map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
    return `<div class="field fixed" style="min-width:260px"><label>${esc(f.label)}</label>
      <select id="${esc(f.key)}">${opts}</select>${hint}</div>`;
  }
  return `<div class="field"><label>${esc(f.label)}</label><input id="${esc(f.key)}">${hint}</div>`;
}

function providerCardHtml(p) {
  const modeField = p.modes && p.modes.length ? `
    <div class="field fixed" style="min-width:220px">
      <label>Mode</label>
      <select id="${esc(p.mode_key)}">
        ${p.modes.map((m) => `<option value="${esc(m.value)}">${esc(m.label)}</option>`).join('')}
      </select>
    </div>` : '';
  const testBtn = p.test_endpoint ? `
    <div class="fixed"><button class="btn btn-outlined" data-test="${esc(p.name)}">Test connection</button></div>
    <div class="fixed" id="test-result-${esc(p.name)}"></div>` : '';
  const services = p.supports && p.supports.service_exclusions ? `
    <div class="card" data-services="${esc(p.name)}">
      <h2>${esc(p.label)} shipping services
        <button class="btn btn-outlined btn-small" data-reload-services="${esc(p.name)}" style="margin-left:auto">Reload</button>
      </h2>
      <p class="hint mb-16">Checked services are hidden from the rate list on the shipping page. Fetched live from your ${esc(p.label)} account (current mode).</p>
      <div id="services-list-${esc(p.name)}"><p class="text-secondary">Loading services…</p></div>
      <div class="row mt-16">
        <div class="fixed"><button class="btn btn-primary" data-save-services="${esc(p.name)}">Save exclusions</button></div>
        <div class="fixed" id="services-status-${esc(p.name)}"></div>
      </div>
    </div>` : '';
  return `
    <div class="card" data-provider="${esc(p.name)}">
      <h2>${esc(p.label)} API
        <label class="svc-selectall" style="margin-left:auto"><input type="checkbox" id="${esc(p.enabled_key)}"> Enabled</label>
      </h2>
      <div class="row mb-16">
        ${modeField}${testBtn}
      </div>
      <div class="row">${p.fields.map(fieldHtml).join('')}</div>
    </div>
    ${services}`;
}

function wireProviderCard(p) {
  if (p.test_endpoint) {
    const btn = document.querySelector(`[data-test="${p.name}"]`);
    btn.addEventListener('click', () => {
      const modeEl = document.getElementById(p.mode_key);
      const mode = modeEl ? (modeEl.value || (p.modes[0] && p.modes[0].value)) : undefined;
      const tokenField = p.fields.find((f) => f.type === 'secret' && f.mode === mode);
      const token = tokenField ? document.getElementById(tokenField.key).value : undefined;
      testResult(`test-result-${p.name}`, api(p.test_endpoint, { method: 'POST', body: { mode, token } }));
    });
  }
  if (p.supports && p.supports.service_exclusions) {
    document.querySelector(`[data-reload-services="${p.name}"]`)
      .addEventListener('click', () => loadServices(p));
    document.querySelector(`[data-save-services="${p.name}"]`)
      .addEventListener('click', () => saveServices(p));
    document.getElementById(`services-list-${p.name}`)
      .addEventListener('change', onServiceToggle);
  }
}

async function loadFieldOptions(f) {
  const select = document.getElementById(f.key);
  try {
    const options = await api(f.options_endpoint);
    select.innerHTML = options
      .map((o) => `<option value="${esc(o.slug ?? o.value)}">${esc(o.name ?? o.label)}</option>`)
      .join('');
  } catch {
    select.innerHTML = '';
  }
}

/* ---------- Shipping services (rate exclusions), per provider ---------- */
async function loadServices(p) {
  const list = document.getElementById(`services-list-${p.name}`);
  list.innerHTML = '<p class="text-secondary">Loading services…</p>';
  try {
    const res = await api(p.services_endpoint);
    renderServices(list, res.services || [], new Set(res.excluded || []));
  } catch (err) {
    list.innerHTML = `<p class="text-secondary">Could not load services: ${esc(err.message)}</p>`;
  }
}

function renderServices(list, services, excluded) {
  if (!services.length) {
    list.innerHTML = '<p class="text-secondary">No services returned. Check the token and mode above.</p>';
    return;
  }
  const groups = new Map();
  services.forEach((s) => {
    const key = s.umbrella_name || 'Other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  });
  list.innerHTML = [...groups.entries()].map(([carrier, items]) => `
    <div class="svc-group">
      <div class="svc-group-head">
        <h4>${esc(carrier)}</h4>
        <label class="svc-selectall"><input type="checkbox" class="svc-group-toggle"> Exclude all</label>
      </div>
      <div class="svc-grid">
        ${items.map((s) => `
          <label class="svc-item">
            <input type="checkbox" class="svc-exclude" value="${esc(s.id)}" ${excluded.has(s.id) ? 'checked' : ''}>
            <span>${esc(s.name)}</span>
          </label>`).join('')}
      </div>
    </div>`).join('');
  list.querySelectorAll('.svc-group').forEach(syncGroupToggle);
}

function svcGroupBoxes(group) {
  return [...group.querySelectorAll('.svc-exclude')];
}

// Reflect the carrier's "Exclude all" checkbox from its services: checked when
// all are excluded, indeterminate when some are.
function syncGroupToggle(group) {
  const boxes = svcGroupBoxes(group);
  const checked = boxes.filter((b) => b.checked).length;
  const toggle = group.querySelector('.svc-group-toggle');
  toggle.checked = boxes.length > 0 && checked === boxes.length;
  toggle.indeterminate = checked > 0 && checked < boxes.length;
}

function onServiceToggle(e) {
  const group = e.target.closest('.svc-group');
  if (!group) return;
  if (e.target.classList.contains('svc-group-toggle')) {
    svcGroupBoxes(group).forEach((b) => { b.checked = e.target.checked; });
  } else if (e.target.classList.contains('svc-exclude')) {
    syncGroupToggle(group);
  }
}

async function saveServices(p) {
  const listEl = document.getElementById(`services-list-${p.name}`);
  const excluded = [...listEl.querySelectorAll('.svc-exclude:checked')].map((el) => el.value);
  document.getElementById(`services-status-${p.name}`).textContent = '';
  try {
    await api(p.excluded_endpoint, { method: 'POST', body: { excluded } });
    snackbar(`Saved — ${excluded.length} service${excluded.length === 1 ? '' : 's'} hidden`, 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  }
}

async function loadSettings() {
  const settings = await api('/api/settings');
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = settings[id] === 'true';
    else el.value = settings[id] || '';
    // A select with no matching saved value falls back to its first option.
    if (el.tagName === 'SELECT' && !el.value) el.selectedIndex = 0;
  });
}

document.getElementById('save-settings').addEventListener('click', async () => {
  const body = {};
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    body[id] = el.type === 'checkbox' ? (el.checked ? 'true' : '') : el.value;
  });
  if (body.origin_state) body.origin_state = body.origin_state.toUpperCase();
  try {
    await api('/api/settings', { method: 'PUT', body });
    snackbar('Settings saved', 'success');
    await loadSettings();
    setDirty(false);
    initNav('settings');
  } catch (err) {
    snackbar(err.message, 'error');
  }
});

function testResult(elId, promise) {
  const el = document.getElementById(elId);
  el.innerHTML = '<span class="spinner"></span>';
  promise
    .then((res) => {
      el.innerHTML = `<span class="chip static ok">✓ Connected${res.account ? ' — ' + esc(res.account) : ''}${res.shop ? ' — ' + esc(res.shop) : ''}</span>`;
    })
    .catch((err) => {
      el.innerHTML = `<span class="chip static err">✕ ${esc(err.message)}</span>`;
    });
}

document.getElementById('test-printer').addEventListener('click', () => {
  testResult('printer-test-result', api('/api/settings/test/printer', {
    method: 'POST',
    body: {
      host: document.getElementById('printer_host').value,
      port: document.getElementById('printer_port').value,
    },
  }).then((r) => ({ ...r, account: 'test label sent' })));
});

document.getElementById('test-shipper').addEventListener('click', () => {
  testResult('shipper-test-result', api('/api/settings/test/shipper', {
    method: 'POST',
    body: {
      host: document.getElementById('shipper_host').value,
      port: document.getElementById('shipper_port').value,
      db: document.getElementById('shipper_db').value,
      user: document.getElementById('shipper_user').value,
      password: document.getElementById('shipper_password').value,
    },
  }));
});

document.getElementById('test-browserprint').addEventListener('click', () => {
  testResult('browserprint-test-result',
    ZebraPrint.test().then(() => ({ account: 'test label sent' })));
});

/* ---------- BackOffice databases ---------- */
async function loadDbs() {
  const dbs = await api('/api/backoffice-dbs');
  const tbody = document.getElementById('dbs-body');
  if (!dbs.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-secondary">No databases configured.</td></tr>';
    return;
  }
  tbody.innerHTML = dbs.map((d) => `<tr>
    <td><strong>${esc(d.name)}</strong></td>
    <td class="mono">${esc(d.host)}:${esc(d.port)}</td>
    <td class="mono">${esc(d.db_name)}</td>
    <td class="mono">${esc(d.prefix || '')}</td>
    <td>${d.is_active ? '<span class="chip static ok">active</span>' : '<span class="chip static">inactive</span>'}</td>
    <td>
      <button class="btn btn-text btn-small" onclick="testDb(${d.id}, this)">Test</button>
      <button class="btn btn-text btn-small" onclick="editDb(${d.id})">Edit</button>
      <button class="btn btn-danger btn-small" onclick="deleteDb(${d.id}, '${esc(d.name)}')">Delete</button>
    </td>
  </tr>`).join('');
  window._dbs = dbs;
}

window.testDb = async (id, btn) => {
  btn.disabled = true;
  try {
    await api(`/api/backoffice-dbs/${id}/test`, { method: 'POST' });
    snackbar('Connected', 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
};

function dbForm(dbRow) {
  openModal(`
    <h3>${dbRow ? 'Edit database' : 'Add BackOffice database'}</h3>
    <div class="field mb-16"><label>Name (shown in dropdowns)</label><input id="m-db-name" value="${dbRow ? esc(dbRow.name) : ''}"></div>
    <div class="field mb-16"><label>Host / IP</label><input id="m-db-host" value="${dbRow ? esc(dbRow.host) : ''}"></div>
    <div class="field mb-16"><label>Port</label><input id="m-db-port" value="${dbRow ? esc(dbRow.port) : '1433'}"></div>
    <div class="field mb-16"><label>Database name</label><input id="m-db-dbname" value="${dbRow ? esc(dbRow.db_name) : ''}"></div>
    <div class="field mb-16"><label>Order-number prefix (for scan auto-detect)</label><input id="m-db-prefix" autocomplete="off" value="${dbRow ? esc(dbRow.prefix || '') : ''}"></div>
    <div class="field mb-16"><label>Username</label><input id="m-db-user" autocomplete="off" value="${dbRow ? esc(dbRow.username) : ''}"></div>
    <div class="field mb-16"><label>Password${dbRow ? ' (leave blank to keep current)' : ''}</label><input type="password" id="m-db-pass" autocomplete="off"></div>
    ${dbRow ? `<div class="field mb-16"><label>Status</label><select id="m-db-active"><option value="true" ${dbRow.is_active ? 'selected' : ''}>Active</option><option value="false" ${!dbRow.is_active ? 'selected' : ''}>Inactive</option></select></div>` : ''}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-db-save">Save</button>
    </div>`);
  document.getElementById('m-db-save').addEventListener('click', async () => {
    const body = {
      name: document.getElementById('m-db-name').value,
      host: document.getElementById('m-db-host').value,
      port: document.getElementById('m-db-port').value,
      db_name: document.getElementById('m-db-dbname').value,
      username: document.getElementById('m-db-user').value,
      prefix: document.getElementById('m-db-prefix').value,
      password: document.getElementById('m-db-pass').value,
    };
    if (dbRow) body.is_active = document.getElementById('m-db-active').value === 'true';
    try {
      if (dbRow) {
        await api(`/api/backoffice-dbs/${dbRow.id}`, { method: 'PUT', body });
      } else {
        await api('/api/backoffice-dbs', { method: 'POST', body });
      }
      closeModal();
      snackbar('Database saved', 'success');
      loadDbs();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

document.getElementById('add-db').addEventListener('click', () => dbForm(null));
window.editDb = (id) => dbForm(window._dbs.find((d) => d.id === id));
window.deleteDb = (id, name) => {
  openModal(`
    <h3>Delete database</h3>
    <p>Delete "<strong>${name}</strong>"? Connections with existing shipments are deactivated instead.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-db-delete">Delete</button>
    </div>`);
  document.getElementById('m-db-delete').addEventListener('click', async () => {
    try {
      await api(`/api/backoffice-dbs/${id}`, { method: 'DELETE' });
      closeModal();
      loadDbs();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

/* ---------- Modal helpers ---------- */
function openModal(html) {
  document.getElementById('modal').innerHTML = html;
  document.getElementById('modal-backdrop').classList.add('show');
}
function closeModal() {
  document.getElementById('modal-backdrop').classList.remove('show');
}
document.getElementById('modal-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modal-backdrop') closeModal();
});

/* ---------- Shopify stores ---------- */
async function loadStores() {
  const stores = await api('/api/shopify-stores');
  const tbody = document.getElementById('stores-body');
  if (!stores.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary">No stores configured.</td></tr>';
    return;
  }
  tbody.innerHTML = stores.map((s) => `<tr>
    <td><strong>${esc(s.name)}</strong></td>
    <td class="mono">${esc(s.shop_domain)}</td>
    <td class="mono">${esc(s.prefix || '')}</td>
    <td>${s.is_active ? '<span class="chip static ok">active</span>' : '<span class="chip static">inactive</span>'}</td>
    <td>
      <button class="btn btn-text btn-small" onclick="testStore(${s.id}, this)">Test</button>
      <button class="btn btn-text btn-small" onclick="editStore(${s.id})">Edit</button>
      <button class="btn btn-danger btn-small" onclick="deleteStore(${s.id}, '${esc(s.name)}')">Delete</button>
    </td>
  </tr>`).join('');
  window._stores = stores;
}

window.testStore = async (id, btn) => {
  btn.disabled = true;
  try {
    const res = await api(`/api/shopify-stores/${id}/test`, { method: 'POST' });
    snackbar(`Connected to "${res.shop}"`, 'success');
  } catch (err) {
    snackbar(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
};

function storeForm(store) {
  openModal(`
    <h3>${store ? 'Edit store' : 'Add Shopify store'}</h3>
    <div class="field mb-16"><label>Name</label><input id="m-store-name" value="${store ? esc(store.name) : ''}"></div>
    <div class="field mb-16"><label>Shop domain (mystore.myshopify.com)</label><input id="m-store-domain" value="${store ? esc(store.shop_domain) : ''}"></div>
    <div class="field mb-16"><label>Order-number prefix (for scan auto-detect)</label><input id="m-store-prefix" autocomplete="off" value="${store ? esc(store.prefix || '') : ''}"></div>
    <div class="field mb-16"><label>Admin API access token${store ? ' (leave blank to keep current)' : ''}</label><input type="password" id="m-store-token" autocomplete="off"></div>
    ${store ? `<div class="field mb-16"><label>Status</label><select id="m-store-active"><option value="true" ${store.is_active ? 'selected' : ''}>Active</option><option value="false" ${!store.is_active ? 'selected' : ''}>Inactive</option></select></div>` : ''}
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-store-save">Save</button>
    </div>`);
  document.getElementById('m-store-save').addEventListener('click', async () => {
    const body = {
      name: document.getElementById('m-store-name').value,
      shop_domain: document.getElementById('m-store-domain').value,
      prefix: document.getElementById('m-store-prefix').value,
      access_token: document.getElementById('m-store-token').value,
    };
    if (store) body.is_active = document.getElementById('m-store-active').value === 'true';
    try {
      if (store) {
        await api(`/api/shopify-stores/${store.id}`, { method: 'PUT', body });
      } else {
        await api('/api/shopify-stores', { method: 'POST', body });
      }
      closeModal();
      snackbar('Store saved', 'success');
      loadStores();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
}

document.getElementById('add-store').addEventListener('click', () => storeForm(null));
window.editStore = (id) => storeForm(window._stores.find((s) => s.id === id));
window.deleteStore = (id, name) => {
  openModal(`
    <h3>Delete store</h3>
    <p>Delete store "<strong>${name}</strong>"? Stores with existing shipments are deactivated instead of deleted.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-confirm-delete">Delete</button>
    </div>`);
  document.getElementById('m-confirm-delete').addEventListener('click', async () => {
    try {
      await api(`/api/shopify-stores/${id}`, { method: 'DELETE' });
      closeModal();
      snackbar('Store deleted', 'success');
      loadStores();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

/* ---------- Users ---------- */
async function loadUsers() {
  const users = await api('/api/users');
  const tbody = document.getElementById('users-body');
  tbody.innerHTML = users.map((u) => `<tr>
    <td><strong>${esc(u.username)}</strong></td>
    <td>${esc(u.role)}</td>
    <td>${u.is_active ? '<span class="chip static ok">active</span>' : '<span class="chip static">inactive</span>'}</td>
    <td>
      <button class="btn btn-text btn-small" onclick="resetPassword(${u.id}, '${esc(u.username)}')">Reset password</button>
      ${u.is_active
        ? `<button class="btn btn-danger btn-small" onclick="deactivateUser(${u.id}, '${esc(u.username)}')">Deactivate</button>`
        : `<button class="btn btn-text btn-small" onclick="activateUser(${u.id})">Activate</button>`}
    </td>
  </tr>`).join('');
}

document.getElementById('add-user').addEventListener('click', () => {
  openModal(`
    <h3>Add user</h3>
    <div class="field mb-16"><label>Username</label><input id="m-user-name" autocomplete="off"></div>
    <div class="field mb-16"><label>Password</label><input type="password" id="m-user-pass" autocomplete="new-password"></div>
    <div class="field mb-16"><label>Role</label><select id="m-user-role"><option value="user">User</option><option value="admin">Admin</option></select></div>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-user-save">Create</button>
    </div>`);
  document.getElementById('m-user-save').addEventListener('click', async () => {
    try {
      await api('/api/users', {
        method: 'POST',
        body: {
          username: document.getElementById('m-user-name').value,
          password: document.getElementById('m-user-pass').value,
          role: document.getElementById('m-user-role').value,
        },
      });
      closeModal();
      snackbar('User created', 'success');
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
});

window.deactivateUser = (id, username) => {
  openModal(`
    <h3>Deactivate user</h3>
    <p>Deactivate "<strong>${username}</strong>"? They will no longer be able to sign in.</p>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-danger" id="m-confirm-deact">Deactivate</button>
    </div>`);
  document.getElementById('m-confirm-deact').addEventListener('click', async () => {
    try {
      await api(`/api/users/${id}`, { method: 'DELETE' });
      closeModal();
      loadUsers();
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

window.activateUser = async (id) => {
  await api(`/api/users/${id}/activate`, { method: 'POST' });
  loadUsers();
};

window.resetPassword = (id, username) => {
  openModal(`
    <h3>Reset password for ${username}</h3>
    <div class="field mb-16"><label>New password</label><input type="password" id="m-reset-pass" autocomplete="new-password"></div>
    <div class="actions">
      <button class="btn btn-text" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="m-reset-save">Set password</button>
    </div>`);
  document.getElementById('m-reset-save').addEventListener('click', async () => {
    try {
      await api(`/api/users/${id}/password`, {
        method: 'PUT',
        body: { password: document.getElementById('m-reset-pass').value },
      });
      closeModal();
      snackbar('Password updated', 'success');
    } catch (err) {
      snackbar(err.message, 'error');
    }
  });
};

document.getElementById('change-my-password').addEventListener('click', async () => {
  const password = document.getElementById('my-new-password').value;
  try {
    const me = await api('/api/auth/me');
    await api(`/api/users/${me.id}/password`, {
      method: 'PUT',
      body: { password },
    });
    snackbar('Password changed', 'success');
    document.getElementById('my-new-password').value = '';
  } catch (err) {
    snackbar(err.message, 'error');
  }
});
