# Rapid Analysis Log Explorer

Interactive log exploration for the **rapid analysis** backend
(Vera C. Rubin Observatory). Type a dataId; the tool resolves its
shutter close time, pulls every pod's logs from the cluster's Loki for a
window around that moment, and serves a browser timeline showing what
each pod did, when, and where the warnings / errors / tracebacks landed.

![timeline placeholder]()

Designed for the typical question:

> "Why was this exposure slow / why did this detector fail / where was the
> hold-up between step1a finishing and step1b dispatching?"

It is **not** a streaming log tail; it works on snapshots of a fixed
window. One exposure at a time.

## Required environment variables

The tool needs **three** things from your environment to fetch logs and
resolve dataIds. Put these in your shell rc (`~/.zshrc`, `~/.bashrc`,
…) so they survive across terminals:

```sh
# 1. Loki credentials — used by `logcli` to authenticate.
export LOKI_PASSWORD='<the password>'

# 2. Exposure-timing service base URL — used by the dataId -> shutter-
#    close TAI lookup. Ask Merlin for the current URL.
export RA_LOG_EXPLORER_EXPOSURE_TIMINGS_URL='https://...'
```

| Env var                                   | Required for                                  | What if it's missing                                 |
|--------------------------------------------|------------------------------------------------|------------------------------------------------------|
| `LOKI_PASSWORD`                            | Every Loki fetch                              | `logcli` refuses to run; fetches fail at submit time. |
| `RA_LOG_EXPLORER_EXPOSURE_TIMINGS_URL`     | dataId → shutter-close auto-resolution        | Home form shows "lookup service not configured"; you can't submit a fetch from the UI until it's set. (The CLI's `--t-zero` flag still works as a manual override.) |
| `RA_LOG_EXPLORER_CACHE`                    | *Optional* — overrides the cache root         | Defaults to `~/.cache/ra_log_explorer/`.             |

The username for `logcli` defaults to `merlin` and is overridable in the
browser's Credentials card (or via the CLI's `--username` flag).

## Other prerequisites

| Requirement                       | Notes                                                                                |
|-----------------------------------|---------------------------------------------------------------------------------------|
| Python ≥ 3.11                     | 3.13 recommended; the runtime itself is stdlib-only.                                  |
| `logcli` on your `$PATH`          | `brew install grafana/grafana/logcli` on macOS, or grab a binary from Grafana releases. |
| `git`                              | There is no PyPI package; you run from a checkout.                                  |
| A browser                         | The tool opens `http://127.0.0.1:8765/` for you.                                      |

## Getting started — first run, step by step

```sh
# 1. Install logcli (macOS; for Linux see the Grafana releases page).
brew install grafana/grafana/logcli
logcli --version    # sanity-check

# 2. Set the env vars (in this shell + ideally in ~/.zshrc).
export LOKI_PASSWORD='ask-merlin'
export RA_LOG_EXPLORER_EXPOSURE_TIMINGS_URL='ask-merlin'

# 3. Clone the repo and cd into it.
git clone git@github.com:lsst-so/ra_log_explorer.git
cd ra_log_explorer

# 4. Start the server. The runtime needs no pip installs (stdlib only).
python3 -m ra_log_explorer.cli
```

That last command starts a local HTTP server and tries to open
`http://127.0.0.1:8765/` in your default browser. If the browser doesn't
open by itself, paste the URL by hand. To stop the server, press
**Ctrl-C** in the terminal where you launched it.

In the browser:

1. The home page loads. Fill in your **Loki username + password** in
   the *Credentials* card if you haven't already — tick "remember in
   this browser" if you want them kept in `localStorage`. The password
   field is optional; if you leave it blank the server falls back to
   `LOKI_PASSWORD` from its environment.
2. Type a **dataId** (e.g. `2026051900722`). After ~300 ms the tool
   resolves the shutter close time and shows it inline under the input.
3. Optionally open *Advanced options* to tweak cluster, namespace,
   worker count, or the pre-/post-shutter window padding.
4. Click **Fetch & explore**. A progress bar follows the fetch live
   (Server-Sent Events). When it's done the page switches to the
   timeline view.

The home page also lists every cached window on disk. Clicking a row
copies its cluster / namespace / window settings into the fetch form so
a subsequent submit cache-hits exactly. The ✕ button on each row
deletes that one window; the "delete all" button under the table wipes
the whole cache.

## Eager mode (CLI-driven, useful for scripting)

```
python3 -m ra_log_explorer.cli \
    --exposure-id 2026051900722 \
    --t-zero 2026-05-20T08:46:16.267
```

Skips the home page — fetches + parses on the CLI side first, then opens
the browser straight at the explore view. Useful when scripting or when
you already have a shutter-close timestamp in hand.

`--t-zero` is treated as **TAI** by default (matching the Butler
`DimensionRecord.timespan.end.isot` convention). Pass `--t-zero-utc` if
you've already done the conversion.

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
  with a hover tooltip showing the raw log line. Pods are listed in
  flow order: each gather sits directly beneath the per-detector
  workers it consumes (`step1b` under `sfm`, `step1b-aos` under
  `aos`).
  - **Group headers fold.** Click a header (e.g. `sfm  (189 pods)`)
    to collapse or expand that group; the arrow flips between ▼ and ▶.
    Groups with more than ten pods (typically `sfm` and sometimes
    `aos`) start collapsed so the page opens with a digestible view.
    Single-pod groups (`head`, `step1b`, …) render as a flat, non-
    clickable header — there's nothing to hide. "collapse all" /
    "expand all" only affects the multi-pod groups too.
    If any pod in a collapsed group has a traceback, the header
    surfaces a red `TB N` pill so you don't miss it. When a large
    group is collapsed, any pod in it that produced a traceback
    stays visible, with a "+ N more pods" footer for the rest. You
    don't have to expand the group manually just to see the red
    rows.
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

Easiest: open the home page in the browser, hit the **delete all**
button under the recent-runs table (or the **✕** on a single row to
remove just one window).

From the CLI:

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
