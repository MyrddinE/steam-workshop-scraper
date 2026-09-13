# Code Issues

Known defects in the current source. Each entry was re-checked against the code rather than carried
forward from an earlier list, and resolved entries are deleted rather than marked as fixed.

* **Checked against source**: `bc94914`.
* **Snapshot, not a tracker**: re-read the code before acting on an entry.
* **Priorities** are judgement calls about impact, not measurements.
* **Status**: `Open` (a defect), `Unverified` (a claim not yet tested), `Informational` (true and
  worth knowing, but not a defect to fix).

Each entry describes only what is wrong and points at the documentation that describes how the
system is meant to behave; the fix is not specified here. Figures marked *measured live* come from
the production database on 2026-09-12.

| # | Issue | Status | Priority | Details |
|---|---|---|---|---|
| 1 | A malformed config crashes with an unhandled traceback | Open | Medium | `load_config` lets YAML parse errors propagate (`src/config.py:14`), and every entry point catches only `FileNotFoundError` (`src/tui.py:1268`, `src/tui.py:1851`, `src/daemon_runner.py:77`; `src/web_runner.py:17` has no handler at all). A YAML mistake therefore ends in a raw `ScannerError` traceback naming a line and column but not what to do about it. The common trigger is an unescaped Windows path: `"D:\Temp\dsh"` is a parse error, and `"D:\temp"` is worse — it parses without complaint to `D:<TAB>emp`. Intended: [config-security.md](config-security.md). |
| 2 | Failure captures truncate before the region that would explain them | Open | Medium | `MAX_BODY_BYTES = 64 * 1024` keeps the *first* 64 KB (`src/capture.py:55`). On the dominant live failure — 101 of 124 misses — the page is 303 KB and the retained head is script and stylesheet tags: no item markup, `class_count` 2, ending mid-tag. The artefact cannot answer the question it exists for. `shape.class_digest` hashes the class names *in that truncated body*, so the signature describes the head bundle rather than the page: two different pages sharing a head collapse into one variant, and the re-capture-when-the-digest-changes rule inherits the blind spot. [failure-capture.md](failure-capture.md). |
| 3 | The description scraper authenticates as nobody | Open | Medium | `scrape_extended_details` issues its request with no cookies at all (`src/web_scraper.py:102`), so it sees only what Steam serves anonymously. `_build_workshop_cookies` — used solely by the browse path — returns `sessionid` and the mature-content preference but not `steamLoginSecure` (`src/web_scraper.py:32`). That cookie is HttpOnly, so the userscript's `document.cookie` read (`userscripts/steam_subscribe.user.js:38`) can never obtain it, which is why the log reports "login_secure: missing"; `config.yaml` already holds a value, read only by the subscribe path (`src/webserver.py:378`). [config-security.md](config-security.md) describes both cookies as authenticating "API requests", which is true of neither scraper. [config-security.md](config-security.md), [data-pipeline.md](data-pipeline.md#web-scraping-phase). |
| 4 | An error page counts as a content miss, so throttling never slows the scraper | Open | Medium | `_handle_selector_miss` captures the response and steps the item's priority down but leaves `web_successes` and `web_failures` untouched (`src/web_worker.py:25`), so `web_delay` cannot rise. The documented reasoning is that the request succeeded so slowing down would not help (`data-pipeline.md`), which holds for a genuine layout change but not for a page that is really a throttle or an error. Steam serves its generic error page with **HTTP 200** — *observed live*, `title_tag` "Steam Community :: Error", 27 KB — and this code cannot tell that from a selector that stopped matching: it decays the item and continues at the same pace. The capture already records `title_tag`, so the signal exists and nothing acts on it. [data-pipeline.md](data-pipeline.md#web-scraping-phase). |
| 5 | Port selection races between probe and bind | Open | Low | `_start_webserver` binds a socket to test a port and closes it again (`src/tui.py:1809`, closed at 1812 and 1818) before Waitress binds later (`src/tui.py:1835`), so another process can take the port in between. Selection order: [tui.md](tui.md#_start_webserver). |
| 6 | `api_priority = 2` sits outside the documented scale | Open | Low | Written by the image worker's failure path (`src/image_worker.py:134`) and the web worker's request-failure path (`src/web_worker.py:124`) as a retry marker. It is not one of `0/1/3/5/10`, and no other queue uses it. [data-model.md](data-model.md#queue-priorities). |
| 7 | `request_delay_seconds` is accepted without deprecation | Open | Low | `src/daemon.py:95` silently falls back to the legacy key when `api_delay_seconds` is absent, so an old config keeps working with no signal that the name changed. The shipped example no longer advertises it. [config-security.md](config-security.md). |
| 8 | `update_app_tracking` and `update_app_tracking_page` are never called | Open | Low | Defined at `src/database.py:2248` and `src/database.py:2259`, imported at `src/daemon.py:17`, but only `update_app_tracking_cursor` is used (`src/daemon.py:696`). Discovery flow: [data-pipeline.md](data-pipeline.md). |
| 9 | Userscript version must be kept in step by hand | Open | Low | `templates/index.html:6` declares `content="6"` and `userscripts/steam_subscribe.user.js:4` declares `@version 6`. The script compares the two at load and refuses to run when outdated (`userscripts/steam_subscribe.user.js:160`), but nothing keeps the literals equal. Check behaviour: [web-ui.md](web-ui.md). |
| 10 | `language` is never populated | Open | Low | Present in `WORKSHOP_ITEM_COLUMNS` (`src/database.py:18`) and the merge allow-list, so a response carrying it would be stored — none has, and every live row is NULL. [data-model.md](data-model.md#known-gaps). |
| 11 | Dead items keep their queue priority | Open | Low | An item marked dead retains `api_priority > 0`. The 404 path clears it (`src/daemon.py:453`), so this is legacy data rather than an ongoing leak, and the discovery guard now counts only fetchable work, so they no longer suppress discovery. They still inflate any count of `api_priority > 0`: *measured live*, 10,476 dead rows against 1 fetchable item. [data-model.md](data-model.md#queue-columns). |
| 12 | `scrape_version` is written by two workers and read by nobody | Open | Low | The web scraper (`src/web_worker.py:87`) and the image worker (`src/image_worker.py:111`) both write the item's `steam_updated_at` into this one column, so it cannot distinguish which stage wrote it last, and no code reads it. It is not a completion timestamp either — it holds Steam's revision, not our clock. [timestamps.md](timestamps.md). |
| 13 | Migration chain still references the dropped `tags` column | Informational | Info | `CREATE TABLE` keeps the historical `tags` name because migrations 1→2 and 5→6 read it (`src/database.py:690`), and a later migration drops it defensively while logging the skip. Fresh databases must therefore replay the old shape. [schema-migrations.md](schema-migrations.md). |
| 14 | `status = 206` is never written | Informational | Info | The schema and one migration query allow a partial-data status (`src/database.py:704`), but no code writes it and the live database contains zero such rows. [data-model.md](data-model.md#known-gaps). |

## Recently closed

Removed from the table above rather than marked resolved. Each is now documented as current
behaviour, or covered by a test:

| Was | Now |
|---|---|
| The suite needed stray `test.db` and `workshop.db` files to pass | Fixtures use an initialised temporary database, so a clean checkout is green: 15 failures to 0. The suite still creates two empty files in the working directory, which nothing reads |
| The translation thread retried a failed batch with no backoff | It backs off now — 2 s→5 min for service failures, 60 s→1 h for account-level ones — [threading.md](threading.md#translation-thread-translatorthread) |
| Full-text index missed 62.9% of the library | Migration 14→15 rebuilds it and installs sync triggers — [search-filter.md](search-filter.md), [schema-migrations.md](schema-migrations.md) |
| The shipped example config was not valid YAML | Fixed, with a regression test that it parses |
| Background paths re-queued already-translated fields | `translation_is_current` gates every trigger — [data-pipeline.md](data-pipeline.md#what-queues-a-field-for-translation) |
| Version keys were written but never compared | `translate_version` now drives re-translation — [timestamps.md](timestamps.md) |
| Silent `except ...: pass` handlers were unaudited | All 24 classified; each states why silence is safe, or logs and captures |
| The `_SafeStreamHandler` startup message was unverified | Verified accurate: the handler list is complete before `basicConfig(force=True)`, and the message reads back `root.handlers` (`src/daemon_runner.py:96`) |
| Priority decay was asymmetric across queues | Superseded: the intended model is change-driven, not per-queue sweeps |
| Discovery was skipped while the queue held work it could not fetch | The guard counts fetchable work, and migration 15→16 requeued the rows the old policy stranded — [data-pipeline.md](data-pipeline.md#discovery-phase) |
| Opening a detail pane re-queued the same item every three seconds | Detail priority is applied once, on open; the poll uses a read-only route — [web-ui.md](web-ui.md), [tui.md](tui.md) |
| Transient API failures were dequeued instead of retried | Permanent failures dequeue; temporary ones step down and stay queued — [architecture.md](architecture.md#reliability), [data-pipeline.md](data-pipeline.md#failure-classification-daemon) |
| Image work was re-queued on every fetch, changed or not | Both stages follow the API refresh as the change detector — [data-pipeline.md](data-pipeline.md#change-detection-across-stages) |
