const tbody = document.querySelector('#users tbody');
const msgEl = document.getElementById('msg');

function setMsg(text, isError = true) {
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.style.color = isError ? '#c00' : '#0a0';
}

async function api(path, opts = {}) {
  const token = localStorage.getItem('AUTH_TOKEN') || '';
  const headers = new Headers(opts.headers || {});
  headers.set('X-Auth', token);
  if (opts.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const res = await fetch(path, { ...opts, headers });

  if (res.status === 401) {
    localStorage.removeItem('AUTH_TOKEN');
    window.location.href = '/';
    throw new Error('401: no autorizado');
  }

  if (!res.ok) {
    const t = await res.text().catch(() => '');
    const msg = t?.trim() || res.statusText || `HTTP ${res.status}`;
    throw new Error(`${res.status}: ${msg}`);
  }

  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

// Llamadas a la pasarela del whatsapp_bot (mismo host: /wabot)
async function wabot(path, opts = {}) {
  const token = localStorage.getItem('AUTH_TOKEN') || '';
  const headers = new Headers(opts.headers || {});
  headers.set('X-Auth', token); // reutilizamos tu auth
  if (opts.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const res = await fetch(`/wabot${path}`, { ...opts, headers });

  if (res.status === 401) {
    localStorage.removeItem('AUTH_TOKEN');
    window.location.href = '/';
    throw new Error('401: no autorizado');
  }
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    const msg = t?.trim() || res.statusText || `HTTP ${res.status}`;
    throw new Error(`${res.status}: ${msg}`);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}


async function refresh() {
  tbody.innerHTML = '';
  setMsg('');
  try {
    const users = await api('/api/users');
    users.forEach((u) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${u.id ?? ''}</td>
        <td>${u.phone}</td>
        <td>${u.email}</td>
        <td>${u.name ?? ''}</td>
        <td>${u.role}</td>
        <td class="actions">
          <button class="btn-qr" data-phone="${u.phone}">Enviar QR</button>
          <button class="btn-del" data-id="${u.id}">Eliminar</button>
        </td>
      `;

      // Botón Enviar QR
      tr.querySelector('.btn-qr').addEventListener('click', async (ev) => {
        const btn = ev.currentTarget;
        const phone = btn.getAttribute('data-phone');
        setMsg('');
        btn.disabled = true;
        const old = btn.textContent;
        btn.textContent = 'Enviando...';
        try {
          const resp = await wabot('/loginqr', {
            method: 'POST',
            body: JSON.stringify({ to: phone }),
          });
          if (resp?.status === 'sent') {
            setMsg(`QR enviado a ${resp.email}`, false);
          } else if (resp?.status === 'already_connected') {
            setMsg('Sesión ya conectada', false);
          } else {
            setMsg(`Respuesta inesperada: ${JSON.stringify(resp)}`);
          }
        } catch (e) {
          const msg = String(e?.message || e);
          if (msg.startsWith('404:')) {
            setMsg('No se encontró usuario en whatsapp_bot para ese teléfono');
          } else {
            setMsg(msg);
          }
        } finally {
          btn.disabled = false;
          btn.textContent = old;
        }
      });

      // Botón Eliminar
      tr.querySelector('.btn-del').addEventListener('click', async () => {
        try {
          await api(`/api/users/${u.id}`, { method: 'DELETE' });
          setMsg('Usuario eliminado', false);
          await refresh();
        } catch (e) { setMsg(e.message); }
      });

      tbody.appendChild(tr);

    });
  } catch (e) {
    setMsg(e.message || 'Error al listar usuarios');
  }
}

document.getElementById('createForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  setMsg('');
  const phone = document.getElementById('phone').value.trim();
  const email = document.getElementById('email').value.trim();
  const name  = document.getElementById('name').value.trim();
  const role  = document.getElementById('role').value; // viene del <select>

  if (!phone || !email || !role) {
    setMsg('phone, email y role son obligatorios');
    return;
  }

  try {
    await api('/api/users', {
      method: 'POST',
      body: JSON.stringify({ phone, email, name, role }),
    });
    e.target.reset();
    document.getElementById('role').value = 'user';
    setMsg('Usuario creado', false);
    await refresh();
  } catch (err) {
    if (String(err.message).startsWith('409:')) {
      setMsg(err.message.replace(/^409:\s*/, '') || 'Conflicto: phone/email');
    } else {
      setMsg(err.message || 'Error creando usuario');
    }
  }
});

if (!localStorage.getItem('AUTH_TOKEN')) {
  window.location.href = '/';
} else {
  refresh();
}
