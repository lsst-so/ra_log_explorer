/* ra-log-explorer — home view: pick an exposure, configure credentials,
 * inspect the cache, watch a fetch happen.
 *
 * The user types a dataId; the browser resolves it to its TAI shutter-
 * close timestamp via /api/exposure-time/<dataId> and keeps that value
 * in memory until submit. The user never types or sees a UTC/TAI
 * choice — exposure timings always come from the same well-known
 * service, which always returns TAI.
 */
'use strict';

const LS = {
  username: 'ra_log_explorer.username',
  password: 'ra_log_explorer.password',
  remember: 'ra_log_explorer.remember',
  rspTokenFile: 'ra_log_explorer.rspTokenFile',
  lastRun: 'ra_log_explorer.lastRun',  // JSON {exposureId, cluster, namespace, ...} — no tZero, that's looked up each time
};

let homeListenersWired = false;
let resolvedTZero = null;       // last looked-up ISOT string (TAI) for the current dataId
let resolvedForExpId = null;    // the exposureId resolvedTZero corresponds to
let lookupTimer = null;         // debounce timer for the dataId input
let lookupSeq = 0;              // sequence number to ignore stale lookup responses

function startHome() {
  if (!homeListenersWired) wireHomeListeners();
  prefillForm();
  prefillCreds();
  refreshCache();
  // If the form already has a dataId pre-filled from localStorage, kick
  // a lookup so the submit button is ready to fire immediately.
  triggerLookupIfReady();
}
window.startHome = startHome;

function prefillForm() {
  const last = readJson(LS.lastRun);
  if (!last) return;
  const form = document.getElementById('fetch-form');
  for (const [k, v] of Object.entries(last)) {
    const el = form.elements.namedItem(k);
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = v;
  }
}

function prefillCreds() {
  const form = document.getElementById('creds-form');
  const remember = localStorage.getItem(LS.remember) === '1';
  const u = localStorage.getItem(LS.username);
  const p = localStorage.getItem(LS.password);
  const tk = localStorage.getItem(LS.rspTokenFile);
  if (u != null) form.elements.username.value = u;
  if (p != null) form.elements.password.value = p;
  // The RSP token path is non-secret (it's a file path, not the token
  // itself), so we persist it unconditionally — no "remember" toggle
  // needed.
  if (tk != null) form.elements.rspTokenFile.value = tk;
  form.elements.remember.checked = remember;
  updateCredsState();
}

function updateCredsState() {
  const remember = document.getElementById('creds-form').elements.remember.checked;
  const has = !!localStorage.getItem(LS.password);
  const el = document.getElementById('creds-state');
  if (has && remember) el.textContent = '(remembered in this browser)';
  else if (has && !remember) el.textContent = '(stored but "remember" off — will clear after this fetch)';
  else el.textContent = '';
}

function saveCreds() {
  const form = document.getElementById('creds-form');
  const u = form.elements.username.value.trim();
  const p = form.elements.password.value;
  const tk = form.elements.rspTokenFile.value.trim();
  const remember = form.elements.remember.checked;
  if (remember) {
    if (u) localStorage.setItem(LS.username, u);
    if (p) localStorage.setItem(LS.password, p);
    localStorage.setItem(LS.remember, '1');
  } else {
    localStorage.removeItem(LS.remember);
  }
  if (tk) localStorage.setItem(LS.rspTokenFile, tk);
  else localStorage.removeItem(LS.rspTokenFile);
  updateCredsState();
}

function forgetCreds() {
  localStorage.removeItem(LS.username);
  localStorage.removeItem(LS.password);
  localStorage.removeItem(LS.remember);
  localStorage.removeItem(LS.rspTokenFile);
  const form = document.getElementById('creds-form');
  form.elements.username.value = 'merlin';
  form.elements.password.value = '';
  form.elements.rspTokenFile.value = '';
  form.elements.remember.checked = false;
  updateCredsState();
}

function saveLastRun(values) {
  const { username, password, tZero, ...rest } = values;  // tZero is looked up each time
  localStorage.setItem(LS.lastRun, JSON.stringify(rest));
}

function readJson(key) {
  const raw = localStorage.getItem(key);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (_) { return null; }
}

function readFormValues() {
  const form = document.getElementById('fetch-form');
  const fd = new FormData(form);
  const out = {};
  for (const [k, v] of fd.entries()) out[k] = v;
  out.exposureId = parseInt(out.exposureId, 10);
  out.workers = parseInt(out.workers, 10);
  out.windowBefore = parseFloat(out.windowBefore);
  out.windowAfter = parseFloat(out.windowAfter);
  const creds = document.getElementById('creds-form');
  out.username = creds.elements.username.value.trim();
  const password = creds.elements.password.value;
  if (password) out.password = password;
  // Always TAI; the server applies the -37 s conversion. We deliberately
  // never expose a UTC opt-out in the UI now that timings come from a
  // service that's TAI by construction.
  out.tZero = resolvedTZero;
  return out;
}

// ----- exposure-time lookup ------------------------------------------------

function setTZeroStatus(text, kind /* 'info' | 'ok' | 'error' */) {
  const el = document.getElementById('tzero-status');
  el.textContent = text;
  el.classList.remove('ok', 'error');
  if (kind === 'ok') el.classList.add('ok');
  if (kind === 'error') el.classList.add('error');
  updateSubmitButton();
}

function clearResolvedTZero() {
  resolvedTZero = null;
  resolvedForExpId = null;
}

function updateSubmitButton() {
  const submit = document.getElementById('fetch-submit');
  // Allow re-submitting an already-typed dataId without forcing a refetch.
  submit.disabled = !resolvedTZero;
}

// dataIds are 13 digits: YYYYMMDDSSSSS. Anything shorter is still
// mid-typing — firing a lookup at every keystroke produces a stream of
// noisy "no exposure-time record for 20260" messages, which is worse
// than just waiting. Anything longer is unambiguously a typo.
const DATAID_LENGTH = 13;

function triggerLookupIfReady() {
  const form = document.getElementById('fetch-form');
  const raw = form.elements.exposureId.value.trim();
  if (!raw) {
    clearResolvedTZero();
    setTZeroStatus('enter a 13-digit dataId to resolve its shutter close time', 'info');
    return;
  }
  if (raw.length < DATAID_LENGTH) {
    clearResolvedTZero();
    setTZeroStatus(`keep typing — dataIds are ${DATAID_LENGTH} digits (have ${raw.length})`, 'info');
    return;
  }
  if (raw.length > DATAID_LENGTH) {
    clearResolvedTZero();
    setTZeroStatus(`dataId too long — expected ${DATAID_LENGTH} digits (have ${raw.length})`, 'error');
    return;
  }
  const expId = parseInt(raw, 10);
  if (!Number.isFinite(expId)) {
    clearResolvedTZero();
    setTZeroStatus('dataId must be an integer', 'error');
    return;
  }
  // If we already resolved this exact dataId, don't re-request.
  if (resolvedForExpId === expId && resolvedTZero) {
    setTZeroStatus(`shutter close (TAI): ${resolvedTZero}`, 'ok');
    return;
  }
  setTZeroStatus(`looking up shutter close for ${expId}...`, 'info');
  const mySeq = ++lookupSeq;
  const tokenFile = document.getElementById('creds-form').elements.rspTokenFile.value.trim();
  const qs = tokenFile ? `?tokenFile=${encodeURIComponent(tokenFile)}` : '';
  fetch(`/api/exposure-time/${expId}${qs}`)
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (mySeq !== lookupSeq) return;  // stale; user typed something newer
      if (r.ok && body.tZero) {
        resolvedTZero = body.tZero;
        resolvedForExpId = expId;
        setTZeroStatus(`shutter close (TAI): ${body.tZero}`, 'ok');
      } else if (r.status === 404) {
        clearResolvedTZero();
        setTZeroStatus(`no exposure-time record for ${expId}`, 'error');
      } else if (r.status === 503) {
        clearResolvedTZero();
        setTZeroStatus(body.error || 'RSP token / ConsDB lookup not configured', 'error');
      } else {
        clearResolvedTZero();
        setTZeroStatus(`lookup failed: ${body.error || r.status}`, 'error');
      }
    })
    .catch((e) => {
      if (mySeq !== lookupSeq) return;
      clearResolvedTZero();
      setTZeroStatus(`lookup failed: ${e}`, 'error');
    });
}

function scheduleLookup() {
  clearResolvedTZero();
  setTZeroStatus('typing...', 'info');
  if (lookupTimer) clearTimeout(lookupTimer);
  lookupTimer = setTimeout(triggerLookupIfReady, 300);
}

// ----- cache list ----------------------------------------------------------

async function refreshCache() {
  try {
    const r = await fetch('/api/cache');
    const data = await r.json();
    renderCache(data);
  } catch (e) {
    document.getElementById('cache-summary').textContent = `(failed to load: ${e})`;
  }
}

function renderCache(data) {
  document.getElementById('home-cache-info').textContent =
    `cache: ${humanBytes(data.root.totalBytes)} @ ${data.root.path}`;
  const sumBytes = data.windows.reduce((acc, w) => acc + (w.sizeOnDisk || 0), 0);
  document.getElementById('cache-summary').textContent =
    `${data.windows.length} cached window${data.windows.length === 1 ? '' : 's'}`
    + ` · ${humanBytes(sumBytes)} total`;
  const tbody = document.getElementById('cache-tbody');
  tbody.innerHTML = '';
  const tfoot = document.getElementById('cache-tfoot');
  tfoot.innerHTML = '';
  const deleteAll = document.getElementById('cache-delete-all');
  deleteAll.disabled = data.windows.length === 0;
  for (const w of data.windows) {
    const tr = document.createElement('tr');
    tr.title = 'click to copy these settings to the form above';
    const fromS = (w.fromIso || '').replace('T', ' ').replace(/\..*Z$/, '');
    const toS = (w.toIso || '').replace('T', ' ').replace(/\..*Z$/, '');
    const fetchedS = (w.fetchedAt || '').replace('T', ' ').replace(/\..*$/, '');
    tr.innerHTML = `
      <td>${w.cluster} / ${w.namespace}</td>
      <td><span class="mono">${fromS} → ${toS}</span></td>
      <td><span class="mono">${fetchedS}</span></td>
      <td>${w.podCount}</td>
      <td>${humanBytes(w.sizeOnDisk)}</td>
      <td><button type="button" class="ghost mini delete" title="delete this cached window">✕</button></td>`;
    // Row click: copy settings into the form. The trailing ✕ button has
    // its own handler that stopPropagation()s so it doesn't trigger this.
    tr.addEventListener('click', () => useCacheSettings(w));
    const delBtn = tr.querySelector('button.delete');
    delBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      deleteCacheWindow(w);
    });
    tbody.appendChild(tr);
  }
  if (data.windows.length > 0) {
    const tr = document.createElement('tr');
    tr.className = 'cache-total-row';
    tr.innerHTML =
      `<td colspan="3" class="total-label">total</td>`
      + `<td>${data.windows.length}</td>`
      + `<td>${humanBytes(sumBytes)}</td>`
      + `<td></td>`;
    tfoot.appendChild(tr);
  }
}

async function deleteCacheWindow(w) {
  if (!window.confirm(
    `Delete cached window?\n\n${w.cluster}/${w.namespace}/${w.windowDir}\n(${humanBytes(w.sizeOnDisk)})`,
  )) return;
  const url = `/api/cache/${encodeURIComponent(w.cluster)}/${encodeURIComponent(w.namespace)}/${encodeURIComponent(w.windowDir)}`;
  try {
    const r = await fetch(url, { method: 'DELETE' });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      alert(`Delete failed: ${body.error || r.status}`);
      return;
    }
    const data = await r.json();
    renderCache(data);
  } catch (e) {
    alert(`Delete failed: ${e}`);
  }
}

async function deleteAllCache() {
  // Read the current total off the visible summary so the confirm dialog
  // is honest about how much disk we're about to free.
  const info = document.getElementById('home-cache-info').textContent;
  if (!window.confirm(`Delete EVERY cached window?\n\n${info}\n\nThis cannot be undone.`)) return;
  try {
    const r = await fetch('/api/cache', { method: 'DELETE' });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      alert(`Delete failed: ${body.error || r.status}`);
      return;
    }
    const data = await r.json();
    renderCache(data);
  } catch (e) {
    alert(`Delete failed: ${e}`);
  }
}

function useCacheSettings(w) {
  const form = document.getElementById('fetch-form');
  form.elements.cluster.value = w.cluster;
  form.elements.namespace.value = w.namespace;
  // Recompute windowBefore/After to land on the same cached slug, *if*
  // we've already resolved a shutter-close time for the current dataId.
  if (resolvedTZero && w.fromIso && w.toIso) {
    try {
      const taiMs = Date.parse(resolvedTZero.endsWith('Z') ? resolvedTZero : resolvedTZero + 'Z');
      const utcMs = taiMs - 37_000;
      const fromMs = Date.parse(w.fromIso);
      const toMs = Date.parse(w.toIso);
      form.elements.windowBefore.value = ((utcMs - fromMs) / 1000).toFixed(1);
      form.elements.windowAfter.value = ((toMs - utcMs) / 1000).toFixed(1);
    } catch (_) { /* leave defaults */ }
  }
  const msg = document.getElementById('fetch-message');
  msg.textContent = `Settings copied from ${w.windowDir}. ` +
    (resolvedTZero
      ? 'Hit Fetch to reopen.'
      : 'Type a dataId so we can compute the matching window-before/after.');
}

// ----- fetch + progress ---------------------------------------------------

let activeJobId = null;
let activeEventSource = null;

async function startFetch(ev) {
  ev.preventDefault();
  if (activeJobId) return;
  const values = readFormValues();
  if (!Number.isFinite(values.exposureId)) {
    showMessage('exposureId must be an integer.', true);
    return;
  }
  if (!values.tZero) {
    showMessage('Shutter close time has not been resolved yet.', true);
    return;
  }
  saveCreds();
  saveLastRun(values);

  const submit = document.getElementById('fetch-submit');
  submit.disabled = true;
  showMessage('Starting fetch...');
  showProgressCard(true);
  resetProgress();

  let jobId;
  try {
    const r = await fetch('/api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(values),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    jobId = data.jobId;
  } catch (e) {
    showMessage(`Failed to start fetch: ${e.message || e}`, true);
    updateSubmitButton();
    return;
  }
  activeJobId = jobId;
  openProgressStream(jobId);
}

function showMessage(text, isError) {
  const el = document.getElementById('fetch-message');
  el.textContent = text;
  el.classList.toggle('error', !!isError);
}

function showProgressCard(visible) {
  document.getElementById('progress-card').hidden = !visible;
}

function resetProgress() {
  document.getElementById('progress-fill').style.width = '0%';
  document.getElementById('progress-text').textContent = '';
  document.getElementById('progress-log').textContent = '';
}

function logProgress(line) {
  const el = document.getElementById('progress-log');
  el.textContent = el.textContent + line + '\n';
  el.scrollTop = el.scrollHeight;
}

function openProgressStream(jobId) {
  if (activeEventSource) activeEventSource.close();
  const es = new EventSource(`/api/fetch/${jobId}/progress`);
  activeEventSource = es;
  let total = 0, done = 0;
  es.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (_) { return; }
    if (ev.type === 'start') {
      logProgress(`window  ${ev.fromIso} → ${ev.toIso}`);
    } else if (ev.type === 'pod-done') {
      total = ev.total;
      done = ev.i;
      const pct = total ? (done / total) * 100 : 0;
      document.getElementById('progress-fill').style.width = pct.toFixed(1) + '%';
      document.getElementById('progress-text').textContent =
        `${done}/${total} pods fetched`;
      if (done % 20 === 0 || done === total) {
        logProgress(`  ${done.toString().padStart(4)}/${total}  ${ev.pod}`);
      }
    } else if (ev.type === 'parsing') {
      document.getElementById('progress-text').textContent =
        `parsing (${ev.podCount} pods, ${humanBytes(ev.totalBytes)}; ${ev.cacheReuse})`;
      logProgress(`parsing ${ev.podCount} pods (${humanBytes(ev.totalBytes)}, cacheReuse=${ev.cacheReuse})`);
    } else if (ev.type === 'shutter-close') {
      // Night-mode only: post-parse pass resolving shutter close times for
      // each dataId. May involve a batched ConsDB call.
      let msg = '';
      if (ev.phase === 'starting') {
        msg = `resolving shutter close for ${ev.total} dataIds…`;
      } else if (ev.phase === 'cache-checked') {
        msg = `cache: ${ev.cacheHits} hits, ${ev.remaining} to query`;
      } else if (ev.phase === 'done') {
        msg = `ConsDB: ${ev.consdbHits} resolved, ${ev.stillMissing} still missing`;
      } else if (ev.phase === 'no-token' || ev.phase === 'empty-token') {
        msg = `no RSP token — ${ev.remaining} dataIds will be missing from Δshutter histograms`;
      } else if (ev.phase === 'consdb-error') {
        msg = `ConsDB query failed: ${ev.error}`;
      }
      if (msg) {
        document.getElementById('progress-text').textContent = msg;
        logProgress(msg);
      }
    } else if (ev.type === 'done') {
      logProgress(`done in ${ev.elapsedS.toFixed(1)}s — ${ev.podCount} pods (${humanBytes(ev.totalBytes)})`);
      document.getElementById('progress-fill').style.width = '100%';
      const target = ev.kind === 'night' ? 'night view' : 'explore view';
      document.getElementById('progress-text').textContent = `done — opening ${target} ...`;
      es.close();
      activeEventSource = null;
      activeJobId = null;
      updateSubmitButton();
      document.getElementById('night-submit').disabled = false;
      transitionToExplore();
    } else if (ev.type === 'error') {
      logProgress(`ERROR: ${ev.error}`);
      showMessage(`Fetch failed: ${ev.error.split('\n')[0]}`, true);
      const nightMsg = document.getElementById('night-message');
      if (nightMsg) {
        nightMsg.textContent = `Fetch failed: ${ev.error.split('\n')[0]}`;
        nightMsg.classList.add('error');
      }
      document.getElementById('night-submit').disabled = false;
      es.close();
      activeEventSource = null;
      activeJobId = null;
      updateSubmitButton();
    }
  };
  es.onerror = () => {};
}

async function transitionToExplore() {
  try {
    const r = await fetch('/api/summary');
    const summary = await r.json();
    if (!summary.loaded) {
      showMessage('Fetch finished but the server reports no loaded data?', true);
      return;
    }
    if (summary.mode === 'night' && window.showNight) {
      window.showNight(summary);
    } else if (window.showExplore) {
      window.showExplore(summary);
    }
  } catch (e) {
    showMessage(`Could not load summary: ${e}`, true);
  }
}

async function startNightFetch(ev) {
  ev.preventDefault();
  if (activeJobId) return;
  const form = document.getElementById('night-form');
  const credsForm = document.getElementById('creds-form');
  const dayObsRaw = form.elements.dayObs.value.trim();
  const dayObs = parseInt(dayObsRaw, 10);
  if (!Number.isFinite(dayObs) || dayObs < 19000000 || dayObs > 30000000) {
    const el = document.getElementById('night-message');
    el.textContent = 'dayObs must be an 8-digit YYYYMMDD integer.';
    el.classList.add('error');
    return;
  }
  saveCreds();
  const body = {
    dayObs,
    cluster: form.elements.cluster.value.trim() || undefined,
    namespace: form.elements.namespace.value.trim() || undefined,
    workers: parseInt(form.elements.workers.value, 10) || undefined,
    username: credsForm.elements.username.value.trim() || undefined,
    password: credsForm.elements.password.value || undefined,
  };
  const submit = document.getElementById('night-submit');
  submit.disabled = true;
  const msgEl = document.getElementById('night-message');
  msgEl.textContent = 'Starting night fetch...';
  msgEl.classList.remove('error');
  showProgressCard(true);
  resetProgress();

  let jobId;
  try {
    const r = await fetch('/api/fetch-night', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    jobId = data.jobId;
  } catch (e) {
    msgEl.textContent = `Failed to start night fetch: ${e.message || e}`;
    msgEl.classList.add('error');
    submit.disabled = false;
    return;
  }
  activeJobId = jobId;
  openProgressStream(jobId);
}

// ----- wiring -------------------------------------------------------------

function wireHomeListeners() {
  document.getElementById('fetch-form').addEventListener('submit', startFetch);
  document.getElementById('night-form').addEventListener('submit', startNightFetch);
  const expIdInput = document.getElementById('fetch-form').elements.exposureId;
  expIdInput.addEventListener('input', scheduleLookup);
  document.getElementById('creds-form').elements.remember.addEventListener('change', saveCreds);
  // RSP token path is non-secret — persist it as the user types so
  // the next lookup uses the right path without an explicit save.
  document.getElementById('creds-form').elements.rspTokenFile.addEventListener('input', () => {
    saveCreds();
    // The user may have just supplied a token after a failed lookup —
    // retry it.
    triggerLookupIfReady();
  });
  document.getElementById('creds-forget').addEventListener('click', forgetCreds);
  document.getElementById('cache-refresh').addEventListener('click', refreshCache);
  document.getElementById('cache-delete-all').addEventListener('click', deleteAllCache);
  homeListenersWired = true;
  updateSubmitButton();  // start with submit disabled until lookup resolves
}
