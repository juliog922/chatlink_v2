const form = document.getElementById('loginForm');
const msgEl = document.getElementById('msg');

function setMsg(text, isError = true) {
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.style.color = isError ? '#c00' : '#0a0';
}

form?.addEventListener('submit', async (e) => {
  e.preventDefault();
  setMsg('');

  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value.trim();

  if (!username || !password) {
    setMsg('Usuario y contraseña son obligatorios');
    return;
  }

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type':'application/json' },
      body: JSON.stringify({ username, password }),
    });

    if (!res.ok) {
      setMsg('Credenciales inválidas');
      return;
    }

    const data = await res.json().catch(() => ({}));
    if (!data?.token) {
      setMsg('Respuesta de login inválida');
      return;
    }

    localStorage.setItem('AUTH_TOKEN', data.token);
    setMsg('Autenticado', false);
    window.location.href = '/users';
  } catch {
    setMsg('Error de red');
  }
});
