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

## Where to run it

**Use the deployed instance.** The tool runs as the Phalanx application
`log-explorer` on the two clusters whose pipelines it explains, behind
the usual RSP login:

| Environment | URL |
|-------------|-----|
| Base Test Stand (`manke`) | `https://base-lsp.lsst.codes/log-explorer` |
| Summit (`yagan`) | `https://summit-lsp.lsst.codes/log-explorer` |

Each instance queries its own cluster and its own ConsDB, so pick the one
matching the data you care about — the same 13-digit dataId means
different exposures at the two sites. There is nothing to install, no
credentials to obtain, and no configuration to fill in: log in and type a
dataId. The on-disk cache is shared, so a window a colleague already
fetched opens instantly for you.

Everything below — installing `logcli`, obtaining tokens, exporting
`LOKI_PASSWORD` — is for **running it on your own machine while
developing this repo**. That mode binds `127.0.0.1`, has no
authentication, and needs your own credentials; it is a development
convenience, not how the tool is meant to be used.

## Quickstart (local development)

```sh
# Prerequisites: Python ≥ 3.13, git, and logcli on $PATH.
# (`brew install grafana/grafana/logcli` on macOS.)

# Loki password — ask Merlin for the value.
export LOKI_PASSWORD='...'

# RSP bearer token for the ConsDB exposure lookup (shutter close +
# the image properties shown in the explore-view info box). Get one at
# https://usdf-rsp.slac.stanford.edu/auth/tokens/ and drop it in:
mkdir -p ~/.lsst && cat > ~/.lsst/log-browser-token.txt   # paste, then Ctrl-D

git clone git@github.com:lsst-so/ra_log_explorer.git
cd ra_log_explorer
python3 -m ra_log_explorer.cli
```

That starts a local server and opens `http://127.0.0.1:8780/` in your
browser. Type a 13-digit dataId, click **Fetch & explore**, wait
~60–90 s the first time (instant on a repeat). Stop with **Ctrl-C**.

For everything else — what's required, where the cache lives, how to
drive it from the CLI, troubleshooting — read on.

## Required environment + credentials

*(Local development only — the deployed instances get all of this from
their environment and their cluster.)*

The tool needs **one env var** and **one ConsDB token file per site
you want to talk to** (see [Sites](#sites) below — today there are
two: the summit and the Base Test Stand). Put the env var in
`~/.zshenv` (or your bash equivalent) so it survives across both
interactive shells and non-interactive ones:

```sh
# 1. Loki credentials — used by `logcli` to authenticate against the
#    Loki cluster behind every site. Same value works for all sites.
export LOKI_PASSWORD='<the password>'
```

```sh
# 2a. Summit RSP bearer token — used to query the ConsDB shutter-close
#     timestamp when the active site is "summit" (cluster yagan). Get
#     one at https://usdf-rsp.slac.stanford.edu/auth/tokens/.
mkdir -p ~/.lsst
echo '<paste-summit-token>' > ~/.lsst/log-browser-token.txt
chmod 600 ~/.lsst/log-browser-token.txt

# 2b. BTS RSP bearer token — used when the active site is "bts"
#     (cluster manke). Get one from base-lsp.lsst.codes.
echo '<paste-bts-token>'    > ~/.lsst/manke-token.txt
chmod 600 ~/.lsst/manke-token.txt
```

Once a dataId has been resolved on this machine it is cached on disk
per-site (under `~/.cache/ra_log_explorer/exposure-times/<site>.json`),
so subsequent lookups for the same id work without the token. Exposure
end-times are immutable once recorded, so the cache never goes stale.
Sites have separate cache files so a colliding bare dataId between
scopes (BTS simulated vs. summit real) can't return the wrong obs_end.

| Setting                                   | Required for                                  | What if it's missing                                 |
|--------------------------------------------|------------------------------------------------|------------------------------------------------------|
| `LOKI_PASSWORD` (env var)                  | Every Loki fetch                              | `logcli` refuses to run; fetches fail at submit time. |
| `~/.lsst/log-browser-token.txt` (file)     | dataId → shutter-close auto-resolution for the **summit** site | The home form shows "ConsDB token file for site 'summit' not found"; cached dataIds still resolve. The CLI's `--t-zero` flag is also a manual override. |
| `~/.lsst/manke-token.txt` (file)           | dataId → shutter-close auto-resolution for the **bts** site    | Same shape of error, scoped to the BTS site. |
| `RA_LOG_EXPLORER_SITES_FILE` (env var)     | *Optional* — point at a custom site catalog   | Defaults to the checked-in [`ra_log_explorer/sites.toml`](ra_log_explorer/sites.toml). |
| `RA_LOG_EXPLORER_CACHE` (env var)          | *Optional* — overrides the cache root         | Defaults to `~/.cache/ra_log_explorer/`.             |

The username for `logcli` defaults to `merlin`; set `$LOKI_USERNAME` (or
pass `--username`) to authenticate as something else. Neither the
username nor the password can be set from the browser — see
[Configuration](#configuration).

## Sites

A *site* pairs a Loki cluster (where the logs live) with the ConsDB
endpoint that owns its shutter-close truth. The site catalog is
checked in at [`ra_log_explorer/sites.toml`](ra_log_explorer/sites.toml)
and currently has two entries:

| Site name | Cluster (Loki) | ConsDB endpoint                                    | Token file                       |
|-----------|----------------|----------------------------------------------------|----------------------------------|
| `summit`  | `yagan`        | `https://usdf-rsp.slac.stanford.edu/consdb/query` | `~/.lsst/log-browser-token.txt`  |
| `bts`     | `manke`        | `https://base-lsp.lsst.codes/consdb/query`        | `~/.lsst/manke-token.txt`        |

The summit site sees the real Vera C. Rubin camera; BTS is the Base
Test Stand replica that runs simulated data through the same pipeline.
The two ConsDBs are independent databases, so the same 13-digit dataId
can refer to a real exposure on the summit and a simulated one on BTS
with completely different `obs_end` values — the catalog keeps them
from crosstalking.

**One running server serves exactly one site.** Pick it with
`--site=summit|bts` (default: the catalog's `default_site`, currently
`summit`); it is fixed for the life of the process and shown as a badge
in the browser's top bar. There is no switcher: a deployment on manke
*is* BTS and one on yagan *is* the summit, and since the same 13-digit
dataId exists at both with different `obs_end` values, a server that
could be talked into answering for the other one would produce results
that looked plausible rather than obviously wrong. To look at the other
site locally, start a second server with `--site` set to it.

`consdbTokenFile` is optional. Omit it (or leave it blank) when the
ConsDB endpoint takes no bearer token — which is the case for a
cluster-internal Service address, reached without passing through
Gafaelfawr. That's how the deployed instances are configured; a
laptop talking to a public RSP endpoint still needs a token.

Point `$RA_LOG_EXPLORER_SITES_FILE` at a different TOML file to
override the catalog without touching the package.

(USDF will get its own site once we plumb that path; it'll share the
summit ConsDB.)

## Other prerequisites

| Requirement                       | Notes                                                                                |
|-----------------------------------|---------------------------------------------------------------------------------------|
| Python ≥ 3.13                     | What `pyproject.toml` requires; the runtime itself is stdlib-only.                     |
| `logcli` on your `$PATH`          | `brew install grafana/grafana/logcli` on macOS, or grab a binary from Grafana releases. |
| `git`                              | There is no PyPI package; you run from a checkout.                                  |
| A browser                         | The tool opens `http://127.0.0.1:8780/` for you.                                      |

## Getting started locally — first run, step by step

```sh
# 1. Install logcli (macOS; for Linux see the Grafana releases page).
brew install grafana/grafana/logcli
logcli --version    # sanity-check

# 2. Set the Loki password env var (in this shell + ideally in ~/.zshrc).
export LOKI_PASSWORD='ask-merlin'

# 3. Drop your RSP bearer token in ~/.lsst/log-browser-token.txt.
mkdir -p ~/.lsst
echo '<paste-rsp-token>' > ~/.lsst/log-browser-token.txt
chmod 600 ~/.lsst/log-browser-token.txt

# 4. Clone the repo and cd into it.
git clone git@github.com:lsst-so/ra_log_explorer.git
cd ra_log_explorer

# 5. Start the server. The runtime needs no pip installs (stdlib only).
python3 -m ra_log_explorer.cli
```

That last command starts a local HTTP server and tries to open
`http://127.0.0.1:8780/` in your default browser. If the browser doesn't
open by itself, paste the URL by hand. To stop the server, press
**Ctrl-C** in the terminal where you launched it.

In the browser:

1. The home page loads with a **site badge** in the top bar naming the
   cluster this server queries — `summit`/yagan for real-camera data,
   `bts`/manke for simulated. It's a label, not a control: the site,
   your Loki credentials, the fetch width and the cache size all come
   from the server's environment, so there is nothing to fill in before
   you start. See [Configuration](#configuration) for how to change them.
2. Type a **dataId** (e.g. `2026051900722`) in the *Explore exposure
   processing* card. After ~300 ms the tool resolves the shutter
   close time and shows it inline under the input.
   - **ConsDB down or missing the entry?** If the lookup can't resolve
     the dataId (ConsDB unreachable, no token, or no row for that id), a
     **manual shutter close** field appears under the input. Type the
     timestamp yourself in TAI ISO-8601 — e.g. `2026-06-24T14:38:41.380663`,
     the same convention as ConsDB's `obs_end` and the CLI's `--t-zero` —
     and the fetch proceeds with that value. It's remembered for this
     dataId (cached under the active site) so reopening or refreshing the
     view doesn't ask again; a real ConsDB value, once reachable, takes
     precedence. Manual entry is single-dataId only — not range or night.
3. Optional *per-exposure tuning* (window before / after t₀) lives
   in a small details fold on the exposure form. These are the one
   thing you can vary per fetch — widening the window to catch a
   neighbouring exposure is a normal move. They start at whatever the
   server is configured with. Cluster, namespace, Loki URL and ConsDB
   endpoint all come from the site the server serves.
4. Click **Fetch & explore**. A progress bar follows the fetch live
   (Server-Sent Events). When it's done the URL updates to
   `/?dataId=<id>` and the page switches to the timeline view.

Caching is invisible in day-to-day use, but the **admin** link in the
topbar (or `/?admin=1` directly) opens a *Cached windows* table listing
every window on disk. Each row carries a **key** column showing the
exposure(s) that triggered fetches landing on that cache, each with the
instrument it was fetched under — the `dayObs` for night caches, the
seq-number span for range caches — as clickable links that open the
cached view in a new tab. (The instrument is part of the link because a
dataId alone doesn't name an exposure: LSSTCam and LATISS both number
from 1 each night, so the same 13 digits are two different images.) The ✕ button on a row
deletes that one window; *flush entire cache* above the table wipes the
lot. Nothing here needs tending: the server LRU-evicts the
least-recently-viewed windows on its own once the on-disk total exceeds
`RA_LOG_EXPLORER_MAX_CACHE_BYTES` (5 GiB by default, and not something
the browser can set — see [Configuration](#configuration)).

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

`--instrument` (default `lsstcam`) names the instrument the dataId
belongs to. A dataId is only unique within one instrument — LSSTCam and
LATISS both count from 1 each night — so the flag decides which
instrument's pods the timeline attributes work to, exactly like the
topbar switch in the browser.

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

The browser app has three views:

- **Home view** — the landing page when nothing is loaded. The topbar
  carries the **instrument switch** (LSSTCam / LATISS, LSSTCam default)
  that the whole page follows — exposure ids restart at 1 per
  instrument each night, so every list, lookup, and fetch is pinned to
  exactly one instrument at a time. Two side-by-side mode cards on top:
  **All RA processing** (one exposure,
  with a single *advanced options* disclosure holding the shared window
  pads and the range-of-exposures form) and **Night processing**
  (investigate a whole night, AOS pods or everything else), each with
  its own inline progress bar.
  Below them, on a deployment running live mode, sits the **Tonight**
  panel: every exposure taken so far tonight, newest first, with a
  ready/wait status chip, refreshing itself every 30 s while the page
  is open — see *Live mode (deployments)* below.
- **Admin view** — `/?admin=1`, linked from the home topbar (no
  separate permissions; anyone can open it). Hosts the cached-windows
  table — every log window on disk, per-row delete — and a *flush
  entire cache* button. Caching is invisible to day-to-day use; this
  page is for operators.
- **Explore view** — the timeline + detail drawer for one loaded
  exposure. Click the **← home** button in its topbar to return to
  the home view (the loaded state stays in memory; the back arrow is
  a navigation, not a reset). In **range mode** the explore view gains
  a navigator strip on top: one chip per exposure in the range (red if
  it raised a traceback), plus ◀/▶ buttons and the ←/→ arrow keys to
  step between them. Each exposure's timeline is anchored at its own
  shutter close, so the `Δt₀ = 0` line always sits on the exposure
  you're looking at.
- **Night view** — the dayObs-wide failure breakdown and Δshutter
  histograms, plus data-completeness banners: an incomplete-fetch
  warning, and a "gather-only" warning listing any dataIds whose
  step1b (gather) ran with no step1a — physically impossible, so a tell
  that step1a logs were dropped (and a cause of a biased first-task
  histogram). It also rolls up **pod restarts & deaths** for the night:
  a `pod restarts` stat tile and a table of every restart / kill /
  OOM / failure (from the `k8s/events` stream), each attributed to the
  dataId the pod was processing at that moment — click it to open that
  visit. A `restart` is an in-place container restart, usually an OOM,
  so it's the fastest way to spot "which exposures had a worker die on
  them tonight".

  The night comes in **two tabs**, which between them cover every pod:

    - **AOS view** — the AOS pods (anything with `aos` in its name).
      This is the default, and it is the quick one: the pod filter is
      applied at the Loki end, so it fetches a few tens of pods.
    - **SFM + misc view** — everything else: the SFM workers, head node,
      plotters, one-offs, redis. Much bigger — a summit night here is
      ~9 GiB against the AOS half's few hundred MB, so expect minutes
      rather than seconds (on a deployment running live mode it is
      usually instant, because the night is already on disk).

  Each tab is its own fetch and its own link, so you can have both open
  side by side. Pick the half you want on the home page's night card
  before fetching; the tab strip switches between them afterwards.

Inside the explore view:

- **Top bar** — the dataId, the UTC t-zero, where the cache lives, and
  how big the on-disk cache currently is.
- **Exposure info box** — a strip of ConsDB exposure properties for the
  loaded dataId: image type, observation reason, science program,
  filter, exposure time, target, and (for multi-exposure groups) the
  "N of M" index — enough to tell *what kind of image* you're looking
  at, e.g. spotting that a dataId is one half of a CWFS donut pair.
  Hidden if ConsDB never resolved the dataId (no token / unknown id).
  The same properties show as a hover tooltip on every dataId link —
  the night view's histogram-bin and failure-table ids, and the range
  navigator chips — so you can triage without opening each one.
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
- **Pod lifecycle legend** — the full-height markers for pod
  death/restart pulled from Kubernetes events (see below):
  `restart`, `killed`, `OOM-killed`, `unhealthy`.
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
  - **Pod death/restart markers.** Alongside each pod's app-log events
    the timeline draws tall, full-height ticks for Kubernetes pod
    lifecycle events — pulled from a second Loki stream (`k8s/events`)
    fetched next to the logs. An orange `restart` tick is the important
    one: it means the container died and was restarted **in place**, so
    if a pod's app log just stops mid-exposure with no traceback, the
    restart tick a few seconds later is usually *why*. Red `killed` /
    `OOM-killed` / `failed` and amber `unhealthy` ticks cover the
    explicit cases. Hover any tick for the k8s reason and message.
    (Caveat: a container that exceeds its own memory limit is killed by
    the kernel with **no** Kubernetes event, and the kernel's OOM line
    isn't shipped to Loki — so that common case shows up only as the
    `restart` tick, not a definitive `OOM-killed`. To confirm OOM, check
    `kubectl ... lastState.terminated` (`reason: OOMKilled`, exit 137).)
- **Detail drawer** — click any pod's row to slide up its full parsed
  log, with Δt₀, level, and a filter / "warn-only" / "only lines
  relevant to this dataId" toggle. The relevance check is smarter than
  a substring search: for worker pods (sfm, aos, step1b, etc.), once a
  dataId has been logged every subsequent line is considered to belong
  to it until a new dataId arrives — matching how those pods actually
  process one exposure at a time. The dataId itself is recognised in
  both the bare 13-digit form (`2026051900722`) and the split form
  (`day_obs=20260519` + `seq_num=722` on the same line, in any of the
  camelCase / snake_case / squashed spellings). Control-plane pods
  (head node, butler-watcher, metadata servers, etc.) interleave many
  dataIds so the toggle there falls back to "lines that explicitly
  mention this id". Tracebacks render as a contiguous block regardless
  of the toggle. With the **Δshutter toggle** on, the inline ISO
  timestamps in this drawer are replaced with shutter-relative
  offsets too.

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

### Explore a contiguous range of exposures

In the home page, use the **Explore a range of exposures** card: type a
start and a stop dataId. The browser resolves both shutter-close times,
then a **single** Loki fetch covers the whole span
(`shutterClose(start) − before → shutterClose(stop) + after`) as one
cache block — far cheaper than fetching each exposure's overlapping
window on its own. The server resolves every in-range dataId's shutter
close from ConsDB (skipped integers in the range are expected and just
omitted), and you land in the explore view with a navigator strip on
top. Step through each exposure with the chips, the ◀/▶ buttons, or the
←/→ arrow keys; every exposure is anchored at its own shutter close.
This mode is browser-only (there's no CLI flag) and is meant for tens
of consecutive exposures.

### Inspect what's cached

```
python3 -m ra_log_explorer.cli cache info
```

Lists every cached window, its on-disk size, and whether it completed
cleanly.

### Flush the cache

Easiest: open the admin view (`/?admin=1`) and hit **flush entire
cache** above the table (or the **✕** on a single row to remove just
one window).

From the CLI:

```
python3 -m ra_log_explorer.cli cache flush         # prompts first
python3 -m ra_log_explorer.cli cache flush --yes   # don't
```

Or just `rm -rf ~/.cache/ra_log_explorer`. The cache is purely a
performance accelerator; flushing it costs you nothing but a re-fetch.

You rarely need to do this by hand: when an upgrade changes *how* logs
are fetched, the tool bumps an internal cache-schema version and flushes
the whole cache automatically on the next start (printing a one-line
notice), so a stale snapshot from an older version is never re-served.
The first loads after such an upgrade re-fetch and so are slower.

### Keep a night on the laptop to work offline

Fetching needs the VPN and takes minutes for a whole night. If you are
developing (or want to demo on a plane), capture a night once and serve
it from disk afterwards:

```
# On the VPN, once. BTS nights are ~150 MB; summit nights are ~9 GB.
python3 tools/captureNight.py --site bts --day-obs 20260811 --out ~/log-nights

# Any time after that, no network needed.
python3 tools/stageNight.py --master ~/log-nights/aug11-night-bts --cache ~/log-cache
RA_LOG_EXPLORER_CACHE=~/log-cache python3 -m ra_log_explorer.cli run --site bts
```

Remember `--site` on the server too: it defaults to the site catalog's
`default_site` (the summit), and a BTS night served under the summit
site looks like an empty night rather than an error.

See [architecture/testing.md](architecture/testing.md#capturing-a-night-to-work-against)
for what a captured night contains and how to replay one as "tonight"
for live-mode work.

## All CLI options

There are two subcommands, `run` and `cache`:

```
run                      start the server (the default; the flags below can
                         also be given with no subcommand at all)
cache info               summarize what's on disk
cache flush [--yes]      delete the whole cache; --yes skips the prompt
```

The `run` flags:

```
--exposure-id ID         13-digit dataId (optional; pair with --t-zero)
--t-zero ISO             shutter-close timestamp (optional; pair with --exposure-id)
--t-zero-utc             treat --t-zero as already-UTC instead of TAI
--instrument NAME        lsstcam | latiss (default lsstcam); which instrument
                         --exposure-id belongs to
--window-before SECONDS  pre-shutter pad (default 5)
--window-after  SECONDS  post-shutter pad (default 300)
--workers N              parallel log fetch threads (default 8)
--site NAME              site from sites.toml (default: catalog default_site)
--username USER          Loki HTTP basic-auth user (default $LOKI_USERNAME, else merlin)
--force-refresh          ignore the cache and re-fetch
--host HOST              bind address (default 127.0.0.1)
--port PORT              HTTP port (default 8780)
--base-path PREFIX       serve under a URL prefix, e.g. /log-explorer
                         (default $RA_LOG_EXPLORER_BASE_PATH, else the root)
--no-serve               with --exposure-id: fetch + parse only, no UI
--no-browser             launch the UI but don't auto-open a browser tab
--live-poll-s SECONDS    live mode: continuously fetch the current night so
                         exposure views load instantly; 0 = off
                         (default $RA_LOG_EXPLORER_LIVE_POLL_S, else 0)
--live-day-obs N         testing: pin live mode to dayObs N so a staged
                         historical night plays the role of "tonight"
```

`--base-path` exists for deployments that share a hostname with other
apps; a local run never needs it. With it set, every URL the app serves
and every request the browser makes back carries the prefix, and paths
outside it are 404ed rather than answered.

Omit `--exposure-id` / `--t-zero` for **home mode** (server starts at the
landing page; pick your exposure in the browser).

Run `python3 -m ra_log_explorer.cli --help` (and `... run --help` /
`... cache --help`) for the authoritative list.

## Configuration

Everything that varies between deployments is an environment variable,
read once when the server starts. **Nothing is configurable from the
browser.** The UI asks questions about exposures; it does not
reconfigure the service that answers them — a shared deployment has many
users and one process, so a settings field would let whoever touched it
last change how everyone else's fetches behave.

| Variable | What it sets | Default |
|----------|--------------|---------|
| `LOKI_PASSWORD` | Loki basic-auth password, used by `logcli` | *(required)* |
| `LOKI_USERNAME` | Loki basic-auth user | `merlin` |
| `RA_LOG_EXPLORER_CACHE` | cache root | `~/.cache/ra_log_explorer` |
| `RA_LOG_EXPLORER_MAX_CACHE_BYTES` | LRU eviction ceiling | 5 GiB |
| `RA_LOG_EXPLORER_WORKERS` | parallel fetch workers | `8` |
| `RA_LOG_EXPLORER_WINDOW_BEFORE_S` | starting value of the window-before field | `5` |
| `RA_LOG_EXPLORER_WINDOW_AFTER_S` | starting value of the window-after field | `300` |
| `RA_LOG_EXPLORER_SITES_FILE` | which site catalog to load | the packaged `sites.toml` |
| `RA_LOG_EXPLORER_BASE_PATH` | URL prefix to serve under | `""` (the root) |
| `RA_LOG_EXPLORER_LIVE_POLL_S` | live-mode poll interval; `0` disables | `0` (off) |
| `RA_LOG_EXPLORER_LIVE_LAG_S` | how far behind *now* live fetches stop | `60` |

Most have a matching CLI flag (`--workers`, `--window-after`, `--site`,
`--base-path`, …) that wins over the environment for a one-off run.

A malformed numeric value stops the server with a clear error rather
than falling back to the default — a setting that quietly never took
effect is much harder to notice than one that refuses to start. **An
empty value counts as malformed**; only leaving the variable out asks
for the default. (Deployed, blank is what a mistyped Helm reference
renders to, which is exactly the case worth failing on.)

The two window values are the *starting* values of editable form fields,
not fixed limits: widening a window to catch a neighbouring exposure is
a normal investigative move, so the deployment picks where the fields
start and you can still change them per fetch. Everything else in the
table the browser cannot influence at all.

## Cache

The tool caches per-pod Loki output under `~/.cache/ra_log_explorer/`
(override with `$RA_LOG_EXPLORER_CACHE`). A second run with the same
window — or any narrower window inside an existing cached one — is
instant.

The cache layout, reuse rules, and `.partial` flag are described in
[architecture/caching.md](architecture/caching.md).

## Live mode (deployments)

The deployed instances run with `RA_LOG_EXPLORER_LIVE_POLL_S` set, which
turns on **live mode**: the server continuously fetches the current
night's logs (all pods, every few minutes) into its cache, and the home
page grows a **Tonight** panel listing every exposure taken so far —
newest first, with what kind of image it is and whether its logs are
ready. An exposure becomes *ready* when the full default window
(shutter close + 5 minutes) is on disk; clicking it opens the ordinary
explore view, served by slicing the already-fetched night rather than
by a fresh Loki query, so it loads in seconds even mid-night.

The **instrument switch** in the topbar decides whose exposures you are
looking at. A dataId's last five digits are a sequence number that
restarts at 1 each night *per instrument*, so on a night where LSSTCam
and LATISS both observe, `2026071100001` is a different image on each —
with a different shutter close — and mixing them would be actively
wrong. The whole page follows the switch: the Tonight list shows only
the selected instrument's exposures, and every dataId lookup and fetch
resolves against it. LSSTCam is the default; the AOS card only appears
for LSSTCam (AOS runs nowhere else).

The panel header shows how far the night has been fetched ("logs
fetched through …"). A freshly-restarted server shows *catching up*
while it backfills from noon UTC; a red banner appears if any pod's
fetch is failing, ConsDB can't be reached, or an earlier night couldn't
be tidied up.

The night view benefits too: opening the *current* night's dayObs no
longer re-fetches hours of AOS logs from Loki — it slices "the night so
far" out of the live cache (the parse still takes a little while for a
busy night, but the minutes-long fetch is gone).

Locally, live mode is off by default (an ordinary laptop run shouldn't
be quietly pulling gigabytes per night). `--live-poll-s 180` turns it
on for development against a real cluster.

## Troubleshooting

**"`logcli` binary not found on PATH"** — install it
(`brew install grafana/grafana/logcli`) and reopen your shell. The tool
will not auto-install or fall back to a different client.

**"LOKI_PASSWORD is not set in the environment"** — export it from
`~/.zshenv` (or your bash equivalent). Don't pass passwords on the
command line, and don't drop the export in `~/.zshrc` — that's only
sourced for interactive shells, so non-interactive child processes
won't see it. An *empty* export raises the same error rather than
letting `logcli` fail with a bare 401.

**"ConsDB token file for site '<name>' not found at <path>"** — this
server's site needs a token that isn't on this machine. Either drop the
right token at the named path (see [Sites](#sites) for the matrix), or
restart with `--site` set to one you do have a token for. Cached dataIds
still resolve without a token. (For a one-off where you
can't fix the token but know the shutter close, use the **manual
shutter close** field that appears under the dataId input — see step 2
of *Getting started*.)

**"No exposure-time record for dataId=N" / ConsDB unreachable** — ConsDB
either has no row for that id yet or is down. The **manual shutter close**
field appears under the input; type the timestamp in TAI ISO-8601
(`2026-06-24T14:38:41.380663`) and fetch with it. The value is cached for
that dataId under the active site, and a real ConsDB value supersedes it
once reachable. This is single-dataId only — the range and night forms
have no manual fallback.

**Shutter close looks off** — two things can make a 13-digit id mean
something other than what you meant.

*Wrong site:* check the site badge in the top bar. The summit cluster's
dataIds resolve against the summit ConsDB; BTS dataIds against the BTS
ConsDB, and the same id exists in both. The per-site cache files at
`~/.cache/ra_log_explorer/exposure-times/` keep them separate. If the
badge says the wrong thing, you are pointed at the wrong server (or
started this one with the wrong `--site`).

*Wrong instrument:* within one site, an id is only unique per
instrument — the sequence number restarts at 1 each night for each — so
on a night LSSTCam and LATISS both observe, the same id names a
different image on each. Check the **instrument switch** in the topbar:
everything on the page (the Tonight list, lookups, fetches) follows it,
and LSSTCam is the default. The explore view's info box shows which
instrument a loaded exposure resolved against.

**A few pods show 0 events but the exposure obviously touched them** —
look at the pod's `.jsonl` directly under the cache directory. If the
file is empty, that's Loki returning nothing for the window — usually a
transient series-index gap. Re-run with `--force-refresh` to pull again.

**A pod's log just stops mid-exposure, no traceback, no finish** — look
for a tall `restart` tick on that pod's lane a few seconds after the last
line (and an amber `⚠ window` flag on the row). That's a container that
died and was restarted in place — most often an out-of-memory kill the
kernel didn't log to us. The `restart` tick comes from the `k8s/events`
stream the fetch pulls alongside the logs; to *confirm* OOM specifically,
`kubectl get pod <pod> -n rapid-analysis -o jsonpath='{.status.containerStatuses[*].lastState.terminated}'`
shows `reason: OOMKilled` and exit code 137 (only until the next restart
overwrites it).

**Red "Incomplete fetch" banner (or an `INCOMPLETE FETCH` line on the
CLI)** — one or more pods are missing data, so the window you're looking
at is incomplete. This matters most in night mode, where missing pods
silently bias the Δshutter histograms. The banner lists the affected
pods; re-run with `--force-refresh` to retry. There are two causes, both
flagged the same way: a hard `logcli` failure (timeout / transient 5xx),
or a pod whose logs couldn't be verified complete. (The tool fetches
every line — it works around a Loki bug that silently drops lines on wide
busy windows by fetching in verified single-batch time-chunks — so it
either gets the whole window or tells you which pods it couldn't, never a
silent truncation.)

**Cache hit when you didn't expect one** — the tool reuses any
*superset* of the requested window. If you specifically want to refetch,
pass `--force-refresh`.

**Timeline shows events from the previous exposure** — the cached
window is wider than the one you asked for (because of superset reuse).
The events are still correctly placed in time; if it bothers you, run
with `--force-refresh` to write a tighter cache directory and reload.

**Browser opens but the page is blank / JS error** — check the terminal
for a Python traceback from the server. Re-run with `--port` set to a
free port if 8780 is in use.

## Running as a deployed service

Most of this README describes running the tool on your own machine.
It also ships as a container image, deployed on the Base Test Stand and
the summit through [Phalanx](https://github.com/lsst-sqre/phalanx) as
the **`log-explorer`** application, served at `https://<fqdn>/log-explorer`
behind Gafaelfawr. The chart lives in Phalanx under
`applications/log-explorer/`; this repo owns the image.

`.github/workflows/build.yaml` builds and pushes
`ghcr.io/lsst-so/ra_log_explorer` on every push to `main`, every
`tickets/**` branch (tagged `tickets-DM-xxxxx`), and every `v*` tag.
The image bakes in no environment — one tag serves both deployments — so
everything site-specific arrives at runtime through the variables in
[Configuration](#configuration). Each deployment ships a one-site
`sites.toml` naming its own cluster's in-cluster ConsDB, gets
`LOKI_PASSWORD` from its environment's Vault secret, and has
`RA_LOG_EXPLORER_MAX_CACHE_BYTES` derived from the size of the volume
provisioned for the cache — so "how much disk may this use" is answered
once, in the Helm values.

The image runs as UID 1000 with a read-only root filesystem: the cache
and `/tmp` arrive as mounted volumes. `/healthz` answers
`{"status": "ok"}` under the base path and is what the readiness probe
polls.

Build and smoke-test it locally with:

```bash
docker build -t ra-log-explorer:local .
docker run --rm -p 8080:8080 \
  -e RA_LOG_EXPLORER_BASE_PATH=/log-explorer \
  ra-log-explorer:local
# then: curl localhost:8080/log-explorer/healthz
```

The pinned `logcli` version in the `Dockerfile` is deliberate, not
tracked to latest — the fetch path works around grafana/loki#17270 by
reasoning about exactly when `logcli` paginates, so bumping it needs the
same end-to-end verification a fetch-path change does.

## For developers / contributors

The contributor guide and coding conventions live in
[CLAUDE.md](CLAUDE.md). Design and implementation docs are under
[architecture/](architecture/). The validation loop (pre-commit, mypy,
mypy-coverage, pytest) is summarised in the
[validation skill](.claude/skills/ra-log-explorer-validation/SKILL.md).

The test suite includes **browser tests** that drive Chromium against
the real app over a real (cut-down) night of logs, so they need one
extra setup step. They fail rather than skip if it is missing, on
purpose — a UI suite that skips itself looks exactly like one that
passes:

```sh
pip install -e '.[ui-test]'
playwright install chromium
pytest -n auto          # unit + browser, ~30 s
```

See [architecture/testing.md](architecture/testing.md#browser-tests) for
what they cover and what they deliberately don't.

If your change is user-visible — a new CLI flag, a new browser feature,
a change in the cache flush command, a new troubleshooting failure mode
— update this README in the same commit (the
[architecture-sync skill](.claude/skills/ra-log-explorer-architecture-sync/SKILL.md)
calls this out).
