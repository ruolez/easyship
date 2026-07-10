const NAV_ICONS = {
  logo: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>',
  scan: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M8 7v10"/><path d="M12 7v10"/><path d="M16 7v10"/></svg>',
  orders: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/></svg>',
  parcels: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 9.4 7.55 4.24"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.29 7 12 12 20.71 7"/><line x1="12" x2="12" y1="22" y2="12"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>',
  logout: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" x2="9" y1="12" y2="12"/></svg>',
  menu: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="18" y2="18"/></svg>',
};

const NAV_ITEMS = [
  { page: 'scan', href: '/index.html', label: 'Scan', icon: 'scan' },
  { page: 'orders', href: '/orders.html', label: 'Orders', icon: 'orders' },
  { page: 'parcels', href: '/parcels.html', label: 'Parcels', icon: 'parcels' },
  { page: 'settings', href: '/settings.html', label: 'Settings', icon: 'settings' },
];

async function initNav(activePage) {
  const mount = document.getElementById('appbar');
  document.body.classList.add('with-sidebar');
  const active = NAV_ITEMS.find((i) => i.page === activePage);

  mount.outerHTML = `
    <div class="mobile-bar">
      <button class="hamburger" id="nav-hamburger" aria-label="Menu">${NAV_ICONS.menu}</button>
      <span class="title">${active ? active.label : 'EasyShip'}</span>
    </div>
    <div class="sidebar-backdrop" id="sidebar-backdrop"></div>
    <aside class="sidebar" id="sidebar">
      <div class="brand"><span class="mark">${NAV_ICONS.logo}</span><span class="label">EasyShip</span></div>
      <nav>
        ${NAV_ITEMS.map((i) => `
          <a href="${i.href}" title="${i.label}" class="${i.page === activePage ? 'active' : ''}">
            ${NAV_ICONS[i.icon]}<span class="label">${i.label}</span>
          </a>`).join('')}
      </nav>
      <div class="spacer"></div>
      <div class="foot">
        <span id="mode-badge"></span>
        <div class="user-line">
          <span class="avatar" id="nav-avatar"></span>
          <span class="name" id="nav-username"></span>
          <button id="logout-btn" title="Logout">${NAV_ICONS.logout}</button>
        </div>
      </div>
    </aside>`;

  const hamburger = document.getElementById('nav-hamburger');
  const backdrop = document.getElementById('sidebar-backdrop');
  hamburger.addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
  backdrop.addEventListener('click', () => document.body.classList.remove('sidebar-open'));

  document.getElementById('logout-btn').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' });
    location.href = '/login.html';
  });

  try {
    const me = await api('/api/auth/me');
    document.getElementById('nav-username').textContent = me.username;
    document.getElementById('nav-avatar').textContent = me.username.slice(0, 2);
    window.currentUser = me;
  } catch {
    return; // api() already redirected to login
  }
  try {
    const mode = await api('/api/settings/easyship-mode');
    const badge = document.getElementById('mode-badge');
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
