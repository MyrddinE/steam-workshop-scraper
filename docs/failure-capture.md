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
| `web_description_absent` | `web_scrape` | The item's own page was served but its description block is absent, so the absence is permanent | `web_worker.py` |
| `web_item_missing` | `web_scrape` | The Workshop reports the item is gone (HTTP 404/410, or its item-error wording on an HTTP 200 page) | `web_worker.py` |
| `web_gated` | `web_scrape` | An age check, a sign-in wall or Steam's error shell withheld the item's page | `web_worker.py` |
| `web_unknown` | `web_scrape` | A served page that is neither the item's nor a recognised condition | `web_worker.py` |
| `api_unparsed_body` | `api_fetch` | The API response body is not JSON | `steam_api.py` |
| `api_unparseable_tags` | `item_write` | The API's tags payload is neither JSON nor a Python-repr list | `database.py` |
| `api_unhandled_status` | `api_fetch` | A status other than 200, 404 or 500 | `daemon.py` |
| `image_download_failed` | `image_download` | The download returned a non-200 status, raised a transport error, or served a MIME type that could not be classified | `image_worker.py` |

Each kind names the cause the worker actually determined, because the distinction
is the point of the tree. `web_selector_miss` is **no longer emitted**: it was the
worker's default label, so a gated page, an unattributable page and a page whose
item simply has no description were all filed under it. `src/capture_promote.py`
still maps it to `fixtures/web/`, so an outbox written by an earlier build
promotes rather than landing in `fixtures/other/`.

All the captures are **additive**. The failure site returns or raises exactly as it
did before; the capture is recorded on the way past. `api_unparsed_body` re-raises
the `ValueError`, so the existing handler still reports its 500 — the body is
simply no longer thrown away. `api_unhandled_status` records the payload but still
falls through to the success path, because changing that flow is a separate
decision. `api_unparseable_tags` keeps the unparseable payload and writes the item
with no tags.

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
  bodies, and an owner reviewing images should not have to collect pages to do
  it.
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
carry the scale. The record writes the signature as `signature` and its hash as
`failure_digest`; that hash, not `shape.class_digest`, is the group's per-shape
key. The manifest entry for an image sample carries the same `failure_digest`.

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

It is deliberately not thinned while it is on — no cap, no dedup — for the same
reason the item-page capture always was: a sample trimmed before anyone has
looked at it just means collecting the evidence twice. What bounds the directory
is **age, not volume**: housekeeping prunes a debug capture once it is
`DEBUG_CAPTURE_RETENTION_DAYS` old (see *Retention* below). The failure capture
is the opposite case: a failure is evidence, so its caps stay and it is never
pruned by age.

Two surfaces are **not** covered by this switch:

* the Steam Web API calls in `src/steam_api.py` — a different surface, which has
  its own failure capture;
* image downloads, which have the separate `daemon.capture_image_downloads`
  switch.

The web server is a separate process from the daemon, so it reads the switch
from the same config itself (`init_webserver`); both processes write into the one
`<outbox>/web_downloads/`, and the multi-process caveat under *Concurrency*
below applies to that directory as much as to the manifest.

## Web UI trace

The page's JavaScript is otherwise only observable through tests that drive
extracted functions, and the operator cannot open a browser. While the
`daemon.capture_web_ui_trace` debug switch is on, the page records its own
timeline and posts it in batches to `POST /api/ui_trace`; the server writes one
JSON file per batch into `<outbox>/web_ui_trace/` and registers it with
`kind: "ui_trace"`, so the puller collects it with no changes. With the switch
off the route is inert and the page installs nothing — no `fetch` wrapper, no
listener, no buffer.

A trace is a timeline of **actions, the calls they caused and the internal
transitions that explain them**, not a failure sample. A record carries a
session header (page load time, viewport, grid `clientHeight`/`scrollTop`,
build version) and the ordered events:

| Event | What it records |
|---|---|
| `session` | the header above, once per page load |
| `keydown` | the key, the focused element's id/`data-wid`, and whether a handler consumed it |
| `click` | the control's id/`label` and `data-wid` |
| `scroll` | the grid's `scrollTop`/`scrollHeight`/`clientHeight`, throttled |
| `sort_change`, `overlay_change` | the new sort or Subscribed-overlay value |
| `do_search` | entry (reset, offset, filter count, sort, overlay), whether the `loading` guard dropped it, and completion (batch size, new offset, `hasMore`, `loading`) |
| `fetch` | one wrapper around `fetch`: method, path, a summary of the body (filter count, id count, offset — never the whole payload), duration, HTTP status, and the response item count where cheap |
| `observe_next_batch` | the branch taken (already-visible shortcut, observing, skip or reset), `rect.top` beside `window.innerHeight`, the grid geometry, and the cell armed and released |
| `intersection` | `isIntersecting`, the entry's target and whether it matched `_observedCell`, and the entry count |
| `list_poll`, `item_poll` | each poll tick, so an endless poll cannot masquerade as scrolling |
| `jump_to_author`, `pane_open` | a creator jump and its id, and a pane open with its `workshop_id` and whether it was the automatic read-only reset selection |
| `view_restore` | removed with the load-time restore (issue 78): nothing pages on load any more, so there is no restore loop to record |

Every event also carries `loads_since_scroll`, and the page resets it on a user
scroll. That one field answers the question the instrument was built for:
records with a rising `loads_since_scroll` and no `scroll` event between them
are the page loading on its own, which is what distinguishes a runaway from the
user scrolling. The `view_restore` events that used to show the loop behind that
runaway are gone with the loop itself: **issue 78** was the load-time restore,
which could issue up to 80 searches per load, and it was removed rather than
bounded, so a page load is one search and one batch at the top.

**A trace is an instrument, not evidence, and it is denser than a web
download**, so it is bounded on top of the age sweep (see *Bounds* and
*Retention* below). The page never holds the session cookie, but the record is
scrubbed with the same literal credential values as a crash dump
(`src/crash.py`'s collector) on the same principle as `_write_web_download`:
elision that depends on the caller being careful is not elision. The trace is
additive — a trace POST that fails, throws or is refused only stops the tracing,
never the action it describes.

## Sort diagnostic

The same `daemon.capture_web_ui_trace` switch gates a second instrument that
writes **no files and no tree**, covered here because it rides that switch rather
than a capture key of its own. With it on, `GET /api/search_diagnostic` returns a
read-only report on the live database's search-sort path — each expected query
index's actual definition beside the expected one, classified `present`, `missing`
or `wrong_definition` (a same-named index on the wrong column survives
`CREATE INDEX IF NOT EXISTS`, so a name-only check cannot see it), the `EXPLAIN
QUERY PLAN` for the real query under each sort column with a derived `uses_index`
boolean, each score column's non-NULL coverage, whether `sqlite_stat1` exists, and
first/deep page timings — and `init_webserver` logs the same summary once at
startup. It opens the database `mode=ro`, so it is safe beside the running daemon,
and it exists because a "sort X is slow" report cannot be answered from this
repository's schema alone. See
[search-filter.md](search-filter.md#sort-indexes-the-subscriber-score-is-slow-investigation).

## Crash dumps

An unhandled traceback goes to a terminal nobody is reading, so `src/crash.py`
writes it to `<outbox_dir>/crashes/<stamp>-<process>-error<N>.txt` and registers
it in the same manifest, with `kind: "crash"`, for the existing puller. It is
installed by all three entry points (`src/tui.py`, `src/web_runner.py`,
`src/daemon_runner.py`) and covers the three escape routes: `sys.excepthook`, the
worker-thread `threading.excepthook`, and Textual's own `App._handle_exception`,
which the TUI overrides because Textual catches the common case in its message
pump before any hook sees it. The hook that was there before is always still
called, so the console shows exactly what it showed before. The traceback is also
written to the log with `logging.error(..., exc_info=True)`, because the
maintainer reads that file too.

Textual's `_handle_exception` records the error and queues its traceback, but
the write to `sys.__stderr__` happens later, on the exit path, after the driver
has closed. When the run's stderr handle is invalid -- the owner's Windows test
run is the measured case, `OSError: [WinError 6] The handle is invalid` -- that
deferred write fails after the dump is already on disk, and the caller gets the
`OSError` instead of the app's own error. The TUI's override therefore guards
both the delegate call and the deferred render (`_print_error_renderables`): the
console failure is logged as `Console traceback render failed` with its
traceback, never silently swallowed, and the queued renderables are dropped so
the exit path cannot raise them again, while the original error still
propagates. Textual's `_exception` and `_return_code` are set before the write
and are left untouched, and on a console that works the output and its timing
are exactly what they were before.

Installation is deliberately split in two, because logging is the one thing a
crash can take down with it. `crash.install_hooks(process_name)` records the
process name and installs `sys.excepthook` and `threading.excepthook` as the
first statement of each `main()`, ahead of `load_config` and `basicConfig`; the
hooks do not need logging, so a failure raised while the config is read or while
the log handler is being built is still dumped. Before the split, that was the
one startup failure with no destination at all: no log record (the handlers were
the thing being built), no ring buffer, and no dump (the hooks were not installed
yet), leaving the operator's terminal as the only copy. `crash.install(...)` then
runs after logging is configured: it records the config — the outbox destination
and the cookie values to elide — and attaches the ring-buffer handler, which the
entry points' `basicConfig(force=True)` would otherwise drop. Both are
idempotent and the hooks are guarded by identity, so calling the early hook and
then `install` installs the hooks once, chains the previous hook once, and leaves
exactly one ring-buffer handler attached.

Where a dump goes follows the same two-step order. With the outbox known, it is
written straight to `<outbox_dir>/crashes/` and registered in the manifest. A
crash before the config is readable cannot know the outbox, so `_destination`
falls back to the configured log file's directory, and with no log file to the
**application folder** derived from `src/crash.py`'s own location — never the
process working directory, because a daemon under a scheduled task does not run
with the app folder as its cwd — and prints that local path, because a dump the
operator cannot find is not a dump. When `install(config)` runs later the outbox
is known, so it adopts what the fallback caught: it scans the log file's
directory and the application folder (deduplicated, never the outbox itself),
matches only this module's `<stamp>-<process>-error<N>.txt` filenames — an
unrelated `.txt`, or the `.tmp` an interrupted atomic write leaves, is left
alone — moves each one in (`os.replace`, or `shutil.move` across volumes, with a
numbered name on collision so an existing outbox dump is never overwritten) and
registers it in the manifest *after* the move, because an entry pointing at a
file that is not there is the failure the puller cannot recover from. Adoption
is best-effort and idempotent: it never raises, a second call finds nothing left
to move, and one log line per adopted dump names its final path so the operator
can follow the file whose local path was printed at crash time.

One run can report more than one error: Textual calls `_handle_exception` once
per unhandled error and prints only the first in normal mode, so the second
traceback used to be discarded at print time. Every error therefore gets its own
file, numbered in the order the process reported it; `error_occurrence` and
`errors_this_run` in the header say which one it is. `captured_at:` records when
it was written, and the manifest entry's `error_occurrence` names the same count.
For one release after the rename the old header line `timestamp:` and the old
manifest key `error` are written beside the new ones, because the puller and any
dump parser live outside this repository; a parser reading a dump written by an
older build must likewise accept `timestamp:`. Each dump is written
synchronously, because Textual closes its message loop as it exits.

The file is a context header, the full traceback **with each frame's locals**,
and the last ~200 formatted log records. Locals are included because the values
in play are usually the whole answer, and they are guarded three ways:

* Mapping entries -- and local variables -- whose name contains `cookie`,
  `token`, `secret`, `password`, `passwd`, `credential`, `login` or `sessionid`,
  or ends in `key`, are replaced with `***` before rendering. Over-redacting a
  benign `sort_key` costs a little context; under-redacting costs a credential.
* Every literal credential the process knows is scrubbed from the finished text,
  longest first, the same rule the web capture uses: the current cookie set, the
  configured `sessionid`/`steamLoginSecure` in both separator forms, the Steam
  and OpenAI API keys from the config **and** the environment (`load_config`
  strips an env-derived key from the config before saving, so the config alone is
  not enough), and any value registered at runtime through
  `crash.register_secret` -- today a refreshed `steamLoginSecure` wherever it is
  persisted.
* Each value is truncated (~2,000 characters), the locals per frame and the
  whole file are capped (256 KB), and a value whose `repr` raises is skipped
  rather than allowed to break the dump. The caps are recorded in the header.

**The elision is not complete.** It is key-name based and known-value based, so
a credential the process never registered, the config does not hold, and no key
name describes would still be written. That is the accepted cost of including
locals at all, and it is why the dump goes only to the owner's own outbox: it is
pulled by their sync and is never published anywhere.

If no outbox can be determined -- the config may not have loaded, which is
exactly when a crash happens -- the dump goes beside the configured log file if
there is one, else into the working directory, and its path is printed to the
console. A dump the user cannot find is not a dump.

## What a capture holds

One JSON record per sample:

| Field | Meaning |
|---|---|
| `kind`, `stage` | Which failure, and which step of the pipeline |
| `workshop_id` | The item being processed |
| `selector` | The CSS selector that failed. Set only for a `web_selector_miss` capture, which the worker no longer emits: a description-less page, a gate, an unknown page and a missing item are not about the selector, so they record `null` here |
| `http_status`, `final_url`, `content_type` | How the response arrived |
| `body_file`, `full_body_bytes`, `full_body_sha256` | The retained bytes, and the hash and length of the **full** response, so a re-fetch can be matched against it |
| `body_truncated` | The 64 KB cap cut the retained content |
| `body_scripts_stripped` | `<script>` and `<style>` bodies were removed before capping |
| `shape` | `class_digest`, `class_count`, `title_tag` — see below |
| `signature`, `failure_digest` | Image-failure records only: the stable failure signature (status, content type and exception class) and its hash, which is the per-shape key for an image group |
| `captured_at`, `app_version` | When, and which build |

For a body-carrying record, `shape.class_digest` is a hash of the sorted set of CSS
class names in the retained content, or of a JSON skeleton when the body has no
classes (the API path). Class names rather than page text, so rotating text does not
change the shape. It is recorded, not interpreted: nothing classifies pages by it yet.

An image-failure record carries **no body**, so it has no class names and its
`shape.class_digest` is `null`. Its per-shape key is `failure_digest`, the hash of
`signature`; the group-state rebuild reads `failure_digest` and falls back to
`shape.class_digest` for a record written before that field existed, so an outbox
produced by the old writer is still counted. `shape.class_count` stays `0` and
`shape.title_tag` stays `null` for these records, because neither was measured.

Retention removes `<script>` and `<style>` bodies before applying the cap. Capping the
raw head instead kept whatever loaded first, and a modern Steam page is mostly script:
on the live capture that prompted this, 64 KB of a 303 KB page was script and stylesheet
tags, `class_count` was 2, and the artefact contained nothing that identified the page —
nor did a digest of it describe the document. A body already under the cap strips to the
same digest as before, so existing variants are not re-keyed.

## An outbox written by an older build

The field names above were renamed in Batch 7, and the outbox is not reset with a
build: `capture_promote.load_capture` maps the old spellings onto the current names
as it reads a record, so an old outbox stays promotable and parseable. `body_bytes`,
`body_sha256` and `body_noise_stripped` are back-filled as `full_body_bytes`,
`full_body_sha256` and `body_scripts_stripped`. `body_complete` is not a spelling
change but its opposite — it recorded completeness while `body_truncated` records
truncation — so an old `body_complete: true` becomes `body_truncated: false`, mapped
by meaning rather than copied.

The same holds for a group state file: `capture._load_group` migrates an old
`_group.json` (top-level `variants_truncated`, and per-digest `count` and `samples`)
onto `digests_truncated`, `misses` and `samples_written` the first time it is read,
then drops the old keys, so the caps an earlier build already enforced survive a
restart. The manifest's group entry keeps both spellings for one release, because
the puller that reads it lives outside this repository.

## Bounds

A persistent break must cost a bounded number of files. Two caps do that, and both
are constants in `src/capture.py`:

* Captures group by `(kind, selector)`. Within a group, the first
  **`SAMPLES_PER_DIGEST`** (3) samples of each distinct per-shape key are kept and
  no more: a body-carrying record's `shape.class_digest`, and an image-failure
  record's `failure_digest`.
* A group tracks at most **`MAX_DIGESTS_PER_GROUP`** (5) distinct digests. Past
  that, a new shape is counted but not written — otherwise a page whose content
  rotates would produce a new digest per fetch and the per-shape cap would mean
  nothing. The group records `digests_truncated: true` when this happens.

Every miss increments `total_misses` whether or not it produced a file. Those
counters, not the samples, are what convey the size of a break. They live on the
manifest's **group** entry (`sample_count`, `total_misses`, `first_seen`,
`last_seen`), because a sample file cannot carry a group counter that stays true.

State is reloaded from disk at startup, so the caps survive a restart. The group
state file is rewritten on every miss rather than throttled: a throttled counter
under-reports after a restart and can move backwards, and what has to stay bounded
is the file count, not the write count.

The **UI trace** has its own caps, because it is far denser than a web download
and there is no group to hold a counter — the bound has to be in the write path
itself. All three are constants in `src/capture.py` and are injected into the
page, so its own buffer and stop agree with what the route enforces:

* **`UI_TRACE_MAX_EVENTS_PER_BATCH`** (200) events are kept from one posted
  batch; a longer batch drops its oldest events. The record's `events_dropped`,
  `events_truncated` and `truncation_reason: "event_cap"` say so.
* **`UI_TRACE_MAX_RECORD_BYTES`** (256 KB) bound one written record. A record
  over the cap drops oldest events until it fits and sets `bytes_truncated`.
* **`UI_TRACE_MAX_FILES_PER_SESSION`** (200) bounds one page session. The batch
  that crosses the cap is written as the truncation marker
  (`truncation_reason: "session_file_cap"`) and closes the session; every later
  batch is refused with a 429 and nothing more is written. The refusal is how
  the page learns to stop buffering, so the stop is recorded rather than silent.
* the page's own buffer is a **ring buffer** (`UI_TRACE_BUFFER_EVENTS`, 500), so
  a runaway — exactly what this instrument exists to catch — cannot grow the
  tab's memory. It reports what it discarded as `buffer_dropped` on the next
  batch.

The per-session count is seeded from the files already on disk the first time a
session is seen, so a server restart mid-session does not hand the page a fresh
budget.

## Retention

The outbox holds two kinds of thing, and they are cleaned differently.

* **Debug captures** — `<outbox_dir>/web_downloads/`,
  `<outbox_dir>/image_downloads/`, `<outbox_dir>/web_ui_trace/` and the legacy
  `<outbox_dir>/scrapes/` tree an earlier build wrote before the rename — are a
  session instrument, not a record. A capture older than
  **`DEBUG_CAPTURE_RETENTION_DAYS`** (7) is removed by the daemon's housekeeping
  sweep, at most once a day (`capture.prune_debug_captures`). The legacy tree is
  included on purpose: it is debug data like the others, and no current code
  writes it, so nothing else would ever clear it. The UI trace is included even
  though it is bounded on its own: seven days is long enough to review a session,
  and short enough that a switch left on cannot keep filling the disk.
* **Failures and crash dumps** are the evidence a regression test is built from.
  They are **never pruned by age**: `failures/` and `crashes/` stay until the
  owner's pull tool fetches them for review, and that fetch is a *move* — it
  removes the file once the transfer is verified. A failure that has not been
  reviewed is never removed, however old it gets.
* `<outbox_dir>/db/` is the database backup, replaced in place by the backup
  thread. It is not a capture and this sweep does not touch it. A snapshot is
  written as a complete second copy beside the destination before `os.replace`
  publishes it, so one run needs room for roughly two copies of the database.
  When the destination volume demonstrably has less than the source size plus a
  fixed headroom free, the backup **refuses up front** (`_require_free_space`)
  and leaves the previous snapshot byte-identical, rather than starting a write
  it cannot finish. A reading that is unavailable — `os.path.getsize` or
  `shutil.disk_usage` raising `OSError` — is not evidence of no room, so it is
  skipped and the snapshot proceeds. Only *reads* cross from the source
  database's volume: the write, the verification of the copy and the
  `os.replace` publish all happen in `<outbox_dir>/db/` itself, so the source
  database and the outbox may live on different drives and no cross-volume move
  is relied on.
  The **schedule is persisted**, so a restart cannot defer the next snapshot.
  The moment a snapshot verified and was published is recorded in the daemon
  state file beside the database (`.daemon_state.yaml`, section `backup`, key
  `last_snapshot_at`), and only on success: a failed snapshot leaves the
  previous record, so the next start still sees the backup as due. On start the
  backup thread derives the due time from that record — missing, unparseable or
  older than `backup_interval_seconds` takes one after a short grace, inside the
  interval waits out the remainder — and logs the last snapshot's age and the
  next due time. A daemon restarted more often than the interval therefore still
  snapshots at least once per interval, while a restart loop cannot take more
  than one copy per interval. The record is this daemon's schedule rather than a
  pullable artifact, which is why it lives beside the database instead of in the
  outbox; the closing snapshot (`Daemon._maybe_final_snapshot`) is unchanged and
  updates the record too when it runs.

Removing a capture also drops its `manifest.json` entry in the same operation
(`backup.remove_manifest_entries`), and the pull tool does the same when it moves
a file. The puller transfers one file per entry, so an entry left behind would
make the next pull fail on a path that no longer exists; whichever side removes
the file removes the entry with it.

**What is deliberately left open** is the failure tree's own growth, and the
rotated log archives. Neither has a retention rule: how long to keep unreviewed
failures, and when to delete the owner's log history, are the owner's decisions,
not this module's.

## Storage layout

```
<outbox_dir>/failures/<group>/_group.json     counters, and per-shape state
<outbox_dir>/failures/<group>/<digest8>-<n>.json   the capture record
<outbox_dir>/failures/<group>/<digest8>-<n>.body   the raw bytes
<outbox_dir>/web_downloads/<stamp>-<kind>-<id>.json  a web pull's record
<outbox_dir>/web_downloads/<stamp>-<kind>-<id>.body  its response body
<outbox_dir>/image_downloads/<stamp>-<id>.json     a successful image download (metadata only)
<outbox_dir>/web_ui_trace/<stamp>-<session>-<n>.json  one batch of page events
<outbox_dir>/crashes/<stamp>-<process>-error<N>.txt  one crash dump
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
so a puller can collect them without also pulling the failure tree. UI-trace
batches are registered with `kind: "ui_trace"` and `role: "batch"`, their own
kind again, so `--only ui_trace` collects exactly them. Crash dumps are
registered with `kind: "crash"`, their own kind again.

## Selector misses change the queue

`scrape_extended_details` returns `{"description": None, "tags": []}` when the
description selector does not match — a truthy value. That used to be taken as
success, writing `extended_description = NULL` and `web_scrape_priority = 0`, so the
item was recorded as permanently scraped with nothing to show for it and was never
retried.

A miss is now a failure. The artefact is captured, and the item is stepped down the
queue by one, floored at one:

```sql
UPDATE workshop_items SET web_scrape_priority = MAX(1, web_scrape_priority - 1)
```

so the item stays queued, sinks below current work, and is never zeroed.

This follows the *shape* of the `image_priority` decay in `image_worker.py` — a direct
arithmetic update rather than a `MAX(current, new)` priority bump — but differs from
it in two deliberate ways. `image_priority` floors at 0, which takes the item out of
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
parametrized over every fixture found. The area comes from `FIXTURE_AREA_BY_KIND`,
keyed by the record's `kind`: every web kind lands in `tests/fixtures/web/`,
every API kind in `tests/fixtures/steam_api/`, and a kind not in the table falls
back to `tests/fixtures/other/` — so a kind the code records but the table omits
is filed away from the code it exercises, which is why the table lists all of
them. Credentials (`key=`, `sessionid`, `steamLoginSecure`, `api_key`) are
scrubbed on the way in, since a body may carry them. A capture with no
`body_file` is skipped rather than promoted: image failures are metadata-only by
design, and turning one into an empty fixture would produce a test that asserts
nothing.

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
