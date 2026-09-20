"""Structured failure capture: keep the evidence, bounded.

When the scraper meets something it cannot handle — a page whose selector no
longer matches, an API response that is not JSON, an API status the code has no
branch for — the data is currently thrown away, so no regression test can ever be
built from it. This module writes that evidence to ``<outbox_dir>/failures/`` and
registers it in the same manifest the database snapshots use, so the existing
pull tooling collects it without changes.

Three design points carry the weight:

* **Bounded.** A persistent break must cost a bounded number of files. Captures
  are grouped by ``(kind, selector)`` and, within a group, by the page's shape
  digest. The first ``SAMPLES_PER_DIGEST`` samples of each distinct shape are
  kept and no more; a group tracks at most ``MAX_DIGESTS_PER_GROUP`` distinct
  shapes, so a digest that flaps (rotating page content) cannot defeat the cap.
  After that the group keeps counting misses and stops writing files. The
  counters, not the samples, are what convey scale — a sample cannot.
* **Additive.** Capturing never changes control flow. It is called from failure
  sites that return or raise exactly as before.
* **Off unless configured.** With no outbox directory every call is a no-op, so
  the feature is a deliberate switch and tests stay hermetic.

Layout::

    failures/<group>/_group.json          counters and per-shape state
    failures/<group>/<digest8>-<n>.json   the capture record
    failures/<group>/<digest8>-<n>.body   the raw bytes

Each file is registered as its own manifest entry (``kind: "failure"``) because
the puller transfers one file per entry. The counter fields live on the group
entry, which is the entry that represents the group; a sample file cannot carry
a group counter that stays true.

Image downloads reuse this machinery, with one hard rule: **the image bytes are
never copied into the outbox.** A download that succeeded already wrote the image
into its ``images/`` bucket and that file is the artefact; copying it here would
store every image twice. ``record_image_download`` therefore has no parameter
that carries a body — a parameter that does not exist cannot be passed by
accident. Its records are metadata and headers only, and they live in
``<outbox>/image_downloads/`` (successes, debug switch only) and the failure tree
above (failures, whenever capture is enabled).

Ordinary Steam community pulls reuse the same manifest under a second debug
switch: ``daemon.capture_web_downloads`` saves every item page, subscriptions
page and server-side subscribe through :func:`record_web_download` into
``<outbox>/web_downloads/`` -- request and response both, the body kept whole.
Those records are not failures; they are the instrument for seeing what a
*working* exchange looks like, and nothing in them may carry a credential. See
:func:`elide_secrets`.
"""

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

from src.backup import update_manifest

# How many samples of one shape are kept.
SAMPLES_PER_DIGEST = 3

# How many distinct shapes are tracked for one (kind, selector) group. Past this,
# a new shape is counted but not captured: otherwise rotating page content would
# produce a new digest per fetch and the per-shape cap would mean nothing.
MAX_DIGESTS_PER_GROUP = 5

# Bytes of a response body written to the .body file.
MAX_BODY_BYTES = 64 * 1024


GROUP_STATE_NAME = "_group.json"
_TEMP_SUFFIX = ".tmp"

_lock = threading.RLock()
_outbox_dir = None
_groups = {}
_app_version_cache = None

# Every Steam community web pull saved while `capture_web_downloads` is set --
# the item page, the subscriptions page and the server-side subscribe. Not
# failures: this is evidence about what a *working* exchange looks like, both
# the request that went out and the answer that came back.
#
# Unbounded, and the body is kept whole. This is a debugging switch that is on
# for a session or two, so the cost is accepted in exchange for not having to
# collect the evidence twice because a sample was thinned before anyone looked
# at it. The failure capture above is the opposite case: it runs for weeks, so
# its caps and its size limits stay.
_capture_web_downloads = False

# Every image download saved while `capture_image_downloads` is set, one
# metadata-only record each. Separate from `capture_web_downloads` because that
# switch keeps whole bodies unbounded; an owner reviewing images should not have
# to collect pages to do it.
_capture_image_downloads = False

FAILURES_DIR_NAME = "failures"
WEB_DOWNLOADS_DIR_NAME = "web_downloads"
IMAGE_DOWNLOADS_DIR_NAME = "image_downloads"

# The pulls the one switch covers, named so a reviewer can filter the captures
# by which request produced them.
ITEM_PAGE_KIND = "item_page"
SUBSCRIPTIONS_PAGE_KIND = "subscriptions_page"
SUBSCRIBE_KIND = "subscribe"
WEB_DOWNLOAD_STAGE = "web_download"
# Manifest kind shared by a web-download record and its body file.
WEB_DOWNLOAD_KIND = "web_download"

# ── credentials ──────────────────────────────────────────────────────────────
#
# The captures are an instrument for a signed-in session, so they necessarily
# describe credentialed requests. What must never reach the outbox is the
# credential *value*: the capture is for seeing which cookie names and which
# form fields went out, and a token on disk is a live credential for as long as
# it lasts.

REDACTED = "***"

# Header names whose whole value is a credential: a cookie jar travels as
# `Cookie`, Steam's answer can carry `Set-Cookie`, and a bearer or basic
# credential travels as `Authorization`.
_SECRET_HEADERS = frozenset({"cookie", "set-cookie", "authorization"})

# Form fields whose value is a credential. `sessionid` is the CSRF token the
# subscribe form carries; the login cookie is a cookie, not a form field.
_SECRET_FORM_FIELDS = frozenset({"sessionid"})

# The `name=value` pairs inside a request's `Cookie` header.
_COOKIE_PAIR_RE = re.compile(r"([^=;\s]+)\s*=\s*([^;]*)")

# Values shorter than this are still elided where they are recognised as
# credentials, but are not used for the whole-record scrub below. That scrub is
# a plain substring replacement over everything written, and a short value would
# shred the capture: a real cookie jar carries `timezoneOffset=0`, and replacing
# every `0` leaves a page that describes nothing. Real tokens are far longer.
MIN_SCRUB_LENGTH = 8

# Kind and stage for the two image record shapes. The stage is shared because
# both are the image-download step of the pipeline.
IMAGE_DOWNLOAD_KIND = "image_download"
IMAGE_FAILURE_KIND = "image_download_failed"
IMAGE_STAGE = "image_download"

# Candidate signs of who the page thinks we are, in the header corner. Recorded
# as a set of flags rather than interpreted, because which one is reliable is
# exactly what the captured pages are for.
_AUTH_MARKER_CANDIDATES = (
    "account_pulldown",       # account dropdown, present when signed in
    "global_action_menu",     # the header container both states use
    "Sign In",                # signed out
    "sign in",
    "store.steampowered.com/login",  # the signed-out sign-in link target
    "g_steamID",
)

_CLASS_RE = re.compile(rb"""class\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Element contents that tell you nothing about why a selector missed. A modern
# Steam page is mostly script: in the live capture that prompted this, the first
# 64 KB of a 303 KB page was <script> and <link> tags with no markup that
# identified the document, so neither the artefact nor its shape digest
# described the page. Removing these first spends the byte budget on markup.
_SCRIPT_STYLE_RE = re.compile(rb"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


def _strip_script_and_style(raw: bytes) -> bytes:
    """Drop script and style bodies, leaving the markup around them.

    Purely a size measure: the elements are replaced by an empty pair so the
    document's structure stays readable and the same input always strips the
    same way.
    """
    if not raw:
        return raw
    return _SCRIPT_STYLE_RE.sub(rb"<\1/>", raw)


# ── configuration ────────────────────────────────────────────────────────────

def configure(outbox_dir, capture_web_downloads=False, capture_image_downloads=False):
    """Enable capture under ``<outbox_dir>/failures``. ``None`` disables it.

    ``capture_web_downloads`` additionally saves *every* Steam community web pull
    -- the item page, the subscriptions page and the server-side subscribe --
    into ``<outbox_dir>/web_downloads``, request and response both, with the
    body kept whole. That answers a question the failure capture cannot: what a
    *working* exchange looks like, which is what identifies the signed-in markup
    and what the subscribe path actually sends.

    ``capture_image_downloads`` additionally saves *every* image download, into
    ``<outbox_dir>/image_downloads``, as metadata and headers only — never the
    image bytes, which already live in the images bucket.
    """
    global _outbox_dir, _capture_web_downloads, _capture_image_downloads
    with _lock:
        _outbox_dir = outbox_dir or None
        _capture_web_downloads = bool(capture_web_downloads) and bool(_outbox_dir)
        _capture_image_downloads = bool(capture_image_downloads) and bool(_outbox_dir)
        _groups.clear()
        if _outbox_dir:
            logging.info("Failure capture enabled: %s", failures_dir(_outbox_dir))
            if _capture_web_downloads:
                logging.info(
                    "Web-download capture enabled: saving every Steam community pull "
                    "(item page, subscriptions page, subscribe), request and response, "
                    "whole body, to %s. This is a debugging switch — turn it off when done.",
                    web_downloads_dir(_outbox_dir),
                )
            if _capture_image_downloads:
                logging.info(
                    "Image-download capture enabled: saving every image download's "
                    "metadata (status, headers, saved path — never the bytes) to %s. "
                    "This is a debugging switch — turn it off when done.",
                    image_downloads_dir(_outbox_dir),
                )


def web_download_switch(daemon_config) -> bool:
    """The ``capture_web_downloads`` debug switch from a parsed config.

    Two processes read this switch -- the daemon and the web server, which owns
    the server-side subscribe -- so the lookup lives here rather than being
    copied into both, and the deprecated name is honoured in both with the same
    warning. A config that still uses ``capture_web_scrapes`` keeps working and
    is told to rename it; the current key wins when both are present.
    """
    daemon_config = daemon_config or {}
    current = daemon_config.get("capture_web_downloads")
    legacy = daemon_config.get("capture_web_scrapes")
    if current is None and legacy is not None:
        logging.warning(
            "Config key 'capture_web_scrapes' is deprecated and still honoured; "
            "rename it to 'capture_web_downloads'."
        )
    return bool(current if current is not None else (legacy or False))


def is_enabled() -> bool:
    return _outbox_dir is not None


def failures_dir(outbox_dir) -> str:
    return os.path.join(outbox_dir, FAILURES_DIR_NAME)


def web_downloads_dir(outbox_dir) -> str:
    return os.path.join(outbox_dir, WEB_DOWNLOADS_DIR_NAME)


def image_downloads_dir(outbox_dir) -> str:
    return os.path.join(outbox_dir, IMAGE_DOWNLOADS_DIR_NAME)


def web_download_capture_active() -> bool:
    """Whether every community web pull is being saved. Read before each pull.

    Also decides whether the item page asks for its body: a successful scrape
    discards it by default, and there is nothing to capture without it.
    """
    with _lock:
        return bool(_outbox_dir) and _capture_web_downloads


def elide_secrets(cookies=None, data=None, headers=None):
    """Replace the credential values in one request with ``***``.

    Returns ``(cookies, data, headers, secrets)``. The three structures come
    back with every credential value removed and its **name** kept: knowing that
    ``steamLoginSecure`` and ``browserid`` were sent is the diagnostic point,
    while their values authenticate the session and must never reach the outbox.
    The ``sessionid`` form field is redacted, and the ``Cookie``, ``Set-Cookie``
    and ``Authorization`` header values are redacted whole.

    ``secrets`` is every literal value that was removed, longest first, so the
    recorder can scrub it from everything else it writes -- a token echoed into
    a response body or a URL is then removed too. That is defence in depth
    rather than the only barrier.

    Never raises: capture is diagnostic, and an elider that can break the
    request it is describing is worse than no elider. If even the fallback
    cannot describe the values, ``secrets`` is ``None`` and the caller must not
    write anything rather than write a value it could not recognise.
    """
    try:
        return _elide_secrets(cookies, data, headers)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logging.warning("Secret elision failed; falling back to wholesale redaction: %s", exc)
    try:
        return (_elide_values(cookies), _elide_values(data), _elide_values(headers),
                _ordered_secrets(_fallback_secret_values(cookies, data, headers)))
    except Exception as exc:  # noqa: BLE001 - see docstring
        logging.warning("Secret elision fallback failed (%s); refusing to record", exc)
        return None, None, None, None


def _elide_secrets(cookies, data, headers):
    elided_headers, header_secrets = _elide_headers(headers)
    secrets = []
    for value in dict(cookies or {}).values():
        if value not in (None, ""):
            secrets.append(str(value))
    for name, value in dict(data or {}).items():
        if str(name).lower() in _SECRET_FORM_FIELDS and value not in (None, ""):
            secrets.append(str(value))
    secrets.extend(header_secrets)
    return (_elide_values(cookies), _elide_form_data(data), elided_headers,
            _ordered_secrets(secrets))


def _elide_values(values) -> dict:
    """Every value redacted and every name kept."""
    return {name: REDACTED for name in dict(values or {})}


def _elide_form_data(data) -> dict:
    return {
        name: REDACTED if str(name).lower() in _SECRET_FORM_FIELDS else value
        for name, value in dict(data or {}).items()
    }


def _elide_headers(headers):
    """``(elided_headers, secrets)`` for one header mapping."""
    elided = {}
    secrets = []
    for name, value in dict(headers or {}).items():
        if str(name).lower() in _SECRET_HEADERS:
            elided[name] = REDACTED
            secrets.extend(_header_secret_values(name, value))
        else:
            elided[name] = value
    return elided, secrets


def _header_secret_values(name, value):
    """The literal credential values inside one credential-bearing header."""
    text = "" if value is None else str(value)
    if not text:
        return []
    values = [text]
    lowered = str(name).lower()
    if lowered == "cookie":
        values.extend(match.group(2).strip() for match in _COOKIE_PAIR_RE.finditer(text))
    elif lowered == "set-cookie":
        # Only the first pair is the cookie; the rest are attributes such as
        # `Path=/`, whose values are not credentials and must not be scrubbed.
        first = text.split(";", 1)[0]
        if "=" in first:
            values.append(first.split("=", 1)[1].strip())
    elif lowered == "authorization":
        # `Bearer <token>` / `Basic <base64>`: the credential is the last word,
        # and a body or URL can echo it without the scheme.
        parts = text.split()
        if len(parts) > 1:
            values.append(parts[-1])
    return [part for part in values if part]


def _fallback_secret_values(cookies, data, headers):
    """Best-effort secrets when the structured elision could not run."""
    values = [str(value) for value in dict(cookies or {}).values()
              if value not in (None, "")]
    values.extend(str(value) for name, value in dict(data or {}).items()
                  if str(name).lower() in _SECRET_FORM_FIELDS and value not in (None, ""))
    _, header_secrets = _elide_headers(headers)
    values.extend(header_secrets)
    return values


def _ordered_secrets(secrets) -> list:
    """Unique, longest first, so a value is replaced before any value it contains."""
    unique = []
    for value in secrets:
        if value and value not in unique:
            unique.append(value)
    return sorted(unique, key=len, reverse=True)


def _scrub_text(text, secrets):
    if not text or not secrets:
        return text
    for secret in secrets:
        if len(secret) >= MIN_SCRUB_LENGTH:
            text = text.replace(secret, REDACTED)
    return text


def ordered_secrets(secrets) -> list:
    """Unique credential values, longest first, ready for :func:`scrub_text`.

    A value is replaced before any value it contains, which is the order the
    whole-file scrub needs and the order :func:`elide_secrets` already returns.
    Exposed for the crash dump, which gathers its values from several sources --
    the cookie set, the config, the environment and the runtime registrations --
    and so cannot take the ordering from a single elision call.
    """
    return _ordered_secrets(secrets)


def scrub_text(text, secrets):
    """Replace every literal credential value in ``text`` with ``***``.

    This is the whole-file scrub :func:`_write_web_download` applies to a capture
    body, exposed so a second writer of diagnostic text -- the crash dump in
    :mod:`src.crash` -- removes the same literal values rather than growing a
    second rule with its own length floor. Values shorter than
    ``MIN_SCRUB_LENGTH`` are left alone on purpose: a real cookie jar carries
    ``timezoneOffset=0``, and replacing every ``0`` would leave a capture that
    describes nothing.
    """
    return _scrub_text(text, secrets)


def _scrub_bytes(raw: bytes, secrets) -> bytes:
    if not raw or not secrets:
        return raw
    marker = REDACTED.encode("utf-8")
    for secret in secrets:
        if len(secret) >= MIN_SCRUB_LENGTH:
            raw = raw.replace(secret.encode("utf-8", "replace"), marker)
    return raw


def record_web_download(kind, workshop_id, url, exchange, *, appid=None, page=None,
                        succeeded=None) -> bool:
    """Save one Steam community web pull, request and answer both. Unbounded.

    ``kind`` names which pull it was -- :data:`ITEM_PAGE_KIND`,
    :data:`SUBSCRIPTIONS_PAGE_KIND` or :data:`SUBSCRIBE_KIND` -- because one
    switch now covers all three and a reviewer has to be able to tell them
    apart. ``exchange`` is the caller's record of what actually went on the wire:

    * ``request`` -- the ``method``, ``url``, ``headers``, ``cookies`` and form
      ``data`` passed to the session, not re-derived guesses. A caller that
      cannot observe one of them is a gap in the capture, so the callers carry
      the values they built rather than reconstructing them.
    * ``http_status``, ``final_url``, ``response_headers`` and ``body`` -- what
      came back, with the body kept whole in a sibling file.

    Credentials are elided from everything written; see :func:`elide_secrets`.

    Deliberately not deduplicated, thinned or capped. The question is what a
    *working* exchange looks like, and one sample of that is worth more than
    several of the same failure: a sample trimmed before anyone has looked at it
    just means collecting the evidence twice. That makes this a *session* switch
    rather than a resident one. There is no budget here and no retention
    anywhere in the outbox, so the directory grows for as long as the switch is
    left on. The failure capture is the opposite case: it runs for weeks, so its
    caps stay.

    Never raises: capture is diagnostic, and a diagnostic that can break the
    request it is describing is worse than no diagnostic.
    """
    if not exchange:
        return False
    with _lock:
        if not _outbox_dir or not _capture_web_downloads:
            return False
        outbox = _outbox_dir

    try:
        return _write_web_download(outbox, kind, workshop_id, url, exchange,
                                   appid, page, succeeded)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logging.warning("Web-download capture failed (request unaffected): %s", exc)
        return False


def _write_web_download(outbox, kind, workshop_id, url, data, appid, page, ok) -> bool:
    request = dict(data.get("request") or {})
    cookies, form, headers, secrets = elide_secrets(
        cookies=request.get("cookies"), data=request.get("data"),
        headers=request.get("headers"))
    if secrets is None:
        # The elider could not name the values to remove, so there is no safe
        # subset to write. Refusing is the only outcome that cannot leak.
        raise RuntimeError("credential values could not be identified")
    response_headers, response_secrets = _elide_headers(data.get("response_headers"))
    secrets = _ordered_secrets(secrets + response_secrets)

    body = data.get("body")
    raw = b""
    if body is not None:
        raw = body.encode("utf-8", "replace") if isinstance(body, str) else bytes(body)
        # Whole and unstripped, deliberately: a debugging capture that drops the
        # <script> blocks also drops g_steamID, which is the most direct answer
        # to "is this page signed in?". Scrubbed, because a token echoed into
        # the page must not survive the elision of the structured fields.
        raw = _scrub_bytes(raw, secrets)

    stamp = _utc_now_iso().replace(":", "-")
    identifier = workshop_id if workshop_id is not None else f"{appid}-p{page}"
    stem = f"{stamp}-{kind}-{identifier}"
    directory = web_downloads_dir(outbox)
    os.makedirs(directory, exist_ok=True)
    body_path = os.path.join(directory, stem + ".body")
    record_path = os.path.join(directory, stem + ".json")

    if raw:
        _write_atomic(body_path, raw)

    record = {
        "kind": kind,
        "stage": WEB_DOWNLOAD_STAGE,
        "workshop_id": workshop_id,
        "appid": appid,
        "page": page,
        "request": {
            "method": request.get("method") or "GET",
            "url": request.get("url") or url,
            "headers": headers,
            "cookies": cookies,
            "data": form,
        },
        "ok": ok,
        "http_status": data.get("http_status"),
        "final_url": data.get("final_url"),
        "response_headers": response_headers,
        "body_file": _relative(outbox, body_path) if raw else None,
        "full_body_bytes": len(raw),
        "body_truncated": False,
        "full_body_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "auth_markers": auth_markers(body),
        "g_steamID": steam_id_from(body),
        "captured_at": _utc_now_iso(),
        "app_version": app_version(),
    }
    payload = _scrub_text(json.dumps(record, indent=2), secrets).encode("utf-8")
    _write_atomic(record_path, payload)
    update_manifest(outbox, {
        "path": _relative(outbox, record_path), "kind": WEB_DOWNLOAD_KIND,
        "bytes": len(payload), "role": "record", "workshop_id": workshop_id,
    })
    if raw:
        # The body has to be registered too, or the puller never sees it — the
        # record used to arrive without the page it describes.
        update_manifest(outbox, {
            "path": _relative(outbox, body_path), "kind": WEB_DOWNLOAD_KIND,
            "bytes": len(raw), "role": "body", "workshop_id": workshop_id,
        })
    return True


# ── image downloads ──────────────────────────────────────────────────────────

def record_image_download(workshop_id, url, succeeded, *, http_status=None, final_url=None,
                          headers=None, content_type=None, content_length=None,
                          bytes_written=None, saved_path=None,
                          error=None, error_type=None) -> bool:
    """Record one image download: metadata and headers, never the image bytes.

    There is deliberately **no parameter that carries the body**. A successful
    download already wrote the image into the ``images/`` bucket and that file
    is the artefact; copying it here would store every image twice. A parameter
    that does not exist cannot be passed by accident, which is stronger than one
    that is accepted and ignored.

    Failures are recorded whenever capture is enabled; they are grouped by
    failure signature (status, content type, exception class) so one 404 loop
    costs a bounded number of files while the group counters still convey its
    scale. Successes are recorded only while the debug switch is on, one record
    each, because the question there is what a good result looks like and the
    image can then be matched against its file on disk.

    ``bytes_written`` is a count, not data: it is the number of body bytes the
    download wrote to ``saved_path``. Never raises; a diagnostic that can break
    the download loop is worse than no diagnostic.
    """
    if not is_enabled():
        return False
    try:
        now = _utc_now_iso()
        if succeeded:
            return _record_image_success(
                workshop_id, url, http_status, final_url, headers, content_type,
                content_length, bytes_written, saved_path, now)
        return _record_image_failure(
            workshop_id, url, http_status, final_url, headers, content_type,
            content_length, error, error_type, now)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logging.warning("Image capture failed (download loop unaffected): %s", exc)
        return False


def _image_failure_signature(http_status, content_type, error_type) -> str:
    """A stable name for one kind of image failure.

    This is digested and counted against the failure caps, so it must not change
    between retries of the same fault: a rotating URL or an exception message
    carrying an id must not mint a new shape and defeat the bound. Hence the
    status, the content type, and the exception *class* — never the message.
    """
    return (f"http={http_status}|content_type={content_type or ''}"
            f"|error={error_type or ''}")


def _record_image_failure(workshop_id, url, http_status, final_url, headers,
                          content_type, content_length, error, error_type, now) -> bool:
    signature = _image_failure_signature(http_status, content_type, error_type)
    digest = hashlib.sha256(signature.encode("utf-8", "replace")).hexdigest()
    gid = group_id(IMAGE_FAILURE_KIND, None, IMAGE_STAGE)
    with _lock:
        sample_number = _select_sample_slot(gid, digest, now)
        if sample_number is None:
            _flush_group(gid)
            return False
        _write_image_failure_sample(
            gid, digest, sample_number, signature, workshop_id, url, http_status,
            final_url, headers, content_type, content_length, error, error_type, now)
        _flush_group(gid)
    return True


def _write_image_failure_sample(gid, digest, sample_number, signature, workshop_id,
                                url, http_status, final_url, headers, content_type,
                                content_length, error, error_type, now) -> dict:
    group_dir = os.path.join(failures_dir(_outbox_dir), gid)
    stem = f"{digest[:8]}-{sample_number}"
    record_path = os.path.join(group_dir, stem + ".json")

    record = {
        "kind": IMAGE_FAILURE_KIND,
        "stage": IMAGE_STAGE,
        "workshop_id": workshop_id,
        "url": url,
        "final_url": final_url,
        "http_status": http_status,
        "headers": dict(headers or {}),
        "content_type": content_type,
        "content_length": content_length,
        "error": error,
        "error_type": error_type,
        # No body file. The bytes of a failed download were never a usable
        # artefact, and writing an empty one would only add noise the puller
        # has to transfer.
        "body_file": None,
        "full_body_bytes": 0,
        "signature": signature,
        # An image failure has no body, so there are no CSS class names and no
        # class digest to record. `failure_digest` is the hash of `signature`,
        # which is what the group's per-shape bound actually keys on; the shape
        # keeps its class-name fields so a reader of either record kind sees the
        # same keys, but `class_digest` is honestly null rather than the failure
        # digest under a name that promises a class-name hash.
        "failure_digest": digest,
        "shape": {"class_digest": None, "class_count": 0, "title_tag": None},
        "captured_at": now,
        "app_version": app_version(),
    }
    _write_atomic(record_path, json.dumps(record, indent=2, sort_keys=True).encode("utf-8"))
    update_manifest(_outbox_dir, _manifest_entry(
        _outbox_dir, record_path, role="sample", group=gid, failure_digest=digest,
        workshop_id=workshop_id))
    return record


def _record_image_success(workshop_id, url, http_status, final_url, headers,
                          content_type, content_length, bytes_written, saved_path,
                          now) -> bool:
    with _lock:
        if not _outbox_dir or not _capture_image_downloads:
            return False
        outbox = _outbox_dir

    directory = image_downloads_dir(outbox)
    os.makedirs(directory, exist_ok=True)
    stem = f"{now.replace(':', '-')}-{workshop_id}"
    record_path = os.path.join(directory, stem + ".json")
    record = {
        "kind": IMAGE_DOWNLOAD_KIND,
        "stage": IMAGE_STAGE,
        "workshop_id": workshop_id,
        "url": url,
        "final_url": final_url,
        "http_status": http_status,
        "headers": dict(headers or {}),
        "content_type": content_type,
        "content_length": content_length,
        "bytes_written": bytes_written,
        "saved_path": saved_path,
        # The image itself is the artefact and is already on disk under
        # `saved_path`; a body_file here would be the same bytes a second time.
        "body_file": None,
        "captured_at": now,
        "app_version": app_version(),
    }
    payload = json.dumps(record, indent=2).encode("utf-8")
    _write_atomic(record_path, payload)
    update_manifest(outbox, {
        "path": _relative(outbox, record_path), "kind": IMAGE_DOWNLOAD_KIND,
        "bytes": len(payload), "role": "record", "workshop_id": workshop_id,
    })
    return True


_G_STEAMID_RE = re.compile(r"g_steamID\s*=\s*\"?([^\";\s]+)", re.IGNORECASE)


def steam_id_from(body) -> str | None:
    """The value assigned to `g_steamID`, or None.

    `false` when signed out, a SteamID64 when signed in, so it separates the two
    states far more decisively than any markup does — the markup is the user's
    chosen route because it is stable, and this is here so the same capture can
    confirm which marker actually tracks it.
    """
    if body is None:
        return None
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    match = _G_STEAMID_RE.search(text)
    return match.group(1) if match else None


def auth_markers(body) -> dict:
    """Which of the candidate header markers a body contains.

    Purely observational: the point is to compare a known signed-in page against
    a known signed-out one and pick the marker that actually separates them,
    rather than assuming which one it is.
    """
    if body is None:
        return {}
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    else:
        text = str(body)
    return {marker: (marker in text) for marker in _AUTH_MARKER_CANDIDATES}


def app_version() -> str:
    """The installed package version, or ``unknown`` when not installed."""
    global _app_version_cache
    if _app_version_cache is None:
        try:
            from importlib.metadata import version
            _app_version_cache = version("steam-workshop-scraper")
        except Exception:
            _app_version_cache = "unknown"
    return _app_version_cache


# ── shape ────────────────────────────────────────────────────────────────────

def _extract_class_names(body: bytes):
    names = set()
    for match in _CLASS_RE.findall(body):
        for name in match.decode("utf-8", "replace").split():
            if name:
                names.add(name)
    return names


def _extract_title_tag(body: bytes):
    match = _TITLE_RE.search(body)
    if not match:
        return None
    title = re.sub(rb"\s+", b" ", match.group(1)).strip()
    return title.decode("utf-8", "replace")[:200] or None


def _json_skeleton(body: bytes, depth: int = 0) -> str:
    """A coarse structural signature of a JSON body, or of arbitrary text."""
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        # Not JSON: fall back to the text with digits and runs of whitespace
        # collapsed, so a rotating id or timestamp does not change the shape.
        text = re.sub(rb"\d+", b"#", body[:4096])
        return "text:" + re.sub(rb"\s+", b" ", text).decode("utf-8", "replace")[:512]

    def walk(node, depth=0):
        if depth > 3:
            return "..."
        if isinstance(node, dict):
            return "{" + ",".join(sorted(str(k) for k in node)) + "}"
        if isinstance(node, list):
            return "[" + (walk(node[0], depth + 1) if node else "") + "]"
        return type(node).__name__

    return "json:" + walk(parsed)


def describe_shape(body: bytes):
    """``(class_digest, class_count, title_tag)`` for a captured body.

    The digest is a hash of the sorted set of CSS class names when the body
    carries any, and of a JSON skeleton otherwise (the API path has no classes).
    It is recorded, not interpreted: nothing classifies pages by it yet.
    """
    classes = _extract_class_names(body)
    title = _extract_title_tag(body)
    if classes:
        basis = "classes:" + "|".join(sorted(classes))
    else:
        basis = "structure:" + _json_skeleton(body)
    digest = hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()
    return digest, len(classes), title


# ── paths and small helpers ──────────────────────────────────────────────────

def _slug(text, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:limit] or "default"


def group_id(kind, selector=None, stage=None):
    """Filesystem-safe identifier for a ``(kind, selector)`` capture group."""
    return f"{_slug(kind, 24)}--{_slug(selector or stage, 48)}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative(outbox_dir, path) -> str:
    return os.path.relpath(path, outbox_dir).replace(os.sep, "/")


def _write_atomic(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    temp_path = path + _TEMP_SUFFIX
    with open(temp_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _manifest_entry(outbox_dir, path, **fields) -> dict:
    raw = open(path, "rb").read()
    entry = {
        "path": _relative(outbox_dir, path),
        "kind": "failure",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "mtime": datetime.fromtimestamp(
            os.path.getmtime(path), timezone.utc).isoformat(),
    }
    entry.update(fields)
    return entry


# ── group state ──────────────────────────────────────────────────────────────

def _new_group(gid) -> dict:
    return {
        "group": gid,
        "sample_count": 0,
        "total_misses": 0,
        "first_seen": None,
        "last_seen": None,
        "digests_truncated": False,
        "digests": {},
    }


def _group_state_path(gid) -> str:
    return os.path.join(failures_dir(_outbox_dir), gid, GROUP_STATE_NAME)


def _apply_legacy_group_keys(loaded: dict) -> None:
    """Normalise a ``_group.json`` written before the Batch 7 key rename.

    The old file stores ``variants_truncated`` at the top level and
    ``count``/``samples`` inside each digest entry; this build stores
    ``digests_truncated`` and ``misses``/``samples_written``. Each old key is
    mapped only when its replacement is absent, then removed, so a state file
    is migrated in place the first time it is read and a file this build wrote
    is left untouched.
    """
    if "digests_truncated" not in loaded and "variants_truncated" in loaded:
        loaded["digests_truncated"] = loaded["variants_truncated"]
    loaded.pop("variants_truncated", None)
    digests = loaded.get("digests")
    if not isinstance(digests, dict):
        return
    for entry in digests.values():
        if not isinstance(entry, dict):
            continue
        if "misses" not in entry and "count" in entry:
            entry["misses"] = entry["count"]
        entry.pop("count", None)
        if "samples_written" not in entry and "samples" in entry:
            entry["samples_written"] = entry["samples"]
        entry.pop("samples", None)


def _load_group(gid) -> dict:
    """Group state, from disk when present so caps survive a restart."""
    cached = _groups.get(gid)
    if cached is not None:
        return cached

    group = _new_group(gid)
    path = _group_state_path(gid)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            _apply_legacy_group_keys(loaded)
            group.update(loaded)
    # No state file yet is the normal first-run case; the group is rebuilt from
    # the samples on disk below.
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        # A damaged state file must not disable capture; rebuild from the samples.
        logging.warning("Could not read capture group %s (%s); rebuilding it", path, exc)

    if not group["digests"]:
        _reconstruct_from_samples(gid, group)
    _groups[gid] = group
    return group


def _reconstruct_from_samples(gid, group) -> None:
    """Rebuild per-shape counts by reading the sample records we already wrote.

    Only needed when the state file is missing or damaged. Reading the records
    (rather than a summary) keeps this honest: the samples on disk are the
    authority for what was captured.
    """
    group_dir = os.path.join(failures_dir(_outbox_dir), gid)
    try:
        names = sorted(os.listdir(group_dir))
    except OSError:
        return
    for name in names:
        if not name.endswith(".json") or name == GROUP_STATE_NAME:
            continue
        try:
            with open(os.path.join(group_dir, name), "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        # A page capture keys by the body's class digest. An image failure has no
        # body and keys by the failure signature's digest, which new records
        # carry as `failure_digest`; a record written before that field existed
        # stored the same digest in `shape.class_digest`, so the fallback keeps
        # an outbox from the old writer countable.
        shape = record.get("shape") or {}
        digest = shape.get("class_digest") or record.get("failure_digest")
        if not digest:
            continue
        entry = group["digests"].setdefault(
            digest, {"misses": 0, "samples_written": 0, "first_seen": None, "last_seen": None})
        entry["samples_written"] += 1
        group["sample_count"] += 1


def _select_sample_slot(gid, digest, now):
    """Pick the sample slot for this shape, counting the miss either way.

    Returns the 1-based sample number to write, or ``None`` when the per-shape
    or per-group cap has been reached and only the counter moves. Mutates the
    in-memory group state; the caller flushes it, so the flush happens next to
    the write whose success it describes.

    Shared by page captures (whose digest comes from the body's shape) and image
    captures (whose digest comes from the failure signature), so the bounding
    rules cannot drift apart between the two.
    """
    group = _load_group(gid)
    if group["first_seen"] is None:
        group["first_seen"] = now
    group["last_seen"] = now
    group["total_misses"] += 1

    variant = group["digests"].get(digest)
    if variant is None:
        if len(group["digests"]) >= MAX_DIGESTS_PER_GROUP:
            # Stop capturing new shapes for this group; keep counting.
            group["digests_truncated"] = True
            return None
        variant = {"misses": 0, "samples_written": 0, "first_seen": now, "last_seen": now}
        group["digests"][digest] = variant
    elif variant["samples_written"] >= SAMPLES_PER_DIGEST:
        variant["misses"] += 1
        variant["last_seen"] = now
        return None

    variant["misses"] += 1
    variant["last_seen"] = now
    variant["samples_written"] += 1
    group["sample_count"] += 1
    return variant["samples_written"]


def _flush_group(gid) -> None:
    """Persist a group's counters and register its state file.

    Written on *every* miss, not throttled: the counters are the only record of
    how large a break is, and a throttled counter both under-reports after a
    restart and can move backwards. What must stay bounded is the number of
    files, which the shape cap handles; one small atomic write per miss at the
    scrape rate (one per web delay) is not a cost worth trading that for.
    """
    group = _groups.get(gid)
    if not group:
        return

    path = _group_state_path(gid)
    payload = dict(group)
    payload["updated_at"] = _utc_now_iso()
    _write_atomic(path, json.dumps(payload, indent=2, sort_keys=True))
    update_manifest(_outbox_dir, _manifest_entry(
        _outbox_dir, path,
        role="group",
        group=gid,
        sample_count=group["sample_count"],
        total_misses=group["total_misses"],
        first_seen=group["first_seen"],
        last_seen=group["last_seen"],
        digests_truncated=group["digests_truncated"],
    ))


def flush() -> None:
    """Persist every known group. Called on daemon shutdown."""
    with _lock:
        for gid in list(_groups):
            try:
                _flush_group(gid)
            except OSError as exc:
                logging.warning("Could not flush capture group %s: %s", gid, exc)


# ── capture ──────────────────────────────────────────────────────────────────

def record_failure(kind, stage=None, workshop_id=None, selector=None,
                   http_status=None, final_url=None, body=None,
                   content_type=None, context=None):
    """Record one failure. Returns the capture record, or None if not captured.

    Never raises: capture is diagnostic, and a diagnostic that can break the
    scrape loop is worse than no diagnostic.
    """
    if not is_enabled():
        return None
    try:
        return _record_failure(kind, stage, workshop_id, selector, http_status,
                               final_url, body, content_type, context)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logging.warning("Failure capture failed (scrape loop unaffected): %s", exc)
        return None


def _record_failure(kind, stage, workshop_id, selector, http_status,
                    final_url, body, content_type, context):
    if body is None:
        raw = b""
    elif isinstance(body, str):
        raw = body.encode("utf-8", "replace")
    else:
        raw = bytes(body)
    stripped = _strip_script_and_style(raw)
    retained = stripped[:MAX_BODY_BYTES]
    retention = {
        "truncated": len(stripped) > MAX_BODY_BYTES,
        "noise_stripped": len(stripped) != len(raw),
    }

    digest, class_count, title_tag = describe_shape(retained)
    gid = group_id(kind, selector, stage)
    now = _utc_now_iso()

    with _lock:
        sample_number = _select_sample_slot(gid, digest, now)
        if sample_number is None:
            _flush_group(gid)
            return None
        record = _write_sample(
            gid, digest, sample_number, raw, retained, retention, kind, stage,
            workshop_id, selector, http_status, final_url, content_type,
            class_count, title_tag, context, now)
        _flush_group(gid)
        return record


def _write_sample(gid, digest, sample_number, raw, retained, retention, kind, stage,
                  workshop_id, selector, http_status, final_url, content_type,
                  class_count, title_tag, context, now):
    group_dir = os.path.join(failures_dir(_outbox_dir), gid)
    stem = f"{digest[:8]}-{sample_number}"
    body_path = os.path.join(group_dir, stem + ".body")
    record_path = os.path.join(group_dir, stem + ".json")

    _write_atomic(body_path, retained)

    record = {
        "kind": kind,
        "stage": stage,
        "workshop_id": workshop_id,
        "selector": selector,
        "http_status": http_status,
        "final_url": final_url,
        "content_type": content_type,
        "body_file": _relative(_outbox_dir, body_path),
        "full_body_bytes": len(raw),
        "body_truncated": retention["truncated"],
        "body_scripts_stripped": retention["noise_stripped"],
        "full_body_sha256": hashlib.sha256(raw).hexdigest(),
        "shape": {
            "class_digest": digest,
            "class_count": class_count,
            "title_tag": title_tag,
        },
        "captured_at": now,
        "app_version": app_version(),
        "context": context or {},
    }
    _write_atomic(record_path, json.dumps(record, indent=2, sort_keys=True))

    update_manifest(_outbox_dir, _manifest_entry(
        _outbox_dir, record_path, role="sample", group=gid, class_digest=digest,
        workshop_id=workshop_id))
    update_manifest(_outbox_dir, _manifest_entry(
        _outbox_dir, body_path, role="body", group=gid, class_digest=digest))
    return record
