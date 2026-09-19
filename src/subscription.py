"""Whether the owner is, or ever was, subscribed to a workshop item.

One question -- "does *this account* subscribe to this item?" -- has five
answers, and both front ends must give the same one for the same row. The TUI
draws a Textual markup label and the web draws a positioned element with a CSS
class, so the mapping from state to appearance lives here, once, and each front
end renders from it. A contract test holds `templates/index.html` to this table,
because two front ends that disagree about why a marker looks the way it does is
worse than either being wrong alone.

The five states, and the precedence between them:

* ``downloaded`` -- the owner is subscribed *and* Steam has the item on disk
  (``own_subscribed`` and the local ``downloaded_at`` latch both set).
* ``subscribed`` -- the owner is subscribed right now (``own_subscribed``).
* ``queued``    -- queued to subscribe (``is_queued_for_subscription``).
* ``previously`` -- we have *seen* this account subscribed, and are not now
  (``own_first_subscribed_at`` set, ``own_subscribed`` clear).
* ``never``      -- none of the above.

When the flags disagree the order is ``downloaded > subscribed > queued >
previously > never``: a downloaded subscription is the strongest statement the
table can make, a confirmed subscription beats a queue entry for the same item
(there is nothing left to queue), and the sticky first-seen timestamp outranks
never having seen anything.

**``downloaded`` requires both flags.** ``downloaded_at`` is a local latch --
only ``src.workshop_folders`` writes it and only the subscription walk clears it
-- and a stray timestamp beside a cleared ``own_subscribed`` must not claim the
green star. The state derivation therefore tests ``own_subscribed`` *and*
``downloaded_at``; either alone is not enough. See
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
DOWNLOADED = "downloaded"
SUBSCRIBED = "subscribed"
QUEUED = "queued"
PREVIOUSLY = "previously"
NEVER = "never"

# Precedence, strongest first. The first state whose flag is set wins, which is
# what makes "subscribed + queued" draw the confirmed subscription and not the
# queue entry.
STATE_PRECEDENCE = (DOWNLOADED, SUBSCRIBED, QUEUED, PREVIOUSLY, NEVER)

# state -> (glyph, colour, CSS class, human label). The CSS class is what the
# web element carries; the colour is the same value the TUI interpolates into
# its markup, so both read "solid yellow" or "green" identically.
#
# `downloaded` is a solid `★` in a deeper green than `queued`'s #2ecc40. The
# glyph says "subscribed" (a filled star, like `subscribed`) and the colour says
# "settled" -- the two greens are deliberately different values in this one
# table, so neither front end has to decide how to distinguish them.
MARKER_SPECS = {
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

# Which states the web marker acts on. `subscribed` deliberately does not: the
# only action would be an unsubscribe, and an accidental unsubscribe is not
# wanted. `downloaded` is the same subscription seen from disk, so it is inert
# too; its action (opening the folder) is a separate button and key.
CLICKABLE_STATES = (QUEUED, PREVIOUSLY, NEVER)


def subscription_state(item: dict) -> str:
    """The state to draw for ``item``, resolving the precedence.

    ``downloaded`` needs *both* the subscription flag and the local latch: a
    timestamp left behind by a cleared subscription must not claim the green
    star. Below that, ``subscribed`` wins over a stale queue flag (there is
    nothing pending for an item that is already subscribed), a queue flag wins
    over the sticky first-seen timestamp, and a set ``own_first_subscribed_at``
    is what makes the difference between ``previously`` and ``never`` --
    including when ``own_subscribed`` has since been cleared.
    """
    if item.get("own_subscribed") and item.get("downloaded_at"):
        return DOWNLOADED
    if item.get("own_subscribed"):
        return SUBSCRIBED
    if item.get("is_queued_for_subscription"):
        return QUEUED
    if item.get("own_first_subscribed_at"):
        return PREVIOUSLY
    return NEVER


def spec(state: str) -> tuple[str, str, str, str]:
    """``(glyph, colour, css class, label)`` for ``state``."""
    try:
        return MARKER_SPECS[state]
    except KeyError:
        raise ValueError(f"unknown subscription state {state!r}") from None


def glyph(state: str) -> str:
    """The Unicode character the marker draws for ``state``."""
    return spec(state)[0]


def colour(state: str) -> str:
    """The hex colour both front ends use for ``state``."""
    return spec(state)[1]


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
