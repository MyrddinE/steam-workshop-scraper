"""The owner's subscription state: which of the six a row is in, and why.

There are six answers to "does this account subscribe to this item?" --
queued_remove, downloaded, subscribed, queued, previously, never -- and both
front ends draw them with real Unicode glyphs from one table in
`src/subscription.py`. The TUI renders that table into Textual markup and the
web renders it into a positioned element, so the two mechanisms are not remotely
alike; the shared table is what stops them disagreeing about why the same row
looks the way it does.

The decisions pinned here are the ones easy to "fix" back:

* **The queue's direction is derived.** The flag carries no direction: with it
  set, ``own_subscribed`` decides -- set means a removal is queued, clear means
  an addition. A queued + subscribed row is therefore ``queued_remove``, not the
  ``subscribed`` it used to be.
* **Precedence.** ``queued_remove`` beats ``downloaded``, which beats
  ``subscribed``, which beats ``queued``, which beats ``previously``, which beats
  ``never``.
* **``downloaded`` needs both flags.** A stray ``steam_download_seen_at`` beside a
  cleared ``own_subscribed`` must not claim the green star.
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
        "steam_download_seen_at": None,
    }
    item.update(over)
    return item


# --- the six states ---------------------------------------------------------

def test_a_never_seen_item_is_never():
    assert subscription.subscription_state(_item()) == subscription.NEVER


def test_a_current_subscription_is_subscribed():
    assert subscription.subscription_state(
        _item(own_subscribed=1, own_first_subscribed_at=1000)) == subscription.SUBSCRIBED


def test_a_subscribed_downloaded_item_is_downloaded():
    assert subscription.subscription_state(
        _item(own_subscribed=1, steam_download_seen_at=1000)) == subscription.DOWNLOADED


def test_a_queued_item_draws_queued():
    assert subscription.subscription_state(
        _item(is_queued_for_subscription=1)) == subscription.QUEUED


def test_a_queued_subscribed_item_draws_a_removal():
    """The derived direction: queued + subscribed is a queued removal."""
    assert subscription.subscription_state(
        _item(own_subscribed=1, is_queued_for_subscription=1)) == subscription.QUEUED_REMOVE


def test_a_seen_but_unsubscribed_item_is_previously():
    """The sticky timestamp is the whole of the `previously` state."""
    assert subscription.subscription_state(
        _item(own_subscribed=0, own_first_subscribed_at=1000)) == subscription.PREVIOUSLY


# --- the precedence ---------------------------------------------------------

def test_a_downloaded_item_beats_a_bare_subscription():
    assert subscription.subscription_state(
        _item(own_subscribed=1, steam_download_seen_at=1000)) == subscription.DOWNLOADED


def test_a_stray_downloaded_timestamp_without_the_flag_is_not_downloaded():
    """The latch is a green claim only beside a live subscription.

    ``steam_download_seen_at`` is cleared with ``own_subscribed`` by the subscription
    walk, but a timestamp that survived some other path -- an old row, a manual
    edit -- must not draw the green star on its own.
    """
    assert subscription.subscription_state(_item(steam_download_seen_at=1000)) == subscription.NEVER
    assert subscription.subscription_state(
        _item(steam_download_seen_at=1000, own_first_subscribed_at=5)) == subscription.PREVIOUSLY
    assert subscription.subscription_state(
        _item(steam_download_seen_at=1000, is_queued_for_subscription=1)) == subscription.QUEUED


def test_a_subscribed_item_with_a_queue_flag_is_a_queued_removal():
    """The flag beside a live subscription is the derived removal direction."""
    assert subscription.subscription_state(
        _item(own_subscribed=1, own_first_subscribed_at=1000,
              is_queued_for_subscription=1)) == subscription.QUEUED_REMOVE


def test_the_queue_flag_beats_a_seen_timestamp():
    assert subscription.subscription_state(
        _item(own_subscribed=0, own_first_subscribed_at=1000,
              is_queued_for_subscription=1)) == subscription.QUEUED


def test_a_stale_timestamp_beside_a_set_flag_is_subscribed():
    """The flag is the present tense; the timestamp is only history."""
    assert subscription.subscription_state(
        _item(own_subscribed=1, own_first_subscribed_at=None)) == subscription.SUBSCRIBED


def test_the_precedence_order_is_declared_strongest_first():
    assert subscription.STATE_PRECEDENCE == (
        subscription.QUEUED_REMOVE, subscription.DOWNLOADED, subscription.SUBSCRIBED,
        subscription.QUEUED, subscription.PREVIOUSLY, subscription.NEVER)


def test_each_adjacent_pair_of_the_stated_precedence():
    """Each adjacent pair of the stated precedence, in one place."""
    assert (subscription.subscription_state(
        _item(own_subscribed=1, is_queued_for_subscription=1,
              steam_download_seen_at=1)) == subscription.QUEUED_REMOVE)
    assert (subscription.subscription_state(_item(own_subscribed=1, steam_download_seen_at=1))
            == subscription.DOWNLOADED)
    assert (subscription.subscription_state(
        _item(own_subscribed=1, is_queued_for_subscription=0)) == subscription.SUBSCRIBED)
    assert (subscription.subscription_state(_item(is_queued_for_subscription=1, own_first_subscribed_at=9))
            == subscription.QUEUED)
    assert (subscription.subscription_state(_item(own_first_subscribed_at=9))
            == subscription.PREVIOUSLY)


# --- the appearance table ---------------------------------------------------

def test_every_state_has_a_marker_spec_and_a_tooltip():
    assert set(subscription.MARKER_SPECS) == set(subscription.STATE_PRECEDENCE)
    assert set(subscription.MARKER_TOOLTIPS) == set(subscription.STATE_PRECEDENCE)


def test_the_glyphs_are_the_real_unicode_characters():
    assert subscription.glyph(subscription.QUEUED_REMOVE) == "\u2606"  # ☆
    assert subscription.glyph(subscription.DOWNLOADED) == "\u2605"  # ★
    assert subscription.glyph(subscription.SUBSCRIBED) == "\u2605"  # ★
    assert subscription.glyph(subscription.QUEUED) == "\u2606"     # ☆
    assert subscription.glyph(subscription.PREVIOUSLY) == "\u2606"  # ☆
    assert subscription.glyph(subscription.NEVER) == "\u25cb"       # ○


def test_the_colours_follow_the_stated_table():
    assert subscription.colour(subscription.QUEUED_REMOVE) == "#e74c3c"  # red
    assert subscription.colour(subscription.DOWNLOADED) == "#00a651"  # deep green
    assert subscription.colour(subscription.SUBSCRIBED) == "#ffd700"  # solid yellow
    assert subscription.colour(subscription.QUEUED) == "#2ecc40"     # green
    assert subscription.colour(subscription.PREVIOUSLY) == "#ffd700"  # yellow
    assert subscription.colour(subscription.NEVER) == "#808080"       # gray


def test_the_queued_removal_is_the_owners_empty_red_star_outline():
    """The owner's marker for a queued removal: QUEUED's glyph, red."""
    glyph, colour, css, label = subscription.marker_spec(subscription.QUEUED_REMOVE)
    assert glyph == subscription.glyph(subscription.QUEUED), "the same empty star"
    assert glyph == "\u2606"
    assert colour == "#e74c3c"
    assert css == "sub-queued-remove"
    assert label == "About to unsubscribe"
    assert subscription.colour(subscription.QUEUED_REMOVE) \
        != subscription.colour(subscription.QUEUED), "red, not the queued green"


def test_a_queued_removal_is_drawn_over_the_subscription_it_removes():
    """Above downloaded and subscribed, or the pending removal is invisible."""
    assert subscription.STATE_PRECEDENCE.index(subscription.QUEUED_REMOVE) \
        < subscription.STATE_PRECEDENCE.index(subscription.DOWNLOADED)
    assert subscription.STATE_PRECEDENCE.index(subscription.QUEUED_REMOVE) \
        < subscription.STATE_PRECEDENCE.index(subscription.SUBSCRIBED)
    assert subscription.subscription_state(
        _item(own_subscribed=1, is_queued_for_subscription=1,
              steam_download_seen_at=1)) == subscription.QUEUED_REMOVE


def test_downloaded_is_a_solid_star_in_a_green_of_its_own():
    """★ green must not be mistakable for ☆ green or ★ yellow."""
    assert (subscription.glyph(subscription.DOWNLOADED)
            == subscription.glyph(subscription.SUBSCRIBED))
    assert (subscription.colour(subscription.DOWNLOADED)
            != subscription.colour(subscription.QUEUED))
    assert (subscription.colour(subscription.DOWNLOADED)
            != subscription.colour(subscription.SUBSCRIBED))


def test_the_downloaded_label_and_tooltip_say_what_it_means():
    assert subscription.marker_spec(subscription.DOWNLOADED)[3] == "Subscribed, and downloaded"
    assert (subscription.tooltip(subscription.DOWNLOADED)
            == "You are subscribed to this item and Steam has downloaded it.")


def test_queued_and_previously_share_a_glyph_but_not_a_colour():
    """☆ in green and ☆ in yellow are different states, and only colour says so."""
    assert (subscription.glyph(subscription.QUEUED)
            == subscription.glyph(subscription.PREVIOUSLY))
    assert (subscription.colour(subscription.QUEUED)
            != subscription.colour(subscription.PREVIOUSLY))


def test_the_marker_spec_and_the_named_getters_agree():
    for state in subscription.STATE_PRECEDENCE:
        glyph, colour, css, label = subscription.marker_spec(state)
        assert (glyph, colour) == (
            subscription.glyph(state), subscription.colour(state))
        assert css and label


def test_the_actionable_states_are_clickable_and_downloaded_is_not():
    assert subscription.is_clickable(subscription.QUEUED)
    assert subscription.is_clickable(subscription.QUEUED_REMOVE)
    assert subscription.is_clickable(subscription.PREVIOUSLY)
    assert subscription.is_clickable(subscription.NEVER)
    # The owner reversed the old deliberate inertness of `subscribed`: its click
    # only queues a removal, and the queue cancels, so an accidental click is
    # recoverable. `downloaded` is the same subscription seen from disk but its
    # action is the separate open-folder button and key, so it stays inert.
    assert subscription.is_clickable(subscription.SUBSCRIBED)
    assert not subscription.is_clickable(subscription.DOWNLOADED)
    assert set(subscription.CLICKABLE_STATES) == {
        subscription.QUEUED, subscription.QUEUED_REMOVE, subscription.SUBSCRIBED,
        subscription.PREVIOUSLY, subscription.NEVER}


def test_a_queued_removal_can_be_cancelled_by_clicking_its_marker():
    """The click is the cancel path for the new state, so it must be clickable."""
    assert subscription.is_clickable(subscription.QUEUED_REMOVE)
    assert subscription.subscription_state(
        _item(own_subscribed=1, is_queued_for_subscription=1)) == subscription.QUEUED_REMOVE


def test_the_subscribed_marker_is_clickable_but_not_a_direct_unsubscribe():
    """The old reasoning was "an accidental unsubscribe is not wanted".

    The reversal is safe because the click only queues a removal; nothing is
    sent to Steam until the queue is drained. `subscribed` (no queue entry) is
    clickable, and a click moves it into `queued_remove`.
    """
    assert subscription.is_clickable(subscription.SUBSCRIBED)
    assert subscription.subscription_state(
        _item(own_subscribed=1, is_queued_for_subscription=0)) == subscription.SUBSCRIBED


# --- the detail pane's one subscription control -----------------------------

def test_the_direct_control_label_follows_the_derived_direction():
    """No state may show a "Subscribe" button whose next press removes."""
    assert subscription.subscription_action(subscription.QUEUED_REMOVE) == (
        "cancel_remove", "Cancel Unsubscribe")
    for state in (subscription.SUBSCRIBED, subscription.DOWNLOADED):
        assert subscription.subscription_action(state) == ("queue_remove", "Unsubscribe")
    for state in (subscription.QUEUED, subscription.PREVIOUSLY, subscription.NEVER):
        assert subscription.subscription_action(state) == ("subscribe", "Subscribe")


def test_every_state_has_a_subscription_action_and_an_unknown_one_is_loud():
    for state in subscription.STATE_PRECEDENCE:
        action, label = subscription.subscription_action(state)
        assert action in ("subscribe", "queue_remove", "cancel_remove")
        assert label
    with pytest.raises(ValueError):
        subscription.subscription_action("nonsense")


def test_a_subscribed_item_never_gets_a_subscribe_button():
    """The lie this replaced: a Subscribe label beside a press that removes."""
    for state in (subscription.SUBSCRIBED, subscription.DOWNLOADED,
                  subscription.QUEUED_REMOVE):
        _action, label = subscription.subscription_action(state)
        assert label != "Subscribe"


def test_attach_marker_carries_the_direct_control():
    item = subscription.attach_marker(
        _item(own_subscribed=1, is_queued_for_subscription=1))
    assert item["subscription_action"] == "cancel_remove"
    assert item["subscription_action_label"] == "Cancel Unsubscribe"


def test_every_css_class_is_distinct():
    assert len({subscription.marker_spec(s)[2] for s in subscription.STATE_PRECEDENCE}) == len(
        subscription.STATE_PRECEDENCE)


def test_an_unknown_state_is_a_loud_error():
    with pytest.raises(ValueError):
        subscription.marker_spec("nonsense")
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
        for state in subscription.STATE_PRECEDENCE:
            _glyph, colour, _css, _label = subscription.marker_spec(state)
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
    assert "subscription.marker_spec(" in source
    # The old leading-`*` queue prefix must be gone: one indicator, not two.
    assert '"*"' not in source
    assert "[green]*[/green]" not in source
