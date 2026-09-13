# Code Issues

Known defects in the current source. Each entry was re-checked against the code rather than carried
forward from an earlier list, and resolved entries are deleted rather than marked as fixed.

* **Checked against source**: `4fb947f`.
* **Snapshot, not a tracker**: re-read the code before acting on an entry.
* **Priorities** are judgement calls about impact, not measurements.
* **Status**: `Open` (a defect), `Unverified` (a claim not yet tested), `Informational` (true and
  worth knowing, but not a defect to fix).

Each entry describes only what is wrong and points at the documentation that describes how the
system is meant to behave; the fix is not specified here.

| # | Issue | Status | Priority | Details |
|---|---|---|---|---|
| 1 | Priority decay is asymmetric | Open | Medium | Only `api_priority` has a runtime staleness sweep (`src/daemon.py:316`, `0 → 1`). `needs_web_scrape` and `needs_image` reach priority `1` solely from one-time migrations (`src/database.py:703`, `src/database.py:731`), so the shared scale's "backlog or stale refresh" step exists for one queue of four. The web scraper's selector-miss step-down is a decay of a different kind and does not change this. Scale: [data-model.md](data-model.md#queue-priorities). Measured distributions: [live-data-profile.md](live-data-profile.md). |
| 2 | Port selection races between probe and bind | Open | Low | `_start_webserver` binds a socket to test a port and closes it again (`src/tui.py:1801`, closed at 1812 and 1817) before Waitress binds later (`src/tui.py:1829`), so another process can take the port in between. Selection order: [tui.md](tui.md#_start_webserver). |
| 3 | `api_priority = 2` sits outside the documented scale | Open | Low | Written by the image worker's failure path (`src/image_worker.py:134`) and the web worker's request-failure path (`src/web_worker.py:124`) as a retry marker. It is not one of `0/1/3/5/10`, and no other queue uses it. [data-model.md](data-model.md#queue-priorities). |
| 4 | `request_delay_seconds` is accepted without deprecation | Open | Low | `src/daemon.py:89` silently falls back to the legacy key when `api_delay_seconds` is absent, so an old config keeps working with no signal that the name changed. The shipped example no longer advertises it. [config-security.md](config-security.md). |
| 5 | `update_app_tracking` and `update_app_tracking_page` are never called | Open | Low | Defined at `src/database.py:2191` and `src/database.py:2202`, imported at `src/daemon.py:16`, but only `update_app_tracking_cursor` is used (`src/daemon.py:643`). Discovery flow: [data-pipeline.md](data-pipeline.md). |
| 6 | Userscript version must be kept in step by hand | Open | Low | `templates/index.html:6` declares `content="6"` and `userscripts/steam_subscribe.user.js:4` declares `@version 6`. The script compares the two at load and refuses to run when outdated (`userscripts/steam_subscribe.user.js:160`), but nothing keeps the literals equal. Check behaviour: [web-ui.md](web-ui.md). |
| 7 | `language` is never populated | Open | Low | Present in `WORKSHOP_ITEM_COLUMNS` (`src/database.py:18`) and the merge allow-list, so a response carrying it would be stored — none has, and every live row is NULL. [data-model.md](data-model.md#known-gaps). |
| 8 | `_SafeStreamHandler` startup message is unverified | Unverified | Low | `src/daemon_runner.py:116` reports the active handler class names at startup; whether that list matches the handlers actually attached was never tested. Handler behaviour: [cross-platform.md](cross-platform.md). |
| 9 | Migration chain still references the dropped `tags` column | Informational | Info | `CREATE TABLE` keeps the historical `tags` name because migrations 1→2 and 5→6 read it (`src/database.py:690`), and a later migration drops it defensively while logging the skip. Fresh databases must therefore replay the old shape. [schema-migrations.md](schema-migrations.md). |
| 10 | `status = 206` is never written | Informational | Info | The schema and one migration query allow a partial-data status (`src/database.py:704`), but no code writes it and the live database contains zero such rows. [data-model.md](data-model.md#known-gaps). |

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
