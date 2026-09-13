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

def configure(outbox_dir):
    """Enable capture under ``<outbox_dir>/failures``. ``None`` disables it."""
    global _outbox_dir
    with _lock:
        _outbox_dir = outbox_dir or None
        _groups.clear()
        if _outbox_dir:
            logging.info("Failure capture enabled: %s", failures_dir(_outbox_dir))


def is_enabled() -> bool:
    return _outbox_dir is not None


def failures_dir(outbox_dir) -> str:
    return os.path.join(outbox_dir, "failures")


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
                variant = None
                capture = False
            else:
                variant = {"count": 0, "samples": 0,
                           "first_seen": now, "last_seen": now}
                group["digests"][digest] = variant
                capture = True
        else:
            capture = variant["samples"] < SAMPLES_PER_DIGEST
            if not capture:
                variant["count"] += 1
                variant["last_seen"] = now

        if variant is not None and capture:
            variant["count"] += 1
            variant["last_seen"] = now
            variant["samples"] += 1
            group["sample_count"] += 1
            record = _write_sample(
                gid, digest, variant["samples"], raw, retained, retention, kind, stage,
                workshop_id, selector, http_status, final_url, content_type,
                class_count, title_tag, context, now)
            _flush_group(gid)
            return record

        _flush_group(gid)
        return None


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
