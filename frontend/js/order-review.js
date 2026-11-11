/* Order review: one order looked up by its number, shown as the Pick lists
   order window shows it (tags, hold, risk, profit, products), on the page.
   The store comes from the number's prefix. Recent lookups are kept per
   computer; ← → step through them. */
const RECENT_KEY = 'easyship.orderReviewRecent';
const RECENT_MAX = 10;

const form = document.getElementById('or-form');
const input = document.getElementById('or-number');
const result = document.getElementById('or-result');

let current = null; // the lookup on screen, with the order as Shopify last answered
let lookupSeq = 0;

initNav('order_review');

function recallRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch { return []; }
}
function keepRecent(list) {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* not kept */ }
}
let recent = recallRecent();

const recentKey = (r) => `${r.store_id}|${r.name}`;

/* A new lookup goes to the front; reopening one from the list leaves the
   order alone so ← → keep walking it. */
function rememberLookup(data, reopened) {
  const entry = { name: data.order.name, store_id: data.store.id, store_name: data.store.name, customer: data.order.customer };
  const at = recent.findIndex((r) => recentKey(r) === recentKey(entry));
  if (reopened && at >= 0) recent[at] = entry;
  else recent = [entry, ...recent.filter((r) => recentKey(r) !== recentKey(entry))].slice(0, RECENT_MAX);
  keepRecent(recent);
  renderRecent();
}

function renderRecent() {
  const box = document.getElementById('or-recent');
  box.hidden = !recent.length;
  const on = current && `${current.store.id}|${current.order.name}`;
  document.getElementById('or-recent-list').innerHTML = recent.map((r, i) => `
    <button type="button" class="or-recent-item${recentKey(r) === on ? ' active' : ''}" data-i="${i}"
      title="${attr(`${r.name} · ${r.store_name}${r.customer ? ` · ${r.customer}` : ''}`)}">
      <strong>${esc(r.name)}</strong><span>${esc(r.store_name)}</span></button>`).join('');
}

document.getElementById('or-recent-list').addEventListener('click', (e) => {
  const b = e.target.closest('.or-recent-item');
  if (b) lookup(recent[Number(b.dataset.i)].name, { reopened: true });
});

function stepRecent(delta) {
  if (!current) return;
  const at = recent.findIndex((r) => r.store_id === current.store.id && r.name === current.order.name);
  const next = recent[at + delta];
  if (at >= 0 && next) lookup(next.name, { reopened: true });
}

/* ---------- Lookup ---------- */
async function lookup(number, { reopened = false, quiet = false } = {}) {
  number = number.trim();
  if (!number) return;
  const seq = ++lookupSeq;
  if (!quiet) {
    input.value = number;
    result.innerHTML = `<div class="card or-state"><span class="spinner"></span> Looking up ${esc(number)}…</div>`;
  }
  let data;
  try {
    data = await api(`/api/picklists/orders/lookup?number=${encodeURIComponent(number)}`);
  } catch (err) {
    if (seq !== lookupSeq) return;
    if (quiet) {
      snackbar(err.message, 'error');
      return;
    }
    current = null;
    renderRecent();
    result.innerHTML = `<div class="card or-state or-error">${ICONS.err}<span>${esc(err.message)}</span></div>`;
    input.select();
    return;
  }
  if (seq !== lookupSeq) return; // a newer lookup took over
  current = data;
  showOrder();
  rememberLookup(data, reopened);
  history.replaceState(null, '', `?number=${encodeURIComponent(data.order.name.replace(/^#/, ''))}`);
  if (!quiet && document.activeElement === input) input.select(); // the next scan replaces it
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  lookup(input.value);
});

/* ---------- The order ---------- */
function pickableHtml() {
  const { not_pickable: reason, picklist, backoffice_error: boError, inventory_error: invError } = current;
  const notes = [
    boError ? `BackOffice couldn't check the barcodes: ${boError}` : '',
    invError ? `Inventory couldn't be checked: ${invError}` : '',
  ].filter(Boolean).map((n) => `<div class="hint">${esc(n)}</div>`).join('');
  if (!reason) {
    return `<div class="or-pick ok">${ICONS.ok}<div><strong>Can go on a pick list</strong>${notes}</div></div>`;
  }
  const on = picklist ? ` — pick list #${picklist.id}${picklist.quotation_number ? `, quotation ${esc(picklist.quotation_number)}` : ''}` : '';
  return `<div class="or-pick warn">${FLAG_ICONS.block}<div><strong>Can't go on a pick list:</strong> ${esc(reason)}${on}${notes}</div></div>`;
}

function holdButtonHtml(o) {
  const why = o.cancelled ? 'The order is cancelled' : o.on_hold ? 'The order is already on hold' : '';
  return `<button class="btn btn-outlined btn-small" id="om-hold" aria-haspopup="dialog" aria-expanded="false"
    title="${why || 'Hold fulfillment in Shopify (H)'}" ${why ? 'disabled' : ''}>${ICONS.pause} Put on hold <kbd>H</kbd></button>`;
}

function showOrder() {
  const { order: o, detail: d, store } = current;
  const meta = [esc(store.name), esc(shortDate(o.created_at)), esc(o.customer), o.units ? `${plural(o.units, 'unit')} to ship` : '']
    .filter(Boolean).join(' · ');
  result.innerHTML = `
    <div class="card om-page">
      <div class="om-head">
        <div>
          <div class="om-title">Order ${esc(o.name)}<span id="om-status">${statusChipsHtml(d)}</span></div>
          <div class="om-meta">${meta}<span id="om-tags">${tagsHtml(o.tags)}</span></div>
        </div>
        <div class="om-head-actions">${holdButtonHtml(o)}</div>
        <div class="om-hold" id="om-hold-panel" role="dialog" aria-label="Put ${attr(o.name)} on hold" hidden></div>
      </div>
      <div class="or-pick-row" id="or-pickable">${pickableHtml()}</div>
      <div class="om-tagbar">
        ${tagBoxHtml('order', o.tags)}
        ${tagBoxHtml('customer', o.customer_tags, !d.customer)}
      </div>
      <div class="om-body">${orderBody(d, o)}</div>
    </div>`;
  const shown = current;
  document.getElementById('om-hold').addEventListener('click', holdPanel);
  result.querySelectorAll('.tagbox').forEach((box) => wireTagBox(box, {
    order: () => shown.order,
    onSaved: (updated) => applyOrderUpdate(shown, updated),
    isCurrent: () => current === shown,
  }));
  renderRecent();
}

function applyOrderUpdate(shown, updated) {
  shown.order = { ...shown.order, ...updated };
  if (!shown.picklist) shown.not_pickable = updated.ineligible_reason;
  if (current !== shown) return;
  document.getElementById('om-tags').innerHTML = tagsHtml(updated.tags);
  const ctags = document.getElementById('om-ctags');
  if (ctags) ctags.innerHTML = tagsHtml(updated.customer_tags);
  document.getElementById('or-pickable').innerHTML = pickableHtml();
}

/* ---------- Hold ----------
   After a hold (or its Undo) the order is read again, so the status, the
   pick list verdict and the button all show it as Shopify now has it. */
function holdPanel() {
  const button = document.getElementById('om-hold');
  if (!current || !button || button.disabled) return;
  const o = current.order;
  toggleHoldPanel(o, (res) => {
    undoSnackbar(`${o.name} is on hold in Shopify`, () => releaseHold(o, res.holds));
    lookup(o.name, { reopened: true, quiet: true });
  });
}

async function releaseHold(o, holds) {
  try {
    await api('/api/picklists/orders/hold/release', { method: 'POST', body: { store_id: o.store_id, order_id: o.id, holds } });
  } catch (err) {
    snackbar(`${o.name} is still on hold: ${err.message}`, 'error');
    return;
  }
  snackbar(`${o.name} is off hold`);
  if (current && current.order.id === o.id) lookup(o.name, { reopened: true, quiet: true });
}

/* ---------- Keyboard ----------
   / finds an order, H puts the one on screen on hold, ← → step through the
   recent lookups. In the number field Esc clears it. */
document.addEventListener('keydown', (e) => {
  if (e.target === input && e.key === 'Escape') {
    input.value = '';
    return;
  }
  if (e.target instanceof Element && e.target.closest('input, select, textarea')) return;
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
  if (e.key === '/') {
    e.preventDefault();
    input.focus();
    input.select();
  } else if (e.key === 'h' || e.key === 'H') {
    e.preventDefault();
    holdPanel();
  } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    e.preventDefault();
    stepRecent(e.key === 'ArrowLeft' ? -1 : 1);
  }
});

renderRecent();
const linked = new URLSearchParams(location.search).get('number');
if (linked) lookup(linked);
