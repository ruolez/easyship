const SETTING_IDS = [
  'easyship_mode', 'easyship_sandbox_token', 'easyship_production_token',
  'default_item_category',
  'origin_company', 'origin_contact', 'origin_address1', 'origin_address2',
  'origin_city', 'origin_state', 'origin_zip', 'origin_phone', 'origin_email',
  'placeholder_email', 'print_mode', 'printer_host', 'printer_port',
  'label_timeout_seconds',
];

initNav('settings').then(async () => {
  if (window.currentUser && window.currentUser.role !== 'admin') {
    ['card-easyship', 'card-origin', 'card-backoffice', 'card-boxes', 'card-workflow', 'card-stores', 'card-users']
      .forEach((id) => document.getElementById(id).style.display = 'none');
    document.getElementById('save-settings').style.display = 'none';
    return;
  }
  await loadCategories();
  await loadSettings();
  await loadStores();
  await loadDbs();
  await loadBoxes();
  await loadUsers();
});

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

async function loadCategories() {
  const select = document.getElementById('default_item_category');
  try {
    const categories = await api('/api/settings/easyship-categories');
    select.innerHTML = categories
      .map((c) => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`)
      .join('');
  } catch {
    select.innerHTML = '<option value="dry_food_supplements">Dry Food Supplements</option>';
  }
}

async function loadSettings() {
  const settings = await api('/api/settings');
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = settings[id] || '';
  });
  const catEl = document.getElementById('default_item_category');
  if (!settings.default_item_category) catEl.value = 'dry_food_supplements';
  else catEl.value = settings.default_item_category;
  if (!catEl.value) catEl.selectedIndex = 0;
}

document.getElementById('save-settings').addEventListener('click', async () => {
  const body = {};
  SETTING_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) body[id] = el.value;
  });
  body.origin_state = body.origin_state.toUpperCase();
  try {
    await api('/api/settings', { method: 'PUT', body });
    snackbar('Settings saved', 'success');
    await loadSettings();
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

document.getElementById('test-easyship').addEventListener('click', () => {
  const modeEl = document.getElementById('easyship_mode');
  const mode = modeEl.value || 'sandbox';
  modeEl.value = mode;
  testResult('easyship-test-result', api('/api/settings/test/easyship', {
    method: 'POST',
    body: {
      mode,
      token: document.getElementById(`easyship_${mode}_token`).value,
    },
  }));
});

document.getElementById('test-printer').addEventListener('click', () => {
  testResult('printer-test-result', api('/api/settings/test/printer', {
    method: 'POST',
    body: {
      host: document.getElementById('printer_host').value,
      port: document.getElementById('printer_port').value,
    },
  }).then((r) => ({ ...r, account: 'test label sent' })));
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
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary">No databases configured.</td></tr>';
    return;
  }
  tbody.innerHTML = dbs.map((d) => `<tr>
    <td><strong>${esc(d.name)}</strong></td>
    <td class="mono">${esc(d.host)}:${esc(d.port)}</td>
    <td class="mono">${esc(d.db_name)}</td>
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
    tbody.innerHTML = '<tr><td colspan="4" class="text-secondary">No stores configured.</td></tr>';
    return;
  }
  tbody.innerHTML = stores.map((s) => `<tr>
    <td><strong>${esc(s.name)}</strong></td>
    <td class="mono">${esc(s.shop_domain)}</td>
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
