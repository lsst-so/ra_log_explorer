"""The night view: a whole dayObs of AOS processing, and its failures.

The corpus is a real night, so the counts here are real counts. That is
the point: a change that makes the parser attribute a traceback to the
wrong dataId, or the histogram put a value in the wrong bin, moves a
number that a test is looking at.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import expect

from .corpus import DAY_OBS, StagedCorpus


def openNight(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageNight()
    app.goto(f"/?dayObs={DAY_OBS}")
    expect(app.page.locator("#night-view")).to_be_visible()
    expect(app.page.locator("#night-stats .night-stat").first).to_be_visible()


def statValue(app: Any, label: str) -> int:
    return int(
        app.page.locator(".night-stat", has_text=label).first.locator(".night-stat-value").inner_text()
    )


def test_the_night_header_names_the_dayObs_and_its_window(app: Any, corpus: StagedCorpus) -> None:
    openNight(app, corpus)
    expect(app.page.locator("#night-dayobs-display")).to_contain_text(str(DAY_OBS))
    # A dayObs is noon UTC to noon UTC, which is the thing people get
    # wrong when reading these views.
    expect(app.page.locator("#night-window-display")).to_contain_text("12:00")


def test_the_top_stats_report_what_is_in_the_logs(app: Any, corpus: StagedCorpus) -> None:
    openNight(app, corpus)
    assert statValue(app, "exposures seen") > 20, "the corpus window holds ~35 visits"
    assert statValue(app, "pods in fetch") >= 5
    assert statValue(app, "tracebacks") > 100, "this night's AOS workers fail a lot"
    assert statValue(app, "distinct exception classes") > 1
    assert statValue(app, "pod restarts") >= 1


def test_errors_are_broken_down_by_type_and_by_pod(app: Any, corpus: StagedCorpus) -> None:
    openNight(app, corpus)
    types = app.page.locator("#night-errors-by-type tbody tr")
    assert types.count() > 1, "the night has several distinct exception classes"
    # Sorted by count, so the first row is the dominant failure mode.
    first = types.first.inner_text()
    assert any(word in first for word in ("Error", "Exception")), first
    assert app.page.locator("#night-errors-by-pod tbody tr").count() >= 1


def test_the_histograms_draw_bars(app: Any, corpus: StagedCorpus) -> None:
    openNight(app, corpus)
    for hist in ("#night-hist-first", "#night-hist-cz"):
        bars = app.page.locator(f"{hist} rect.night-hist-bar")
        assert bars.count() > 0, f"{hist} rendered no bars"
    expect(app.page.locator("#night-hist-first-title")).to_contain_text("First task pickup")
    expect(app.page.locator("#night-hist-first-meta")).not_to_be_empty()


def test_clicking_a_histogram_bin_lists_the_visits_in_it(app: Any, corpus: StagedCorpus) -> None:
    """The histogram is a way in, not just a picture: a slow bin should
    hand you the dataIds that made it slow."""
    openNight(app, corpus)
    panel = app.page.locator("#night-hist-first-bin")
    expect(panel).to_be_empty()
    # The tallest bar, so the panel has several ids in it.
    tallest = app.page.evaluate("""() => {
             const bars = [...document.querySelectorAll('#night-hist-first rect.night-hist-bar')];
             bars.sort((a, b) => Number(b.getAttribute('height')) - Number(a.getAttribute('height')));
             return bars[0].getAttribute('data-bin-index');
           }""")
    app.page.locator(f"#night-hist-first .night-hist-bar-hit[data-bin-index='{tallest}']").click()
    expect(panel).not_to_be_empty()
    ids = panel.locator(".night-hist-bin-id")
    assert ids.count() >= 1
    # The ids have to be *this bin's*. Asserting only that the panel
    # filled would pass just as well if the click served some other
    # bin's dataIds, which is the one thing this feature must not do:
    # the whole point is "these are the visits that made this bar tall".
    expected = app.apiJson(f"/api/summary?dayObs={DAY_OBS}")["histograms"]["firstTaskStart"]["dataIdsByBin"][
        int(tallest)
    ]
    assert [t.strip() for t in ids.all_inner_texts()] == [str(i) for i in expected]
    href = ids.first.get_attribute("href")
    assert href is not None and "dataId=" in href
    # And pinned: this link lands on a fresh home page, which otherwise
    # takes its instrument from whatever this browser last looked at.
    assert "instrument=lsstcam" in href
    expect(
        app.page.locator(f"#night-hist-first rect.night-hist-bar[data-bin-index='{tallest}']")
    ).to_have_class(re.compile(r"\bselected\b"))
    app.page.locator("#night-hist-bin-close").click()
    expect(panel).to_be_empty()


def test_failures_are_listed_and_drill_down_to_their_traceback(app: Any, corpus: StagedCorpus) -> None:
    """Expanding a row goes back to the server for the pod's log around
    the failure — the drilldown is the reason this table is useful."""
    openNight(app, corpus)
    rows = app.page.locator("#night-failures .night-failure-row")
    assert rows.count() > 3, "this night has plenty of failures"
    expect(app.page.locator("#night-failures-count")).not_to_be_empty()
    rows.first.click()
    detail = app.page.locator("#night-failures tr.night-failure-detail")
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("Traceback")
    # Clicking again folds it away rather than stacking a second copy.
    rows.first.click()
    expect(detail).to_have_count(0)


def test_pod_restarts_are_surfaced(app: Any, corpus: StagedCorpus) -> None:
    """A pod that died mid-visit explains a gap that no traceback
    accounts for; the markers come from the k8s/events stream."""
    openNight(app, corpus)
    rows = app.page.locator("#night-restarts tbody tr")
    assert rows.count() >= 1
    expect(rows.first).to_contain_text("aos")


def test_the_gather_only_banner_flags_visits_whose_step1a_is_missing(app: Any, corpus: StagedCorpus) -> None:
    """Gather (step1b) aggregates step1a's output, so a visit with gather
    activity and no step1a is physically impossible — it means the fetch
    didn't get the step1a logs, and every Δshutter number derived from
    that visit is wrong.

    The corpus is a cut of a real night, so the first couple of visits in
    it genuinely have their step1a on the far side of the cut. That makes
    this the honest test of the banner: the condition is real, not
    injected.
    """
    openNight(app, corpus)
    banner = app.page.locator("#night-gather-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("step1a")
    links = banner.locator("a[href*='dataId=']")
    assert links.count() >= 1
    href = links.first.get_attribute("href")
    assert href is not None and "autoFetch=1" in href, href
    assert "instrument=lsstcam" in href, href


def test_a_complete_night_shows_no_incomplete_fetch_banner(app: Any, corpus: StagedCorpus) -> None:
    """The window's own fetch was complete; only the gather-only warning
    above applies, and the two are separate signals."""
    openNight(app, corpus)
    expect(app.page.locator("#night-fetch-banner")).to_be_hidden()


def test_the_night_fetch_banner_names_pods_that_fell_short(app: Any, corpus: StagedCorpus) -> None:
    """A night missing a pod silently biases every histogram on the page,
    which is why it gets a banner rather than a quiet flag."""
    import json

    cacheDir = corpus.stageNight()
    metaPath = cacheDir / "_meta.json"
    meta = json.loads(metaPath.read_text())
    meta["incomplete_pods"] = {"s-lsstcam-run-aos-worker-aosworkerset-2": "chunk floor"}
    meta["fetchComplete"] = False
    metaPath.write_text(json.dumps(meta))

    app.goto(f"/?dayObs={DAY_OBS}")
    banner = app.page.locator("#night-fetch-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("aosworkerset-2")


def test_a_crash_loop_is_spelled_out_in_the_restarts_table(app: Any, corpus: StagedCorpus) -> None:
    """A pod that dies mid-visit explains a gap no traceback accounts
    for, so the night view has to say which pod, how many times, and —
    when k8s knows — why. This is a real crash loop: five in-place
    restarts, then an image pull it never recovered from."""
    pod = corpus.plantPodCrash()
    openNight(app, corpus)
    rows = app.page.locator("#night-restarts tbody tr")
    text = rows.all_inner_texts()
    joined = " ".join(text)
    assert len(text) >= 10, f"expected the whole crash loop, got {len(text)} rows"
    assert pod.split("-run-")[1] in joined
    # The escalation, in the words k8s used.
    assert "restart #6" in joined
    assert "ImagePullBackOff" in joined
    # And the stat tile counts them rather than leaving them to the table.
    assert statValue(app, "pod restarts") >= 5


def test_the_crash_is_attributed_to_what_the_pod_was_working_on(app: Any, corpus: StagedCorpus) -> None:
    """A restart is only actionable next to the visit it interrupted."""
    corpus.plantPodCrash()
    openNight(app, corpus)
    links = app.page.locator("#night-restarts tbody a[href*='dataId=']")
    assert links.count() >= 1, "no restart was tied to a dataId"
    href = links.first.get_attribute("href")
    assert href is not None and "dataId=20260711" in href
    assert "instrument=lsstcam" in href, href
