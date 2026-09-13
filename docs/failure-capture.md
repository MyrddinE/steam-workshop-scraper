# Failure Capture

The scraper meets input it cannot handle in three places: a Workshop page whose
description selector no longer matches, an API response that is not JSON, and an
API status code with no branch. Before this existed, each of those collapsed into
a generic failure — or worse, into a success — and the response body was dropped,
so no regression test could ever be built from a real break.

Capture writes that evidence to the pull-outbox and registers it in the same
manifest the database snapshots use. It is described here as behaviour; the
reasoning behind the bounds is in the module docstring of `src/capture.py`.

## Enabling it

Capture is **off** unless `daemon.outbox_dir` (or the legacy `backup_dir`) is set,
exactly like the database backup — but it needs no `backup_interval_seconds`. With
no outbox configured, `capture.record_failure` returns immediately and touches
nothing, so the feature is a deliberate switch and tests stay hermetic.

## What is captured

| Kind | Stage | Trigger | Where |
|---|---|---|---|
| `web_selector_miss` | `web_scrape` | The page loaded but `DESCRIPTION_SELECTOR` did not match | `web_worker.py` |
| `api_unparsed_body` | `api_fetch` | The API response body is not JSON | `steam_api.py` |
| `api_unhandled_status` | `api_fetch` | A status other than 200, 404 or 500 | `daemon.py` |

All three are **additive**. The failure site returns or raises exactly as it did
before; the capture is recorded on the way past. `api_unparsed_body` re-raises the
`ValueError`, so the existing handler still reports its 500 — the body is simply
no longer thrown away. `api_unhandled_status` records the payload but still falls
through to the success path, because changing that flow is a separate decision.

## What a capture holds

One JSON record per sample:

| Field | Meaning |
|---|---|
| `kind`, `stage` | Which failure, and which step of the pipeline |
| `workshop_id` | The item being processed |
| `selector` | The CSS selector that failed, for the web kind |
| `http_status`, `final_url`, `content_type` | How the response arrived |
| `body_file`, `body_bytes`, `body_sha256`, `body_truncated` | The raw bytes, capped at `MAX_BODY_BYTES` (64 KB); the hash and length describe the full body |
| `shape` | `class_digest`, `class_count`, `title_tag` — see below |
| `captured_at`, `app_version` | When, and which build |

`shape.class_digest` is a hash of the sorted set of CSS class names in the body,
or of a JSON skeleton when the body has no classes (the API path). Class names
rather than page text, so rotating text does not change the shape. It is recorded,
not interpreted: nothing classifies pages by it yet.

## Bounds

A persistent break must cost a bounded number of files. Two caps do that, and both
are constants in `src/capture.py`:

* Captures group by `(kind, selector)`. Within a group, the first
  **`SAMPLES_PER_DIGEST`** (3) samples of each distinct `class_digest` are kept and
  no more.
* A group tracks at most **`MAX_VARIANTS_PER_GROUP`** (5) distinct digests. Past
  that, a new shape is counted but not written — otherwise a page whose content
  rotates would produce a new digest per fetch and the per-shape cap would mean
  nothing. The group records `variants_truncated: true` when this happens.

Every miss increments `total_misses` whether or not it produced a file. Those
counters, not the samples, are what convey the size of a break. They live on the
manifest's **group** entry (`sample_count`, `total_misses`, `first_seen`,
`last_seen`), because a sample file cannot carry a group counter that stays true.

State is reloaded from disk at startup, so the caps survive a restart. The group
state file is rewritten on every miss rather than throttled: a throttled counter
under-reports after a restart and can move backwards, and what has to stay bounded
is the file count, not the write count.

## Storage layout

```
<outbox_dir>/failures/<group>/_group.json     counters, and per-shape state
<outbox_dir>/failures/<group>/<digest8>-<n>.json   the capture record
<outbox_dir>/failures/<group>/<digest8>-<n>.body   the raw bytes
```

Every one of those files is registered as its own `manifest.json` entry with
`kind: "failure"` and a `role` of `group`, `sample` or `body`. The puller
transfers one file per entry, filters `--only` on `kind`, and only gunzips when
`compression` is declared — so these plain, uncompressed entries are collected by
the existing tooling with no changes.

## Selector misses change the queue

`scrape_extended_details` returns `{"description": None, "tags": []}` when the
description selector does not match — a truthy value. That used to be taken as
success, writing `extended_description = NULL` and `needs_web_scrape = 0`, so the
item was recorded as permanently scraped with nothing to show for it and was never
retried.

A miss is now a failure. The artefact is captured, and the item is stepped down the
queue by one, floored at one:

```sql
UPDATE workshop_items SET needs_web_scrape = MAX(1, needs_web_scrape - 1)
```

so the item stays queued, sinks below current work, and is never zeroed.

This follows the *shape* of the `needs_image` decay in `image_worker.py` — a direct
arithmetic update rather than a `MAX(current, new)` priority bump — but differs from
it in two deliberate ways. `needs_image` floors at 0, which takes the item out of
its queue; a web-scrape item must stay queued, so this floors at 1. And the image
worker also raises `api_priority` to 2 on failure, which the web worker does not do
here: the request succeeded, so this is not a network failure, and slowing down
would not make a broken selector match.

This is separate from the priority-decay issue listed in
[code-issues.md](code-issues.md): the one queue with a runtime staleness sweep is
still `api_priority` alone.

## Promotion to regression tests

A capture that stays in the outbox is an artefact nobody runs. `src/capture_promote.py`
turns captures into fixtures and a replay test:

```
python3 -m src.capture_promote --from <outbox>/failures
```

For each capture it writes `tests/fixtures/<area>/<name>.<ext>` plus a
`.meta.json` sidecar, then regenerates `tests/test_ingest_regressions.py`
parametrized over every fixture found. Credentials (`key=`, `sessionid`,
`steamLoginSecure`, `api_key`) are scrubbed on the way in, since a body may carry
them.

The generated assertions are deliberately weak: they prove the input is handled
gracefully and the payload is preserved, and nothing more. A generated test that
pinned today's output would cement today's bug. Tightening one case into a real
assertion is a human decision, and is the step that turns a capture into a
regression test.

## Concurrency

`update_manifest` reads, modifies and rewrites one JSON file, so it assumes a
single writer. The backup thread and the capture writer are both threads of the
daemon and both publish into the same manifest, so the read-modify-write is now
serialised by a module lock in `backup.py`. Two separate **processes** sharing one
outbox would still need a real file lock, which is not implemented.

## Related

* [data-pipeline.md](data-pipeline.md) — where in the pipeline these failures occur.
* [web-ui.md](web-ui.md) — the queue the web scraper drains.
* [config-security.md](config-security.md) — `daemon.outbox_dir`.
