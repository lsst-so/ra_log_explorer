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
  // Loki creds (still per-user-browser).
  username: 'ra_log_explorer.username',
  password: 'ra_log_explorer.password',
  remember: 'ra_log_explorer.remember',
  // App settings shared across both fetchers. Lives in localStorage
  // because they're per-user-browser; maxCacheGiB is also POSTed
  // server-side so the cache eviction can act on it. Cluster /
  // namespace / Loki URL / ConsDB token file all moved into the
  // server-side site catalog (sites.toml) and are picked via the
  // top-bar site switcher.
  settings: 'ra_log_explorer.settings',  // JSON {workers, maxCacheGiB, cacheDir}
  // Which site the user last picked in the top-bar switcher.
  site: 'ra_log_explorer.site',  // bare site name string
  // Last per-exposure tuning (windowBefore/After) keyed off the
  // exposure form alone.
  lastExpTuning: 'ra_log_explorer.lastExpTuning',  // {windowBefore, windowAfter}
};

// Application defaults for the user-tunable knobs (everything left
// after cluster/namespace/Loki URL/ConsDB token moved into the site
// catalog). Kept in sync with config.DEFAULT_* and DEFAULT_MAX_CACHE_BYTES.
const SETTINGS_DEFAULTS = {
  workers: 8,
  maxCacheGiB: 5,
  cacheDir: '',
};

// Site catalog as returned by /api/sites: {default_site, sites: [...]}.
// Loaded once at startup; the switcher renders from it and the rest of
// the home view reads ``activeSite`` for `site` request fields.
let siteCatalog = null;
let activeSite = null;
let siteSwitcherWired = false;  // guard: startHome() re-runs on every back-home

let homeListenersWired = false;
let resolvedTZero = null;       // last looked-up ISOT string (TAI) for the current dataId
let resolvedForExpId = null;    // the exposureId resolvedTZero corresponds to
let lookupTimer = null;         // debounce timer for the dataId input
let lookupSeq = 0;              // sequence number to ignore stale lookup responses

function startHome() {
  if (!homeListenersWired) wireHomeListeners();
  prefillSettings();
  prefillForm();
  prefillCreds();
  loadSiteCatalog();
  refreshCache();
  // URL-driven entry. We land here either via a deep-link
  // (/?dataId=…&autoFetch=1) or because the URL points at a key the
  // server hasn't loaded yet — in both cases we want the form
  // prefilled so a one-click fetch reproduces what the URL implies.
  const params = new URLSearchParams(window.location.search);
  const urlDataId = params.get('dataId');
  const urlDayObs = params.get('dayObs');
  const urlRangeStart = params.get('rangeStart');
  const urlRangeStop = params.get('rangeStop');
  const urlAutoFetch = params.get('autoFetch') === '1';
  if (urlDataId) {
    document.getElementById('fetch-form').elements.exposureId.value = urlDataId;
  }
  if (urlDayObs) {
    document.getElementById('night-form').elements.dayObs.value = urlDayObs;
  }
  if (urlRangeStart) {
    document.getElementById('range-form').elements.rangeStart.value = urlRangeStart;
  }
  if (urlRangeStop) {
    document.getElementById('range-form').elements.rangeStop.value = urlRangeStop;
  }
  // If the forms already have ids pre-filled (URL or localStorage), kick
  // the lookups so the submit buttons are ready to fire immediately.
  triggerLookupIfReady();
  triggerRangeLookupsIfReady();
  if (urlAutoFetch && urlDataId) waitAndAutoFetch(parseInt(urlDataId, 10));
}
window.startHome = startHome;

async function loadSiteCatalog() {
  // The site catalog is the same for every user of this deployment
  // (it's checked into sites.toml on the server). Pull it once: the
  // switcher's <option>s, the active-site state, and the change listener
  // all live in the persistent DOM / module scope, so re-running on a
  // later back-home would only leak a duplicate listener — guard it.
  if (siteSwitcherWired) return;
  try {
    const r = await fetch('/api/sites');
    if (!r.ok) return;
    siteCatalog = await r.json();
  } catch (_) {
    return;
  }
  if (!siteCatalog || !Array.isArray(siteCatalog.sites) || !siteCatalog.sites.length) return;
  const sel = document.getElementById('site-select');
  sel.innerHTML = '';
  for (const s of siteCatalog.sites) {
    const opt = document.createElement('option');
    opt.value = s.name;
    opt.textContent = `${s.name} (${s.cluster})`;
    sel.appendChild(opt);
  }
  const stored = localStorage.getItem(LS.site);
  const startName = siteCatalog.sites.find(s => s.name === stored)
    ? stored
    : siteCatalog.default_site;
  sel.value = startName;
  setActiveSite(startName);
  document.getElementById('site-switcher').hidden = false;
  sel.addEventListener('change', () => {
    setActiveSite(sel.value);
    localStorage.setItem(LS.site, sel.value);
    // A site switch changes which ConsDB the lookup hits AND which
    // per-site exposure-time cache we read, so any pending dataId
    // resolution must re-run.
    clearResolvedTZero();
    clearRangeSlot(rangeStartSlot);
    clearRangeSlot(rangeStopSlot);
    triggerLookupIfReady();
    triggerRangeLookupsIfReady();
    refreshCache();
  });
  siteSwitcherWired = true;
}

function setActiveSite(name) {
  if (!siteCatalog) return;
  activeSite = siteCatalog.sites.find(s => s.name === name) || null;
  // Drive the per-site CSS accent (border-top strip, switcher chip).
  // Persists across home/explore/night views — the strip is visible
  // even when the switcher itself isn't on screen.
  if (activeSite) {
    document.body.dataset.site = activeSite.name;
  } else {
    delete document.body.dataset.site;
  }
  const info = document.getElementById('site-info');
  if (info && activeSite) {
    info.textContent = `consdb=${activeSite.consdbUrl.replace(/^https?:\/\//, '')}`;
  }
}

function waitAndAutoFetch(expId) {
  // Wait for the shutter-close lookup to resolve, then submit the
  // exposure form on the user's behalf. We poll every 200ms with a
  // 30-second cap so a missing RSP token or an unknown dataId
  // surfaces normally rather than hanging silently.
  let elapsed = 0;
  const maxMs = 30_000;
  const tick = setInterval(() => {
    elapsed += 200;
    if (resolvedForExpId === expId && resolvedTZero) {
      clearInterval(tick);
      const form = document.getElementById('fetch-form');
      if (form && !document.getElementById('fetch-submit').disabled) {
        showMessage('Auto-fetching from night-view drilldown...');
        form.requestSubmit();
      }
    } else if (elapsed >= maxMs) {
      clearInterval(tick);
    }
  }, 200);
}

function readSettings() {
  return { ...SETTINGS_DEFAULTS, ...(readJson(LS.settings) || {}) };
}

function prefillSettings() {
  // Settings panel: app-wide knobs that both fetchers (and the cache
  // eviction layer) read from. Persisted in localStorage; the
  // maxCacheGiB value is also pushed to the server so its eviction
  // pass uses the latest limit.
  const s = readSettings();
  const form = document.getElementById('settings-form');
  for (const k of Object.keys(SETTINGS_DEFAULTS)) {
    const el = form.elements.namedItem(k);
    if (el) el.value = s[k] != null ? s[k] : SETTINGS_DEFAULTS[k];
  }
  // Reflect what the server currently believes the cache limit + the
  // persisted cacheDir override are. ``effectiveCacheRoot`` is the
  // path the server will *actually* use next time (env-var > setting >
  // default) — render it as a hint so the user can see where their
  // typed input resolves to, including the case where it's overridden
  // by RA_LOG_EXPLORER_CACHE.
  fetch('/api/settings').then(async (r) => {
    if (!r.ok) return;
    const data = await r.json();
    const gib = Math.round((data.maxCacheBytes / (1024 ** 3)) * 100) / 100;
    if (gib !== parseFloat(form.elements.maxCacheGiB.value)) {
      form.elements.maxCacheGiB.value = gib;
      saveSettings();  // sync localStorage to the server's value
    }
    if (data.cacheDir !== form.elements.cacheDir.value) {
      form.elements.cacheDir.value = data.cacheDir || '';
      saveSettings();
    }
    updateCacheDirHint(data.effectiveCacheRoot);
  }).catch(() => { /* offline — leave the form alone */ });
}

function updateCacheDirHint(effective) {
  const el = document.getElementById('cache-dir-effective');
  if (!el) return;
  if (!effective) { el.textContent = ''; return; }
  el.textContent = `currently using: ${effective}`;
}

function saveSettings() {
  const form = document.getElementById('settings-form');
  const s = {};
  for (const k of Object.keys(SETTINGS_DEFAULTS)) {
    const el = form.elements.namedItem(k);
    if (!el) continue;
    s[k] = el.type === 'number' ? parseFloat(el.value) : el.value.trim();
  }
  localStorage.setItem(LS.settings, JSON.stringify(s));
  // Push the server-side fields (cache size + cache root override).
  // Other fields are client-side only. Errors are surfaced via
  // #settings-state but don't block anything else.
  const stateEl = document.getElementById('settings-state');
  const bytes = Math.round((s.maxCacheGiB || 0) * (1024 ** 3));
  fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ maxCacheBytes: bytes, cacheDir: s.cacheDir || null }),
  }).then(async (r) => {
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      stateEl.textContent = `(server: ${body.error || `HTTP ${r.status}`})`;
      stateEl.classList.add('error');
      return;
    }
    const data = await r.json().catch(() => ({}));
    if (data.effectiveCacheRoot) updateCacheDirHint(data.effectiveCacheRoot);
    refreshCache();  // the cache listing comes from the new root
    stateEl.textContent = '(saved)';
    stateEl.classList.remove('error');
    setTimeout(() => { stateEl.textContent = ''; }, 1500);
  }).catch((e) => {
    stateEl.textContent = `(server unreachable: ${e})`;
  });
}

function prefillForm() {
  // Per-exposure tuning (windowBefore/After) only — cluster/namespace/
  // workers are in the settings panel.
  const last = readJson(LS.lastExpTuning);
  if (!last) return;
  const form = document.getElementById('fetch-form');
  for (const [k, v] of Object.entries(last)) {
    const el = form.elements.namedItem(k);
    if (el) el.value = v;
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

function saveExpTuning() {
  // Just the two per-exposure tuning knobs. cluster/namespace/workers
  // are global settings and persisted separately.
  const f = document.getElementById('fetch-form');
  localStorage.setItem(
    LS.lastExpTuning,
    JSON.stringify({
      windowBefore: f.elements.windowBefore.value,
      windowAfter: f.elements.windowAfter.value,
    }),
  );
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
  // Merge in the app-wide knobs that no longer live on the per-fetch
  // form. cluster / namespace / Loki URL are derived from the
  // currently-selected site on the server side (we just pass `site`).
  const s = readSettings();
  out.site = activeSite ? activeSite.name : undefined;
  out.workers = parseInt(s.workers, 10);
  out.exposureId = parseInt(out.exposureId, 10);
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
  // The site picks which ConsDB to ask AND which per-site cache file
  // the resolved obs_end lands in. Falls through to the server's
  // default_site if we haven't loaded the catalog yet (rare race on
  // first paint).
  const siteName = activeSite ? activeSite.name : '';
  const qs = siteName ? `?site=${encodeURIComponent(siteName)}` : '';
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

// ----- range exposure-time lookups ----------------------------------------

// One slot per range endpoint. Each resolves its own dataId -> shutter
// close (TAI) independently, mirroring the single-exposure lookup but
// driving the two range inputs. `seq` guards against stale responses;
// `timer` is the per-field debounce.
const rangeStartSlot = { tZero: null, forId: null, seq: 0, timer: null, input: 'rangeStart', status: 'range-start-status' };
const rangeStopSlot = { tZero: null, forId: null, seq: 0, timer: null, input: 'rangeStop', status: 'range-stop-status' };

function setRangeStatus(statusId, text, kind) {
  const el = document.getElementById(statusId);
  el.textContent = text;
  el.classList.remove('ok', 'error');
  if (kind === 'ok') el.classList.add('ok');
  if (kind === 'error') el.classList.add('error');
  updateRangeSubmit();
}

function clearRangeSlot(slot) {
  slot.tZero = null;
  slot.forId = null;
}

function updateRangeSubmit() {
  const submit = document.getElementById('range-submit');
  submit.disabled = !(rangeStartSlot.tZero && rangeStopSlot.tZero);
}

function triggerRangeLookup(slot) {
  const raw = document.getElementById('range-form').elements[slot.input].value.trim();
  if (!raw) {
    clearRangeSlot(slot);
    setRangeStatus(slot.status, `enter a ${DATAID_LENGTH}-digit dataId`, 'info');
    return;
  }
  if (raw.length !== DATAID_LENGTH) {
    clearRangeSlot(slot);
    const over = raw.length > DATAID_LENGTH;
    setRangeStatus(slot.status, `dataIds are ${DATAID_LENGTH} digits (have ${raw.length})`, over ? 'error' : 'info');
    return;
  }
  const expId = parseInt(raw, 10);
  if (!Number.isFinite(expId)) {
    clearRangeSlot(slot);
    setRangeStatus(slot.status, 'dataId must be an integer', 'error');
    return;
  }
  if (slot.forId === expId && slot.tZero) {
    setRangeStatus(slot.status, `t₀ (TAI): ${slot.tZero}`, 'ok');
    return;
  }
  setRangeStatus(slot.status, `looking up ${expId}…`, 'info');
  const mySeq = ++slot.seq;
  const siteName = activeSite ? activeSite.name : '';
  const qs = siteName ? `?site=${encodeURIComponent(siteName)}` : '';
  fetch(`/api/exposure-time/${expId}${qs}`)
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (mySeq !== slot.seq) return;  // stale; user typed something newer
      if (r.ok && body.tZero) {
        slot.tZero = body.tZero;
        slot.forId = expId;
        setRangeStatus(slot.status, `t₀ (TAI): ${body.tZero}`, 'ok');
      } else if (r.status === 404) {
        clearRangeSlot(slot);
        setRangeStatus(slot.status, `no exposure-time record for ${expId}`, 'error');
      } else if (r.status === 503) {
        clearRangeSlot(slot);
        setRangeStatus(slot.status, body.error || 'RSP token / ConsDB lookup not configured', 'error');
      } else {
        clearRangeSlot(slot);
        setRangeStatus(slot.status, `lookup failed: ${body.error || r.status}`, 'error');
      }
    })
    .catch((e) => {
      if (mySeq !== slot.seq) return;
      clearRangeSlot(slot);
      setRangeStatus(slot.status, `lookup failed: ${e}`, 'error');
    });
}

function scheduleRangeLookup(slot) {
  clearRangeSlot(slot);
  setRangeStatus(slot.status, 'typing...', 'info');
  if (slot.timer) clearTimeout(slot.timer);
  slot.timer = setTimeout(() => triggerRangeLookup(slot), 300);
}

function triggerRangeLookupsIfReady() {
  // Fire both lookups for whatever's currently in the inputs (used on
  // first paint when the form is prefilled and after a site switch).
  triggerRangeLookup(rangeStartSlot);
  triggerRangeLookup(rangeStopSlot);
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
    const isNight = w.kind === 'night';
    const isRange = w.kind === 'range';
    const url = cacheRowUrl(w);
    tr.title = url
      ? 'click to open this cached run in a new tab'
      : 'click to copy this window into the exposure form';
    const fromS = (w.fromIso || '').replace('T', ' ').replace(/\..*Z$/, '');
    const toS = (w.toIso || '').replace('T', ' ').replace(/\..*Z$/, '');
    const fetchedS = (w.fetchedAt || '').replace('T', ' ').replace(/\..*$/, '');
    const lastViewedS = (w.lastViewedAt || '').replace('T', ' ').replace(/\..*$/, '');
    let kindBadge;
    if (isNight) {
      kindBadge = `<span class="cache-kind cache-kind-night" title="night-mode AOS-only fetch">night</span>`;
    } else if (isRange) {
      kindBadge = `<span class="cache-kind cache-kind-range" title="range fetch — one wide window over [start, stop]">range</span>`;
    } else {
      kindBadge = `<span class="cache-kind cache-kind-exposure">exposure</span>`;
    }
    // The "key" column shows the most useful identifier for the row's
    // kind. Night caches have a single dayObs (computed by the server
    // from the noon-UTC start). Range caches show their seq-number span
    // (linking back to the range view). Exposure caches carry one *or
    // more* dataIds — superset reuse means consecutive fetches often
    // land on the same cache, so we render every dataId that's known to
    // have triggered this cache as a clickable link.
    let keyCell;
    if (isNight) {
      keyCell = `<a class="mono cache-key-link" href="${escapeHtml(url || '#')}" target="_blank" rel="noopener">${w.dayObs}</a>`;
    } else if (isRange) {
      // Full start/stop dataIds on two lines — the seq-only form hid the
      // dayObs (the leading 8 digits of the id), which made the row
      // ambiguous about which night it covers.
      keyCell = `<a class="mono cache-key-link cache-key-range" href="${escapeHtml(url || '#')}" target="_blank" rel="noopener" title="range ${w.rangeStart} → ${w.rangeStop}">${w.rangeStart}<br>→ ${w.rangeStop}</a>`;
    } else if (w.exposureIds && w.exposureIds.length > 0) {
      keyCell = w.exposureIds
        .map((id) => `<a class="mono cache-key-link" href="/?dataId=${encodeURIComponent(id)}" target="_blank" rel="noopener">${id}</a>`)
        .join(' ');
    } else {
      keyCell = `<span class="muted">—</span>`;
    }
    tr.innerHTML = `
      <td>${w.cluster} / ${w.namespace} ${kindBadge}</td>
      <td>${keyCell}</td>
      <td><span class="mono">${fromS} → ${toS}</span></td>
      <td><span class="mono">${fetchedS}</span></td>
      <td><span class="mono">${lastViewedS || '<span class="muted">never</span>'}</span></td>
      <td>${w.podCount}</td>
      <td>${humanBytes(w.sizeOnDisk)}</td>
      <td><button type="button" class="ghost mini delete" title="delete this cached window">✕</button></td>`;
    // Row click: for night rows, navigate; for exposure rows, copy
    // window-before/after. The trailing ✕ button has its own handler
    // that stopPropagation()s so it doesn't trigger this. The key
    // cell's own <a> handles its target=_blank navigation; we don't
    // double-trigger the row click for it.
    tr.addEventListener('click', (ev) => {
      if (ev.target instanceof HTMLAnchorElement) return;
      useCacheSettings(w);
    });
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
      `<td colspan="5" class="total-label">total</td>`
      + `<td>${data.windows.length}</td>`
      + `<td>${humanBytes(sumBytes)}</td>`
      + `<td></td>`;
    tfoot.appendChild(tr);
  }
}

async function deleteCacheWindow(w) {
  const subPath = w.relPath || w.windowDir;
  if (!window.confirm(
    `Delete cached window?\n\n${w.cluster}/${w.namespace}/${subPath}\n(${humanBytes(w.sizeOnDisk)})`,
  )) return;
  // For night-mode caches relPath is `<window>/<pods=…>` — we need two
  // URL segments rather than one. encodeURIComponent each segment so
  // the `=` in `pods=__aos__` survives intact.
  const segments = subPath.split('/').map(encodeURIComponent).join('/');
  const url = `/api/cache/${encodeURIComponent(w.cluster)}/${encodeURIComponent(w.namespace)}/${segments}`;
  try {
    const r = await fetch(url, { method: 'DELETE' });
    if (r.status === 404) {
      // Dir is already gone (deleted out of band, or from another tab).
      // Re-fetch the listing so the stale row disappears instead of
      // leaving the user clicking ✕ on a phantom.
      await refreshCache();
      return;
    }
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
  const info = document.getElementById('cache-summary').textContent;
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]
  ));
}

function dayObsFromIso(fromIso) {
  // The night window starts at noon UTC on dayObs. Pull the YYYY-MM-DD
  // out of the from-ISO and pack it back into a YYYYMMDD integer.
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(fromIso || '');
  if (!m) return null;
  return parseInt(`${m[1]}${m[2]}${m[3]}`, 10);
}

function cacheRowUrl(w) {
  // The deep-link a cache row navigates to. For night caches that's
  // /?dayObs=Y; for exposure caches we don't have a single dataId in
  // the cache meta (multiple visits may share a window), so we copy
  // the windowBefore/After + cluster/ns into the form and the user
  // submits manually. Returns null for that case.
  if (w.kind === 'night' && w.dayObs != null) {
    return `/?dayObs=${encodeURIComponent(w.dayObs)}`;
  }
  if (w.kind === 'range' && w.rangeStart != null && w.rangeStop != null) {
    return `/?rangeStart=${encodeURIComponent(w.rangeStart)}&rangeStop=${encodeURIComponent(w.rangeStop)}`;
  }
  return null;
}

function useCacheSettings(w) {
  // Night caches deep-link to their already-cached view. Open in a
  // new tab so the current tab is preserved.
  const url = cacheRowUrl(w);
  if (url) {
    window.open(url, '_blank', 'noopener');
    return;
  }
  // Exposure caches don't carry a single dataId — fall back to the
  // old behaviour of copying the window into the form so the user
  // can type a matching dataId and submit.
  const form = document.getElementById('fetch-form');
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
  msg.textContent = `Window from ${w.windowDir} copied. ` +
    (resolvedTZero
      ? 'Hit Fetch to reopen.'
      : 'Type a matching dataId, then Fetch.');
}

// ----- fetch + progress ---------------------------------------------------

let activeJobId = null;
let activeEventSource = null;
// Which card launched the active fetch. The inline progress region for
// each form is rendered next to its own submit button, so when an SSE
// update comes in we know which DOM element to update without having
// to look up the job kind on the server.
let activeProgressKind = null;  // 'exposure' | 'night' | null

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
  saveExpTuning();

  const submit = document.getElementById('fetch-submit');
  submit.disabled = true;
  showMessage('Starting fetch...');
  activeProgressKind = 'exposure';
  showInlineProgress('exposure', true);
  resetInlineProgress('exposure');

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
    activeProgressKind = null;
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

function progressEls(kind) {
  // Each form hosts an identically-shaped inline progress region. Look
  // up the wrapper and its children by kind so the SSE consumer can
  // route updates to whichever card launched the active job.
  const id = kind === 'night' ? 'night-progress' : kind === 'range' ? 'range-progress' : 'fetch-progress';
  const wrap = document.getElementById(id);
  return {
    wrap,
    fill: wrap.querySelector('.progress-fill'),
    text: wrap.querySelector('.progress-text'),
  };
}

function showInlineProgress(kind, visible) {
  progressEls(kind).wrap.hidden = !visible;
}

function resetInlineProgress(kind) {
  const els = progressEls(kind);
  els.fill.style.width = '0%';
  els.text.textContent = '';
}

function setProgressText(text) {
  if (!activeProgressKind) return;
  progressEls(activeProgressKind).text.textContent = text;
}

function setProgressFill(pct) {
  if (!activeProgressKind) return;
  progressEls(activeProgressKind).fill.style.width = pct;
}

function openProgressStream(jobId) {
  if (activeEventSource) activeEventSource.close();
  const es = new EventSource(`/api/fetch/${jobId}/progress`);
  activeEventSource = es;
  let total = 0, done = 0;
  let windowStr = '';  // remembered between events so each pod-count update can re-include the window
  es.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch (_) { return; }
    if (ev.type === 'start') {
      const fromS = (ev.fromIso || '').replace('T', ' ').replace(/\..*Z$/, '');
      const toS = (ev.toIso || '').replace('T', ' ').replace(/\..*Z$/, '');
      windowStr = `${fromS} → ${toS}`;
      setProgressText(`window ${windowStr}`);
    } else if (ev.type === 'pod-done') {
      total = ev.total;
      done = ev.i;
      const pct = total ? (done / total) * 100 : 0;
      setProgressFill(pct.toFixed(1) + '%');
      setProgressText(`${windowStr}  ·  ${done}/${total} pods fetched`);
    } else if (ev.type === 'parsing') {
      setProgressText(
        `${windowStr}  ·  parsing (${ev.podCount} pods, ${humanBytes(ev.totalBytes)}; ${ev.cacheReuse})`,
      );
    } else if (ev.type === 'shutter-close') {
      // Night-mode only: post-parse pass resolving shutter close times for
      // each dataId. May involve a batched ConsDB call.
      let phaseMsg = '';
      if (ev.phase === 'starting') {
        phaseMsg = `resolving shutter close for ${ev.total} dataIds…`;
      } else if (ev.phase === 'cache-checked') {
        phaseMsg = `cache: ${ev.cacheHits} hits, ${ev.remaining} to query`;
      } else if (ev.phase === 'done') {
        phaseMsg = `ConsDB: ${ev.consdbHits} resolved, ${ev.stillMissing} still missing`;
      } else if (ev.phase === 'no-token' || ev.phase === 'empty-token') {
        phaseMsg = `no RSP token — ${ev.remaining} dataIds will be missing from Δshutter histograms`;
      } else if (ev.phase === 'consdb-error') {
        phaseMsg = `ConsDB query failed: ${ev.error}`;
      }
      if (phaseMsg) setProgressText(`${windowStr}  ·  ${phaseMsg}`);
    } else if (ev.type === 'done') {
      setProgressFill('100%');
      const target = ev.kind === 'night' ? 'night view' : ev.kind === 'range' ? 'range view' : 'explore view';
      setProgressText(`done in ${ev.elapsedS.toFixed(1)}s — opening ${target} …`);
      es.close();
      activeEventSource = null;
      activeJobId = null;
      updateSubmitButton();
      document.getElementById('night-submit').disabled = false;
      document.getElementById('range-submit').disabled = false;
      transitionToExplore({
        kind: ev.kind, expId: ev.expId, dayObs: ev.dayObs, startId: ev.startId, stopId: ev.stopId,
      });
    } else if (ev.type === 'error') {
      setProgressText(`ERROR: ${ev.error.split('\n')[0]}`);
      showMessage(`Fetch failed: ${ev.error.split('\n')[0]}`, true);
      for (const msgId of ['night-message', 'range-message']) {
        const m = document.getElementById(msgId);
        if (m) {
          m.textContent = `Fetch failed: ${ev.error.split('\n')[0]}`;
          m.classList.add('error');
        }
      }
      document.getElementById('night-submit').disabled = false;
      document.getElementById('range-submit').disabled = false;
      es.close();
      activeEventSource = null;
      activeJobId = null;
      activeProgressKind = null;
      updateSubmitButton();
    }
  };
  es.onerror = () => {};
}

async function transitionToExplore(activeJob) {
  // The completed job carries the key we need — read it before asking
  // /api/summary so we route to the right loaded state on the server.
  // Without this the request would be context-less and the server
  // couldn't tell us which exposure / night to summarise.
  let params;
  if (activeJob && activeJob.kind === 'night') {
    params = `dayObs=${encodeURIComponent(activeJob.dayObs)}`;
  } else if (activeJob && activeJob.kind === 'range') {
    params = `rangeStart=${encodeURIComponent(activeJob.startId)}`
      + `&rangeStop=${encodeURIComponent(activeJob.stopId)}`;
  } else {
    params = `dataId=${encodeURIComponent(activeJob.expId)}`;
  }
  try {
    const r = await fetch(`/api/summary?${params}`);
    const summary = await r.json();
    if (!summary.loaded) {
      showMessage('Fetch finished but the server reports no loaded data?', true);
      return;
    }
    // Reflect the loaded state in the URL so a refresh / bookmark
    // lands on the same view, and so opening this URL in a new tab
    // independently routes to it.
    const newUrl = `${window.location.pathname}?${params}`;
    window.history.replaceState({}, '', newUrl);
    if (summary.mode === 'night' && window.showNight) {
      window.showNight(summary);
    } else if (summary.mode === 'range' && window.showRange) {
      window.showRange(summary);
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
  const s = readSettings();
  const body = {
    dayObs,
    site: activeSite ? activeSite.name : undefined,
    workers: parseInt(s.workers, 10) || undefined,
    username: credsForm.elements.username.value.trim() || undefined,
    password: credsForm.elements.password.value || undefined,
  };
  const submit = document.getElementById('night-submit');
  submit.disabled = true;
  const msgEl = document.getElementById('night-message');
  msgEl.textContent = 'Starting night fetch...';
  msgEl.classList.remove('error');
  activeProgressKind = 'night';
  showInlineProgress('night', true);
  resetInlineProgress('night');

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

async function startRangeFetch(ev) {
  ev.preventDefault();
  if (activeJobId) return;
  const msgEl = document.getElementById('range-message');
  if (!(rangeStartSlot.tZero && rangeStopSlot.tZero)) {
    msgEl.textContent = 'Resolve both start and stop shutter-close times first.';
    msgEl.classList.add('error');
    return;
  }
  if (rangeStopSlot.forId <= rangeStartSlot.forId) {
    msgEl.textContent = 'Stop dataId must be greater than start dataId.';
    msgEl.classList.add('error');
    return;
  }
  saveCreds();
  const form = document.getElementById('range-form');
  const credsForm = document.getElementById('creds-form');
  const s = readSettings();
  const body = {
    rangeStart: rangeStartSlot.forId,
    rangeStop: rangeStopSlot.forId,
    // Always TAI; the server applies the -37 s conversion (same contract
    // as the single-exposure form).
    tZeroStart: rangeStartSlot.tZero,
    tZeroStop: rangeStopSlot.tZero,
    site: activeSite ? activeSite.name : undefined,
    workers: parseInt(s.workers, 10) || undefined,
    windowBefore: parseFloat(form.elements.windowBefore.value),
    windowAfter: parseFloat(form.elements.windowAfter.value),
    username: credsForm.elements.username.value.trim() || undefined,
    password: credsForm.elements.password.value || undefined,
  };

  const submit = document.getElementById('range-submit');
  submit.disabled = true;
  msgEl.textContent = 'Starting range fetch...';
  msgEl.classList.remove('error');
  activeProgressKind = 'range';
  showInlineProgress('range', true);
  resetInlineProgress('range');

  let jobId;
  try {
    const r = await fetch('/api/fetch-range', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    jobId = data.jobId;
  } catch (e) {
    msgEl.textContent = `Failed to start range fetch: ${e.message || e}`;
    msgEl.classList.add('error');
    submit.disabled = false;
    activeProgressKind = null;
    return;
  }
  activeJobId = jobId;
  openProgressStream(jobId);
}

// ----- wiring -------------------------------------------------------------

function wireHomeListeners() {
  document.getElementById('fetch-form').addEventListener('submit', startFetch);
  document.getElementById('night-form').addEventListener('submit', startNightFetch);
  document.getElementById('range-form').addEventListener('submit', startRangeFetch);
  const expIdInput = document.getElementById('fetch-form').elements.exposureId;
  expIdInput.addEventListener('input', scheduleLookup);
  const rangeForm = document.getElementById('range-form');
  rangeForm.elements.rangeStart.addEventListener('input', () => scheduleRangeLookup(rangeStartSlot));
  rangeForm.elements.rangeStop.addEventListener('input', () => scheduleRangeLookup(rangeStopSlot));
  document.getElementById('creds-form').elements.remember.addEventListener('change', saveCreds);
  document.getElementById('creds-forget').addEventListener('click', forgetCreds);
  // Save settings on every input — they're tiny, latency-free, and the
  // user expects "I changed it" to mean "it's saved".
  const settingsForm = document.getElementById('settings-form');
  for (const el of settingsForm.querySelectorAll('input')) {
    el.addEventListener('input', saveSettings);
  }
  document.getElementById('cache-refresh').addEventListener('click', refreshCache);
  document.getElementById('cache-delete-all').addEventListener('click', deleteAllCache);
  homeListenersWired = true;
  updateSubmitButton();  // start with submit disabled until lookup resolves
  updateRangeSubmit();   // same for the range card
}
