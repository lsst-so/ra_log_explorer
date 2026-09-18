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


def test_a_chip_flags_an_exposure_that_failed(app: Any, corpus: StagedCorpus) -> None:
    """The navigator doubles as a map of where the trouble is."""
    openRange(app, corpus)
    chips = app.page.locator("#range-chips .range-chip")
    titles = [chips.nth(i).get_attribute("title") or "" for i in range(chips.count())]
    assert any("traceback" in t.lower() for t in titles), titles
