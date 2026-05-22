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
  // ?dataId=… is the night-view bar-drilldown deep-link. When present we
  // always land on the home view (which reads the same param and kicks
  // an auto-fetch) — otherwise the server's existing state would
  // hijack the new tab into showing the previously-loaded view.
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('dataId')) {
    window.showHome();
    return;
  }
  let summary;
  try {
    const r = await fetch('/api/summary');
    summary = await r.json();
  } catch (e) {
    document.body.innerHTML =
      `<pre style="padding:14px;color:#c1252b">Error loading /api/summary: ${e}</pre>`;
    return;
  }
  if (summary.loaded && summary.mode === 'night') {
    window.showNight(summary);
  } else if (summary.loaded) {
    window.showExplore(summary);
  } else {
    window.showHome();
  }
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

window.showExplore = function showExplore(summary) {
  hideAllViews();
  document.getElementById('explore-view').hidden = false;
  window.startExplore(summary);
};

window.showNight = function showNight(summary) {
  hideAllViews();
  document.getElementById('night-view').hidden = false;
  window.startNight(summary);
};

bootstrap();
