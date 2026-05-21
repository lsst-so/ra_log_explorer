/* ra-log-explorer client */
'use strict';

// ----- state -----------------------------------------------------------------
let summary = null;       // /api/summary payload
let refIso = null;        // current t=0 ISO string
let refOffsetS = 0;       // current t=0 offset, relative to API's tZero
let timeMinS = 0;         // window start in seconds relative to API's tZero
let timeMaxS = 0;         // window end
let pxPerSecond = 8;      // initial scale; user can adjust via zoom buttons
let podDetailCache = {};  // pod -> full detail payload
let selectedPod = null;

// ----- helpers ---------------------------------------------------------------

function fmtOffset(s) {
  if (!Number.isFinite(s)) return '?';
  const sign = s < 0 ? '-' : '+';
  const a = Math.abs(s);
  if (a < 60) return `${sign}${a.toFixed(3)}s`;
  const m = Math.floor(a / 60);
  const rem = a - m * 60;
  return `${sign}${m}m${rem.toFixed(2)}s`;
}

function isoToDate(iso) {
  return new Date(iso);
}

function kindClass(kind, level) {
  if (level === 'error') return 'kind-error';
  if (kind === 'WORKER_PICKUP') return 'kind-pickup';
  if (kind === 'WORKER_QG_START' || kind === 'WORKER_QG_BUILT') return 'kind-qg';
  if (kind === 'QUANTUM_PREP' || kind === 'QUANTUM_DONE') return 'kind-quantum';
  if (kind && kind.startsWith('WORKER_BINNED_')) return 'kind-binned';
  if (kind === 'HEAD_FANOUT_START' || kind === 'HEAD_FANOUT_DONE' || kind === 'HEAD_PIPELINE_DECIDED') return 'kind-fanout';
  if (kind === 'HEAD_DEFINE_VISIT') return 'kind-fanout';
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
    'sfm', 'aos', 'backlog',
    'step1b', 'step1b-aos',
    'mosaic', 'psf-plot', 'fwhm-plot', 'radial-plot', 'zernike-plot',
    'guider',
    'metadata-server', 'cluster-mgr', 'other'
  ];
}

// ----- fetch -----------------------------------------------------------------

async function loadSummary() {
  const r = await fetch('/api/summary');
  summary = await r.json();
  document.getElementById('expId-display').textContent =
    `expId=${summary.expId}  ·  t₀=${summary.tZero}`;
  const cb = summary.cacheBytes;
  const meta = summary.meta || {};
  const dl = meta.total_bytes || 0;
  const fromCache = meta.fromCache ? ' (from cache)' : ' (freshly downloaded)';
  document.getElementById('cache-display').textContent =
    `downloaded: ${human(dl)}${fromCache}  ·  cache: ${human(cb)} @ ${summary.cacheDir}`;
  populateRefSelect();
  recomputeTimeRange();
  render();
}

function human(n) {
  if (!Number.isFinite(n)) return '?';
  const u = ['B','KiB','MiB','GiB','TiB'];
  let x = n, i = 0;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x.toFixed(1)} ${u[i]}`;
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
  sel.addEventListener('change', () => {
    const r = refs[parseInt(sel.value, 10)];
    refIso = r.t;
    refOffsetS = r.offsetS;
    document.getElementById('t0-info').textContent =
      `(${r.source}, ${r.t})`;
    render();
  });
  // default to shutter close
  sel.selectedIndex = 0;
  sel.dispatchEvent(new Event('change'));
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
  // pad slightly
  timeMinS = Math.floor(lo - 2);
  timeMaxS = Math.ceil(hi + 2);
}

// ----- render ---------------------------------------------------------------

function render() {
  const tl = document.getElementById('timeline');
  tl.innerHTML = '';

  const filterText = (document.getElementById('search').value || '').toLowerCase();
  const hideQuiet = document.getElementById('hide-quiet').checked;

  // axis
  tl.appendChild(renderAxis());

  // group pods
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
  for (const g of groupOrder()) {
    const ps = grouped[g];
    if (!ps || ps.length === 0) continue;
    const hdr = document.createElement('div');
    hdr.className = 'tl-group-header';
    hdr.textContent = `${g}  (${ps.length} pod${ps.length === 1 ? '' : 's'})`;
    tl.appendChild(hdr);
    // sort within group by ordinal then name
    ps.sort((a, b) => {
      if (a.ordinal != null && b.ordinal != null) return a.ordinal - b.ordinal;
      return a.pod.localeCompare(b.pod);
    });
    for (const p of ps) tl.appendChild(renderPodRow(p));
  }
}

function trackWidthPx() {
  const span = (timeMaxS - timeMinS) * pxPerSecond;
  return Math.max(span, 800);
}

function xForOffset(offsetApiS) {
  // offsetApiS is relative to API tZero. Convert to time-axis coords.
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
  row.appendChild(spacer);

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('class', 'tl-axis-svg');
  svg.setAttribute('width', trackWidthPx());
  svg.setAttribute('height', 28);

  // pick a sensible step
  const span = timeMaxS - timeMinS;
  let step = 1;
  if (span > 30) step = 5;
  if (span > 120) step = 15;
  if (span > 300) step = 30;
  if (span > 900) step = 60;

  for (let s = Math.ceil(timeMinS / step) * step; s <= timeMaxS; s += step) {
    const x = xForOffset(s);
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', x);
    line.setAttribute('x2', x);
    line.setAttribute('y1', 18);
    line.setAttribute('y2', 28);
    line.setAttribute('class', 'tl-axis-tick-line');
    svg.appendChild(line);
    const tx = document.createElementNS(svgNS, 'text');
    tx.setAttribute('x', x + 2);
    tx.setAttribute('y', 14);
    tx.setAttribute('class', 'tl-axis-tick');
    const refS = s - refOffsetS;
    tx.textContent = fmtOffset(refS);
    svg.appendChild(tx);
  }
  // t=0 line (using selected reference offset)
  const x0 = xForOffset(refOffsetS);
  const t0 = document.createElementNS(svgNS, 'line');
  t0.setAttribute('x1', x0);
  t0.setAttribute('x2', x0);
  t0.setAttribute('y1', 0);
  t0.setAttribute('y2', 28);
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

  const name = document.createElement('div');
  name.className = 'tl-podname';
  const badge = `<span class="badge">${p.group}</span>`;
  const ordPart = p.ordinal != null ? `<span class="stat">·${p.ordinal}</span>` : '';
  const warn = p.nWarn ? `<span class="stat warn">W:${p.nWarn}</span>` : '';
  const err = p.nError ? `<span class="stat err">E:${p.nError}</span>` : '';
  const tb = p.nTraceback ? `<span class="stat err">TB:${p.nTraceback}</span>` : '';
  name.innerHTML = `${badge}${shortenPod(p.pod)}${ordPart} ${warn} ${err} ${tb}`;
  name.title = p.pod;
  name.addEventListener('click', () => selectPod(p.pod));
  row.appendChild(name);

  const track = document.createElement('div');
  track.className = 'tl-track';
  track.style.width = trackWidthPx() + 'px';

  // t=0 marker line within row
  const t0 = document.createElement('div');
  t0.className = 'tl-t0-marker';
  t0.style.left = xForOffset(refOffsetS) + 'px';
  track.appendChild(t0);

  for (const e of p.events) {
    track.appendChild(makeEventNode(e));
  }
  track.addEventListener('click', () => selectPod(p.pod));
  row.appendChild(track);
  return row;
}

function shortenPod(pod) {
  // strip the long "s-lsstcam-run-" prefix etc. but keep the rest readable
  return pod.replace(/^s-lsstcam-run-/, '').replace(/^s-latiss-run-/, 'la:');
}

function makeEventNode(e) {
  const n = document.createElement('div');
  n.className = 'tl-event ' + kindClass(e.kind, e.level);
  if (e.durationS && (e.kind === 'QUANTUM_DONE')) {
    // draw quantum as a bar from start (offset - duration) to offset
    n.classList.add('bar');
    const startS = e.offsetS - e.durationS;
    n.style.left = xForOffset(startS) + 'px';
    n.style.width = Math.max(2, (e.durationS) * pxPerSecond) + 'px';
  } else if (e.kind === 'HEAD_LOOP_SLOW' && e.durationS) {
    n.classList.add('bar');
    const startS = e.offsetS - e.durationS;
    n.style.left = xForOffset(startS) + 'px';
    n.style.width = Math.max(2, (e.durationS) * pxPerSecond) + 'px';
  } else {
    n.style.left = xForOffset(e.offsetS) + 'px';
  }
  // We deliberately don't set `n.title` here — the custom dark tooltip
  // (showTooltip) already presents this info richly, and the browser's
  // default title-attribute tooltip would render a duplicate on top of it.
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
  lines.push((e.raw || e.message || '').slice(0, 800));
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
    const r = await fetch(`/api/pod/${pod}`);
    podDetailCache[pod] = await r.json();
  }
  renderDetail();
  render();  // refresh selection highlight
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
  const targetExpId = String(summary.expId);

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

    // filter logic — traceback lines are always kept so multi-line tracebacks
    // render in their entirety regardless of the relevance/warn toggles.
    const isTraceback = looksLikeTbStart || isPyTbCont;
    if (!isTraceback) {
      if (warnOnly && !(ln.level === 'warn' || ln.level === 'error')) continue;
      if (onlyRelevant && !raw.includes(targetExpId)) continue;
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
    row.querySelector('.body').textContent = raw;
    body.appendChild(row);
  }
}

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

function setZoom(x) {
  pxPerSecond = Math.max(0.5, Math.min(200, x));
}
window.setZoom = setZoom;

// keyboard shortcuts
window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === '+' || e.key === '=') { setZoom(pxPerSecond * 1.5); render(); }
  else if (e.key === '-') { setZoom(pxPerSecond / 1.5); render(); }
  else if (e.key === 'Escape') {
    document.getElementById('detail').classList.add('closed');
    selectedPod = null;
    render();
  }
});

loadSummary().catch(e => {
  document.body.innerHTML = `<pre style="padding:14px;color:#c1252b">Error loading summary: ${e}</pre>`;
});
