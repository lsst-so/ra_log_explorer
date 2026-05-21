# Testing

The project has two test layers:

1. **Unit tests** under [tests/](../tests/) — pure-Python, no network or
   filesystem state required. Run with `pytest`.
2. **End-to-end smoke test** — running the CLI against the real Loki
   cluster for a known dataId, which we do by hand before tagging a
   release-worthy state. No automated CI exists yet.

Pre-commit and the unit tests together are the supported validation
loop; see the [ra-log-explorer-validation](../.claude/skills/ra-log-explorer-validation/SKILL.md)
skill for the exact commands.

## Unit-test scope

The unit tests target the deterministic pieces of the codebase:

| Area               | What's tested                                                              | File                              |
|--------------------|----------------------------------------------------------------------------|------------------------------------|
| Log line parsing   | `parseLogLine` against the rapid-analysis Python log format and fallbacks  | `tests/test_parse.py`             |
| Event classification | `classify` for every kind in [parsing.md](parsing.md)                    | `tests/test_parse.py`             |
| Pod classification | `podGroup`, `podOrdinal`, `podInstrument`                                  | `tests/test_parse.py`             |
| Per-pod summary    | `summarizePod` against a JSONL fixture, including traceback counting       | `tests/test_parse.py`             |
| Cache paths        | `windowCachePath` determinism; `ensureWindowCacheDir` is the only I/O side  | `tests/test_config.py`            |
| Cache reuse        | `findSupersetCache` exact/superset/no-meta/partial-flag/smallest-wins      | `tests/test_fetch.py`             |
| Misc helpers       | `humanBytes`, `_parseIso` (timezone handling)                              | `tests/test_fetch.py`             |
| Task palette       | `_assignTaskColors` collision-freeness up to palette length, stable order   | `tests/test_server.py`            |
| JSON serialisation | `_toJsonable` for `datetime`, `set`, dataclass, nested containers          | `tests/test_server.py`            |
| Job manager        | `FetchJob` event ordering, `JobManager.runJob` happy path + error, condvar wake | `tests/test_jobs.py`         |
| HTTP endpoints     | `/api/summary` (empty/loaded), `/api/cache` (partial/no-meta), `/api/fetch` (body validation, 202 + status polling), `/api/pod` (404 when no state), `_buildSpecFromRequest` (TAI/UTC, password passthrough, validation errors) | `tests/test_server_endpoints.py` |
| CLI parsing        | `_parseIsoUtc` for `Z`, explicit offset, naive (assumed UTC); `cmdRun` rejects partial --exposure-id/--t-zero | `tests/test_cli.py`     |

What we don't unit-test:

- `fetch._run_logcli` and `listPods` — they shell out and depend on a
  real Loki instance. The end-to-end smoke test exercises them.
- The HTTP handler in `server.py` — small enough to read, and exercised
  by the smoke test. We could add `http.client`-based tests if regressions
  appear.
- The browser UI. We rely on hand verification.

## Fixtures

Sample Loki JSONL lines live under [tests/data/](../tests/data/):

- `head_node_sample.jsonl`  — representative head-node events covering
  every `HEAD_*` kind plus one `WARN`.
- `sfm_worker_sample.jsonl` — an SFM worker doing one full step1a
  (pickup → QG build → ISR quantum → calibrateImage quantum → binned
  visit image written).
- `aos_worker_sample.jsonl` — the longer AOS quantum chain (ISR plus
  the donut tasks).
- `traceback_sample.jsonl` — a small synthetic traceback so the
  detail-drawer logic has something to grab.

These are hand-trimmed from real cluster logs to keep the fixtures small
and to make the test assertions specific. When you extend the parser to
recognise a new event kind, add the minimal triggering line to the
relevant fixture and a test assertion that pins the parsed event.

## Running the suite

From the repo root, with the venv active or its tools on `$PATH`:

```bash
.venv/bin/pytest -q
```

The runtime itself is stdlib-only; pytest is a dev-only dependency that
lives in the venv alongside black/isort/flake8/mypy.

## End-to-end smoke test

There's no CI yet. Before declaring work done, hand-verify against a
known exposure:

```bash
export LOKI_PASSWORD=...
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267 \
    --no-browser
```

Then `curl http://127.0.0.1:8765/api/summary` and sanity-check:

- `referencePoints` includes the two stable refs (shutter close,
  head-node first defined visit).
- `taskColors` has more unique values than tasks (i.e. no collisions).
- `len(pods)` is roughly what you expect for the exposure (210 for a
  full LSSTCam science visit).
- Pod groups break down sensibly (1 head, ~189 sfm, 8 aos, …).
