initNav('scan');

const sourceSelect = document.getElementById('source-select');
const numberInput = document.getElementById('order-number');
const statusEl = document.getElementById('lookup-status');

async function loadSources() {
  const [dbs, stores] = await Promise.all([
    api('/api/backoffice-dbs').catch(() => []),
    api('/api/shopify-stores').catch(() => []),
  ]);
  const groups = [];
  const activeDbs = dbs.filter((d) => d.is_active);
  const activeStores = stores.filter((s) => s.is_active);
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
  numberInput.focus();
}

async function lookup() {
  const source = sourceSelect.value;
  const number = numberInput.value.trim();
  if (!source) { snackbar('Configure a BackOffice database or Shopify store in Settings first', 'error'); return; }
  if (!number) { numberInput.focus(); return; }
  localStorage.setItem('easyship.lastSource', source);
  const [kind, id] = source.split(':');
  statusEl.innerHTML = '<span class="spinner"></span>';
  try {
    if (kind === 'backoffice') {
      const inv = await api(`/api/backoffice/${id}/lookup?number=${encodeURIComponent(number)}`);
      location.href = `/ship.html?source=backoffice&db_id=${id}&invoice_id=${inv.invoice_id}`;
    } else {
      const order = await api(`/api/shopify/lookup?store_id=${id}&number=${encodeURIComponent(number)}`);
      location.href = `/ship.html?source=shopify&store_id=${id}&order_id=${encodeURIComponent(order.id)}`;
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

loadSources();
