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
  kept and no more; a group tracks at most ``MAX_VARIANTS_PER_GROUP`` distinct
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
MAX_VARIANTS_PER_GROUP = 5

# Bytes of a response body written to the .body file.
MAX_BODY_BYTES = 64 * 1024


GROUP_STATE_NAME = "_group.json"
_TEMP_SUFFIX = ".tmp"

_lock = threading.RLock()
_outbox_dir = None
_groups = {}
_app_version_cache = None

# Ordinary web scrapes saved while `capture_web_scrapes` is set. Not failures:
# this is evidence about what a working page looks like, to identify the
# signed-in markup that tells us the session is still good.
#
# Unbounded, and the body is kept whole. This is a debugging switch that is on
# for a session or two, so the cost is accepted in exchange for not having to
# collect the evidence twice because a sample was thinned before anyone looked
# at it. The failure capture above is the opposite case: it runs for weeks, so
# its caps and its size limits stay.
_scrape_capture = False

# Every image download saved while `capture_image_downloads` is set, one
# metadata-only record each. Separate from `capture_web_scrapes` because that
# switch keeps whole page bodies unbounded; an owner reviewing images should not
# have to collect pages to do it.
_image_capture = False

SCRAPES_DIR_NAME = "scrapes"
IMAGE_DOWNLOADS_DIR_NAME = "image_downloads"

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
_NOISE_RE = re.compile(rb"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


def _strip_noise(raw: bytes) -> bytes:
    """Drop script and style bodies, leaving the markup around them.

    Purely a size measure: the elements are replaced by an empty pair so the
    document's structure stays readable and the same input always strips the
    same way.
    """
    if not raw:
        return raw
    return _NOISE_RE.sub(rb"<\1/>", raw)


# ── configuration ────────────────────────────────────────────────────────────

def configure(outbox_dir, web_scrape_capture=False, image_capture=False):
    """Enable capture under ``<outbox_dir>/failures``. ``None`` disables it.

    ``web_scrape_capture`` additionally saves *every* ordinary web scrape, into
    ``<outbox_dir>/scrapes``, success or failure, with the response body kept
    whole. That answers a question the failure capture cannot: what a page looks
    like when a scrape works, which is what identifies the signed-in markup.

    ``image_capture`` additionally saves *every* image download, into
    ``<outbox_dir>/image_downloads``, as metadata and headers only — never the
    image bytes, which already live in the images bucket.
    """
    global _outbox_dir, _scrape_capture, _image_capture
    with _lock:
        _outbox_dir = outbox_dir or None
        _scrape_capture = bool(web_scrape_capture) and bool(_outbox_dir)
        _image_capture = bool(image_capture) and bool(_outbox_dir)
        _groups.clear()
        if _outbox_dir:
            logging.info("Failure capture enabled: %s", failures_dir(_outbox_dir))
            if _scrape_capture:
                logging.info(
                    "Web-scrape capture enabled: saving every scrape, whole body, to %s. "
                    "This is a debugging switch — turn it off when done.", scrapes_dir(_outbox_dir),
                )
            if _image_capture:
                logging.info(
                    "Image-download capture enabled: saving every image download's "
                    "metadata (status, headers, saved path — never the bytes) to %s. "
                    "This is a debugging switch — turn it off when done.",
                    image_downloads_dir(_outbox_dir),
                )


def is_enabled() -> bool:
    return _outbox_dir is not None


def failures_dir(outbox_dir) -> str:
    return os.path.join(outbox_dir, "failures")


def scrapes_dir(outbox_dir) -> str:
    return os.path.join(outbox_dir, SCRAPES_DIR_NAME)


def image_downloads_dir(outbox_dir) -> str:
    return os.path.join(outbox_dir, IMAGE_DOWNLOADS_DIR_NAME)


def web_scrape_capture_active() -> bool:
    """Whether ordinary scrapes are being saved. Read before each scrape.

    Also decides whether the scrape asks for its body: a successful scrape
    discards it by default, and there is nothing to capture without it.
    """
    with _lock:
        return bool(_outbox_dir) and _scrape_capture


def image_capture_active() -> bool:
    """Whether every image download is being saved. Read before each download."""
    with _lock:
        return bool(_outbox_dir) and _image_capture


def record_web_scrape(workshop_id, url, scrape_data) -> bool:
    """Save one web scrape, success or failure. Unbounded, on purpose.

    Deliberately not deduplicated: the question is what a *working* page looks
    like, and one sample of that is worth more than several of the same failure.
    Both states are needed to tell the two apart, which is why the successes are
    saved too — the failure capture by definition only ever holds misses. By the
    same argument nothing is thinned or dropped: a sample trimmed before anyone
    has looked at it just means collecting the evidence twice.

    That makes this a *session* switch rather than a resident one. There is no
    budget here and no retention anywhere in the outbox, so the directory grows
    for as long as the switch is left on — measured live at about 73 KB per
    scrape, roughly a gigabyte a day at the web worker's current pace. The
    failure capture is the opposite case: it runs for weeks, so its caps stay.
    """
    global _scrape_capture
    if not scrape_data:
        return False
    with _lock:
        if not _outbox_dir or not _scrape_capture:
            return False
        outbox = _outbox_dir

    body = scrape_data.get("body")
    hit = scrape_data.get("description") is not None
    stamp = _utc_now_iso().replace(":", "-")
    stem = f"{stamp}-{workshop_id}"
    directory = scrapes_dir(outbox)
    os.makedirs(directory, exist_ok=True)
    body_path = os.path.join(directory, stem + ".body")
    record_path = os.path.join(directory, stem + ".json")

    raw = b""
    if body is not None:
        raw = body.encode("utf-8", "replace") if isinstance(body, str) else bytes(body)
        # Whole and unstripped, deliberately: a debugging capture that drops the
        # <script> blocks also drops g_steamID, which is the most direct answer
        # to "is this page signed in?".
        _write_atomic(body_path, raw)

    record = {
        "kind": "web_scrape",
        "stage": "web_scrape",
        "workshop_id": workshop_id,
        "url": url,
        "scraped_ok": hit,
        "http_status": scrape_data.get("http_status"),
        "final_url": scrape_data.get("final_url"),
        "body_file": _relative(outbox, body_path) if raw else None,
        "body_bytes": len(raw),
        "body_complete": True,
        "body_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "auth_markers": auth_markers(body),
        "g_steamID": steam_id_from(body),
        "captured_at": _utc_now_iso(),
        "app_version": app_version(),
    }
    _write_atomic(record_path, json.dumps(record, indent=2).encode("utf-8"))
    update_manifest(outbox, {
        "path": _relative(outbox, record_path), "kind": "scrape",
        "bytes": len(json.dumps(record)), "role": "record",
        "scraped_ok": hit, "workshop_id": workshop_id,
    })
    if raw:
        # The body has to be registered too, or the puller never sees it — the
        # record used to arrive without the page it describes.
        update_manifest(outbox, {
            "path": _relative(outbox, body_path), "kind": "scrape",
            "bytes": len(raw), "role": "body",
            "scraped_ok": hit, "workshop_id": workshop_id,
        })
    return True


# ── image downloads ──────────────────────────────────────────────────────────

def record_image_download(workshop_id, url, ok, *, http_status=None, final_url=None,
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
        if ok:
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
        sample_number = _select_variant(gid, digest, now)
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
        "body_bytes": 0,
        "signature": signature,
        "shape": {"class_digest": digest, "class_count": 0, "title_tag": None},
        "captured_at": now,
        "app_version": app_version(),
    }
    _write_atomic(record_path, json.dumps(record, indent=2, sort_keys=True).encode("utf-8"))
    update_manifest(_outbox_dir, _manifest_entry(
        _outbox_dir, record_path, role="sample", group=gid, class_digest=digest,
        workshop_id=workshop_id))
    return record


def _record_image_success(workshop_id, url, http_status, final_url, headers,
                          content_type, content_length, bytes_written, saved_path,
                          now) -> bool:
    with _lock:
        if not _outbox_dir or not _image_capture:
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
        "variants_truncated": False,
        "digests": {},
    }


def _group_state_path(gid) -> str:
    return os.path.join(failures_dir(_outbox_dir), gid, GROUP_STATE_NAME)


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
        digest = (record.get("shape") or {}).get("class_digest")
        if not digest:
            continue
        entry = group["digests"].setdefault(
            digest, {"count": 0, "samples": 0, "first_seen": None, "last_seen": None})
        entry["samples"] += 1
        group["sample_count"] += 1


def _select_variant(gid, digest, now):
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
        if len(group["digests"]) >= MAX_VARIANTS_PER_GROUP:
            # Stop capturing new shapes for this group; keep counting.
            group["variants_truncated"] = True
            return None
        variant = {"count": 0, "samples": 0, "first_seen": now, "last_seen": now}
        group["digests"][digest] = variant
    elif variant["samples"] >= SAMPLES_PER_DIGEST:
        variant["count"] += 1
        variant["last_seen"] = now
        return None

    variant["count"] += 1
    variant["last_seen"] = now
    variant["samples"] += 1
    group["sample_count"] += 1
    return variant["samples"]


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
        variants_truncated=group["variants_truncated"],
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
    stripped = _strip_noise(raw)
    retained = stripped[:MAX_BODY_BYTES]
    retention = {
        "truncated": len(stripped) > MAX_BODY_BYTES,
        "noise_stripped": len(stripped) != len(raw),
    }

    digest, class_count, title_tag = describe_shape(retained)
    gid = group_id(kind, selector, stage)
    now = _utc_now_iso()

    with _lock:
        sample_number = _select_variant(gid, digest, now)
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
        "body_bytes": len(raw),
        "body_truncated": retention["truncated"],
        "body_noise_stripped": retention["noise_stripped"],
        "body_sha256": hashlib.sha256(raw).hexdigest(),
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
