let stores = [];
let activeStoreId = null;

initNav('orders');

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-shopify').style.display =
      tab.dataset.tab === 'shopify' ? '' : 'none';
    document.getElementById('tab-backoffice').style.display =
      tab.dataset.tab === 'backoffice' ? '' : 'none';
  });
});

/* ----- Shopify ----- */
async function loadStores() {
  const chipsEl = document.getElementById('store-chips');
  try {
    stores = (await api('/api/shopify-stores')).filter((s) => s.is_active);
  } catch (err) {
    stores = [];
  }
  if (!stores.length) {
    chipsEl.innerHTML =
      '<span class="text-secondary">No Shopify stores configured — add one in <a href="/settings.html">Settings</a>.</span>';
    showShopifyEmpty('No stores configured.');
    return;
  }
  activeStoreId = activeStoreId || stores[0].id;
  chipsEl.innerHTML = stores
    .map(
      (s) =>
        `<button class="chip ${s.id === activeStoreId ? 'active' : ''}" data-store="${s.id}">${esc(s.name)}</button>`
    )
    .join(' ');
  chipsEl.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      activeStoreId = Number(chip.dataset.store);
      loadStores();
      loadShopifyOrders();
    });
  });
  loadShopifyOrders();
}

function showShopifyEmpty(msg) {
  document.getElementById('shopify-orders').innerHTML = '';
  const el = document.getElementById('shopify-empty');
  el.textContent = msg;
  el.style.display = '';
}

async function loadShopifyOrders() {
  if (!activeStoreId) return;
  const tbody = document.getElementById('shopify-orders');
  document.getElementById('shopify-empty').style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="7"><span class="spinner"></span> Loading…</td></tr>';
  try {
    const orders = await api(`/api/shopify/orders?store_id=${activeStoreId}`);
    if (!orders.length) {
      showShopifyEmpty('No unfulfilled orders.');
      return;
    }
    tbody.innerHTML = orders
      .map(
        (o) => `<tr>
          <td><strong>${esc(o.name)}</strong></td>
          <td>${esc(o.created_at)}</td>
          <td>${esc(o.customer)}</td>
          <td class="wrap">${esc(o.ship_to)}</td>
          <td>${o.item_count}</td>
          <td>${money(o.total)}</td>
          <td><button class="btn btn-primary btn-small"
            onclick="location.href='/ship.html?source=shopify&store_id=${activeStoreId}&order_id=${encodeURIComponent(o.id)}'">
            Ship</button></td>
        </tr>`
      )
      .join('');
  } catch (err) {
    showShopifyEmpty(err.message);
  }
}

/* ----- BackOffice ----- */
async function loadInvoices() {
  const tbody = document.getElementById('bo-invoices');
  document.getElementById('bo-empty').style.display = 'none';
  tbody.innerHTML = '<tr><td colspan="8"><span class="spinner"></span> Loading…</td></tr>';
  const q = encodeURIComponent(document.getElementById('bo-search').value.trim());
  const days = document.getElementById('bo-days').value;
  try {
    const invoices = await api(`/api/backoffice/invoices?days=${days}&q=${q}`);
    if (!invoices.length) {
      tbody.innerHTML = '';
      const el = document.getElementById('bo-empty');
      el.textContent = 'No open invoices found.';
      el.style.display = '';
      return;
    }
    tbody.innerHTML = invoices
      .map(
        (inv) => `<tr>
          <td><strong>${esc(inv.invoice_number)}</strong>${inv.tracking_no ? `<br><span class="chip static warn" title="Already has tracking number">✓ ${esc(inv.tracking_no)}</span>` : ''}</td>
          <td>${esc(inv.ship_date)}</td>
          <td>${esc(inv.business_name)}</td>
          <td class="wrap">${esc(inv.ship_to)}</td>
          <td>${inv.no_boxes ?? ''}</td>
          <td>${inv.total_weight ?? ''}</td>
          <td>${money(inv.invoice_total)}</td>
          <td><button class="btn btn-primary btn-small"
            onclick="location.href='/ship.html?source=backoffice&invoice_id=${inv.invoice_id}'">
            Ship</button></td>
        </tr>`
      )
      .join('');
  } catch (err) {
    tbody.innerHTML = '';
    const el = document.getElementById('bo-empty');
    el.textContent = err.message;
    el.style.display = '';
  }
}

document.getElementById('refresh-shopify').addEventListener('click', loadShopifyOrders);
document.getElementById('refresh-bo').addEventListener('click', loadInvoices);
document.getElementById('bo-search').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadInvoices();
});
document.getElementById('bo-days').addEventListener('change', loadInvoices);

loadStores();
loadInvoices();
