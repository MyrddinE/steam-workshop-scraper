# Future Plans

Changes we intend to make, kept separate from the documents that describe how the system works
today. The statistics rework in the workstream below has landed; whatever is still marked
deferred or planned is not implemented. Defects in the current code live in
[code-issues.md](code-issues.md); this file is for enhancements, not repairs.

Status values: **Deferred** (agreed direction, deliberately parked), **Under discussion** (open
questions remain), **Planned** (agreed and ready to start), **Landed** (implemented).

Findings recorded here were measured or exercised against a copy of the production snapshot
described in [live-data-profile.md](live-data-profile.md) — 1,725,544 items, roughly 1.9 GB.
Absolute timings are hardware-dependent; the ratios are the point.

---

## UI enhancements: web parity, queue-state statistics, page performance

**Status: Landed.** The statistics rework is implemented: the monolithic payload is
split into named metrics in `src/metrics.py`, and both front ends render one independent
chunk per metric, ordered and throttled by measured cost rather than a fixed classification
([tui.md](tui.md), [web-ui.md](web-ui.md)). The schema-dependent half landed with the
per-queue completion timestamps that throughput and ETA read, and the two parity gaps this
paragraph used to list — the author list and view-state persistence — are both **Present** in
the table below.


Bring the web UI to parity with the TUI, rebuild the statistics surface in both so it reports the
state of the work queues rather than raw column values, and remove the stalls that make the page slow
to load. These are one workstream because they share a cause: a single monolithic statistics query
feeds several endpoints, and neither front end has a metrics layer to draw from.

The three parts are separable, and the performance work is worth doing even if the rest is not.

### Scope

* **Both front ends.** The statistics must be produced once and rendered by both the TUI
  ([tui.md](tui.md)) and the web UI ([web-ui.md](web-ui.md)), so parity is structural rather than two
  implementations kept in step by hand.
* **Shared dependency.** `search-filter.md` governs the filter semantics both UIs send; the metrics
  work must not change query semantics, only how results are obtained and delivered.

### Verified parity gaps

The web UI covers the search surface well. The gaps are in the *inspection* surface. This table was
produced by exercising each endpoint and checking whether the client actually calls it, not by
reading markup — the distinction proved to matter, because three endpoints exist that no client uses.

| TUI feature | Web UI | Evidence |
|---|---|---|
| Statistics screen (`ctrl+r`) | Present | Both front ends stream the metrics as independent chunks: the TUI `StatsScreen` and the web `#stats-modal` panel. The bare link to the raw JSON endpoint is gone, and each metric appears as soon as it is ready rather than waiting for the slowest. |
| Analysis screen (`ctrl+?`) | Present | The `#analysis-modal` panel calls `/api/analysis` and renders the bucket table, with the bucket width defaulting to the TUI's seven days. |
| Tag statistics | Present | The panel renders a tag summary from the `tag_counts` metric. It is the most expensive single statistic (a 9.2M-row join); the owner has decided it stays because they want the results. |
| Author list and jump-to-author | Present | Jump-to-author enters the same single-creator mode the TUI has — filters replaced, sort kept, `Return` restoring an in-memory snapshot (`jumpToAuthor`, `returnFromAuthor`). The author *list* is now consumed by a creator picker the TUI does not have; `/api/authors` existed with no client before it. See [web-ui.md](web-ui.md#the-creator-list) for why the list earns its place and [tui.md](tui.md#jump-to-author) for the one-sided position. |
| Daemon start/stop/restart (`ctrl+d`) | Present | Both UIs drive one shared `DaemonController`. Routes: `/api/daemon`, `/api/daemon/start`, `/stop`, `/restart`, `/log`. |
| Daemon log view | Present | Both sides poll one `DaemonController.tail_log` incrementally — the web panel through `/api/daemon/log`, the TUI's pane through `_poll_tail` on a two-second timer (`src/tui.py:950`) — and every read is bounded to `TAIL_BYTES`, so neither scans the file. The subprocess-based `_start_tail` is gone. |
| Translation toggle (`ctrl+w`) | Present | Both language variants ship in the detail payload, so switching costs no request. The toggle appears only when `translate_version` is set, as in the TUI. |
| Translation-queued notice | Present | Shown above the description while `translation_priority > 0` and no translation is stored, matching the TUI. |
| Subscription queue (`s`, `l`) | Present | Genuine parity: toggle, indicator, and queued list. The web queue additionally drains itself. |
| Detail-pane queue/unqueue buttons | Present | The detail pane shows one button whose label follows `is_queued_for_subscription`, alongside the `s` key on a grid cell. It POSTs `/api/toggle_subscription_queue/<id>`, reads the item back through the read-only route, re-renders from that, and keeps the grid star in step. |
| Delete Never Fetched Items | Present | The `#btn-delete-never-fetched` affordance confirms first, naming the exact set the TUI deletes (no status/404 and no successful fetch), then calls `POST /api/delete_never_fetched_items`, a thin wrapper over the same `delete_never_fetched_items`. The route reports the row count and the UI shows it; there is no dry-run, matching the TUI. |
| Save filter for the scraper (`ctrl+s`) | Present | The route still answers **400** with "No target AppID configured" when none is set, but the client reads the response: a non-2xx shows the server's message, a network error shows its own, and only a 2xx claims success (`templates/index.html:1211`). |
| View state persistence | Present | The browser keeps its own view (filter rows, sort, open item, scroll position) in `localStorage` under the versioned `view.state.v1` entry, restored by `_restoreView` with bounded paging. Local state wins outright; `/api/state` is the first-visit seed only. It deliberately does not write `.tui_state.yaml`, whose shape belongs to the TUI. |

### Statistics: from raw columns to queue state

The screen used to report only raw column values — status distribution, fetch recency, a translation
classification, per-level priority counts, app tracking, and tag frequencies. Those describe storage,
not progress. The useful question is the state of the backlog: what is outstanding, how fast it is
draining, and how long it will take. The presentation half of that has landed: the queue signals
below that do not need new history are now metrics, and both front ends render them
(`coverage`, `dead_items_by_queue`, `priority_breakdowns`, `item_counts`).

For each of the four work queues — API fetch, web scrape, image download, translation — the signals
that answer that are:

| Signal | Purpose |
|---|---|
| Outstanding depth | How much work is queued right now. |
| Priority mix | How much is urgent (visible or open) versus backlog. |
| Throughput | Items completed per hour or day. |
| Burn-down and ETA | Outstanding divided by throughput: time to drain at the current rate. |
| Last success | Freshness, and whether a worker has stalled. |
| Failure rate | Distinguishes slow progress from a worker that is failing its way through the queue. |

Plus an overall view: discovered items, whether discovery is still advancing, and processing coverage
— how much of the library has API data, an extended description, an image, a translation, and a known
creator.

**Coverage needs a caveat.** There is no denominator for the Steam Workshop's true size, so "how much
of the workshop has been pulled" can only be stated as coverage of *discovered* items plus the
discovery rate over time. Discovery position is tracked per app; it is a position, not a fraction.

### What each queue can and cannot report

Throughput and ETA need a completion timestamp on our own clock. The schema's three-clocks design
([timestamps.md](timestamps.md)) deliberately separates Steam's time from ours, and the separation
leaves a gap: several completion paths record Steam's version value instead of when we did the work.

| Queue | Completion time available? |
|---|---|
| API fetch | Yes — our clock, written on success. |
| Web scrape | No — the stored version is Steam's value, not when we scraped. |
| Image download | No — nothing records completion. |
| Translation | No — the stored version is Steam's value. Note that the equivalent column on the user table *is* our clock; item rows are the asymmetric case. |

So **three of the four queues cannot report a rate or an ETA from the data alone**; only the API queue
can. This is the single largest gap between the current stats screen and the intended one, and it is a
schema question rather than a presentation one. *(Landed: migration 26→27 added
`web_scraped_at`, `image_fetched_at` and `translated_at`, and the per-queue throughput and
last-success metrics read them. A rate is now available for all four queues, and the `queue_eta`
metric reports each one's outstanding depth, active-time rate and `53d ± 30%` time to drain from
whatever history exists — see [data-pipeline.md](data-pipeline.md#queue-state-outstanding-rate-and-time-to-drain).)*

**Decision taken:** add a per-queue completion timestamp for the three queues that lack one, rather
than inferring rates from sampled queue-depth deltas. Sampled deltas would avoid a migration but
provide no history, reset on restart, and only work while the process is being watched.

### Measured cost

On the snapshot above:

| Statistic | Cost | Cause |
|---|---|---|
| Totals, user count, app tracking | 0.1–4 ms | covering indexes |
| Translation queue breakdown | 4 ms | covering index |
| Status distribution | 57 ms | covering index |
| Fetch recency | 79 ms | covering index |
| API queue breakdown | 219 ms | no index — full scan |
| Tag frequencies | 358 ms | 9.2M-row join |
| Image queue breakdown | 401 ms | no index — full scan |
| Web queue breakdown | 470 ms | no index — full scan |
| Translation classification | 1,709 ms | full table scan with the classification done in Python |
| The same classification done in SQL | 255 ms | 6.7× faster |

These measurements are the source of the `seed_ms` hints in `src/metrics.py`. They are one
database's costs on one machine — a hint to break the tie on the first ever open, not a
classification — and each front end replaces them with the durations it actually measures.

The monolithic statistics payload took roughly **5.4 seconds**, and three separate endpoints
requested it:

* The statistics endpoint computed all of it to return all of it.
* The tag endpoint computed all of it and returned one field — about 5 seconds of work for under 2 KB.
* The analysis endpoint runs a separate expensive query.

With the metrics split, `/api/tags` now computes only `tag_counts` and each front end requests one
metric at a time. Two further problems compounded this:

1. **A second stall sat on the search path.** *(Fixed.)* The search flow awaited the percentile-cutoff
   query *after* clearing the results grid and *before* fetching any items, so every fresh search or
   filter change blanked the pane for as long as that query took — measured at 8+ seconds in the
   browser. It was assumed this needed nothing more than removing the `await`, because the colouring
   was believed to "degrade gracefully to an unknown marker". It did not: with no cutoffs the three
   thresholds were all `0`, so every score matched `p99` and rendered gold with `!` markers. An
   uncoloured state had to be added first, and a search now also clears the previous set's cutoffs
   rather than colouring one filter set with another's percentiles. With that in place the query is
   started and left to land, and the rows already on screen are re-coloured from the score kept on
   each span.
2. **The refresh throttle is global.** *(Fixed for the statistics screen.)* Statistics refreshed no
   more often than 50× the previous duration, so a five-second query yielded a refresh interval of
   about four minutes. The 50× rule now applies per metric against that metric's own measured
   duration, and each front end orders its requests by what each metric last cost.

### Approach

1. **Split the monolith into named metrics.** *(Landed: `src/metrics.py`.)* One function per
   statistic, each with its own cost and its own cache lifetime, so a cheap metric is never priced
   at the cost of an expensive one and the tag endpoint stops paying for everything else. The
   module earlier carried a tier per metric; the tiers are gone, because a cost measured on one
   database is an assumption about the data rather than a property of a query.
2. **Move the per-item classification into SQL.** *(Landed.)* The 1.7-second Python loop answers a
   question SQLite can answer in 255 ms.
3. **One independent chunk per metric.** *(Landed.)* Every metric renders into its own element and
   appears as soon as it is ready; nothing is grouped or classified. Ordering and throttling are
   learned from measured durations rather than a fixed tier table: each front end seeds its request
   order from `seed_ms`, then replaces those hints with the durations it actually measured
   ([tui.md](tui.md), [web-ui.md](web-ui.md)). The `seed_ms` values in `src/metrics.py` are hints
   from one measurement on one database; they only break the tie on the very first open and are
   meant to be superseded by real measurements.
4. **Throttle per metric,** not globally. *(Landed: both front ends derive each metric's interval
   from that metric's own measured duration, so a slow query cannot stretch a fast one's refresh.)*
5. **Get the cutoff query off the critical path.** Render results first; apply score colouring when
   cutoffs arrive. *(Landed for the web UI. It needed an uncoloured state as well as the removed
   `await` — see the note under "Measured cost".)*
6. **Take the tag-compaction write off the render path.** Opening a statistics screen should not
   perform database maintenance, even when that maintenance is idempotent. *(Still open: it now runs
   once per tag-metric arrival rather than on every screen update.)*
7. **Decide the fate of the three unused endpoints.** Build the missing UI for them, or delete them.
   *(Landed: the web statistics panel consumes the tag metric, the web view window analysis panel
   consumes `/api/analysis`, and the creator picker consumes `/api/authors`.)*

### Queue indexes

**Landed (migration 24→25).** The three partial composite indexes are created by
migration 24→25 (`idx_web_scrape_queue`, `idx_image_queue`, `idx_api_queue`,
`EXPECTED_VERSION = 25`) and documented in
[schema-migrations.md](schema-migrations.md). `tests/test_queue_indexes.py`
asserts with `EXPLAIN QUERY PLAN` that all three worker polls and the web and
image statistics breakdowns plan through the matching index with no `TEMP
B-TREE` sort — the check that the index's column order really does satisfy the
poll's `ORDER BY`. The measurements below are the evidence the change was
adopted on and are kept as measured.

The three unindexed queues account for most of the expensive metrics, and the same missing indexes also
affect the crawler: the web and image workers select their next item with a full scan plus a sort on
**every poll**, not only when statistics are requested.

Partial composite indexes of the form

```sql
CREATE INDEX ... ON workshop_items(<queue_column> DESC, api_fetched_at ASC)
  WHERE <queue_column> > 0
```

were measured on a copy. The `WHERE` clause matches the worker poll and the statistics query exactly,
and the composite column order satisfies the poll's `ORDER BY`, so the poll reads one index entry and
stops.

| Query | Before | After | Index size |
|---|---|---|---|
| Next web scrape item (worker poll) | 258 ms | ~0 ms | 28.4 MB |
| Next image item (worker poll) | 252 ms | ~0 ms | 23.8 MB |
| Web queue breakdown | 470 ms | 70 ms | *(same index)* |
| Image queue breakdown | 401 ms | 59 ms | *(same index)* |
| API queue breakdown | 219 ms | 0.4 ms | 0.19 MB |

Build time 2.3 s; total 52.4 MB, or 2.8% of the database. Index maintenance measured at 1.0 µs per
completion update — 0.13% duty at the crawler's pace.

Caveats: a `GROUP BY` over a queue still costs time proportional to the queued rows, so the
breakdowns improve by roughly 7× rather than becoming free; and the web queue's `> 0` predicate
covers about 89% of rows, so its partial index is nearly full-size.

**One row's framing is narrower than the code.** The shipped `priority_breakdowns` metric covers
`translation_priority`, `image_priority` and `web_scrape_priority` only, so the table's "API queue
breakdown" has no caller in the statistics today — it is the `GROUP BY api_priority` shape the row
was measured on. The API queue is read in shipped code by the fetch poll and by
`count_fetchable_items` (the daemon's "is there work?" count), and the new index serves both; the
plan's breakdown shape is index-served too. `tests/test_queue_indexes.py` pins the shipped queries
and notes the breakdown shape's missing caller.

**Recommendation:** adopt them, as part of the schema work below rather than the repair work in
flight. *(Done: migration 24→25.)*

### Sequencing

The schema changes this plan needs are additive and independent of the migration currently in flight
for the full-text index. They should therefore take the next migration number rather than sharing
one, so each can be deployed and reverted on its own. *(The queue indexes took their own number,
24→25, following this rule. The per-queue completion timestamps took 26→27: `web_scraped_at`,
`image_fetched_at` and `translated_at`, with the per-queue throughput and last-success metrics that
read them. Burn-down and ETA landed on top of them as the `queue_eta` metric — see below.)*

### Open decisions

* Whether `/api/analysis`, `/api/authors` and the untiered `/api/stats` should get UI or be deleted.
  *(Resolved for `/api/analysis`: the web view window analysis panel now consumes it, matching the
  TUI's `ctrl+?` screen. `/api/authors` and the untiered `/api/stats` remain undecided.)*
* Whether coverage should be expressed against discovered items only, or whether discovery progress
  should be presented as a separate "still exploring" indicator. (The landed coverage metric uses
  discovered live items as its denominator.)
* Whether ETA should be shown for queues whose completion timestamps are newly added, before there is
  enough history for a stable rate. *(Resolved: it is shown immediately, and the uncertainty is a
  percentage that is widest while the evidence is thinnest, so a newly-started queue reads as
  uncertain rather than as a stable wrong answer — [data-pipeline.md](data-pipeline.md#the-uncertainty).)*

---

## Stage handoffs: prove the next stage can see what the last one left

**Status: Landed.** The consumer predicates are one named function each (`api_fetch_queue_predicate`,
`web_scrape_queue_predicate`, `image_queue_predicate`, `translation_queue_predicate`, and the
`queued_anywhere_predicate` the invariant needs) in `src/database.py`, and the polls interpolate them,
so the worker and the test ask the same question; `tests/test_handoff_contract.py` tests all five
handoffs by the contract rather than by re-writing their SQL. Both detectors are statistics —
`dead_queued` and `queued_nowhere` — rendered by both front ends. The handoff table is in
[data-pipeline.md](data-pipeline.md). What the plan predicted is what the counts show: they are cheap,
they are meant to be zero, and they were measured at 28 and 1 rather than zero, which is the argument
for having them in the statistics.

The detectors were only as good as the predicate they asked, and the translation half was asking the
wrong one. `queued_anywhere_predicate` was built from `translation_priority_predicate` — the item-level
mirror — while the translation poll hands out *rows* of `translation_queue`; the mirror and the row are
cleared together only on the success path, not when an item is marked dead. *Measured* on the v35
snapshot (`real-copy.db`, 1,725,544 items, 68,323 dead): `dead_queued` read **0** while **910 dead items
held 1,016 `translation_queue` rows**, every one with the mirror already cleared — the same population
the paragraph above measured as "1 dead item is still queued for translation", grown. The producer
(`_settle_api_failure`) now deletes the item's rows in the same transaction as `fetch_status = -1`, the
predicate asks the queue row as well as the flags, and migration 35→36 removed the rows already
stranded, so the detector reads zero and no dead item's fields are translated or paid for. The tests
that pin it are `tests/test_dead_translation_rows_migration.py`,
`tests/test_handoff_contract.py` and `tests/test_metrics.py`.

Three defects found in quick succession turned out to be one shape. In each, a stage finished with an
item, wrote the item's new state, and reported success — and the state it wrote was one the next
stage's query could not see. Nothing errored, nothing logged a failure, and the loss showed up only
much later as work that mysteriously never happened.

| Issue | What the producing stage wrote | Why the consuming stage could not see it |
|---|---|---|
| 17 | `fetch_status = -1` and `api_priority = 0` | `web_scrape_priority` and `image_priority` were left set, so the worker polls kept selecting a dead item forever |
| 19 | `extended_description = NULL`, `web_scrape_priority = 0` | The consumer requires a description; the item was recorded as done and never retried |
| 20 | `api_priority` left to the column default | On a migrated database that default is `0` and the fetch queue requires `> 0`, so a discovered item was queued nowhere |
| 66 | `fetch_status = -1` with the four flags cleared | `translation_queue` rows were left behind, and the poll selects rows while `queued_anywhere_predicate` asked only the item-level mirror — so the dead item's fields were still translated, and `dead_queued` read zero |

The common cause is that each stage's exit condition is written down only in the stage that performs
it, while the next stage's entry condition lives in a different function. Nothing states the contract
between them, so nothing can test it.

### The invariant to state and test

Every item is, at all times, in **exactly one** of these states:

* queued for the API fetch (`api_priority > 0`), or
* queued for a web scrape (`web_scrape_priority > 0`), or
* queued for an image (`image_priority > 0`), or
* queued for translation (a `translation_queue` row exists; `translation_priority > 0` is its
  item-level mirror), or
* complete for the stage that owns it, or
* deliberately dead (`fetch_status = -1`) and therefore in **no** queue.

Never in none of them by accident, and never in a queue the owning stage has finished with. Issue 19
violates the first kind — recorded as complete while storing nothing; issue 20 is the same violation;
issues 17 and 66 are the second — dead, yet still queued, once by a flag and once by a queue row.

### Approach

1. **Enumerate the handoffs.** Discovery → API fetch; API fetch → web scrape; API fetch → image;
   API fetch and web scrape → translation; and the terminal one, any stage → dead. Record each pair
   with the column the producer writes and the predicate the consumer selects on.
2. **Extract each consumer's predicate into one named function**, so the worker poll and the test ask
   the same question. A test that re-writes the SQL it is checking proves nothing.
3. **Test the contract, not the stage.** For each handoff, take an item the producer has just finished
   with and assert the consumer's own predicate agrees: either it is selected because the work is
   genuinely outstanding, or it is not selected *and* the stage's output is present. A third outcome —
   not selected, and nothing stored — is the bug being looked for.
4. **Add the invariant as a database-level check**, not only as a unit test: one query counting items
   that are queued nowhere and not complete, another counting items that are dead yet still queued.
   Both should read zero, and both are cheap enough to sit in the statistics beside `dead_items_by_queue`. A
   number that is meant to be zero is a better detector than a log line nobody reads.
5. **Make the exit explicit at every call site.** Where a stage completes an item, pass every queue
   flag it intends to change instead of relying on a column default. Issue 20 existed only because a
   `CREATE TABLE` default of `3` and an `ALTER TABLE` default of `0` disagreed and the insert leaned
   on whichever it happened to get.

### What this would have caught

All three, at the moment they were introduced: 19 and 20 by the "queued nowhere and not complete"
count, 17 by the "dead but still queued" count. Both are single statements. They survived as long as
they did because every stage reported success, so the only signal was a coverage figure drifting
downwards over weeks.

*Measured live* on 2026-09-17 against 2,497,545 rows, neither count is zero today: **28** items are
`fetch_status = 200` with no description and in no queue at all, and **1** dead item is still queued for
translation. Both numbers came from running the two statements described above, which is the argument
for putting them in the statistics rather than leaving them in a document: they are cheap, they are
meant to be zero, and one query finds them.

---

## Discovery logging: mark what was queued for the item, not what the filters matched

**Status: Landed.** The marker is chosen in `src/daemon.py` from the
`ScrapeImageOutcome` that `_raise_scrape_and_image_priorities` returns; the behaviour and its
colour are pinned by `tests/test_discovery_marker.py`. The measured evidence that
motivated the change is kept below.

A discovery line carries a red `ignored` marker when the item failed its enrichment filters, and
nothing at all when it did not (`src/daemon.py:931`):

```
[A:2063560223] "gwiezdny papiesz" — ignored
[A:2063564494] "NSX"
```

*(Landed: the red `ignored` marker is gone, and the marker now answers "was a
scrape or an image actually queued for this item" rather than "did the item match
its AppID's enrichment filters". Three shapes:)*

```
[A:...] "title" — current      # nothing was queued
[A:...] "title" — enriching    # the filters matched and work was queued
[A:...] "title"                # work was queued only as backlog (filters did not match)
```

*(`current` is grey (SGR `\033[90m`), `enriching` is green (SGR `\033[32m`). The
word answers "was anything queued", so an item the filters *rejected* still reads
`current` when it has nothing to queue: the marker was never a filter verdict.)*

The marker is therefore on almost every line, and says the least interesting thing about it.
*Measured live* on 2026-09-17 over the last 6 MB of `scraper.log`: **53,523 of 54,057** discovery
lines (**99.0%**) carried it, while 534 (1.0%) did not. A red word that appears 99% of the time is
decoration; the informative event is the one item in a hundred that is about to have its page and
preview fetched, and that is the one currently unmarked.

Invert it: drop the `ignored` marker, and mark the line from whether
`_raise_scrape_and_image_priorities` actually queued work — `enriching` in green (`\033[32m`) when the item also
matched its AppID's filters, `current` in grey (`\033[90m`) when it queued nothing, and no marker
when the queued work is backlog because the filters did not match. One line changes, and the web log
viewer needs nothing: its SGR table already renders 32 as `#98c379` and 90 as `#6b7280`. The TUI's
pane needs nothing either, but for a different reason — `_poll_tail` writes the raw line into a
`RichLog` and nothing in the TUI *decodes* ANSI, so no marker colour has ever been applied by the
widget; the raw escape bytes do reach it, unrendered and zero-width.

*(Landed. `_raise_scrape_and_image_priorities` returns a `ScrapeImageOutcome(enriched, queued)` rather than a
bare bool: `queued` is set when `raise_web_scrape_priority` or `raise_image_priority` is called, and `enriched`
is the filter verdict the two other consumers still need — `_queue_translations` and
`_creator_to_refresh` read `outcome.enriched`, because a filter match is what gates translation and
the creator refresh. The first landed form chose the marker from `enriched` alone, so an item that
matched its filters and queued nothing — its description at the stored revision with a renderable
preview, or a preview the server has already answered with 404 or a non-image type — was drawn
`enriching` for work that was never queued. Both "needs nothing" claims were verified rather than
assumed: the viewer's `ANSI_SGR` table maps `32: '#98c379'` and `90: '#6b7280'`, and `_poll_tail`
writes each raw line into a `RichLog` with no ANSI decoding. Seven tests in
`tests/test_discovery_marker.py` drive the real `_process_item`: an enriched item that queues is
marked `\033[32menriching\033[0m`; a rejected item that queues a backlog scrape carries no escape at
all; an enriched item and a rejected item that both queue nothing are marked
`\033[90mcurrent\033[0m`; an enriched item with only its preview queued is still `enriching`; and
the emitted codes are asserted against the viewer's own table. The three `current` tests fail
against the old `enriched`-only marker, and the image-only one fails against a marker keyed on the
description alone.)*

The old wording is also load-bearing in three places that would move with it:

* `src/daemon_runner.py:39` explains the em-dash encoding trap by naming the "ignored" marker, and
  the example line it describes would no longer exist. *(Landed: the comment now names the
  "enriching" marker.)*
* `tests/test_daemon_runner.py:146` and `tests/test_webserver.py:2285` use an `ignored` line as a
  *sample* — one for the reader's encoding, one for SGR decoding. Neither depends on which word is
  used, but both comments read as if they did, which is how a stale example outlives its subject.
  *(Landed: the encoding sample is now the daemon's own `[A:...] "..." — enriching` line, and the
  SGR driver decodes the green `enriching` marker and asserts the viewer's `#98c379`.)*

---

## Removing the browser bridge from the subscribe path

**Status: Landed.** The browser-free engine in `src/subscribe_engine.py` drives the TUI's subscription
queue and, through `/api/subscribe/<id>`, the Web UI's Subscribe button and queue drain; the owner ran
it for days and then uninstalled the userscript. The bridge is gone, with everything that existed only
to serve it.

The Web UI could not subscribe on its own, and that was the only reason the Tampermonkey bridge
existed: the server could not build a working Steam session request, so a browser tab did the
subscribing and reported the outcome back. Everything the bridge compensated for is now addressed on
the server side — the credential comes from one read (`web_scraper._build_workshop_cookies`), the
request presents the same identity as every scrape, and `/api/subscribe/<id>` records the
confirmation instead of discarding it. That is the same route the TUI has always called, and the Web
UI now calls it too: `doSubscribe` POSTs it, and `_startAutoSubscribe` drains the queue through it one
awaited call at a time, so the flow opens no tab.

**What went, and why it was safe.** The removal was driven by callers, not by this plan's prose: a
route or path was deleted only when, with the userscript gone, nothing in the repository called it.
The userscript file (and the now-empty `userscripts/` directory) went with its `autosubscribe=true`
tab flow; so did `POST /api/sessionid` (the only writer of `_pushed_sessionid`), the bridge's outcome
reports `POST /api/subscribe_failed/<id>` and `POST /api/subscribe_throttled/<id>`, the
`/userscript/<file>` install endpoint, the page's `_userscriptPresent` detection and
`userscript-version` meta, and the now-unread `WEB_DELAY` injection. **Kept because the browser-free
flow calls them:** `GET /api/queued` (the drain's queue read and its overlay poll),
`GET /api/subscribe_failures` and `GET /api/subscribe_throttle` (that poll and the per-item throttle
check — so the plan's "verification poll" and "throttle-reporting endpoints" are only half bridge),
and `POST /api/subscribed/<id>` (the overlay's Cancel and Clear Failed dequeue calls). The two kept
reads now have no writer and report the resting state; removing the drain's use of them would be a
behaviour change, not part of this removal.

**The pacing did not regress.** The tab flow spread many subscribes across a browser session and
reported throttle pages separately; the replacement's pacing is the engine's shared AIMD interval —
the persisted `web_delay` state section of `.daemon_state.yaml` — with the subscribe POST deliberately
exempt because it is an XHR, not a page load. The drain adds no client-side delay and awaits each call, so it never has two
requests in flight and the interval is paid once per item inside the route. Three tests pin it:
`tests/test_webserver.py::test_the_queue_drain_calls_the_subscribe_route_once_per_item_in_order`
(the serial loop), `test_the_route_gates_its_page_read_on_the_shared_interval` (the single gated
read), and `tests/test_subscribe_engine.py::test_every_page_read_waits_the_interval_and_the_post_does_not`
with `test_the_pass_spaces_every_item` and `test_a_wall_grows_and_persists_the_shared_interval` (the
shared interval and its throttle doubling).

---

## Retiring the subscribe confirmation read

**Status: step 1 landed** (`VERIFY_AFTER_SUBSCRIBE = False`) — the confirmation read is retired by
default; the pre-read is **not** next by default.

The engine reads the item page, sends the POST only when the page says the item is not subscribed,
and then reads the page again to confirm. That third read is deliberate while the flow is being
proven, but it is a cost: it **doubles each item's gated page reads** (the POST itself is exempt), so
a queue of N items costs 2N interval-paced reads where 1N would do. It goes first, ahead of any other
change to the engine.

Why it can go: the production confirmation measured the endpoint directly. `POST /sharedfiles/subscribe`
returned `{"success": 1}` for an item that was **already** subscribed, and the item's page was still
`toggled` afterwards with a byte-identical length — so the call is safe to repeat, and the response
body cannot distinguish "newly subscribed" from "already subscribed". The page read is the only
confirmation *available*, not the only one that will always be needed. The step is isolated in
`subscribe_engine.confirm_subscription` behind the module-level `VERIFY_AFTER_SUBSCRIBE` switch, so
retiring it is one call site and one flag and cannot perturb the click, the recording or the capture.

What must not go with it is the **pre-read**, and the order matters:

1. **First, drop the confirmation read** — *landed* (`VERIFY_AFTER_SUBSCRIBE = False`), which removes
   one read per item and leaves the POST's own answer as the record.
2. **Only then, and only once idempotency is trusted beyond a single observation, consider dropping
   the pre-read.** It has two jobs that no other code does: it is the guard against the endpoint ever
   turning out to be a *toggle* (an item that is already subscribed would be unsubscribed by a blind
   POST), and it is the filter that lets a re-run of a queue skip items that are already subscribed
   without spending a request on them. Until idempotency has more than one observation behind it, both
   jobs are load-bearing, and the "already `toggled` → no request at all" short-circuit stays.

**The cheap middle ground.** Rather than a third read or trusting the JSON, the existing daily
subscription reconcile (`src/subscription_sync.py`, the `/my/myworkshopfiles/?browsefilter=mysubscriptions`
walk) can be the confirmer: it costs no extra request, because it already runs once a day and already
writes `own_subscribed`. The price is latency — up to a day to notice that a subscribe silently failed
— and that is the trade to weigh when the confirmation read is retired.

---

## Estimating a subscription pass from measurement

**Status: Landed** (issue 41). The owner decided the approach: this is a progress bar's output, not a
long-running graph — the pane is up for a minute or two at most.

The TUI's queue estimate priced every item at two intervals of the configured delay. *Measured live* on
2026-09-18 that was about a third low, and for a mundane reason: a gated page read costs the interval
**plus the request** — 6.8 s against a 6.0 s delay in the scraper's own loop, 7.7–8.6 s inside the
subscribe pass — and the POST, exempt from the interval, still spends 1.1–1.8 s on the clock. One item
therefore ran about 18 s against an estimate of 12 s.

The decided fix keeps that starting number and adds **a live correction for the rest of the queue**.
`run_subscription_pass` calls its per-item callback synchronously after each item, so the screen times
each finished item between two callbacks and prices the rows still waiting from a running mean of those
observed durations, seeded with the configured guess. The seed is one virtual observation, which is why
the first item moves the estimate a lot and later ones less; the row being processed is already drawn
distinctly ("subscribing...") with no countdown, which is where the timing starts. Nothing is persisted
and no rolling window or rate from past passes is used. See
[tui.md](tui.md#subscription-queue-sl-keys). The web overlay's countdown keeps its own flow's cost and
is deliberately not part of this ([web-ui.md](web-ui.md#subscribe-feature)).

**Rejected: derive the starting number from recorded history.** The subscribe path's requests are
already timestamped twice over — every web download is captured with the moment it was made, and the
daemon log carries the web worker's own pace over weeks — so a per-item cost, with the delay it was
measured at, could have been derived from that history and recomputed when the delay moves. It was
rejected with the live correction above: a transient pane does not need persisted history to be useful,
and keeping it would have cost a store, a re-derivation rule and a staleness question for a number that
is on screen for a minute or two. A rolling mean of the last few item durations was considered and
rejected for the same reason: the running mean over the pass already converges within two or three
items, and seeding it with the configured guess is enough.

---

## One item-update path: a change to an item reaches every display of it

**Status: Landed.** The owner's six-point design is implemented in both front ends: a per-item
subscription registry (`src/item_updates.py` for the TUI, `_itemSubscribers` in
`templates/index.html`), one dispatch point per side (`ScraperApp.dispatch_item_update` /
`dispatchItemUpdate`), updates that carry a block of columns rather than a field, the registry as the
only coupling, and a general registry-scoped poll that is not conditional on anything pending
(`ScraperApp._poll_item_updates`; `_startItemUpdatePoll` in the page). The four invariant tests are in
`tests/test_item_update_path.py`. What is deliberately not done: the daemon-side push for background
changes was not added — the general poll is the agreed acceptable trigger — and the TUI's modal
subscription-queue screen still refreshes its own rows from the subscribe pass's callback rather than
subscribing to the registry, because it is a transient overlay over a list that is already covered.
The old subscription-marker fast paths were left in place as latency, not as correctness; see
[tui.md](tui.md#one-item-update-path) and [web-ui.md](web-ui.md#one-item-update-path).

**Reported symptom.** Three items were subscribed and the stars turned yellow correctly, but when
they downloaded, the detail pane showed the green star and the list did not. The two displays of
the same item disagreed.

**What the code does today — the cause is structural, not a missing trigger.** There is no
notification path between a writer and the components that display an item; each display is
refreshed by whichever code path happens to know about a change.

- The TUI has exactly one list-refresh path, and it is **subscription-scoped by construction**:
  `SubscriptionQueueScreen.refresh_subscription_rows` (`src/tui.py:2543`) documents that "Only the
  subscription columns are replaced, so the rest of the row's data is left as it was". A field
  that changes the marker's *precedence* — `steam_download_seen_at`, which outranks `subscribed` in
  `src/subscription.py` — is therefore outside the scope of the only method that redraws a row.
  It is driven by `_start_subscription_poll`/`_poll_queued_subscriptions` (`:2507`, `:2525`) and
  reached from the detail pane through `getattr(self.app, "refresh_subscription_rows", None)`
  (`:1304`), so it exists mainly while a subscribe pass is running.
- The web grid's poll is conditional on rendered state: `_listNeedsPoll`
  (`templates/index.html:852`) returns true only for a pending stage spinner or
  `subscription_state === 'queued'`, and `_startListPoll`'s tick re-reads only rows that carry a
  spinner or a pending marker. When nothing is pending, the grid stops polling entirely. Its
  `refreshItemState(wid)` (`:1412`) is a per-item path, but it is called from specific actions
  rather than from a general "this item changed" signal.
- Changes made by **background workers have no path at all**: `src/workshop_folders.py:362` writes
  `steam_download_seen_at` from the daemon's folder scan, and neither front end has a channel to that.

So the two front ends do not merely differ in detail — they have differently shaped mechanisms
(one subscription-column-only refresh, one state-conditional poll), which is why the same item can
be current in one component and stale in another.

**The invariant to state and test.** *Any data a front end obtains about an item — from a callback,
a poll, or an action — is reflected in every component that displays that item.* The list row and
the detail pane must never disagree about the same `workshop_id`, and neither may be current while
the other is stale.

**Approach — the owner's design: components subscribe to the items they display.**

1. **A per-item subscription registry in each front end.** A component that displays an item
   (a list row, the detail pane, anything else showing that `workshop_id`) **subscribes to that
   `workshop_id` when it starts displaying it**, and unsubscribes when it stops — so the registry
   always describes what is on screen rather than what exists in the database.
2. **One dispatch point.** Every update the UI receives — from a callback, a poll, an action, or a
   signal from the daemon — goes through a single path that looks up the subscribers for that
   `workshop_id` and hands each of them the update.
3. **The update carries a block of data, not a field.** A subscriber receives **everything about
   the item that came in** — which may be one field or all of them — and applies whatever it
   renders from those values, ignoring the rest. A panel therefore never has to know which fields
   a given producer happened to send, and a field added later reaches every panel that renders it
   without touching any call site.
4. **The registry is the only coupling.** No component is a "target" a caller has to remember to
   refresh: the producer of an update says what changed, and the subscription decides who hears it.
5. **A refresh trigger that is not conditional on one state.** A general poll while either front end
   is open, or better a daemon-side signal for background changes — the folder scan writing
   `steam_download_seen_at`, a scrape or translation completing — so a change made while the user is watching
   arrives without the user having to act. Whatever the mechanism, the web must not stop updating
   merely because nothing is `pending`.
6. **Tests that pin the invariant rather than the paths**: a change written to the database behind
   the front end's back appears in the list row *and* the detail pane; a marker change reaches both
   front ends; a non-subscription change (a download) moves the marker in both; a panel that has
   stopped displaying an item receives nothing.

**What this would have caught.** The owner's report directly: `steam_download_seen_at` moving while the list
held the previous marker. It also covers the general class — any new column that feeds a display
and any new writer of an existing one — because the fix is defined by the invariant rather than by
enumerating the paths.


