"""Stdlib HTTP server serving the timeline UI + JSON API.

Usage:

    from ra_log_explorer.server import serve
    serve(host="127.0.0.1", port=8765, state=state)

`state` is a `ServerState` object pre-populated by the CLI with the parsed
summaries; the server only reads from it.
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import parse as parser
from .fetch import loadPodLogPath

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


# Palette tuned to be visually distinguishable on white. The first two
# entries are pinned to `isr` and `calibrateImage` so the most common bars
# always get the same colour; everything else is assigned by position in
# sorted order, guaranteeing zero collisions until we exceed the palette
# size. The full LSSTCam SFM+AOS step1a/step1b pipeline graph today emits
# ~16 distinct task labels, well under the 24 here.
_TASK_PALETTE = [
    "#d4801f",  # 0 orange (pinned: isr)
    "#1a9c8c",  # 1 teal   (pinned: calibrateImage)
    "#1d6fb0",  # 2 blue
    "#6a4ea3",  # 3 purple
    "#c1252b",  # 4 red
    "#4d9221",  # 5 green
    "#c41a85",  # 6 magenta
    "#a05f00",  # 7 brown
    "#168aad",  # 8 cyan
    "#842cad",  # 9 violet
    "#d4b500",  # 10 gold
    "#8b1ab3",  # 11 deep purple
    "#5b8a3f",  # 12 olive
    "#306b6b",  # 13 dark teal
    "#a52a2a",  # 14 dark red
    "#d96a9d",  # 15 pink
    "#005f73",  # 16 dark cyan
    "#9b59b6",  # 17 mauve
    "#27ae60",  # 18 emerald
    "#e67e22",  # 19 amber
    "#7f7f7f",  # 20 grey
    "#2c3e50",  # 21 slate
    "#16a085",  # 22 sea green
    "#34495e",  # 23 charcoal
]
_TASK_COLOR_PINNED = {
    "isr": _TASK_PALETTE[0],
    "calibrateImage": _TASK_PALETTE[1],
}


def _assignTaskColors(tasks: list[str]) -> dict[str, str]:
    """Return a deterministic {task: hex} mapping with no collisions.

    Sort order is alphabetical for stability across re-renders within a
    single run. We do not try to make the mapping stable across different
    *exposures* — pin the few important tasks in ``_TASK_COLOR_PINNED`` if
    cross-exposure consistency matters for that one.
    """
    result: dict[str, str] = {}
    available = _TASK_PALETTE[len(_TASK_COLOR_PINNED) :]
    others: list[str] = []
    for t in sorted(tasks):
        if t in _TASK_COLOR_PINNED:
            result[t] = _TASK_COLOR_PINNED[t]
        else:
            others.append(t)
    for i, t in enumerate(others):
        result[t] = available[i % len(available)]
    return result


@dataclass
class ServerState:
    cacheDir: Path
    cacheBytes: int
    meta: dict  # cache meta produced by fetch.fetchAll
    summaries: list[parser.PodSummary]
    expId: int
    tZero: dt.datetime
    referencePoints: list[dict] = field(default_factory=list)


def _toJsonable(obj: Any) -> Any:
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return _toJsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _toJsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_toJsonable(x) for x in obj]
    return obj


def _eventToDict(ev: parser.Event, tZero: dt.datetime) -> dict:
    return {
        "pod": ev.pod,
        "t": ev.t.isoformat(),
        "offsetS": (ev.t - tZero).total_seconds(),
        "kind": ev.kind,
        "level": ev.level,
        "expId": ev.expId,
        "detector": ev.detector,
        "visit": ev.visit,
        "who": ev.who,
        "taskLabel": ev.taskLabel,
        "durationS": ev.durationS,
        "flavor": ev.flavor,
        "message": ev.message,
        "raw": ev.raw,
    }


def _summaryToDict(s: parser.PodSummary, tZero: dt.datetime, expId: int) -> dict:
    """Build the per-pod payload for the timeline view.

    Events that explicitly reference `expId` are always included. Generic
    WARN/ERROR events (which carry no expId by construction) are included
    only if they fall inside the temporal window where this pod was working
    on the target exposure — defined as the interval between the first and
    last explicitly-relevant event, padded by a few seconds on either side.
    This keeps unrelated warnings from the previous/next exposure from
    cluttering the per-pod timeline.
    """
    targeted = [ev for ev in s.events if ev.expId == expId]
    window: tuple[dt.datetime, dt.datetime] | None = None
    if targeted:
        window = (
            targeted[0].t - dt.timedelta(seconds=3),
            targeted[-1].t + dt.timedelta(seconds=3),
        )

    relevant: list[parser.Event] = []
    for ev in s.events:
        if ev.expId == expId:
            relevant.append(ev)
            continue
        if ev.expId is not None:
            continue  # explicitly tagged to a different exposure
        # Untagged event (typically a generic WARN/ERROR). Keep iff it
        # falls inside this pod's working window for the target exposure.
        if window is None:
            # Pod has no explicit target events at all (e.g. head node only
            # references the exposure transitively via expRecord). Keep
            # untagged events so the user can still see warnings.
            relevant.append(ev)
        elif window[0] <= ev.t <= window[1]:
            relevant.append(ev)

    return {
        "pod": s.pod,
        "group": s.group,
        "instrument": s.instrument,
        "ordinal": s.ordinal,
        "nLines": s.nLines,
        "nWarn": s.nWarn,
        "nError": s.nError,
        "nTraceback": s.nTraceback,
        "firstTs": s.firstTs.isoformat() if s.firstTs else None,
        "lastTs": s.lastTs.isoformat() if s.lastTs else None,
        "events": [_eventToDict(ev, tZero) for ev in relevant],
    }


def _buildSummaryPayload(state: ServerState) -> dict:
    matchingSummaries = parser.podsTouchingExp(state.summaries, state.expId)
    refs = list(state.referencePoints)
    # Head node's first acknowledgement of this exposure — the moment
    # ButlerWatcher+head observed it as "ready to process". Useful for
    # subtracting out readout + Butler ingest latency.
    firstDefineVisit: parser.Event | None = None
    for s in matchingSummaries:
        if s.group != "head":
            continue
        for ev in s.events:
            if ev.expId != state.expId:
                continue
            if ev.kind == "HEAD_DEFINE_VISIT" and (firstDefineVisit is None or ev.t < firstDefineVisit.t):
                firstDefineVisit = ev
    if firstDefineVisit is not None:
        refs.append(
            {
                "label": "head node first defined visit",
                "t": firstDefineVisit.t.isoformat(),
                "offsetS": (firstDefineVisit.t - state.tZero).total_seconds(),
                "source": "head",
            }
        )

    # Discover every distinct task label across the relevant pods so we can
    # emit a collision-free colour map for the timeline + dynamic legend.
    taskLabels: set[str] = set()
    for s in matchingSummaries:
        for ev in s.events:
            if ev.taskLabel:
                taskLabels.add(ev.taskLabel)
    taskColors = _assignTaskColors(sorted(taskLabels))

    return {
        "expId": state.expId,
        "tZero": state.tZero.isoformat(),
        "cacheDir": str(state.cacheDir),
        "cacheBytes": state.cacheBytes,
        "meta": _toJsonable(state.meta),
        "referencePoints": refs,
        "taskColors": taskColors,
        "pods": [_summaryToDict(s, state.tZero, state.expId) for s in matchingSummaries],
        "podsAll": [
            {"pod": s.pod, "group": s.group, "nLines": s.nLines, "nWarn": s.nWarn, "nError": s.nError}
            for s in state.summaries
        ],
    }


def _podDetail(state: ServerState, pod: str) -> dict:
    logPath = loadPodLogPath(state.cacheDir, pod)
    lines: list[dict] = []
    for ln in parser.iterPodLines(logPath):
        lines.append(
            {
                "t": ln.timestamp.isoformat(),
                "offsetS": (ln.timestamp - state.tZero).total_seconds(),
                "level": ln.level,
                "logger": ln.logger,
                "function": ln.function,
                "message": ln.message,
                "raw": ln.raw,
            }
        )
    return {"pod": pod, "lines": lines}


def _makeHandler(state: ServerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(404, f"Not found: {path.name}")
                return
            mime, _ = mimetypes.guess_type(path.name)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            # silence the default access log; uncomment for debugging.
            return

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send_file(TEMPLATES_DIR / "timeline.html")
                return
            if path.startswith("/static/"):
                rel = path[len("/static/") :]
                # disallow path traversal
                if ".." in rel.split("/"):
                    self.send_error(400)
                    return
                self._send_file(STATIC_DIR / rel)
                return
            if path == "/api/summary":
                self._send_json(_buildSummaryPayload(state))
                return
            if path.startswith("/api/pod/"):
                pod = path[len("/api/pod/") :]
                pod = pod.split("?")[0]
                if not re.match(r"^[A-Za-z0-9._-]+$", pod):
                    self.send_error(400, "Invalid pod name")
                    return
                self._send_json(_podDetail(state, pod))
                return
            self.send_error(404)

    return Handler


def serve(host: str, port: int, state: ServerState) -> None:
    handler = _makeHandler(state)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Serving on http://{host}:{port} (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
