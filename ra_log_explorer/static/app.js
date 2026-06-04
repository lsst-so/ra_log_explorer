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
  const urlAutoFetch = urlParams.get('autoFetch') === '1';

  if (urlAutoFetch && urlDataId) {
    // Deep-link with explicit auto-fetch request: always land on home
    // and let it fire the fetch. Never try to load existing state.
    window.showHome();
    return;
  }
  if (urlDataId) {
    let summary;
    try {
      const r = await fetch(`/api/summary?dataId=${encodeURIComponent(urlDataId)}`);
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
      const r = await fetch(`/api/summary?dayObs=${encodeURIComponent(urlDayObs)}`);
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
  document.getElementById('explore-view').hidden = false;
  window.startExplore(summary);
};

window.showNight = function showNight(summary) {
  hideAllViews();
  _applyActiveSite(summary);
  document.getElementById('night-view').hidden = false;
  window.startNight(summary);
};

bootstrap();
