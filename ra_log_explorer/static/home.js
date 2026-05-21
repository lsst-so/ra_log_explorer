/* ra-log-explorer — home view: pick an exposure, configure credentials,
 * inspect the cache, watch a fetch happen.
 *
 * `startHome()` is the entry point; it's called by app.js when the server
 * reports no exposure is currently loaded. While a fetch job is running,
 * progress is streamed in via the SSE `/api/fetch/<id>/progress` endpoint
 * and rendered into the #progress-card region.
 */
'use strict';

// localStorage keys — namespaced so they don't collide with anything else.
const LS = {
  username: 'ra_log_explorer.username',
  password: 'ra_log_explorer.password',
  remember: 'ra_log_explorer.remember',
  lastRun: 'ra_log_explorer.lastRun',  // JSON {expId, tZero, tZeroUtc, cluster, namespace, ...}
};

let homeListenersWired = false;

function startHome() {
  if (!homeListenersWired) wireHomeListeners();
  prefillForm();
  prefillCreds();
  refreshCache();
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
  if (u != null) form.elements.username.value = u;
  if (p != null) form.elements.password.value = p;
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
  const remember = form.elements.remember.checked;
  if (remember) {
    if (u) localStorage.setItem(LS.username, u);
    if (p) localStorage.setItem(LS.password, p);
    localStorage.setItem(LS.remember, '1');
  } else {
    // Don't clear if the user hadn't typed anything; only clear when
    // they explicitly hit the "forget" button.
    localStorage.removeItem(LS.remember);
  }
  updateCredsState();
}

function forgetCreds() {
  localStorage.removeItem(LS.username);
  localStorage.removeItem(LS.password);
  localStorage.removeItem(LS.remember);
  const form = document.getElementById('creds-form');
  form.elements.username.value = 'merlin';
  form.elements.password.value = '';
  form.elements.remember.checked = false;
  updateCredsState();
}

function saveLastRun(values) {
  // Don't persist credentials here; they have their own keys.
  const { username, password, ...rest } = values;
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
  // FormData doesn't include unchecked checkboxes — fix tZeroUtc.
  out.tZeroUtc = form.elements.tZeroUtc.checked;
  // Numeric coercions for fields the server is fussy about.
  out.exposureId = parseInt(out.exposureId, 10);
  out.workers = parseInt(out.workers, 10);
  out.windowBefore = parseFloat(out.windowBefore);
  out.windowAfter = parseFloat(out.windowAfter);
  // Folder credentials in from the creds form.
  const creds = document.getElementById('creds-form');
  out.username = creds.elements.username.value.trim();
  const password = creds.elements.password.value;
  if (password) out.password = password;
  return out;
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
  document.getElementById('cache-summary').textContent =
    `${data.windows.length} cached window${data.windows.length === 1 ? '' : 's'}`;
  const tbody = document.getElementById('cache-tbody');
  tbody.innerHTML = '';
  for (const w of data.windows) {
    const tr = document.createElement('tr');
    tr.title = 'click to copy these settings to the form above';
    tr.addEventListener('click', () => useCacheSettings(w));
    const fromS = (w.fromIso || '').replace('T', ' ').replace(/\..*Z$/, '');
    const toS = (w.toIso || '').replace('T', ' ').replace(/\..*Z$/, '');
    const fetchedS = (w.fetchedAt || '').replace('T', ' ').replace(/\..*$/, '');
    tr.innerHTML = `
      <td>${w.cluster} / ${w.namespace}</td>
      <td><span class="mono">${fromS} → ${toS}</span></td>
      <td><span class="mono">${fetchedS}</span></td>
      <td>${w.podCount}</td>
      <td>${humanBytes(w.sizeOnDisk)}</td>`;
    tbody.appendChild(tr);
  }
}

function useCacheSettings(w) {
  // Populate the cluster/namespace/window fields with this row's values
  // so a subsequent fetch hits the same on-disk window.
  const form = document.getElementById('fetch-form');
  form.elements.cluster.value = w.cluster;
  form.elements.namespace.value = w.namespace;
  // Try to infer windowBefore/After from the cached span and the user's
  // current tZero. If they don't have a tZero typed yet, just leave the
  // defaults — they can pick the cache row again after entering it.
  const tZeroStr = form.elements.tZero.value;
  if (tZeroStr && w.fromIso && w.toIso) {
    try {
      const tai = parseClientIso(tZeroStr);
      const tZeroUtc = form.elements.tZeroUtc.checked ? tai : tai - 37_000;
      const fromMs = Date.parse(w.fromIso);
      const toMs = Date.parse(w.toIso);
      form.elements.windowBefore.value = ((tZeroUtc - fromMs) / 1000).toFixed(1);
      form.elements.windowAfter.value = ((toMs - tZeroUtc) / 1000).toFixed(1);
    } catch (_) { /* leave defaults */ }
  }
  const msg = document.getElementById('fetch-message');
  msg.textContent = `Settings copied from ${w.windowDir}. Type a dataId + t₀ if you haven't already and hit Fetch.`;
}

function parseClientIso(s) {
  // Loose ISO parser — accepts "YYYY-MM-DDTHH:MM:SS[.fff]" with optional Z.
  // We hand the actual parsing to Date.parse but fall back to a manual
  // path if the browser is stricter than expected.
  if (!s.endsWith('Z') && !/[+-]\d\d:?\d\d$/.test(s)) s = s + 'Z';
  const t = Date.parse(s);
  if (!Number.isFinite(t)) throw new Error('bad ISO');
  return t;
}

// ----- fetch + progress -----------------------------------------------------

let activeJobId = null;
let activeEventSource = null;

async function startFetch(ev) {
  ev.preventDefault();
  if (activeJobId) return;  // ignore double-submit
  const values = readFormValues();
  if (!Number.isFinite(values.exposureId)) {
    showMessage('exposureId must be an integer.', true);
    return;
  }
  if (!values.tZero) {
    showMessage('tZero is required.', true);
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
    submit.disabled = false;
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
    } else if (ev.type === 'done') {
      logProgress(`done in ${ev.elapsedS.toFixed(1)}s — ${ev.podCount} pods (${humanBytes(ev.totalBytes)})`);
      document.getElementById('progress-fill').style.width = '100%';
      document.getElementById('progress-text').textContent =
        `done — opening explore view ...`;
      es.close();
      activeEventSource = null;
      activeJobId = null;
      document.getElementById('fetch-submit').disabled = false;
      transitionToExplore();
    } else if (ev.type === 'error') {
      logProgress(`ERROR: ${ev.error}`);
      showMessage(`Fetch failed: ${ev.error.split('\n')[0]}`, true);
      es.close();
      activeEventSource = null;
      activeJobId = null;
      document.getElementById('fetch-submit').disabled = false;
    }
  };
  es.onerror = () => {
    // EventSource auto-retries on network blips; we only force-close if
    // the job has already finished.
  };
}

async function transitionToExplore() {
  try {
    const r = await fetch('/api/summary');
    const summary = await r.json();
    if (!summary.loaded) {
      showMessage('Fetch finished but the server reports no loaded exposure?', true);
      return;
    }
    if (window.showExplore) window.showExplore(summary);
  } catch (e) {
    showMessage(`Could not load summary: ${e}`, true);
  }
}

// ----- wiring --------------------------------------------------------------

function wireHomeListeners() {
  document.getElementById('fetch-form').addEventListener('submit', startFetch);
  document.getElementById('creds-form').elements.remember.addEventListener('change', () => {
    saveCreds();
  });
  document.getElementById('creds-forget').addEventListener('click', forgetCreds);
  document.getElementById('cache-refresh').addEventListener('click', refreshCache);
  homeListenersWired = true;
}
