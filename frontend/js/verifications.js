/* Verification history: every order verified (or started) on the Verify
   page, with its scans; and the wrong-barcode queue (admins and Verify
   override users). Packers see only their own orders of today. */
const STATUS_LABEL = { verified: 'Verified', in_progress: 'In progress', reset: 'Started over' };
const EVENT_LABEL = {
  start: 'Opened', scan: 'Scan', count: 'Typed count', check_all: 'Check all', confirm: 'Confirmed (no barcode)',
  force_line: 'Marked without scanning', force_order: 'Whole order marked without scanning', undo: 'Undo',
  reset: 'Started over', complete: 'Order verified', lines_changed: 'Order changed', reopened: 'Needs verifying again',
};
const SCAN_LABEL = {
  ok: '', line_done: 'item complete', order_done: 'order complete', wrong: 'not on the order',
  over: 'already complete', case_over: 'case bigger than what was left',
};
let rows = [];
let wrong = [];
let canSeeAll = false;
let filterTimer = null;

initNav('verify').then(() => {
  load();
});

function ct(iso) {
  return iso ? new Date(iso).toLocaleString('en-US', {
    timeZone: 'America/Chicago', month: '2-digit', day: '2-digit', year: 'numeric', hour: 'numeric', minute: '2-digit',
  }) : '';
}

function params() {
  const p = new URLSearchParams();
  const val = (id) => document.getElementById(id).value.trim();
  [['q', 'vh-q'], ['status', 'vh-status'], ['source', 'vh-source'], ['user_id', 'vh-user'], ['from', 'vh-from'], ['to', 'vh-to']]
    .forEach(([key, id]) => { if (val(id)) p.set(key, val(id)); });
  if (document.getElementById('vh-forced').checked) p.set('forced', '1');
  return p;
}

async function load() {
  let data;
  try {
    data = await api(`/api/verify/history?${params()}`);
  } catch (err) {
    snackbar(err.message, 'error');
    return;
  }
  rows = data.rows;
  if (data.can_see_all !== canSeeAll || (data.can_see_all && document.getElementById('vh-user').options.length === 1)) {
    canSeeAll = data.can_see_all;
    document.getElementById('vh-user-field').hidden = !canSeeAll;
    document.getElementById('vh-wrong-tab').hidden = !canSeeAll;
    document.getElementById('vh-sub').textContent = canSeeAll
      ? 'Every order scanned on the Verify page'
      : 'Your orders verified today';
    const sel = document.getElementById('vh-user');
    sel.innerHTML = '<option value="">Anyone</option>' + data.users.map((u) => `<option value="${u.id}">${esc(u.username)}</option>`).join('');
    if (canSeeAll) loadWrong();
  }
  renderRows(data.limit);
}

function statusPill(status) {
  return `<span class="vh-pill ${status}">${STATUS_LABEL[status] || esc(status)}</span>`;
}

function renderStats() {
  const count = (fn) => rows.filter(fn).length;
  const stats = [
    ['verified', 'Verified', count((r) => r.status === 'verified')],
    ['in_progress', 'In progress', count((r) => r.status === 'in_progress')],
    ['forced', 'Marked without scanning', count((r) => r.forced)],
    ['errors', 'Wrong scans', rows.reduce((sum, r) => sum + (r.errors || 0), 0)],
  ];
  document.getElementById('vh-stats').innerHTML = stats.map(([key, label, n]) => `
    <div class="vh-stat ${key}${n ? '' : ' zero'}"><b>${n}</b><span>${label}</span></div>`).join('');
}

function writebackCell(r) {
  const state = r.source === 'shopify' ? r.shopify_tag_state : r.dbadmin_state;
  const label = r.source === 'shopify' ? 'Shopify tag' : 'DB_Admin';
  if (state === 'done') return `<span class="chip static ok">✓ ${label}</span>`;
  if (state === 'failed') return `<span class="chip static err">✕ ${label}</span>`;
  if (state === 'pending') return `<span class="chip static">${label}…</span>`;
  return '';
}

function renderRows(limit) {
  const body = document.getElementById('vh-body');
  const empty = document.getElementById('vh-empty');
  body.innerHTML = rows.map((r) => `<tr class="vh-row" data-id="${r.id}">
    <td class="vh-c-order"><b>${esc(r.order_name)}</b></td>
    <td class="vh-c-source">${esc(r.source === 'shopify' ? (r.store_name || 'Shopify') : (r.source_db || 'BackOffice'))}</td>
    <td class="vh-c-customer">${esc(r.customer || '')}</td>
    <td class="vh-c-status">${statusPill(r.status)}${r.forced ? ' <span class="vf-forced-tag">marked</span>' : ''}</td>
    <td class="num vh-c-units" data-label="Units"><span class="vh-units"><span class="vh-units-bar" style="--pct:${r.units_required ? Math.round((r.units_verified / r.units_required) * 100) : 0}%"></span>${r.units_verified} / ${r.units_required}</span></td>
    <td class="num vh-c-errors${r.errors ? ' err' : ''}" data-label="Wrong scans">${r.errors || ''}</td>
    <td data-label="Verified by">${esc(r.verified_by_name || '')}</td>
    <td data-label="Verified">${esc(ct(r.verified_at))}</td>
    <td class="text-secondary" data-label="Started">${esc(r.started_by_name || '')} · ${esc(ct(r.started_at))}</td>
    <td class="vh-c-wb">${writebackCell(r)}</td>
  </tr>`).join('');
  renderStats();
  empty.style.display = rows.length ? 'none' : '';
  empty.textContent = 'No verifications match.';
  if (rows.length >= limit) snackbar(`Showing the newest ${limit} — narrow the filters to see older ones`);
}

document.getElementById('vh-body').addEventListener('click', (e) => {
  const tr = e.target.closest('.vh-row');
  if (tr) openDetail(Number(tr.dataset.id));
});

['vh-status', 'vh-source', 'vh-user', 'vh-from', 'vh-to', 'vh-forced'].forEach((id) => {
  document.getElementById(id).addEventListener('change', load);
});
document.getElementById('vh-q').addEventListener('input', () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(load, 300);
});

/* ---------- detail ---------- */

function eventText(e) {
  if (e.kind === 'scan') {
    const what = e.result in SCAN_LABEL ? SCAN_LABEL[e.result] : e.result;
    const product = e.title ? ` — ${e.title}` : '';
    return `${e.barcode}${product}${e.units > 1 ? ` (+${e.units})` : ''}${what ? ` · ${what}` : ''}`;
  }
  if (['count', 'check_all', 'confirm', 'force_line'].includes(e.kind)) {
    return `${EVENT_LABEL[e.kind]} — ${e.title || ''}: ${e.qty_before} → ${e.qty_after}`;
  }
  if (e.kind === 'lines_changed' && e.detail && e.detail.changes) {
    return `${EVENT_LABEL[e.kind]}: ${e.detail.changes.map((c) => `${c.title} ${c.from} → ${c.to}`).join(', ')}`;
  }
  if (e.kind === 'undo') return `Undo — ${e.title || ''} ${e.units}`;
  return EVENT_LABEL[e.kind] || e.kind;
}

async function openDetail(id) {
  const backdrop = document.getElementById('modal-backdrop');
  const modal = document.getElementById('modal');
  modal.innerHTML = '<div class="spinner"></div>';
  backdrop.classList.add('show');
  let state;
  let events;
  try {
    [state, events] = await Promise.all([api(`/api/verify/${id}`), api(`/api/verify/${id}/events`)]);
  } catch (err) {
    modal.innerHTML = `<h3>Could not load</h3><p>${esc(err.message)}</p><div class="actions"><button class="btn btn-text" id="m-close">Close</button></div>`;
    document.getElementById('m-close').addEventListener('click', () => backdrop.classList.remove('show'));
    return;
  }
  const wb = state.writebacks;
  const wbState = state.source === 'shopify' ? wb.shopify : wb.dbadmin;
  modal.innerHTML = `
    <h3>${esc(state.order_name)} <span class="text-secondary" style="font-weight:400">· ${esc(state.source_name || '')}${state.customer ? ` · ${esc(state.customer)}` : ''}</span></h3>
    <p class="mb-16">${statusPill(state.status)}
      ${state.verified_by ? ` by ${esc(state.verified_by)} · ${esc(ct(state.verified_at))}` : ''}
      <span class="vh-detail-units">${state.progress.units_verified} / ${state.progress.units_required} units</span>
      ${wbState && wbState.state === 'failed' ? `<br><span class="chip static err">✕ ${state.source === 'shopify' ? 'Shopify tag' : 'DB_Admin Checker'} failed</span> <span class="text-secondary">${esc(wbState.error || '')}</span> <button class="btn btn-text btn-small" id="m-retry">Retry</button>` : ''}</p>
    <div class="table-wrap vh-lines-wrap"><table class="striped compact vh-lines">
      <thead><tr><th>Item</th><th>Barcode</th><th class="num">Verified</th></tr></thead>
      <tbody>${state.lines.map((l) => `<tr>
        <td>${esc(l.title)}${l.forced ? ' <span class="vf-forced-tag">marked</span>' : ''}</td>
        <td class="${l.barcode && !l.skip_reason ? 'mono' : 'text-secondary'}">${esc(l.skip_reason || l.barcode || 'no barcode')}</td>
        <td class="num">${l.skip_reason ? 'skipped' : `${l.qty_verified} / ${l.qty_required}`}</td></tr>`).join('')}</tbody>
    </table></div>
    <ul class="vh-events">${events.map((e) => `<li class="${e.kind === 'scan' && ['wrong', 'over', 'case_over'].includes(e.result) ? 'err' : ''}${e.undone_at ? ' undone' : ''}">
      <span class="text-secondary">${esc(ct(e.created_at))}</span>
      <span>${esc(e.username || '')} — ${esc(eventText(e))}</span></li>`).join('')}</ul>
    <div class="actions"><button class="btn btn-text" id="m-close">Close</button></div>`;
  document.getElementById('m-close').addEventListener('click', () => backdrop.classList.remove('show'));
  const retry = document.getElementById('m-retry');
  if (retry) {
    retry.addEventListener('click', async () => {
      try {
        await api(`/api/verify/${id}/writebacks/retry`, { method: 'POST' });
        openDetail(id);
        load();
      } catch (err) {
        snackbar(err.message, 'error');
      }
    });
  }
}

document.getElementById('modal-backdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modal-backdrop') e.target.classList.remove('show');
});

/* ---------- wrong barcodes ---------- */

async function loadWrong() {
  try {
    wrong = await api(`/api/verify/wrong-barcodes${document.getElementById('vw-show-fixed').checked ? '?fixed=1' : ''}`);
  } catch (err) {
    snackbar(err.message, 'error');
    return;
  }
  const open = wrong.filter((w) => !w.fixed_at).length;
  document.getElementById('vh-wrong-count').textContent = open ? `(${open})` : '';
  document.getElementById('vw-body').innerHTML = wrong.map((w) => `<tr>
    <td>${w.fixed_at ? '' : `<input type="checkbox" class="vw-pick" value="${w.id}">`}</td>
    <td class="mono">${esc(w.barcode)}</td>
    <td>${esc(w.order_name)}</td>
    <td>${esc(w.source === 'shopify' ? (w.store_name || 'Shopify') : (w.source_db || 'BackOffice'))}</td>
    <td>${esc(w.username || '')}</td>
    <td>${esc(ct(w.created_at))}</td>
    <td class="text-secondary">${w.fixed_at ? `${esc(w.fixed_by_name || '')} · ${esc(ct(w.fixed_at))}` : ''}</td>
  </tr>`).join('');
  const empty = document.getElementById('vw-empty');
  empty.style.display = wrong.length ? 'none' : '';
  empty.textContent = 'No wrong barcodes to look at.';
  document.getElementById('vw-all').checked = false;
  updateFixButton();
}

function updateFixButton() {
  document.getElementById('vw-fix').disabled = !document.querySelectorAll('.vw-pick:checked').length;
}

document.getElementById('vw-body').addEventListener('change', updateFixButton);
document.getElementById('vw-all').addEventListener('change', (e) => {
  document.querySelectorAll('.vw-pick').forEach((cb) => { cb.checked = e.target.checked; });
  updateFixButton();
});
document.getElementById('vw-show-fixed').addEventListener('change', loadWrong);
document.getElementById('vw-fix').addEventListener('click', async () => {
  const ids = [...document.querySelectorAll('.vw-pick:checked')].map((cb) => Number(cb.value));
  try {
    await api('/api/verify/wrong-barcodes/fix', { method: 'POST', body: { ids } });
    snackbar(`Marked ${ids.length} fixed`, 'success');
    loadWrong();
  } catch (err) {
    snackbar(err.message, 'error');
  }
});

/* ---------- tabs ---------- */

document.getElementById('vh-tabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (!tab) return;
  document.querySelectorAll('#vh-tabs .tab').forEach((b) => b.classList.toggle('active', b === tab));
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === tab.dataset.tab));
});
