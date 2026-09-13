"""Captured bodies must keep the part that explains the miss.

Cutting at MAX_BODY_BYTES alone kept the head, and a modern Steam page is mostly
script — the live capture of the dominant failure retained 64 KB of `<script>`
and `<link>` tags, reported `class_count: 2`, and contained no markup that
identified the page. The artefact could not answer the question it existed for,
and the shape digest described the script bundle rather than the document.
"""

from src.capture import MAX_BODY_BYTES, _strip_noise, describe_shape

MARKUP = (
    b'<html><head><title>Steam Workshop :: Item</title></head><body>'
    b'<div class="workshopItemTitle">T</div>'
    b'<div class="workshopItemDescription" id="highlightContent">D</div>'
    b'<span class="workshopTags">tag</span>'
    b'</body></html>'
)


def test_script_bodies_are_removed():
    blob = b'<html><script>var a = 1;</script><div class="x">y</div></html>'
    out = _strip_noise(blob)
    assert b'var a = 1;' not in out
    assert b'class="x"' in out
    assert b'>y<' in out


def test_style_bodies_are_removed():
    blob = b'<style>.a{color:red}</style><p class="b">hi</p>'
    out = _strip_noise(blob)
    assert b'color:red' not in out
    assert b'class="b"' in out


def test_an_empty_body_is_untouched():
    assert _strip_noise(b'') == b''


def test_a_body_under_the_cap_keeps_the_same_digest():
    """Stripping must not re-key captures whose body already fitted.

    Otherwise every existing variant would be seen as a new shape and the
    per-group variant cap would be spent re-learning shapes already known.
    """
    raw = MARKUP
    assert len(raw) < MAX_BODY_BYTES
    assert describe_shape(raw) == describe_shape(_strip_noise(raw))


def test_a_script_heavy_page_now_keeps_its_markup():
    """The regression, modelled on the live page: markup behind a script head."""
    raw = b'<html><head><script>' + b'var x=1;' * 20000 + b'</script></head>' + MARKUP
    assert len(raw) > MAX_BODY_BYTES

    unused_digest, old_classes, _ = describe_shape(raw[:MAX_BODY_BYTES])
    unused_digest2, new_classes, new_title = describe_shape(
        _strip_noise(raw)[:MAX_BODY_BYTES])

    assert old_classes == 0, "the old window was entirely script"
    assert new_classes >= 3, "the new window carries the page's own markup"
    assert new_title == "Steam Workshop :: Item"


def test_stripping_is_stable():
    """The same body must strip the same way every time, or digests flap."""
    blob = b'<script>a</script><div class="x"></div><style>b</style>'
    assert _strip_noise(blob) == _strip_noise(blob)
