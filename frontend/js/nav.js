async function initNav(activePage) {
  const el = document.getElementById('appbar');
  el.className = 'appbar';
  el.innerHTML = `
    <div class="brand">📦 EasyShip</div>
    <nav>
      <a href="/index.html" data-page="scan">Scan</a>
      <a href="/orders.html" data-page="orders">Orders</a>
      <a href="/parcels.html" data-page="parcels">Parcels</a>
      <a href="/settings.html" data-page="settings">Settings</a>
    </nav>
    <div class="user-area">
      <span id="mode-badge"></span>
      <span id="nav-username"></span>
      <button class="btn btn-text btn-small" id="logout-btn">Logout</button>
    </div>`;
  const link = el.querySelector(`[data-page="${activePage}"]`);
  if (link) link.classList.add('active');
  el.querySelector('#logout-btn').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' });
    location.href = '/login.html';
  });
  try {
    const me = await api('/api/auth/me');
    el.querySelector('#nav-username').textContent = me.username;
    window.currentUser = me;
  } catch {
    return; // api() already redirected to login
  }
  try {
    const mode = await api('/api/settings/easyship-mode');
    const badge = el.querySelector('#mode-badge');
    if (mode.mode === 'sandbox') {
      badge.className = 'badge-sandbox';
      badge.textContent = 'SANDBOX';
    } else {
      badge.className = 'badge-production';
      badge.textContent = 'PRODUCTION';
    }
  } catch {
    // mode endpoint unavailable — leave badge empty
  }
}
