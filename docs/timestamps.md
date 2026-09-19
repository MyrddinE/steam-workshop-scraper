# Timestamps and Version Keys

Timestamp-like columns come in three kinds. Keeping them distinct is the point of the naming:
several of them used to share one name and were read as if they all meant the same thing.

| Clock | Convention | Columns |
|---|---|---|
| Steam's clock | `steam_*` | `steam_created_at`, `steam_updated_at` (`workshop_items`) |
| Our clock | `*_at` | `first_seen_at`, `api_fetched_at`, `last_fetch_attempted_at`, `web_scraped_at`, `image_fetched_at`, `translated_at`, `steam_download_seen_at` (`workshop_items`); `api_fetched_at`, `translated_at` (`creators`); `queued_at` (`translation_queue`) |
| A stored Steam value used as a version key | `*_version` | `translate_version` (`workshop_items`) |

One our-clock column carries the `steam_` prefix: `steam_download_seen_at` records when **we**
first saw Steam's downloaded copy of a subscribed item on disk (the folder scan's one-way latch),
not a time Steam supplied.

All of them are Unix epoch integers (seconds), except where noted. There are no ISO 8601 strings
in the current schema.

## What Each Column Means

| Column | Table | Written by | Meaning |
|---|---|---|---|
| `steam_created_at` | `workshop_items` | Steam API | Steam's clock: when the author created the item. |
| `steam_updated_at` | `workshop_items` | Steam API | Steam's clock: when the author last updated it. Also the source value for `translate_version`. |
| `first_seen_at` | `workshop_items` | `insert_or_update_item`, new rows only | Our clock: when the row was first inserted. |
| `api_fetched_at` | `workshop_items` | daemon, on a successful API content pull only | Our clock: the last time the API returned usable content. |
| `last_fetch_attempted_at` | `workshop_items` | daemon, on every API attempt | Our clock: the last time a fetch was attempted, success or failure. |
| `translate_version` | `workshop_items` | translator | Steam value: `steam_updated_at` at the moment translation ran. |
| `web_scraped_at` | `workshop_items` | web worker, on a successful scrape only | Our clock: when this item's page was last scraped successfully. |
| `image_fetched_at` | `workshop_items` | image worker, on a successful download only | Our clock: when this item's preview image was last fetched successfully. |
| `translated_at` | `workshop_items` | translator, when the item's last queued field is translated | Our clock: when this item's translation last completed. One stamp per item, not per field. |
| `steam_download_seen_at` | `workshop_items` | `src/workshop_folders`, on the periodic download scan only | Our observation: when this app first saw Steam's downloaded copy of a subscribed item on disk. A one-way latch — a missing folder, an unplugged drive or a moved library never clears it — and the only clearer is `apply_own_subscriptions`, when the item leaves the subscription list. |
| `api_fetched_at` | `creators` | daemon | Our clock: when the creator profile was last refreshed. |
| `translated_at` | `creators` | translator | Our wall-clock time of the translation. Creators have no `steam_updated_at`, so this is not a version key. |
| `queued_at` | `translation_queue` | `queue_field_for_translation`, new rows only | Our clock: when the queue entry was created. NULL on rows that predate migration 13→14, because their queue time is unknown. |

## Write Rules

| Event | `first_seen_at` | `api_fetched_at` | `last_fetch_attempted_at` | `web_scraped_at` | `image_fetched_at` | `translated_at` | `translate_version` |
|---|---|---|---|---|---|---|---|
| Row first inserted | set | — | — | — | — | — | — |
| API fetch attempted | — | — | **set** | — | — | — | — |
| API content received | — | **set** | **set** | — | — | — | — |
| Web scrape succeeds | — | — | — | **set** = our clock | — | — | — |
| Image download succeeds | — | — | — | — | **set** = our clock | — | — |
| A field is translated | — | — | — | — | — | — | **set** = `steam_updated_at` (or our clock when the row has no Steam payload) |
| The item's last queued field is translated | — | — | — | — | — | **set** = our clock | **set** = `steam_updated_at` |

Web scraping and translation do not touch `api_fetched_at`, and never have.

**A failed or partial stage stamps nothing.** The completion clocks move only on
the success paths: the web worker's success branch (not a selector miss, a wall,
a throttle or a transport failure), the image worker's completed download (not a
404, an unclassifiable content type or a transport failure), and the translator's
last-field write for an item (not a per-field write while another field is still
queued, and not a batch that returned no text). This is deliberate: a stamp on a
partial stage would overstate the rate, and the throughput metric could not tell
the difference between "we did the work" and "we tried".

## Completion Clocks and the No-History Rule

`web_scraped_at`, `image_fetched_at` and `translated_at` are the completion
timestamps throughput, burn-down and ETA are measured from, and they exist
because the three queues had none of our own: the web stage recorded only a
Steam revision it never used, the translation stage recorded Steam's version
value, and the image stage recorded nothing.
Migration 26→27 adds them; [schema-migrations.md](schema-migrations.md) has the
storage detail. That unused web revision, `scrape_version`, was dropped in
34→35.

**Every row that predates the migration keeps NULL, and nothing backfills them.**
The stages never recorded when they completed an item, so the time is not
recoverable, and an estimate would be a fabricated rate rather than a
measurement. The metrics say so instead of guessing:

* `last_success` is **NULL** when a queue's column holds no value at all. The
  front ends render that as "no history yet", and the throughput for that queue
  is also NULL rather than `0`.
* Once a single stamp exists the counts are real answers. `0` completed in the
  last hour then means a queue that was measured and is idle, which is a
  different statement from an unmeasurable one.
* The counts only include rows inside their window, so a NULL-stamped row can
  never be counted in either.

## Active Time and the Pause Record

A completion clock says when we finished a stage. A **rate** taken from one is
completions divided by time, and the time that matters is the time the queues
were actually running: a pause stops the web and image workers while the clock
keeps going, so a wall-clock rate falls every time the daemon is switched off.
The dot-prefixed `.pauselock` intervals are therefore recorded in
`.daemon_state.yaml` beside the database (`src/activity.py`) — the same
transient, restart-surviving store as the pacing backoff and the session warning
— rather than in the versioned schema, because they describe a condition that
passes and the clocks table above is about the item rows. The drain metric reads
that record and subtracts the paused time from the rate window
([data-pipeline.md](data-pipeline.md#active-time-not-wall-clock)). The API
queue's rate additionally subtracts the staleness sweep's recorded rowcount,
because the sweep makes work rather than doing it.

## Why `last_fetch_attempted_at` Exists

`get_next_items_to_fetch` orders by `api_priority DESC, api_fetched_at ASC`. If
`api_fetched_at` moved on every attempt, then a just-failed item would look freshly fetched and
would be retried only after every other item in its priority band — which is why the old shared
column was written before the API call, failures included. If it moved only on success without a
replacement, a failing item would keep a stale `api_fetched_at` and sit at the front of the queue,
retrying in a tight loop.

The two columns split those jobs:

* `api_fetched_at` moves only when the API returned content, and is what freshness and staleness
  are measured from.
* `last_fetch_attempted_at` is stamped before the status branches in `_process_item`, so a `404`,
  a `500`, and a success all advance it. It drives retry spacing, and `get_db_stats` reports fetch
  recency from it.

## The Legacy Approximation for `api_fetched_at`

For rows created before migration 13→14 that have not succeeded since, `api_fetched_at` is a
best-effort approximation. The old column held only the most recent attempt, so a row that
succeeded once and then failed a later attempt holds the **failure** time until its next success;
the true last-success time is not recoverable from the existing data. It self-corrects on the next
successful fetch.

The migration reduced the scope of the approximation by clearing `api_fetched_at` on rows that
never received API content at all (`steam_updated_at IS NULL`). Those rows now read as
never-fetched, which is correct. See [schema-migrations.md](schema-migrations.md) for the full
migration-13→14 behaviour.

## How the Version Key Is Used

`translate_version` records the Steam update time at which the translator ran. It is compared
against the current `steam_updated_at`, which is what makes it useful rather than merely honest.
`scrape_version`, the scraper's equivalent, was dropped in migration 34→35: no code ever compared
it, and when our own scrape time is wanted it is `web_scraped_at`.

**Translation.** `translation_is_current(translated_text, translate_version, steam_updated_at)`
decides whether a field needs (re-)translating. A translation is current when the `_en` value exists
and `translate_version >= steam_updated_at`; otherwise the field is queued. Every trigger in
[data-pipeline.md](data-pipeline.md) applies this rule, so unchanged text is not re-translated and
edited text is.

The rule's edges are deliberate:

* **No `_en` value** → not current, so the field is queued. Missing text always needs translating.
* **`steam_updated_at IS NULL`** (an item that never received a Steam payload) → treated as
  **current**, because no change can be detected; otherwise such items would be re-translated
  forever. The translator stamps the wall clock in this case, since there is no Steam value to
  record.
* **`translate_version IS NULL` with an `_en` value present** → treated as **stale**, because the
  provenance is unknown. This costs one re-translation per row. It is currently empty in the
  production data: of 142,748 translated titles, none has a NULL version.
* **`translate_version > steam_updated_at`** → current. This happens when a translation was stamped
  with the wall clock for an item that later acquired a Steam payload, whose update time predates
  it.

The comparison is per **item**, not per field: `translate_version` is one column on
`workshop_items`, so an edit to any Steam-visible field makes every non-ASCII field of that item
stale and re-queues them together. That is the granularity of the only version stamp that exists.

**Scraping.** There is no scrape version key. The web worker's re-queue decision is made from
`steam_updated_at` and whether `extended_description` is already present
(`_raise_scrape_and_image_priorities` compares `steam_updated_at` against the pre-fetch record), and
its completion time is `web_scraped_at`. `scrape_version` used to record the same
`steam_updated_at` the scraper ran at, but nothing consumed it; the image worker also overwrote it on
every download until issue 7 removed that write, and migration 34→35 removed the column.
