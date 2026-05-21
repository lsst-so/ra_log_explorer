# Log Line Parsing & Event Taxonomy

Every Loki log line ends up as a `LogLine` dataclass; a subset are further
classified into `Event` dataclasses with structured fields. This page is
the source of truth for both stages. When the rapid analysis backend
introduces a new log message kind worth surfacing on the timeline, add the
pattern here, in [parse.py](../ra_log_explorer/parse.py), and update the
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

- `timestamp` carries the canonical (UTC, with offset) time. We parse it
  with `datetime.fromisoformat`, truncating to microsecond precision (Loki
  sometimes emits nanoseconds).
- `labels.detected_level` is Loki's promoted log level (`info`, `warn`,
  `error`, …). It can be missing.
- `line` is the raw log line; the rapid analysis Python loggers use a
  consistent format:

  ```
  YYYY-MM-DD HH:MM:SS,mmm <logger> <function>  <LEVEL>   <message>
  ```

  Captured by `_PYLOG_RE` in [parse.py](../ra_log_explorer/parse.py). Lines
  that don't match (third-party libraries, multi-line tracebacks) get a
  fallback `LogLine` with empty `logger`/`function` and the full text in
  `message`/`raw`.

## Event kinds

The classifier in [`parse.classify`](../ra_log_explorer/parse.py) inspects
`LogLine.logger` first, then matches `message` against a kind-specific
regex. Each event carries optional fields populated where the regex can
extract them.

### Head-node events  (logger contains `HeadProcessController` or `processControl`)

| Kind                       | Triggering message fragment                                            | Fields populated         |
|----------------------------|-------------------------------------------------------------------------|--------------------------|
| `HEAD_DEFINE_VISIT`        | `Defining visit (if needed) for <expId>`                                | `expId`                  |
| `HEAD_PIPELINE_DECIDED`    | `Sending <expId> imageType='...' for <rest>`                            | `expId`, `who` (rest)    |
| `HEAD_FANOUT_START`        | `Fanning <inst> out to <n> detectors of <m> enabled`                    | —                        |
| `HEAD_FANOUT_DONE`         | `Sent <n> payloads to free workers, <m> to busy workers for <who>`      | `who`                    |
| `HEAD_GATHER_DISPATCH`     | `Dispatching step1b for <who> with complete inputs: ... visit: <vis>`   | `expId` (=visit), `who`  |
| `HEAD_POSTISR_MOSAIC`      | `Dispatching complete post_isr_image mosaic for expId=<expId>`          | `expId`                  |
| `HEAD_VISITIMAGE_MOSAIC`   | `Dispatching complete preliminary_visit_image mosaic for <expId>`       | `expId`                  |
| `HEAD_ONEOFF`              | `Sending signal to one-off processor for <inst>-<dayObs>-<seq>+PodFlavor.<flavor>` | `expId` (reconstructed), `flavor` |
| `HEAD_LOOP_SLOW`           | `Event loop running slow, last loop took <wall>s with <work>s of work`  | `durationS` (wall)       |

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

### Tracebacks

Detected separately by `PodSummary.summarizePod`: any line containing
`"Traceback (most recent call last):"` increments `nTraceback`. The
classifier doesn't emit a discrete event for them; the per-pod detail UI
keeps the traceback header + continuation lines together via a small JS
state machine.

## Helpers used by the classifier

- `_dataIdFields(body)` extracts `(expId, visit, detector)` from a dataId
  brace-body. When only `visit:` is present (calibrateImage-style quanta)
  it falls back to copying `visit` into `expId` so downstream filters
  don't lose the connection between the visit-keyed quantum and the
  exposure that produced it.

- `_PYLOG_RE` only matches the rapid-analysis Python log format. Lines
  from third-party libraries usually trip the fallback; the timestamp
  still comes from the JSON `timestamp` field, so they remain time-ordered
  in the detail view.

- `_BARE_EXPID_RE = r"\b(202\d{10})\b"` is the catch-all used to attribute
  generic warnings to an exposure when it's mentioned anywhere in the
  raw line.

## Pod classification

`podGroup(pod)` walks `POD_GROUPS` (in order) and returns the first label
whose needle is a substring of the pod name. Order is important:
`step-1b-aos-worker` must be checked before `aos-worker` or it would be
classified as plain `aos`. The list lives in
[parse.py](../ra_log_explorer/parse.py) — keep it sorted from
most-specific to most-general.

`podOrdinal(pod)` parses the trailing StatefulSet ordinal out of names
like `sfm-runner-workerset-094` or `aos-worker-aosworkerset-2`. Used to
sort same-group rows numerically in the UI rather than lexicographically.

## When to add a new event kind

Add a new kind when both apply:

1. The log line is structurally identifiable (a stable substring or
   `<logger> <function>` pair), and
2. surfacing it on the timeline or in a derived reference point would
   meaningfully help reconstruct a pipeline run.

Do **not** add classifications for every interesting log line — generic
WARN/ERROR fallthrough already catches problems, and the per-pod detail
drawer shows everything. The classifier exists for the *timeline*, not as
a replacement for full-text reading.

Whenever you add or change a pattern, update [tests/test_parse.py](../tests/test_parse.py)
with the smallest fixture line that reproduces the match.
