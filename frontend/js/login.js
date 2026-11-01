const REMEMBER_KEY = 'easyship-remember';
const rememberEl = document.getElementById('remember');
try {
  rememberEl.checked = localStorage.getItem(REMEMBER_KEY) !== '0';
} catch {
  // storage blocked: keep the default
}

// Chrome and Edge only offer to save a password after a real form post;
// a fetch sign-in has to hand the credential over itself.
async function offerToSavePassword(username, password) {
  if (!window.PasswordCredential || !navigator.credentials) return;
  try {
    await navigator.credentials.store(new PasswordCredential({ id: username, password, name: username }));
  } catch {
    // declined or unsupported: signing in still worked
  }
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('login-btn');
  const errEl = document.getElementById('login-error');
  errEl.style.display = 'none';
  btn.disabled = true;
  const username = document.getElementById('username').value;
  const password = document.getElementById('password').value;
  try {
    const me = await api('/api/auth/login', {
      method: 'POST',
      body: { username, password, remember: rememberEl.checked },
    });
    try {
      localStorage.setItem(REMEMBER_KEY, rememberEl.checked ? '1' : '0');
    } catch {
      // storage blocked: the choice just isn't remembered
    }
    await offerToSavePassword(username, password);
    location.href = me.home || '/index.html';
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
});
