/* Vigil — global UI behaviour: theme toggle + mobile nav. */
(function () {
  // --- Theme toggle ---
  const themeBtn = document.getElementById('theme-toggle');
  const root = document.documentElement;

  function resolveTheme(saved) {
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  // Watch system preference if user is on "system"
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
      if ((window.__vigilTheme || 'system') === 'system') {
        root.setAttribute('data-theme', e.matches ? 'dark' : 'light');
      }
    });
  }

  if (themeBtn) {
    themeBtn.addEventListener('click', async () => {
      const current = root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
      const next = current === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      window.__vigilTheme = next;

      // Persist: try the authenticated profile route first; fall back to /theme for anon.
      try {
        const fd = new FormData();
        fd.append('action', 'set_theme');
        fd.append('theme', next);
        const csrf = document.querySelector('input[name="csrf_token"]');
        if (csrf) fd.append('csrf_token', csrf.value);
        const r = await fetch('/profile', { method: 'POST', body: fd, credentials: 'same-origin', redirect: 'manual' });
        if (!r.ok && r.type !== 'opaqueredirect') throw new Error('profile save failed');
      } catch {
        try {
          await fetch('/theme', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ theme: next }),
          });
        } catch { /* swallow — UI already updated */ }
      }
    });
  }

  // --- Mobile nav toggle ---
  const navBtn = document.getElementById('nav-toggle');
  const nav = document.getElementById('topbar-nav');
  if (navBtn && nav) {
    navBtn.addEventListener('click', () => {
      const open = nav.classList.toggle('open');
      navBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    // Close on outside click
    document.addEventListener('click', (e) => {
      if (!nav.classList.contains('open')) return;
      if (nav.contains(e.target) || navBtn.contains(e.target)) return;
      nav.classList.remove('open');
      navBtn.setAttribute('aria-expanded', 'false');
    });
  }
})();
