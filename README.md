# ra_log_explorer

Interactive log exploration for the **rapid analysis** backend
(Vera C. Rubin Observatory). Given an LSSTCam exposure id and the
shutter-close time from its Butler `DimensionRecord`, it pulls every
pod's logs from the cluster's Loki for a window around that time and
serves a browser timeline that shows what each pod did, when, and where
the warnings / errors / tracebacks landed.

![timeline placeholder]()

Designed for the typical question:

> "Why was this exposure slow / why did this detector fail / where was the
> hold-up between step1a finishing and step1b dispatching?"

It is **not** a streaming log tail; it works on snapshots of a fixed
window. One exposure at a time.

## What you need

| Requirement                       | Notes                                                                                |
|-----------------------------------|---------------------------------------------------------------------------------------|
| Python ≥ 3.11                     | 3.13 recommended; the runtime itself is stdlib-only (no `pip install` needed).        |
| `logcli` on your `$PATH`          | `brew install grafana/grafana/logcli` on macOS, or grab a binary from Grafana releases. |
| `LOKI_PASSWORD` in your environment | Set it in your shell rc (e.g. `~/.zshrc`).                                          |
| `git` to clone this repo          | There is no published PyPI package; you run it from a checkout.                       |
| A browser                         | The tool opens `http://127.0.0.1:8765/` for you.                                      |

## Install

There's nothing to install besides the repo. The tool runs out of a
checkout:

```
git clone git@github.com:lsst-so/ra_log_explorer.git
cd ra_log_explorer
export LOKI_PASSWORD=...     # ideally pinned in your shell rc
```

## Run

There are two paths into the tool. Pick whichever is more comfortable.

### Home mode (recommended)

```
python3 -m ra_log_explorer.cli
```

Starts the server with nothing loaded and opens the browser at
`http://127.0.0.1:8765/`. From there you:

1. Type your **Loki username + password** in the credentials card
   (leave password blank to fall back to `LOKI_PASSWORD` in the
   environment). Tick "remember in this browser" if you'd like them
   kept in `localStorage`. A "forget stored credentials" button
   clears the slot any time.
2. Type a **dataId** (e.g. `2026051900722`) and the **shutter-close
   time** (e.g. `2026-05-20T08:46:16.267`).
   The shutter-close defaults to being interpreted as **TAI** —
   matching the Butler `DimensionRecord.timespan.end.isot` field;
   tick "t₀ is already UTC" if you've already done the conversion.
3. Optionally open *Advanced options* to tweak cluster, namespace,
   worker count, or the pre-/post-shutter window padding.
4. Click **Fetch & explore**.

A progress bar tracks per-pod completion as the fetch runs (live
Server-Sent Events from the backend). When it finishes the page
switches automatically to the timeline / explore view; the back-arrow
button in the explore topbar returns you to the home page.

The home page also lists every cached window on disk; clicking a row
copies its cluster/namespace/window settings into the fetch form, so a
subsequent fetch of the same exposure id cache-hits instantly.

### Eager mode (CLI-driven, useful for scripting)

```
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267
```

Same TAI default as the home form (pass `--t-zero-utc` to opt out).
Fetches + parses on the CLI side first, then opens the browser
straight at the explore view for that exposure.

By default this fetches **5 s before to 5 min after** the shutter close.
A rapid analysis exposure usually finishes within ~90 s; the longer
default window gives you context on the next exposure's dispatch too.

While the fetch runs you'll see progress per pod on stderr; this takes
~60–90 s the first time for a busy LSSTCam window (~430 pods, ~40 MiB).
A second run for the same (or any overlapping) window is instant — see
[Cache](#cache) below.

`--no-browser` skips the browser auto-open; `--port` changes the bind
port. Ctrl-C in the terminal stops the server.

## What the UI shows

The browser app has two views:

- **Home view** — the landing page when no exposure is loaded.
  Hosts the fetch form, the credentials panel, the cached-runs table,
  and the live progress bar for an in-flight fetch.
- **Explore view** — the timeline + detail drawer for one loaded
  exposure. Click the **← home** button in its topbar to return to
  the home view (the loaded state stays in memory; the back arrow is
  a navigation, not a reset).

Inside the explore view:

- **Top bar** — the dataId, the UTC t-zero, where the cache lives, and
  how big the on-disk cache currently is.
- **t₀ selector** — pick between the two reference points the tool
  derives: shutter close (caller-supplied) and the head-node's first
  `Defining visit` for this exposure. Whichever you pick becomes the
  zero of the timeline x-axis and of every `Δt₀` label.
- **Search box / "hide pods with no events"** — narrow the rendered
  pods.
- **"times as Δshutter" toggle** — when on, the inline timestamps
  inside each raw log line (the leading `2026-05-20 08:45:46,216`
  prefix) are replaced with their offset from shutter close — e.g.
  `+6.949s` — both in the detail drawer and in event hover tooltips.
  The Δ is **always relative to shutter close**, independent of the
  t₀ selector above, so it stays consistent as a "real seconds after
  the camera closed" reading.
- **"collapse all" / "expand all"** — toggle every pod group at once.
- **Events legend** — head-node / worker event icons.
- **Tasks legend** — one colour swatch per pipeline task seen (`isr`,
  `calibrateImage`, `calcZernikesTask`, …). Quantum bars on the
  timeline are coloured to match.
- **Timeline** — one row per pod, grouped by role (head, sfm, aos,
  step1b, mosaic, plotters, …). Each event renders as a tick or a bar
  with a hover tooltip showing the raw log line.
  - **Group headers fold.** Click a header (e.g. `sfm  (189 pods)`)
    to collapse or expand that group; the arrow flips between ▼ and ▶.
    Groups with more than ten pods (typically `sfm` and sometimes
    `aos`) start collapsed so the page opens with a digestible view.
    Single-pod groups (`head`, `step1b`, …) render as a flat, non-
    clickable header — there's nothing to hide. "collapse all" /
    "expand all" only affects the multi-pod groups too.
    If any pod in a collapsed group has a traceback, the header
    surfaces a red `TB N` pill so you don't miss it.
  - **Pods with tracebacks** in the window get a red left border and
    a red `TB N` pill in their row name. The cheapest visual scan
    for "where are things going wrong" is to look down the left
    edge of the timeline for red bars.
- **Detail drawer** — click any pod's row to slide up its full parsed
  log, with Δt₀, level, and a filter / "warn-only" / "only lines
  containing this dataId" toggle. Tracebacks render as a contiguous
  block. With the **Δshutter toggle** on, the inline ISO timestamps
  in this drawer are replaced with shutter-relative offsets too.

## Common workflows

### Investigate one specific exposure

```
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267
```

Open the browser. Look at the head-node row first — does the
fanout look healthy? Scan the SFM rows for anomalously long ISR
bars (orange) or calibrateImage bars (teal). Click any pod whose
row has a red WARN/ERROR tick to dig in.

### Widen the window to see neighbouring exposures

```
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267 \
    --window-before 60 --window-after 600
```

The `Δt₀ = 0` reference stays on the same exposure; you just see more
of the surrounding cluster activity. If a cached run already covers
the wider window the subset is reused immediately — no re-fetch.

### Inspect what's cached

```
python3 -m ra_log_explorer.cli cache info
```

Lists every cached window, its on-disk size, and whether it completed
cleanly.

### Flush the cache

```
python3 -m ra_log_explorer.cli cache flush
```

Or just `rm -rf ~/.cache/ra_log_explorer`. The cache is purely a
performance accelerator; flushing it costs you nothing but a re-fetch.

## All CLI options

```
--exposure-id ID         13-digit dataId (optional; pair with --t-zero)
--t-zero ISO             shutter-close timestamp (optional; pair with --exposure-id)
--t-zero-utc             treat --t-zero as already-UTC instead of TAI
--window-before SECONDS  pre-shutter pad (default 5)
--window-after  SECONDS  post-shutter pad (default 300)
--workers N              parallel log fetch threads (default 8)
--cluster NAME           Loki cluster label (default yagan)
--namespace NAME         Loki namespace label (default rapid-analysis)
--loki-addr URL          Loki API base URL
--username USER          Loki HTTP basic-auth user (default merlin)
--force-refresh          ignore the cache and re-fetch
--host HOST              bind address (default 127.0.0.1)
--port PORT              HTTP port (default 8765)
--no-serve               with --exposure-id: fetch + parse only, no UI
--no-browser             launch the UI but don't auto-open a browser tab
```

Omit `--exposure-id` / `--t-zero` for **home mode** (server starts at the
landing page; pick your exposure in the browser).

Run `python3 -m ra_log_explorer.cli --help` for the same list.

## Cache

The tool caches per-pod Loki output under `~/.cache/ra_log_explorer/`
(override with `$RA_LOG_EXPLORER_CACHE`). A second run with the same
window — or any narrower window inside an existing cached one — is
instant.

The cache layout, reuse rules, and `.partial` flag are described in
[architecture/caching.md](architecture/caching.md).

## Troubleshooting

**"`logcli` binary not found on PATH"** — install it
(`brew install grafana/grafana/logcli`) and reopen your shell. The tool
will not auto-install or fall back to a different client.

**"LOKI_PASSWORD is not set in the environment"** — export it. Don't
pass passwords on the command line.

**A few pods show 0 events but the exposure obviously touched them** —
look at the pod's `.jsonl` directly under the cache directory. If the
file is empty, that's Loki returning nothing for the window — usually a
transient series-index gap. Re-run with `--force-refresh` to pull again.

**Cache hit when you didn't expect one** — the tool reuses any
*superset* of the requested window. If you specifically want to refetch,
pass `--force-refresh`.

**Timeline shows events from the previous exposure** — the cached
window is wider than the one you asked for (because of superset reuse).
The events are still correctly placed in time; if it bothers you, run
with `--force-refresh` to write a tighter cache directory and reload.

**Browser opens but the page is blank / JS error** — check the terminal
for a Python traceback from the server. Re-run with `--port` set to a
free port if 8765 is in use.

## For developers / contributors

The contributor guide and coding conventions live in
[CLAUDE.md](CLAUDE.md). Design and implementation docs are under
[architecture/](architecture/). The validation loop (pre-commit, mypy,
mypy-coverage, pytest) is summarised in the
[validation skill](.claude/skills/ra-log-explorer-validation/SKILL.md).

If your change is user-visible — a new CLI flag, a new browser feature,
a change in the cache flush command, a new troubleshooting failure mode
— update this README in the same commit (the
[architecture-sync skill](.claude/skills/ra-log-explorer-architecture-sync/SKILL.md)
calls this out).
