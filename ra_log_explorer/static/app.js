/* ra-log-explorer — bootstrap.
 *
 * Decides which top-level view to render on page load by inspecting
 * /api/summary: when an exposure is loaded, switch to #explore-view and
 * hand control to explore.js; otherwise show #home-view and hand control
 * to home.js. Also wires the home↔explore transitions invoked by either
 * side via the `window.showHome` / `window.showExplore` hooks.
 */
'use strict';

async function bootstrap() {
  // URL is the source of truth for which view to render:
  //
  //   /                    -> home
  //   /?dataId=X           -> explore view for exposure X (or home + auto-fetch)
  //   /?dataId=X&autoFetch=1 -> home with the form pre-submitted (deep-link
  //                            from a night-view bar drilldown)
  //   /?dayObs=Y           -> night view for night Y (or home if not loaded)
  //
  // Each tab carries its own URL, so opening / refreshing different
  // tabs hits the server state for *that tab's* key without disturbing
  // the others.
  const urlParams = new URLSearchParams(window.location.search);
  const urlDataId = urlParams.get('dataId');
  const urlDayObs = urlParams.get('dayObs');
  const urlRangeStart = urlParams.get('rangeStart');
  const urlRangeStop = urlParams.get('rangeStop');
  const urlAutoFetch = urlParams.get('autoFetch') === '1';

  if (urlRangeStart && urlRangeStop) {
    let summary;
    try {
      const qs = `rangeStart=${encodeURIComponent(urlRangeStart)}&rangeStop=${encodeURIComponent(urlRangeStop)}`;
      const r = await fetch(apiUrl(`/api/summary?${qs}`));
      summary = await r.json();
    } catch (e) { /* fall through to home */ }
    if (summary && summary.loaded) {
      window.showRange(summary);
    } else {
      window.showHome();
    }
    return;
  }
  if (urlAutoFetch && urlDataId) {
    // Deep-link with explicit auto-fetch request: always land on home
    // and let it fire the fetch. Never try to load existing state.
    window.showHome();
    return;
  }
  if (urlDataId) {
    let summary;
    try {
      const r = await fetch(apiUrl(`/api/summary?dataId=${encodeURIComponent(urlDataId)}`));
      summary = await r.json();
    } catch (e) { /* fall through to home */ }
    if (summary && summary.loaded) {
      window.showExplore(summary);
    } else {
      window.showHome();
    }
    return;
  }
  if (urlDayObs) {
    let summary;
    try {
      const r = await fetch(apiUrl(`/api/summary?dayObs=${encodeURIComponent(urlDayObs)}`));
      summary = await r.json();
    } catch (e) { /* fall through to home */ }
    if (summary && summary.loaded) {
      window.showNight(summary);
    } else {
      window.showHome();
    }
    return;
  }
  window.showHome();
}

function hideAllViews() {
  document.getElementById('home-view').hidden = true;
  document.getElementById('explore-view').hidden = true;
  document.getElementById('night-view').hidden = true;
}

window.showHome = function showHome() {
  hideAllViews();
  document.getElementById('home-view').hidden = false;
  window.startHome();
};

function _applyActiveSite(summary) {
  // Set the per-site accent (border-top strip + switcher chip colour)
  // as soon as we know which site this view belongs to. Without this
  // the strip wouldn't appear on a deep-linked /?dataId=… or /?dayObs=…
  // load because the home view's switcher never runs.
  if (summary && summary.site) {
    document.body.dataset.site = summary.site;
  }
}

window.showExplore = function showExplore(summary) {
  hideAllViews();
  _applyActiveSite(summary);
  // Single-exposure mode: ensure the range navigator (shared explore-view
  // chrome) is hidden in case we arrived here after a range.
  document.getElementById('range-nav').hidden = true;
  document.getElementById('explore-view').hidden = false;
  window.startExplore(summary);
};

window.showNight = function showNight(summary) {
  hideAllViews();
  _applyActiveSite(summary);
  document.getElementById('night-view').hidden = false;
  window.startNight(summary);
};

window.showRange = function showRange(indexSummary) {
  // Range mode reuses the explore view; the navigator strip on top lets
  // the user step between the exposures in the range. range.js loads the
  // first dataId's timeline into the explore renderer.
  hideAllViews();
  _applyActiveSite(indexSummary);
  document.getElementById('explore-view').hidden = false;
  document.getElementById('range-nav').hidden = false;
  window.startRange(indexSummary);
};

// ----- incomplete-fetch banner (shared by explore + night views) -----------
//
// A fetch is "complete" only when every pod's logs came back in full. There
// are two ways to fall short, both flagged here: a hard per-pod failure
// (timeout / transient 5xx) in meta.errors, or a pod whose chunks couldn't be
// reconciled to a lossless single-batch fetch in meta.incomplete_pods (data
// the Loki #17270 bug may have dropped). When that happens the window is
// missing data — and for the night view that silently biases the Δshutter
// histograms — so we shout rather than letting a partial window pass for the
// whole night. `summary.meta.fetchComplete` is the explicit signal; we also
// treat either non-empty map as incomplete for robustness.
function renderFetchBanner(bannerEl, summary) {
  if (!bannerEl) return;
  bannerEl.innerHTML = '';
  const meta = summary && summary.meta;
  const errors = (meta && meta.errors) || {};
  const incomplete = (meta && meta.incomplete_pods) || {};
  // Merge both failure modes; a hard error wins over a soft shortfall.
  const problems = Object.assign({}, incomplete, errors);
  const pods = Object.keys(problems);
  const complete = meta ? (meta.fetchComplete !== false && pods.length === 0) : true;
  if (complete) {
    bannerEl.hidden = true;
    return;
  }
  bannerEl.hidden = false;
  const total = (meta && meta.pod_count) || pods.length;

  const title = document.createElement('div');
  title.className = 'fetch-banner-title';
  title.textContent = `⚠ Incomplete fetch — ${pods.length} of ${total} pods missing data`;
  bannerEl.appendChild(title);

  const sub = document.createElement('div');
  sub.className = 'fetch-banner-sub';
  sub.textContent =
    'The logs shown are missing data and may be misleading'
    + (summary && summary.mode === 'night' ? ' (this skews the Δshutter histograms below).' : '.')
    + ' Re-fetch with force-refresh to retry.';
  bannerEl.appendChild(sub);

  const list = document.createElement('ul');
  list.className = 'fetch-banner-pods';
  const shown = pods.slice(0, 12);
  for (const pod of shown) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'mono';
    name.textContent = pod;
    li.appendChild(name);
    li.appendChild(document.createTextNode(` — ${problems[pod]}`));
    list.appendChild(li);
  }
  if (pods.length > shown.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = `+${pods.length - shown.length} more (see _meta.json)`;
    list.appendChild(li);
  }
  bannerEl.appendChild(list);
}
window.renderFetchBanner = renderFetchBanner;

// ----- "what is this?" overlay ---------------------------------------------
// One shared panel, toggled from every view's topbar. Wired here (the
// bootstrap script) because the buttons exist in all three views.

function wireFaq() {
  const overlay = document.getElementById('faq-overlay');
  if (!overlay) return;
  const setOpen = (open) => { overlay.hidden = !open; };
  for (const btn of document.querySelectorAll('.faq-toggle')) {
    btn.addEventListener('click', () => setOpen(overlay.hidden));
  }
  document.getElementById('faq-close').addEventListener('click', () => setOpen(false));
  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) setOpen(false);  // click outside the panel
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !overlay.hidden) setOpen(false);
  });
}
wireFaq();

bootstrap();
