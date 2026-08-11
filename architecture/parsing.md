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

### Pod lifecycle events  (from the `k8s/events` stream, not the app log)

These come from a **different Loki stream** (`job="k8s/events"`), fetched
per pod into `pods_events/<pod>.jsonl` alongside the app logs (see
[caching.md](caching.md)). Their line shape is **not** the LSST Python log
format — it's a flat `key=value` record with a quoted `msg="…"` tail:

```
name=… kind=Pod … reason=Started type=Normal count=2 msg="Started container run-aos-worker"
```

so they get a separate parse + classify path:
[`parse.classifyK8sEvent`](../ra_log_explorer/parse.py) (via
`_parseK8sEventFields`), wholly distinct from `classify`. We surface only
the reasons that explain a pod dropping off the timeline and drop the rest
(image pulls, scheduling, sandbox setup, container *create*). Events whose
`kind` isn't `Pod` (a StatefulSet/ReplicaSet event names the *set*, not the
pod) are dropped too.

| Kind            | k8s `reason`                                            | level   | Notes |
|-----------------|----------------------------------------------------------|---------|-------|
| `POD_RESTARTED` | `Started` with `count ≥ 2`                               | warn    | The container has started before in this pod → it died and was restarted **in place**. The key "explains an abrupt mid-work gap" signal (e.g. an OOM the kernel didn't ship a message for). |
| `POD_STARTED`   | `Started` with `count == 1`                              | info    | First start of the container; mostly relevant in night-wide windows. |
| `POD_KILLED`    | `Killing`                                                | warn    | Container being stopped — graceful (rollout/scale-down) or pre-restart. |
| `POD_OOMKILLED` | reason containing `OOM` (e.g. `OOMKilling`)             | error   | Node-pressure OOM. Note: a *container-limit* OOM emits no k8s event on this cluster (and the kernel line isn't shipped to Loki) — that case shows up only as `POD_RESTARTED`. |
| `POD_FAILED`    | `Failed`/`BackOff`/`Evicted`/`Preempted`/`NodeNotReady`/`FailedKillPod` | error | Container failed / crash-looping / evicted. |
| `POD_UNHEALTHY` | `Unhealthy`                                              | warn    | Liveness/readiness probe failed (often precedes a `Killing`). |
| `POD_MOUNT_FAILED` | `FailedMount`                                         | warn    | The kubelet couldn't mount one of the pod's volumes — the pod is down (or wedged restarting) until it can, so this explains a gap the way a restart does. The kubelet retries on a backoff and emits one event per attempt, so a single incident shows as a small burst of markers. |

**What real data exists behind these.** The nights we have captured are
mostly healthy: their lifecycle streams hold `Started`, `Killing`,
`Pulling`/`Pulled`/`Created`, `Scheduled` and little else. One reason
shows up in the full captures that we deliberately drop (the cut-down
corpus in this repo has no example) — `TaintManagerEviction`, whose
message is *"Cancelling deletion of Pod …"* (the controller calling an
eviction off, not a pod dying). `POD_MOUNT_FAILED` has a real capture
behind it: a cluster-wide secret-sync hiccup on the summit (dayObs
20260711) interrupted five running pods at one moment, ~1h40m into
their app logs — pinned by a fixture line in `tests/test_parse.py`. One
real crash is captured too: a step1b-AOS worker on BTS that restarted
in place five times and then wedged in `ImagePullBackOff`, kept as
`tests/data/pod_crash_events.jsonl` and used by both the parser tests
and the browser tests.

`POD_OOMKILLED` and `POD_UNHEALTHY` have **no** real capture behind them
and are covered by hand-written event lines only. That is not an
oversight: container-limit OOM emits no k8s event on these clusters at
all (see the row above), so there is nothing to capture; `Unhealthy`
simply hasn't occurred in a night we pulled. If one ever does, add it to
the crash fixture rather than inventing a line.

All lifecycle kinds share the `POD_` prefix (and are enumerated in
`parse.LIFECYCLE_EVENT_KINDS`). They carry **no dataId** — `expId` is always
`None`, since they're pod-global, not per-exposure — so `who`/`detector`/
`visit`/`taskLabel`/`durationS` are all unset. The specific k8s `reason`
is kept in `flavor`, and `message` is the event's own text (plus
`(restart #N)` and the node for a restart). The server includes them on any
pod already in the timeline, windowed by time rather than by dataId (see
the `_summaryToDict` note in [architecture.md](architecture.md)).

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
| `excClass`   | first exception class line seen inside the body, e.g. `RuntimeError`. Module-qualified shapes like `galsim.errors.GalSimRangeError` are stripped to the rightmost segment. When no class is recognised, one of two sentinels is used, kept distinct: `<unclassified>` if the traceback reached its terminating exception line but the class wasn't in the classifier's suffix set (e.g. `StopIteration`, a custom `Halt: …`) — the record is complete, just unnamed; `<truncated>` if the body was cut short before any terminator (the log forwarder dropped the tail, or another logger interleaved a line mid-stack). |
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

`podInstrument(pod)` returns `"LSSTCam"`, `"LATISS"`, or `None`. Those
two are the whole set this repo covers, so the needles no longer
collide as substrings of one another and the match order carries no
meaning. `None` means *instrument-neutral* (redis, cluster-manager, …)
rather than unknown, and such pods are attributed to whichever exposure
is being viewed.

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
