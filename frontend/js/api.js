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

function money(v) {
  if (v == null || v === '') return '';
  return '$' + Number(v).toFixed(2);
}
