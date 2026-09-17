"""The owner's subscription state: which of the four a row is in, and why.

There are four answers to "does this account subscribe to this item?" --
subscribed, pending, previously, never -- and both front ends draw them with
real Unicode glyphs from one table in `src/subscription.py`. The TUI renders
that table into Textual markup and the web renders it into a positioned element,
so the two mechanisms are not remotely alike; the shared table is what stops
them disagreeing about why the same row looks the way it does.

The decisions pinned here are the ones easy to "fix" back:

* **Precedence.** ``subscribed`` beats ``pending`` (there is nothing left to
  queue), ``pending`` beats ``previously``, and ``previously`` beats ``never``.
* **The sticky timestamp is the only source of ``previously``.** A row whose
  ``own_subscribed`` has been cleared still reads ``previously`` because we saw
  it once, and a stale timestamp beside a set ``own_subscribed`` reads
  ``subscribed``.
* **``previously`` is a claim about observation, not history.** Steam exposes no
  per-account subscription history, so the tooltip must say "we have seen you
  subscribed" rather than implying a complete record.
"""

from pathlib import Path

import pytest

from src import subscription

TEMPLATE = Path("templates/index.html")
TUI_SOURCE = Path("src/tui.py")


def _item(**over) -> dict:
    item = {
        "workshop_id": 1,
        "own_subscribed": 0,
        "own_first_subscribed_at": None,
        "is_queued_for_subscription": 0,
    }
    item.update(over)
    return item


# --- the four states --------------------------------------------------------

def test_a_never_seen_item_is_never():
    assert subscription.subscription_state(_item()) == subscription.NEVER


def test_a_current_subscription_is_subscribed():
    assert subscription.subscription_state(
        _item(own_subscribed=1, own_first_subscribed_at=1000)) == subscription.SUBSCRIBED


def test_a_queued_item_is_pending():
    assert subscription.subscription_state(
        _item(is_queued_for_subscription=1)) == subscription.PENDING


def test_a_seen_but_unsubscribed_item_is_previously():
    """The sticky timestamp is the whole of the `previously` state."""
    assert subscription.subscription_state(
        _item(own_subscribed=0, own_first_subscribed_at=1000)) == subscription.PREVIOUSLY


# --- the precedence ---------------------------------------------------------

def test_a_subscribed_item_with_a_stale_queue_flag_is_subscribed():
    """There is nothing pending for an item that is already subscribed."""
    assert subscription.subscription_state(
        _item(own_subscribed=1, own_first_subscribed_at=1000,
              is_queued_for_subscription=1)) == subscription.SUBSCRIBED


def test_a_previously_seen_item_that_is_queued_is_pending():
    assert subscription.subscription_state(
        _item(own_subscribed=0, own_first_subscribed_at=1000,
              is_queued_for_subscription=1)) == subscription.PENDING


def test_a_stale_timestamp_beside_a_set_flag_is_subscribed():
    """The flag is the present tense; the timestamp is only history."""
    assert subscription.subscription_state(
        _item(own_subscribed=1, own_first_subscribed_at=None)) == subscription.SUBSCRIBED


def test_the_precedence_order_is_declared_strongest_first():
    assert subscription.PRECEDENCE == (
        subscription.SUBSCRIBED, subscription.PENDING,
        subscription.PREVIOUSLY, subscription.NEVER)


def test_queued_beats_previously_and_subscribed_beats_pending():
    """Each adjacent pair of the stated precedence, in one place."""
    assert (subscription.subscription_state(_item(own_subscribed=1, is_queued_for_subscription=1))
            == subscription.SUBSCRIBED)
    assert (subscription.subscription_state(_item(is_queued_for_subscription=1, own_first_subscribed_at=9))
            == subscription.PENDING)
    assert (subscription.subscription_state(_item(own_first_subscribed_at=9))
            == subscription.PREVIOUSLY)


# --- the appearance table ---------------------------------------------------

def test_every_state_has_a_spec_and_a_tooltip():
    assert set(subscription.SPECS) == set(subscription.PRECEDENCE)
    assert set(subscription.TOOLTIPS) == set(subscription.PRECEDENCE)


def test_the_glyphs_are_the_real_unicode_characters():
    assert subscription.glyph(subscription.SUBSCRIBED) == "\u2605"   # ★
    assert subscription.glyph(subscription.PENDING) == "\u2606"      # ☆
    assert subscription.glyph(subscription.PREVIOUSLY) == "\u2606"   # ☆
    assert subscription.glyph(subscription.NEVER) == "\u25cb"        # ○


def test_the_colours_follow_the_stated_table():
    assert subscription.colour(subscription.SUBSCRIBED) == "#ffd700"  # solid yellow
    assert subscription.colour(subscription.PENDING) == "#2ecc40"     # green
    assert subscription.colour(subscription.PREVIOUSLY) == "#ffd700"  # yellow
    assert subscription.colour(subscription.NEVER) == "#808080"       # gray


def test_pending_and_previously_share_a_glyph_but_not_a_colour():
    """☆ in green and ☆ in yellow are different states, and only colour says so."""
    assert (subscription.glyph(subscription.PENDING)
            == subscription.glyph(subscription.PREVIOUSLY))
    assert (subscription.colour(subscription.PENDING)
            != subscription.colour(subscription.PREVIOUSLY))


def test_the_spec_and_the_named_getters_agree():
    for state in subscription.PRECEDENCE:
        glyph, colour, css, label = subscription.spec(state)
        assert (glyph, colour, css, label) == (
            subscription.glyph(state), subscription.colour(state),
            subscription.css_class(state), subscription.label(state))
        assert css and label


def test_only_the_three_actionable_states_are_clickable():
    assert subscription.is_clickable(subscription.PENDING)
    assert subscription.is_clickable(subscription.PREVIOUSLY)
    assert subscription.is_clickable(subscription.NEVER)
    # An accidental unsubscribe is not wanted, so subscribed does nothing.
    assert not subscription.is_clickable(subscription.SUBSCRIBED)
    assert set(subscription.CLICKABLE) == {
        subscription.PENDING, subscription.PREVIOUSLY, subscription.NEVER}


def test_every_css_class_is_distinct():
    assert len({subscription.css_class(s) for s in subscription.PRECEDENCE}) == 4


def test_an_unknown_state_is_a_loud_error():
    with pytest.raises(ValueError):
        subscription.spec("nonsense")
    with pytest.raises(ValueError):
        subscription.glyph("nonsense")
    with pytest.raises(ValueError):
        subscription.tooltip("nonsense")
    with pytest.raises(ValueError):
        subscription.is_clickable("nonsense")


# --- the honesty constraint -------------------------------------------------

def test_the_previously_tooltip_admits_the_history_is_partial():
    """`previously` cannot mean complete history, so the tooltip must not imply it.

    Steam exposes no per-account subscription history: lifetime_subscriptions is
    an item-wide count and EnumerateUserSubscribedFiles is publisher-key-only. On
    the day this shipped there were zero `previously` markers, so the wording has
    to say what was observed rather than what happened.
    """
    text = subscription.tooltip(subscription.PREVIOUSLY).lower()
    assert "no subscription history" in text
    assert "we have seen you subscribed" in text
    assert "not a complete" in text


def test_the_never_tooltip_is_also_honest():
    text = subscription.tooltip(subscription.NEVER).lower()
    assert "no subscription history" in text
    assert "not proof" in text


# --- the two front ends must agree -----------------------------------------

def _function_body(source: str, name: str) -> str:
    """The body of a top-level JS function, for scoping a source assertion."""
    start = source.index(f"function {name}(")
    return source[start:source.index("\n}", start)]


def test_the_template_renders_the_glyphs_and_colours_from_the_shared_table():
    """The page must not spell the four states itself.

    Its glyph, colour, tooltip and clickability all arrive on the payload,
    computed by the server from `src/subscription.py`. There is no glyph or hex
    colour literal for this marker anywhere in the page's own source, so a
    re-colouring in the shared table moves both front ends at once.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    # Scoped to the marker's own code rather than the whole page: colour literals
    # exist elsewhere (the Wilson percentile colours), and the claim here is
    # about the subscription marker.
    for name in ("showSubscriptionMarker", "_applySub", "onSubMarkerClick"):
        body = _function_body(html, name)
        for state in subscription.PRECEDENCE:
            _glyph, colour, _css, _label = subscription.spec(state)
            assert colour not in body, \
                f"{name} must take {state}'s colour from the payload"
        for glyph in ("\u2605", "\u2606", "\u25cb"):
            assert glyph not in body, \
                f"{name} must take the glyph from the payload, not a literal"
    # The marker element itself, and the one place that writes it.
    assert 'class="grid-sub"' in html
    assert "_applySub" in html


def test_the_tui_draws_the_same_glyphs_and_colours():
    """The TUI's markers come from the table, not from literals."""
    source = TUI_SOURCE.read_text(encoding="utf-8")
    assert "subscription.subscription_state(" in source
    assert "subscription.spec(" in source
    # The old leading-`*` queue prefix must be gone: one indicator, not two.
    assert '"*"' not in source
    assert "[green]*[/green]" not in source
