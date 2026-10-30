/* Verify page: scan an order's barcode (Shopify packing slip or BackOffice
   quotation), then scan every product in it. Each scan is saved at once —
   one at a time, in order, retried on network trouble — and answered with a
   tone, a vibration and a color: green flash for a good scan, a red screen
   that must be tapped away for a wrong one. */
const OPEN_KEY = 'easyship.verifyOpen';
const RETRY_DELAYS_MS = [600, 1500];
const AUTO_NEXT_MS = 3000;
const svg = (paths) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
const ICON_X = svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>');
const ICON_BANG = svg('<path d="M12 6v8"/><path d="M12 18.5v.01"/>');
const ICON_CHECK = svg('<path d="m5 12.5 4.5 4.5L19 7.5"/>');
const ICON_SCAN = svg('<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2M8 8v8M12 8v8M16 8v8"/>');
const ICON_TAP = svg('<path d="M9 11V5.5a1.5 1.5 0 0 1 3 0V10"/><path d="M12 10V9a1.5 1.5 0 0 1 3 0v2"/><path d="M15 11a1.5 1.5 0 0 1 3 0v3a6 6 0 0 1-6 6h-.5a6 6 0 0 1-5-2.7L4.2 14a1.5 1.5 0 0 1 2.5-1.7L9 15"/>');
const ICON_SOUND_ON = svg('<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/>');
const ICON_SOUND_OFF = svg('<path d="M11 5 6 9H2v6h4l5 4z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>');

const startView = document.getElementById('vf-start');
const orderView = document.getElementById('vf-order');
const startStatus = document.getElementById('vf-start-status');
const alertEl = document.getElementById('vf-alert');
const doneEl = document.getElementById('vf-done');
const sheetBackdrop = document.getElementById('vf-sheet-backdrop');
const sheetEl = document.getElementById('vf-sheet');

const readyEl = document.getElementById('vf-ready');

let current = null; // the open verification, as the API returns it
let lastScan = null; // { lineId, units, caseUnits, kind, seq }
let scanSeq = 0;
let blocker = null; // 'alert' | 'error' | 'opening' — scans are refused until it clears
let openingNumber = '';
let banner = null;
const openGroups = new Set(); // folded line groups the packer opened
const queue = [];
let pumping = false;
let autoNext = null; // { endsAt, timer } while the done screen counts down to the next order

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

function clock(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
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

function replay(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth; // restart the animation
  el.classList.add(cls);
}

/* Whether a scan right now would count: the packer should never have to
   guess if the phone is listening. */
function readyState() {
  if (blocker === 'opening') return ['busy', `Opening ${openingNumber}…`];
  if (queue.length || pumping) return ['busy', queue.length > 1 ? `Saving ${queue.length}…` : 'Saving…'];
  if (blocker || !sheetBackdrop.hidden) return ['paused', 'Paused'];
  if (!document.hasFocus()) return ['paused', 'Tap to scan'];
  return ['ready', 'Ready'];
}

function renderReady() {
  const [state, label] = readyState();
  const pill = (text) => `<span class="vf-ready ${state}"><i aria-hidden="true"></i>${esc(text)}</span>`;
  readyEl.className = `vf-ready ${state}`;
  readyEl.innerHTML = `<i aria-hidden="true"></i>${esc(label)}`;
  startStatus.innerHTML = pill(state === 'ready' ? 'Ready — scan now' : label);
}

/* A scan that arrived while something needs a tap first: shake it so the
   packer sees why nothing was counted. */
function nudge() {
  if (!alertEl.hidden) {
    replay(alertEl.querySelector('.vf-alert-card'), 'vf-nudge');
    const hint = alertEl.querySelector('.vf-alert-hint');
    if (hint) hint.textContent = 'That scan was not counted — tap OK first';
  } else if (!sheetBackdrop.hidden) {
    replay(sheetEl, 'vf-nudge');
  }
}

function deltaLabel(s) {
  if (!s || !s.units) return '';
  if (s.kind === 'undo') return `−${s.units} undone`;
  if (s.caseUnits) return `+${s.units} · case`;
  return `+${s.units}`;
}

// The last digits are what a packer compares against the shelf label.
function codeHtml(code) {
  const s = String(code);
  return s.length > 6 ? `${esc(s.slice(0, -4))}<b>${esc(s.slice(-4))}</b>` : `<b>${esc(s)}</b>`;
}

/* ---------- blocking alert (wrong scan, refusals) ---------- */

function showAlert({ title, message, code = '', sound = 'wrong', tone = 'error' }) {
  blocker = 'alert';
  Feedback.play(sound);
  alertEl.className = `vf-alert ${tone}`;
  alertEl.innerHTML = `
    <div class="vf-alert-card">
      <div class="vf-alert-icon" aria-hidden="true">${tone === 'error' ? ICON_X : ICON_BANG}</div>
      <h2>${esc(title)}</h2>
      <p>${esc(message)}</p>
      ${code ? `<div class="vf-alert-code mono">${esc(code)}</div>` : ''}
      <button class="vf-alert-ok" id="vf-alert-ok">OK</button>
      <div class="vf-alert-hint">Tap OK to keep scanning</div>
    </div>`;
  alertEl.hidden = false;
  document.getElementById('vf-alert-ok').addEventListener('click', closeAlert);
  renderReady();
}

function closeAlert() {
  alertEl.hidden = true;
  if (blocker === 'alert' || blocker === 'error') blocker = null;
  scanner.focus();
  renderReady();
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
      <div class="vf-alert-icon" aria-hidden="true">${ICON_BANG}</div>
      <h2>${esc(title)}</h2>
      <pre class="vf-error-text" id="vf-error-text">${esc(text)}</pre>
      <div class="vf-error-actions">
        <button class="vf-alert-ok" id="vf-error-retry">Retry</button>
        <button class="vf-alert-secondary" id="vf-error-copy">Copy</button>
        ${discard ? '<button class="vf-alert-secondary" id="vf-error-discard">Skip this scan</button>' : '<button class="vf-alert-secondary" id="vf-error-close">Close</button>'}
      </div>
    </div>`;
  alertEl.hidden = false;
  renderReady();
  document.getElementById('vf-error-retry').addEventListener('click', () => {
    alertEl.hidden = true;
    blocker = null;
    renderReady();
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
    nudge();
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
      renderReady();
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
    renderReady();
  }
}

function announceScan(scan, code) {
  if (!scan) return;
  const line = lineById(scan.line_id);
  const counted = { lineId: scan.line_id, units: scan.units, caseUnits: scan.case_units, kind: 'scan', seq: ++scanSeq };
  if (scan.result === 'ok' || scan.result === 'line_done') {
    lastScan = counted;
    Feedback.play(scan.result === 'ok' ? 'ok' : 'line');
    flash(scan.result === 'ok' ? 'ok' : 'line');
    render();
    return;
  }
  if (scan.result === 'order_done') {
    lastScan = counted;
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
  stopAutoNext();
  blocker = 'opening';
  openingNumber = number;
  renderReady();
  doneEl.hidden = true;
  let res;
  try {
    res = await withRetry(() => api('/api/verify/open', { method: 'POST', body: { number } }));
  } catch (err) {
    blocker = null;
    renderReady();
    if (isTransient(err) || err.status === 502) {
      showError({ title: `Could not open ${number}`, err, retry: () => openOrder(number) });
    } else {
      showAlert({ title: err.status === 404 ? 'Order not found' : 'Cannot verify this order', message: err.message, code: number });
    }
    if (!current || current.status !== 'in_progress') showStart();
    return;
  }
  blocker = null;
  queue.length = 0;
  lastScan = null;
  banner = null;
  openGroups.clear();
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
  renderReady();
  if (!doneEl.hidden && res.status === 'verified') renderDone(doneEl.dataset.already === '1');
}

function showStart() {
  stopAutoNext();
  current = null;
  lastScan = null;
  try { sessionStorage.removeItem(OPEN_KEY); } catch { /* private mode */ }
  orderView.hidden = true;
  doneEl.hidden = true;
  startView.hidden = false;
  document.body.classList.remove('vf-active');
  loadToday();
  scanner.focus();
  renderReady();
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
  const left = p.units_required - p.units_verified;
  const verified = current.status === 'verified';
  document.getElementById('vf-order-name').textContent = current.order_name;
  document.getElementById('vf-order-sub').textContent = [current.source_name, current.customer].filter(Boolean).join(' · ');
  document.getElementById('vf-left').textContent = verified ? '✓' : left;
  document.getElementById('vf-left-label').textContent = verified ? 'verified' : `unit${left === 1 ? '' : 's'} left`;
  document.getElementById('vf-units-done').textContent = p.units_verified;
  document.getElementById('vf-units-total').textContent = ` / ${p.units_required} units`;
  document.getElementById('vf-lines-count').textContent = `${p.lines_done} of ${p.lines_total} item${p.lines_total === 1 ? '' : 's'} done`;
  document.getElementById('vf-bar').style.width = `${p.units_required ? Math.round((p.units_verified / p.units_required) * 100) : 0}%`;
  document.querySelector('.vf-progress').classList.toggle('complete', verified);

  const bannerEl = document.getElementById('vf-banner');
  const notes = [];
  if (banner) notes.push(banner);
  if (verified) {
    notes.push({ tone: 'ok', html: `Verified by <b>${esc(current.verified_by || '')}</b> · ${esc(time(current.verified_at))}${current.forced ? ' · some items marked without scanning' : ''}` });
  } else if (current.started_by && window.currentUser && current.started_by !== window.currentUser.username) {
    notes.push({ tone: 'info', html: `Started by <b>${esc(current.started_by)}</b> · ${esc(time(current.started_at))}` });
  }
  bannerEl.hidden = !notes.length;
  bannerEl.innerHTML = notes.map((n) => `<div class="vf-note ${n.tone}">${n.html}</div>`).join('');

  renderLast();
  renderLines();
}

function lineHtml(l) {
  const state = lineState(l);
  const pct = l.qty_required ? Math.min(100, Math.round((l.qty_verified / l.qty_required) * 100)) : 0;
  const qty = l.skip_reason
    ? '<span class="vf-skip-tag">Skipped</span>'
    : `<b>${l.qty_verified}</b><span>/${l.qty_required}</span>`;
  let code;
  if (l.skip_reason) code = esc(l.skip_reason);
  else if (l.needs_tap && state !== 'done') code = '<span class="vf-tap-tag">No barcode · tap to confirm</span>';
  else if (l.barcode) code = `<span class="mono">${codeHtml(l.barcode)}</span>`;
  else code = 'No barcode';
  const icon = state === 'done' ? ICON_CHECK : l.needs_tap && state !== 'skip' ? ICON_TAP : '';
  return `<li class="vf-line ${state}${l.needs_tap ? ' tap' : ''}${lastScan && lastScan.lineId === l.id ? ' last' : ''}" data-id="${l.id}" style="--pct:${pct}%">
    <span class="vf-line-icon" aria-hidden="true">${icon}</span>
    <div class="vf-line-main">
      <div class="vf-line-title">${esc(l.title || '(no name)')}</div>
      <div class="vf-line-code">${code}${l.forced ? ' <span class="vf-forced-tag">marked</span>' : ''}</div>
    </div>
    <div class="vf-line-qty">${qty}</div>
  </li>`;
}

/* To scan on top (started lines first), then the done and the never-scanned
   lines folded away so the list stays short while packing. */
function renderLines() {
  const groups = { todo: [], done: [], skip: [] };
  current.lines.forEach((l) => {
    const state = lineState(l);
    groups[state === 'partial' || state === 'todo' ? 'todo' : state].push(l);
  });
  groups.todo.sort((a, b) => (b.qty_verified > 0) - (a.qty_verified > 0));
  const isOpen = (key) => openGroups.has(key) || (key === 'done' && !groups.todo.length);
  const fold = (key, label, items) => (items.length ? `
    <details class="vf-group" data-group="${key}"${isOpen(key) ? ' open' : ''}>
      <summary>${label}<span class="vf-group-count">${items.length}</span></summary>
      <ul class="vf-lines">${items.map(lineHtml).join('')}</ul>
    </details>` : '');
  document.getElementById('vf-lines').innerHTML = [
    groups.todo.length ? `<div class="vf-group-title">To scan<span class="vf-group-count">${groups.todo.length}</span></div>
      <ul class="vf-lines">${groups.todo.map(lineHtml).join('')}</ul>` : '',
    fold('done', 'Done', groups.done),
    fold('skip', 'Not scanned', groups.skip),
  ].join('');
}

document.getElementById('vf-lines').addEventListener('toggle', (e) => {
  const key = e.target.dataset && e.target.dataset.group;
  if (!key) return;
  if (e.target.open) openGroups.add(key);
  else openGroups.delete(key);
}, true);

function renderLast() {
  const el = document.getElementById('vf-last');
  const line = lastScan && lineById(lastScan.lineId);
  if (!line) {
    const open = current.status === 'in_progress' ? current.lines.filter((l) => l.remaining > 0 && !l.skip_reason) : [];
    el.hidden = !open.length;
    if (!open.length) return;
    const onlyTaps = open.every((l) => l.needs_tap);
    el.className = 'vf-last prompt';
    el.dataset.seq = '';
    el.innerHTML = `
      <span class="vf-last-prompt-icon" aria-hidden="true">${onlyTaps ? ICON_TAP : ICON_SCAN}</span>
      <div>
        <div class="vf-last-title">${onlyTaps ? 'Tap the items that have no barcode' : 'Scan any item from the order'}</div>
        <div class="vf-last-hint">${onlyTaps ? 'Confirm each one once it is in the box' : 'Each scan counts one unit · a case barcode counts the whole case'}</div>
      </div>`;
    return;
  }
  el.hidden = false;
  const canCount = current.status === 'in_progress' && line.scanned && line.remaining > 0;
  const delta = deltaLabel(lastScan);
  el.className = `vf-last${line.remaining === 0 ? ' done' : ''}${lastScan.kind === 'undo' ? ' undo' : ''}`;
  el.innerHTML = `
    <div class="vf-last-top">
      <span class="vf-last-label">${lastScan.kind === 'undo' ? 'Undone' : 'Last scan'}</span>
      ${delta ? `<span class="vf-delta">${esc(delta)}</span>` : ''}
    </div>
    <div class="vf-last-title">${esc(line.title)}</div>
    ${line.barcode ? `<div class="vf-last-code mono">${codeHtml(line.barcode)}</div>` : ''}
    <div class="vf-last-row">
      <div class="vf-last-qty"><b>${line.qty_verified}</b> of ${line.qty_required}
        <span class="vf-last-left">${line.remaining === 0 ? '✓ complete' : `${line.remaining} left`}</span></div>
      ${canCount ? `<div class="vf-last-actions">
        <button class="btn btn-outlined" data-act="count" data-id="${line.id}">Type count</button>
        <button class="btn btn-primary" data-act="check-all" data-id="${line.id}">Check all ${line.remaining}</button>
      </div>` : ''}
    </div>`;
  if (el.dataset.seq !== String(lastScan.seq)) {
    el.dataset.seq = String(lastScan.seq);
    replay(el, 'pop');
  }
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
    const after = res.lines.find((l) => l.id === line.id);
    lastScan = { lineId: line.id, units: after ? after.qty_verified - line.qty_verified : 0, kind: action, seq: ++scanSeq };
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
  renderReady();
}

function closeSheet() {
  sheetBackdrop.hidden = true;
  sheetEl.innerHTML = '';
  scanner.focus();
  renderReady();
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
    <div class="vf-sheet-sub">${[line.barcode ? `<span class="mono">${codeHtml(line.barcode)}</span>` : '', line.sku ? `SKU ${esc(line.sku)}` : ''].filter(Boolean).join(' · ')}</div>
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
    lastScan = line ? { lineId: line.id, units: res.undone.units, kind: 'undo', seq: ++scanSeq } : null;
    applyState(res);
    Feedback.play('undo');
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
  if (!already && !writebackFailed()) startAutoNext();
}

function writebackFailed() {
  const wb = current.writebacks || {};
  return (wb.shopify && wb.shopify.state === 'failed') || (wb.dbadmin && wb.dbadmin.state === 'failed');
}

function startAutoNext() {
  stopAutoNext();
  autoNext = { endsAt: Date.now() + AUTO_NEXT_MS, timer: setInterval(renderAutoNext, 200) };
  renderAutoNext();
}

function stopAutoNext() {
  if (!autoNext) return;
  clearInterval(autoNext.timer);
  autoNext = null;
  renderAutoNext();
}

function renderAutoNext() {
  const next = document.getElementById('vf-next');
  if (!next) return;
  const label = next.querySelector('.vf-next-label');
  const fill = next.querySelector('.vf-next-fill');
  const stay = document.getElementById('vf-stay');
  if (!autoNext) {
    label.textContent = 'Next order';
    fill.hidden = true;
    stay.hidden = true;
    return;
  }
  const left = autoNext.endsAt - Date.now();
  if (left <= 0) {
    showStart();
    return;
  }
  label.textContent = `Next order in ${Math.ceil(left / 1000)}`;
  if (fill.hidden) {
    fill.style.animationDuration = `${AUTO_NEXT_MS}ms`;
    fill.style.animationDelay = `${left - AUTO_NEXT_MS}ms`;
    fill.hidden = false;
  }
  stay.hidden = false;
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
  const failed = writebackFailed();
  const failure = [wb.shopify && wb.shopify.state === 'failed' ? wb.shopify.error : '',
    wb.dbadmin && wb.dbadmin.state === 'failed' ? wb.dbadmin.error : ''].filter(Boolean).join(' · ');
  const ship = current.source === 'shopify' && current.shopify_order_id && window.currentUser && window.currentUser.ships
    ? `/ship.html?source=shopify&store_id=${current.shopify_store_id}&order_id=${encodeURIComponent(current.shopify_order_id)}&order_name=${encodeURIComponent(current.order_name)}`
    : '';
  doneEl.innerHTML = `
    <div class="vf-done-card">
      <div class="vf-done-check" aria-hidden="true">${ICON_CHECK}</div>
      <h2>${already ? 'Already verified' : 'Order verified'}</h2>
      <div class="vf-done-order">${esc(current.order_name)}</div>
      <div class="vf-done-stats">${current.progress.units_required} unit${current.progress.units_required === 1 ? '' : 's'} · ${current.progress.lines_total} item${current.progress.lines_total === 1 ? '' : 's'}</div>
      <div class="vf-done-meta">by ${esc(current.verified_by || '')} · ${esc(time(current.verified_at))}${current.forced ? ' · some items marked without scanning' : ''}</div>
      <div class="vf-done-wb">${chips.join(' ')}</div>
      ${failed ? `<div class="vf-done-error">${esc(failure)} <button class="btn btn-text" id="vf-wb-retry">Retry</button></div>` : ''}
      <p class="vf-done-hint"><span class="vf-ready ready"><i aria-hidden="true"></i>Scan the next order</span></p>
      <div class="vf-done-actions">
        <button class="btn btn-primary vf-big vf-next" id="vf-next"><span class="vf-next-fill" hidden></span><span class="vf-next-label">Next order</span></button>
        ${already ? '<button class="btn btn-outlined vf-big" id="vf-again">Verify again</button>' : ''}
        ${ship ? `<a class="btn btn-outlined vf-big vf-desktop-only" href="${ship}">Ship now</a>` : ''}
      </div>
      <button class="btn btn-text vf-stay" id="vf-stay" hidden>Stay on this screen</button>
    </div>`;
  if (failed) stopAutoNext();
  renderAutoNext();
  document.getElementById('vf-next').addEventListener('click', showStart);
  document.getElementById('vf-stay').addEventListener('click', stopAutoNext);
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
  const rows = data.rows.filter((r) => r.status !== 'reset');
  const open = rows.filter((r) => r.status === 'in_progress').slice(0, 5);
  const done = rows.filter((r) => r.status === 'verified');
  const row = (r) => {
    const pct = r.units_required ? Math.round((r.units_verified / r.units_required) * 100) : 0;
    return `<button class="vf-today-row ${r.status}" data-number="${esc(r.order_name)}">
      <span class="vf-today-name">${esc(r.order_name)}</span>
      <span class="vf-today-meta">${esc(r.customer || '')}</span>
      <span class="vf-today-state ${r.status}">${r.status === 'verified' ? `${ICON_CHECK}<span>${esc(clock(r.verified_at))}</span>` : `${r.units_verified}/${r.units_required}`}</span>
      ${r.status === 'in_progress' ? `<span class="vf-today-bar" style="--pct:${pct}%"></span>` : ''}
    </button>`;
  };
  el.innerHTML = [
    open.length ? `<div class="vf-today-title">Not finished<span class="vf-group-count">${open.length}</span></div>${open.map(row).join('')}` : '',
    done.length ? `<div class="vf-today-title">Verified today<span class="vf-group-count">${done.length}</span></div>${done.slice(0, 6).map(row).join('')}` : '',
  ].join('');
}

document.getElementById('vf-today').addEventListener('click', (e) => {
  const row = e.target.closest('.vf-today-row');
  if (row) onScan(row.dataset.number);
});

function renderSoundToggle() {
  const btn = document.getElementById('vf-sound-toggle');
  btn.innerHTML = Feedback.muted() ? `${ICON_SOUND_OFF}Sound off` : `${ICON_SOUND_ON}Sound on`;
  btn.classList.toggle('off', Feedback.muted());
  btn.setAttribute('aria-pressed', String(!Feedback.muted()));
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
window.addEventListener('focus', renderReady);
window.addEventListener('blur', renderReady);

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
