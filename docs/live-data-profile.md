# Live Data Profile

What the production database actually contains, measured rather than assumed. The figures below
come from a verified snapshot of `WallpaperEngine.db` — 1,725,544 items, schema version 13, taken
2026-09-12 and SHA-256-matched to the source:

```
2de6319c3924d532292e40744f85da4674c0af884ad46aa2496262f41a1bbbb5
```

The snapshot predates migration 13→14, which renamed the timestamp columns and cleared a small
number of stale version values. Row counts and distributions are unchanged by a metadata-only
rename; the places where the migration changed an interpretation are called out below. Column
names here are the current ones unless a passage is explicitly describing the migration.

## Scale and backlog

* **1,725,544** items.
* **96.7%** of them (1,667,817) have no `extended_description` — the web scraper has run on only a
  small fraction of the dataset.
* **89%** (1,537,998) sit at `web_scrape_priority = 1`.
* **1,273,020** items (73.8%) reference a `creator_steamid` that is absent from `creators`; only **24,566**
  creators exist.
* Only **142,748** items have any translation.

The enrichment pipeline is operating on a dataset that is roughly 3% processed.

## Queue distributions

The intended priority scale is `0 / 1 / 3 / 5 / 10`. The measured data:

`api_priority`:

```
0 : 1,715,069      2 : 10,474      5 : 1
```

No `1`, no `3`, no `10` — and `2` appears, which the scale does not document. It comes from the
image worker's failure path (`image_worker.py`), a worker-specific retry marker rather than part
of the shared vocabulary. The queues do not behave uniformly:

* `web_scrape_priority`: `1` for **1,537,998** (89%), plus 0 / 2 / 3 / 5
* `image_priority`: `1` for 1,391,080, `0` for 334,005, `5` for 459
* `translation_priority`: `3` for 91,655, `5` for 64, `10` for 5, `0` for the rest

Treat the scale as per-queue. See [data-model.md](data-model.md) for the column semantics.

**Re-measured 2026-09-17** (2,497,545 rows, five days after the snapshot above): `web_scrape_priority`
held `0` for 87,535, **`1` for 1,544,799**, `2` for 2,406, **`3` for 862,650**, `5` for 155; and
`image_priority` `1` for 1,391,781 with `3` for 670,502. The `3` band had gone from a rounding error to
a third of the queue in five days, because the daemon inherited a discovered item's `api_priority`
(`3`) into these queues while **95% of what discovery finds fails the enrichment filters** — so the
backlog was being filled, at discovery speed, with work on items the filters exclude. Migration
21→22 returns those rows to backlog;
[code-issues.md](code-issues.md#recently-closed) records the measurement in full.

## Dead columns

| Column | Reality |
|---|---|
| `language` | **Was NULL for all 1,725,544 rows; the column has since been dropped.** The API merge would have stored it if a Steam response included it, and none can: `GetPublishedFileDetails` has no language field, and the request protocol's `language` is the viewer's localization parameter, not an item property. Migration 23→24 removes the column and its index — see [schema-migrations.md](schema-migrations.md#v23--v24-drop-the-never-populated-language-column). |
| `scrape_version` | **Had no reader; dropped in migration 34→35.** It recorded `steam_updated_at` at scrape time, was overwritten by the image worker until issue 7, and was never compared by any code. |
| `app_discovery.last_historical_date_scanned`, `app_discovery.window_size` | **Had no reader; dropped in migration 34→35.** Both were written only by `update_app_tracking`, which nothing but a test called. |
| `is_queued_for_subscription` | **`0` for every row in this snapshot.** That is the resting state, not disuse. The column backs the subscription queue: the TUI and web UI set it, the userscript polls `GET /api/queued` and clears each entry on success or failure. Do not read this snapshot as "the feature is unused". |
| `fetch_status = 206` | **Zero occurrences.** The schema's "web scrape failed but API succeeded" status has never been written. |

## The version key that used to hold two things (and has since been dropped)

The column that became `scrape_version` was written from `steam_updated_at` whenever a Steam payload
existed, but for a failed fetch there was no `steam_updated_at` to record, so the daemon wrote the
attempt time instead. Measured on the pre-migration snapshot:

```
scrape_version (then dt_attempted) == steam_updated_at (then time_updated) : 1,724,528  (99.94%)
scrape_version (then dt_attempted) != steam_updated_at (then time_updated) :     1,016
```

Every one of the 1,016 rows is a failed fetch. The column therefore meant "Steam's version marker"
when the fetch succeeded and "when we tried" when it failed — a silent dual meaning for 0.06% of
rows.

Migration 13→14 separated the two meanings: the attempt time now lives in `last_fetch_attempted_at`
(backfilled from the old shared column, whose history genuinely was attempt times), and
`scrape_version` was set NULL where `steam_updated_at IS NULL`. The dual meaning no longer existed in
the schema after that. Migration 34→35 then dropped the column outright: nothing ever read it, and
`web_scraped_at` is the scraper's completion clock.

## `translation_queue.queued_at`

On the pre-migration snapshot, **all 114,125 rows had `queued_at` (then `dt_queued`) NULL**, yet
the translator ordered by it. Within a priority band, ordering was arbitrary. The current code
writes `queued_at` for new rows; the pre-existing backlog keeps NULL because the queue time is
genuinely unknown, and `get_next_batch_for_translation` explicitly orders NULL rows ahead of dated
ones at the same priority so the backlog is not jumped.

Queue composition at the time of measurement: `title_en` 87,658 · `short_description_en` 24,673 ·
`extended_description_en` 1,794.

## Data-quality anomalies

* **1,016 rows** where the version key disagreed with `steam_updated_at` (all failed fetches; now
  resolved by the migration, as described above).
* **148 rows** with `fetch_status IS NULL` but `first_seen_at` set and `api_fetched_at` NULL —
  discovered but never fetched. These match the `delete_never_fetched_items` deletion criteria.
* **One row** (`workshop_id 2804549163`, fetch_status 200, title present) had `first_seen_at` NULL. The
  insert path is supposed to set it unconditionally; migration 13→14 repaired the row from
  `api_fetched_at`.
* Data integrity is otherwise clean: `PRAGMA quick_check` = `ok`, **zero** duplicate
  `workshop_id`s, and **zero** orphan rows in either direction of the `workshop_tags` join.

## Text shapes

| Column | Max length | Average length |
|---|---|---|
| `title` | **128** | 19.8 |
| `short_description` | **8000** | 39.8 |
| `extended_description` | **7999** | 104.8 |

The maxima are suspiciously round, which suggests a truncation cap rather than a natural bound.
One title contains a newline — a rendering hazard for both the TUI and the web grid. There are no
control characters anywhere.

## Tags and unicode

* **98 distinct tags**, 9,168,729 associations, **no non-ASCII tag names**, longest 29 characters.
* Top tags are Wallpaper Engine categories: `Wallpaper` (1.64M), `Everyone` (1.30M), `Scene`
  (856k), `Video` (788k), `1920 x 1080` (533k).
* **704,525 titles are non-ASCII** (40.8%) — unicode handling is the main path, not an edge case.
  A sample of the categories present: `Lo` (CJK and other letters) dominant, plus `So` (emoji),
  `Cf` (format characters such as zero-width), and `Mn` (combining marks).

## The live target

`app_discovery` holds exactly one row: **appid 431960 (Wallpaper Engine)**, with `last_cursor`
`AoJckZidMXaL38lT` and enrichment filters
`Tags contains Mature AND Tags contains Video AND File Size > 100000000`.
