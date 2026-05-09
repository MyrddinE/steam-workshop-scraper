# TODO

| # | Status | Priority | Difficulty | Impact | Issue |
|---|---|---|---|---|---|---|---|---|
| 1 | pushed | medium | medium | medium | **Tags as JSON TEXT, queried via LIKE** — false positives, no structured querying, no index benefit |
| 2 | pushed | medium | easy | medium | **No composite indexes** — Added `(status, dt_attempted)`, `(consumer_appid, status)`, `(creator, dt_updated)`, `(translation_priority)`. |
| 3 | pending | medium | medium | medium | **No Full-Text Search (FTS5)** — `LIKE '%...%'` on text fields scales poorly |
| 4 | pushed | low | easy | low | **No foreign key constraints** — Documented design decision: items are discovered before users are fetched, so FK is intentionally omitted. LEFT JOIN handles missing users. |
| 5 | pushed | low | easy | low | **Mixed timestamp formats** — Documented: `dt_*` columns are daemon-managed (ISO TEXT), `time_*` columns are Steam-side (Unix INTEGER). Different concepts, not a bug. |
| 6 | pushed | low | easy | low | **Incomplete sort whitelist** — Already correct: whitelist matches the TUI sort dropdown options exactly. No change needed. |
| 7 | pushed | low | easy | low | **Broad `except OperationalError: pass`** in migrations — Tightened to only catch "duplicate column" errors, re-raises all others. |
| 8 | pushed | low | easy | low | **Dynamic SQL column interpolation** — Added `WORKSHOP_ITEM_COLUMNS` and `USER_COLUMNS` frozensets. Insert functions filter keys against whitelists before building SQL. |
| 9 | pushed | high | medium | high | **Replace HTML scraper discovery with Steam Web API** — Replaced `discover_items_by_date_html()` with `query_workshop_files()` API pagination. Filters moved from discovery URL params to post-discovery enrichment gating in `_should_enrich()`. Non-matching items get basic API metadata only. 167 tests pass. |
| 10 | pushed | high | medium | high | **Review and improve test suite coverage, structure, and quality** — 25 test files (~2991 lines) exist but `conftest.py` is minimal (2 fixtures), integration test is thin (31 lines), no full daemon pipeline E2E test, `test_tui.py` is a monolithic 592-line file, and mocking patterns may be brittle. Focus areas: add missing fixtures, parametrize repetitive tests, split oversized files, add integration paths, and ensure tests follow Arrange-Act-Assert conventions. |
| 11 | pushed | medium | medium | medium | **Refactor long functions into smaller modules for clarity** — mean function length reduced from 26 to 21 across 124 functions. Key improvements: `process_batch` 234→131, `search_items` 157→82, `discover_items_by_date_html` 87→25, `update_content` 108→84, `on_button_pressed` 81→64, `get_db_stats` 105→50. Extracted 20 new helper functions across all source files. |

---

## Column Reference

| Column | Values |
|---|---|
| **Impact** | `low`, `medium`, `high`, `critical` |
| **Difficulty** | `easy`, `medium`, `hard`, `sisyphean` |
| **Priority** | `low`, `medium`, `high`, `critical` |
| **Status** | `pending` → `working` → `testing` → `pushed` → `validated` |
