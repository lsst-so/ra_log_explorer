"""Command-line entry point.

The CLI has two modes:

* **Home mode** (no arguments) — launches the server and opens the
  browser at the home page. Picking an exposure, supplying credentials,
  watching the fetch happen, and exploring it all happen in the UI.
  This is the recommended path.

* **Eager fetch mode** (``--exposure-id`` and ``--t-zero`` supplied) —
  performs the fetch + parse on the command line first, then starts the
  server already loaded into the explore view. Useful for scripting or
  if you already know exactly what you want and prefer not to click
  through the home form.

Examples::

    # Home mode — start the server, pick an exposure in the browser.
    python3 -m ra_log_explorer.cli

    # Eager mode — fetch this exposure first, then open the explore view.
    python3 -m ra_log_explorer.cli \\
        --exposure-id 2026051900722 \\
        --t-zero 2026-05-20T08:46:16.267

    # Show cache info / flush.
    python3 -m ra_log_explorer.cli cache info
    python3 -m ra_log_explorer.cli cache flush
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import webbrowser

from . import parse as parser
from .config import (
    DEFAULT_CLUSTER,
    DEFAULT_HTTP_PORT,
    DEFAULT_LOKI_ADDR,
    DEFAULT_NAMESPACE,
    DEFAULT_USERNAME,
    DEFAULT_WINDOW_AFTER_S,
    DEFAULT_WINDOW_BEFORE_S,
    DEFAULT_WORKERS,
    FetchSpec,
    cache_root,
)
from .fetch import (
    cacheDuSizeBytes,
    fetchAll,
    humanBytes,
    stderrProgress,
)
from .jobs import JobManager
from .server import ServerContext, ServerState, serve

# TAI is ahead of UTC by 37 seconds (since 2017-01-01; no further leap
# seconds have been added). Butler `DimensionRecord` timestamps are TAI,
# so by default we subtract this when converting the user's t-zero into
# UTC. Override with --t-zero-utc.
TAI_MINUS_UTC_S = 37.0


def _parseIsoUtc(s: str) -> dt.datetime:
    """Parse an ISO-8601 string into a UTC `datetime`.

    A trailing 'Z' is honoured; an explicit ``+HH:MM`` / ``-HH:MM`` offset is
    honoured; otherwise the string is assumed to be in the timezone of its
    domain (UTC for log timestamps, TAI for Butler DimensionRecords — the
    caller is responsible for any TAI→UTC adjustment before passing it here).
    """
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "+" not in s and "-" not in s[10:]:
        # no offset given - assume UTC
        s = s + "+00:00"
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)


def _isoForLogcli(t: dt.datetime) -> str:
    """Format a datetime for logcli --from/--to (RFC3339Nano UTC, with Z)."""
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _addCommonArgs(p: argparse.ArgumentParser) -> None:
    p.add_argument("--loki-addr", default=DEFAULT_LOKI_ADDR)
    p.add_argument("--username", default=DEFAULT_USERNAME)
    p.add_argument("--cluster", default=DEFAULT_CLUSTER)
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel download workers (default {DEFAULT_WORKERS})",
    )
    p.add_argument(
        "--window-before",
        type=float,
        default=DEFAULT_WINDOW_BEFORE_S,
        help="Seconds before t-zero to start the fetch window",
    )
    p.add_argument(
        "--window-after",
        type=float,
        default=DEFAULT_WINDOW_AFTER_S,
        help="Seconds after t-zero to end the fetch window",
    )
    p.add_argument("--force-refresh", action="store_true", help="Re-fetch even if cached results exist")


def _eagerFetchAndBuildState(args: argparse.Namespace) -> ServerState:
    """Do the CLI-side fetch + parse and return a populated `ServerState`."""
    tZeroInput = _parseIsoUtc(args.t_zero)
    if args.t_zero_utc:
        tZero = tZeroInput
        tZeroScale = "UTC"
    else:
        tZero = tZeroInput - dt.timedelta(seconds=TAI_MINUS_UTC_S)
        tZeroScale = "TAI"
    print(
        f"t-zero (input, {tZeroScale}): {tZeroInput.isoformat()}\n"
        f"t-zero (used, UTC):     {tZero.isoformat()}",
        file=sys.stderr,
    )
    fromT = tZero - dt.timedelta(seconds=args.window_before)
    toT = tZero + dt.timedelta(seconds=args.window_after)
    spec = FetchSpec(
        lokiAddr=args.loki_addr,
        username=args.username,
        cluster=args.cluster,
        namespace=args.namespace,
        fromIso=_isoForLogcli(fromT),
        toIso=_isoForLogcli(toT),
        workers=args.workers,
    )
    print(f"Window: {spec.fromIso}  to  {spec.toIso}", file=sys.stderr)
    cacheDir, meta = fetchAll(spec, progress=stderrProgress, forceRefresh=args.force_refresh)
    print(f"Cache dir: {cacheDir}", file=sys.stderr)
    reuse = meta.get("cacheReuse", "none")
    if reuse == "exact":
        print(
            f"Cache hit (exact): {meta['pod_count']} pods, " f"{humanBytes(meta['total_bytes'])}.",
            file=sys.stderr,
        )
    elif reuse == "superset":
        print(
            f"Cache hit (superset reuse): {meta['pod_count']} pods, "
            f"{humanBytes(meta['total_bytes'])} from {meta.get('cacheReusePath')}",
            file=sys.stderr,
        )
    else:
        print(
            f"Downloaded {meta['pod_count']} pods, "
            f"{humanBytes(meta['total_bytes'])} in {meta['elapsed_s']:.1f}s.",
            file=sys.stderr,
        )
        if meta.get("errors"):
            print(
                f"  WARNING: {len(meta['errors'])} pods failed; see {cacheDir}/_meta.json",
                file=sys.stderr,
            )
    cacheBytes = cacheDuSizeBytes(cache_root())
    print(f"Total cache: {humanBytes(cacheBytes)} at {cache_root()}", file=sys.stderr)

    print("Parsing logs ...", file=sys.stderr)
    summaries = parser.summarizeAll(cacheDir)
    nRelevant = sum(1 for s in summaries if args.exposure_id in s.expIdsSeen)
    print(
        f"  {len(summaries)} pods parsed, " f"{nRelevant} touched expId={args.exposure_id}",
        file=sys.stderr,
    )

    return ServerState(
        cacheDir=cacheDir,
        cacheBytes=cacheBytes,
        meta=meta,
        summaries=summaries,
        expId=args.exposure_id,
        tZero=tZero,
        referencePoints=[
            {
                "label": (f"shutter close (caller-supplied, " f"{tZeroScale} input)"),
                "t": tZero.isoformat(),
                "offsetS": 0.0,
                "source": "shutter close",
            }
        ],
    )


def cmdRun(args: argparse.Namespace) -> int:
    eager = args.exposure_id is not None and args.t_zero is not None
    if (args.exposure_id is None) != (args.t_zero is None):
        # one without the other — easy mistake; surface it explicitly.
        print(
            "Error: --exposure-id and --t-zero must be supplied together "
            "(or both omitted, in which case the server opens at the home page).",
            file=sys.stderr,
        )
        return 2

    state: ServerState | None = None
    if eager:
        state = _eagerFetchAndBuildState(args)
        if args.no_serve:
            return 0
    else:
        print("Starting in home mode — pick an exposure in the browser.", file=sys.stderr)

    ctx = ServerContext(jobs=JobManager(), state=state)
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    serve(args.host, args.port, ctx)
    return 0


def cmdCacheInfo(args: argparse.Namespace) -> int:
    root = cache_root()
    total = cacheDuSizeBytes(root)
    print(f"Cache root: {root}")
    print(f"Total size: {humanBytes(total)}")
    print()
    print("Windows:")
    if not any(root.iterdir()):
        print("  (empty)")
        return 0
    for cluster in sorted(p for p in root.iterdir() if p.is_dir()):
        for ns in sorted(p for p in cluster.iterdir() if p.is_dir()):
            for window in sorted(p for p in ns.iterdir() if p.is_dir()):
                size = cacheDuSizeBytes(window)
                meta = window / "_meta.json"
                tag = "ok " if meta.exists() else "partial"
                print(f"  [{tag}] {humanBytes(size):>10}  " f"{cluster.name}/{ns.name}/{window.name}")
    return 0


def cmdCacheFlush(args: argparse.Namespace) -> int:
    root = cache_root()
    if not root.exists():
        print("Nothing to flush.")
        return 0
    if args.yes or input(f"Delete entire cache at {root}? [y/N] ").lower() == "y":
        shutil.rmtree(root)
        print("Cache flushed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ra-log-explorer")
    sub = p.add_subparsers(dest="cmd")

    # default subcommand: run
    runP = sub.add_parser("run", help="start the server (optionally with an eager fetch)")
    runP.add_argument(
        "--exposure-id",
        type=int,
        help="13-digit dataId, e.g. 2026051900722. Optional; if omitted (along with "
        "--t-zero) the server opens at the home page.",
    )
    runP.add_argument(
        "--t-zero",
        help="Shutter-close time, ISO-8601, e.g. 2026-05-20T08:46:16.267. "
        "Treated as TAI by default (Butler DimensionRecord convention); "
        "pass --t-zero-utc if your value is already in UTC. "
        "Optional; must be paired with --exposure-id when supplied.",
    )
    runP.add_argument(
        "--t-zero-utc",
        action="store_true",
        help="Treat --t-zero as already-UTC instead of TAI (default off; "
        f"the default subtracts {int(TAI_MINUS_UTC_S)} s from the input).",
    )
    runP.add_argument("--host", default="127.0.0.1")
    runP.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    runP.add_argument(
        "--no-serve",
        action="store_true",
        help="With --exposure-id/--t-zero: fetch + parse only, do not start the web UI. "
        "Ignored in home mode.",
    )
    runP.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    _addCommonArgs(runP)
    runP.set_defaults(fn=cmdRun)

    cacheP = sub.add_parser("cache", help="inspect or flush the on-disk cache")
    cacheSub = cacheP.add_subparsers(dest="cacheCmd")
    infoP = cacheSub.add_parser("info", help="summarize cache contents")
    infoP.set_defaults(fn=cmdCacheInfo)
    flushP = cacheSub.add_parser("flush", help="delete the entire cache")
    flushP.add_argument("--yes", action="store_true", help="Don't prompt for confirmation")
    flushP.set_defaults(fn=cmdCacheFlush)

    # allow invoking with the run flags directly, no subcommand
    p.add_argument("--exposure-id", type=int)
    p.add_argument("--t-zero")
    p.add_argument("--t-zero-utc", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    p.add_argument("--no-serve", action="store_true")
    p.add_argument("--no-browser", action="store_true")
    _addCommonArgs(p)
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if getattr(args, "fn", None) is None:
        # No subcommand: default to run (home mode if exposure args missing).
        args.fn = cmdRun
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
