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
from urllib.parse import parse_qs, urlparse

from . import parse as parser
from .fetch import loadPodLogPath


STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


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
    if is_dataclass(obj):
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


def _summaryToDict(
    s: parser.PodSummary, tZero: dt.datetime, expId: int
) -> dict:
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
    if targeted:
        firstT = targeted[0].t
        lastT = targeted[-1].t
        windowStart = firstT - dt.timedelta(seconds=3)
        windowEnd = lastT + dt.timedelta(seconds=3)
    else:
        windowStart = None
        windowEnd = None

    relevant: list[parser.Event] = []
    for ev in s.events:
        if ev.expId == expId:
            relevant.append(ev)
            continue
        if ev.expId is not None:
            continue  # explicitly tagged to a different exposure
        # Untagged event (typically a generic WARN/ERROR). Keep iff it
        # falls inside this pod's working window for the target exposure.
        if windowStart is None:
            # Pod has no explicit target events at all (e.g. head node only
            # references the exposure transitively via expRecord). Keep
            # untagged events so the user can still see warnings.
            relevant.append(ev)
        elif windowStart <= ev.t <= windowEnd:
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
    # Derive additional reference points from the head node events if available.
    for s in matchingSummaries:
        if s.group != "head":
            continue
        for ev in s.events:
            if ev.expId != state.expId:
                continue
            if ev.kind in (
                "HEAD_DEFINE_VISIT",
                "HEAD_FANOUT_DONE",
                "HEAD_GATHER_DISPATCH",
                "HEAD_POSTISR_MOSAIC",
                "HEAD_VISITIMAGE_MOSAIC",
            ):
                refs.append(
                    {
                        "label": f"{ev.kind} ({ev.who})" if ev.who else ev.kind,
                        "t": ev.t.isoformat(),
                        "offsetS": (ev.t - state.tZero).total_seconds(),
                        "source": "head",
                    }
                )
    # Derive "first ISR start" and "last visit-image written" across workers
    firstIsr: parser.Event | None = None
    lastVisitImg: parser.Event | None = None
    for s in matchingSummaries:
        for ev in s.events:
            if ev.expId != state.expId:
                continue
            if (
                ev.kind == "QUANTUM_PREP"
                and ev.taskLabel == "isr"
                and (firstIsr is None or ev.t < firstIsr.t)
            ):
                firstIsr = ev
            if (
                ev.kind == "WORKER_BINNED_PRELIMINARY_VISIT_IMAGE"
                and (lastVisitImg is None or ev.t > lastVisitImg.t)
            ):
                lastVisitImg = ev
    if firstIsr is not None:
        refs.append(
            {
                "label": "first ISR quantum start",
                "t": firstIsr.t.isoformat(),
                "offsetS": (firstIsr.t - state.tZero).total_seconds(),
                "source": "derived",
            }
        )
    if lastVisitImg is not None:
        refs.append(
            {
                "label": "last preliminary_visit_image written",
                "t": lastVisitImg.t.isoformat(),
                "offsetS": (lastVisitImg.t - state.tZero).total_seconds(),
                "source": "derived",
            }
        )

    return {
        "expId": state.expId,
        "tZero": state.tZero.isoformat(),
        "cacheDir": str(state.cacheDir),
        "cacheBytes": state.cacheBytes,
        "meta": _toJsonable(state.meta),
        "referencePoints": refs,
        "pods": [
            _summaryToDict(s, state.tZero, state.expId)
            for s in matchingSummaries
        ],
        "podsAll": [
            {"pod": s.pod, "group": s.group, "nLines": s.nLines,
             "nWarn": s.nWarn, "nError": s.nError}
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


def _makeHandler(state: ServerState):
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

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            # silence the default access log; uncomment for debugging.
            return

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send_file(TEMPLATES_DIR / "timeline.html")
                return
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
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
                pod = path[len("/api/pod/"):]
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
