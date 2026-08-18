/* ra-log-explorer — bootstrap.
 *
 * Decides which top-level view to render on page load by inspecting
 * /api/summary: when an exposure is loaded, switch to #explore-view and
 * hand control to explore.js; otherwise show #home-view and hand control
 * to home.js. Also wires the home↔explore transitions invoked by either
 * side via the `window.showHome` / `window.showExplore` hooks, and owns
 * the browser history those transitions write (see `navigateTo` below).
 */
'use strict';

// Which routing pass is the current one. Back and forward can arrive
// faster than /api/summary answers, and the loser of that race would
// otherwise render its view over the top of the winner's.
let routeToken = 0;

async function bootstrap() {
  const token = ++routeToken;
  // URL is the source of truth for which view to render:
  //
  //   /                    -> home
  //   /?dataId=X           -> explore view for exposure X (or home + auto-fetch)
  //   /?dataId=X&autoFetch=1 -> home with the form pre-submitted (deep-link
  //                            from a night-view bar drilldown)
  //   /?dayObs=Y           -> night view for night Y (or home if not loaded)
  //   /?dayObs=Y&nightView=sfm -> the SFM+misc half of that night, which is
  //                            a separate fetch and a separate server state
  //
  // Each tab carries its own URL, so opening / refreshing different
  // tabs hits the server state for *that tab's* key without disturbing
  // the others.
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('admin') === '1') {
    window.showAdmin();
    return;
  }
  const urlDataId = urlParams.get('dataId');
  const urlDayObs = urlParams.get('dayObs');
  const urlRangeStart = urlParams.get('rangeStart');
  const urlRangeStop = urlParams.get('rangeStop');
  const urlAutoFetch = urlParams.get('autoFetch') === '1';

  if (urlRangeStart && urlRangeStop) {
    let summary;
    try {
      // Same pin the dataId form carries: [startId, stopId] names a
      // different run of exposures on each instrument, so a bare span
      // would resolve to whichever twin was fetched last.
      const rangeInstrument = urlParams.get('instrument');
      const rangeInstQ = rangeInstrument ? `&instrument=${encodeURIComponent(rangeInstrument)}` : '';
      const qs = `rangeStart=${encodeURIComponent(urlRangeStart)}&rangeStop=${encodeURIComponent(urlRangeStop)}`;
      const r = await fetch(apiUrl(`/api/summary?${qs}${rangeInstQ}`));
      summary = await r.json();
    } catch (e) { /* fall through to home */ }
    if (token !== routeToken) return;  // a later back/forward overtook us
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
      // Carry the URL's instrument so the server can refuse to serve a
      // same-id state that belongs to the *other* instrument.
      const urlInstrument = urlParams.get('instrument');
      const instQ = urlInstrument ? `&instrument=${encodeURIComponent(urlInstrument)}` : '';
      const r = await fetch(apiUrl(`/api/summary?dataId=${encodeURIComponent(urlDataId)}${instQ}`));
      summary = await r.json();
    } catch (e) { /* fall through to home */ }
    if (token !== routeToken) return;
    if (summary && summary.loaded) {
      window.showExplore(summary);
    } else {
      window.showHome();
    }
    return;
  }
  if (urlDayObs) {
    let summary;
    const urlNightView = urlParams.get('nightView') || 'aos';
    try {
      const r = await fetch(
        apiUrl(`/api/summary?dayObs=${encodeURIComponent(urlDayObs)}`
          + `&nightView=${encodeURIComponent(urlNightView)}`),
      );
      summary = await r.json();
    } catch (e) { /* fall through to home */ }
    if (token !== routeToken) return;
    if (summary && summary.loaded) {
      window.showNight(summary);
    } else {
      window.showHome();
    }
    return;
  }
  window.showHome();
}

// ----- history --------------------------------------------------------------
//
// Every view swap happens inside one document, so the entries the Back
// button walks are ours to create — and until this existed the app
// created none: `replaceState` everywhere meant one entry for the whole
// session, and Back left the application altogether, landing on whatever
// the tab held before it (a stale link, an old bookmark, a mistyped
// URL). Deployed, that reads as the app sending you somewhere invalid.
//
// So: a transition the user asked for (home -> a view, a view -> home)
// gets its own entry, and anything that merely refines the view already
// on screen (the instrument pin, the range navigator's selected
// exposure) rewrites the current one — otherwise stepping through
// twenty exposures would cost twenty Back presses to undo.

window.navigateTo = function navigateTo(query, opts) {
  const url = query ? `${window.location.pathname}?${query}` : window.location.pathname;
  // Pushing the URL we are already on buys the user a Back press that
  // does nothing visible; rewrite in place instead.
  const same = url === window.location.pathname + window.location.search;
  if (same || (opts && opts.replace)) {
    window.history.replaceState({}, '', url);
  } else {
    window.history.pushState({}, '', url);
  }
};

window.addEventListener('popstate', () => {
  // The URL is the router's only input, so re-running the router *is*
  // the handling of back/forward. Everything we push is same-document,
  // so nothing reloads and no fetch is repeated that the URL doesn't ask
  // for.
  bootstrap();
});

function hideAllViews() {
  document.getElementById('home-view').hidden = true;
  document.getElementById('explore-view').hidden = true;
  document.getElementById('night-view').hidden = true;
  document.getElementById('admin-view').hidden = true;
}

window.showHome = function showHome() {
  hideAllViews();
  document.getElementById('home-view').hidden = false;
  window.startHome();
};

window.showAdmin = function showAdmin() {
  hideAllViews();
  document.getElementById('admin-view').hidden = false;
  window.startAdmin();
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
  // A fetch that found *no pods at all* reports itself perfectly
  // complete, because it completed — it just completed over nothing.
  // On screen that is indistinguishable from a quiet night, and the
  // usual cause is asking the wrong cluster: this server serves one
  // site, so a BTS dayObs opened on a summit instance renders an empty
  // night with no error anywhere. Say so.
  if (meta && meta.pod_count === 0) {
    bannerEl.hidden = false;
    const t = document.createElement('div');
    t.className = 'fetch-banner-title';
    t.textContent = '⚠ No pods found in this window';
    bannerEl.appendChild(t);
    const s2 = document.createElement('div');
    s2.className = 'fetch-banner-sub';
    s2.textContent =
      `Nothing logged to ${(summary && summary.site) || 'this site'} in the requested window. `
      + 'Most often that means the dataId or dayObs belongs to the other site — '
      + 'this server serves one cluster and cannot see the other.';
    bannerEl.appendChild(s2);
    return;
  }
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
