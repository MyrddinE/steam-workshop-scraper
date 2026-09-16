"""What a workshop item is waiting on, and how both front ends draw it.

The list marker is not a binary "pending": its speed says *which* stage is
outstanding, and its colour fades with the speed, so a marker that will clear in
seconds does not look like one that may take hours. An item owing a web scrape
can sit there for a long time, and a bright, frantic spinner on it would be
visual noise; the same item's marker is therefore slow and grey.

The TUI and the web draw this with entirely different mechanisms -- a braille
glyph advanced by a Textual timer, and a CSS-animated ring -- so the mapping
from state to appearance lives here, once. A contract test holds the template's
durations and colours to this table, because two front ends that disagree about
why an item looks pending is worse than either being wrong on its own.

**Fastest stage wins when an item owes several.** A marker that is about to
clear should say so rather than being hidden behind a slower stage; the slow,
faded marker is also the one that must not nag, and it will still be there
after the quick stages are done.

**The API is deliberately not a stage.** A list only ever shows items the API
has already returned, so a pending *refresh* is not something the reader is
waiting on for content -- the row in front of them is complete as far as it can
be. The clause this replaced was measured at 0 across 2.27M live rows, and the
one place it did fire was a detail-pane open re-queueing a refresh for an item
that was already fully fetched.
"""

from __future__ import annotations

from src import images

# One rotation of the base marker. Every stage is a multiple of this.
BASE_ROTATION_SECONDS = 0.6

# (stage, period multiplier, colour), fastest first. The order is the
# precedence: the first pending stage in this sequence is the one drawn.
STAGES = (
    ("image", 1, "#44aa44"),
    ("translation", 4, "#7f9a7f"),
    ("web", 16, "#8a8a8a"),
)

STAGE_NAMES = tuple(stage for stage, _multiplier, _colour in STAGES)


def _is_pending(stage: str, item: dict) -> bool:
    if stage == "image":
        # A non-empty image_extension is not proof of a picture: it may hold the
        # status that said there is none, which is settled, not pending.
        return (item.get("needs_image", 0) >= 5
                and not images.is_resolved(item.get("image_extension")))
    if stage == "translation":
        return item.get("translation_priority", 0) >= 5
    if stage == "web":
        return item.get("needs_web_scrape", 0) >= 5
    raise ValueError(f"unknown stage {stage!r}")


def pending_stage(item: dict) -> str | None:
    """The stage whose marker this item should show, or ``None``.

    The first pending stage in :data:`STAGES` order, so the fastest wins.
    """
    for stage, _multiplier, _colour in STAGES:
        if _is_pending(stage, item):
            return stage
    return None


def stage_spec(stage: str) -> tuple[int, str]:
    """``(period multiplier, colour)`` for ``stage``."""
    for name, multiplier, colour in STAGES:
        if name == stage:
            return multiplier, colour
    raise ValueError(f"unknown stage {stage!r}")


def rotation_seconds(stage: str) -> float:
    """How long one rotation takes for ``stage``, in seconds."""
    multiplier, _colour = stage_spec(stage)
    return BASE_ROTATION_SECONDS * multiplier
