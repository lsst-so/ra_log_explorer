/* ra-log-explorer — night view.
 *
 * Consumes the /api/summary payload when `mode === "night"` and renders
 * a one-page failure dashboard:
 *
 *   - Top stats (visits seen, tracebacks, distinct exception classes …)
 *   - An incomplete-fetch banner and a gather-only banner (dataIds whose
 *     step1b ran with no step1a — a dropped-step1a-logs tell)
 *   - Errors-by-type and errors-by-pod tables
 *   - Δshutter histograms for the first task pickup and calcZernikes end
 *   - Per-traceback failure list with click-to-expand drilldown
 */
'use strict';

let nightSummary = null;
let nightListenersWired = false;

// Deep link from a night drilldown into one exposure's explore view.
//
// The instrument has to travel with the dataId. Night mode is AOS, so
// it is always LSSTCam — but the link lands on a *fresh* home page,
// which otherwise picks up whatever instrument this browser last used.
// On a browser that had been looking at LATISS, an un-pinned link would
// resolve the LATISS exposure sharing this 13-digit id — a different
// exposure, an hour away — and `autoFetch=1` would fetch it without
// anyone touching a control.
function nightExposureHref(dataId) {
  const inst = (nightSummary && nightSummary.instrument) || 'lsstcam';
  return apiUrl(
    `/?dataId=${encodeURIComponent(dataId)}&autoFetch=1&instrument=${encodeURIComponent(inst)}`,
  );
}

function startNight(summary) {
  nightSummary = summary;
  if (window.renderFetchBanner) {
    window.renderFetchBanner(document.getElementById('night-fetch-banner'), summary);
  }
  renderGatherOnly(summary.gatherOnly || []);
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
    'night-hist-first-bin',
    'First task pickup',
    summary.histograms.firstTaskStart,
  );
  renderHistogram(
    'night-hist-cz',
    'night-hist-cz-meta',
    'night-hist-cz-title',
    'night-hist-cz-bin',
    'calcZernikes end',
    summary.histograms.calcZernikesEnd,
  );
  renderFailures(summary.failures);
  renderRestarts(summary.restarts);
  if (!nightListenersWired) {
    document.getElementById('night-back-home').addEventListener('click', () => {
      // Drop the dayObs key out of the URL bar so a subsequent refresh
      // lands on home — not back on whatever night we just left.
      history.replaceState({}, '', window.location.pathname);
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
    {
      label: 'pod restarts',
      value: stats.nPodRestarts || 0,
      accent: (stats.nPodRestarts || 0) > 0 ? 'warn' : null,
    },
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

// Tooltip (filter / exp time / image type / reason / …) for a dataId,
// built from the night payload's exposureInfo map. '' when the record
// wasn't resolved (no token, or a skipped id), so a hover just shows
// nothing extra. Lets a user tell *what kind of image* a flagged dataId
// is — e.g. spot a CWFS pair — without leaving the night view.
function nightExposureTooltip(dataId) {
  const info = (nightSummary && nightSummary.exposureInfo) || {};
  const fn = window.exposureInfoTooltip;
  return fn ? fn(info[String(dataId)]) : '';
}

// ----- gather-only warning --------------------------------------------------
//
// A gather (step1b) step aggregates step1a's per-detector output, so it
// cannot run for a dataId unless step1a ran first. Seeing gather activity
// with no step1a for the same dataId is physically impossible — it means the
// fetch dropped the step1a lines (the grafana/loki#17270 symptom). We flag it
// loudly because it also explains a biased "first task pickup" histogram:
// with step1a gone, the earliest event left for that dataId is the gather.
function renderGatherOnly(dataIds) {
  const el = document.getElementById('night-gather-banner');
  if (!el) return;
  el.innerHTML = '';
  if (!dataIds || dataIds.length === 0) {
    el.hidden = true;
    return;
  }
  el.hidden = false;

  const title = document.createElement('div');
  title.className = 'fetch-banner-title';
  title.textContent =
    `⚠ ${dataIds.length} dataId${dataIds.length === 1 ? '' : 's'} show gather (step1b) `
    + 'activity with no step1a — impossible unless the fetch dropped step1a logs';
  el.appendChild(title);

  const sub = document.createElement('div');
  sub.className = 'fetch-banner-sub';
  sub.textContent =
    'Gather aggregates step1a output, so it cannot run without it. These are '
    + 'almost certainly missing data (and bias the first-task-pickup histogram). '
    + 'Re-fetch with force-refresh.';
  el.appendChild(sub);

  const list = document.createElement('div');
  list.className = 'night-hist-bin-list';
  const shown = dataIds.slice(0, 40);
  for (const id of shown) {
    const a = document.createElement('a');
    a.className = 'night-hist-bin-id mono';
    a.href = nightExposureHref(id);
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = String(id);
    const tip = nightExposureTooltip(id);
    if (tip) a.title = tip;
    list.appendChild(a);
  }
  el.appendChild(list);
  if (dataIds.length > shown.length) {
    const more = document.createElement('div');
    more.className = 'fetch-banner-sub muted';
    more.textContent = `+${dataIds.length - shown.length} more (see the night cache _meta.json / re-fetch)`;
    el.appendChild(more);
  }
}

// ----- histograms (SVG, no D3 — just a few <rect>s) ------------------------

function renderHistogram(svgId, metaId, titleId, binPanelId, titleText, hist) {
  const svg = document.getElementById(svgId);
  const meta = document.getElementById(metaId);
  const title = document.getElementById(titleId);
  const binPanel = document.getElementById(binPanelId);
  title.textContent = titleText;
  svg.innerHTML = '';
  if (binPanel) binPanel.innerHTML = '';
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
  // Bars. Each bin renders TWO rects:
  //
  //   1. The visible bar at its real height. Even a single-dataId bar
  //      gets a 2-pixel minimum height so the eye can pick it out of a
  //      long tail when the y-scale is dominated by a 500-count peak.
  //
  //   2. A transparent full-column overlay sitting on top, the same
  //      width as the bar but spanning the whole plot height. That's
  //      the click / hover target — so the user can drill into an
  //      outlier bin by clicking *anywhere* in its column, not just on
  //      the few pixels of visible bar.
  const dataIdsByBin = hist.dataIdsByBin || [];
  for (let i = 0; i < hist.counts.length; i++) {
    const c = hist.counts[i];
    const rawH = maxCount === 0 ? 0 : (c / maxCount) * plotH;
    const h = c > 0 ? Math.max(2, rawH) : 0;
    const x = padL + i * barW;
    const y = (H - padB) - h;
    const rect = document.createElementNS(ns, 'rect');
    rect.setAttribute('x', x);
    rect.setAttribute('y', y);
    rect.setAttribute('width', Math.max(0.5, barW - 1));
    rect.setAttribute('height', h);
    rect.setAttribute('class', 'night-hist-bar');
    rect.setAttribute('data-bin-index', String(i));
    svg.appendChild(rect);

    const binLo = hist.xMin + i * hist.binWidth;
    const binHi = binLo + hist.binWidth;
    const ids = dataIdsByBin[i] || [];

    // Full-column overlay: same width as the bar, full plot height,
    // transparent, captures pointer events. Tooltip + click handlers
    // live here so the entire column is interactive.
    const hit = document.createElementNS(ns, 'rect');
    hit.setAttribute('x', x);
    hit.setAttribute('y', padT);
    hit.setAttribute('width', Math.max(0.5, barW - 1));
    hit.setAttribute('height', plotH);
    hit.setAttribute('class', 'night-hist-bar-hit');
    hit.setAttribute('data-bin-index', String(i));
    const previewIds = ids.slice(0, 8).join('\n');
    const moreSuffix = ids.length > 8 ? `\n+${ids.length - 8} more (click anywhere in column)` : '';
    const t = document.createElementNS(ns, 'title');
    let titleText = `${binLo.toFixed(2)}–${binHi.toFixed(2)} s\n${c} dataIds`;
    if (ids.length > 0) {
      titleText += '\n\n' + previewIds + moreSuffix + '\n\nclick to drill down';
    }
    t.textContent = titleText;
    hit.appendChild(t);
    // Hover highlight: forward to the visible bar so the user gets
    // visual feedback. Only "live" bins (non-zero, has dataIds) get
    // the highlight + click handler — empty columns stay inert.
    if (c > 0 && ids.length > 0 && binPanel) {
      hit.style.cursor = 'pointer';
      hit.addEventListener('mouseenter', () => rect.classList.add('hover'));
      hit.addEventListener('mouseleave', () => rect.classList.remove('hover'));
      hit.addEventListener('click', () => {
        renderBinPanel(binPanel, svg, i, binLo, binHi, ids);
      });
    }
    svg.appendChild(hit);
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

function renderBinPanel(panel, svg, binIdx, binLo, binHi, dataIds) {
  // Highlight the selected bar so it's clear which bin the panel maps to.
  svg.querySelectorAll('rect.night-hist-bar').forEach((r) => {
    r.classList.toggle('selected', r.getAttribute('data-bin-index') === String(binIdx));
  });
  panel.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'night-hist-bin-header';
  header.innerHTML =
    `<strong>${binLo.toFixed(2)}–${binHi.toFixed(2)} s</strong>`
    + ` · ${dataIds.length} dataId${dataIds.length === 1 ? '' : 's'}`
    + ` · <span class="muted">click any to open its per-visit drilldown</span>`
    + ` <button type="button" class="ghost mini" id="night-hist-bin-close">close</button>`;
  panel.appendChild(header);
  panel.querySelector('#night-hist-bin-close').addEventListener('click', () => {
    panel.innerHTML = '';
    svg.querySelectorAll('rect.night-hist-bar.selected').forEach((r) => r.classList.remove('selected'));
  });
  const list = document.createElement('div');
  list.className = 'night-hist-bin-list';
  for (const id of dataIds) {
    const a = document.createElement('a');
    a.className = 'night-hist-bin-id mono';
    a.href = nightExposureHref(id);
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = String(id);
    const tip = nightExposureTooltip(id);
    if (tip) a.title = tip;
    list.appendChild(a);
  }
  panel.appendChild(list);
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
    const idTd = asTd(r.dataId == null ? '?' : String(r.dataId), 'mono');
    if (r.dataId != null) {
      const tip = nightExposureTooltip(r.dataId);
      if (tip) idTd.title = tip;
    }
    tr.appendChild(idTd);
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
  // The expanded detail row lives directly below the trigger row. Toggle
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
  td.innerHTML = '<div class="night-tb-context">loading…</div>';
  detail.appendChild(td);
  tr.parentNode.insertBefore(detail, tr.nextSibling);
  try {
    const r = await fetch(
      apiUrl(`/api/night/traceback/${encodeURIComponent(row.bodyKey)}`
        + `?dayObs=${encodeURIComponent(nightSummary.dayObs)}`),
    );
    if (!r.ok) {
      td.firstChild.textContent = `(failed to load: HTTP ${r.status})`;
      return;
    }
    const data = await r.json();
    renderTracebackContext(td.firstChild, data);
  } catch (e) {
    td.firstChild.textContent = `(failed to load: ${e})`;
  }
}

function renderTracebackContext(container, data) {
  // Header summarising what's about to be shown — pod, dataId, source
  // of context (whole dataId block vs fixed time-window fallback), and
  // a truncation warning if the body buffer was capped.
  container.innerHTML = '';
  const header = document.createElement('div');
  header.className = 'night-tb-header';
  const idLabel = data.expId == null ? '(no dataId)' : `dataId=${data.expId}`;
  const contextLabel = data.contextSource === 'dataId-block'
    ? `lines from dataId pickup through end of its processing block`
    : `lines in a ±${Math.round(data.firstTs && data.lastTs ? 30 : 0)} s window around the traceback`;
  header.innerHTML =
    `<span class="mono">${escapeHtml(data.pod)}</span>`
    + ` · <span class="mono">${escapeHtml(idLabel)}</span>`
    + ` · ${escapeHtml(contextLabel)}`
    + (data.truncated ? ` <span class="night-tb-truncated">(truncated)</span>` : '');
  container.appendChild(header);

  // Body: each log line on its own row, formatted as
  // `HH:MM:SS.fff  LEVEL  <raw>`. The line that starts the traceback
  // gets a leading divider so it's easy to find by eye.
  const body = document.createElement('pre');
  body.className = 'night-tb-body';
  const tbStartT = data.tracebackTs;
  const fragments = [];
  let firstTracebackLineSeen = false;
  if (!data.lines || data.lines.length === 0) {
    // Fall back to the captured traceback body if the line list is empty
    // (e.g. cache file vanished between fetch and click).
    body.textContent = data.body || '(no context available)';
    container.appendChild(body);
    return;
  }
  for (const ln of data.lines) {
    const t = formatLineTs(ln.t);
    // Traceback continuation lines parse as level=unknown — display
    // blank rather than a noisy "UNKNO" tag.
    const levRaw = (ln.level === 'unknown' ? '' : (ln.level || ''));
    const lev = levRaw.toUpperCase().padEnd(5);
    const lvlClass = ln.level === 'warn' ? 'tb-warn'
      : ln.level === 'error' ? 'tb-error'
      : '';
    let prefix = '';
    // Visual marker on the first line whose timestamp >= tracebackTs —
    // that's the start of the traceback. Render it after a blank line
    // and an arrow so the eye lands on it.
    if (!firstTracebackLineSeen && tbStartT && ln.t >= tbStartT) {
      firstTracebackLineSeen = true;
      prefix = '\n--- traceback below ---\n';
    }
    const safe = escapeHtml(ln.raw);
    fragments.push(
      prefix
      + `<span class="tb-ts">${t}</span>`
      + ` <span class="tb-lvl ${lvlClass}">${lev}</span>`
      + ` ${safe}`
    );
  }
  body.innerHTML = fragments.join('\n');
  container.appendChild(body);
}

function formatLineTs(iso) {
  // "2026-05-21T22:47:25.646000+00:00" -> "22:47:25.646"
  const m = /T(\d\d:\d\d:\d\d(?:\.\d{1,3})?)/.exec(iso || '');
  return m ? m[1] : iso || '';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]
  ));
}

// ----- pod restarts / deaths table ------------------------------------------
//
// POD_* lifecycle events from the k8s/events stream (see parse.classifyK8sEvent).
// `[label, cssSuffix]` per kind; the cssSuffix matches the `.life-*` badge
// colours in style.css and the timeline lifecycle markers.
const LIFECYCLE_LABELS = {
  POD_RESTARTED: ['restart', 'restart'],
  POD_KILLED: ['killed', 'killed'],
  POD_OOMKILLED: ['OOM-killed', 'oom'],
  POD_FAILED: ['failed', 'podfail'],
  POD_UNHEALTHY: ['unhealthy', 'podunhealthy'],
  POD_MOUNT_FAILED: ['mount failed', 'mountfail'],
};

function renderRestarts(rows) {
  const tbody = document.querySelector('#night-restarts tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  rows = rows || [];
  document.getElementById('night-restarts-count').textContent =
    `(${rows.length} event${rows.length === 1 ? '' : 's'})`;
  if (rows.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="6" class="muted">No pod restarts or deaths in this window 🎉</td></tr>';
    return;
  }
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.appendChild(asTd(formatTimeHM(r.tIso), 'mono'));
    // dataId the pod was processing when it died — clickable into its
    // per-visit explore view, same as the failures table.
    const idTd = document.createElement('td');
    idTd.className = 'mono';
    if (r.dataId == null) {
      idTd.textContent = '?';
    } else {
      const a = document.createElement('a');
      a.className = 'mono';
      a.href = nightExposureHref(r.dataId);
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = String(r.dataId);
      const tip = nightExposureTooltip(r.dataId);
      if (tip) a.title = tip;
      idTd.appendChild(a);
    }
    tr.appendChild(idTd);
    tr.appendChild(asTd(r.offsetS == null ? '—' : fmtOffset(r.offsetS), 'mono'));
    tr.appendChild(asTd(r.pod, 'mono night-pod-cell'));
    const evTd = document.createElement('td');
    const [label, cls] = LIFECYCLE_LABELS[r.kind] || [r.kind, 'podfail'];
    const badge = document.createElement('span');
    badge.className = `life-badge life-${cls}`;
    badge.textContent = label;
    evTd.appendChild(badge);
    tr.appendChild(evTd);
    const msgCell = document.createElement('td');
    msgCell.className = 'night-msg-cell';
    msgCell.textContent = r.message || r.reason || '';
    tr.appendChild(msgCell);
    tbody.appendChild(tr);
  }
}
