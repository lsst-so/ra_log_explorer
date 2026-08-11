/* ra-log-explorer — home view: pick an exposure, inspect the cache, watch
 * a fetch happen.
 *
 * The user types a dataId; the browser resolves it to its TAI shutter-
 * close timestamp via /api/exposure-time/<dataId> and keeps that value
 * in memory until submit. The user never types or sees a UTC/TAI
 * choice — exposure timings always come from the same well-known
 * service, which always returns TAI.
 */
'use strict';

const LS = {
  // Last per-exposure tuning (windowBefore/After) and the last-used
  // instrument. The only things the home view still remembers per
  // browser: everything else that used to live here — credentials,
  // worker count, cache size and location, which site to query — is
  // deployment configuration the server reads from its environment,
  // not something a visitor gets to set.
  lastExpTuning: 'ra_log_explorer.lastExpTuning',  // {windowBefore, windowAfter}
  instrument: 'ra_log_explorer.instrument',        // 'lsstcam' | 'latiss'
};

function readLsInstrument() {
  try { return localStorage.getItem(LS.instrument) || ''; } catch (_) { return ''; }
}

// This deployment's site, from /api/site. Read-only: it labels the page so
// nobody mistakes summit data for BTS data, and it is decided by where the
// server runs.
let site = null;

let homeListenersWired = false;
let resolvedTZero = null;       // last looked-up ISOT string (TAI) for the current dataId
let resolvedForExpId = null;    // the exposureId resolvedTZero corresponds to
// The instrument resolvedTZero was resolved under. A shutter close is
// only meaningful for one (instrument, dataId) pair — the same 13-digit
// id names a different exposure on each instrument, an hour apart — so
// every consumer checks this before using the value. Carrying it on the
// resolution is what makes a stale t0 impossible to submit under a
// different pin, no matter which path changed the pin.
let resolvedForInstrument = null;
let tZeroIsManual = false;      // true when resolvedTZero was hand-entered (ConsDB couldn't resolve it)
let lookupTimer = null;         // debounce timer for the dataId input
let lookupSeq = 0;              // sequence number to ignore stale lookup responses
// The page-level instrument context. A dataId alone does not name an
// exposure: every instrument numbers from 1 each night, so on a night
// where LSSTCam and LATISS both observe, the same id exists on both
// with different shutter closes — the two must never be mixed. The
// topbar switch pins the whole page (Tonight list, lookups, fetches)
// to exactly one instrument; LSSTCam is always the default.
const INSTRUMENTS = ['lsstcam', 'latiss'];
let pageInstrument = 'lsstcam';

function getInstrument() {
  return pageInstrument;
}

function setInstrument(inst, opts) {
  inst = (inst || '').toLowerCase();
  if (!INSTRUMENTS.includes(inst)) inst = 'lsstcam';
  const changed = inst !== pageInstrument;
  pageInstrument = inst;
  try { localStorage.setItem(LS.instrument, inst); } catch (_) { /* private mode */ }
  for (const b of document.querySelectorAll('#instrument-switch button')) {
    b.classList.toggle('active', b.dataset.instrument === inst);
  }
  // Keep the URL in step so a reload (or a copied link) lands on the
  // same instrument.
  const url = new URL(window.location);
  url.searchParams.set('instrument', inst);
  if (changed && !(opts && opts.initial)) {
    // Call off a drilldown's autoFetch once the user takes the wheel. It
    // means "fetch the thing I clicked", and the thing they clicked was
    // an exposure of the *other* instrument.
    //
    // Both halves are needed. Off the URL, or a later reload silently
    // fires a fetch nobody asked for; and off the clock, because the
    // poller armed on arrival is still running and re-resolving under
    // the new pin is exactly what satisfies it — so the switch that was
    // meant to stop the auto-fetch would instead redirect it onto the
    // twin, within a second, with nothing else touched.
    url.searchParams.delete('autoFetch');
    cancelAutoFetch();
  }
  history.replaceState(null, '', url);
  // AOS (night mode) runs on LSSTCam only — its wavefront sensors live
  // in LSSTCam's corners — so the card has nothing to offer for LATISS.
  document.getElementById('night-card').hidden = inst !== 'lsstcam';
  if (changed) {
    // Same typed ids, different instrument => different exposures with
    // different shutter closes: drop every resolved t0 so nothing
    // resolved under the old pin can be carried into the new one.
    //
    // This runs on the initial pin too. `startHome` re-runs whenever a
    // view hands back to home, and the pin it reads can differ from the
    // one this document last used (another tab wrote localStorage, or
    // the URL carries a different instrument) — at which point the t0
    // still sitting in `resolvedTZero` belongs to the other instrument's
    // exposure.
    clearResolvedTZero();
    hideManualEntry();
    clearRangeSlot(rangeStartSlot);
    clearRangeSlot(rangeStopSlot);
    if (!(opts && opts.initial)) {
      // A deliberate switch re-resolves right away. The initial pin
      // doesn't: startHome fires the same triggers immediately after,
      // and doing it here as well would double every lookup on load.
      triggerLookupIfReady();
      triggerRangeLookupsIfReady();
      if (tonightLastLive) renderTonight(tonightLastLive);
    }
  }
}

function startHome() {
  // Whatever was armed belongs to the visit we just left; this one
  // re-arms below if its own URL asks for it.
  cancelAutoFetch();
  if (!homeListenersWired) wireHomeListeners();
  prefillForm();
  loadSite();
  refreshTonight();
  wireTonightTimer();
  // URL-driven entry. We land here either via a deep-link
  // (/?dataId=…&autoFetch=1) or because the URL points at a key the
  // server hasn't loaded yet — in both cases we want the form
  // prefilled so a one-click fetch reproduces what the URL implies.
  const params = new URLSearchParams(window.location.search);
  const urlDataId = params.get('dataId');
  // Instrument context: an explicit URL param (deep links from the
  // Tonight panel carry one) wins; otherwise whatever this browser used
  // last; otherwise LSSTCam.
  setInstrument(params.get('instrument') || readLsInstrument(), { initial: true });
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

async function loadSite() {
  // One request, once: which observatory's logs this server serves. There
  // is nothing to choose, so there is no listener to wire and no state to
  // guard against startHome() re-running on every back-home.
  try {
    const r = await fetch(apiUrl('/api/site'));
    if (!r.ok) return;
    site = await r.json();
  } catch (_) {
    return;
  }
  if (!site || !site.name) return;
  // Drives the per-site CSS accent (border-top strip, badge), which stays
  // visible across the home / explore / night views.
  document.body.dataset.site = site.name;
  document.getElementById('site-name').textContent = `${site.name} · ${site.cluster}`;
  const info = document.getElementById('site-info');
  if (info) info.textContent = `consdb=${site.consdbUrl.replace(/^https?:\/\//, '')}`;
  document.getElementById('site-badge').hidden = false;
}

// ----- tonight (live mode) -------------------------------------------------

// How many exposures the Tonight table shows before folding the rest into
// an expandable "…and N older" note — the panel is for what's happening
// *now*, and the full night is one click away.
const TONIGHT_MAX_ROWS = 20;
const TONIGHT_REFRESH_MS = 30_000;
let tonightTimerWired = false;
let tonightShowAll = false;   // toggled by the "…and N older" note
let tonightLastLive = null;   // last snapshot, so the toggle re-renders instantly

async function refreshTonight() {
  let live;
  try {
    const r = await fetch(apiUrl('/api/live'));
    if (!r.ok) return;
    live = await r.json();
  } catch (_) {
    return;  // leave whatever is rendered; next poll retries
  }
  renderTonight(live);
}

function wireTonightTimer() {
  if (tonightTimerWired) return;
  tonightTimerWired = true;
  setInterval(() => {
    // Only poll while the home view is actually on screen — an explore
    // tab doesn't need the panel refreshed behind its back.
    if (!document.getElementById('home-view').hidden) refreshTonight();
  }, TONIGHT_REFRESH_MS);
}

function renderTonight(live) {
  const card = document.getElementById('tonight-card');
  if (!live || !live.enabled) {
    card.hidden = true;
    return;
  }
  tonightLastLive = live;
  card.hidden = false;
  // Only the page's instrument: the same id in both lists would be two
  // different exposures, so mixing the lists would be actively wrong.
  const exposures = (live.exposures || []).filter(
    (e) => (e.instrument || 'lsstcam') === getInstrument(),
  );
  const nReady = exposures.filter((e) => e.ready).length;
  document.getElementById('tonight-summary').textContent =
    `dayObs ${live.dayObs ?? '?'} · ${exposures.length} ${getInstrument() === 'latiss' ? 'LATISS' : 'LSSTCam'} ` +
    `exposure${exposures.length === 1 ? '' : 's'}` +
    (exposures.length ? ` · ${nReady} viewable` : '');

  const watermarkS = (live.watermark || '').replace('T', ' ').replace(/\..*Z?$/, '');
  let statusText;
  if (live.finalised) {
    statusText = `night complete · logs fetched through ${watermarkS}Z`;
  } else if (live.catchingUp) {
    statusText = `catching up — logs fetched through ${watermarkS}Z so far`;
  } else {
    statusText = `logs fetched through ${watermarkS}Z · refreshes every ${Math.round(live.pollSeconds)}s`;
  }
  document.getElementById('tonight-status').textContent = statusText;

  const banner = document.getElementById('tonight-banner');
  const problems = [];
  const nErr = Object.keys(live.errors || {}).length;
  const nInc = Object.keys(live.incompletePods || {}).length;
  if (nErr) problems.push(`${nErr} pod${nErr === 1 ? '' : 's'} with fetch failures`);
  if (nInc) problems.push(`${nInc} pod${nInc === 1 ? '' : 's'} with unverifiable chunks`);
  if (live.consdbError) problems.push(`ConsDB: ${live.consdbError}`);
  if (live.orphanError) problems.push(`an earlier night could not be finalised: ${live.orphanError}`);
  if (live.lastError) problems.push('last poll cycle failed (see server log)');
  banner.hidden = problems.length === 0;
  banner.textContent = problems.length ? `Live fetch problems — ${problems.join(' · ')}` : '';

  const tbody = document.getElementById('tonight-tbody');
  tbody.innerHTML = '';
  const shown = tonightShowAll ? exposures : exposures.slice(0, TONIGHT_MAX_ROWS);
  for (const exp of shown) {
    const rec = exp.record || {};
    const closeS = (exp.obsEndUtc || '').replace('T', ' ').replace(/\..*$/, '');
    let statusCell;
    if (exp.ready) {
      statusCell = '<span class="tonight-chip tonight-ready">view</span>';
    } else if (exp.readyAtUtc && live.watermark) {
      const waitS = Math.max(0, (Date.parse(exp.readyAtUtc) - Date.parse(live.watermark)) / 1000);
      statusCell = `<span class="tonight-chip tonight-wait">ready in ~${Math.ceil(waitS / 60)} min</span>`;
    } else {
      statusCell = '<span class="tonight-chip tonight-wait">in progress</span>';
    }
    // The dataId is a server-side integer, so it needs no escaping and
    // is safe in a URL; every other cell is a ConsDB string. The link
    // carries the instrument because a dataId alone does not identify an
    // exposure — LSSTCam and LATISS number from 1 each night, so the
    // same id means a different image on each.
    const q = `dataId=${exp.dataId}` +
      (exp.instrument ? `&instrument=${encodeURIComponent(exp.instrument)}` : '') +
      '&autoFetch=1';
    const idCell = exp.ready
      ? `<a class="mono" href="${escapeHtml(apiUrl(`/?${q}`))}">${exp.dataId}</a>`
      : `<span class="mono">${exp.dataId}</span>`;
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${idCell}</td>` +
      `<td class="mono">${escapeHtml(closeS)}</td>` +
      `<td>${escapeHtml(rec.img_type || '')}</td>` +
      `<td>${escapeHtml(rec.physical_filter || '')}</td>` +
      `<td class="tonight-reason">${escapeHtml(rec.observation_reason || '')}</td>` +
      `<td>${statusCell}</td>`;
    tbody.appendChild(tr);
  }
  const more = document.getElementById('tonight-more');
  if (exposures.length > TONIGHT_MAX_ROWS) {
    more.hidden = false;
    more.innerHTML = tonightShowAll
      ? `showing all ${exposures.length} exposures from tonight — ` +
        `<a href="#">show only the latest ${TONIGHT_MAX_ROWS}</a>`
      : `…and ${exposures.length - TONIGHT_MAX_ROWS} older exposures — ` +
        `<a href="#">show the full list from tonight</a>`;
    more.querySelector('a').onclick = (ev) => {
      ev.preventDefault();
      tonightShowAll = !tonightShowAll;
      renderTonight(tonightLastLive);
    };
  } else {
    more.hidden = true;
  }
}

// The armed drilldown auto-fetch, if any. Held at module scope because
// it outlives the click that armed it — it polls for up to 30 seconds —
// and anything that changes what the page is about in that window has
// to be able to call it off.
let autoFetchTimer = null;

function cancelAutoFetch() {
  if (autoFetchTimer !== null) {
    clearInterval(autoFetchTimer);
    autoFetchTimer = null;
  }
}

function waitAndAutoFetch(expId) {
  // Wait for the shutter-close lookup to resolve, then submit the
  // exposure form on the user's behalf. We poll every 200ms with a
  // 30-second cap so a missing RSP token or an unknown dataId
  // surfaces normally rather than hanging silently.
  cancelAutoFetch();  // never two armed at once
  let elapsed = 0;
  const maxMs = 30_000;
  autoFetchTimer = setInterval(() => {
    elapsed += 200;
    if (resolutionMatches(expId)) {
      cancelAutoFetch();
      const form = document.getElementById('fetch-form');
      if (form && !document.getElementById('fetch-submit').disabled) {
        showMessage('Auto-fetching from night-view drilldown...');
        form.requestSubmit();
      }
    } else if (elapsed >= maxMs) {
      cancelAutoFetch();
    }
  }, 200);
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
  // Only what the user actually asked for. Site, credentials and worker
  // count are the server's own configuration; sending them from here would
  // let one browser change how every other user's fetch behaves.
  out.exposureId = parseInt(out.exposureId, 10);
  out.windowBefore = parseFloat(out.windowBefore);
  out.windowAfter = parseFloat(out.windowAfter);
  // Which exposure this id names depends on the page's instrument; the
  // server keys the resulting view (and its pod attribution) off it.
  out.instrument = getInstrument();
  // Refuse to pair a shutter close with an exposure it wasn't resolved
  // for. Every path that changes the id or the pin clears the
  // resolution, so reaching here with a mismatch means one of them
  // leaked — and submitting anyway would fetch a window around another
  // exposure's shutter close and label it with this one's identity.
  if (!resolutionMatches(out.exposureId)) return null;
  // Always TAI; the server applies the -37 s conversion. We deliberately
  // never expose a UTC opt-out in the UI now that timings come from a
  // service that's TAI by construction (and a manual entry is, by the
  // label next to the field, a TAI shutter close too).
  out.tZero = resolvedTZero;
  // Tell the server this t-zero was hand-entered (ConsDB couldn't resolve
  // the dataId) so it persists it to the exposure-time cache and the
  // explore view can be reopened/refreshed without re-typing.
  if (tZeroIsManual) out.tZeroManual = true;
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
  resolvedForInstrument = null;
  tZeroIsManual = false;
}

// True when the resolved shutter close belongs to this exact
// (dataId, instrument) pair. Anything else is another exposure's t0.
function resolutionMatches(expId) {
  return (
    !!resolvedTZero
    && resolvedForExpId === expId
    && resolvedForInstrument === getInstrument()
  );
}

function updateSubmitButton() {
  const submit = document.getElementById('fetch-submit');
  // Allow re-submitting an already-typed dataId without forcing a refetch.
  submit.disabled = !resolvedTZero;
}

// ----- manual shutter-close fallback --------------------------------------
//
// When ConsDB is down/unreachable or has no row for the dataId, the
// automatic lookup can't produce a t-zero. Rather than dead-end, we reveal
// a text field so the user can type the shutter close themselves (TAI,
// ISO-8601 — the same convention ConsDB's obs_end and the CLI's --t-zero
// use). A valid entry drives the same resolvedTZero the submit path reads.

// ISO-8601 local time, no timezone: YYYY-MM-DD(T| )HH:MM:SS with optional
// fractional seconds — matches the obs_end / Butler `.isot` form and the
// example placeholder. A trailing Z / offset is rejected on purpose: the
// value is interpreted as TAI wall-clock, so a UTC marker would mislead.
const MANUAL_TZERO_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?$/;

function setManualStatus(text, kind /* 'info' | 'ok' | 'error' */) {
  const el = document.getElementById('manual-tzero-status');
  el.textContent = text;
  el.classList.remove('ok', 'error');
  if (kind === 'ok') el.classList.add('ok');
  if (kind === 'error') el.classList.add('error');
}

function hideManualEntry() {
  // Hide and reset — a fresh dataId (or a successful lookup) starts clean.
  // The block's own line stays purely instructional; the main #tzero-status
  // line above owns the "why ConsDB couldn't resolve this" message.
  const row = document.getElementById('manual-tzero');
  row.hidden = true;
  document.getElementById('fetch-form').elements.manualTZero.value = '';
  setManualStatus('enter the shutter close as TAI ISO-8601, e.g. 2026-06-24T14:38:41.380663', 'info');
}

function revealManualEntry() {
  // Show the field (keeping whatever the user already typed) and evaluate
  // it, so a re-fired lookup failure leaves a valid entry still applied.
  document.getElementById('manual-tzero').hidden = false;
  applyManualTZero();
}

function applyManualTZero() {
  const raw = document.getElementById('fetch-form').elements.manualTZero.value.trim();
  const expId = currentExposureId();
  if (expId === null) {
    if (tZeroIsManual) clearResolvedTZero();
    setManualStatus('enter a valid 13-digit dataId first', 'error');
    updateSubmitButton();
    return;
  }
  if (!raw) {
    if (tZeroIsManual) clearResolvedTZero();
    setManualStatus('type the shutter-close timestamp, e.g. 2026-06-24T14:38:41.380663 (TAI)', 'info');
    updateSubmitButton();
    return;
  }
  if (!MANUAL_TZERO_RE.test(raw)) {
    if (tZeroIsManual) clearResolvedTZero();
    setManualStatus('expected ISO-8601 TAI like 2026-06-24T14:38:41.380663 (no timezone)', 'error');
    updateSubmitButton();
    return;
  }
  resolvedTZero = raw;
  resolvedForExpId = expId;
  resolvedForInstrument = getInstrument();
  tZeroIsManual = true;
  setManualStatus('this manual shutter close overrides ConsDB for this dataId', 'ok');
  // Flip the main status green too — it's the affordance the user already
  // associates with "resolved, ready to fetch".
  setTZeroStatus(`manual shutter close (TAI): ${raw}`, 'ok');
}

// The exposureId field as a finite 13-digit int, or null if it isn't one.
function currentExposureId() {
  const raw = document.getElementById('fetch-form').elements.exposureId.value.trim();
  if (raw.length !== DATAID_LENGTH) return null;
  const expId = parseInt(raw, 10);
  return Number.isFinite(expId) ? expId : null;
}

// dataIds are 13 digits: YYYYMMDDSSSSS. Anything shorter is still
// mid-typing — firing a lookup at every keystroke produces a stream of
// noisy "no exposure-time record for 20260" messages, which is worse
// than just waiting. Anything longer is unambiguously a typo.
const DATAID_LENGTH = 13;

function triggerLookupIfReady() {
  const form = document.getElementById('fetch-form');
  const raw = form.elements.exposureId.value.trim();
  // Invalidate any in-flight lookup *before* the validation branches
  // below can return early. Bumping only on the path that starts a new
  // request would let a response for the previously-typed id land after
  // the user had edited the field or switched instrument, and be
  // accepted as the answer to a question nobody asked.
  lookupSeq += 1;
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
  // If we already resolved this exact dataId *under this instrument*,
  // don't re-request. A resolution from the other pin is a different
  // exposure's shutter close and has to be looked up again.
  if (resolutionMatches(expId)) {
    const prefix = tZeroIsManual ? 'manual shutter close (TAI)' : 'shutter close (TAI)';
    setTZeroStatus(`${prefix}: ${resolvedTZero}`, 'ok');
    return;
  }
  setTZeroStatus(`looking up shutter close for ${expId}...`, 'info');
  // Always pinned: the page's instrument decides which exposure this
  // bare id names, and therefore which shutter close comes back. The
  // pin is captured here and stamped onto the resolution below, so a
  // response that lands after the user has switched is recognisably
  // not about the exposure now being asked for.
  const myInstrument = getInstrument();
  const suffix = `?instrument=${encodeURIComponent(myInstrument)}`;
  const mySeq = lookupSeq;
  fetch(apiUrl(`/api/exposure-time/${expId}${suffix}`))
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (mySeq !== lookupSeq) return;  // stale; user typed something newer
      if (r.ok && body.manual && body.tZero) {
        // ConsDB couldn't answer, but a manual stand-in was cached earlier
        // (this machine, or another tab). Pre-fill it into the editable
        // field and let revealManualEntry() validate + apply it, so the
        // user sees their prior value and can re-fetch or amend it.
        document.getElementById('fetch-form').elements.manualTZero.value = body.tZero;
        revealManualEntry();
      } else if (r.ok && body.tZero) {
        resolvedTZero = body.tZero;
        resolvedForExpId = expId;
        resolvedForInstrument = myInstrument;
        tZeroIsManual = false;
        setTZeroStatus(`shutter close (TAI): ${body.tZero}`, 'ok');
        hideManualEntry();
      } else if (r.status === 404) {
        clearResolvedTZero();
        setTZeroStatus(`no exposure-time record for ${expId} — enter the shutter close manually below`, 'error');
        revealManualEntry();
      } else if (r.status === 503) {
        clearResolvedTZero();
        setTZeroStatus(body.error || 'RSP token / ConsDB lookup not configured', 'error');
        revealManualEntry();
      } else {
        clearResolvedTZero();
        setTZeroStatus(`lookup failed: ${body.error || r.status} — enter the shutter close manually below`, 'error');
        revealManualEntry();
      }
    })
    .catch((e) => {
      if (mySeq !== lookupSeq) return;
      clearResolvedTZero();
      setTZeroStatus(`lookup failed: ${e} — enter the shutter close manually below`, 'error');
      revealManualEntry();
    });
}

function scheduleLookup() {
  clearResolvedTZero();
  // A new/edited dataId starts fresh — drop any manual entry from the
  // previous one; the impending lookup re-reveals it only if it fails.
  hideManualEntry();
  setTZeroStatus('typing...', 'info');
  if (lookupTimer) clearTimeout(lookupTimer);
  lookupTimer = setTimeout(triggerLookupIfReady, 300);
}

// ----- range exposure-time lookups ----------------------------------------

// One slot per range endpoint. Each resolves its own dataId -> shutter
// close (TAI) independently, mirroring the single-exposure lookup but
// driving the two range inputs. `seq` guards against stale responses;
// `timer` is the per-field debounce.
const rangeStartSlot = { tZero: null, forId: null, forInstrument: null, seq: 0, timer: null, input: 'rangeStart', status: 'range-start-status' };
const rangeStopSlot = { tZero: null, forId: null, forInstrument: null, seq: 0, timer: null, input: 'rangeStop', status: 'range-stop-status' };

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
  slot.forInstrument = null;
}

// True when this slot's t0 belongs to the (dataId, instrument) pair the
// form is currently asking about — see resolutionMatches().
function rangeSlotMatches(slot, expId) {
  return !!slot.tZero && slot.forId === expId && slot.forInstrument === getInstrument();
}

function updateRangeSubmit() {
  const submit = document.getElementById('range-submit');
  submit.disabled = !(rangeStartSlot.tZero && rangeStopSlot.tZero);
}

function triggerRangeLookup(slot) {
  const raw = document.getElementById('range-form').elements[slot.input].value.trim();
  // Bumped before the early returns below, for the reason spelled out
  // in triggerLookupIfReady().
  slot.seq += 1;
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
  if (rangeSlotMatches(slot, expId)) {
    setRangeStatus(slot.status, `t₀ (TAI): ${slot.tZero}`, 'ok');
    return;
  }
  setRangeStatus(slot.status, `looking up ${expId}…`, 'info');
  const mySeq = slot.seq;
  const myInstrument = getInstrument();
  fetch(apiUrl(`/api/exposure-time/${expId}?instrument=${encodeURIComponent(myInstrument)}`))
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (mySeq !== slot.seq) return;  // stale; user typed something newer
      if (r.ok && body.tZero) {
        slot.tZero = body.tZero;
        slot.forId = expId;
        slot.forInstrument = myInstrument;
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
    const r = await fetch(apiUrl('/api/cache'));
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
    // have triggered this cache as a clickable link. Each carries the
    // instrument it was fetched under: the same 13 digits name a
    // different exposure on the other instrument, so a bare link would
    // open the twin rather than the run this row holds.
    let keyCell;
    if (isNight) {
      keyCell = `<a class="mono cache-key-link" href="${escapeHtml(url || '#')}" target="_blank" rel="noopener">${w.dayObs}</a>`;
    } else if (isRange) {
      // Full start/stop dataIds on two lines — the seq-only form hid the
      // dayObs (the leading 8 digits of the id), which made the row
      // ambiguous about which night it covers.
      keyCell = `<a class="mono cache-key-link cache-key-range" href="${escapeHtml(url || '#')}" target="_blank" rel="noopener" title="range ${w.rangeStart} → ${w.rangeStop}">${w.rangeStart}<br>→ ${w.rangeStop}</a>`;
    } else if (w.exposures && w.exposures.length > 0) {
      keyCell = w.exposures
        .map((e) => {
          const q = `/?dataId=${encodeURIComponent(e.dataId)}`
            + (e.instrument ? `&instrument=${encodeURIComponent(e.instrument)}` : '');
          const label = e.instrument ? `${e.dataId} (${e.instrument})` : String(e.dataId);
          return `<a class="mono cache-key-link" href="${escapeHtml(apiUrl(q))}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
        })
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
  // For night-mode caches relPath is `<window>/<pods=…>` — two URL
  // segments, not one. Split on `/` and encodeURIComponent each segment
  // (so the `=` in `pods=__aos__` is percent-encoded, not treated as a
  // query delimiter); the server percent-decodes them back before
  // resolving the directory.
  const segments = subPath.split('/').map(encodeURIComponent).join('/');
  const url = apiUrl(`/api/cache/${encodeURIComponent(w.cluster)}/${encodeURIComponent(w.namespace)}/${segments}`);
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
    const r = await fetch(apiUrl('/api/cache'), { method: 'DELETE' });
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
    return apiUrl(`/?dayObs=${encodeURIComponent(w.dayObs)}`);
  }
  if (w.kind === 'range' && w.rangeStart != null && w.rangeStop != null) {
    // Pin the run's own instrument: the same bounds exist on the other
    // instrument as a different set of exposures.
    const instQ = w.rangeInstrument ? `&instrument=${encodeURIComponent(w.rangeInstrument)}` : '';
    return apiUrl(
      `/?rangeStart=${encodeURIComponent(w.rangeStart)}&rangeStop=${encodeURIComponent(w.rangeStop)}${instQ}`,
    );
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
  if (!values) {
    showMessage(
      'That shutter close was resolved for a different exposure — re-resolving.',
      true,
    );
    clearResolvedTZero();
    triggerLookupIfReady();
    return;
  }
  if (!Number.isFinite(values.exposureId)) {
    showMessage('exposureId must be an integer.', true);
    return;
  }
  if (!values.tZero) {
    showMessage('Shutter close time has not been resolved yet.', true);
    return;
  }
  saveExpTuning();

  const submit = document.getElementById('fetch-submit');
  submit.disabled = true;
  showMessage('Starting fetch...');
  activeProgressKind = 'exposure';
  showInlineProgress('exposure', true);
  resetInlineProgress('exposure');

  let jobId;
  try {
    const r = await fetch(apiUrl('/api/fetch'), {
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
  const es = new EventSource(apiUrl(`/api/fetch/${jobId}/progress`));
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
        instrument: ev.instrument,
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
  // The job's own instrument, not the topbar's: the user may have
  // flipped the switch while the fetch ran, and this request has to ask
  // for the exposure that was actually fetched. Without it the bare
  // expId's slot could by then hold the other instrument's exposure —
  // another tab's fetch, or a rebuild — and we would render that one
  // and then stamp its instrument into the URL as if it were ours.
  const jobInstrument = activeJob && activeJob.instrument;
  const instQ = jobInstrument ? `&instrument=${encodeURIComponent(jobInstrument)}` : '';
  let key;
  if (activeJob && activeJob.kind === 'night') {
    key = `dayObs=${encodeURIComponent(activeJob.dayObs)}`;
  } else if (activeJob && activeJob.kind === 'range') {
    key = `rangeStart=${encodeURIComponent(activeJob.startId)}`
      + `&rangeStop=${encodeURIComponent(activeJob.stopId)}`;
  } else {
    key = `dataId=${encodeURIComponent(activeJob.expId)}`;
  }
  // Night mode needs no pin (AOS is LSSTCam by construction); the other
  // two carry the job's.
  const params = activeJob && activeJob.kind === 'night' ? key : key + instQ;
  try {
    const r = await fetch(apiUrl(`/api/summary?${params}`));
    const summary = await r.json();
    if (!summary.loaded) {
      showMessage('Fetch finished but the server reports no loaded data?', true);
      return;
    }
    // Reflect the loaded state in the URL so a refresh / bookmark
    // lands on the same view, and so opening this URL in a new tab
    // independently routes to it. The instrument rides along: a dataId
    // alone does not name an exposure, so a bare ?dataId=… URL reopened
    // tomorrow (or pasted to a colleague) would resolve to the
    // probe-order twin — the same 13 digits, the other instrument, a
    // shutter close an hour away. The summary's own instrument is used
    // rather than the topbar's, in case the user flipped the switch
    // while the fetch was running.
    let urlParams = key;
    if (summary.instrument && (!activeJob || activeJob.kind !== 'night')) {
      urlParams += `&instrument=${encodeURIComponent(summary.instrument)}`;
    }
    const newUrl = `${window.location.pathname}?${urlParams}`;
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
  const dayObsRaw = form.elements.dayObs.value.trim();
  const dayObs = parseInt(dayObsRaw, 10);
  if (!Number.isFinite(dayObs) || dayObs < 19000000 || dayObs > 30000000) {
    const el = document.getElementById('night-message');
    el.textContent = 'dayObs must be an 8-digit YYYYMMDD integer.';
    el.classList.add('error');
    return;
  }
  const body = { dayObs };
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
    const r = await fetch(apiUrl('/api/fetch-night'), {
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
  // The window pads live in the shared "advanced options" section and
  // are form-associated with fetch-form: one pair of knobs serves both
  // flows, since a range applies them to its first/last exposure only.
  const expForm = document.getElementById('fetch-form');
  const body = {
    rangeStart: rangeStartSlot.forId,
    rangeStop: rangeStopSlot.forId,
    // Always TAI; the server applies the -37 s conversion (same contract
    // as the single-exposure form).
    tZeroStart: rangeStartSlot.tZero,
    tZeroStop: rangeStopSlot.tZero,
    windowBefore: parseFloat(expForm.elements.windowBefore.value),
    windowAfter: parseFloat(expForm.elements.windowAfter.value),
    instrument: getInstrument(),
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
    const r = await fetch(apiUrl('/api/fetch-range'), {
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
  // Manual shutter-close fallback (shown only when the ConsDB lookup fails).
  document.getElementById('fetch-form').elements.manualTZero.addEventListener('input', applyManualTZero);
  const rangeForm = document.getElementById('range-form');
  rangeForm.elements.rangeStart.addEventListener('input', () => scheduleRangeLookup(rangeStartSlot));
  rangeForm.elements.rangeStop.addEventListener('input', () => scheduleRangeLookup(rangeStopSlot));
  for (const b of document.querySelectorAll('#instrument-switch button')) {
    b.addEventListener('click', () => setInstrument(b.dataset.instrument));
  }
  homeListenersWired = true;
  updateSubmitButton();  // start with submit disabled until lookup resolves
  updateRangeSubmit();   // same for the range card
}

// ----- admin view ----------------------------------------------------------
// The cache browser lives on its own page (/?admin=1): day-to-day users
// shouldn't need to think about caching, but operators still want to see
// what's on disk and be able to flush it.

let adminListenersWired = false;

function startAdmin() {
  if (!adminListenersWired) {
    document.getElementById('cache-refresh').addEventListener('click', refreshCache);
    document.getElementById('cache-delete-all').addEventListener('click', deleteAllCache);
    adminListenersWired = true;
  }
  loadSite();
  refreshCache();
}
window.startAdmin = startAdmin;
