async function api(path, options = {}) {
  const opts = {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  };
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && !location.pathname.endsWith('/login.html')) {
    location.href = '/login.html';
    throw new Error('Not authenticated');
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    // non-JSON response
  }
  if (!res.ok) {
    throw new Error((data && data.error) || `Request failed (${res.status})`);
  }
  return data;
}

let snackbarTimer = null;
function snackbar(message, type = '') {
  let el = document.getElementById('snackbar');
  if (!el) {
    el = document.createElement('div');
    el.id = 'snackbar';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = `show ${type}`;
  clearTimeout(snackbarTimer);
  snackbarTimer = setTimeout(() => { el.className = ''; }, 4500);
}

function esc(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

/* Inline printer icon — the 🖨 emoji has no glyph in the app font and renders
   as a fallback box on some systems. */
const ICON_PRINTER = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
  + '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
  + '<path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6"/>'
  + '<rect x="6" y="14" width="12" height="8" rx="1"/></svg>';

function money(v) {
  if (v == null || v === '') return '';
  return '$' + Number(v).toFixed(2);
}

// Print a PDF (often multi-page — one page per box on a multi-box shipment)
// through the browser print dialog, printing EVERY page. Pointing an iframe's
// src at the label URL and calling contentWindow.print() only prints the first
// page: the request fires while Chrome's PDF plugin is still streaming/paginating
// the document. Loading the fully-fetched bytes as a Blob into a dedicated hidden
// iframe and printing on load hands the whole document to the plugin up front.
async function printPdfUrl(url) {
  let blobUrl;
  try {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`Could not load label (HTTP ${res.status})`);
    blobUrl = URL.createObjectURL(await res.blob());
  } catch (err) {
    window.open(url, '_blank'); // last resort: open the label so it can be printed manually
    throw err;
  }
  const frame = document.createElement('iframe');
  frame.setAttribute('aria-hidden', 'true');
  // visibility:hidden (not display:none) — a display:none iframe won't print.
  frame.style.cssText =
    'position:fixed;right:0;bottom:0;width:1px;height:1px;border:0;visibility:hidden';
  frame.src = blobUrl;
  frame.addEventListener('load', () => {
    try {
      frame.contentWindow.focus();
      frame.contentWindow.print();
    } catch {
      window.open(blobUrl, '_blank');
    }
    // Keep the frame/blob alive while the modal dialog is open, then clean up.
    setTimeout(() => {
      frame.remove();
      URL.revokeObjectURL(blobUrl);
    }, 60000);
  }, { once: true });
  document.body.appendChild(frame);
}
