# Data Model

The database is a single SQLite file in WAL mode. Its current schema version is 30
(`EXPECTED_VERSION` in `src/database.py`). All application state lives in three tables —
`workshop_items`, `creators`, and `translation_queue` — plus two tables that hold tags,
`tags` and `workshop_tags`.

Column names follow two conventions, described in full in [timestamps.md](timestamps.md):

* `steam_*` is Steam's clock; `*_at` is our clock; `*_version` is a stored Steam value used as a
  version key.
* Work-queue columns end in `_priority` (with historical exceptions: `api_priority`,
  `needs_web_scrape`, `needs_image`).

## Provenance Classes

Every column has exactly one owner. This is the classification the naming alone does not carry.

| Class | Meaning | Owner |
|---|---|---|
| **STEAM** | Passed through from the Steam API, sometimes under a different name | Steam |
| **SCRAPED** | Extracted from Workshop HTML | web scraper |
| **DERIVED** | Computed from other columns | daemon |
| **TRANSLATED** | Produced by the translation service | translator |
| **QUEUE** | Work-queue priority or progress counters | daemon |
| **STATE** | Lineage and outcome bookkeeping | daemon |

## `workshop_items`

One row per Steam Workshop item, keyed by `workshop_id` (the Steam `publishedfileid`).

### Steam-provided columns

| Column | Source | Notes |
|---|---|---|
| `workshop_id` | `publishedfileid` | Primary key. The API key supplied alongside it is dropped before storage. |
| `creator` | `creator` | SteamID of the author. No foreign key is declared: items are discovered before their creator is fetched, and the join to `creators` is a `LEFT JOIN`. |
| `creator_appid` | `creator_app_id` | Renamed in `_merge_and_clean_api_data`. |
| `consumer_appid` | `consumer_app_id` | The game the item belongs to. |
| `filename` | `filename` | |
| `file_size` | `file_size` | Bytes. |
| `preview_url` | `preview_url` | |
| `hcontent_file` | `hcontent_file` | Opaque content handle. |
| `hcontent_preview` | `hcontent_preview` | Opaque content handle. |
| `title` | `title` | |
| `short_description` | `description` | Renamed in `_merge_and_clean_api_data`; Steam calls it `description`. |
| `steam_created_at` | `time_created` | Steam epoch seconds. |
| `steam_updated_at` | `time_updated` | Steam epoch seconds. Also the value recorded as the scrape and translation version key. |
| `visibility` | `visibility` | |
| `banned` | `banned` | |
| `ban_reason` | `ban_reason` | |
| `app_name` | `app_name` | |
| `file_type` | `file_type` | |
| `subscriptions` | `subscriptions` | Current subscriber count. |
| `favorited` | `favorited` | Current favorite count. |
| `views` | `views` | |
| `lifetime_subscriptions` | `lifetime_subscriptions` | |
| `lifetime_favorited` | `lifetime_favorited` | |

There is deliberately no `language` column: no Steam response this project
consumes can populate one. See [Known Gaps](#known-gaps).

### Scraped and translated columns

| Column | Class | Source |
|---|---|---|
| `extended_description` | SCRAPED | `div.workshopItemDescription#highlightContent` on the item's HTML page. Never comes from the API. |
| `title_en` | TRANSLATED | OpenAI. |
| `short_description_en` | TRANSLATED | OpenAI. |
| `extended_description_en` | TRANSLATED | OpenAI. |

Tags are not a column on `workshop_items`. They live in `tags(tag_id, tag_name)` and
`workshop_tags(workshop_id, tag_id)`.

### Derived and bookkeeping columns

| Column | Class | Meaning |
|---|---|---|
| `wilson_favorite_score` | DERIVED | `wilson_lower(favorited, lifetime_subscriptions)`. |
| `wilson_subscription_score` | DERIVED | `wilson_lower(subscriptions, lifetime_subscriptions)`. |
| `status` | STATE | Fetch outcome. `200` = fetched, `206` = partial (see gaps below), `-1` = dead, `500` = transient failure to retry, `NULL` = discovered but never fetched. Synthetic, not an actual HTTP response code. |
| `first_seen_at` | STATE (ours) | Set when the row is first inserted. |
| `api_fetched_at` | STATE (ours) | Set only when the API returned content (see [timestamps.md](timestamps.md)). |
| `last_fetch_attempted_at` | STATE (ours) | Set on every API attempt, success or failure. |
| `scrape_version` | STATE (Steam value) | `steam_updated_at` at the moment the web scraper ran. |
| `translate_version` | STATE (Steam value) | `steam_updated_at` at the moment the translator ran. |
| `web_scraped_at` | STATE (ours) | Our clock: when the web worker last scraped this item's page successfully. NULL means no success has been recorded since the column arrived in v27; unlike `scrape_version` it is not a Steam revision. |
| `image_fetched_at` | STATE (ours) | Our clock: when the image worker last fetched this item's preview successfully. NULL means no success has been recorded since the column arrived in v27. |
| `translated_at` | STATE (ours) | Our clock: when the translator finished the **last** queued field for this item. One stamp per item, moved on each later completion; NULL means no completion has been recorded since the column arrived in v27. The `creators` table has a column of the same name meaning "when this profile's text was translated"; the item column is the completion time of the item as a whole, because a per-field stamp is what `translate_version` already carries. |
| `image_extension` | STATE | The preview's **outcome**, not only its file type. A real extension (`jpg`, `png`, …) means the file exists at `images/<bucket>/<id>.<ext>` and a URL may be built from it. A **wholly numeric** value is an HTTP status the server answered with: `404`/`410` mean the preview is permanently missing and will not be retried, any other code is recorded but still retryable. Any other token (`html`, `svg+xml`) is a content type that was not a picture this downloader can store. NULL means nothing has been recorded yet. One rule follows from this: **a URL is only ever built from a known image extension**, which `src/images.py` owns so the writer and every reader agree. |
| `is_queued_for_subscription` | QUEUE | Subscription queue flag. Set by the TUI (`s`) and by `POST /api/toggle_subscription_queue/<id>`; cleared whenever the owner's subscription is observed, so nothing is left pending for an item that is now subscribed: by `mark_own_subscribed` — which the subscribe engine calls on a confirmed subscribe and on its already-`toggled` short-circuit — by `POST /api/subscribed/<id>` and `POST /api/subscribe_failed/<id>` when the userscript reports an outcome, and by a subscription reconcile for any item it finds already subscribed. Transient working state — it reads `0` whenever nothing is queued, which is the normal resting state, not evidence of disuse. |
| `own_subscribed` | STATE (ours) | Whether **the owner** — the account whose API key and cookies are configured — is subscribed to this item right now. Reconciled from the signed-in Workshop subscriptions page (`src/subscription_sync.py`), stamped immediately by `POST /api/subscribed/<id>` when the userscript confirms a subscribe, and stamped by `src/subscribe_engine.py` both when its confirmation read shows the item subscribed and when its pre-read already shows `toggled` (no request is sent in that case). Not to be confused with `subscriptions` / `lifetime_subscriptions`, which are item-wide counts that cannot be attributed to an account. |
| `own_first_subscribed_at` | STATE (ours) | When we first *saw* the owner subscribed, Unix epoch seconds; NULL means never seen. **Sticky**: it is never moved or cleared, and it is the only source of the `previously` marker state. Steam exposes no per-account subscription history, so this means "first seen by us", not "first subscribed" — on the day this column shipped it was NULL for every row, and it fills in over time. |
| `downloaded_at` | STATE (ours) | When this app first saw Steam's downloaded copy of a **subscribed** item on disk, Unix epoch seconds; NULL means not confirmed on disk. **The `downloaded` marker requires this column *and* `own_subscribed`**, so a stray timestamp beside a cleared subscription cannot claim the green star. It is a local latch with exactly one writer and one clearer: `src.workshop_folders` stamps it when its periodic scan finds `<library>/steamapps/workshop/content/<consumer_appid>/<workshop_id>/` for an item that is `own_subscribed = 1` and not yet confirmed, and it only ever writes — a missing folder, an unplugged drive or a moved library changes nothing and a confirmed item is never revisited. The only clearer is `apply_own_subscriptions`, in the same transaction that clears `own_subscribed` when the item leaves the owner's subscription list; re-subscribing re-earns the stamp on the next scan. It is excluded from the API merge allow-list (`daemon.MERGE_EXCLUDED_KEYS`), because it is not a Steam field. |

### Queue columns

| Column | Queue |
|---|---|
| `api_priority` | Steam API fetch queue. |
| `needs_web_scrape` | HTML scrape queue. |
| `needs_image` | Preview image download queue. |
| `translation_priority` | Translation queue mirror. Raised together with the `translation_queue` row by `queue_field_for_translation` (which takes the `MAX` of the stored and new priority) **in one transaction**, and zeroed by the translator when the item's last queue row is deleted. A priority above `0` therefore means the item has at least one queued field; migration 22→23 cleared the rows that disagreed. |

## `creators`

One row per Steam creator whose profile has been fetched, keyed by `steamid`.

| Column | Class | Meaning |
|---|---|---|
| `steamid` | STEAM | SteamID64, primary key. |
| `personaname` | STEAM | Display name. |
| `personaname_en` | TRANSLATED | Translated display name, written by the translator when it drains the creator's `personaname_en` queue row. |
| `api_fetched_at` | STATE (ours) | Our clock: when the profile was last refreshed. |
| `translated_at` | TRANSLATED/STATE | Our wall-clock time of the last translation. This is **not** a Steam version key: creators have no `steam_updated_at`. |
| `translation_priority` | QUEUE | Translation queue mirror, exactly as on `workshop_items`: raised by `queue_field_for_translation` in the same transaction as the `translation_queue` row and zeroed by the translator when the creator's last queue row is deleted. A priority above `0` therefore means the creator has at least one queued field. Migration 27→28 queued the flags that had no row behind them and cleared the ones with nothing left to translate. |

## `translation_queue`

One row per text field awaiting translation.

| Column | Meaning |
|---|---|
| `id` | Auto-increment queue entry ID. |
| `item_type` | `"item"` or `"user"`. |
| `item_id` | `workshop_id` or `steamid`. |
| `field` | Target column, for example `title_en`. |
| `original_text` | Source text. |
| `priority` | Work priority; the translator drains highest first. |
| `queued_at` | Our clock: when the entry was created. Written for new rows. Rows that predate migration 13→14 keep NULL because their queue time is genuinely unknown. |

Ordering (`get_next_batch_for_translation`) is `priority DESC`, then NULL-`queued_at` rows ahead
of dated rows, then `queued_at ASC`. NULL-first is SQLite's implicit ascending order, so the
legacy backlog is not jumped by newly queued work at the same priority. An older
`queued_at IS NOT NULL` term made that rule explicit but was redundant with the implicit ordering,
and it forced a temp B-tree; it was dropped so `idx_translation_queue_poll` can serve the sort.

`idx_translation_queue_lookup` on `(item_type, item_id, field)` serves both the per-field lookup
`queue_field_for_translation` runs before every queue write and migration 22→23's correlated
`NOT EXISTS` repair, which constrains only the first two columns. Unlike the other query indexes it
is created by both schema builders rather than `_ensure_indexes`: the repair runs inside the migration
loop, before `_ensure_indexes` does, so an index created there would not exist yet when the repair
scans. Creating it in the unversioned schema also means an existing database picks it up on the next
startup without a migration step.

`idx_translation_queue_poll` on `(priority DESC, queued_at ASC)` serves that poll's ordering. It is
created by `_ensure_indexes` and not by the schema builders, the opposite placement for a concrete
reason: on a legacy-chain fresh database `_create_legacy_schema` runs while the column is still called
`dt_queued` (migration 13→14 renames it), so an index naming `queued_at` there fails with "no such
column". `_ensure_indexes` runs after the migration chain.

## Queue Priorities

The work-queue columns share a vocabulary but not a single distribution. The intended scale is:

| Value | Meaning |
|---|---|
| 0 | Idle / not queued. |
| 1 | Backlog or stale background refresh. |
| 2 | Retry after a stage failure — `api_priority` only. |
| 3 | Newly discovered item. |
| 5 | Visible in a list. |
| 10 | Open in the detail pane. |

Higher wins, and writers use `MAX(stored, new)` so a flag is never downgraded. The queues do not
each use every value. `2` is the narrowest of them: it is written only by the image worker's
download-failure path (`src/image_worker.py:201`) and the web worker's request-failure path
(`src/web_worker.py:336`), both raising the item's *API* priority so that the refresh which
re-evaluates it happens soon — above the backlog, below an item someone is looking at. See
[live-data-profile.md](live-data-profile.md) for the measured distribution of each queue.

**The lower half belongs to the daemon, the top to the user.** `1`, `2` and `3` are what the daemon
writes to organise its own work — a stale refresh, a retry, a discovery — while `5` and `10` are set
by a person looking at the item. `USER_PRIORITY_FLOOR` (`src/database.py`) is that boundary, and two
things depend on it:

* **Only a user request cascades.** When the API fetch re-queues the stages that hang off an item, it
  inherits the part of `api_priority` at or above the floor and nothing else
  (`user_requested_priority`, `src/daemon.py`). Inheriting the whole value made a discovery priority
  (`3`) behave like a request, so a newly discovered item that failed its AppID's **enrichment
  filters** was queued in the same band as one the filters selected — and since `MAX` never
  downgrades, those rows stayed there. Migration 21→22 repairs the rows that wrote
  ([schema-migrations.md](schema-migrations.md#v21--v22-filter-excluded-items-give-up-their-queue-priority)).
* **A filter-excluded item is still scraped, at backlog.** The enrichment filters choose priority,
  not membership: an item that does not match is queued at `1` for anything the page can change,
  and translation is the only stage skipped outright for it. What it must never do is *outrank* an
  item the filters did select. An enrichment filter can be a `Subscribed` row over
  `own_subscribed` / `own_first_subscribed_at` / `is_queued_for_subscription` / `downloaded_at`;
  both in-memory readers (the daemon's decision and migration 21→22's demotion walk) load all four
  columns for it — see [search-filter.md](search-filter.md) and
  [data-pipeline.md](data-pipeline.md).

## Known Gaps

These are properties of the current implementation, stated so a reader does not infer behaviour
that is not there.

* **`translate_version` drives item re-translation; `scrape_version` is only a record.** An item
  translation is current when its `translate_version` is not older than the item's
  `steam_updated_at`, and every *item* translation trigger applies that rule (see
  [timestamps.md](timestamps.md)). A **creator's** name is the one exception, and deliberately so: a
  creator has no `steam_updated_at`, and `api_fetched_at` — the only clock left — moves on every
  profile refresh, so the refresh queues every non-ASCII name it fetches rather than trying to prove
  the name unchanged ([data-pipeline.md](data-pipeline.md#what-queues-a-field-for-translation)).
  `scrape_version` is
  written by the web scraper and the image worker, but no code compares it: the daemon decides
  whether to re-queue the HTML scrape from `steam_updated_at` and whether `extended_description` is
  already present, so the HTML scrape refreshes on an item update and the image worker uses its own
  priority queue.
* **`language` no longer exists, and never had a source.** It was added as a Steam-provided column
  expecting the API to return a language, but no response this project consumes carries one.
  `GetPublishedFileDetails` has no language field in its response message, and `language` appears in
  the request protocol only as the *viewer's* localization parameter — the language to render
  `title`/`description` in — which the client would set and never read back. The recorded response
  body in `tests/test_steam_api.py` carries none, and every row in the live database was NULL, so the
  column backed a permanently "N/A" web-tooltip line and a "Language ID" filter that could never
  match. Migration 23→24 drops the column and its index; the filter alias went with it.
* **`is_queued_for_subscription` is `0` in an idle database, but it is not dead.** It backs a
  working feature: the TUI (`s`) and the web UI toggle it, the userscript polls
  `GET /api/queued` and clears each entry once it has subscribed or failed. Because it is
  transient, an all-zero snapshot only means nothing was queued at that moment.
* **`status = 206` has never occurred.** The schema and one migration query allow a partial-data
  status, but the production database contains zero rows with it.
* **The completion clocks start empty and are never backfilled.** `web_scraped_at`,
  `image_fetched_at` and `translated_at` arrived in v27, so every item that completed its stage
  earlier keeps NULL and no rate can be reconstructed for it. The per-queue throughput metrics
  report "no history yet" for a queue with no stamps rather than a zero or an estimate
  ([timestamps.md](timestamps.md)). The `queue_eta` metric is not blocked on history: it reports
  each queue's outstanding depth and, from whatever completions exist, a rate in active time and a
  `53d ± 30%` time to drain, with the uncertainty widening when the evidence is thin
  ([data-pipeline.md](data-pipeline.md#queue-state-outstanding-rate-and-time-to-drain)).
