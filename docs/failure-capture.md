# Failure Capture

The scraper meets input it cannot handle in four places: a Workshop page whose
description selector no longer matches, an API response that is not JSON, an API
status code with no branch, and an image download that fails. Before this
existed, each of those collapsed into a generic failure — or worse, into a
success — and the response body was dropped, so no regression test could ever be
built from a real break. The image case is the same gap with a different shape:
16,400 failures had nothing but a warning, and no body worth keeping.

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
| `web_item_missing` | `web_scrape` | The Workshop reports the item is gone (HTTP 404/410, or its item-error wording on an HTTP 200 page) | `web_worker.py` |
| `api_unparsed_body` | `api_fetch` | The API response body is not JSON | `steam_api.py` |
| `api_unhandled_status` | `api_fetch` | A status other than 200, 404 or 500 | `daemon.py` |
| `image_download_failed` | `image_download` | The download returned a non-200 status, raised a transport error, or served a MIME type that could not be classified | `image_worker.py` |

All five are **additive**. The failure site returns or raises exactly as it did
before; the capture is recorded on the way past. `api_unparsed_body` re-raises the
`ValueError`, so the existing handler still reports its 500 — the body is simply
no longer thrown away. `api_unhandled_status` records the payload but still falls
through to the success path, because changing that flow is a separate decision.

## Image downloads

Image downloads are the case the capture was added for: 16,400 recorded failures
previously left nothing but a one-line warning, so the 404 loop in
[code-issues.md](code-issues.md) could not be reviewed. The rule differs from the
web capture in one place, because the artefact already exists:

* **Failures are always captured** whenever `daemon.outbox_dir` is set — the HTTP
  status, the response headers, the URL (requested and final), the content type
  and length, and the exception text when there was no response.
* **Successes are captured only under the `daemon.capture_image_downloads` debug
  switch** — the same metadata, plus the number of bytes written and the path of
  the saved file, so a good result can be correlated with the image on disk. It
  is a separate switch from `daemon.capture_web_downloads`: that one keeps whole
  bodies unbounded, and an owner reviewing images should not have to collect
  pages to do it.
* **The bytes are never captured.** A successful download already wrote the image
  into its `images/` bucket and that file is the artefact; copying it into the
  outbox would store every image twice. `capture.record_image_download` has no
  parameter that carries a body — a parameter that does not exist cannot be
  passed by accident. Image capture records therefore contain **no `body_file`**
  and no `.body` file is ever written for them.

Image failures are grouped by a **failure signature** — the status code, the
content type and the exception *class*, never the exception message — so a 404
loop costs a bounded number of files while a genuinely different failure (a 404
and a transport error, say) still produces its own evidence. The group counters
carry the scale.

## Web downloads

Every Steam community web pull is saved, whole, while the
`daemon.capture_web_downloads` debug switch is set. Unlike the failure capture
this is not about what broke: it is about what a *working* exchange looks like,
for the three requests whose shape matters and which a failure-only capture can
never show.

| `kind` | Request | Caller |
|---|---|---|
| `item_page` | `GET` the item's `filedetails` page | `web_worker.py` |
| `subscriptions_page` | `GET` one page of the owner's subscriptions | `subscription_sync.py` |
| `subscribe` | `POST` `/sharedfiles/subscribe`, and Steam's answer | `webserver.py` |

Each record holds **both sides of the exchange**. The request is the method, URL,
headers, cookie jar and form data *as they were sent* — the callers carry the
values they built (the item scrape returns them from `scrape_extended_details`)
rather than re-deriving them — and the response is the status, the final URL, the
headers and the body. The body is kept whole in a sibling `.body` file, with the
same `_relative` registration and the same `auth_markers` / `g_steamID`
diagnostics the failure capture's HTML bodies carry. A subscriptions-page record
also names its `appid` and `page`; every record names its `workshop_id` where
there is one.

**No credential value is ever written.** `capture.elide_secrets` replaces the
value of every cookie — the *names* stay, because knowing that
`steamLoginSecure` and `browserid` were sent is the diagnostic point — the
`sessionid` form field, and the `Cookie`, `Set-Cookie` and `Authorization`
header values with `***`. Those literal values are then scrubbed from everything
the recorder writes, the JSON record and the whole-body file both, so a token
echoed into a response body or a URL cannot leak either. Values shorter than
`MIN_SCRUB_LENGTH` are still elided where they are recognised, but are not used
for that whole-file scrub: a real cookie jar carries `timezoneOffset=0`, and
replacing every `0` would leave a capture that describes nothing. The elider
never raises: capture is diagnostic, and a diagnostic that can break the request
it describes is worse than no diagnostic.

It is deliberately unbounded — no cap, no dedup, no thinning — for the same
reason the item-page capture always was: a sample trimmed before anyone has
looked at it just means collecting the evidence twice. The directory therefore
grows for as long as the switch is left on; there is no retention anywhere in
the outbox.

Two surfaces are **not** covered by this switch:

* the Steam Web API calls in `src/steam_api.py` — a different surface, which has
  its own failure capture;
* image downloads, which have the separate `daemon.capture_image_downloads`
  switch.

The web server is a separate process from the daemon, so it reads the switch
from the same config itself (`init_webserver`); both processes write into the one
`<outbox>/web_downloads/`, and the multi-process caveat under *Concurrency*
below applies to that directory as much as to the manifest.

## What a capture holds

One JSON record per sample:

| Field | Meaning |
|---|---|
| `kind`, `stage` | Which failure, and which step of the pipeline |
| `workshop_id` | The item being processed |
| `selector` | The CSS selector that failed, for `web_selector_miss` (absent for `web_item_missing`, which is a missing item rather than a broken selector) |
| `http_status`, `final_url`, `content_type` | How the response arrived |
| `body_file`, `body_bytes`, `body_sha256` | The retained bytes, and the hash and length of the **full** response, so a re-fetch can be matched against it |
| `body_truncated` | The 64 KB cap cut the retained content |
| `body_noise_stripped` | `<script>` and `<style>` bodies were removed before capping |
| `shape` | `class_digest`, `class_count`, `title_tag` — see below |
| `captured_at`, `app_version` | When, and which build |

`shape.class_digest` is a hash of the sorted set of CSS class names in the retained
content, or of a JSON skeleton when the body has no classes (the API path). Class names
rather than page text, so rotating text does not change the shape. It is recorded,
not interpreted: nothing classifies pages by it yet.

Retention removes `<script>` and `<style>` bodies before applying the cap. Capping the
raw head instead kept whatever loaded first, and a modern Steam page is mostly script:
on the live capture that prompted this, 64 KB of a 303 KB page was script and stylesheet
tags, `class_count` was 2, and the artefact contained nothing that identified the page —
nor did a digest of it describe the document. A body already under the cap strips to the
same digest as before, so existing variants are not re-keyed.

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
<outbox_dir>/web_downloads/<stamp>-<kind>-<id>.json  a web pull's record
<outbox_dir>/web_downloads/<stamp>-<kind>-<id>.body  its response body
<outbox_dir>/image_downloads/<stamp>-<id>.json     a successful image download (metadata only)
```

Every one of those files is registered as its own `manifest.json` entry with
`kind: "failure"` and a `role` of `group`, `sample` or `body`. The puller
transfers one file per entry, filters `--only` on `kind`, and only gunzips when
`compression` is declared — so these plain, uncompressed entries are collected by
the existing tooling with no changes.

Image failures live in the same `failures/` tree under the
`image-download-failed--image-download` group and follow the same roles, except
that they have no `body` entry: there is no body to transfer. Successful image
downloads live in `<outbox_dir>/image_downloads/`, a sibling of the web capture's
`<outbox_dir>/web_downloads/`, and are registered with `kind: "image_download"`
and `role: "record"`. Web-download records and their bodies are registered with
`kind: "web_download"` and a `role` of `record` or `body` — a kind of their own,
so a puller can collect them without also pulling the failure tree.

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
them. A capture with no `body_file` is skipped rather than promoted: image
failures are metadata-only by design, and turning one into an empty fixture would
produce a test that asserts nothing.

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
