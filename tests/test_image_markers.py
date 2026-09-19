"""What `workshop_items.image_answer` holds, and what may be built from it.

The column carries two kinds of value. A real image extension means a file was
written and a URL may be built. A wholly numeric value is an HTTP status: the
server answered, and the answer was that there is no image. One rule follows,
and it is the reason this module exists: **a URL must never be built from the
second kind**, because that asks the browser for a file that was never written.

The same value also carries the fix for a preview that was fetched forever. A
404 and a non-image content type are answers about the item that asking again
cannot change, so they are final; a transient status is not. Both halves are
pinned here, and the loop they close end to end in
`tests/test_stage_refresh.py`.
"""

import pytest

from src import images
from src.images import (
    ABSENT,
    IMAGE_EXTENSIONS,
    MAGIC_EXT_MAP,
    MIME_MAP,
    NOT_AN_IMAGE,
    PERMANENTLY_MISSING,
    PRESENT,
    TRANSIENT,
    blocks_retry,
    can_render_image,
    image_state,
    is_image_extension,
    is_resolved,
    is_status_marker,
    served_type_marker,
    permanent_status_marker,
)

# Every value that must never be handed to the browser as a filename.
NOT_A_PICTURE = ["404", "410", "503", "429", "500", "html", "json", "", None]


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("ext", sorted(IMAGE_EXTENSIONS))
def test_a_real_extension_is_a_picture(ext):
    assert image_state(ext) == PRESENT
    assert can_render_image(ext)
    assert not blocks_retry(ext)
    assert is_resolved(ext)


@pytest.mark.parametrize("code", ["404", "410"])
def test_a_permanent_status_is_final_and_never_a_picture(code):
    assert image_state(code) == PERMANENTLY_MISSING
    assert not can_render_image(code), "a status must never become a URL"
    assert blocks_retry(code), "a 404 will still be a 404 next time"
    assert is_resolved(code)


@pytest.mark.parametrize("code", ["500", "502", "503", "429", "403"])
def test_any_other_status_stays_retryable(code):
    """A server error is not a fact about the preview, so it keeps its turn."""
    assert image_state(code) == TRANSIENT
    assert not can_render_image(code)
    assert not blocks_retry(code)
    assert not is_resolved(code)


@pytest.mark.parametrize("token", ["html", "json", "plain", "svg+xml"])
def test_a_non_image_type_is_final(token):
    """What was served once is what will be served next time."""
    assert image_state(token) == NOT_AN_IMAGE
    assert not can_render_image(token)
    assert blocks_retry(token)


@pytest.mark.parametrize("empty", [None, "", "   ", 0, [], {}, object()])
def test_nothing_recorded_is_absent_and_retryable(empty):
    assert image_state(empty) == ABSENT
    assert not can_render_image(empty)
    assert not blocks_retry(empty), "an item with no answer must still be attempted"


@pytest.mark.parametrize("value", NOT_A_PICTURE)
def test_a_url_is_never_built_from_anything_but_an_extension(value):
    """The headline guard: this is what stops a bogus /images/<id>.<ext>."""
    assert not can_render_image(value)


def test_wholly_numeric_is_the_test_not_merely_numeric_ish():
    """The owner's rule: a marker is wholly numeric, so a near-miss is not one."""
    assert is_status_marker("404")
    assert is_status_marker(" 404 "), "the column is text and may carry padding"
    assert not is_status_marker("404x")
    assert not is_status_marker("4o4")
    assert not is_status_marker("40.4")
    assert not is_status_marker("jpg")
    assert not is_status_marker(None)


def test_case_and_padding_do_not_hide_a_real_picture():
    assert image_state("JPG") == PRESENT
    assert image_state(" jpg ") == PRESENT


def test_a_marker_is_not_mistaken_for_an_image_answer():
    """Wholly numeric and a real extension must not be confusable."""
    for ext in IMAGE_EXTENSIONS:
        assert not is_status_marker(ext)
    assert not is_image_extension("404")


# --- the writer and the reader must agree ---------------------------------

def test_the_allowlist_is_derived_from_what_the_downloader_writes():
    """A format the downloader can write must be one we will render.

    If these ever drift, the downloader writes a file the page refuses to show:
    the failure mode is a silently blank cell, which is why the list is derived
    from the maps rather than maintained alongside them.
    """
    written = set(MIME_MAP.values()) | set(MAGIC_EXT_MAP.values())
    assert written, "the downloader's maps must not be empty"
    assert written <= IMAGE_EXTENSIONS
    for ext in written:
        assert can_render_image(ext), f"{ext} can be written but not rendered"


# --- what gets stored -----------------------------------------------------

@pytest.mark.parametrize("status,expected", [(404, "404"), (410, "410"), ("404", "404")])
def test_a_permanent_status_is_recorded_as_its_number(status, expected):
    assert permanent_status_marker(status) == expected


@pytest.mark.parametrize("status", [500, 503, 429, 200, None, "abc", ""])
def test_a_non_permanent_status_records_nothing(status):
    """Only the final answers are written; the rest stay queued and retryable."""
    assert permanent_status_marker(status) is None


@pytest.mark.parametrize("content_type,expected", [
    ("text/html; charset=UTF-8", "html"),
    ("text/html", "html"),
    ("application/json", "json"),
    ("TEXT/HTML", "html"),
    # An image format this downloader cannot write is still final: recording it
    # stops the item being retried forever for a picture we will never store.
    ("image/svg+xml", "svg+xml"),
])
def test_the_served_type_is_recorded_as_a_bare_subtype(content_type, expected):
    assert served_type_marker(content_type) == expected


@pytest.mark.parametrize("content_type", [None, "", "   "])
def test_no_content_type_records_nothing(content_type):
    assert served_type_marker(content_type) is None
