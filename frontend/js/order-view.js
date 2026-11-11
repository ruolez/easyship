/* The order window shared by Pick lists (in a modal) and Order review (on
   the page): risk, profitability, products, the "Needs review" flags, the
   order and customer tag fields and the hold panel. Pages give the order as
   the pick list lists it (/api/picklists/orders) and the details from
   /api/picklists/orders/risk. Needs api.js. */

const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
const attr = (v) => esc(v).replace(/"/g, '&quot;');

/* ---------- Flags ----------
   What to look at before an order goes on a pick list. */
const PICKLIST_TAG = /^picklist\s+\d+$/i;
const FLAG_ICONS = {
  block: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/></svg>',
  risk: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>',
  add: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M16 16h6"/><path d="M19 13v6"/><path d="M21 10V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l2-1.14"/><path d="m7.5 4.27 9 5.15"/><path d="M3.29 7 12 12l8.71-5"/><path d="M12 22V12"/></svg>',
  tag: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12.59 2.59A2 2 0 0 0 11.17 2H4a2 2 0 0 0-2 2v7.17a2 2 0 0 0 .59 1.42l8.7 8.7a2.43 2.43 0 0 0 3.42 0l6.58-6.58a2.43 2.43 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/></svg>',
};

const ISSUE_SHORT = { no_barcode: 'No barcode', unknown_barcode: 'Not in BackOffice' };

/* Each flag: a label, a short reason that fits the Needs review column, and the full detail. */
function orderFlags(o) {
  const flags = [];
  if (o.issues && o.issues.length) {
    const first = o.issues[0];
    const title = first.text.split(/ has no barcode| — barcode /)[0];
    flags.push({ tone: 'err', icon: 'block', label: 'Will be left out at Generate',
      short: `${ISSUE_SHORT[first.kind] || 'Left out'}: ${title}${o.issues.length > 1 ? ` +${o.issues.length - 1}` : ''}`,
      detail: o.issues.map((i) => i.text).join('; ') });
  }
  if (o.risk_level === 'HIGH' || o.risk_level === 'MEDIUM') {
    const advice = RECOMMENDATION_TEXT[o.risk_recommendation] || 'Shopify flagged this order';
    flags.push({ tone: o.risk_level === 'HIGH' ? 'err' : 'warn', icon: 'risk', label: `${titleCase(o.risk_level)} risk`,
      short: `${titleCase(o.risk_level)} risk`, detail: advice });
  }
  const tag = o.tags.find((t) => PICKLIST_TAG.test(t.trim()));
  if (tag) {
    flags.push({ tone: 'warn', icon: 'tag', label: 'Pick list tag', short: `Tagged “${tag}”`,
      detail: `Tagged “${tag}” — it may already be on a pick list` });
  }
  if (o.adds && o.adds.length) {
    flags.push({ tone: 'info', icon: 'add', label: 'Added to BackOffice at Generate',
      short: `From Inventory: ${o.adds[0].title}${o.adds.length > 1 ? ` +${o.adds.length - 1}` : ''}`,
      detail: `${o.adds.map((a) => `${a.title} (${a.barcode})`).join('; ')} — not in BackOffice yet, copied from Inventory when the pick list is created` });
  }
  return flags;
}

/* Each flag spelled out: short in the Needs review column, in full in the order window. */
function flagReasonsHtml(o, full = false) {
  return orderFlags(o).map((f) => `<span class="pl-why ${f.tone}" title="${attr(`${f.label}: ${f.detail}`)}">${FLAG_ICONS[f.icon]}${
    full ? `<strong>${esc(f.label)}</strong><span class="pl-why-detail">${esc(f.detail)}</span>` : `<strong class="pl-why-short">${esc(f.short)}</strong>`}</span>`).join('');
}

function tagsHtml(tags) {
  return tags.length ? `<div class="order-tags">${tags.map((t) => `<span class="tag-chip">${esc(t)}</span>`).join('')}</div>` : '';
}

const RECOMMENDATION_TEXT = {
  ACCEPT: 'You can fulfill this order',
  INVESTIGATE: 'Investigate this order before fulfilling it',
  CANCEL: 'Consider canceling this order',
};
const LEVEL_TONE = { HIGH: 'err', MEDIUM: 'warn', LOW: 'ok' };
const CARDHOLDER_TEXT = {
  MATCH: ['ok', 'Name on card matches the order'],
  LAST_NAME_ONLY: ['warn', 'Only the last name on the card matches'],
  MISMATCH: ['err', 'Name on card does not match the billing or shipping name'],
};
const SENTIMENT_TONE = { NEGATIVE: 'err', POSITIVE: 'ok' };
const svg = (paths) => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
const ICONS = {
  err: svg('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
  warn: svg('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
  ok: svg('<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>'),
  info: svg('<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>'),
  card: svg('<rect width="20" height="14" x="2" y="5" rx="2"/><path d="M2 10h20"/>'),
  globe: svg('<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>'),
  prev: svg('<path d="m15 18-6-6 6-6"/>'),
  next: svg('<path d="m9 18 6-6-6-6"/>'),
  close: svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
  search: svg('<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>'),
  check: svg('<path d="M20 6 9 17l-5-5"/>'),
  copy: svg('<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>'),
  pause: svg('<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>'),
  box: svg('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>'),
};

const titleCase = (v) => (v ? v.charAt(0) + v.slice(1).toLowerCase().replace(/_/g, ' ') : '');

function shortDate(iso) {
  if (!iso) return '';
  const d = new Date(`${iso}T12:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function sameAddress(a, b) {
  const fields = ['address1', 'address2', 'city', 'state', 'zip', 'country'];
  return !!a && !!b && fields.every((f) => (a[f] || '').trim().toLowerCase() === (b[f] || '').trim().toLowerCase());
}

function addressLines(a) {
  return [a.name, a.address1, a.address2, [[a.city, a.state].filter(Boolean).join(', '), a.zip].filter(Boolean).join(' '),
    a.country === 'US' ? '' : a.country, a.phone].filter(Boolean).map((l) => `<div>${esc(l)}</div>`).join('');
}

function omCard(label, body) {
  return `<div class="om-card"><div class="om-label">${label}</div>${body}</div>`;
}

function riskPanel(risk) {
  if (!risk) {
    return `<div class="om-risk"><div class="om-label">Order risk evaluation</div>
      <div class="text-secondary">Shopify has no risk evaluation for this order.</div></div>`;
  }
  const tone = LEVEL_TONE[risk.level] || '';
  const pill = risk.level && risk.level !== 'PENDING'
    ? `<span class="om-pill ${tone}">${esc(titleCase(risk.level))} risk</span>`
    : '<span class="om-pill">Analysis pending</span>';
  const advice = risk.level === 'PENDING' ? 'Risk analysis in progress' : RECOMMENDATION_TEXT[risk.recommendation] || '';
  const many = risk.assessments.length > 1;
  const assessments = risk.assessments.map((a) => `
    ${many ? `<div class="om-provider">${esc(a.provider || 'Shopify')} · ${esc(a.level ? `${titleCase(a.level)} risk` : 'No risk level')}</div>` : ''}
    ${a.facts.length ? `<ul class="om-facts">${a.facts.map((f) => {
      const t = SENTIMENT_TONE[f.sentiment] || 'info';
      return `<li class="${t}">${ICONS[t]}<span>${esc(f.description)}</span></li>`;
    }).join('')}</ul>` : ''}`).join('');
  const checks = [];
  if (risk.cardholder) {
    const c = risk.cardholder;
    const [t, text] = CARDHOLDER_TEXT[c.status];
    checks.push(`<div class="om-check ${t}">${ICONS.card}<div><div class="om-check-title">${text}</div>
      <div class="om-cardholder">${c.card_names.length > 1 ? 'Cards' : 'Card'}: <strong>${esc(c.card_names.join(', '))}</strong></div>
      <div class="hint">Billing: ${esc(c.billing_name || '—')} · Shipping: ${esc(c.shipping_name || '—')}</div></div></div>`);
  }
  if (risk.ip) {
    checks.push(`<div class="om-check">${ICONS.globe}<div><div class="om-check-title">IP ${esc(risk.ip)}</div>
      <div class="hint">${risk.ip_location ? `Placed from ${esc(risk.ip_location)}` : 'Location unknown'}</div></div></div>`);
  }
  return `<div class="om-risk ${tone}">
    <div class="om-risk-top"><span class="om-label">Order risk evaluation</span>${pill}${advice ? `<span class="om-advice">${esc(advice)}</span>` : ''}</div>
    ${assessments}
    ${checks.length ? `<div class="om-checks">${checks.join('')}</div>` : ''}
  </div>`;
}

function orderBody(d, o) {
  const flags = o ? orderFlags(o) : [];
  const c = d.customer || {};
  const stats = [
    ['Total', money(d.total)],
    ['Items', d.items ?? '—'],
    ['Shipping', esc(d.shipping_method || '—')],
    ['Customer orders', c.orders_count ?? '—'],
  ];
  const customer = `<div class="om-strong">${esc(c.name || '—')}</div>
    ${d.email ? `<div>${esc(d.email)}</div>` : ''}${d.phone ? `<div>${esc(d.phone)}</div>` : ''}
    <div class="hint">${[c.since ? `Customer since ${esc(shortDate(c.since))}` : '', c.amount_spent != null ? `spent ${money(c.amount_spent)}` : ''].filter(Boolean).join(' · ')}</div>
    <span id="om-ctags">${tagsHtml(c.tags || [])}</span>`;
  const ship = d.shipping_address ? addressLines(d.shipping_address) : '<span class="text-secondary">—</span>';
  const bill = !d.billing_address ? '<span class="text-secondary">—</span>'
    : sameAddress(d.billing_address, d.shipping_address)
      ? `<div class="om-strong">${esc(d.billing_address.name || '')}</div><div class="text-secondary">Same as ship to</div>`
      : addressLines(d.billing_address);
  return `
    <div class="om-main">
      ${flags.length ? `<div class="om-review"><span class="om-label">${flags.some((f) => f.tone !== 'info') ? 'Needs review' : 'Note'}</span><div class="om-review-list">${flagReasonsHtml(o, true)}</div></div>` : ''}
      <div class="om-stats">${stats.map(([l, v]) => `<div class="om-stat"><div class="om-label">${l}</div><div class="om-value">${v}</div></div>`).join('')}</div>
      <div class="om-cards">${omCard('Customer', customer)}${omCard('Ship to', ship)}${omCard('Bill to', bill)}</div>
      ${riskPanel(d.risk)}
      ${profitPanel(d.profit)}
    </div>
    ${productsPanel(d.products || [])}`;
}

/* Profitability, as in Shopify Automation's order window: revenue − product
   cost − the shipping EasyShip's past labels suggest, against the profit
   check's thresholds. */
function estimateText(est) {
  if (est.cost == null) {
    if (!est.weight_lb) return 'No estimate — the products have no weight in Shopify';
    return `No estimate — no EasyShip labels to ${esc(est.state || 'this state')} near ${est.weight_lb} lb in the last 90 days`;
  }
  return `Average of ${plural(est.samples, 'EasyShip label')} to ${esc(est.state)} within ±${est.tolerance_lb} lb (${est.weight_lb} lb of products)`;
}

function profitPanel(p) {
  if (!p) return '';
  const head = (extra = '') => `<div class="om-profit-top"><span class="om-label">Profitability</span>${extra}</div>`;
  if (!p.available) {
    return `<div class="om-profit">${head()}<div class="text-secondary">Profit can't be worked out: ${esc(p.reason_text || 'unknown reason')}.</div></div>`;
  }
  const est = p.shipping_estimate || {};
  const tone = p.below_threshold || p.profit < 0 ? 'err' : p.includes_shipping ? 'ok' : '';
  const margin = p.margin_pct != null ? ` · ${pct(p.margin_pct)} margin` : '';
  const pill = `<span class="om-pill ${tone}">${signedMoney(p.profit)}${margin}${p.includes_shipping ? '' : ' before shipping'}</span>`;
  const row = (label, value, cls = '', note = '') =>
    `<div class="profit-row${cls ? ` ${cls}` : ''}"><span>${label}${note ? ` <span class="hint">(${note})</span>` : ''}</span><span class="num">${value}</span></div>`;
  const missing = p.missing_cost.length;
  const t = p.thresholds || {};
  const limits = [t.min_amount != null ? `profit ≥ ${money(t.min_amount)}` : '', t.min_margin_pct != null ? `margin ≥ ${t.min_margin_pct}%` : '']
    .filter(Boolean).join(' · ');
  let check = '';
  if (limits && !p.includes_shipping) check = `<div class="hint">Profit check (${limits}) needs a shipping estimate</div>`;
  else if (limits && p.reasons.length) check = `<ul class="profit-reasons">${p.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`;
  else if (limits) check = `<div class="om-profit-ok">${ICONS.ok}<span>Clears the profit check (${limits})</span></div>`;
  return `<div class="om-profit ${tone}">
    ${head(pill)}
    <div class="om-profit-grid">
      <div class="profit-table">
        ${row('Products (after discounts)', money(p.items_subtotal))}
        ${row('+ Shipping paid', money(p.shipping_paid))}
        ${row('− Product cost', money(p.items_cost), '', missing ? `${missing} without cost, at 90% of price` : '')}
        ${row('− Est. shipping', est.cost == null ? '—' : money(est.cost))}
        ${row(p.includes_shipping ? '= Profit' : '= Profit before shipping', signedMoney(p.profit), `total${tone === 'err' ? ' bad' : ''}`)}
      </div>
      <div class="om-profit-side">
        <div class="om-label">Estimated shipping</div>
        <div class="om-profit-ship">${est.cost == null ? '—' : money(est.cost)}</div>
        <div class="hint">${estimateText(est)}</div>
        ${check}
      </div>
    </div>
  </div>`;
}

/* The order's products beside the details; a long list scrolls on its own
   so the window keeps its height. */
function productsPanel(products) {
  const units = products.reduce((n, p) => n + p.qty, 0);
  const rows = products.map((p) => `
    <li class="om-product">
      ${p.image ? `<img class="om-thumb" src="${esc(p.image)}" alt="" loading="lazy">` : `<span class="om-thumb om-thumb-empty">${ICONS.box}</span>`}
      <div class="om-product-info">
        <div class="om-product-title" title="${esc(p.title)}">${esc(p.title)}</div>
        ${p.variant ? `<div class="hint">${esc(p.variant)}</div>` : ''}
        <div class="om-product-codes">${[p.sku && `SKU ${esc(p.sku)}`, p.barcode ? esc(p.barcode) : p.ships && '<span class="om-nobarcode">No barcode</span>']
          .filter(Boolean).join(' · ')}</div>
        ${!p.ships ? '<div class="hint">No shipping needed</div>' : p.to_ship < p.qty ? `<div class="hint">${p.to_ship} left to ship</div>` : ''}
        ${p.cost != null
          ? `<div class="om-product-cost">Cost ${money(p.cost)}${p.margin_pct != null ? ` · <span class="${p.margin_pct < 0 ? 'neg' : p.margin_pct < 15 ? 'low' : 'good'}">${pct(p.margin_pct)} margin</span>` : ''}</div>`
          : p.ships ? '<div class="om-product-cost low">No cost in Shopify</div>' : ''}
      </div>
      <div class="om-product-qty"><strong>×${p.qty}</strong><span class="hint">${money(p.price)}</span>${p.price_original ? `<s class="hint">${money(p.price_original)}</s>` : ''}</div>
    </li>`).join('');
  return `<aside class="om-products">
    <div class="om-products-inner">
      <div class="om-products-head"><span class="om-label">Products</span><span class="hint">${products.length} item${products.length === 1 ? '' : 's'} · ${units} unit${units === 1 ? '' : 's'}</span></div>
      ${products.length ? `<ul class="om-product-list">${rows}</ul>` : '<div class="om-products-empty text-secondary">No products</div>'}
    </div>
  </aside>`;
}

/* The order's payment and fulfillment status as chips beside its title. */
function statusChipsHtml(d) {
  return [d.financial_status, d.fulfillment_status].filter(Boolean)
    .map((v) => `<span class="om-chip">${esc(titleCase(v))}</span>`).join('');
}

/* A notice with an Undo button. */
function undoSnackbar(message, undo) {
  snackbar(message);
  const el = document.getElementById('snackbar');
  el.insertAdjacentHTML('beforeend', '<button type="button" class="snackbar-action">Undo</button>');
  el.querySelector('.snackbar-action').addEventListener('click', undo, { once: true });
}

/* ---------- Hold ----------
   Like Shopify's "Hold fulfillment": a reason and an optional note. The page
   decides what follows a hold (onHeld gets the holds placed, for Undo). */
const HOLD_REASONS = [
  ['INVENTORY_OUT_OF_STOCK', 'Inventory out of stock'],
  ['INCORRECT_ADDRESS', 'Incorrect address'],
  ['HIGH_RISK_OF_FRAUD', 'High risk of fraud'],
  ['AWAITING_PAYMENT', 'Awaiting payment'],
  ['OTHER', 'Other'],
];

function toggleHoldPanel(o, onHeld) {
  const panel = document.getElementById('om-hold-panel');
  if (!panel) return;
  const button = document.getElementById('om-hold');
  if (!panel.hidden) {
    closeHoldPanel();
    return;
  }
  panel.innerHTML = `<div class="om-hold-title">Put ${esc(o.name)} on hold</div>
    <div class="hint">Fulfillment is held in Shopify until the hold is released; a held order can't go on a pick list.</div>
    <fieldset class="om-hold-reasons"><legend class="om-label">Reason</legend>
      ${HOLD_REASONS.map(([value, label], i) => `<label class="om-hold-reason"><input type="radio" name="om-hold-reason" value="${value}" ${i === 0 ? 'checked' : ''}> ${label}</label>`).join('')}
    </fieldset>
    <div class="field"><label for="om-hold-notes">Note <span class="hint">(optional)</span></label>
      <input id="om-hold-notes" maxlength="255" autocomplete="off" placeholder="Shown on the hold in Shopify"></div>
    <div class="error-note" id="om-hold-error" hidden></div>
    <div class="om-hold-actions">
      <button type="button" class="btn btn-text btn-small" id="om-hold-cancel">Cancel</button>
      <button type="button" class="btn btn-primary btn-small" id="om-hold-save">Put on hold</button>
    </div>`;
  panel.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  panel.querySelector('input[type=radio]:checked').focus();
  document.getElementById('om-hold-cancel').addEventListener('click', closeHoldPanel);
  document.getElementById('om-hold-save').addEventListener('click', () => holdOrder(o, onHeld));
  // The window's shortcuts stay out of the panel; Esc closes only the panel, Enter puts on hold.
  panel.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Escape') closeHoldPanel();
    else if (e.key === 'Enter' && e.target.tagName === 'INPUT') {
      e.preventDefault();
      holdOrder(o, onHeld);
    }
  });
}

function closeHoldPanel() {
  const panel = document.getElementById('om-hold-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  const button = document.getElementById('om-hold');
  button.setAttribute('aria-expanded', 'false');
  button.focus();
}

async function holdOrder(o, onHeld) {
  const save = document.getElementById('om-hold-save');
  if (!save || save.disabled) return;
  const reason = document.querySelector('input[name="om-hold-reason"]:checked').value;
  const notes = document.getElementById('om-hold-notes').value.trim();
  const error = document.getElementById('om-hold-error');
  save.disabled = true;
  save.textContent = 'Putting on hold…';
  error.hidden = true;
  let res;
  try {
    res = await api('/api/picklists/orders/hold', { method: 'POST', body: { store_id: o.store_id, order_id: o.id, reason, notes } });
  } catch (err) {
    if (!save.isConnected) {
      snackbar(err.message, 'error');
      return;
    }
    error.textContent = err.message;
    error.hidden = false;
    save.disabled = false;
    save.textContent = 'Put on hold';
    return;
  }
  onHeld(res);
}

/* ---------- Tag fields in the order window ----------
   Like Shopify's: the current tags as chips (× removes one), typing suggests
   the store's tags, Enter / comma / a click adds. Every change is saved to
   Shopify straight away, one at a time per field, and the order comes back
   with the rules it now matches. */
const shopTags = new Map(); // `${store}|${kind}` → Promise<string[]>
const TAG_SUGGESTIONS = 8;

function loadShopTags(storeId, kind) {
  const key = `${storeId}|${kind}`;
  if (!shopTags.has(key)) {
    const pending = api(`/api/picklists/tags?store_id=${storeId}&kind=${kind}`).then((r) => r.tags);
    pending.catch(() => shopTags.delete(key)); // tried again on the next focus
    shopTags.set(key, pending);
  }
  return shopTags.get(key);
}

function rememberShopTag(storeId, kind, tag) {
  const key = `${storeId}|${kind}`;
  if (shopTags.has(key)) {
    shopTags.set(key, shopTags.get(key).then((all) => (all.some((t) => t.toLowerCase() === tag.toLowerCase()) ? all : [tag, ...all])));
  }
}

const tagChipHtml = (t, pending = false) => `<span class="tagbox-chip${pending ? ' pending' : ''}" data-tag="${attr(t)}">${esc(t)}<button type="button" class="tagbox-x" aria-label="Remove ${attr(t)}" title="Remove">${ICONS.close}</button></span>`;

function tagBoxHtml(kind, tags, disabled = false) {
  const label = kind === 'order' ? 'Order tags' : 'Customer tags';
  return `<div class="tagbox${disabled ? ' disabled' : ''}" data-kind="${kind}">
    <div class="tagbox-field" title="${disabled ? 'This order has no customer' : label}">
      <span class="tagbox-label">${label}</span>
      ${(tags || []).map((t) => tagChipHtml(t)).join('')}
      <input class="tagbox-input" placeholder="${disabled ? 'No customer' : 'Add…'}" ${disabled ? 'disabled' : ''}
        autocomplete="off" spellcheck="false" aria-label="Add ${kind} tag" aria-autocomplete="list">
    </div>
    <ul class="tagbox-menu" role="listbox" hidden></ul>
  </div>`;
}

/* Typed text first as "Add …" when it is new, then the store's tags that
   start with it, then those that contain it (Shopify's popularity order). */
function tagOptions(all, have, typed) {
  const needle = typed.toLowerCase();
  const free = all.filter((t) => !have.has(t.toLowerCase()));
  const starts = free.filter((t) => t.toLowerCase().startsWith(needle));
  const contains = needle ? free.filter((t) => !t.toLowerCase().startsWith(needle) && t.toLowerCase().includes(needle)) : [];
  const options = [...starts, ...contains].slice(0, TAG_SUGGESTIONS).map((tag) => ({ tag }));
  const known = have.has(needle) || all.some((t) => t.toLowerCase() === needle);
  return typed && !known ? [{ tag: typed, isNew: true }, ...options] : options;
}

function highlightTag(tag, typed) {
  const at = typed ? tag.toLowerCase().indexOf(typed.toLowerCase()) : -1;
  if (at < 0) return esc(tag);
  return `${esc(tag.slice(0, at))}<mark>${esc(tag.slice(at, at + typed.length))}</mark>${esc(tag.slice(at + typed.length))}`;
}

/* order() is the order as the page has it now; onSaved gets it back from
   Shopify after each change; isCurrent says the field is still on screen. */
function wireTagBox(box, { order, onSaved, isCurrent }) {
  const kind = box.dataset.kind;
  const input = box.querySelector('.tagbox-input');
  const menu = box.querySelector('.tagbox-menu');
  const saved = () => (kind === 'order' ? order().tags : order().customer_tags) || [];
  const shown = () => [...box.querySelectorAll('.tagbox-chip')].map((c) => c.dataset.tag);
  let options = [];
  let active = 0;
  let queue = Promise.resolve();
  let saving = 0;
  const unsaved = new Set();

  function showChips(tags) {
    box.querySelectorAll('.tagbox-chip').forEach((c) => c.remove());
    input.insertAdjacentHTML('beforebegin', tags.map((t) => tagChipHtml(t, unsaved.has(t))).join(''));
  }

  function closeMenu() {
    menu.hidden = true;
    options = [];
  }

  async function openMenu() {
    const typed = input.value.trim();
    let all = [];
    try { all = await loadShopTags(order().store_id, kind); } catch { /* typing still adds */ }
    if (document.activeElement !== input || input.value.trim() !== typed) return; // moved on meanwhile
    options = tagOptions(all, new Set(shown().map((t) => t.toLowerCase())), typed);
    active = 0;
    if (!options.length) {
      closeMenu();
      return;
    }
    menu.innerHTML = options.map((opt, i) => `<li class="tagbox-opt${i === active ? ' active' : ''}" role="option" data-i="${i}">
      ${opt.isNew ? `<span class="hint">Add</span> “${esc(opt.tag)}”` : highlightTag(opt.tag, typed)}</li>`).join('');
    menu.hidden = false;
  }

  function markActive() {
    menu.querySelectorAll('.tagbox-opt').forEach((li, i) => li.classList.toggle('active', i === active));
    const li = menu.children[active];
    if (li) li.scrollIntoView({ block: 'nearest' });
  }

  function save(change, optimistic) {
    (change.add || []).forEach((t) => unsaved.add(t));
    showChips(optimistic);
    saving += 1;
    queue = queue.then(async () => {
      try {
        const o = order();
        const res = await api('/api/picklists/orders/tags', {
          method: 'POST', body: { store_id: o.store_id, order_id: o.id, kind, ...change },
        });
        (change.add || []).forEach((t) => rememberShopTag(o.store_id, kind, t));
        onSaved(res.order);
      } catch (err) {
        snackbar(err.message, 'error');
      }
      (change.add || []).forEach((t) => unsaved.delete(t));
      saving -= 1;
      // Redraw from Shopify's answer once nothing else is queued, so later chips don't flicker.
      if (!saving && isCurrent() && box.isConnected) showChips(saved());
    });
  }

  function addTag(raw) {
    const have = new Set(shown().map((t) => t.toLowerCase()));
    const tags = raw.split(',').map((t) => t.trim()).filter((t) => t && !have.has(t.toLowerCase()));
    input.value = '';
    closeMenu();
    if (tags.length) save({ add: tags }, [...shown(), ...tags]);
  }

  if (input.disabled) return;
  input.addEventListener('focus', openMenu);
  input.addEventListener('input', () => {
    if (input.value.includes(',')) addTag(input.value);
    else openMenu();
  });
  input.addEventListener('blur', closeMenu);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!options.length) return;
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      markActive();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const pick = options[active];
      if (pick && !menu.hidden) addTag(pick.tag);
      else if (input.value.trim()) addTag(input.value);
    } else if (e.key === 'Escape') {
      if (!menu.hidden) closeMenu();
      else input.blur();
    }
  });
  menu.addEventListener('mousedown', (e) => {
    const li = e.target.closest('.tagbox-opt');
    e.preventDefault(); // keep the input focused
    if (li) addTag(options[Number(li.dataset.i)].tag);
  });
  box.querySelector('.tagbox-field').addEventListener('click', (e) => {
    const x = e.target.closest('.tagbox-x');
    if (!x) {
      input.focus();
      return;
    }
    const tag = x.closest('.tagbox-chip').dataset.tag;
    save({ remove: [tag] }, shown().filter((t) => t !== tag));
  });
}
