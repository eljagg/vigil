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
  // --- Tabbed report panes (scan detail) ---
  document.querySelectorAll('[data-tab-group]').forEach((group) => {
    const groupId = group.getAttribute('data-tab-group');
    const buttons = group.querySelectorAll('[data-tab]');
    const panes = document.querySelectorAll(`[data-tab-pane][data-tab-group-ref="${groupId}"]`);
    if (!buttons.length || !panes.length) return;

    // Restore last-active tab from localStorage, or default to first
    const storageKey = `vigil.activeTab.${groupId}`;
    const saved = localStorage.getItem(storageKey);
    const defaultTab = buttons[0].getAttribute('data-tab');
    const initial = saved && Array.from(buttons).some(b => b.getAttribute('data-tab') === saved)
      ? saved : defaultTab;

    function activate(tab) {
      buttons.forEach((b) => {
        const isActive = b.getAttribute('data-tab') === tab;
        b.classList.toggle('active', isActive);
        b.setAttribute('aria-selected', isActive ? 'true' : 'false');
      });
      panes.forEach((p) => {
        p.classList.toggle('active', p.getAttribute('data-tab-pane') === tab);
      });
      try { localStorage.setItem(storageKey, tab); } catch (e) {}
    }
    activate(initial);

    buttons.forEach((b) => {
      b.addEventListener('click', () => activate(b.getAttribute('data-tab')));
    });
  });

  // --- Collapsible sections (dashboard panels) — state persisted to localStorage ---
  document.querySelectorAll('.collapsible[data-section-id]').forEach((sec) => {
    const id = sec.getAttribute('data-section-id');
    const heading = sec.querySelector('.collapsible-heading');
    if (!heading) return;
    const storageKey = `vigil.collapsed.${id}`;
    const saved = localStorage.getItem(storageKey);
    if (saved === '1') sec.setAttribute('data-collapsed', '1');

    heading.setAttribute('role', 'button');
    heading.setAttribute('tabindex', '0');
    heading.setAttribute('aria-expanded', sec.getAttribute('data-collapsed') === '1' ? 'false' : 'true');

    function toggle() {
      const wasCollapsed = sec.getAttribute('data-collapsed') === '1';
      if (wasCollapsed) {
        sec.removeAttribute('data-collapsed');
        try { localStorage.removeItem(storageKey); } catch (e) {}
        heading.setAttribute('aria-expanded', 'true');
      } else {
        sec.setAttribute('data-collapsed', '1');
        try { localStorage.setItem(storageKey, '1'); } catch (e) {}
        heading.setAttribute('aria-expanded', 'false');
      }
    }
    heading.addEventListener('click', toggle);
    heading.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggle();
      }
    });
  });
  // --- Admin: inline edit row toggle on /admin/users ---
  document.querySelectorAll('[data-edit-user]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const userId = btn.getAttribute('data-edit-user');
      const row = document.getElementById(`user-edit-${userId}`);
      if (!row) return;
      const isOpen = row.style.display !== 'none';
      row.style.display = isOpen ? 'none' : '';
      btn.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
      if (!isOpen) {
        const firstInput = row.querySelector('input[type="text"]');
        if (firstInput) firstInput.focus();
      }
    });
  });
  document.querySelectorAll('[data-cancel-edit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const userId = btn.getAttribute('data-cancel-edit');
      const row = document.getElementById(`user-edit-${userId}`);
      const trigger = document.querySelector(`[data-edit-user="${userId}"]`);
      if (row) row.style.display = 'none';
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
  });

  // --- Admin: inline edit row toggle on /admin/paths ---
  document.querySelectorAll('[data-edit-path]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const pathId = btn.getAttribute('data-edit-path');
      const row = document.getElementById(`path-edit-${pathId}`);
      if (!row) return;
      const isOpen = row.style.display !== 'none';
      row.style.display = isOpen ? 'none' : '';
      btn.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
      if (!isOpen) {
        const firstInput = row.querySelector('input[type="text"], textarea');
        if (firstInput) firstInput.focus();
      }
    });
  });
  document.querySelectorAll('[data-cancel-path-edit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const pathId = btn.getAttribute('data-cancel-path-edit');
      const row = document.getElementById(`path-edit-${pathId}`);
      const trigger = document.querySelector(`[data-edit-path="${pathId}"]`);
      if (row) row.style.display = 'none';
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
  });
})();
