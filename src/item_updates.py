"""One item-update path: a per-item subscription registry and one dispatch point.

Both front ends display the same item in more than one place at once -- the TUI
draws a list row and a detail pane, the web draws a grid cell and a detail pane
-- and before this module each display was refreshed by whichever code path
happened to know about a change. That is how one display could be current while
another showed a stale marker: nothing connected the writer to the components.

The registry is that connection, and it is deliberately small:

* a **component subscribes to the ``workshop_id`` it is displaying** when it
  starts and unsubscribes when it stops, so the registry describes what is on
  screen rather than what exists in the database;
* every update the front end receives -- a callback, a poll, an action, a
  signal from another process -- goes through :meth:`ItemUpdateRegistry.dispatch`,
  which looks up that id's subscribers and hands each of them the update;
* an **update is a block of data, not a field**: the mapping the producer
  actually has (one column or all of them), with the derived
  ``subscription_*`` keys attached by :func:`src.subscription.attach_marker`, so
  the TUI and the web hand their subscribers the same shape. A subscriber
  applies the fields it renders and ignores the rest, which is why a column
  added later reaches every panel without a new call site.

The registry is the only coupling. No component is a target a caller must
remember to refresh, so a display added later is correct as soon as it
subscribes.

The web page mirrors this module in ``templates/index.html`` (``_itemSubscribers``
/ ``dispatchItemUpdate``); ``tests/test_item_update_path.py`` holds the two to
the same behaviour.
"""

from __future__ import annotations

from typing import Any

from src import subscription

#: The general poll's cadence, in seconds, in the TUI. The web page mirrors it
#: as ``_ITEM_UPDATE_POLL_MS``. It is the always-on trigger: it runs whether or
#: not anything is pending, because a read that only happens while a spinner is
#: drawn is a read that misses a change made behind the front end's back (the
#: daemon's folder scan stamping ``steam_download_seen_at``). The cost is one
#: batched read of the registry's ids, not a table scan and not one read per
#: component.
ITEM_UPDATE_POLL_SECONDS = 3.0


class ItemUpdateRegistry:
    """``workshop_id`` -> the components currently displaying it.

    Subscribers are objects with an ``apply_item_update(item)`` method; they are
    validated when they subscribe, so a component that cannot receive an update
    fails there rather than silently receiving nothing at dispatch time.
    """

    def __init__(self) -> None:
        self._subscribers: dict[int, list[Any]] = {}

    @staticmethod
    def _key(workshop_id) -> int | None:
        try:
            return int(workshop_id)
        except (TypeError, ValueError):
            return None

    def subscribe(self, workshop_id, subscriber) -> bool:
        """Start sending ``workshop_id``'s updates to ``subscriber``.

        Returns False when the id is unusable (a row with no id cannot be
        subscribed to); raises TypeError when the subscriber does not implement
        the one-method protocol, which is a programming error rather than a
        runtime condition.
        """
        key = self._key(workshop_id)
        if key is None:
            return False
        if not callable(getattr(subscriber, "apply_item_update", None)):
            raise TypeError(
                f"{subscriber!r} cannot subscribe: an item-update subscriber "
                "needs an apply_item_update(item) method"
            )
        subscribers = self._subscribers.setdefault(key, [])
        if subscriber not in subscribers:
            subscribers.append(subscriber)
        return True

    def unsubscribe(self, workshop_id, subscriber) -> None:
        """Stop sending ``workshop_id``'s updates to ``subscriber``.

        Idempotent: a component that unmounts without ever subscribing, or that
        unsubscribes twice, is not an error.
        """
        key = self._key(workshop_id)
        if key is None:
            return
        subscribers = self._subscribers.get(key)
        if not subscribers:
            return
        try:
            subscribers.remove(subscriber)
        except ValueError:
            return
        if not subscribers:
            del self._subscribers[key]

    def subscribers(self, workshop_id) -> tuple:
        """The components currently displaying ``workshop_id``."""
        return tuple(self._subscribers.get(self._key(workshop_id), ()))

    def workshop_ids(self) -> list[int]:
        """The ids on screen, for the general poll's single batched read."""
        return list(self._subscribers.keys())

    def dispatch(self, item) -> int:
        """Hand one item's block to every component displaying that id.

        Returns how many subscribers received it. The block is copied and
        enriched with the derived ``subscription_*`` keys, so the shape a TUI
        subscriber receives is the one the web page's ``/api/items`` payload
        carries.
        """
        if not isinstance(item, dict):
            return 0
        key = self._key(item.get("workshop_id"))
        if key is None:
            return 0
        subscribers = self._subscribers.get(key)
        if not subscribers:
            return 0
        payload = subscription.attach_marker(dict(item))
        for subscriber in list(subscribers):
            subscriber.apply_item_update(payload)
        return len(subscribers)

    def dispatch_many(self, items) -> int:
        """Dispatch a batch of blocks; returns the number of deliveries."""
        return sum(self.dispatch(item) for item in (items or ()))
