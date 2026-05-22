/* ra-log-explorer — night view.
 *
 * Consumes the /api/summary payload when `mode === "night"` and renders
 * a one-page failure dashboard:
 *
 *   - Top stats (visits seen, tracebacks, distinct exception classes …)
 *   - Errors-by-type and errors-by-pod tables
 *   - Δshutter histograms for the first task pickup and calcZernikes end
 *   - Per-traceback failure list with click-to-expand drilldown
 */
'use strict';

let nightSummary = null;
let nightListenersWired = false;

function startNight(summary) {
  nightSummary = summary;
  document.getElementById('night-dayobs-display').textContent = `dayObs=${summary.dayObs}`;
  document.getElementById('night-window-display').textContent =
    `(${summary.startTime} → ${summary.endTime})`;
  document.getElementById('night-cache-info').textContent =
    `cache: ${humanBytes(summary.cacheBytes || 0)} @ ${summary.cacheDir}`;
  renderTopStats(summary.stats);
  renderErrorsByType(summary.errorsByType);
  renderErrorsByPod(summary.errorsByPod);
  renderHistogram(
    'night-hist-first',
    'night-hist-first-meta',
    'night-hist-first-title',
    'First task pickup',
    summary.histograms.firstTaskStart,
  );
  renderHistogram(
    'night-hist-cz',
    'night-hist-cz-meta',
    'night-hist-cz-title',
    'calcZernikes end',
    summary.histograms.calcZernikesEnd,
  );
  renderFailures(summary.failures);
  if (!nightListenersWired) {
    document.getElementById('night-back-home').addEventListener('click', () => {
      if (window.showHome) window.showHome();
    });
    nightListenersWired = true;
  }
}
window.startNight = startNight;

// ----- top stats ------------------------------------------------------------

function renderTopStats(stats) {
  const wrap = document.getElementById('night-stats');
  wrap.innerHTML = '';
  const items = [
    { label: 'exposures seen', value: stats.nVisitsSeen },
    { label: 'tracebacks', value: stats.nTracebacks, accent: stats.nTracebacks > 0 ? 'err' : null },
    { label: 'dataIds w/ traceback', value: stats.nDataIdsWithTraceback },
    { label: 'pods w/ traceback', value: stats.nPodsWithTraceback },
    { label: 'distinct exception classes', value: stats.nDistinctExceptionClasses },
    { label: 'pods in fetch', value: stats.nPods },
  ];
  if (stats.nMissingShutterClose > 0) {
    items.push({
      label: 'dataIds w/o shutter close',
      value: stats.nMissingShutterClose,
      hint: '(Δshutter histograms exclude these — RSP token / ConsDB needed to fill in)',
      accent: 'warn',
    });
  }
  for (const it of items) {
    const card = document.createElement('div');
    card.className = 'night-stat' + (it.accent ? ` night-stat-${it.accent}` : '');
    card.innerHTML =
      `<div class="night-stat-value">${it.value}</div>` +
      `<div class="night-stat-label">${it.label}</div>` +
      (it.hint ? `<div class="night-stat-hint">${it.hint}</div>` : '');
    wrap.appendChild(card);
  }
}

// ----- errors-by-type / errors-by-pod --------------------------------------

function renderErrorsByType(rows) {
  const tbody = document.querySelector('#night-errors-by-type tbody');
  tbody.innerHTML = '';
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted">No tracebacks in this window.</td></tr>';
    return;
  }
  for (const r of rows) {
    const tr = document.createElement('tr');
    const msgCell = document.createElement('td');
    msgCell.className = 'night-msg-cell';
    msgCell.textContent = r.sampleMessage || '';
    tr.appendChild(asTd(r.excClass, 'mono'));
    tr.appendChild(asTd(String(r.count), 'numeric'));
    tr.appendChild(msgCell);
    tbody.appendChild(tr);
  }
}

function renderErrorsByPod(rows) {
  const tbody = document.querySelector('#night-errors-by-pod tbody');
  tbody.innerHTML = '';
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted">No tracebacks in this window.</td></tr>';
    return;
  }
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.appendChild(asTd(r.pod, 'mono'));
    tr.appendChild(asTd(r.group));
    tr.appendChild(asTd(String(r.count), 'numeric'));
    tbody.appendChild(tr);
  }
}

function asTd(text, klass) {
  const td = document.createElement('td');
  if (klass) td.className = klass;
  td.textContent = text;
  return td;
}

// ----- histograms (SVG, no D3 — just a few <rect>s) ------------------------

function renderHistogram(svgId, metaId, titleId, titleText, hist) {
  const svg = document.getElementById(svgId);
  const meta = document.getElementById(metaId);
  const title = document.getElementById(titleId);
  title.textContent = titleText;
  svg.innerHTML = '';
  if (!hist || !hist.counts || hist.counts.length === 0) {
    meta.textContent = hist && hist.nDropped > 0
      ? `(no values to plot; ${hist.nDropped} dataIds excluded — no shutter close known)`
      : '(no values to plot)';
    return;
  }
  const W = 560, H = 180;
  const padL = 36, padR = 8, padT = 8, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const maxCount = Math.max(...hist.counts);
  const barW = plotW / hist.counts.length;
  const ns = 'http://www.w3.org/2000/svg';
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');

  // Axes (light).
  for (const [x1, y1, x2, y2, klass] of [
    [padL, padT, padL, H - padB, 'night-hist-axis'],
    [padL, H - padB, W - padR, H - padB, 'night-hist-axis'],
  ]) {
    const l = document.createElementNS(ns, 'line');
    l.setAttribute('x1', x1); l.setAttribute('y1', y1);
    l.setAttribute('x2', x2); l.setAttribute('y2', y2);
    l.setAttribute('class', klass);
    svg.appendChild(l);
  }
  // Bars.
  for (let i = 0; i < hist.counts.length; i++) {
    const c = hist.counts[i];
    const h = maxCount === 0 ? 0 : (c / maxCount) * plotH;
    const x = padL + i * barW;
    const y = (H - padB) - h;
    const rect = document.createElementNS(ns, 'rect');
    rect.setAttribute('x', x);
    rect.setAttribute('y', y);
    rect.setAttribute('width', Math.max(0.5, barW - 1));
    rect.setAttribute('height', h);
    rect.setAttribute('class', 'night-hist-bar');
    // Tooltip via <title>: native browser hover.
    const binLo = hist.xMin + i * hist.binWidth;
    const binHi = binLo + hist.binWidth;
    const t = document.createElementNS(ns, 'title');
    t.textContent = `${binLo.toFixed(2)}–${binHi.toFixed(2)} s\n${c} dataIds`;
    rect.appendChild(t);
    svg.appendChild(rect);
  }
  // X-axis tick labels: min / mid / max.
  for (const x of [hist.xMin, (hist.xMin + hist.xMax) / 2, hist.xMax]) {
    const tx = document.createElementNS(ns, 'text');
    const xPos = padL + ((x - hist.xMin) / (hist.xMax - hist.xMin || 1)) * plotW;
    tx.setAttribute('x', xPos);
    tx.setAttribute('y', H - padB + 14);
    tx.setAttribute('text-anchor', 'middle');
    tx.setAttribute('class', 'night-hist-tick');
    tx.textContent = `${x.toFixed(1)} s`;
    svg.appendChild(tx);
  }
  // Y-axis ticks: 0 and max.
  for (const val of [0, maxCount]) {
    const ty = document.createElementNS(ns, 'text');
    ty.setAttribute('x', padL - 6);
    ty.setAttribute('y', val === 0 ? H - padB + 4 : padT + 10);
    ty.setAttribute('text-anchor', 'end');
    ty.setAttribute('class', 'night-hist-tick');
    ty.textContent = String(val);
    svg.appendChild(ty);
  }

  let metaText = `n=${hist.nValues}, bin width = ${hist.binWidth.toFixed(2)} s`;
  if (hist.nDropped > 0) {
    metaText += `; ${hist.nDropped} dataIds excluded (no shutter close known)`;
  }
  meta.textContent = metaText;
}

// ----- failures table + drilldown -------------------------------------------

function renderFailures(rows) {
  const tbody = document.querySelector('#night-failures tbody');
  tbody.innerHTML = '';
  document.getElementById('night-failures-count').textContent =
    `(${rows.length} traceback${rows.length === 1 ? '' : 's'})`;
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">No tracebacks 🎉</td></tr>';
    return;
  }
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.className = 'night-failure-row';
    tr.appendChild(asTd(formatTimeHM(r.tIso), 'mono'));
    tr.appendChild(asTd(r.dataId == null ? '?' : String(r.dataId), 'mono'));
    tr.appendChild(asTd(r.offsetS == null ? '—' : fmtOffset(r.offsetS), 'mono'));
    tr.appendChild(asTd(r.pod, 'mono night-pod-cell'));
    tr.appendChild(asTd(r.excClass, 'mono'));
    const msgCell = document.createElement('td');
    msgCell.className = 'night-msg-cell';
    msgCell.textContent = r.excMessage || '';
    tr.appendChild(msgCell);
    const more = document.createElement('td');
    more.className = 'night-failure-more';
    more.innerHTML = '▾';
    tr.appendChild(more);
    tbody.appendChild(tr);
    tr.addEventListener('click', () => toggleFailureExpansion(tr, r));
  }
}

function formatTimeHM(iso) {
  // Just show HH:MM:SS — the full date is in the page header.
  const m = /T(\d\d:\d\d:\d\d)/.exec(iso);
  return m ? m[1] : iso;
}

async function toggleFailureExpansion(tr, row) {
  // The expanded body row lives directly below the trigger row. Toggle
  // by inserting / removing it.
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('night-failure-detail')) {
    next.remove();
    return;
  }
  const detail = document.createElement('tr');
  detail.className = 'night-failure-detail';
  const td = document.createElement('td');
  td.colSpan = 7;
  td.innerHTML = '<pre class="night-tb-body">loading…</pre>';
  detail.appendChild(td);
  tr.parentNode.insertBefore(detail, tr.nextSibling);
  try {
    const r = await fetch(`/api/night/traceback/${encodeURIComponent(row.bodyKey)}`);
    if (!r.ok) {
      td.firstChild.textContent = `(failed to load: HTTP ${r.status})`;
      return;
    }
    const data = await r.json();
    td.firstChild.textContent = data.body || '(empty body)';
  } catch (e) {
    td.firstChild.textContent = `(failed to load: ${e})`;
  }
}
