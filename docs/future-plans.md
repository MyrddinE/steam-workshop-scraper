# Future Plans

Changes we intend to make, kept separate from the documents that describe how the system works
today. Nothing here is implemented. Defects in the current code live in
[code-issues.md](code-issues.md); this file is for enhancements, not repairs.

Status values: **Deferred** (agreed direction, deliberately parked), **Under discussion** (open
questions remain), **Planned** (agreed and ready to start).

Findings recorded here were measured or exercised against a copy of the production snapshot
described in [live-data-profile.md](live-data-profile.md) — 1,725,544 items, roughly 1.9 GB.
Absolute timings are hardware-dependent; the ratios are the point.

---

## UI enhancements: web parity, queue-state statistics, page performance

**Status: Planned.** Work has started on the three gaps the owner named — daemon control, the
statistics surface, and the translation toggle. The schema-dependent parts below (per-queue
completion timestamps, and the queue indexes) stay deferred and take their own migration, so the
metrics split can land without waiting on them.

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
| Statistics screen (`ctrl+r`) | Not present | The only affordance is a bare link to the raw JSON endpoint (`templates/index.html:95`). Clicking it leaves the single-page app for an unstyled JSON document, with no way back, and the web UI does not save view state. |
| Analysis screen (`ctrl+?`) | Not present | The endpoint returns data, but the client never calls it. |
| Tag statistics | Not present | Same — endpoint only, no client reference. |
| Author list and jump-to-author | Not present | Same. No author affordance exists in the web UI at all. |
| Daemon start/stop/restart (`ctrl+d`) | Partial | The web UI exposes pause and resume only. Start, stop, restart and the running status/PID all live in `DaemonManagerScreen` with no route behind them. |
| Daemon log view | Partial | The TUI pane exists but is inert — the tail call is commented out (`src/tui.py:316`), recorded as issue 10 in [code-issues.md](code-issues.md). A web endpoint would give the log a working consumer and could be reused to repair the TUI pane. |
| Translation toggle (`ctrl+w`) | Partial | The server prefers the translated field and offers no way back to the original (`src/webserver.py`). The queueing half is already at parity: opening an item bumps translation priority in both UIs. |
| Translation-queued notice | Not present | The TUI says when a translation is requested and still pending (`src/tui.py:796`); the web shows the raw `translation_priority` number in a debug tooltip. |
| Subscription queue (`s`, `l`) | Present | Genuine parity: toggle, indicator, and queued list. The web queue additionally drains itself. |
| Detail-pane queue/unqueue buttons | Not present | The TUI has both (`src/tui.py:642`); the web only supports the `s` key while a grid cell holds focus. |
| Clear Pending Database | Not present | A TUI command-palette action (`src/tui.py:1697`) with no route or element. |
| Save filter for the scraper (`ctrl+s`) | Partial | The happy path works, but `/api/save_filter` answers 400 when no target AppID is set and the client reports success regardless — issue 14 in [code-issues.md](code-issues.md). |
| View state persistence | Partial | The web UI reads the TUI's saved state but never writes it, so anything done in the browser is lost on navigation or reload. It also restores neither the selected item nor the scroll position. |

### Statistics: from raw columns to queue state

The current screen reports raw column values — status distribution, fetch recency, a translation
classification, per-level priority counts, app tracking, and tag frequencies. Those describe storage,
not progress. The useful question is the state of the backlog: what is outstanding, how fast it is
draining, and how long it will take.

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
schema question rather than a presentation one.

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

The whole statistics payload takes roughly **5.4 seconds**, and three separate endpoints request it:

* The statistics endpoint computes all of it to return all of it.
* The tag endpoint computes all of it and returns one field — about 5 seconds of work for under 2 KB.
* The analysis endpoint runs a separate expensive query.

Two further problems compound this:

1. **A second stall sits on the search path.** The search flow awaits the percentile-cutoff query
   *after* clearing the results grid and *before* fetching any items, so every fresh search or filter
   change blanks the pane for roughly five seconds. The cutoff-dependent colouring already degrades
   gracefully to an unknown marker when cutoffs are absent, so it does not need to be awaited at all.
2. **The refresh throttle is global.** Statistics refresh no more often than 50× the previous
   duration, so a five-second query yields a refresh interval of about four minutes. That rule is
   reasonable per metric and wrong for the screen as a whole.

### Approach

1. **Split the monolith into named metrics.** One function per statistic, each with its own cost and
   its own cache lifetime, so a cheap metric is never priced at the cost of an expensive one and the
   tag endpoint stops paying for everything else.
2. **Move the per-item classification into SQL.** The 1.7-second Python loop answers a question
   SQLite can answer in 255 ms.
3. **Tier and stream.** Draw the instant metrics (<10 ms) immediately, then the fast tier
   (50–80 ms), then the slow tier, each replacing its own element as it lands. Both front ends
   consume the same metric definitions.
4. **Throttle per metric,** not globally.
5. **Get the cutoff query off the critical path.** Render results first; apply score colouring when
   cutoffs arrive.
6. **Take the tag-compaction write off the render path.** Opening a statistics screen should not
   perform database maintenance, even when that maintenance is idempotent.
7. **Decide the fate of the three unused endpoints.** Build the missing UI for them, or delete them.
   Leaving an endpoint able to emit a multi-megabyte response to no caller is worse than either
   option.

### Queue indexes

The three unindexed queues account for most of the slow tier, and the same missing indexes also
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

**Recommendation:** adopt them, as part of the schema work below rather than the repair work in
flight.

### Sequencing

The schema changes this plan needs are additive and independent of the migration currently in flight
for the full-text index. They should therefore take the next migration number rather than sharing
one, so each can be deployed and reverted on its own.

### Open decisions

* Build UI for the three unused endpoints, or delete them.
* Whether coverage should be expressed against discovered items only, or whether discovery progress
  should be presented as a separate "still exploring" indicator.
* Whether ETA should be shown for queues whose completion timestamps are newly added, before there is
  enough history for a stable rate.
