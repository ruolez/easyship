/* Verify page: scan an order's barcode (Shopify packing slip or BackOffice
   quotation), then scan every product in it. Each scan is saved at once —
   one at a time, in order, retried on network trouble — and answered with a
   tone, a vibration and a color: green flash for a good scan, a red screen
   that must be tapped away for a wrong one. */
const OPEN_KEY = 'easyship.verifyOpen';
const RETRY_DELAYS_MS = [600, 1500];

const startView = document.getElementById('vf-start');
const orderView = document.getElementById('vf-order');
const startStatus = document.getElementById('vf-start-status');
const alertEl = document.getElementById('vf-alert');
const doneEl = document.getElementById('vf-done');
const sheetBackdrop = document.getElementById('vf-sheet-backdrop');
const sheetEl = document.getElementById('vf-sheet');

let current = null; // the open verification, as the API returns it
let lastScan = null; // { lineId, result, code }
let blocker = null; // 'alert' | 'error' | 'opening' — scans are refused until it clears
let banner = null;
const queue = [];
let pumping = false;

const scanner = createScanCapture({ onScan: (code) => onScan(code) });

initNav('verify').then(() => {
  document.getElementById('vf-camera-order').hidden = !CameraScan.supported;
  document.getElementById('vf-camera').hidden = !CameraScan.supported;
  renderSoundToggle();
  resumeOpen();
});

/* ---------- helpers ---------- */

function clientId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const isTransient = (err) => !err.status || [500, 503, 504].includes(err.status);

async function withRetry(fn) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await fn();
    } catch (err) {
      if (!isTransient(err) || attempt >= RETRY_DELAYS_MS.length) throw err;
      await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]));
    }
  }
}

function time(iso) {
  return iso ? new Date(iso).toLocaleString([], { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
}

function lineById(id) {
  return current && current.lines.find((l) => l.id === id);
}

function canScanNow() {
  return !blocker && sheetBackdrop.hidden;
}

function flash(kind) {
  const el = document.getElementById('vf-flash');
  el.className = `vf-flash show ${kind}`;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => { el.className = 'vf-flash'; }, kind === 'done' ? 600 : 320);
}

/* ---------- blocking alert (wrong scan, refusals) ---------- */

function showAlert({ title, message, code = '', sound = 'wrong', tone = 'error' }) {
  blocker = 'alert';
  Feedback.play(sound);
  alertEl.className = `vf-alert ${tone}`;
  alertEl.innerHTML = `
    <div class="vf-alert-card">
      <div class="vf-alert-icon" aria-hidden="true">${tone === 'error' ? '✕' : '!'}</div>
      <h2>${esc(title)}</h2>
      <p>${esc(message)}</p>
      ${code ? `<div class="vf-alert-code mono">${esc(code)}</div>` : ''}
      <button class="vf-alert-ok" id="vf-alert-ok">OK</button>
      <div class="vf-alert-hint">Tap OK to keep scanning</div>
    </div>`;
  alertEl.hidden = false;
  document.getElementById('vf-alert-ok').addEventListener('click', closeAlert);
}

function closeAlert() {
  alertEl.hidden = true;
  if (blocker === 'alert' || blocker === 'error') blocker = null;
  scanner.focus();
  pump();
}

/* A request that failed for good: the full text stays on screen so it can be
   read and copied. Retry sends the same request again (counted once). */
function showError({ title, err, retry, discard }) {
  blocker = 'error';
  Feedback.play('wrong');
  const text = [
    `${title}`,
    `Error: ${err.message}`,
    err.status ? `HTTP status: ${err.status}` : 'No response from the server (network)',
    current ? `Order: ${current.order_name} (verification ${current.id})` : '',
    `Time: ${new Date().toLocaleString()}`,
  ].filter(Boolean).join('\n');
  alertEl.className = 'vf-alert error';
  alertEl.innerHTML = `
    <div class="vf-alert-card">
      <div class="vf-alert-icon" aria-hidden="true">!</div>
      <h2>${esc(title)}</h2>
      <pre class="vf-error-text" id="vf-error-text">${esc(text)}</pre>
      <div class="vf-error-actions">
        <button class="vf-alert-ok" id="vf-error-retry">Retry</button>
        <button class="vf-alert-secondary" id="vf-error-copy">Copy</button>
        ${discard ? '<button class="vf-alert-secondary" id="vf-error-discard">Skip this scan</button>' : '<button class="vf-alert-secondary" id="vf-error-close">Close</button>'}
      </div>
    </div>`;
  alertEl.hidden = false;
  document.getElementById('vf-error-retry').addEventListener('click', () => {
    alertEl.hidden = true;
    blocker = null;
    retry();
  });
  document.getElementById('vf-error-copy').addEventListener('click', () => copyText(text));
  const dismiss = document.getElementById(discard ? 'vf-error-discard' : 'vf-error-close');
  dismiss.addEventListener('click', () => {
    if (discard) discard();
    closeAlert();
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    snackbar('Copied', 'success');
  } catch {
    const range = document.createRange();
    range.selectNodeContents(document.getElementById('vf-error-text'));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    snackbar('Selected — copy it from the menu', '');
  }
}

/* ---------- scanning ---------- */

function onScan(code) {
  Feedback.unlock();
  Feedback.keepAwake();
  if (blocker || !sheetBackdrop.hidden) {
    Feedback.play('blocked');
    return;
  }
  if (!current || current.status !== 'in_progress') {
    openOrder(code);
    return;
  }
  queue.push({ code, id: clientId() });
  pump();
}

async function pump() {
  if (pumping || blocker) return;
  pumping = true;
  try {
    while (queue.length && !blocker && current) {
      const scan = queue[0];
      let res;
      try {
        res = await withRetry(() => api(`/api/verify/${current.id}/scan`, {
          method: 'POST', body: { code: scan.code, client_id: scan.id },
        }));
      } catch (err) {
        if (isTransient(err)) {
          showError({
            title: `Scan ${scan.code} was NOT saved`,
            err,
            retry: () => pump(),
            discard: () => queue.shift(),
          });
          return;
        }
        queue.shift();
        showAlert({ title: 'Scan refused', message: err.message, code: scan.code });
        refresh();
        return;
      }
      queue.shift();
      applyState(res);
      announceScan(res.scan, scan.code);
    }
  } finally {
    pumping = false;
  }
}

function announceScan(scan, code) {
  if (!scan) return;
  const line = lineById(scan.line_id);
  if (scan.result === 'ok' || scan.result === 'line_done') {
    lastScan = { lineId: scan.line_id, result: scan.result, code };
    Feedback.play(scan.result === 'ok' ? 'ok' : 'line');
    flash(scan.result === 'ok' ? 'ok' : 'line');
    render();
    return;
  }
  if (scan.result === 'order_done') {
    lastScan = { lineId: scan.line_id, result: scan.result, code };
    Feedback.play('done');
    flash('done');
    render();
    showDone(false);
    return;
  }
  if (scan.result === 'over') {
    showAlert({ title: 'Already complete', sound: 'over',
      message: `All ${line ? line.qty_required : ''} of ${line ? line.title : 'this item'} are already scanned. Set this one aside.`, code });
  } else if (scan.result === 'case_over') {
    showAlert({ title: 'Case is too big', sound: 'over',
      message: `This case counts ${scan.case_units} units, but only ${scan.left} of ${line ? line.title : 'this item'} are left. Scan single units instead.`, code });
  } else {
    showAlert({ title: 'Not on this order',
      message: `Set it aside — this barcode is not on ${current.order_name}.`, code });
  }
}

/* ---------- opening an order ---------- */

async function openOrder(number) {
  blocker = 'opening';
  startStatus.innerHTML = `<span class="spinner"></span> Opening ${esc(number)}…`;
  doneEl.hidden = true;
  let res;
  try {
    res = await withRetry(() => api('/api/verify/open', { method: 'POST', body: { number } }));
  } catch (err) {
    blocker = null;
    startStatus.innerHTML = '';
    if (isTransient(err) || err.status === 502) {
      showError({ title: `Could not open ${number}`, err, retry: () => openOrder(number) });
    } else {
      showAlert({ title: err.status === 404 ? 'Order not found' : 'Cannot verify this order', message: err.message, code: number });
    }
    if (!current || current.status !== 'in_progress') showStart();
    return;
  }
  blocker = null;
  startStatus.innerHTML = '';
  queue.length = 0;
  lastScan = null;
  banner = null;
  if (res.reopened && res.changes && res.changes.length) {
    banner = { tone: 'warn', html: `<b>This order changed after it was verified.</b> ${res.changes.map((c) => `${esc(c.title)}: ${c.from} → ${c.to}`).join(' · ')}. Verify the new items.` };
  } else if (res.changes && res.changes.length) {
    banner = { tone: 'info', html: `Order updated since it was started: ${res.changes.map((c) => `${esc(c.title)}: ${c.from} → ${c.to}`).join(' · ')}` };
  }
  applyState(res);
  Feedback.play(res.status === 'verified' ? 'done' : 'open');
  if (res.status === 'verified') showDone(true);
}

async function refresh() {
  if (!current) return;
  try {
    applyState(await api(`/api/verify/${current.id}`));
  } catch { /* the next scan reports problems */ }
}

async function resumeOpen() {
  let id = null;
  try { id = sessionStorage.getItem(OPEN_KEY); } catch { /* private mode */ }
  if (!id) { showStart(); return; }
  try {
    const res = await api(`/api/verify/${id}`);
    if (res.status === 'in_progress') { applyState(res); return; }
  } catch { /* gone or no access */ }
  showStart();
}

function applyState(res) {
  current = res;
  try { sessionStorage.setItem(OPEN_KEY, String(res.id)); } catch { /* private mode */ }
  startView.hidden = true;
  orderView.hidden = false;
  document.body.classList.add('vf-active');
  render();
  if (!doneEl.hidden && res.status === 'verified') renderDone(doneEl.dataset.already === '1');
}

function showStart() {
  current = null;
  lastScan = null;
  try { sessionStorage.removeItem(OPEN_KEY); } catch { /* private mode */ }
  orderView.hidden = true;
  doneEl.hidden = true;
  startView.hidden = false;
  document.body.classList.remove('vf-active');
  loadToday();
  scanner.focus();
}

/* ---------- rendering ---------- */

function lineState(l) {
  if (l.skip_reason) return 'skip';
  if (l.remaining === 0) return 'done';
  return l.qty_verified > 0 ? 'partial' : 'todo';
}

function render() {
  if (!current) return;
  const p = current.progress;
  document.getElementById('vf-order-name').textContent = current.order_name;
  document.getElementById('vf-order-sub').textContent = [current.source_name, current.customer].filter(Boolean).join(' · ');
  document.getElementById('vf-units-done').textContent = p.units_verified;
  document.getElementById('vf-units-total').textContent = ` / ${p.units_required} units`;
  document.getElementById('vf-lines-count').textContent = `${p.lines_done} of ${p.lines_total} item${p.lines_total === 1 ? '' : 's'} done`;
  document.getElementById('vf-bar').style.width = `${p.units_required ? Math.round((p.units_verified / p.units_required) * 100) : 0}%`;
  document.getElementById('vf-bar').parentElement.classList.toggle('complete', current.status === 'verified');

  const bannerEl = document.getElementById('vf-banner');
  const notes = [];
  if (banner) notes.push(banner);
  if (current.status === 'verified') {
    notes.push({ tone: 'ok', html: `Verified by <b>${esc(current.verified_by || '')}</b> · ${esc(time(current.verified_at))}${current.forced ? ' · some items marked without scanning' : ''}` });
  } else if (current.started_by && window.currentUser && current.started_by !== window.currentUser.username) {
    notes.push({ tone: 'info', html: `Started by <b>${esc(current.started_by)}</b> · ${esc(time(current.started_at))}` });
  }
  bannerEl.hidden = !notes.length;
  bannerEl.innerHTML = notes.map((n) => `<div class="vf-note ${n.tone}">${n.html}</div>`).join('');

  renderLast();

  const order = { partial: 0, todo: 1, done: 2, skip: 3 };
  const lines = [...current.lines].sort((a, b) => order[lineState(a)] - order[lineState(b)]);
  document.getElementById('vf-lines').innerHTML = lines.map((l) => {
    const state = lineState(l);
    const qty = l.skip_reason
      ? '<span class="vf-skip-tag">Skipped</span>'
      : `<b>${l.qty_verified}</b><span>/${l.qty_required}</span>`;
    const code = l.needs_tap ? '<span class="vf-tap-tag">No barcode — tap to confirm</span>'
      : l.skip_reason ? esc(l.skip_reason) : esc(l.barcode);
    return `<li class="vf-line ${state}${lastScan && lastScan.lineId === l.id ? ' last' : ''}" data-id="${l.id}" tabindex="-1">
      <div class="vf-line-main">
        <div class="vf-line-title">${esc(l.title || '(no name)')}</div>
        <div class="vf-line-code${l.needs_tap || l.skip_reason ? '' : ' mono'}">${code}${l.forced ? ' <span class="vf-forced-tag">marked</span>' : ''}</div>
      </div>
      <div class="vf-line-qty">${qty}</div>
    </li>`;
  }).join('');
}

function renderLast() {
  const el = document.getElementById('vf-last');
  const line = lastScan && lineById(lastScan.lineId);
  if (!line) { el.hidden = true; return; }
  el.hidden = false;
  const canCount = current.status === 'in_progress' && line.scanned && line.remaining > 0;
  el.className = `vf-last ${line.remaining === 0 ? 'done' : ''}`;
  el.innerHTML = `
    <div class="vf-last-label">Last scan</div>
    <div class="vf-last-title">${esc(line.title)}</div>
    <div class="vf-last-row">
      <div class="vf-last-qty"><b>${line.qty_verified}</b> of ${line.qty_required}${line.remaining === 0 ? ' ✓' : ''}</div>
      ${canCount ? `<div class="vf-last-actions">
        <button class="btn btn-outlined" data-act="count" data-id="${line.id}">Type count</button>
        <button class="btn btn-primary" data-act="check-all" data-id="${line.id}">Check all ${line.remaining}</button>
      </div>` : ''}
    </div>`;
}

document.getElementById('vf-last').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const line = lineById(Number(btn.dataset.id));
  if (btn.dataset.act === 'check-all') lineAction(line, 'check-all');
  else openCountSheet(line);
});

document.getElementById('vf-lines').addEventListener('click', (e) => {
  const li = e.target.closest('.vf-line');
  if (li) openLineSheet(lineById(Number(li.dataset.id)));
});

/* ---------- line actions ---------- */

async function lineAction(line, action, body = {}) {
  if (!line || !current) return;
  const verificationId = current.id;
  const id = clientId();
  try {
    const res = await withRetry(() => api(`/api/verify/${verificationId}/lines/${line.id}/${action}`, {
      method: 'POST', body: { ...body, client_id: id },
    }));
    closeSheet();
    lastScan = { lineId: line.id, result: 'ok', code: line.barcode };
    applyState(res);
    if (res.status === 'verified') {
      Feedback.play('done');
      flash('done');
      showDone(false);
    } else {
      Feedback.play('line');
      flash('line');
    }
  } catch (err) {
    if (isTransient(err)) {
      closeSheet();
      showError({ title: `Could not save ${line.title}`, err, retry: () => lineAction(line, action, body) });
    } else {
      showAlert({ title: 'Not saved', message: err.message, sound: 'over', tone: 'warn' });
    }
  }
}

function openSheet(html) {
  sheetEl.innerHTML = html;
  sheetBackdrop.hidden = false;
}

function closeSheet() {
  sheetBackdrop.hidden = true;
  sheetEl.innerHTML = '';
  scanner.focus();
}

sheetBackdrop.addEventListener('click', (e) => {
  if (e.target === sheetBackdrop) closeSheet();
});

function openLineSheet(line) {
  if (!line) return;
  const open = current.status === 'in_progress' && line.remaining > 0 && !line.skip_reason;
  const buttons = [];
  if (open && line.needs_tap) {
    buttons.push(`<button class="vf-sheet-btn primary" data-act="confirm">It's in the box — confirm ${line.qty_required}</button>`);
  } else if (open && line.scanned) {
    buttons.push(`<button class="vf-sheet-btn primary" data-act="check-all">Check all — ${line.remaining} left</button>`);
    buttons.push('<button class="vf-sheet-btn" data-act="count">Type the count</button>');
  } else if (open) {
    buttons.push('<div class="vf-sheet-note">Scan one unit of this item first — then you can count the rest at once.</div>');
  }
  if (open && current.can_override) {
    buttons.push('<button class="vf-sheet-btn danger" data-act="force">Mark verified without scanning</button>');
  }
  buttons.push('<button class="vf-sheet-btn" data-act="close">Close</button>');
  openSheet(`
    <div class="vf-sheet-title">${esc(line.title || '(no name)')}</div>
    <div class="vf-sheet-sub">${line.barcode ? `<span class="mono">${esc(line.barcode)}</span>` : ''}${line.sku ? ` · SKU ${esc(line.sku)}` : ''}</div>
    <div class="vf-sheet-qty">${line.skip_reason ? esc(line.skip_reason) : `<b>${line.qty_verified}</b> of ${line.qty_required} verified`}</div>
    <div class="vf-sheet-actions">${buttons.join('')}</div>`);
  sheetEl.querySelector('.vf-sheet-actions').addEventListener('click', (e) => {
    const act = e.target.closest('button') && e.target.closest('button').dataset.act;
    if (act === 'confirm') lineAction(line, 'confirm');
    else if (act === 'check-all') lineAction(line, 'check-all');
    else if (act === 'count') openCountSheet(line);
    else if (act === 'force') confirmSheet(`Mark ${line.title} verified without scanning?`, 'It is recorded under your name.', 'Mark verified', () => lineAction(line, 'force'));
    else if (act === 'close') closeSheet();
  });
}

function openCountSheet(line) {
  openSheet(`
    <div class="vf-sheet-title">How many ${esc(line.title)} in total?</div>
    <div class="vf-sheet-sub">${line.qty_required} ordered · ${line.qty_verified} counted so far</div>
    <form class="vf-sheet-form" id="vf-count-form">
      <input id="vf-count-input" type="number" inputmode="numeric" min="1" max="${line.qty_required}" step="1" value="${line.qty_required}" autocomplete="off">
      <button class="vf-sheet-btn primary" type="submit">Save count</button>
      <button class="vf-sheet-btn" type="button" id="vf-count-cancel">Cancel</button>
    </form>`);
  const input = document.getElementById('vf-count-input');
  input.focus();
  input.select();
  document.getElementById('vf-count-cancel').addEventListener('click', closeSheet);
  document.getElementById('vf-count-form').addEventListener('submit', (e) => {
    e.preventDefault();
    lineAction(line, 'count', { qty: Number(input.value) });
  });
}

function openTypeSheet() {
  const forOrder = !current || current.status !== 'in_progress';
  openSheet(`
    <div class="vf-sheet-title">${forOrder ? 'Order or quotation number' : 'Type a product barcode'}</div>
    <form class="vf-sheet-form" id="vf-type-form">
      <input id="vf-type-input" ${forOrder ? '' : 'inputmode="numeric"'} autocomplete="off" autocapitalize="characters" spellcheck="false">
      <button class="vf-sheet-btn primary" type="submit">${forOrder ? 'Open' : 'Add'}</button>
      <button class="vf-sheet-btn" type="button" id="vf-type-cancel">Cancel</button>
    </form>`);
  const input = document.getElementById('vf-type-input');
  input.focus();
  document.getElementById('vf-type-cancel').addEventListener('click', closeSheet);
  document.getElementById('vf-type-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const code = input.value.trim();
    if (!code) return;
    closeSheet();
    onScan(code);
  });
}

function confirmSheet(title, note, label, action) {
  openSheet(`
    <div class="vf-sheet-title">${esc(title)}</div>
    <div class="vf-sheet-sub">${esc(note)}</div>
    <div class="vf-sheet-actions">
      <button class="vf-sheet-btn danger" id="vf-confirm-yes">${esc(label)}</button>
      <button class="vf-sheet-btn" id="vf-confirm-no">Cancel</button>
    </div>`);
  document.getElementById('vf-confirm-no').addEventListener('click', closeSheet);
  document.getElementById('vf-confirm-yes').addEventListener('click', () => {
    closeSheet();
    action();
  });
}

/* ---------- order menu ---------- */

function openMenu() {
  const items = [];
  if (current.status === 'in_progress' && current.can_override) {
    items.push('<button class="vf-sheet-btn danger" data-act="force-order">Mark the whole order verified</button>');
  }
  items.push(`<button class="vf-sheet-btn" data-act="reset">${current.status === 'verified' ? 'Verify again from zero' : 'Start over from zero'}</button>`);
  items.push(`<button class="vf-sheet-btn" data-act="sound">${Feedback.muted() ? 'Turn sound on' : 'Turn sound off'}</button>`);
  items.push('<a class="vf-sheet-btn" href="/verifications.html">History</a>');
  items.push('<button class="vf-sheet-btn" data-act="close">Close</button>');
  openSheet(`<div class="vf-sheet-title">${esc(current.order_name)}</div><div class="vf-sheet-actions">${items.join('')}</div>`);
  sheetEl.querySelector('.vf-sheet-actions').addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === 'force-order') {
      confirmSheet('Mark every remaining item verified?', 'Nothing is scanned — it is recorded under your name.', 'Mark verified', forceOrder);
    } else if (act === 'reset') {
      confirmSheet('Start this order over?', 'All counts go back to zero. The old attempt stays in History.', 'Start over', resetOrder);
    } else if (act === 'sound') {
      Feedback.setMuted(!Feedback.muted());
      renderSoundToggle();
      closeSheet();
    } else {
      closeSheet();
    }
  });
}

async function forceOrder() {
  try {
    const res = await withRetry(() => api(`/api/verify/${current.id}/force`, { method: 'POST' }));
    applyState(res);
    Feedback.play('done');
    showDone(false);
  } catch (err) {
    if (isTransient(err)) showError({ title: 'Could not mark the order verified', err, retry: forceOrder });
    else showAlert({ title: 'Not saved', message: err.message, tone: 'warn', sound: 'over' });
  }
}

async function resetOrder() {
  const name = current.order_name;
  try {
    await withRetry(() => api(`/api/verify/${current.id}/reset`, { method: 'POST' }));
  } catch (err) {
    showError({ title: 'Could not start over', err, retry: resetOrder });
    return;
  }
  current = null;
  openOrder(name);
}

document.getElementById('vf-menu').addEventListener('click', openMenu);
document.getElementById('vf-close').addEventListener('click', showStart);
document.getElementById('vf-type').addEventListener('click', openTypeSheet);
document.getElementById('vf-type-order').addEventListener('click', openTypeSheet);

async function undo() {
  if (!current || current.status !== 'in_progress' || blocker) return;
  try {
    const res = await withRetry(() => api(`/api/verify/${current.id}/undo`, { method: 'POST' }));
    const line = res.lines.find((l) => l.id === res.undone.line_id);
    lastScan = line ? { lineId: line.id, result: 'undo', code: '' } : null;
    applyState(res);
    Feedback.play('undo');
    snackbar(`Undone: ${line ? line.title : 'last change'} −${res.undone.units}`);
  } catch (err) {
    if (isTransient(err)) showError({ title: 'Could not undo', err, retry: undo });
    else showAlert({ title: 'Nothing undone', message: err.message, tone: 'warn', sound: 'over' });
  }
}

document.getElementById('vf-undo').addEventListener('click', undo);

/* ---------- done ---------- */

function showDone(already) {
  doneEl.dataset.already = already ? '1' : '';
  renderDone(already);
  doneEl.hidden = false;
}

function renderDone(already) {
  const wb = current.writebacks || {};
  const chips = [];
  const chip = (state, label, error) => {
    if (state === 'done') return `<span class="chip static ok">✓ ${label}</span>`;
    if (state === 'failed') return `<span class="chip static err" title="${esc(error || '')}">✕ ${label} failed</span>`;
    if (state === 'pending') return `<span class="chip static">${label}…</span>`;
    return '';
  };
  if (current.source === 'shopify') chips.push(chip(wb.shopify.state, 'Shopify tagged “Checked”', wb.shopify.error));
  if (current.source === 'backoffice') chips.push(chip(wb.dbadmin.state, 'Checker saved in DB_Admin', wb.dbadmin.error));
  const failed = (wb.shopify && wb.shopify.state === 'failed') || (wb.dbadmin && wb.dbadmin.state === 'failed');
  const failure = [wb.shopify && wb.shopify.state === 'failed' ? wb.shopify.error : '',
    wb.dbadmin && wb.dbadmin.state === 'failed' ? wb.dbadmin.error : ''].filter(Boolean).join(' · ');
  const ship = current.source === 'shopify' && current.shopify_order_id
    ? `/ship.html?source=shopify&store_id=${current.shopify_store_id}&order_id=${encodeURIComponent(current.shopify_order_id)}&order_name=${encodeURIComponent(current.order_name)}`
    : '';
  doneEl.innerHTML = `
    <div class="vf-done-card">
      <div class="vf-done-check" aria-hidden="true">✓</div>
      <h2>${already ? 'Already verified' : 'Order verified'}</h2>
      <div class="vf-done-order">${esc(current.order_name)} · ${current.progress.units_required} units</div>
      <div class="vf-done-meta">by ${esc(current.verified_by || '')} · ${esc(time(current.verified_at))}${current.forced ? ' · some items marked without scanning' : ''}</div>
      <div class="vf-done-wb">${chips.join(' ')}</div>
      ${failed ? `<div class="vf-done-error">${esc(failure)} <button class="btn btn-text" id="vf-wb-retry">Retry</button></div>` : ''}
      <p class="vf-done-hint">Scan the next order</p>
      <div class="vf-done-actions">
        <button class="btn btn-primary vf-big" id="vf-next">Next order</button>
        ${already ? '<button class="btn btn-outlined vf-big" id="vf-again">Verify again</button>' : ''}
        ${ship ? `<a class="btn btn-outlined vf-big vf-desktop-only" href="${ship}">Ship now</a>` : ''}
      </div>
    </div>`;
  document.getElementById('vf-next').addEventListener('click', showStart);
  const again = document.getElementById('vf-again');
  if (again) again.addEventListener('click', () => confirmSheet('Verify this order again?', 'Counts start from zero. The earlier verification stays in History.', 'Verify again', resetOrder));
  const retry = document.getElementById('vf-wb-retry');
  if (retry) retry.addEventListener('click', retryWritebacks);
}

async function retryWritebacks() {
  try {
    applyState(await api(`/api/verify/${current.id}/writebacks/retry`, { method: 'POST' }));
  } catch (err) {
    showError({ title: 'Retry failed', err, retry: retryWritebacks });
  }
}

/* ---------- start screen ---------- */

async function loadToday() {
  const el = document.getElementById('vf-today');
  const today = new Date();
  const ymd = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  let data;
  try {
    const me = window.currentUser ? `&user_id=${window.currentUser.id}` : '';
    data = await api(`/api/verify/history?from=${ymd}${me}`);
  } catch {
    el.innerHTML = '';
    return;
  }
  const rows = data.rows.filter((r) => r.status !== 'reset').slice(0, 8);
  el.innerHTML = rows.length ? `
    <div class="vf-today-title">Today</div>
    ${rows.map((r) => `<button class="vf-today-row" data-number="${esc(r.order_name)}">
      <span class="vf-today-name">${esc(r.order_name)}</span>
      <span class="vf-today-meta">${esc(r.customer || '')}</span>
      <span class="vf-today-state ${r.status}">${r.status === 'verified' ? '✓ verified' : `${r.units_verified}/${r.units_required}`}</span>
    </button>`).join('')}` : '';
}

document.getElementById('vf-today').addEventListener('click', (e) => {
  const row = e.target.closest('.vf-today-row');
  if (row) onScan(row.dataset.number);
});

function renderSoundToggle() {
  document.getElementById('vf-sound-toggle').textContent = Feedback.muted() ? 'Sound is off — turn on' : 'Sound is on — turn off';
}

document.getElementById('vf-sound-toggle').addEventListener('click', () => {
  Feedback.setMuted(!Feedback.muted());
  renderSoundToggle();
  Feedback.play('ok');
});

/* ---------- camera ---------- */

async function openCamera() {
  Feedback.unlock();
  try {
    await CameraScan.open({ onCode: onScan, canScan: canScanNow });
  } catch (err) {
    CameraScan.close();
    showAlert({ title: 'Camera unavailable', message: err.message, tone: 'warn', sound: 'over' });
  }
}

document.getElementById('vf-camera').addEventListener('click', openCamera);
document.getElementById('vf-camera-order').addEventListener('click', openCamera);
document.getElementById('vf-camera-close').addEventListener('click', () => { CameraScan.close(); scanner.focus(); });

/* ---------- page-wide ---------- */

document.addEventListener('pointerdown', () => { Feedback.unlock(); Feedback.keepAwake(); });

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!sheetBackdrop.hidden) closeSheet();
    else if (blocker === 'alert') closeAlert();
    else if (CameraScan.isOpen()) CameraScan.close();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && sheetBackdrop.hidden) {
    e.preventDefault();
    undo();
  }
});

window.addEventListener('beforeunload', (e) => {
  if (queue.length || pumping) {
    e.preventDefault();
    e.returnValue = '';
  }
});
