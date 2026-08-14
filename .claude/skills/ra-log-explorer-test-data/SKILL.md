---
name: ra-log-explorer-test-data
description: Capture whole nights of rapid-analysis logs from a cluster into local *master* corpora and stage them so the server can serve them. Use this skill when the task needs real data at real scale on the laptop — "download a night", "pull the logs for dayObs N", "get some local data to test against", "run the app against a real night", "set up live mode locally", "rebuild the browser-test corpus", or any work on night summaries, restarts/lifecycle events, histograms or live mode where the hand-trimmed fixtures in `tests/data/` are too small to be meaningful. Covers `tools/captureNight.py`, `tools/stageNight.py`, what a master contains, resuming an interrupted capture, and the traps (never serve a master, never capture a night that hasn't ended, what the k8s/events stream does and does not carry). **Not** for editing the fetch path itself — that's `ra-log-explorer-loki`.
---

# ra_log_explorer: capturing and staging real log data

Two checked-in tools; use them rather than writing a script.

```sh
export LOKI_PASSWORD=...      # VPN must be up; logcli on $PATH

# 1. Capture — one dir per night, into a place the app never looks.
.venv/bin/python tools/captureNight.py \
    --site bts --day-obs 20260811 20260812 --out ~/temp/log_explorer_data/master

# 2. Stage — clone masters into a throwaway cache root.
.venv/bin/python tools/stageNight.py \
    --master ~/temp/log_explorer_data/master/aug11-night-bts \
    --master ~/temp/log_explorer_data/master/aug12-night-bts \
    --cache ~/temp/log_explorer_data/app-cache

# 3. Serve.
RA_LOG_EXPLORER_CACHE=~/temp/log_explorer_data/app-cache \
RA_LOG_EXPLORER_MAX_CACHE_BYTES=32212254720 \
    .venv/bin/python -m ra_log_explorer.cli run --no-browser
```

`--site` names an entry in [sites.toml](../../../ra_log_explorer/sites.toml)
(`bts` → cluster `manke`, `summit` → cluster `yagan`). Nights land in
`<out>/<mon><dd>-night-<site>`; **always keep the site in the name** —
the same dayObs exists on both clusters and is entirely different data.

The long-form explanation is
[*Capturing a night to work against*](../../../architecture/testing.md#capturing-a-night-to-work-against).
This skill is the operational summary.

## Before you start a capture

- **VPN.** `logcli` timeouts or connection errors mean it's down. Ask
  rather than retrying — a capture that half-fails wastes an hour.
- **`LOKI_PASSWORD`.** `captureNight.py` refuses to start without it, on
  purpose: the failure otherwise arrives per-pod, minutes in.
- **Size the job.** Summit (`yagan`) nights are ~9 GiB / 576 pods /
  ~65 min. BTS (`manke`) nights are 50–150 MiB / ~200 pods / ~3 min. Run
  a summit capture in the background and tell the user roughly how long
  it will take.
- **Check the night has ended.** A dayObs runs noon-UTC to noon-UTC, so
  20260813 is not complete until 12:00 UTC on the 14th. The tool refuses
  a live night unless you pass `--allow-partial`; don't reach for that
  flag to avoid waiting — the resulting master claims a completeness it
  doesn't have.

## What you get

```
master/
  aug11-night-bts/            a finalised live-night dir: pods/, pods_events/,
                              pods.txt, _live.json, _meta.json
  aug12-night-bts/
  exposure-times/bts.json     ConsDB records for every night captured, shared
```

Check `_meta.json`'s `fetchComplete`, `errors` and `incomplete_pods`
before using a master for anything — the tool prints all three when it
finishes. A capture is only worth keeping if it came back clean, and
`errors` usually clears on a re-run (re-running resumes; it does not
start over).

## The traps

- **Pass `--site` to the *server* too, not just to the capture.** It
  defaults to the catalog's `default_site` (summit / `yagan`), so a
  staged BTS night served without it produces an empty night view and a
  live fetch against the wrong cluster — which looks like a broken
  capture and isn't.
- **Never point the server at a master.** Stage a clone. One LRU pass,
  one *delete window* click, or one live-mode tick against a master
  destroys hours of fetching. `stageNight.py` uses `cp -c` (APFS
  copy-on-write), so cloning is instant and costs no disk until
  something diverges.
- **Restage to reset.** `stageNight.py` wipes the cache root by default;
  that is the "undo whatever this session did" button.
- **An interrupted capture is resumed, not restarted.** Re-run the exact
  same command. The sidecar's per-pod watermarks and the poller's
  recovery path handle torn bytes; only missing spans are refetched.
- **`--unfinalise <dayObs>`** stages a night as *in progress*, which is
  what `--live-day-obs <dayObs>` needs to replay it as tonight. Without
  it the night stages finalised and every view is served by slicing.
- **The `k8s/events` stream carries no OOM kill.** Neither cluster ships
  `OOMKilling`/`OOMKilled` to Loki — captured nights hold `Killing`,
  `Started`, `Scheduled`, `FailedMount`, `BackOff` and friends. An OOM
  can be *inferred* (a pod's log stopping abruptly plus an in-place
  restart) but only confirmed out-of-band via `kubectl` or Mimir. Don't
  build a feature that claims to detect OOM from these logs alone.
- **Lifecycle volume varies wildly by night.** 20260811 on BTS has 4,644
  event lines (a churny night: `FailedMount`, `FailedCreatePodSandBox`);
  20260812 has 157. If you're testing restart/lifecycle rendering, check
  the night actually contains the events first.
- **`pods_events/` legitimately holds more names than `pods/`.** The
  capture takes the namespace-wide stream, so ReplicaSets, StatefulSets
  and Deployments are in there too. `summarizeAll` enumerates `pods/`, so
  they're never opened; don't "fix" the mismatch.

## Cutting a corpus down

The browser tests' fixture is a master with most of it removed —
`tests/data/ui/build_fixture.py --master <a master night dir>`. See
[tests/data/ui/README.md](../../../tests/data/ui/README.md) for what it
keeps and why.
