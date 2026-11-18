
/* Categorical slots (dataviz reference palette, light), assigned by each
   name's place in the range's option list so a filter never repaints the
   names that stay. Past eight, names fold into Other. */
const RP_PALETTE = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'];
const RP_OTHER = '#9aa3b2';
const RP_SOURCES = { parcels: 'Parcel log', backoffice: 'BackOffice' };
const RP_PAGE = 100;
const RP_KEY = 'reports.filters';

const pad = (n) => String(n).padStart(2, '0');
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDay = (s) => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };

function quickRange(kind) {
  const today = new Date();
  const y = today.getFullYear();
  const m = today.getMonth();
  if (kind === 'month') return [new Date(y, m, 1), today];
  if (kind === 'last-month') return [new Date(y, m - 1, 1), new Date(y, m, 0)];
  if (kind === 'year') return [new Date(y, 0, 1), today];
  return [new Date(y, m, today.getDate() - 29), today];
}

function loadSaved() {
  try { return JSON.parse(localStorage.getItem(RP_KEY)) || {}; } catch { return {}; }
}

const saved = loadSaved();
const [defFrom, defTo] = quickRange('month');
const state = {
  from: saved.from || isoDay(defFrom),
  to: saved.to || isoDay(defTo),
  bucket: ['day', 'week', 'month'].includes(saved.bucket) ? saved.bucket : 'day',
  stack: saved.stack === 'store' ? 'store' : 'carrier',
  sort: 'date',
  dir: 'desc',
  offset: 0,
};
let report = null;
let loadSeq = 0;

function save() {
  try {
    localStorage.setItem(RP_KEY, JSON.stringify({
      from: state.from, to: state.to, bucket: state.bucket, stack: state.stack,
      carrier: [...carrierFilter.values], store: [...storeFilter.values], source: [...sourceFilter.values],
    }));
  } catch { /* private mode: filters just aren't remembered */ }
}

const onFilter = () => { state.offset = 0; save(); load(); };
const carrierFilter = multiFilter('rp-carrier', { onChange: onFilter });
const storeFilter = multiFilter('rp-store', { onChange: onFilter });
const sourceFilter = multiFilter('rp-source', {
  options: Object.entries(RP_SOURCES).map(([value, label]) => ({ value, label })), onChange: onFilter,
});
carrierFilter.setValues(saved.carrier);
storeFilter.setValues(saved.store);
sourceFilter.setValues(saved.source);

function query(extra = {}) {
  const p = new URLSearchParams({ from: state.from, to: state.to, bucket: state.bucket });
  [['carrier', carrierFilter], ['store', storeFilter], ['source', sourceFilter]].forEach(([k, f]) => {
    if (f.values.size) p.set(k, [...f.values].join(','));
  });
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

/* ---------- Controls ---------- */
const fromInput = document.getElementById('rp-from');
const toInput = document.getElementById('rp-to');
fromInput.value = state.from;
toInput.value = state.to;
[fromInput, toInput].forEach((el) => el.addEventListener('change', () => {
  if (!fromInput.value || !toInput.value) return;
  state.from = fromInput.value;
  state.to = toInput.value;
  onFilter();
}));

document.getElementById('rp-quick').addEventListener('click', (e) => {
  const chip = e.target.closest('[data-range]');
  if (!chip) return;
  const [f, t] = quickRange(chip.dataset.range);
  state.from = fromInput.value = isoDay(f);
  state.to = toInput.value = isoDay(t);
  onFilter();
});

function bindSeg(id, key, after) {
  const seg = document.getElementById(id);
  const paint = () => seg.querySelectorAll('button').forEach((b) => {
    b.classList.toggle('on', b.dataset.v === state[key]);
    b.setAttribute('aria-pressed', b.dataset.v === state[key]);
  });
  seg.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || b.dataset.v === state[key]) return;
    state[key] = b.dataset.v;
    paint();
    save();
    after();
  });
  paint();
}
bindSeg('rp-bucket', 'bucket', () => load());
bindSeg('rp-stack', 'stack', () => renderChart());

document.getElementById('rp-refresh').addEventListener('click', () => load(true));

document.querySelectorAll('th.sortable').forEach((th) => th.addEventListener('click', () => {
  const key = th.dataset.sort;
  state.dir = state.sort === key && state.dir === 'desc' ? 'asc' : 'desc';
  state.sort = key;
  state.offset = 0;
  document.querySelectorAll('th.sortable').forEach((h) => h.classList.remove('asc', 'desc'));
  th.classList.add(state.dir);
  loadRows();
}));
document.getElementById('rp-prev').addEventListener('click', () => { state.offset = Math.max(0, state.offset - RP_PAGE); loadRows(); });
document.getElementById('rp-next').addEventListener('click', () => { state.offset += RP_PAGE; loadRows(); });

/* ---------- Loading ---------- */
function showNotices(list, isError) {
  document.getElementById('rp-notices').innerHTML = list.map((n) => `
    <div class="rp-notice${isError ? ' error' : ''}">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>
      <span>${esc(n)}</span></div>`).join('');
}

function updateCsvLinks() {
  document.querySelectorAll('.rp-csv').forEach((a) => {
    const extra = { kind: a.dataset.kind };
    if (a.dataset.kind === 'rows') Object.assign(extra, { sort: state.sort, dir: state.dir });
    a.href = `/api/reports/spend.csv?${query(extra)}`;
  });
}

async function load(fresh = false) {
  const seq = ++loadSeq;
  document.body.classList.add('rp-loading');
  try {
    const data = await api(`/api/reports/spend?${query(fresh ? { fresh: 1 } : {})}`);
    if (seq !== loadSeq) return;
    report = data;
    carrierFilter.setOptions(data.options.carriers);
    storeFilter.setOptions(data.options.stores);
    showNotices(data.notices);
    renderTiles();
    renderChart();
    renderBreakdown('rp-carriers', 'Carrier', data.by_carrier, 'carrier');
    renderBreakdown('rp-stores', 'Store', data.by_store, 'store');
    await loadRows();
  } catch (e) {
    if (seq !== loadSeq) return;
    showNotices([e.message], true);
  } finally {
    if (seq === loadSeq) document.body.classList.remove('rp-loading');
  }
}

async function loadRows() {
  updateCsvLinks();
  try {
    renderRows(await api(`/api/reports/spend/rows?${query({
      sort: state.sort, dir: state.dir, offset: state.offset, limit: RP_PAGE,
    })}`));
  } catch (e) {
    showNotices([e.message], true);
  }
}

/* ---------- Rendering ---------- */
function colorFor(field, name) {
  const list = report.options[field === 'carrier' ? 'carriers' : 'stores'];
  const i = list.indexOf(name);
  return i >= 0 && i < RP_PALETTE.length ? RP_PALETTE[i] : RP_OTHER;
}
const folded = (field, name) => (colorFor(field, name) === RP_OTHER ? 'Other' : name);

function renderTiles() {
  const t = report.totals;
  document.getElementById('rp-total').textContent = money(t.cost);
  document.getElementById('rp-count').textContent = t.shipments.toLocaleString('en-US');
  document.getElementById('rp-avg').textContent = t.shipments ? money(t.avg) : '—';
}

function swatch(color) {
  return `<span class="rp-swatch" style="background:${color}"></span>`;
}

function renderBreakdown(id, title, groups, field) {
  const table = document.getElementById(id);
  if (!groups.length) {
    table.innerHTML = '<tbody><tr><td class="rp-none">Nothing in this range</td></tr></tbody>';
    return;
  }
  table.innerHTML = `
    <thead><tr><th>${title}</th><th class="num">Shipments</th><th class="num">Spend</th><th class="num">Average</th><th class="rp-share-col">Share</th></tr></thead>
    <tbody>${groups.map((g) => `
      <tr>
        <td>${swatch(colorFor(field, g.name))}${esc(g.name)}</td>
        <td class="num">${g.shipments.toLocaleString('en-US')}</td>
        <td class="num">${money(g.cost)}</td>
        <td class="num">${money(g.avg)}</td>
        <td class="rp-share-col"><span class="rp-share"><span class="rp-share-bar" style="width:${Math.min(100, g.share)}%"></span></span><span class="rp-share-pct">${g.share.toFixed(1)}%</span></td>
      </tr>`).join('')}</tbody>
    <tfoot><tr><td>Total</td><td class="num">${report.totals.shipments.toLocaleString('en-US')}</td><td class="num">${money(report.totals.cost)}</td><td class="num">${money(report.totals.avg)}</td><td></td></tr></tfoot>`;
}

function renderRows(data) {
  const body = document.getElementById('rp-rows');
  body.innerHTML = data.rows.map((r) => `
    <tr>
      <td>${esc(parseDay(r.date).toLocaleDateString('en-US'))}</td>
      <td><span class="rp-src ${r.source}">${RP_SOURCES[r.source]}</span></td>
      <td>${esc(r.store)}</td>
      <td>${esc(r.carrier)}</td>
      <td>${esc(r.service)}</td>
      <td class="mono">${esc(r.ref)}</td>
      <td class="mono">${esc(r.tracking)}</td>
      <td class="num">${money(r.cost)}</td>
    </tr>`).join('');
  document.getElementById('rp-empty').style.display = data.total ? 'none' : '';
  const pager = document.getElementById('rp-pager');
  pager.style.display = data.total > RP_PAGE ? '' : 'none';
  const end = Math.min(data.offset + data.rows.length, data.total);
  document.getElementById('rp-range').textContent = `${(data.offset + 1).toLocaleString('en-US')}–${end.toLocaleString('en-US')} of ${data.total.toLocaleString('en-US')}`;
  document.getElementById('rp-prev').disabled = data.offset === 0;
  document.getElementById('rp-next').disabled = end >= data.total;
}

function periodLabel(iso, long = false) {
  const d = parseDay(iso);
  if (state.bucket === 'month') return d.toLocaleDateString('en-US', { month: 'short', year: long || d.getMonth() === 0 ? 'numeric' : undefined });
  const text = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: long ? 'numeric' : undefined });
  return state.bucket === 'week' && long ? `Week of ${text}` : text;
}

/* Four gridline steps of 1, 2, 2.5 or 5 × a power of ten. */
function niceStep(max) {
  if (max <= 0) return 2.5;
  const raw = max / 4;
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= raw);
}

function compactMoney(v, step) {
  if (v >= 1000) return `$${(v / 1000).toLocaleString('en-US', { maximumFractionDigits: 1 })}k`;
  return `$${v.toLocaleString('en-US', { maximumFractionDigits: step % 1 ? 2 : 0 })}`;
}

/* A rectangle whose top corners are rounded (the free end of a bar). */
function topRounded(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function stackOf(p) {
  const field = state.stack;
  const parts = {};
  Object.entries(field === 'carrier' ? p.carriers : p.stores).forEach(([name, cost]) => {
    const key = folded(field, name);
    parts[key] = (parts[key] || 0) + cost;
  });
  return parts;
}

function seriesOrder() {
  const field = state.stack;
  const groups = field === 'carrier' ? report.by_carrier : report.by_store;
  const names = [];
  groups.forEach((g) => { const n = folded(field, g.name); if (!names.includes(n)) names.push(n); });
  return names;
}

function renderChart() {
  if (!report) return;
  const field = state.stack;
  const order = seriesOrder();
  const color = (n) => (n === 'Other' ? RP_OTHER : colorFor(field, n));
  document.getElementById('rp-legend').innerHTML = order.length > 1
    ? order.map((n) => `<span class="rp-legend-item">${swatch(color(n))}${esc(n)}</span>`).join('') : '';

  const el = document.getElementById('rp-chart');
  const periods = report.by_period;
  const W = Math.max(320, el.clientWidth);
  const H = 260;
  const M = { top: 12, right: 8, bottom: 26, left: 52 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const step = niceStep(Math.max(0, ...periods.map((p) => p.cost)));
  const max = step * 4;
  const y = (v) => M.top + plotH - (v / max) * plotH;
  const band = plotW / Math.max(1, periods.length);
  const barW = Math.max(2, Math.min(band * 0.68, 44));
  const labelEvery = Math.max(1, Math.ceil(periods.length / Math.max(1, Math.floor(plotW / 70))));

  let svg = '';
  for (let i = 0; i <= 4; i += 1) {
    const v = step * i;
    svg += `<line class="rp-grid-line" x1="${M.left}" x2="${W - M.right}" y1="${y(v)}" y2="${y(v)}"/>`;
    svg += `<text class="rp-axis" x="${M.left - 8}" y="${y(v) + 4}" text-anchor="end">${compactMoney(v, step)}</text>`;
  }
  periods.forEach((p, i) => {
    const cx = M.left + band * i + band / 2;
    const x = cx - barW / 2;
    const parts = stackOf(p);
    const names = order.filter((n) => parts[n] > 0);
    let base = 0;
    names.forEach((n, k) => {
      const y0 = y(base);
      const y1 = y(base + parts[n]);
      base += parts[n];
      const gap = k > 0 ? 2 : 0;
      const h = Math.max(0, y0 - y1 - gap);
      if (h <= 0) return;
      svg += k === names.length - 1
        ? `<path d="${topRounded(x, y1, barW, h, 4)}" fill="${color(n)}"/>`
        : `<rect x="${x}" y="${y1}" width="${barW}" height="${h}" fill="${color(n)}"/>`;
    });
    if (i % labelEvery === 0) {
      svg += `<text class="rp-axis" x="${cx}" y="${H - 8}" text-anchor="middle">${esc(periodLabel(p.period))}</text>`;
    }
    svg += `<rect class="rp-hit" data-i="${i}" x="${M.left + band * i}" y="${M.top}" width="${band}" height="${plotH}"/>`;
  });
  svg += `<line class="rp-baseline" x1="${M.left}" x2="${W - M.right}" y1="${y(0)}" y2="${y(0)}"/>`;
  el.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Shipping spend per ${state.bucket}">${svg}</svg>`;
}

/* ---------- Chart tooltip ---------- */
const tip = document.getElementById('rp-tip');
const chartEl = document.getElementById('rp-chart');
chartEl.addEventListener('mousemove', (e) => {
  const hit = e.target.closest('.rp-hit');
  chartEl.querySelectorAll('.rp-hit.on').forEach((h) => { if (h !== hit) h.classList.remove('on'); });
  if (!hit) { tip.hidden = true; return; }
  hit.classList.add('on');
  const p = report.by_period[Number(hit.dataset.i)];
  const parts = stackOf(p);
  const field = state.stack;
  const lines = seriesOrder().filter((n) => parts[n] > 0).map((n) => `
    <div class="rp-tip-row">${swatch(n === 'Other' ? RP_OTHER : colorFor(field, n))}<span>${esc(n)}</span><b>${money(parts[n])}</b></div>`).join('');
  tip.innerHTML = `<div class="rp-tip-head">${esc(periodLabel(p.period, true))}</div>
    <div class="rp-tip-row total"><span>Total · ${p.shipments.toLocaleString('en-US')} shipment${p.shipments === 1 ? '' : 's'}</span><b>${money(p.cost)}</b></div>${lines}`;
  tip.hidden = false;
  const left = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const top = e.clientY + 14 + tip.offsetHeight > window.innerHeight ? e.clientY - tip.offsetHeight - 10 : e.clientY + 14;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
});
chartEl.addEventListener('mouseleave', () => {
  tip.hidden = true;
  chartEl.querySelectorAll('.rp-hit.on').forEach((h) => h.classList.remove('on'));
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderChart, 120);
});

// initNav never resolves for a user without Reports (it redirects them).
initNav('reports').then(() => load());
