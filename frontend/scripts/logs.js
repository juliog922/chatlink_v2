(function () {
    const msgEl = document.getElementById('msg');
    const outEl = document.getElementById('out');
    const serviceEl = document.getElementById('service');
    const dayEl = document.getElementById('day');
    const patternEl = document.getElementById('pattern');
    const limitEl = document.getElementById('limit');
    const btnFetch = document.getElementById('btnFetch');
  
    if (!localStorage.getItem('AUTH_TOKEN')) {
      window.location.href = '/';
      return;
    }
  
    const token = localStorage.getItem('AUTH_TOKEN') || '';
    const setMsg = (t, err=true) => { msgEl.textContent = t || ''; msgEl.style.color = err ? '#e91e63' : '#0a0'; };
    const headers = () => ({ 'X-Auth': token });
  
    function todayISO() {
      const d = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    }
  
    async function fetchJSON(url) {
      const r = await fetch(url, { headers: headers() });
      if (r.status === 401) { localStorage.removeItem('AUTH_TOKEN'); location.href = '/'; throw new Error('401'); }
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      return r.json();
    }
  
    async function loadServices() {
      try {
        const data = await fetchJSON('/api/dlogs/services');
        const services = data?.services || [];
        serviceEl.innerHTML = '';
        if (!services.length) {
          const opt = document.createElement('option');
          opt.value = ''; opt.textContent = '(sin servicios)';
          serviceEl.appendChild(opt);
          return;
        }
        services.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s; opt.textContent = s;
          serviceEl.appendChild(opt);
        });
      } catch (e) {
        setMsg(String(e.message || e));
      }
    }
  
    async function runQuery() {
      setMsg('');
      outEl.textContent = 'Cargando…';
      const svc = serviceEl.value;
      const date = dayEl.value || todayISO();
      const pat = patternEl.value.trim();
      let lim = parseInt(limitEl.value || '1000', 10);
      if (!Number.isFinite(lim) || lim < 1) lim = 1000;
      if (!svc) { setMsg('Selecciona un servicio'); outEl.textContent = ''; return; }
  
      const params = new URLSearchParams({ service: svc, date, limit: String(lim) });
      if (pat) params.set('pattern', pat);
  
      try {
        const data = await fetchJSON(`/api/dlogs/view?${params.toString()}`);
        const lines = data?.lines || [];
        outEl.textContent = lines.join('\n');
        setMsg(`Resultados: ${lines.length}`, false);
      } catch (e) {
        outEl.textContent = '';
        setMsg(String(e.message || e));
      }
    }
  
    // init
    dayEl.value = todayISO();
    loadServices();
    btnFetch.addEventListener('click', runQuery);
    // enter en inputs
    [patternEl, limitEl].forEach(el => el.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); runQuery(); }
    }));
  })();
  