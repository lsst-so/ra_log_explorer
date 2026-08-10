# The UI tests' log corpus

`july11.tar.gz` (1.8 MB) is the fixture every browser test in
[`tests/ui/`](../../ui/) runs against. It is a cut-down but **entirely
real** night of rapid-analysis logs: dayObs 20260711 on the summit, both
instruments, exactly as this tool fetched it from Loki. Nothing in it is
synthesised — every line is a captured log line at its real timestamp,
and the only edit is which lines are present.

Unpacked it is ~20 MB laid out as a **live night dir** — the shape the
live poller maintains:

```
yagan/rapid-analysis/2026-07-11T120000_000000Z__2026-07-12T120000_000000Z/
    _live.json            watermark + per-pod byte/line counts
    pods.txt
    pods/<pod>.jsonl      32 pods, ~93k lines of app logs
    pods_events/<pod>.jsonl   the whole night's k8s lifecycle events
exposure-times/summit.json    the ConsDB records for those exposures
```

That shape is deliberate: every other window a test needs — one
exposure's window, the AOS night, a range — is produced from it by the
application's *own* slicing code, so a test opening an exposure sees
exactly the bytes a real user would.

## What is in it, and why

The full night is 576 pods, 35.7M lines, 9.25 GiB. Two cuts bring it
down, both chosen from measurements rather than guesses:

- **Pods.** 378 of the 576 are SFM workers and account for 370 MB of any
  30-minute window; the whole rest of the pod-group taxonomy is ~25 MB.
  So the corpus keeps a handful per group and *every* group across both
  instruments — which is what the UI actually renders (group ordering,
  labels, collapse, the instrument split).
- **Time.** App logs are cut to two windows totalling ~29 minutes of
  real observing. Two, not one, because an exposure id collides across
  instruments but a *time* does not: each instrument counts its own
  sequence from 1 each night, so LSSTCam's ids 427–458 are taken at
  04:12–04:29 while LATISS's ids 427–458 — different exposures, same
  numbers — are taken an hour later. A single window would give a corpus
  with no colliding ids at all, and the instrument pin is the thing that
  most needs testing against real ones. There are 31.

The `k8s/events` stream is kept for the **whole night** regardless: it
is 2.5 MB for all 576 pods, and the night's only `POD_RESTARTED` events
happen during a rollout at 14:40 UTC, hours before observing starts.
Cutting it to the app-log window would leave the restarts table empty.

What that buys, in the views:

| View | What the corpus gives it |
|---|---|
| Explore (LSSTCam) | 15 pods over 12 groups, ~1250 events, 124 tracebacks |
| Explore (LATISS) | the *same* dataId, an hour earlier, a different pod set |
| Night (AOS) | 35 visits, 435 tracebacks, 6 exception classes, 1 pod restart |
| Banners | a real gather-only warning — the cut genuinely leaves two visits with step1b and no step1a, which is exactly what that banner exists to catch |

## Regenerating it

`build_fixture.py` is the one-shot that made it. It is checked in so the
corpus can be widened, re-cut, or refreshed from a newer capture rather
than being an opaque blob nobody can reproduce:

```sh
python tests/data/ui/build_fixture.py --master /path/to/a/fetched/night
tar -czf tests/data/ui/july11.tar.gz -C tests/data/ui july11
rm -rf tests/data/ui/july11
```

Adjust `POD_BUDGET` or the two windows at the top of the script to
change what is included. It prints the resulting size, pod count and
number of colliding ids.

Gzip rather than xz, though xz would be a third smaller: `lzma` is an
optional CPython build dependency and is genuinely missing on some
interpreters (including the pyenv build this was developed on), which
would turn "run the tests" into "rebuild your Python".

**Rebuilding invalidates the constants** pinned in
[`tests/ui/corpus.py`](../../ui/corpus.py) — the shared dataId, the two
shutter closes, the range bounds. They are facts about this data, and
they are pinned there rather than scattered through the tests precisely
so there is one place to re-check.
