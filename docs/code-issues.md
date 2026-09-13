# Code Issues

Known defects in the current source. Each entry was re-checked against the code rather than carried
forward from an earlier list, and resolved entries are deleted rather than marked as fixed.

* **Checked against source**: `1dd6240` (2026-09-13).
* **Snapshot, not a tracker**: re-read the code before acting on an entry.
* **Priorities** are judgement calls about impact, not measurements.
* **Status**: `Open` (a defect), `Unverified` (a claim not yet tested), `Informational` (true and
  worth knowing, but not a defect to fix).

Each entry describes only what is wrong and points at the documentation that describes how the
system is meant to behave; the fix is not specified here.

| # | Issue | Status | Priority | Details |
|---|---|---|---|---|
| 1 | Full-text index misses most of the library | Open | High | `workshop_fts` is content-sync with no triggers, and its only `'rebuild'` runs inside `if db_version < 5` (`src/database.py:752`), so rows added since the migration were never indexed: 640,471 indexed documents against 1,725,544 items (62.9% absent). Intended behaviour: [search-filter.md](search-filter.md#full-text-search-fts5). Migration history: [schema-migrations.md](schema-migrations.md). |
| 2 | Shipped example config is not valid YAML | Open | High | A stray ` example` token at `config.yaml.example:19` ends the `openai:` block early, and the file fails to parse (`expected <block end>, but found '<scalar>'` at line 22). Anyone copying it to `config.yaml` begins with a parse error. Key reference: [config-security.md](config-security.md). |
| 3 | Background paths re-queue already-translated fields | Open | High | `src/daemon.py:453` and `src/web_worker.py:52` flag non-ASCII text without checking the `_en` counterpart, while the two UI paths (`src/database.py:1971`, `src/database.py:1991`) do check — the guards disagree across the four triggers. Consequence and trigger table: [data-pipeline.md](data-pipeline.md#what-queues-a-field-for-translation). |
| 4 | Version keys are written but never compared | Open | Medium | `scrape_version` and `translate_version` are set from `steam_updated_at` (`src/database.py:1117`, `src/translator.py:164`), but nothing compares them against the current `steam_updated_at`, so an edited item is never re-scraped or re-translated. [data-model.md](data-model.md#known-gaps), [timestamps.md](timestamps.md). |
| 5 | Silent exception handlers are unaudited | Open | Medium | 24 handlers in `src/` have `pass` as their entire body (11 in `tui.py`; 3 each in `daemon.py`, `daemon_runner.py`, `database.py`; 2 in `webserver.py`; 1 each in `backup.py`, `web_runner.py`) and none records why silence is safe. No bare `except:` remains. Convention: [architecture.md](architecture.md#reliability). |
| 6 | Priority decay is asymmetric | Open | Medium | Only `api_priority` has a runtime staleness sweep (`src/daemon.py:303`, `0 → 1`). `needs_web_scrape` and `needs_image` reach priority `1` solely from one-time migrations (`src/database.py:703`, `src/database.py:731`), so the shared scale's "backlog or stale refresh" step exists for one queue of four. Scale: [data-model.md](data-model.md#queue-priorities). Measured distributions: [live-data-profile.md](live-data-profile.md). |
| 7 | Port selection races between probe and bind | Open | Low | `_start_webserver` binds a socket to test a port, closes it, then lets Waitress bind later, so another process can take the port in between (`src/tui.py:131`). Selection order: [tui.md](tui.md#_start_webserver). |
| 8 | `api_priority = 2` sits outside the documented scale | Open | Low | Written only by the image worker's failure path (`src/image_worker.py:134`) as a retry marker. It is not one of `0/1/3/5/10`, and no other queue uses it. [data-model.md](data-model.md#queue-priorities). |
| 9 | `request_delay_seconds` is accepted without deprecation | Open | Low | `src/daemon.py:83` silently falls back to the legacy key when `api_delay_seconds` is absent, so an old config keeps working with no signal that the name changed. [config-security.md](config-security.md). |
| 10 | `update_app_tracking` and `update_app_tracking_page` are never called | Open | Low | Defined at `src/database.py:2056` and `src/database.py:2067`, imported at `src/daemon.py:16`, but only `update_app_tracking_cursor` is used (`src/daemon.py:596`). Discovery flow: [data-pipeline.md](data-pipeline.md). |
| 11 | Userscript version must be kept in step by hand | Open | Low | `templates/index.html:6` declares `content="6"` and `userscripts/steam_subscribe.user.js:4` declares `@version 6`. The script compares the two at load and refuses to run when outdated (`userscripts/steam_subscribe.user.js:160`), but nothing keeps the literals equal. Check behaviour: [web-ui.md](web-ui.md). |
| 12 | `language` is never populated | Open | Low | Present in `WORKSHOP_ITEM_COLUMNS` (`src/database.py:18`) and the merge allow-list, so a response carrying it would be stored — none has, and every live row is NULL. [data-model.md](data-model.md#known-gaps). |
| 13 | `_SafeStreamHandler` startup message is unverified | Unverified | Low | `src/daemon_runner.py:110` reports the active handler class names at startup; whether that list matches the handlers actually attached was never tested. Handler behaviour: [cross-platform.md](cross-platform.md). |
| 14 | Migration chain still references the dropped `tags` column | Informational | Info | `CREATE TABLE` keeps the historical `tags` name because migrations 1→2 and 5→6 read it (`src/database.py:687`), and a later migration drops it defensively while logging the skip. Fresh databases must therefore replay the old shape. [schema-migrations.md](schema-migrations.md). |
| 15 | `status = 206` is never written | Informational | Info | The schema and one migration query allow a partial-data status (`src/database.py:704`), but no code writes it and the live database contains zero such rows. [data-model.md](data-model.md#known-gaps). |
