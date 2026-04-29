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

  // --- Dashboard rollup expand: lazy-load file list inline ---
  document.querySelectorAll('[data-expand-scan]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      const scanId = btn.getAttribute('data-expand-scan');
      const targetSelector = btn.getAttribute('data-target');
      const target = document.querySelector(targetSelector);
      if (!target) return;
      const isOpen = target.classList.toggle('open');
      btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      btn.textContent = isOpen ? 'Hide files' : 'Show files';
      if (isOpen && !target.dataset.loaded) {
        target.innerHTML = '<div class="muted small" style="padding:0.6rem;">Loading…</div>';
        try {
          const r = await fetch(`/scan/${scanId}/files-fragment`, { credentials: 'same-origin' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          target.innerHTML = await r.text();
          target.dataset.loaded = '1';
        } catch (err) {
          target.innerHTML = `<div class="muted small" style="padding:0.6rem;">Failed to load files: ${err.message}</div>`;
        }
      }
    });
  });

  // --- Generic table search: any input[data-search-target="#tbl-id"] filters
  //     rows of that table by substring match, case-insensitive. Multiple
  //     comma-separated targets supported for filtering several tables at once. ---
  document.querySelectorAll('[data-search-target]').forEach((input) => {
    const targets = input.getAttribute('data-search-target')
      .split(',').map(s => s.trim()).filter(Boolean);
    const counterEl = input.parentElement
      && input.parentElement.querySelector('[data-search-count]');

    function applyFilter() {
      const q = input.value.trim().toLowerCase();
      let totalShown = 0, totalRows = 0;

      targets.forEach((sel) => {
        const tbl = document.querySelector(sel);
        if (!tbl) return;
        // Only direct rows of THIS table — never rows inside nested tables
        // (e.g. the inner files panels in the by-job report).
        const rows = tbl.querySelectorAll(':scope > tbody > tr');
        rows.forEach((row) => {
          // Skip rows that are companion expand rows (no real content, e.g. files-expand-row in dashboard, or the details rows in by-job)
          if (row.classList.contains('files-expand-row')) return;
          if (row.dataset.searchSkip === '1') return;
          totalRows += 1;
          let visible;
          if (!q) {
            row.style.display = '';
            visible = true;
            totalShown += 1;
          } else {
            const text = (row.dataset.searchText || row.textContent).toLowerCase();
            if (text.includes(q)) {
              row.style.display = '';
              visible = true;
              totalShown += 1;
            } else {
              row.style.display = 'none';
              visible = false;
            }
          }
          // If the next sibling is a companion (skip / expand row), match its visibility
          // to its parent's so we don't leave orphan expand panels visible.
          const nxt = row.nextElementSibling;
          if (nxt && (nxt.dataset.searchSkip === '1' || nxt.classList.contains('files-expand-row'))) {
            nxt.style.display = visible ? '' : 'none';
          }
        });
      });

      if (counterEl) {
        if (q) {
          counterEl.textContent = `${totalShown} of ${totalRows}`;
          counterEl.classList.add('search-active');
        } else {
          counterEl.textContent = `${totalRows}`;
          counterEl.classList.remove('search-active');
        }
      }
    }

    input.addEventListener('input', applyFilter);
    applyFilter(); // initial count
  });
})();
