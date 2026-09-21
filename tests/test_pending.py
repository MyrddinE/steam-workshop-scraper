"""What the list marker says: which stage an item is waiting on.

The marker is not a binary "pending". Its speed names the stage and its colour
fades with the speed, so a marker that clears in seconds looks different from
one that may take hours. Both front ends draw that from one mapping
(`src/pending.py`), because the two could otherwise disagree about why the same
item looks busy -- and the mechanisms behind them are not remotely alike (a
braille glyph on a Textual timer, a CSS-animated ring).

Two decisions are pinned here because both are easy to "fix" back:

* **Fastest stage wins.** An item owing several stages shows the one that is
  about to clear. The slow, faded marker is the one that must not nag, and it
  will still be there afterwards.
* **The API is not a stage.** A list only shows items the API has already
  returned, so a pending refresh is not content anyone is waiting on.
"""

from pathlib import Path

import pytest

from src import pending

TEMPLATE = Path("templates/index.html")


def _item(**over) -> dict:
    item = {
        "image_priority": 0,
        "image_answer": "jpg",
        "translation_priority": 0,
        "web_scrape_priority": 0,
        "api_priority": 0,
    }
    item.update(over)
    return item


# --- which stage -----------------------------------------------------------

def test_a_settled_item_shows_nothing():
    assert pending.pending_stage(_item()) is None


@pytest.mark.parametrize("stage,over", [
    ("image", {"image_priority": 5, "image_answer": None}),
    ("translation", {"translation_priority": 5}),
    ("web", {"web_scrape_priority": 5}),
])
def test_each_stage_is_recognised(stage, over):
    assert pending.pending_stage(_item(**over)) == stage


@pytest.mark.parametrize("level", [1, 3, 4])
def test_below_the_threshold_is_not_pending(level):
    """The levels below 5 are background work, not something to draw."""
    assert pending.pending_stage(_item(web_scrape_priority=level)) is None
    assert pending.pending_stage(_item(translation_priority=level)) is None
    assert pending.pending_stage(_item(image_priority=level, image_answer=None)) is None


def test_an_image_that_is_settled_is_not_pending():
    """A recorded answer is settled even though no picture exists.

    `image_answer` may hold the 404 that said there is none; that is an
    answer, and the item is not waiting on anything.
    """
    assert pending.pending_stage(_item(image_priority=10, image_answer="404")) is None
    assert pending.pending_stage(_item(image_priority=10, image_answer="jpg")) is None


def test_a_pending_image_with_a_real_file_is_not_pending():
    """A re-flagged image that is already on disk has nothing to wait for."""
    assert pending.pending_stage(_item(image_priority=10, image_answer="png")) is None


# --- the precedence --------------------------------------------------------

def test_the_fastest_pending_stage_wins():
    """All three at once shows the image: it is the one about to clear."""
    everything = _item(image_priority=5, image_answer=None,
                       translation_priority=5, web_scrape_priority=5)
    assert pending.pending_stage(everything) == "image"


def test_translation_beats_the_web_scrape():
    assert pending.pending_stage(_item(translation_priority=5, web_scrape_priority=5)) == "translation"


def test_a_settled_image_lets_the_next_stage_through():
    """The precedence must be over *pending* stages, not merely present ones."""
    item = _item(image_priority=5, image_answer="404", translation_priority=5, web_scrape_priority=5)
    assert pending.pending_stage(item) == "translation"
    item = _item(image_priority=5, image_answer="404", web_scrape_priority=5)
    assert pending.pending_stage(item) == "web"


# --- the API is not a stage ------------------------------------------------

@pytest.mark.parametrize("level", [5, 10])
def test_a_pending_api_refresh_draws_nothing(level):
    """Every list item has already been pulled by the API, so a refresh is not
    something the reader is waiting on for content."""
    assert pending.pending_stage(_item(api_priority=level)) is None


def test_the_api_clause_is_gone_from_the_page_too():
    js = _js_function("_pendingStage")
    assert "api_priority" not in js, "the API is not a stage on either side"


# --- the appearance --------------------------------------------------------

def test_speed_and_colour_are_fastest_brightest_first():
    assert pending.STAGE_NAMES == ("image", "translation", "web")
    assert pending.rotation_seconds("image") == pytest.approx(0.6)
    assert pending.rotation_seconds("translation") == pytest.approx(2.4)
    assert pending.rotation_seconds("web") == pytest.approx(9.6)


def test_each_stage_gets_slower_and_greyer():
    """The slowest marker must also be the quietest, or it nags for hours."""
    def greyness(hex_colour):
        # A crude vividness proxy: the spread between the brightest and
        # dimmest channel. Green has a wide spread, grey almost none.
        r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
        return max(r, g, b) - min(r, g, b)

    spreads = [greyness(pending.stage_spec(s)[1]) for s in pending.STAGE_NAMES]
    assert spreads == sorted(spreads, reverse=True), \
        "colour must fade from vivid to grey as the stage gets slower"


def test_an_unknown_stage_is_a_loud_error():
    with pytest.raises(ValueError):
        pending.stage_spec("nonsense")
    with pytest.raises(ValueError):
        pending._is_pending("nonsense", _item())


# --- the wording -----------------------------------------------------------

def test_every_stage_has_a_wording():
    """The marker's meaning must be recoverable without knowing the colours."""
    for stage in pending.STAGE_NAMES:
        assert pending.stage_label(stage).strip(), f"{stage} needs a wording"


def test_an_unknown_stage_has_no_wording():
    with pytest.raises(ValueError):
        pending.stage_label("nonsense")


# --- the two front ends must agree -----------------------------------------

def _css_rule(selector: str) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    marker = selector + " {"
    assert marker in html, f"missing the CSS rule for {selector}"
    return html[html.index(marker):html.index("}", html.index(marker))]


def _js_function(name: str) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    start = html.index(f"function {name}(")
    return html[start:html.index("\n}", start)]


@pytest.mark.parametrize("stage", pending.STAGE_NAMES)
def test_the_template_mirrors_the_mapping(stage):
    """Duration and colour, per stage, taken from the shared table.

    Without this the page and the TUI drift, and the same item looks pending for
    different reasons in each -- which is the one thing the shared table exists
    to prevent.
    """
    rule = _css_rule(f".grid-cell.pending-{stage} .grid-spinner")
    _multiplier, colour = pending.stage_spec(stage)
    seconds = pending.rotation_seconds(stage)
    assert f"animation-duration: {seconds:g}s" in rule, \
        f"{stage} must rotate every {seconds:g}s, matching src/pending.py"
    assert colour in rule, f"{stage} must use {colour}, matching src/pending.py"


def test_the_page_tests_the_stages_in_the_same_order():
    """Fastest first, so a marker about to clear is not hidden by a slower one."""
    js = _js_function("_pendingStage")
    positions = [js.index(f"'{stage}'") for stage in pending.STAGE_NAMES]
    assert positions == sorted(positions), \
        f"the page must test {pending.STAGE_NAMES} in order"


def test_the_page_clears_every_stage_class_before_setting_one():
    """Otherwise an item that moves from web to image keeps both classes."""
    js = _js_function("_applyPending")
    assert "classList.toggle" in js
    for stage in pending.STAGE_NAMES:
        assert f"'pending-' + name" in js or f"pending-{stage}" in js
    assert "has-spinner', stage !== null" in js


@pytest.mark.parametrize("stage", pending.STAGE_NAMES)
def test_the_template_mirrors_the_wording(stage):
    """The wording, per stage, taken from the shared table.

    The web marker is the only one with a hover, but the wording still lives in
    `src/pending.py` beside the speed and colour: the TUI draws the same state,
    and a table kept only in the page would let the two describe it differently.
    """
    assert pending.stage_label(stage) in _js_function("_pendingLabel"), \
        f"{stage} must be worded {pending.stage_label(stage)!r}, matching src/pending.py"


def test_the_marker_carries_the_wording_as_its_title():
    """The stage the marker just set is what the tooltip must name."""
    js = _js_function("_applyPending")
    assert "grid-spinner" in js, "the wording goes on the marker element"
    assert "title" in js
    assert "_pendingLabel(stage)" in js, \
        "the label must come from the stage _pendingStage just returned"


def test_the_translation_notice_wording_is_shared_not_retyped():
    """The TUI and the page print one sentence, defined once, here.

    The notice is a detail-pane element on both sides; a retyped copy in the
    template is exactly the drift the shared table exists to prevent.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    assert pending.TRANSLATION_REQUESTED_NOTICE.strip()
    assert "currently in queue" not in html, \
        "the page must not retype the notice; take it from src/pending.py"
    assert "TRANSLATION_NOTICE" in html, "the page must use the shared wording"
