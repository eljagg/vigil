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

  let pickedHandle = null;
  let pickedName = '';

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
