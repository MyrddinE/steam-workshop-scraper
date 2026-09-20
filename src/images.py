"""What ``workshop_items.image_answer`` holds, and what may be built from it.

The column started as a file extension and nothing else: a non-empty value
meant an image had been written to ``images/<h1>/<h2>/<h3>/<id>.<ext>``, and the only
question anyone asked of it was "which extension", so that a URL could be
built. An image that could not be downloaded left it NULL — the same value as
an image that had never been attempted, which is why a preview that 404s was
re-requested forever: nothing could tell "not yet" from "never will be".

So the column now carries the *answer* rather than only the file type. Two
kinds of value live in it:

* **A real image extension** (``jpg``, ``png``, …) — a local file exists and a
  URL may be built from it. :data:`IMAGE_EXTENSIONS` is derived from the maps
  the downloader writes with, so a format added there is renderable here
  without a second list to keep in step.
* **A wholly numeric value** — an HTTP status. The server answered, and the
  answer was that no image is available. No URL may be built; the interface
  draws the number instead. Keeping the status rather than a boolean means
  other answers can be recorded later without another schema change, and 404
  and 410 can be permanent while a future 429 is not.

The rule that matters is the one this module exists to enforce: **never build
an image URL from a value that is not a known image extension.** The browser
must not be told to fetch a file that was never written.
"""

from __future__ import annotations

# The extensions the downloader can write, and therefore the only ones a URL
# may be built from. Derived from the maps below so the writer and the reader
# cannot disagree about what an image extension is.
MIME_MAP = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/jpg": "jpg",
}

MAGIC_EXT_MAP = {
    ".jpeg": "jpg",
    ".jpg": "jpg",
    ".jfif": "jpg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
    ".bmp": "bmp",
}

IMAGE_EXTENSIONS = frozenset(MIME_MAP.values()) | frozenset(MAGIC_EXT_MAP.values())

# Statuses that will not change however many times we ask. A missing preview is
# a fact about the item, not about our request, so it is never retried and it
# never moves the download delay.
PERMANENT_IMAGE_STATUSES = frozenset({404, 410})

# The three classifications the rest of the code branches on.
PRESENT = "present"       # a real extension; a file exists; build the URL
PERMANENTLY_MISSING = "permanent"   # a definitive answer; do not retry, do not render
TRANSIENT = "transient"   # some other status; recorded, but still worth retrying
NOT_AN_IMAGE = "other"           # a content type that was not an image
ABSENT = "absent"         # nothing recorded yet


def is_status_marker(stored) -> bool:
    """Whether ``stored`` is a wholly numeric value, i.e. an HTTP status.

    Wholly numeric is the test the owner asked for, so this stays true only
    when every character is a digit: an extension is never a number, and a
    served content type is never *only* a number.
    """
    if not isinstance(stored, str):
        return False
    value = stored.strip()
    return bool(value) and value.isdigit()


def is_image_extension(stored) -> bool:
    """Whether ``stored`` names a format the downloader can write."""
    return isinstance(stored, str) and stored.strip().lower() in IMAGE_EXTENSIONS


def image_state(stored) -> str:
    """Classify what the column currently holds.

    Unusable input — ``None``, an empty string, a non-string — is :data:`ABSENT`
    rather than an error: a database being read is not a place to raise.
    """
    if not isinstance(stored, str) or not stored.strip():
        return ABSENT
    value = stored.strip()
    if is_image_extension(value):
        return PRESENT
    if is_status_marker(value):
        return PERMANENTLY_MISSING if int(value) in PERMANENT_IMAGE_STATUSES else TRANSIENT
    # A non-numeric, non-image token: the server served something that is not a
    # picture, and it will serve the same thing next time.
    return NOT_AN_IMAGE


def can_render_image(stored) -> bool:
    """Whether an image URL may be built from ``stored``.

    The single guard every URL-building site must consult. Anything that is not
    a known image extension — a status, a bad content type, nothing at all —
    must be drawn, not fetched.
    """
    return image_state(stored) == PRESENT


def blocks_retry(stored) -> bool:
    """Whether the recorded answer is final, so the image must not be re-fetched.

    A permanent status and a non-image content type are both answers about the
    item that asking again cannot change. This is the predicate the re-fetch
    gates use; note it is *not* the same question as :func:`can_render_image`,
    because a real image still needs re-fetching when the item is revised.
    """
    return image_state(stored) in (PERMANENTLY_MISSING, NOT_AN_IMAGE)


def is_resolved(stored) -> bool:
    """Whether the image stage has an answer and needs no further attempt.

    True once there is a file (nothing to do) or a final answer (nothing can be
    done). False for :data:`TRANSIENT` and :data:`ABSENT`, both of which are
    still worth another attempt.
    """
    return can_render_image(stored) or blocks_retry(stored)


def permanent_status_marker(status) -> str | None:
    """The value to store for a response ``status``, or ``None`` if not permanent.

    Returns a string because the column is text and because a stored number
    must be distinguishable from a stored extension by shape alone.
    """
    if status is None:
        return None
    try:
        code = int(status)
    except (TypeError, ValueError):
        return None
    return str(code) if code in PERMANENT_IMAGE_STATUSES else None


def served_type_marker(content_type) -> str | None:
    """The value to store when a response could not be stored as an image.

    Called on the path where no extension could be resolved, which covers two
    cases: the server served something that is not a picture at all (``text/html``
    for an error page), and it served a picture in a format this downloader
    cannot write (``image/svg+xml``). Both are final — the same response is what
    the next attempt would get — so both are recorded, and neither is a real
    extension, so no URL is ever built from either.

    The bare subtype is stored because it is drawn in a grid cell at a large
    size; the full header, parameters and all, is already kept verbatim in the
    failure capture.
    """
    if not content_type:
        return None
    mime = str(content_type).split(";")[0].strip().lower()
    if not mime:
        return None
    return mime.split("/")[-1] or mime
