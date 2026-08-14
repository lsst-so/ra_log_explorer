"""The explore view: one exposure's timeline, and what you can do to it.

Everything here runs against a real slice of the captured night, so the
numbers on the page are the numbers in the logs. Where a test pins a
count it is because getting that count wrong would mean the parser or
the renderer had changed what the timeline says happened.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import expect

from .corpus import CRASH_ID, SHARED_ID, StagedCorpus


def openExposure(
    app: Any, corpus: StagedCorpus, instrument: str = "lsstcam", dataId: int = SHARED_ID
) -> None:
    corpus.stageExposure(dataId, instrument)
    app.goto(f"/?dataId={dataId}&instrument={instrument}")
    expect(app.page.locator("#explore-view")).to_be_visible()
    expect(app.page.locator("#timeline .tl-row").first).to_be_visible()


def test_the_timeline_groups_pods_by_role(app: Any, corpus: StagedCorpus) -> None:
    openExposure(app, corpus)
    headers = app.page.locator("#timeline .tl-group-header").all_inner_texts()
    joined = " ".join(headers)
    # The control plane leads, then the workers that did the processing.
    assert "head-node" in joined
    assert "sfm-runner" in joined
    assert "aos-worker" in joined
    assert headers.index(next(h for h in headers if "head-node" in h)) == 0


def test_every_pod_row_carries_a_name_and_a_track(app: Any, corpus: StagedCorpus) -> None:
    openExposure(app, corpus)
    rows = app.page.locator("#timeline .tl-row")
    assert rows.count() > 5
    expect(app.page.locator("#timeline .tl-podname").first).to_be_visible()
    expect(app.page.locator("#timeline .tl-track").first).to_be_visible()
    # Events are the point of the view; an empty timeline is a failure
    # mode that "the page rendered" would not catch.
    assert app.page.locator("#timeline .tl-event").count() > 20


def test_events_are_placed_in_time_order_along_the_track(app: Any, corpus: StagedCorpus) -> None:
    """Each event's x comes from its offset, so a broken mapping shows up
    as events in the wrong place rather than as anything missing.

    Rows are emitted in time order, so within a row the x positions must
    ascend. Duration bars are excluded: they are drawn from their *start*
    (offset minus duration), so one legitimately begins to the left of
    the instantaneous event before it.
    """
    openExposure(app, corpus)
    rows = app.page.evaluate("""() => [...document.querySelectorAll('#timeline .tl-row')]
              .map(row => [...row.querySelectorAll('.tl-event:not(.bar)')]
                  .map(e => e.getBoundingClientRect().left))
              .filter(xs => xs.length > 3)""")
    assert len(rows) >= 3, f"expected several pod rows with events, got {len(rows)}"
    for xs in rows:
        assert xs == sorted(xs), xs
    # And they are actually spread out, not all stacked at the origin.
    assert max(max(xs) - min(xs) for xs in rows) > 50


def test_switching_the_t_zero_reference_re_anchors_the_timeline(app: Any, corpus: StagedCorpus) -> None:
    """t0 is a choice: the shutter close, or something the logs
    themselves establish — the head node's first defined visit. Picking a
    different one moves the zero line and relabels the axis. The events
    keep their positions, because what changed is where "zero" is, not
    when anything happened.
    """
    openExposure(app, corpus)
    select = app.page.locator("#ref-select")
    options = select.locator("option").all_inner_texts()
    # The list opens with the API t0 and the payload's own shutter-close
    # entry — the same instant twice. The interesting one is derived from
    # the logs.
    derived = next((i for i, o in enumerate(options) if "head" in o.lower()), None)
    assert derived is not None, f"no log-derived reference point offered: {options}"

    def markerX() -> float:
        return float(
            app.page.evaluate(
                "() => document.querySelector('#timeline .tl-t0-marker').getBoundingClientRect().left"
            )
        )

    def eventX() -> float:
        return float(
            app.page.evaluate(
                "() => document.querySelector('#timeline .tl-event').getBoundingClientRect().left"
            )
        )

    zeroBefore, eventBefore = markerX(), eventX()
    labelsBefore = app.page.locator("#timeline .tl-axis-row").inner_text()
    t0Before = app.page.locator("#t0-info").inner_text()

    select.select_option(index=derived)
    expect(app.page.locator("#t0-info")).not_to_have_text(t0Before)
    assert markerX() != zeroBefore, "the zero line should move to the new reference"
    assert app.page.locator("#timeline .tl-axis-row").inner_text() != labelsBefore
    assert eventX() == eventBefore, "the events happened when they happened"


def test_the_filter_box_narrows_the_visible_pods(app: Any, corpus: StagedCorpus) -> None:
    openExposure(app, corpus)
    allRows = app.page.locator("#timeline .tl-row:visible").count()
    app.page.locator("#search").fill("aos-worker")
    expect(app.page.locator("#timeline .tl-row:visible")).not_to_have_count(allRows)
    visible = app.page.locator("#timeline .tl-row:visible .tl-podname").all_inner_texts()
    assert visible and all("aos" in name for name in visible), visible
    app.page.locator("#search").fill("")
    expect(app.page.locator("#timeline .tl-row:visible")).to_have_count(allRows)


def test_clicking_a_pod_opens_its_log(app: Any, corpus: StagedCorpus) -> None:
    """The drawer re-reads the pod's JSONL from disk, so this covers the
    /api/pod route as well as the rendering."""
    openExposure(app, corpus)
    row = app.page.locator("#timeline .tl-row").first
    podName = row.locator(".tl-podname").inner_text()
    row.click()
    detail = app.page.locator("#detail")
    expect(detail).not_to_have_class("closed")
    expect(app.page.locator("#detail-title")).to_contain_text(podName.split()[0][:12])
    expect(app.page.locator("#detail-body .dt-row").first).to_be_visible()
    assert app.page.locator("#detail-body .dt-row").count() > 1
    # Real log text, not just rows: this is read back off disk per request.
    expect(app.page.locator("#detail-body")).to_contain_text(str(SHARED_ID))
    app.page.locator("#detail-close").click()
    expect(detail).to_have_class("closed")


def test_the_detail_drawer_can_be_narrowed_to_warnings_and_errors(app: Any, corpus: StagedCorpus) -> None:
    openExposure(app, corpus)
    # A pod with errors, so the filter has something to keep.
    app.page.locator("#timeline .tl-row", has=app.page.locator(".tl-podname")).filter(
        has_text="aos-worker"
    ).first.click()
    expect(app.page.locator("#detail")).to_be_visible()
    allRows = app.page.locator("#detail-body .dt-row").count()
    app.page.locator("#detail-warn-only").check()
    expect(app.page.locator("#detail-body .dt-row")).not_to_have_count(allRows)
    levels = app.page.evaluate("""() => [...document.querySelectorAll('#detail-body .dt-row')]
               .map(r => r.className)""")
    assert levels, "warn/error-only should not empty a pod that has errors"
    assert all("level-warn" in c or "level-error" in c for c in levels), levels


def test_groups_collapse_and_expand(app: Any, corpus: StagedCorpus) -> None:
    """Collapse folds away the groups with more than one pod — the ones
    that make a real exposure's 200-row fan-out unreadable. Single-pod
    groups stay, because folding a group of one hides information and
    saves nothing."""
    openExposure(app, corpus)
    visibleBefore = app.page.locator("#timeline .tl-row:visible").count()
    app.page.locator("#groups-collapse-all").click()
    collapsed = app.page.locator("#timeline .tl-row:visible").count()
    assert 0 < collapsed < visibleBefore
    multiPodGroups = app.page.locator("#timeline .tl-group-header").filter(has_text="pods")
    assert multiPodGroups.count() > 0
    app.page.locator("#groups-expand-all").click()
    expect(app.page.locator("#timeline .tl-row:visible")).to_have_count(visibleBefore)


def test_the_exposure_info_box_leads_with_the_instrument(app: Any, corpus: StagedCorpus) -> None:
    """It is the first thing shown because it is part of the exposure's
    identity, not one of its properties."""
    openExposure(app, corpus)
    box = app.page.locator("#exposure-info")
    expect(box).to_be_visible()
    # CSS upper-cases the labels, so compare case-insensitively.
    labels = [t.strip().lower() for t in box.locator(".exp-label").all_inner_texts()]
    assert labels[0].startswith("instrument"), labels[:4]
    expect(box).to_contain_text("lsstcam")
    expect(box).to_contain_text("science")


def test_a_pod_with_a_traceback_is_flagged_on_its_row(app: Any, corpus: StagedCorpus) -> None:
    openExposure(app, corpus)
    flagged = app.page.locator("#timeline .tl-row.has-traceback")
    assert flagged.count() > 0, "this exposure really does have failing pods"
    expect(flagged.first).to_contain_text("TB")


def test_no_fetch_banner_for_a_complete_window(app: Any, corpus: StagedCorpus) -> None:
    openExposure(app, corpus)
    expect(app.page.locator("#explore-fetch-banner")).to_be_hidden()


def test_the_fetch_banner_shouts_when_pods_are_missing(app: Any, corpus: StagedCorpus) -> None:
    """An incomplete window silently biases everything computed from it,
    so it gets a banner naming the pods rather than a quiet flag."""
    import json

    cacheDir = corpus.stageExposure(SHARED_ID, "lsstcam")
    metaPath = cacheDir / "_meta.json"
    meta = json.loads(metaPath.read_text())
    meta["incomplete_pods"] = {"s-misc-run-tma-telemetry-6d86bc45bd-4bmz4": "chunk floor"}
    meta["errors"] = {"s-lsstcam-run-sfm-runner-workerset-327": "logcli timed out"}
    meta["fetchComplete"] = False
    metaPath.write_text(json.dumps(meta))

    app.goto(f"/?dataId={SHARED_ID}&instrument=lsstcam")
    banner = app.page.locator("#explore-fetch-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("tma-telemetry")
    expect(banner).to_contain_text("sfm-runner")


def test_the_task_legend_lists_the_pipeline_tasks(app: Any, corpus: StagedCorpus) -> None:
    openExposure(app, corpus)
    tasks = app.page.locator("#task-legend .lg-task").all_inner_texts()
    assert tasks, "the legend should name the tasks the coloured bars stand for"
    assert any("isr" in t.lower() for t in tasks), tasks


def test_a_pod_death_shows_up_on_the_timeline(app: Any, corpus: StagedCorpus) -> None:
    """`looksTruncatedEnd` says a pod stopped without finishing; a
    lifecycle marker says why. They are complementary, and the marker is
    drawn full-height so "the whole pod went" reads differently from a
    task tick.

    The crash here is real (see StagedCorpus.plantPodCrash) and its final
    ImagePullBackOff burst falls inside this exposure's window.
    """
    corpus.plantPodCrash()
    openExposure(app, corpus, dataId=CRASH_ID)
    row = app.page.locator("#timeline .tl-row", has_text="gather1baosset-0").first
    expect(row).to_be_visible()
    markers = row.locator(".tl-event.lifecycle")
    assert markers.count() >= 1, "the crash left no marker on the pod's lane"
    markers.first.hover()
    tooltip = app.page.locator("#tooltip")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("POD_")


def test_lifecycle_markers_are_kept_across_the_whole_window(app: Any, corpus: StagedCorpus) -> None:
    """Unlike dataId-keyed events these are kept on the broad exposure
    window, not the tight per-dataId one: a pod usually dies a few
    seconds after its last work line, and cutting at the last line would
    hide exactly the marker that explains the gap."""
    corpus.plantPodCrash()
    openExposure(app, corpus, dataId=CRASH_ID)
    assert app.page.locator("#timeline .tl-event.lifecycle").count() >= 1


def test_a_pod_death_is_hard_to_miss(app: Any, corpus: StagedCorpus) -> None:
    """A pod dying mid-night should never happen, so when it does the
    marker is meant to be the loudest thing on the page rather than a
    4px tick to go looking for: it pulses, it is wider, and it writes
    what happened next to itself so the tooltip isn't the only way to
    find out.

    A graceful `Killing` — an ordinary rollout — deliberately gets none
    of that; an alarm that fires on routine events is one people learn
    to ignore.
    """
    corpus.plantPodCrash()
    openExposure(app, corpus, dataId=CRASH_ID)
    row = app.page.locator("#timeline .tl-row", has_text="gather1baosset-0").first
    alarms = row.locator(".tl-event.lifecycle.alarm")
    assert alarms.count() >= 1, "the crash left no alarming marker"
    expect(alarms.first.locator(".tl-lifecycle-label")).to_have_text(
        re.compile(r"restart|OOM kill|pod failed")
    )
    # The animation is real, not just a class name.
    assert (
        app.page.evaluate("() => getComputedStyle(document.querySelector('.tl-event.alarm')).animationName")
        == "ra-alarm-pulse"
    )
    # The label is decoration: it must not swallow hovers meant for what
    # is underneath it. (The marker's own hover → tooltip is covered by
    # test_a_pod_death_shows_up_on_the_timeline; asserting it here as
    # well would only be testing which of several coincident markers
    # happens to be on top.)
    assert (
        app.page.evaluate(
            "() => getComputedStyle(document.querySelector('.tl-lifecycle-label')).pointerEvents"
        )
        == "none"
    )
    # A graceful kill is not alarmed.
    killed = row.locator(".tl-event.kind-killed")
    if killed.count():
        expect(killed.first).not_to_have_class(re.compile(r"\balarm\b"))
