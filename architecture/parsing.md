# Log Line Parsing & Event Taxonomy

Every Loki log line ends up as a `LogLine` dataclass; a subset are
further classified into `Event` dataclasses with structured fields;
multi-line Python tracebacks are captured separately into
`TracebackRecord`s during the per-pod summary pass. This page is the
source of truth for all three stages. When the rapid analysis backend
introduces a new log message kind worth surfacing, add the pattern
here, in [parse.py](../ra_log_explorer/parse.py), and update the
fixtures + tests in [tests/](../tests/).

## Loki JSONL shape

`logcli ... -o jsonl --forward` emits one JSON object per line:

```json
{
  "labels": {"detected_level": "info"},
  "line":   "2026-05-20 08:45:46,216 lsst.rubintv.production.processControl.HeadProcessController doDetectorFanout INFO   Fanning ... \n",
  "timestamp": "2026-05-20T09:45:46.216887282+01:00"
}
```

- `timestamp` carries the canonical (UTC, with offset) time. We parse
  it with `datetime.fromisoformat`, truncating to microsecond
  precision (Loki sometimes emits nanoseconds). A timestamp that
  doesn't parse drops the whole record; the parser logs nothing.
- `labels.detected_level` is Loki's promoted log level (`info`,
  `warn`, `error`, …). It can be missing. The classifier normalises
  it: `crit`/`critical`/`fatal` collapse to `error`; `warning` to
  `warn`. The label level wins over the inline `<LEVEL>` field on the
  raw line when both are present.
- `line` is the raw log line; the rapid analysis Python loggers use
  a consistent format:

  ```
  YYYY-MM-DD HH:MM:SS,mmm <logger> <function>  <LEVEL>   <message>
  ```

  Captured by `_PYLOG_RE` in [parse.py](../ra_log_explorer/parse.py).
  Lines that don't match (third-party libraries, multi-line traceback
  bodies) get a fallback `LogLine` with empty `logger`/`function` and
  the full text in `message`/`raw`.

## Event kinds

The classifier in [`parse.classify`](../ra_log_explorer/parse.py)
inspects `LogLine.logger` first, then matches `message` against a
kind-specific regex. Each event carries optional fields populated
where the regex can extract them.

### Head-node events  (logger contains `processControl`)

| Kind                       | Triggering message fragment                                            | Fields populated         |
|----------------------------|-------------------------------------------------------------------------|--------------------------|
| `HEAD_INCOMING`            | `New exposure record for <expId>`                                       | `expId`                  |
| `HEAD_DEFINE_VISIT`        | `Defining visit (if needed) for <expId>`                                | `expId`                  |
| `HEAD_PIPELINE_DECIDED`    | `Sending <expId> imageType='...' for <rest>`                            | `expId`, `who` (rest)    |
| `HEAD_FANOUT_START`        | `Fanning <inst> out to <n> detectors of <m> enabled`                    | —                        |
| `HEAD_FANOUT_DONE`         | `Sent <n> payloads to free workers, <m> to busy workers for <who>`      | `who`                    |
| `HEAD_GATHER_DISPATCH`     | `Dispatching step1b for <who> with complete inputs: ... visit: <vis>`   | `expId` (=visit), `who`  |
| `HEAD_POSTISR_MOSAIC`      | `Dispatching complete post_isr_image mosaic for expId=<expId>`          | `expId`                  |
| `HEAD_VISITIMAGE_MOSAIC`   | `Dispatching complete preliminary_visit_image mosaic for <expId>`       | `expId`                  |
| `HEAD_ONEOFF`              | `Sending signal to one-off processor for <inst>-<dayObs>-<seq>+PodFlavor.<flavor>` | `expId` (reconstructed), `flavor` |
| `HEAD_LOOP_SLOW`           | `Event loop running slow, last loop took <wall>s with <work>s of work`  | `durationS` (wall)       |

`HEAD_INCOMING` is the earliest head-node-side moment for an
exposure: butler-watcher → head-node handoff. The
`HEAD_INCOMING → HEAD_DEFINE_VISIT` gap is the butler `defineVisit`
cost.

### Worker events  (logger contains `SingleCorePipelineRunner`)

| Kind                                | Triggering message fragment                                    | Fields populated                |
|-------------------------------------|----------------------------------------------------------------|----------------------------------|
| `WORKER_PICKUP`                     | `Running pipeline for {<dataId-body>}`                         | `expId`, `visit`, `detector`     |
| `WORKER_WAIT_RAW`                   | `Waiting for raw for {<dataId-body>}`                          | `expId`, `visit`, `detector`     |
| `WORKER_QG_START`                   | `Making <kind>QG builder for <step> for expId <exp> for <who>` | `expId`, `who`                   |
| `WORKER_QG_BUILT`                   | `Building quantum graph for {<dataId>} for <who> took <dur>s`  | `expId`, `visit`, `detector`, `who`, `durationS` |
| `WORKER_BINNED_<KIND>`              | `Wrote binned <kind> for {<dataId-body>}`                      | `expId`, `visit`, `detector`. `<KIND>` is the binned dataset kind (e.g. `POST_ISR_IMAGE`, `PRELIMINARY_VISIT_IMAGE`). |
| `WORKER_REPORT_FINISHED`/`_FAILED`  | `Reporting <who> finished\|failed for detector <det> of exposure <exp>` | `expId`, `detector`, `who` |

### Quantum execution events  (logger contains `single_quantum_executor`)

| Kind            | Triggering message fragment                                                | Fields populated                                |
|------------------|-----------------------------------------------------------------------------|--------------------------------------------------|
| `QUANTUM_PREP`   | `Preparing execution of quantum for label=<task> dataId={<body>}`           | `expId`, `visit`, `detector`, `taskLabel`        |
| `QUANTUM_DONE`   | `Execution of task '<task>' on quantum {<body>} took <dur> seconds`         | `expId`, `visit`, `detector`, `taskLabel`, `durationS` |

### Generic WARN / ERROR fallthrough

Any line with `detected_level == "warn"` or `"error"` and a non-empty
`logger` that didn't match a more specific pattern becomes:

| Kind     | When                          | Fields populated |
|----------|--------------------------------|-------------------|
| `WARN`   | `level == "warn"`              | `expId` (only if a bare 13-digit dataId appears in `raw`) |
| `ERROR`  | `level == "error"`             | `expId` (best-effort, as above) |

### Tracebacks  (captured by `summarizePod`, not `classify`)

Tracebacks are captured separately because they span multiple log
lines. The state machine in
[`parse.summarizePod`](../ra_log_explorer/parse.py) looks for
`Traceback (most recent call last):` as a leader and then keeps
appending body lines as long as they look like a Python traceback
continuation (indented frame lines, `During handling…` / `The above
exception…` chain markers, or an exception class line).

Each captured traceback becomes a `TracebackRecord`:

| Field        | What                                                              |
|--------------|--------------------------------------------------------------------|
| `pod`        | pod name                                                          |
| `t`          | timestamp of the leader line                                       |
| `expId`      | carryover-attributed dataId for worker pods; bare-id-on-the-line for control-plane pods; `None` if neither |
| `excClass`   | first exception class line seen inside the body, e.g. `RuntimeError`. Module-qualified shapes like `galsim.errors.GalSimRangeError` are stripped to the rightmost segment. Stays `<unknown>` if no class line is found before the body ends. |
| `excMessage` | the rest of the exception line, capped at 200 chars                |
| `body`       | the full traceback text, capped at `_TRACEBACK_MAX_LINES = 250` lines and `_TRACEBACK_MAX_CHARS = 32_000` chars |

Class detection continues past the body cap so a pathological
30-frame traceback still gets the right `excClass` even though the
body itself is truncated.

A blank line currently terminates traceback capture — that's the
known limitation pinned by `test_summarizePod_blank_line_terminates_traceback_capture`.

## DataId attribution: `extractExpId` and carryover

Lines mention dataIds in two shapes:

- **Bare 13-digit form**: `2026051900722`, matched by
  `_BARE_EXPID_RE = r"\b(202\d{10})\b"`.
- **Split form**: `day_obs=20260519`, `seqNum: 722`, `day-obs:
  20260519` etc., matched per-line by `_DAYOBS_RE` + `_SEQNUM_RE` (any
  of camelCase / snake_case / squashed spellings, paired on the same
  line). Combined id is `dayObs * 100000 + seqNum`.

When both shapes appear on a line, the bare form wins.

### Carryover  (worker pods)

For pods classified as `sfm`, `aos`, `step1b`, `step1b-aos`,
`backlog`, `nightly-worker`, `mosaic`, `guider`, `plotter`,
`psf-plot`, `fwhm-plot`, `radial-plot`, `zernike-plot`,
`one-off-exprecord`, `one-off-postisr`, `one-off-visitimage`, every
line *after* the first dataId mention belongs to that dataId until
a new dataId appears. This matches how those pods actually process
one exposure at a time. `parse.carryoverGroups()` is the source of
truth.

Control-plane and cluster-wide pods (head, butler-watcher,
metadata-server, metadata-server-aos, metadata-server-guiders,
metadata-server-ra-performance, cluster-mgr, cleanup,
performance-monitor) interleave many dataIds in one stream, so they
are deliberately excluded from carryover. Only lines that explicitly
mention a dataId get attributed.

`PodSummary.expIdFirstLast`, `expIdWaitSeconds`, and
`TracebackRecord.expId` all use this rule.

## Helpers used by the classifier

- `_dataIdFields(body)` extracts `(expId, visit, detector)` from a
  dataId brace-body. When only `visit:` is present
  (`calibrateImage`-style quanta) it falls back to copying `visit`
  into `expId` so downstream filters don't lose the connection
  between the visit-keyed quantum and the exposure that produced it.

- `_PYLOG_RE` only matches the rapid-analysis Python log format.
  Lines from third-party libraries usually trip the fallback; the
  timestamp still comes from the JSON `timestamp` field, so they
  remain time-ordered in the detail view.

- `_BARE_EXPID_RE = r"\b(202\d{10})\b"` is the catch-all used to
  attribute generic warnings to an exposure when it's mentioned
  anywhere in the raw line.

## Pod classification

`podGroup(pod)` strips the leading `s-<instrument>-run-` stem and
then matches the remaining prefix against `POD_GROUPS` using
**longest-prefix-wins**. Order in the dict doesn't matter:
`metadata-server-aos` always beats `metadata-server`, and
`step-1b-aos-worker` always beats `aos-worker`, regardless of
dict iteration order. This was deliberately re-architected to remove
the substring-collision footgun the previous first-needle-wins
implementation had.

Pods that don't match any known role fall into `"other"` — the UI
surfaces those anyway as a safety net for new / unrecognised roles.

`podOrdinal(pod)` parses the trailing StatefulSet ordinal out of
names like `sfm-runner-workerset-094` or
`aos-worker-aosworkerset-2`. Used to sort same-group rows
numerically in the UI rather than lexicographically.

`podInstrument(pod)` returns one of `"LSSTCam"`, `"LATISS"`,
`"LSSTComCam"`, `"LSSTComCamSim"`, or `None`. The list is order-
sensitive (`LSSTComCamSim` must be checked before `LSSTComCam`).

## When to add a new event kind

Add a new kind when both apply:

1. The log line is structurally identifiable (a stable substring or
   `<logger> <function>` pair), and
2. surfacing it on the timeline or in a derived reference point
   would meaningfully help reconstruct a pipeline run.

Do **not** add classifications for every interesting log line —
generic WARN/ERROR fallthrough already catches problems, and the
per-pod detail drawer shows everything. The classifier exists for
the *timeline*, not as a replacement for full-text reading.

Whenever you add or change a pattern, update [tests/test_parse.py](../tests/test_parse.py)
with the smallest fixture line that reproduces the match, plus an
assertion that pins the parsed event's expected fields.
