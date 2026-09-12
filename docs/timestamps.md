# Timestamps and Version Keys

Timestamp-like columns come in three kinds. Keeping them distinct is the point of the naming:
several of them used to share one name and were read as if they all meant the same thing.

| Clock | Convention | Columns |
|---|---|---|
| Steam's clock | `steam_*` | `steam_created_at`, `steam_updated_at` (`workshop_items`) |
| Our clock | `*_at` | `first_seen_at`, `api_fetched_at`, `last_fetch_attempted_at` (`workshop_items`); `api_fetched_at`, `translated_at` (`users`); `queued_at` (`translation_queue`) |
| A stored Steam value used as a version key | `*_version` | `scrape_version`, `translate_version` (`workshop_items`) |

All of them are Unix epoch integers (seconds), except where noted. There are no ISO 8601 strings
in the current schema.

## What Each Column Means

| Column | Table | Written by | Meaning |
|---|---|---|---|
| `steam_created_at` | `workshop_items` | Steam API | Steam's clock: when the author created the item. |
| `steam_updated_at` | `workshop_items` | Steam API | Steam's clock: when the author last updated it. Also the source value for both version keys. |
| `first_seen_at` | `workshop_items` | `insert_or_update_item`, new rows only | Our clock: when the row was first inserted. |
| `api_fetched_at` | `workshop_items` | daemon, on a successful API content pull only | Our clock: the last time the API returned usable content. |
| `last_fetch_attempted_at` | `workshop_items` | daemon, on every API attempt | Our clock: the last time a fetch was attempted, success or failure. |
| `scrape_version` | `workshop_items` | web scraper / image worker | Steam value: `steam_updated_at` at the moment the scraper ran. |
| `translate_version` | `workshop_items` | translator | Steam value: `steam_updated_at` at the moment translation ran. |
| `api_fetched_at` | `users` | daemon | Our clock: when the creator profile was last refreshed. |
| `translated_at` | `users` | translator | Our wall-clock time of the translation. Users have no `steam_updated_at`, so this is not a version key. |
| `queued_at` | `translation_queue` | `flag_field_for_translation`, new rows only | Our clock: when the queue entry was created. NULL on rows that predate migration 13→14, because their queue time is unknown. |

## Write Rules

| Event | `first_seen_at` | `api_fetched_at` | `last_fetch_attempted_at` | `scrape_version` | `translate_version` |
|---|---|---|---|---|---|
| Row first inserted | set | — | — | — | — |
| API fetch attempted | — | — | **set** | — | — |
| API content received | — | **set** | **set** | — | — |
| Web scrape succeeds | — | — | — | **set** = `steam_updated_at` | — |
| Translation succeeds | — | — | — | — | **set** = `steam_updated_at` |

Web scraping and translation do not touch `api_fetched_at`, and never have.

## Why `last_fetch_attempted_at` Exists

`get_next_items_to_scrape` orders by `api_priority DESC, api_fetched_at ASC`. If
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

## The Version Keys Are Not Compared

`scrape_version` and `translate_version` faithfully record the Steam update time at which the
scraper and translator ran. Nothing compares them against the current `steam_updated_at`: there is
no code that re-scrapes or re-translates when the two disagree. The intended behaviour is not
implemented; the columns are records, not triggers.
