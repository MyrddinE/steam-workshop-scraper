# Code Issues

Known defects in the current source. Each entry was re-checked against the code rather than carried
forward from an earlier list, and resolved entries are deleted rather than marked as fixed.

* **Checked against source**: `bf9c3ae`.
* **Snapshot, not a tracker**: re-read the code before acting on an entry.
* **Priorities** are judgement calls about impact, not measurements.
* **Status**: `Open` (a defect), `Unverified` (a claim not yet tested), `Informational` (true and
  worth knowing, but not a defect to fix).

Each entry describes only what is wrong and points at the documentation that describes how the
system is meant to behave; the fix is not specified here. Figures marked *measured live* come from
the production database on 2026-09-12.

| # | Issue | Status | Priority | Details |
|---|---|---|---|---|
| 1 | Discovery is skipped while the queue holds work it cannot fetch | Open | High | `seed_database` skips discovery while `count_unscraped_items` is at or above `target_new` (`src/daemon.py:610`), but that count is `api_fetched_at IS NULL` (`src/database.py:1490`) — items discovered and never successfully fetched, which sit at `api_priority = 0`. The fetch requires `api_priority > 0 AND (status IS NULL OR status != -1)` (`src/database.py:1477`). Measured live: the 890 counted items are 702 failures, 116 not-founds, 20 dead and 52 never attempted, of which **0** are fetchable — the two populations do not overlap at all, so discovery is suppressed permanently and the queue cannot refill. Intended rule: [data-pipeline.md](data-pipeline.md#discovery-phase). |
| 2 | Opening a detail pane re-queues the same item every three seconds | Open | High | The web UI's detail poll refetches the open item every three seconds (`templates/index.html:492`, `:494`), and that endpoint applies detail priority on every request (`src/webserver.py:215` → `src/database.py:2260`, setting `api_priority = 10`). The fetch orders by `api_priority DESC` (`src/database.py:1478`), so whatever is on screen leads the queue: it is fetched, cleared, and re-queued three seconds later, indefinitely. Measured live: the repeating item held `api_priority = 10`. The TUI repeats this on highlight (`src/tui.py:1547`). Intended: [data-model.md](data-model.md#queue-priorities), [web-ui.md](web-ui.md). |
| 3 | Transient API failures are dequeued instead of retried | Open | High | The 500 path logs "Retrying later" (`src/daemon.py:406`) but has already set `api_priority = 0` (`src/daemon.py:397`), and the staleness sweep promotes only rows with `status = 200` (`src/daemon.py:317`). A transient failure therefore removes the item from every queue permanently, with nothing to bring it back. Measured live: 702 items sit at status 500 with `api_priority = 0`. Intended: [architecture.md](architecture.md#reliability). |
| 4 | Changed content does not re-queue web scrape or image work | Open | Medium | Only translation reacts to a source edit: `translation_is_current` compares `translate_version` against `steam_updated_at`. Web scrape and image are never re-queued when `steam_updated_at` advances, and `scrape_version` — written for exactly that comparison — has no consumer. The intended model is that the API refresh is the change detector: it is the cheapest call and the only stage that goes stale on a timer, and when it observes a changed `steam_updated_at` the dependent stages for that item follow. That `needs_web_scrape` and `needs_image` reach priority `1` only from one-time migrations is consistent with this model rather than a defect in it. [timestamps.md](timestamps.md), [data-model.md](data-model.md#queue-priorities). |
| 5 | Port selection races between probe and bind | Open | Low | `_start_webserver` binds a socket to test a port and closes it again (`src/tui.py:1801`, closed at 1812 and 1817) before Waitress binds later (`src/tui.py:1829`), so another process can take the port in between. Selection order: [tui.md](tui.md#_start_webserver). |
| 6 | `api_priority = 2` sits outside the documented scale | Open | Low | Written by the image worker's failure path (`src/image_worker.py:134`) and the web worker's request-failure path (`src/web_worker.py:124`) as a retry marker. It is not one of `0/1/3/5/10`, and no other queue uses it. [data-model.md](data-model.md#queue-priorities). |
| 7 | `request_delay_seconds` is accepted without deprecation | Open | Low | `src/daemon.py:89` silently falls back to the legacy key when `api_delay_seconds` is absent, so an old config keeps working with no signal that the name changed. The shipped example no longer advertises it. [config-security.md](config-security.md). |
| 8 | `update_app_tracking` and `update_app_tracking_page` are never called | Open | Low | Defined at `src/database.py:2191` and `src/database.py:2202`, imported at `src/daemon.py:16`, but only `update_app_tracking_cursor` is used (`src/daemon.py:643`). Discovery flow: [data-pipeline.md](data-pipeline.md). |
| 9 | Userscript version must be kept in step by hand | Open | Low | `templates/index.html:6` declares `content="6"` and `userscripts/steam_subscribe.user.js:4` declares `@version 6`. The script compares the two at load and refuses to run when outdated (`userscripts/steam_subscribe.user.js:160`), but nothing keeps the literals equal. Check behaviour: [web-ui.md](web-ui.md). |
| 10 | `language` is never populated | Open | Low | Present in `WORKSHOP_ITEM_COLUMNS` (`src/database.py:18`) and the merge allow-list, so a response carrying it would be stored — none has, and every live row is NULL. [data-model.md](data-model.md#known-gaps). |
| 11 | Dead items keep their queue priority | Open | Low | An item marked dead retains `api_priority > 0`. The 404 path clears it now (`src/daemon.py:397`), so this is legacy data rather than an ongoing leak, but 10,476 dead rows still carry a queue priority against 1 fetchable item. They are excluded from fetching by the status test, yet they inflate any count of `api_priority > 0` and anything that treats that column as a measure of outstanding work — including the guard in #1. [data-model.md](data-model.md#queue-columns). |
| 12 | Migration chain still references the dropped `tags` column | Informational | Info | `CREATE TABLE` keeps the historical `tags` name because migrations 1→2 and 5→6 read it (`src/database.py:690`), and a later migration drops it defensively while logging the skip. Fresh databases must therefore replay the old shape. [schema-migrations.md](schema-migrations.md). |
| 13 | `status = 206` is never written | Informational | Info | The schema and one migration query allow a partial-data status (`src/database.py:704`), but no code writes it and the live database contains zero such rows. [data-model.md](data-model.md#known-gaps). |

## Recently closed

Removed from the table above rather than marked resolved. Each is now documented as current
behaviour, or covered by a test:

| Was | Now |
|---|---|
| Full-text index missed 62.9% of the library | Migration 14→15 rebuilds it and installs sync triggers — [search-filter.md](search-filter.md), [schema-migrations.md](schema-migrations.md) |
| The shipped example config was not valid YAML | Fixed, with a regression test that it parses |
| Background paths re-queued already-translated fields | `translation_is_current` gates every trigger — [data-pipeline.md](data-pipeline.md#what-queues-a-field-for-translation) |
| Version keys were written but never compared | `translate_version` now drives re-translation — [timestamps.md](timestamps.md) |
| Silent `except ...: pass` handlers were unaudited | All 24 classified; each states why silence is safe, or logs and captures |
| The `_SafeStreamHandler` startup message was unverified | Verified accurate: the handler list is complete before `basicConfig(force=True)`, and the message reads back `root.handlers` (`src/daemon_runner.py:96`) |
| Priority decay was asymmetric across queues | Superseded: the intended model is change-driven, not per-queue sweeps — see #4 |
