/* ra-log-explorer — range view: navigate a span of exposures fetched as
 * one wide Loki window.
 *
 * Range mode reuses the per-exposure explore renderer wholesale. This
 * module owns only the navigator strip (#range-nav): a chip per resolved
 * dataId plus prev/next + arrow keys. Selecting a dataId fetches that
 * exposure's timeline (computed server-side from the shared in-memory
 * summaries, anchored at its own shutter close) and hands it to
 * window.startExplore — which renders the timeline without touching the
 * navigator, so the strip stays put while you step through the range.
 */
'use strict';

let rangeIndex = null;          // the index summary: {startId, stopId, dataIds[], nMissing, ...}
let rangeSelectedExpId = null;  // currently-shown dataId
let rangeListenersWired = false;

function startRange(indexSummary) {
  rangeIndex = indexSummary;
  if (!rangeListenersWired) wireRangeListeners();
  renderRangeNav();
  const ids = indexSummary.dataIds || [];
  if (!ids.length) {
    // Token-less server with nothing cached, or a range ConsDB knows
    // nothing about. Leave the timeline empty rather than crash.
    document.getElementById('expId-display').textContent = '';
    document.getElementById('timeline').innerHTML =
      '<div class="range-empty muted">No exposures in this range have a resolved shutter close yet.</div>';
    return;
  }
  // Prefer the dataId named in the URL (reload / deep-link), else the
  // first exposure that actually produced logs, else just the first.
  const urlDataId = parseInt(new URLSearchParams(window.location.search).get('dataId'), 10);
  let initial = ids.find((d) => d.expId === urlDataId);
  if (!initial) initial = ids.find((d) => d.hasLogs) || ids[0];
  loadRangeExposure(initial.expId);
}
window.startRange = startRange;

function renderRangeNav() {
  const idx = rangeIndex;
  const ids = idx.dataIds || [];
  const nSkipped = idx.nMissing || 0;
  const n = ids.length;
  document.getElementById('range-nav-label').textContent =
    `range ${idx.startId} → ${idx.stopId}  ·  ${n} exposure${n === 1 ? '' : 's'}`
    + (nSkipped ? `, ${nSkipped} skipped` : '');
  const chips = document.getElementById('range-chips');
  chips.innerHTML = '';
  for (const d of ids) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'range-chip';
    if (d.nTraceback > 0) chip.classList.add('has-traceback');
    if (!d.hasLogs) chip.classList.add('no-logs');
    if (d.expId === rangeSelectedExpId) chip.classList.add('selected');
    chip.textContent = seqOf(d.expId);
    chip.title = chipTitle(d);
    chip.addEventListener('click', () => loadRangeExposure(d.expId));
    chips.appendChild(chip);
  }
}

function seqOf(expId) {
  // dataIds are YYYYMMDDSSSSS — the chip shows the 5-digit seq number,
  // which is the only part that varies within a single night's range.
  return String(expId).slice(-5);
}

function chipTitle(d) {
  const parts = [`dataId ${d.expId}`, `t₀ ${d.tZero}`, `${d.nPods} pod${d.nPods === 1 ? '' : 's'}`];
  if (d.nTraceback) parts.push(`${d.nTraceback} traceback${d.nTraceback === 1 ? '' : 's'}`);
  if (!d.hasLogs) parts.push('no logs in window');
  // ConsDB properties (filter / image type / reason / …) so the user can
  // tell what kind of image each step in the range is without opening it.
  const fn = window.exposureInfoTooltip;
  const info = fn ? fn(d.exposure) : '';
  if (info) parts.push('', info);
  return parts.join('\n');
}

async function loadRangeExposure(expId) {
  rangeSelectedExpId = expId;
  markSelectedChip();
  const idx = rangeIndex;
  const qs =
    `rangeStart=${encodeURIComponent(idx.startId)}`
    + `&rangeStop=${encodeURIComponent(idx.stopId)}`
    + `&dataId=${encodeURIComponent(expId)}`;
  let payload;
  try {
    const r = await fetch(`/api/summary?${qs}`);
    payload = await r.json();
  } catch (e) {
    return;
  }
  if (!payload || !payload.loaded) return;
  // Keep the URL pointed at the selected exposure so a refresh returns
  // here (bootstrap still routes ?rangeStart&rangeStop to the range view
  // and startRange reads back the dataId).
  window.history.replaceState({}, '', `${window.location.pathname}?${qs}`);
  window.startExplore(payload);
}

function markSelectedChip() {
  const chips = document.getElementById('range-chips').children;
  const ids = (rangeIndex && rangeIndex.dataIds) || [];
  for (let i = 0; i < chips.length; i++) {
    chips[i].classList.toggle('selected', !!ids[i] && ids[i].expId === rangeSelectedExpId);
  }
  const sel = document.querySelector('.range-chip.selected');
  if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function stepRange(delta) {
  const ids = (rangeIndex && rangeIndex.dataIds) || [];
  if (!ids.length) return;
  let i = ids.findIndex((d) => d.expId === rangeSelectedExpId);
  if (i < 0) i = 0;
  const j = Math.min(ids.length - 1, Math.max(0, i + delta));
  if (j !== i) loadRangeExposure(ids[j].expId);
}

function wireRangeListeners() {
  document.getElementById('range-prev').addEventListener('click', () => stepRange(-1));
  document.getElementById('range-next').addEventListener('click', () => stepRange(1));
  window.addEventListener('keydown', (e) => {
    if (document.getElementById('range-nav').hidden) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 'ArrowLeft') { stepRange(-1); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { stepRange(1); e.preventDefault(); }
  });
  rangeListenersWired = true;
}
