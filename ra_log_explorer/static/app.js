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
  let summary;
  try {
    const r = await fetch('/api/summary');
    summary = await r.json();
  } catch (e) {
    document.body.innerHTML =
      `<pre style="padding:14px;color:#c1252b">Error loading /api/summary: ${e}</pre>`;
    return;
  }
  if (summary.loaded) {
    window.showExplore(summary);
  } else {
    window.showHome();
  }
}

window.showHome = function showHome() {
  document.getElementById('home-view').hidden = false;
  document.getElementById('explore-view').hidden = true;
  window.startHome();
};

window.showExplore = function showExplore(summary) {
  document.getElementById('home-view').hidden = true;
  document.getElementById('explore-view').hidden = false;
  window.startExplore(summary);
};

bootstrap();
