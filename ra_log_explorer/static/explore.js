/* ra-log-explorer — explore view: per-exposure timeline + detail drawer.
 *
 * The bootstrap in app.js decides whether to render this view at all, then
 * calls `startExplore()` after the explore-view section is visible. Event
 * listeners for explore-view widgets live inside `startExplore` so they
 * can't fire before the elements exist (the home view is rendered first
 * on a cold start and hides #explore-view via the `hidden` attribute).
 */
'use strict';

// ----- module state ---------------------------------------------------------
let summary = null;
let refIso = null;
let refOffsetS = 0;
let timeMinS = 0;
let timeMaxS = 0;
let pxPerSecond = 8;
let podDetailCache = {};
let selectedPod = null;
let collapsedGroups = new Set();
// Shutter-close UTC epoch ms; set from summary.tZero. This is the *fixed*
// anchor used by the "times as Δshutter" toggle — deliberately separate
// from the user-selectable t₀ used elsewhere.
let shutterUtcMs = null;
let largeGroupThreshold = 10;
let exploreListenersWired = false;  // do the one-time listener wiring lazily

// ----- shared helpers (consumed by both views) ------------------------------

function fmtOffset(s) {
  if (!Number.isFinite(s)) return '?';
  const sign = s < 0 ? '-' : '+';
  const a = Math.abs(s);
  if (a < 60) return `${sign}${a.toFixed(3)}s`;
  const m = Math.floor(a / 60);
  const rem = a - m * 60;
  return `${sign}${m}m${rem.toFixed(2)}s`;
}

function humanBytes(n) {
  if (!Number.isFinite(n)) return '?';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let x = n, i = 0;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x.toFixed(1)} ${u[i]}`;
}

window.fmtOffset = fmtOffset;
window.humanBytes = humanBytes;

const LOG_TS_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3})\s/;

function translateInlineTimestamp(raw) {
  if (!showShutterDelta() || shutterUtcMs == null) return raw;
  const m = LOG_TS_RE.exec(raw);
  if (!m) return raw;
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], +m[7]);
  const deltaS = (t - shutterUtcMs) / 1000;
  const tag = fmtOffset(deltaS).padStart(12);
  return tag + ' ' + raw.slice(m[0].length);
}

function showShutterDelta() {
  const el = document.getElementById('shutter-delta');
  return !!(el && el.checked);
}

function kindClass(kind, level) {
  if (level === 'error') return 'kind-error';
  if (kind === 'WORKER_PICKUP') return 'kind-pickup';
  if (kind === 'WORKER_QG_START' || kind === 'WORKER_QG_BUILT') return 'kind-qg';
  if (kind === 'QUANTUM_PREP' || kind === 'QUANTUM_DONE') return 'kind-quantum';
  if (kind && kind.startsWith('WORKER_BINNED_')) return 'kind-binned';
  if (kind === 'HEAD_FANOUT_START' || kind === 'HEAD_FANOUT_DONE' || kind === 'HEAD_PIPELINE_DECIDED') return 'kind-fanout';
  if (kind === 'HEAD_DEFINE_VISIT' || kind === 'HEAD_INCOMING') return 'kind-fanout';
  if (kind === 'HEAD_GATHER_DISPATCH') return 'kind-gather';
  if (kind === 'HEAD_POSTISR_MOSAIC' || kind === 'HEAD_VISITIMAGE_MOSAIC') return 'kind-mosaic';
  if (kind === 'HEAD_LOOP_SLOW') return 'kind-loopslow';
  if (kind === 'HEAD_ONEOFF') return 'kind-oneoff';
  if (level === 'warn') return 'kind-warn';
  return 'kind-info';
}

function groupOrder() {
  return [
    'head', 'butler-watcher',
    'one-off-exprecord', 'one-off-postisr', 'one-off-visitimage',
    // Each gather pairs with its per-detector workers: step1b consumes
    // sfm output; step1b-aos consumes aos output. Render the gather
    // directly beneath the workers so the dependency reads vertically.
    'sfm', 'step1b',
    'aos', 'step1b-aos',
    'backlog',
    'nightly-worker',
    'mosaic', 'plotter', 'psf-plot', 'fwhm-plot', 'radial-plot', 'zernike-plot',
    'guider',
    'metadata-server', 'metadata-server-aos', 'metadata-server-guiders',
    'metadata-server-ra-performance',
    'cluster-mgr', 'performance-monitor', 'cleanup',
    'other'
  ];
}

// ----- entry point ----------------------------------------------------------

async function startExplore(loadedSummary) {
  // Reset transient state so re-entering the view after a fresh fetch
  // doesn't leak detail caches or selections from the previous run.
  podDetailCache = {};
  selectedPod = null;
  collapsedGroups = new Set();
  summary = loadedSummary;
  if (window.renderFetchBanner) {
    window.renderFetchBanner(document.getElementById('explore-fetch-banner'), summary);
  }
  document.getElementById('expId-display').textContent =
    `expId=${summary.expId}  ·  t₀=${summary.tZero}`;
  shutterUtcMs = new Date(summary.tZero).getTime();
  populateRefSelect();
  populateTaskLegend();
  autoCollapseLargeGroups();
  recomputeTimeRange();
  if (!exploreListenersWired) wireExploreListeners();
  applyPodColumnWidth();
  render();
}

window.startExplore = startExplore;

function autoCollapseLargeGroups() {
  const counts = {};
  for (const p of summary.pods) counts[p.group] = (counts[p.group] || 0) + 1;
  for (const [g, n] of Object.entries(counts)) {
    if (n > largeGroupThreshold) collapsedGroups.add(g);
  }
}

function populateTaskLegend() {
  const target = document.getElementById('task-legend');
  if (!target) return;
  target.innerHTML = '';
  const tc = summary.taskColors || {};
  const tasks = Object.keys(tc).sort();
  if (tasks.length === 0) {
    target.innerHTML = '<span class="muted">(no quantum tasks in window)</span>';
    return;
  }
  for (const t of tasks) {
    const chip = document.createElement('span');
    chip.className = 'lg-task';
    chip.innerHTML = `<span class="swatch" style="background:${tc[t]}"></span>${t}`;
    target.appendChild(chip);
  }
}

function populateRefSelect() {
  const sel = document.getElementById('ref-select');
  sel.innerHTML = '';
  const apiT0 = {
    label: 'API tZero (shutter close)',
    t: summary.tZero,
    offsetS: 0,
    source: 'shutter close'
  };
  const refs = [apiT0, ...(summary.referencePoints || [])];
  refs.forEach((r, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = `${r.label}  (${r.t}, ${fmtOffset(r.offsetS)})`;
    sel.appendChild(opt);
  });
  sel._refs = refs;
  sel.onchange = () => {
    const r = refs[parseInt(sel.value, 10)];
    refIso = r.t;
    refOffsetS = r.offsetS;
    document.getElementById('t0-info').textContent = `(${r.source}, ${r.t})`;
    render();
  };
  sel.selectedIndex = 0;
  sel.onchange();
}

function recomputeTimeRange() {
  let lo = Infinity, hi = -Infinity;
  for (const p of summary.pods) {
    for (const e of p.events) {
      lo = Math.min(lo, e.offsetS);
      hi = Math.max(hi, e.offsetS + (e.durationS || 0));
    }
  }
  if (!Number.isFinite(lo)) { lo = -2; hi = 60; }
  timeMinS = Math.floor(lo - 2);
  timeMaxS = Math.ceil(hi + 2);
}

// ----- render ---------------------------------------------------------------

function render() {
  const tl = document.getElementById('timeline');
  tl.innerHTML = '';
  const filterText = (document.getElementById('search').value || '').toLowerCase();
  const hideQuiet = document.getElementById('hide-quiet').checked;
  tl.appendChild(renderAxis());
  const pods = summary.pods.filter(p => {
    if (hideQuiet && p.events.length === 0) return false;
    if (!filterText) return true;
    if (p.pod.toLowerCase().includes(filterText)) return true;
    return p.events.some(e =>
      (e.raw || '').toLowerCase().includes(filterText) ||
      (e.message || '').toLowerCase().includes(filterText) ||
      String(e.detector || '').includes(filterText)
    );
  });
  const grouped = {};
  for (const p of pods) {
    (grouped[p.group] || (grouped[p.group] = [])).push(p);
  }
  // Anything in `grouped` that isn't named in groupOrder() (e.g. a new
  // role landed in POD_GROUPS but groupOrder wasn't updated) renders at
  // the end in alphabetical order, just before 'other'. Without this
  // fallback such pods would disappear from the timeline silently.
  const known = new Set(groupOrder());
  const extras = Object.keys(grouped).filter(g => !known.has(g)).sort();
  const orderedGroups = [
    ...groupOrder().filter(g => g !== 'other'),
    ...extras,
    'other',
  ];
  for (const g of orderedGroups) {
    const ps = grouped[g];
    if (!ps || ps.length === 0) continue;
    ps.sort((a, b) => {
      if (a.ordinal != null && b.ordinal != null) return a.ordinal - b.ordinal;
      return a.pod.localeCompare(b.pod);
    });
    const foldable = ps.length > 1;
    const isCollapsed = foldable && collapsedGroups.has(g);
    const nTb = ps.reduce((acc, p) => acc + (p.nTraceback || 0), 0);
    const tbPill = nTb ? `<span class="tg-tb-pill">TB ${nTb}</span>` : '';
    const hdr = document.createElement('div');
    hdr.className = 'tl-group-header'
      + (isCollapsed ? ' collapsed' : '')
      + (foldable ? '' : ' static');
    const arrow = foldable ? '<span class="tg-arrow"></span>' : '';
    const groupName = groupDisplay(g);
    hdr.innerHTML = `${arrow}${groupName}  (${ps.length} pod${ps.length === 1 ? '' : 's'})${tbPill}`;
    if (foldable) hdr.addEventListener('click', () => toggleGroup(g));
    tl.appendChild(hdr);
    if (isCollapsed) {
      // Even when the group is folded, surface any pods that produced a
      // traceback in this window — that's almost always what you opened
      // the explorer to find, and hiding it behind a fold defeats the
      // "scan down the left edge for red" workflow the README documents.
      const tbPods = ps.filter(p => (p.nTraceback || 0) > 0);
      for (const p of tbPods) tl.appendChild(renderPodRow(p));
      const hidden = ps.length - tbPods.length;
      if (hidden > 0) {
        const more = document.createElement('div');
        more.className = 'tl-group-more';
        more.textContent =
          `+ ${hidden} more pod${hidden === 1 ? '' : 's'} — click to expand`;
        more.addEventListener('click', () => toggleGroup(g));
        tl.appendChild(more);
      }
      continue;
    }
    for (const p of ps) tl.appendChild(renderPodRow(p));
  }
}

function toggleGroup(g) {
  if (collapsedGroups.has(g)) collapsedGroups.delete(g);
  else collapsedGroups.add(g);
  render();
}

function collapseAllGroups() {
  const counts = {};
  for (const p of summary.pods) counts[p.group] = (counts[p.group] || 0) + 1;
  for (const [g, n] of Object.entries(counts)) {
    if (n > 1) collapsedGroups.add(g);
  }
  render();
}

function expandAllGroups() {
  collapsedGroups.clear();
  render();
}

function trackWidthPx() {
  const span = (timeMaxS - timeMinS) * pxPerSecond;
  return Math.max(span, 800);
}

function xForOffset(offsetApiS) {
  return (offsetApiS - timeMinS) * pxPerSecond;
}

function renderAxis() {
  const wrap = document.createElement('div');
  wrap.id = 'tl-axis';
  const row = document.createElement('div');
  row.className = 'tl-axis-row';
  const spacer = document.createElement('div');
  spacer.className = 'tl-axis-spacer';
  spacer.innerHTML = `<span style="font-size:11px;padding-left:8px;line-height:28px;display:inline-block">${timeMinS}s ... ${timeMaxS}s  (zoom: <button onclick="setZoom(pxPerSecond/1.5);render();return false">-</button><button onclick="setZoom(pxPerSecond*1.5);render();return false">+</button>)</span>`;
  const grip = document.createElement('div');
  grip.className = 'tl-podcol-resize';
  grip.title = 'drag to resize the pod-name column';
  grip.addEventListener('mousedown', startPodColumnDrag);
  spacer.appendChild(grip);
  row.appendChild(spacer);
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('class', 'tl-axis-svg');
  svg.setAttribute('width', trackWidthPx());
  svg.setAttribute('height', 28);
  const span = timeMaxS - timeMinS;
  let step = 1;
  if (span > 30) step = 5;
  if (span > 120) step = 15;
  if (span > 300) step = 30;
  if (span > 900) step = 60;
  for (let s = Math.ceil(timeMinS / step) * step; s <= timeMaxS; s += step) {
    const x = xForOffset(s);
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', x); line.setAttribute('x2', x);
    line.setAttribute('y1', 18); line.setAttribute('y2', 28);
    line.setAttribute('class', 'tl-axis-tick-line');
    svg.appendChild(line);
    const tx = document.createElementNS(svgNS, 'text');
    tx.setAttribute('x', x + 2);
    tx.setAttribute('y', 14);
    tx.setAttribute('class', 'tl-axis-tick');
    tx.textContent = fmtOffset(s - refOffsetS);
    svg.appendChild(tx);
  }
  const x0 = xForOffset(refOffsetS);
  const t0 = document.createElementNS(svgNS, 'line');
  t0.setAttribute('x1', x0); t0.setAttribute('x2', x0);
  t0.setAttribute('y1', 0); t0.setAttribute('y2', 28);
  t0.setAttribute('class', 'tl-axis-t0');
  svg.appendChild(t0);
  row.appendChild(svg);
  wrap.appendChild(row);
  return wrap;
}

function renderPodRow(p) {
  const row = document.createElement('div');
  row.className = 'tl-row';
  if (selectedPod === p.pod) row.classList.add('selected');
  if (p.nTraceback) row.classList.add('has-traceback');
  const truncated = !!p.looksTruncatedEnd;
  if (truncated) row.classList.add('window-truncated');
  const name = document.createElement('div');
  name.className = 'tl-podname';
  const groupName = groupDisplay(p.group);
  const badge = `<span class="badge badge-${p.group}">${groupName}</span>`;
  const warn = p.nWarn ? `<span class="stat warn" title="warnings in window">W:${p.nWarn}</span>` : '';
  const err = p.nError ? `<span class="stat err" title="errors in window">E:${p.nError}</span>` : '';
  const tb = p.nTraceback ? `<span class="stat tb" title="tracebacks in window">TB ${p.nTraceback}</span>` : '';
  const trunc = truncated
    ? `<span class="stat trunc" title="${truncationTooltipText(p)}">⚠ window</span>`
    : '';
  const label = `<span class="podlabel">${shortenPod(p.pod, p.group)}</span>`;
  name.innerHTML = `${badge}${label}${warn}${err}${tb}${trunc}`;
  name.addEventListener('click', () => selectPod(p.pod));
  // Rich hover tooltip with the pod's per-dataId timing stats. Reuses
  // the same floating #tooltip element used for events on the track.
  name.addEventListener('mouseenter', (ev) => showPodTooltip(ev, p));
  name.addEventListener('mousemove', moveTooltip);
  name.addEventListener('mouseleave', hideTooltip);
  row.appendChild(name);
  const track = document.createElement('div');
  track.className = 'tl-track';
  track.style.width = trackWidthPx() + 'px';
  const t0 = document.createElement('div');
  t0.className = 'tl-t0-marker';
  t0.style.left = xForOffset(refOffsetS) + 'px';
  track.appendChild(t0);
  for (const e of p.events) track.appendChild(makeEventNode(e));
  track.addEventListener('click', () => selectPod(p.pod));
  row.appendChild(track);
  return row;
}

function shortenPod(pod, group) {
  // Strip the StatefulSet's "s-<instrument>-run-" prefix and then the role
  // prefix (e.g. "sfm-runner-") so a name like
  // `s-lsstcam-run-sfm-runner-workerset-094` collapses to `workerset-094`.
  // The role prefix is what's already in the badge to the left.
  let stem = pod
    .replace(/^s-lsstcam-run-/, '')
    .replace(/^s-latiss-run-/, '')
    .replace(/^s-lsstcomcamsim-run-/, '')
    .replace(/^s-lsstcomcam-run-/, '');
  const prefix = (summary && summary.groupLabels && summary.groupLabels[group]);
  if (prefix && stem.startsWith(prefix + '-')) {
    stem = stem.slice(prefix.length + 1);
  } else if (prefix && stem === prefix) {
    stem = '';
  }
  return stem;
}

function groupDisplay(group) {
  if (summary && summary.groupLabels && summary.groupLabels[group]) {
    return summary.groupLabels[group];
  }
  return group;
}

function makeEventNode(e) {
  const n = document.createElement('div');
  n.className = 'tl-event ' + kindClass(e.kind, e.level);
  const taskColor = (e.taskLabel && summary.taskColors)
    ? summary.taskColors[e.taskLabel] : null;
  if (e.durationS && e.kind === 'QUANTUM_DONE') {
    n.classList.add('bar');
    const startS = e.offsetS - e.durationS;
    n.style.left = xForOffset(startS) + 'px';
    n.style.width = Math.max(2, e.durationS * pxPerSecond) + 'px';
    if (taskColor) n.style.background = taskColor;
  } else if (e.kind === 'QUANTUM_PREP') {
    n.style.left = xForOffset(e.offsetS) + 'px';
    if (taskColor) n.style.background = taskColor;
  } else if (e.kind === 'WORKER_QG_BUILT' && e.durationS) {
    n.classList.add('bar');
    const startS = e.offsetS - e.durationS;
    n.style.left = xForOffset(startS) + 'px';
    n.style.width = Math.max(2, e.durationS * pxPerSecond) + 'px';
  } else if (e.kind === 'HEAD_LOOP_SLOW' && e.durationS) {
    n.classList.add('bar');
    const startS = e.offsetS - e.durationS;
    n.style.left = xForOffset(startS) + 'px';
    n.style.width = Math.max(2, e.durationS * pxPerSecond) + 'px';
  } else {
    n.style.left = xForOffset(e.offsetS) + 'px';
  }
  n.addEventListener('mouseenter', (ev) => showTooltip(ev, e));
  n.addEventListener('mousemove', moveTooltip);
  n.addEventListener('mouseleave', hideTooltip);
  return n;
}

// ----- tooltip --------------------------------------------------------------

let tooltipEl = null;
function ensureTooltip() {
  if (!tooltipEl) {
    tooltipEl = document.createElement('div');
    tooltipEl.id = 'tooltip';
    document.body.appendChild(tooltipEl);
  }
  return tooltipEl;
}
function showPodTooltip(ev, p) {
  const t = ensureTooltip();
  const lines = [p.pod];
  const fmt = (s) => (s == null ? null : fmtOffset(s));
  const dur = (s) => (s == null ? null : `${s.toFixed(2)}s`);
  const stats = [];
  if (p.firstRelevantOffsetS != null) {
    stats.push(`start (Δshutter) = ${fmt(p.firstRelevantOffsetS)}`);
  }
  if (p.relevantDurationS != null) {
    stats.push(`total duration  = ${dur(p.relevantDurationS)}`);
  }
  if (p.qgBuildSeconds != null) {
    stats.push(`QG build        = ${dur(p.qgBuildSeconds)}`);
  }
  if (p.waitSeconds != null) {
    stats.push(`waiting for load = ${dur(p.waitSeconds)}`);
  }
  if (stats.length > 0) {
    lines.push('---');
    lines.push(...stats);
  }
  if (p.looksTruncatedEnd) {
    lines.push('---');
    lines.push(truncationTooltipText(p));
  }
  t.textContent = lines.join('\n');
  t.style.display = 'block';
  moveTooltip(ev);
}

function truncationTooltipText(_p) {
  return "⚠ fetch window probably ended before this pod finished its work";
}

function showTooltip(ev, e) {
  const t = ensureTooltip();
  const lines = [
    `Δt₀ = ${fmtOffset(e.offsetS - refOffsetS)}`,
    `kind = ${e.kind}`,
  ];
  if (e.who) lines.push(`who  = ${e.who}`);
  if (e.detector != null) lines.push(`det  = ${e.detector}`);
  if (e.taskLabel) lines.push(`task = ${e.taskLabel}`);
  if (e.durationS != null) lines.push(`dur  = ${e.durationS}s`);
  if (e.flavor) lines.push(`flav = ${e.flavor}`);
  lines.push('---');
  const rawText = translateInlineTimestamp(e.raw || e.message || '');
  lines.push(rawText.slice(0, 800));
  t.textContent = lines.join('\n');
  t.style.display = 'block';
  moveTooltip(ev);
}
function moveTooltip(ev) {
  const t = ensureTooltip();
  const pad = 12;
  let x = ev.clientX + pad;
  let y = ev.clientY + pad;
  const w = t.offsetWidth, h = t.offsetHeight;
  if (x + w > window.innerWidth) x = ev.clientX - pad - w;
  if (y + h > window.innerHeight) y = ev.clientY - pad - h;
  t.style.left = x + 'px';
  t.style.top = y + 'px';
}
function hideTooltip() {
  if (tooltipEl) tooltipEl.style.display = 'none';
}

// ----- detail panel --------------------------------------------------------

async function selectPod(pod) {
  selectedPod = pod;
  document.querySelectorAll('.tl-row').forEach(r => r.classList.remove('selected'));
  const detail = document.getElementById('detail');
  detail.classList.remove('closed');
  document.getElementById('detail-title').textContent = pod;
  document.getElementById('detail-body').textContent = 'loading...';
  if (!podDetailCache[pod]) {
    // In range mode the timeline payload carries a podDetailQuery that
    // routes the lookup back through the range state (so offsets anchor
    // at this dataId's shutter close); single-exposure mode just keys
    // off the loaded dataId.
    const q = summary.podDetailQuery || `dataId=${encodeURIComponent(summary.expId)}`;
    const r = await fetch(`/api/pod/${pod}?${q}`);
    podDetailCache[pod] = await r.json();
  }
  renderDetail();
  render();
}

function renderDetail() {
  const pod = selectedPod;
  if (!pod || !podDetailCache[pod]) return;
  const body = document.getElementById('detail-body');
  body.innerHTML = '';
  const detail = podDetailCache[pod];
  const filter = (document.getElementById('detail-search').value || '').toLowerCase();
  const onlyRelevant = document.getElementById('detail-only-relevant').checked;
  const warnOnly = document.getElementById('detail-warn-only').checked;
  const targetExpId = summary.expId;
  let inTraceback = false;
  for (const ln of detail.lines) {
    const raw = ln.raw || '';
    const looksLikeTbStart = raw.includes('Traceback (most recent call last):');
    const isPyTbCont = inTraceback && (
      raw.startsWith('  ') || raw.startsWith('\t') ||
      /^[A-Z][A-Za-z_]+(Error|Exception|Exit):/.test(raw)
    );
    if (looksLikeTbStart) inTraceback = true;
    else if (!isPyTbCont) inTraceback = false;
    const isTraceback = looksLikeTbStart || isPyTbCont;
    if (!isTraceback) {
      if (warnOnly && !(ln.level === 'warn' || ln.level === 'error')) continue;
      // ln.expId is the dataId the server inferred for this line — either
      // the id explicitly on the line, or (for carryover-group pods) the
      // id last seen. A line with no inferred id is dropped here, which
      // is what we want: "lines we can't attribute to this dataId".
      if (onlyRelevant && ln.expId !== targetExpId) continue;
    }
    if (filter && !raw.toLowerCase().includes(filter)) continue;
    const row = document.createElement('div');
    row.className = 'dt-row';
    if (ln.level === 'warn') row.classList.add('level-warn');
    else if (ln.level === 'error') row.classList.add('level-error');
    if (isTraceback) row.classList.add('traceback');
    const off = (ln.offsetS - refOffsetS);
    row.innerHTML = `
      <span class="offset">${fmtOffset(off)}</span>
      <span class="level">${(ln.level || '').toUpperCase()}</span>
      <span class="body"></span>`;
    row.querySelector('.body').textContent = translateInlineTimestamp(raw);
    body.appendChild(row);
  }
}

// ----- one-time listener wiring (deferred until startExplore runs) ----------

function wireExploreListeners() {
  document.getElementById('detail-close').addEventListener('click', () => {
    document.getElementById('detail').classList.add('closed');
    selectedPod = null;
    render();
  });
  document.getElementById('detail-search').addEventListener('input', renderDetail);
  document.getElementById('detail-only-relevant').addEventListener('change', renderDetail);
  document.getElementById('detail-warn-only').addEventListener('change', renderDetail);
  document.getElementById('search').addEventListener('input', render);
  document.getElementById('hide-quiet').addEventListener('change', render);
  document.getElementById('shutter-delta').addEventListener('change', () => {
    if (selectedPod) renderDetail();
  });
  document.getElementById('groups-collapse-all').addEventListener('click', collapseAllGroups);
  document.getElementById('groups-expand-all').addEventListener('click', expandAllGroups);
  document.getElementById('back-home').addEventListener('click', () => {
    // Drop the exposure key out of the URL bar so a subsequent refresh
    // lands on home — not back on whatever exposure we just left.
    history.replaceState({}, '', window.location.pathname);
    if (window.showHome) window.showHome();
  });
  window.addEventListener('keydown', (e) => {
    // Only react when explore is the visible view.
    const explore = document.getElementById('explore-view');
    if (explore.hidden) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === '+' || e.key === '=') { setZoom(pxPerSecond * 1.5); render(); }
    else if (e.key === '-') { setZoom(pxPerSecond / 1.5); render(); }
    else if (e.key === 'Escape') {
      document.getElementById('detail').classList.add('closed');
      selectedPod = null;
      render();
    }
  });
  exploreListenersWired = true;
}

function setZoom(x) {
  pxPerSecond = Math.max(0.5, Math.min(200, x));
}
window.setZoom = setZoom;
window.render = render;  // for the inline-onclick zoom buttons on the axis spacer

// ----- pod-name column resize ----------------------------------------------

const POD_COL_KEY = 'ra_log_explorer.podColWidth';
const POD_COL_MIN = 140;
const POD_COL_MAX = 720;

function applyPodColumnWidth() {
  const stored = parseInt(localStorage.getItem(POD_COL_KEY) || '', 10);
  const w = Number.isFinite(stored) ? clampPodCol(stored) : 280;
  document.documentElement.style.setProperty('--pod-col-width', w + 'px');
}

function clampPodCol(w) {
  return Math.max(POD_COL_MIN, Math.min(POD_COL_MAX, w));
}

function startPodColumnDrag(ev) {
  ev.preventDefault();
  const startX = ev.clientX;
  const rootStyle = getComputedStyle(document.documentElement);
  const startW = parseInt(rootStyle.getPropertyValue('--pod-col-width'), 10) || 280;
  document.body.classList.add('col-resizing');
  function onMove(e) {
    const w = clampPodCol(startW + (e.clientX - startX));
    document.documentElement.style.setProperty('--pod-col-width', w + 'px');
  }
  function onUp() {
    document.body.classList.remove('col-resizing');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--pod-col-width'), 10);
    if (Number.isFinite(cur)) localStorage.setItem(POD_COL_KEY, String(cur));
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}
