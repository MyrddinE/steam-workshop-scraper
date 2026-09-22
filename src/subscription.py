"""Whether the owner is, or ever was, subscribed to a workshop item.

One question -- "does *this account* subscribe to this item?" -- has six
answers, and both front ends must give the same one for the same row. The TUI
draws a Textual markup label and the web draws a positioned element with a CSS
class, so the mapping from state to appearance lives here, once, and each front
end renders from it. A contract test holds `templates/index.html` to this table,
because two front ends that disagree about why a marker looks the way it does is
worse than either being wrong alone.

The six states, and the precedence between them:

* ``queued_remove`` -- the owner is subscribed *and* the queue entry points at a
  removal (``is_queued_for_subscription`` and ``own_subscribed`` both set).
* ``downloaded`` -- the owner is subscribed *and* Steam has the item on disk
  (``own_subscribed`` and the local ``steam_download_seen_at`` latch both set).
* ``subscribed`` -- the owner is subscribed right now (``own_subscribed``).
* ``queued``    -- queued to subscribe (``is_queued_for_subscription``).
* ``previously`` -- we have *seen* this account subscribed, and are not now
  (``own_first_subscribed_at`` set, ``own_subscribed`` clear).
* ``never``      -- none of the above.

The queue flag carries no direction of its own; the direction is **derived**,
because that keeps every existing queued row meaning what it always meant and
needs no schema change: with the flag set, a subscribed row is a removal and an
unsubscribed one is an addition. When the flags disagree the order is
``queued_remove > downloaded > subscribed > queued > previously > never``: a
queued removal outranks the subscription it is about (otherwise the queue entry
would be invisible and the removal could never be seen to be pending), a
downloaded subscription is the strongest statement the table can make, a queue
entry for an unsubscribed item is the addition it always was, and the sticky
first-seen timestamp outranks never having seen anything.

**``downloaded`` requires both flags.** ``steam_download_seen_at`` is a local latch --
only ``src.workshop_folders`` writes it and only the subscription walk clears it
-- and a stray timestamp beside a cleared ``own_subscribed`` must not claim the
green star. The state derivation therefore tests ``own_subscribed`` *and*
``steam_download_seen_at``; either alone is not enough. See
[data-model.md](data-model.md) for the column's one-setter/one-clearer rule.

**The honesty constraint.** Steam exposes no endpoint that returns an account's
subscription *history*: ``lifetime_subscriptions`` is an item-wide count that
cannot be attributed to an account, and ``EnumerateUserSubscribedFiles`` is
publisher-key-only. ``previously`` therefore means only "we have seen this
account subscribed", and on the day this shipped there were zero such markers
regardless of real history; they fill in over time. That limitation is stated in
the marker's tooltip rather than hidden, which is exactly what :data:`MARKER_TOOLTIPS`
is for.

**Why the glyph is not a bare symbol.** Both front ends draw a real Unicode
character rather than a CSS pseudo-element or a text prefix, so a reader (or a
test) can find the marker in the rendered output and so the two front ends
cannot spell it differently.
"""

from __future__ import annotations

# The shared vocabulary. Exported so the front ends and the tests name the same
# strings rather than re-typing literals.
QUEUED_REMOVE = "queued_remove"
DOWNLOADED = "downloaded"
SUBSCRIBED = "subscribed"
QUEUED = "queued"
PREVIOUSLY = "previously"
NEVER = "never"

# Precedence, strongest first. The first state whose flag is set wins. A queued
# removal comes first because it is the only state the queue flag can be in
# beside a live subscription -- "subscribed + queued" is a pending removal now,
# not the confirmed subscription it used to draw.
STATE_PRECEDENCE = (QUEUED_REMOVE, DOWNLOADED, SUBSCRIBED, QUEUED, PREVIOUSLY, NEVER)

# state -> (glyph, colour, CSS class, human label). The CSS class is what the
# web element carries; the colour is the same value the TUI interpolates into
# its markup, so both read "solid yellow" or "green" identically.
#
# `queued_remove` is the empty star (`QUEUED`'s glyph) in red: the owner asked
# for an empty red star outline, and colour is the only thing separating it from
# the green ☆ of a queued addition.
#
# `downloaded` is a solid `★` in a deeper green than `queued`'s #2ecc40. The
# glyph says "subscribed" (a filled star, like `subscribed`) and the colour says
# "settled" -- the two greens are deliberately different values in this one
# table, so neither front end has to decide how to distinguish them.
MARKER_SPECS = {
    QUEUED_REMOVE: ("\u2606", "#e74c3c", "sub-queued-remove", "About to unsubscribe"),
    DOWNLOADED: ("\u2605", "#00a651", "sub-downloaded", "Subscribed, and downloaded"),
    SUBSCRIBED: ("\u2605", "#ffd700", "sub-subscribed", "Currently subscribed"),
    QUEUED: ("\u2606", "#2ecc40", "sub-queued", "About to subscribe"),
    PREVIOUSLY: ("\u2606", "#ffd700", "sub-previously", "Was subscribed; not now"),
    NEVER: ("\u25cb", "#808080", "sub-never", "Never subscribed"),
}

# state -> the explanatory hover text, for the web element's `title`.
#
# `subscribed`, `queued` and `never` can be stated plainly. `previously` must
# not imply a complete history: it is a claim about what we have observed, and
# spelling that out is the honest wording the owner asked for.
MARKER_TOOLTIPS = {
    QUEUED_REMOVE: "Queued to unsubscribe.",
    DOWNLOADED: "You are subscribed to this item and Steam has downloaded it.",
    SUBSCRIBED: "You are subscribed to this item.",
    QUEUED: "Queued to subscribe.",
    PREVIOUSLY: (
        "You were subscribed to this item at some point since this marker "
        "started being recorded. Steam exposes no subscription history for an "
        "account, so this means \"we have seen you subscribed\", not a complete "
        "record: earlier subscriptions are not shown."
    ),
    NEVER: (
        "You have never been seen subscribed to this item. Steam exposes no "
        "subscription history for an account, so this is what we have observed, "
        "not proof that you never subscribed."
    ),
}

# Which states the web marker acts on. The old table deliberately left
# `subscribed` out: "the only action would be an unsubscribe, and an accidental
# unsubscribe is not wanted." The owner has reversed that. The action on a
# subscribed item is no longer an unsubscribe at all -- it only queues a removal,
# and the queue is cancelled by a second click -- so an accidental click is
# recoverable and the marker becomes a control for the new `queued_remove` state
# as well as for itself. `downloaded` is the same subscription seen from disk and
# stays inert; its action (opening the folder) is a separate button and key.
CLICKABLE_STATES = (QUEUED, QUEUED_REMOVE, SUBSCRIBED, PREVIOUSLY, NEVER)


def subscription_state(item: dict) -> str:
    """The state to draw for ``item``, resolving the precedence.

    ``queued_remove`` is the derived direction: the queue flag alone cannot say
    which way it points, so a queued row that is subscribed is a removal and a
    queued row that is not is an addition. It is tested before ``downloaded``
    because a subscribed item can also have the download latch, and the pending
    removal must stay visible over the green star. Below it, ``downloaded``
    needs *both* the subscription flag and the local latch: a timestamp left
    behind by a cleared subscription must not claim the green star, and
    ``subscribed`` wins over an unsubscribed queue entry, a queue flag wins over
    the sticky first-seen timestamp, and a set ``own_first_subscribed_at`` is
    what makes the difference between ``previously`` and ``never`` -- including
    when ``own_subscribed`` has since been cleared.
    """
    if item.get("is_queued_for_subscription") and item.get("own_subscribed"):
        return QUEUED_REMOVE
    if item.get("own_subscribed") and item.get("steam_download_seen_at"):
        return DOWNLOADED
    if item.get("own_subscribed"):
        return SUBSCRIBED
    if item.get("is_queued_for_subscription"):
        return QUEUED
    if item.get("own_first_subscribed_at"):
        return PREVIOUSLY
    return NEVER


def marker_spec(state: str) -> tuple[str, str, str, str]:
    """``(glyph, colour, css class, label)`` for ``state``."""
    try:
        return MARKER_SPECS[state]
    except KeyError:
        raise ValueError(f"unknown subscription state {state!r}") from None


def glyph(state: str) -> str:
    """The Unicode character the marker draws for ``state``."""
    return marker_spec(state)[0]


def colour(state: str) -> str:
    """The hex colour both front ends use for ``state``."""
    return marker_spec(state)[1]


def tooltip(state: str) -> str:
    """The web element's ``title`` text for ``state``.

    Raises ``ValueError`` for an unknown state, so a typo cannot silently fall
    back to some default wording.
    """
    try:
        return MARKER_TOOLTIPS[state]
    except KeyError:
        raise ValueError(f"unknown subscription state {state!r}") from None


def is_clickable(state: str) -> bool:
    """Whether clicking the web marker for ``state`` does anything."""
    if state not in MARKER_SPECS:
        raise ValueError(f"unknown subscription state {state!r}")
    return state in CLICKABLE_STATES


# state -> (action, label) for the detail pane's one subscription control.
#
# The control acts on the derived direction, and its wording says what that
# press does, so no state shows a "Subscribe" button that removes:
#
# * an unsubscribed item's ``subscribe`` acts directly (``doSubscribe``);
# * a subscribed item's ``queue_remove`` only queues its removal -- the owner's
#   removals are deliberate and recoverable, so the button never unsubscribes
#   behind one click -- and the marker draws the pending red star;
# * a ``queued_remove`` item's ``cancel_remove`` cancels the queued removal.
#
# Derived here rather than re-derived in the template, so the payload and any
# future front end read one table.
SUBSCRIPTION_ACTIONS = {
    QUEUED_REMOVE: ("cancel_remove", "Cancel Unsubscribe"),
    DOWNLOADED: ("queue_remove", "Unsubscribe"),
    SUBSCRIBED: ("queue_remove", "Unsubscribe"),
    QUEUED: ("subscribe", "Subscribe"),
    PREVIOUSLY: ("subscribe", "Subscribe"),
    NEVER: ("subscribe", "Subscribe"),
}


def subscription_action(state: str) -> tuple[str, str]:
    """``(action, label)`` for the direct subscription control at ``state``.

    ``action`` is ``subscribe`` (act now), ``queue_remove`` (queue the removal)
    or ``cancel_remove`` (cancel the queued removal); ``label`` is the word the
    control carries. Raises ``ValueError`` for an unknown state, so a typo
    cannot silently fall back to "Subscribe".
    """
    if state not in SUBSCRIPTION_ACTIONS:
        raise ValueError(f"unknown subscription state {state!r}")
    return SUBSCRIPTION_ACTIONS[state]


def attach_marker(item: dict) -> dict:
    """Add the whole derived marker to ``item`` in place and return it.

    This is the marker half of the front ends' shared item-update payload: the
    web server enriches every payload it returns through here, and
    :mod:`src.item_updates` enriches every block the TUI dispatches through
    here, so a subscriber on either side receives the same field names for the
    same stored row. The raw columns (``own_subscribed``,
    ``steam_download_seen_at``, ...) stay on the payload as well, because the
    state is derived from them and a display may legitimately read either.
    """
    state = subscription_state(item)
    glyph, colour, css, label = marker_spec(state)
    action, action_label = subscription_action(state)
    item["subscription_state"] = state
    item["subscription_glyph"] = glyph
    item["subscription_colour"] = colour
    item["subscription_class"] = css
    item["subscription_label"] = label
    item["subscription_tooltip"] = tooltip(state)
    item["subscription_clickable"] = is_clickable(state)
    item["subscription_action"] = action
    item["subscription_action_label"] = action_label
    return item
