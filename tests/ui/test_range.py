"""Range mode: one wide fetch, and a navigator to step through it.

The point of the mode is that consecutive exposures overlap heavily, so
fetching them one at a time downloads the same logs N times. One window
is fetched and each exposure's timeline is computed from it, anchored at
that exposure's own shutter close — which is the thing to check: moving
between exposures has to re-anchor, not just re-label.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import expect

from .corpus import RANGE_START, RANGE_STOP, StagedCorpus


def openRange(app: Any, corpus: StagedCorpus) -> None:
    corpus.stageRange(RANGE_START, RANGE_STOP)
    app.goto(f"/?rangeStart={RANGE_START}&rangeStop={RANGE_STOP}")
    expect(app.page.locator("#range-nav")).to_be_visible()
    expect(app.page.locator("#range-chips .range-chip").first).to_be_visible()


def test_the_navigator_shows_a_chip_per_resolved_exposure(app: Any, corpus: StagedCorpus) -> None:
    openRange(app, corpus)
    chips = app.page.locator("#range-chips .range-chip")
    assert chips.count() == RANGE_STOP - RANGE_START + 1
    expect(app.page.locator("#range-nav-label")).to_contain_text(str(RANGE_START))
    expect(chips.first).to_have_class(re.compile(r"\bselected\b"))


def test_stepping_moves_the_selection_and_re_anchors_the_timeline(app: Any, corpus: StagedCorpus) -> None:
    """Each exposure's timeline is computed from the same shared window
    with its *own* shutter close as zero, so stepping has to change what
    the reference says as well as which chip is lit."""
    openRange(app, corpus)
    chips = app.page.locator("#range-chips .range-chip")
    # The chips render before the timeline fills #t0-info, and the text
    # is read as a snapshot: sampled too early it is '', which every
    # later reference "differs" from.
    expect(app.page.locator("#t0-info")).to_contain_text("shutter close")
    firstT0 = app.page.locator("#t0-info").inner_text()

    app.page.locator("#range-next").click()
    expect(chips.nth(1)).to_have_class(re.compile(r"\bselected\b"))
    expect(app.page.locator("#t0-info")).not_to_have_text(firstT0)
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START + 1))

    app.page.locator("#range-prev").click()
    expect(chips.nth(0)).to_have_class(re.compile(r"\bselected\b"))
    expect(app.page.locator("#t0-info")).to_have_text(firstT0)


def test_arrow_keys_step_between_exposures(app: Any, corpus: StagedCorpus) -> None:
    """Flicking through a run is the whole workflow; reaching for the
    mouse each time would make it tedious."""
    openRange(app, corpus)
    app.page.keyboard.press("ArrowRight")
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START + 1))
    app.page.keyboard.press("ArrowLeft")
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START))


def test_clicking_a_chip_jumps_straight_to_that_exposure(app: Any, corpus: StagedCorpus) -> None:
    openRange(app, corpus)
    app.page.locator("#range-chips .range-chip").last.click()
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_STOP))
    expect(app.page.locator("#timeline .tl-row").first).to_be_visible()


def test_a_chip_picked_after_the_range_was_unloaded_opens_that_exposure(
    app: Any, corpus: StagedCorpus
) -> None:
    """The server can unload a range while its tab stays open: a ninth
    range, the loaded-views memory cap, a restart. Picking a chip then
    lit the chip and left the previous exposure's timeline under it. The
    page now follows the rebuild from cache and opens the range on the
    exposure that was picked."""
    openRange(app, corpus)
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START))
    with app.ctx.jobs.stateLock:
        app.ctx.rangeStates.clear()
    app.page.locator("#range-chips .range-chip").last.click()
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_STOP), timeout=30_000)
    expect(app.page.locator("#range-nav")).to_be_visible()
    expect(app.page.locator("#range-chips .range-chip").last).to_have_class(re.compile(r"\bselected\b"))
    assert f"dataId={RANGE_STOP}" in app.page.url
    assert len(app.ctx.rangeStates) == 1


def test_a_chip_whose_request_fails_gives_the_selection_back(app: Any, corpus: StagedCorpus) -> None:
    """With no answer at all, the timeline is still the previous
    exposure's, so the lit chip has to be that one too."""
    openRange(app, corpus)
    chips = app.page.locator("#range-chips .range-chip")
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START))
    app.page.route("**/api/summary?*", lambda route: route.abort())
    app.page.locator("#range-next").click()
    expect(chips.nth(0)).to_have_class(re.compile(r"\bselected\b"))
    expect(chips.nth(1)).not_to_have_class(re.compile(r"\bselected\b"))
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START))


@pytest.mark.parametrize("late", ["answered", "failed"])
def test_an_answer_to_a_superseded_step_changes_nothing(app: Any, corpus: StagedCorpus, late: str) -> None:
    """Stepping can outrun the server. The answer to a step already
    stepped past used to render over the newer exposure, under the newer
    exposure's chip and URL; a late failure put the lit chip back on an
    exposure that was never on screen. Only the latest request counts."""
    openRange(app, corpus)
    chips = app.page.locator("#range-chips .range-chip")
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START))
    # Held, not slept on: the first step's request stays unanswered while
    # the second step is asked and answered.
    held: list[Any] = []
    app.page.route(
        re.compile(rf"/api/summary\?.*dataId={RANGE_START + 1}\b"), lambda route: held.append(route)
    )
    app.page.locator("#range-next").click()
    expect(chips.nth(1)).to_have_class(re.compile(r"\bselected\b"))
    app.page.locator("#range-next").click()
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START + 2))
    assert len(held) == 1

    if late == "answered":
        with app.page.expect_response(re.compile(rf"dataId={RANGE_START + 1}\b")):
            held[0].continue_()
    else:
        with app.page.expect_event("requestfailed"):
            held[0].abort()
    # Let the page act on it, as far as it is going to.
    app.page.evaluate("() => new Promise((resolve) => setTimeout(resolve, 100))")
    expect(app.page.locator("#expId-display")).to_contain_text(str(RANGE_START + 2))
    expect(chips.nth(2)).to_have_class(re.compile(r"\bselected\b"))
    expect(chips.nth(1)).not_to_have_class(re.compile(r"\bselected\b"))
    assert f"dataId={RANGE_START + 2}" in app.page.url


def test_a_chip_flags_an_exposure_that_failed(app: Any, corpus: StagedCorpus) -> None:
    """The navigator doubles as a map of where the trouble is."""
    openRange(app, corpus)
    chips = app.page.locator("#range-chips .range-chip")
    titles = [chips.nth(i).get_attribute("title") or "" for i in range(chips.count())]
    assert any("traceback" in t.lower() for t in titles), titles
