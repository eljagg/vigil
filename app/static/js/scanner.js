/* Vigil — browser-side scanner.
 *
 * Uses the File System Access API to walk a folder the user picks, builds
 * a manifest of (relative_path, filename, size, mtime), and POSTs it to
 * /api/scan/submit.
 *
 * Browser support: Chrome, Edge, Opera, Brave (desktop). Firefox and
 * Safari do not support showDirectoryPicker — we detect and warn.
 *
 * No file contents are read — only metadata (.name, .size, .lastModified).
 */
(function () {
  const supported = typeof window.showDirectoryPicker === 'function';

  const compatWarn = document.getElementById('compat-warn');
  const browseBtn = document.getElementById('browse-btn');
  const pickedBox = document.getElementById('picked-folder');
  const labelInput = document.getElementById('label-input');
  const workstationInput = document.getElementById('workstation-input');
  const runBtn = document.getElementById('run-btn');
  const progressCard = document.getElementById('progress-card');
  const progressBar = document.getElementById('progress-bar');
  const progressFiles = document.getElementById('progress-files');
  const progressBytes = document.getElementById('progress-bytes');
  const progressStage = document.getElementById('progress-stage');

  if (!browseBtn) return; // not on the scan page

  // Persist workstation across sessions
  if (workstationInput) {
    const saved = localStorage.getItem('vigil.workstation');
    if (saved) workstationInput.value = saved;
    workstationInput.addEventListener('blur', () => {
      localStorage.setItem('vigil.workstation', workstationInput.value.trim());
    });
  }

  if (!supported) {
    if (compatWarn) compatWarn.classList.add('show');
    browseBtn.disabled = true;
    runBtn.disabled = true;
    return;
  }

  // ===== v2.0.7: persisted FileSystemDirectoryHandle (Option A) =====
  // We store the handle keyed by path_entry_id so a rescan of the same
  // path can reuse it. Chrome will still ask permission once per session,
  // but the file picker is skipped — operator just clicks Allow.
  //
  // IndexedDB schema: db 'vigil' v1, store 'handles' keyed by path_entry_id.
  const HANDLE_DB = 'vigil';
  const HANDLE_STORE = 'handles';

  function openHandleDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(HANDLE_DB, 1);
      req.onupgradeneeded = () => {
        req.result.createObjectStore(HANDLE_STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  async function saveHandle(pathEntryId, handle) {
    if (!pathEntryId || !handle) return;
    try {
      const db = await openHandleDB();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(HANDLE_STORE, 'readwrite');
        tx.objectStore(HANDLE_STORE).put(handle, String(pathEntryId));
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
      });
      db.close();
    } catch (e) {
      console.warn('vigil: failed to save handle', e);
    }
  }
  async function loadHandle(pathEntryId) {
    if (!pathEntryId) return null;
    try {
      const db = await openHandleDB();
      const handle = await new Promise((resolve, reject) => {
        const tx = db.transaction(HANDLE_STORE, 'readonly');
        const req = tx.objectStore(HANDLE_STORE).get(String(pathEntryId));
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
      });
      db.close();
      return handle;
    } catch (e) {
      console.warn('vigil: failed to load handle', e);
      return null;
    }
  }
  async function deleteHandle(pathEntryId) {
    if (!pathEntryId) return;
    try {
      const db = await openHandleDB();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(HANDLE_STORE, 'readwrite');
        tx.objectStore(HANDLE_STORE).delete(String(pathEntryId));
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
      });
      db.close();
    } catch (e) { /* swallow */ }
  }
  async function ensureReadPermission(handle) {
    // Chrome requires re-authorization per session even if the handle is saved.
    // queryPermission tells us whether we already have it; requestPermission
    // shows the small "Allow this site to view files in X?" dialog (NOT the
    // full file picker).
    try {
      const opts = { mode: 'read' };
      if ((await handle.queryPermission(opts)) === 'granted') return true;
      const result = await handle.requestPermission(opts);
      return result === 'granted';
    } catch (e) {
      console.warn('vigil: permission check failed', e);
      return false;
    }
  }

  let pickedHandle = null;
  let pickedName = '';

  // Read the rescan context from the scanner-card's data attributes
  const scannerCard = document.querySelector('.scanner-card');
  const pathEntryId = scannerCard ? scannerCard.dataset.pathEntryId : null;
  const isRescan    = scannerCard ? scannerCard.dataset.fromScan === '1' : false;

  // Saved-handle UI elements (only present on rescan pages)
  const savedHandleBlock = document.getElementById('saved-handle-block');
  const savedHandleName  = document.getElementById('saved-handle-name');
  const savedHandleName2 = document.getElementById('saved-handle-name-2');
  const reuseHandleBtn   = document.getElementById('reuse-handle-btn');
  const pickDifferentBtn = document.getElementById('pick-different-btn');

  // On rescan page load, check if we have a saved handle for this path
  if (isRescan && pathEntryId && savedHandleBlock) {
    (async () => {
      const handle = await loadHandle(pathEntryId);
      if (!handle) return; // no saved handle — leave Browse-only flow visible
      // Show the quick-reuse block
      if (savedHandleName)  savedHandleName.textContent  = handle.name;
      if (savedHandleName2) savedHandleName2.textContent = handle.name;
      savedHandleBlock.style.display = 'block';
      // Hide Browse and rename it for "different folder" path below
      if (browseBtn) browseBtn.style.display = 'none';
    })();
  }

  // "Use saved folder" button
  if (reuseHandleBtn) {
    reuseHandleBtn.addEventListener('click', async () => {
      const handle = await loadHandle(pathEntryId);
      if (!handle) {
        alert('Saved folder is no longer available. Please pick the folder again.');
        if (savedHandleBlock) savedHandleBlock.style.display = 'none';
        if (browseBtn) browseBtn.style.display = '';
        return;
      }
      const ok = await ensureReadPermission(handle);
      if (!ok) {
        alert('Permission was not granted. Click "Use saved folder" again, or "Pick a different folder".');
        return;
      }
      // Confirm the handle still resolves (folder might have been moved/deleted)
      try {
        // Touch the handle by listing one entry — throws NotFoundError if gone
        const it = handle.values()[Symbol.asyncIterator]();
        await it.next();
      } catch (e) {
        if (e && e.name === 'NotFoundError') {
          alert('The saved folder no longer exists. Please pick the folder again.');
          await deleteHandle(pathEntryId);
          if (savedHandleBlock) savedHandleBlock.style.display = 'none';
          if (browseBtn) browseBtn.style.display = '';
          return;
        }
        // Other errors — fall through (might be empty folder which is fine)
      }
      pickedHandle = handle;
      pickedName = handle.name;
      pickedBox.textContent = pickedName + ' (saved)';
      pickedBox.style.display = 'block';
      runBtn.disabled = false;
    });
  }

  // "Pick a different folder" — falls back to fresh Browse
  if (pickDifferentBtn) {
    pickDifferentBtn.addEventListener('click', () => {
      if (savedHandleBlock) savedHandleBlock.style.display = 'none';
      if (browseBtn) {
        browseBtn.style.display = '';
        browseBtn.click();
      }
    });
  }

  browseBtn.addEventListener('click', async () => {
    try {
      const handle = await window.showDirectoryPicker({ mode: 'read' });
      pickedHandle = handle;
      pickedName = handle.name;
      pickedBox.textContent = pickedName;
      pickedBox.style.display = 'block';
      runBtn.disabled = false;
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      console.error(err);
      alert('Could not open folder picker: ' + (err.message || err));
    }
  });

  runBtn.addEventListener('click', async () => {
    if (!pickedHandle) {
      alert('Pick a folder first.');
      return;
    }
    const label = (labelInput && labelInput.value || '').trim();
    if (!label) {
      alert('A label is required. Describe the actual path so this scan can be identified later (e.g. "Z:\\Backup Logs\\Archive on MARS").');
      labelInput && labelInput.focus();
      return;
    }
    runBtn.disabled = true;
    browseBtn.disabled = true;
    progressCard.classList.add('active');
    setProgress(0, 0, 0, 'Walking folder…');

    let files;
    try {
      files = await walkDirectory(pickedHandle, '', (count, bytes) => {
        setProgress(null, count, bytes, 'Walking folder…');
      });
    } catch (err) {
      console.error(err);
      alert('Scan failed: ' + (err.message || err));
      browseBtn.disabled = false;
      runBtn.disabled = false;
      return;
    }

    setProgress(60, files.length, sumBytes(files), 'Sending to server…');

    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const workstation = (workstationInput && workstationInput.value || '').trim() || null;
    const payload = {
      path: pickedName,
      label: label,
      workstation: workstation,
      files: files,
    };

    let resp;
    try {
      resp = await fetch('/api/scan/submit', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrf,
        },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      console.error(err);
      alert('Network error submitting scan: ' + err.message);
      browseBtn.disabled = false;
      runBtn.disabled = false;
      return;
    }

    if (!resp.ok) {
      const txt = await resp.text();
      alert('Server rejected scan (' + resp.status + '): ' + txt);
      browseBtn.disabled = false;
      runBtn.disabled = false;
      return;
    }

    const data = await resp.json();
    setProgress(100, files.length, sumBytes(files), 'Done');

    // v2.0.7: persist the directory handle keyed by path_entry_id so the
    // next rescan can reuse it without showing the file picker again.
    if (data && data.path_entry_id && pickedHandle) {
      await saveHandle(data.path_entry_id, pickedHandle);
    }

    if (data && data.redirect) {
      window.location.href = data.redirect;
    } else {
      alert('Scan submitted, but no redirect target was returned.');
      browseBtn.disabled = false;
      runBtn.disabled = false;
    }
  });

  function setProgress(percent, fileCount, byteCount, stage) {
    if (percent !== null && progressBar) progressBar.style.width = percent + '%';
    if (progressFiles) progressFiles.textContent = fileCount.toLocaleString() + ' files';
    if (progressBytes) progressBytes.textContent = formatBytes(byteCount);
    if (progressStage) progressStage.textContent = stage || '';
  }

  function sumBytes(files) {
    let t = 0;
    for (const f of files) t += f.size_bytes;
    return t;
  }

  function formatBytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let s = n, i = 0;
    while (Math.abs(s) >= 1024 && i < units.length - 1) { s /= 1024; i++; }
    return s.toFixed(s < 10 && i > 0 ? 1 : 0) + ' ' + units[i];
  }

  /**
   * Recursively walks a FileSystemDirectoryHandle and returns a flat array
   * of { relative_path, filename, size_bytes, mtime } for every file.
   *
   * onProgress(count, bytes) is called periodically so the UI can update.
   */
  async function walkDirectory(dirHandle, prefix, onProgress) {
    const out = [];
    let bytes = 0;
    let lastUiUpdate = 0;

    async function walk(handle, prefix) {
      // for-await-of is the supported iteration pattern for FS handles
      for await (const [name, entry] of handle.entries()) {
        if (entry.kind === 'directory') {
          const subPrefix = prefix ? prefix + '/' + name : name;
          await walk(entry, subPrefix);
        } else if (entry.kind === 'file') {
          let file;
          try {
            file = await entry.getFile();
          } catch (err) {
            // Skip files we can't read (locked, no permission, etc.)
            console.warn('Skipping ' + (prefix ? prefix + '/' + name : name) + ': ' + err.message);
            continue;
          }
          const rel = prefix ? prefix + '/' + name : name;
          out.push({
            relative_path: rel,
            filename: name,
            size_bytes: file.size,
            mtime: new Date(file.lastModified).toISOString(),
          });
          bytes += file.size;

          const now = Date.now();
          if (now - lastUiUpdate > 80) {
            onProgress(out.length, bytes);
            // yield to the event loop so the UI repaints
            await new Promise(r => setTimeout(r, 0));
            lastUiUpdate = now;
          }
        }
      }
    }

    await walk(dirHandle, prefix);
    onProgress(out.length, bytes);
    return out;
  }
})();
