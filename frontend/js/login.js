document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('login-btn');
  const errEl = document.getElementById('login-error');
  errEl.style.display = 'none';
  btn.disabled = true;
  try {
    const me = await api('/api/auth/login', {
      method: 'POST',
      body: {
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
      },
    });
    location.href = me.home || '/index.html';
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
});
