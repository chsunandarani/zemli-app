// Admin login box shared by the admin panel and the disease review desk.
// zemliAdminLogin() shows the box and resolves with a login token (saved in localStorage 'zemli_api_key').
(function () {
  const KEY = 'zemli_api_key';
  const css = `
  #zl{position:fixed;inset:0;z-index:9999;display:grid;place-items:center;padding:16px;background:rgba(3,10,6,.92);backdrop-filter:blur(6px);font-family:inherit}
  #zl form{width:100%;max-width:360px;background:#0d1d14;border:1px solid rgba(94,224,127,.35);border-radius:18px;padding:26px;box-shadow:0 0 40px rgba(94,224,127,.15);color:#E8F5EC}
  #zl img{width:58px;height:58px;border-radius:50%;background:#fff;display:block;margin:0 auto 10px}
  #zl h2{margin:0 0 4px;text-align:center;font-size:20px}#zl p{margin:0 0 18px;text-align:center;color:#9DB8A6;font-size:13px}
  #zl label{display:block;font-size:12px;color:#9DB8A6;margin:10px 0 4px}
  #zl input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.14);background:#07110C;color:#E8F5EC;font-size:15px}
  #zl input:focus{outline:none;border-color:#5EE07F}
  #zl button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:10px;background:#5EE07F;color:#07110C;font-weight:700;font-size:15px;cursor:pointer}
  #zl button:disabled{opacity:.6}#zl .e{color:#FF6A55;font-size:13px;min-height:18px;margin-top:10px;text-align:center}
  #zl .k{display:block;margin-top:14px;text-align:center;color:#9DB8A6;font-size:12px;cursor:pointer;text-decoration:underline}`;
  let pending = null;
  window.zemliAdminToken = () => { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } };
  window.zemliAdminLogout = () => { try { localStorage.removeItem(KEY); } catch (e) {} location.reload(); };
  window.zemliAdminLogin = function (msg) {
    if (pending) return pending;
    pending = new Promise(resolve => {
      if (!document.getElementById('zlcss')) { const s = document.createElement('style'); s.id = 'zlcss'; s.textContent = css; document.head.appendChild(s); }
      const box = document.createElement('div'); box.id = 'zl';
      box.innerHTML = `<form autocomplete="on"><img src="/static/logo-mark.png" alt=""><h2>Zemli admin</h2>
        <p>Only Zemli admins can open this page.</p>
        <label for="zlPhone">Phone number</label><input id="zlPhone" name="username" inputmode="numeric" autocomplete="username" required>
        <label for="zlPw">Password</label><input id="zlPw" name="password" type="password" autocomplete="current-password" required>
        <button type="submit">Log in</button><div class="e">${msg || ''}</div>
        <span class="k" id="zlKey">Use the master key instead</span></form>`;
      document.body.appendChild(box);
      const f = box.querySelector('form'), err = box.querySelector('.e'), btn = box.querySelector('button');
      const done = tok => { try { localStorage.setItem(KEY, tok); } catch (e) {} box.remove(); pending = null; resolve(tok); };
      box.querySelector('#zlPhone').focus();
      box.querySelector('#zlKey').onclick = () => { const k = prompt('Master key (ZEMLI_API_KEY from Render)'); if (k) done(k.trim()); };
      f.onsubmit = async ev => {
        ev.preventDefault(); btn.disabled = true; err.textContent = '';
        try {
          const r = await fetch('/api/admin/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone: box.querySelector("#zlPhone").value, password: box.querySelector("#zlPw").value }) });
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(j.detail || 'Login failed');
          done(j.token);
        } catch (e) { err.textContent = e.message; btn.disabled = false; }
      };
    });
    return pending;
  };
})();
