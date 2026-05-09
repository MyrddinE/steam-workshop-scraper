# TODO

| # | Issue | Impact | Difficulty | Priority | Status |
|---|---|---|---|---|---|---|
| 1 | **Tags as JSON TEXT, queried via LIKE** — false positives, no structured querying, no index benefit | medium | medium | medium | pushed |
| 2 | **No composite indexes** — `(consumer_appid, status)`, `(creator, dt_updated)` etc. missing | medium | easy | medium | pending |
| 3 | **No Full-Text Search (FTS5)** — `LIKE '%...%'` on text fields scales poorly | medium | medium | medium | pending |
| 4 | **No foreign key constraints** — `creator` → `users.steamid` unenforced | low | easy | low | pending |
| 5 | **Mixed timestamp formats** — ISO 8601 TEXT in some columns, Unix INTEGER in others | low | easy | low | pending |
| 6 | **Incomplete sort whitelist** — `search_items()` missing `status`, `dt_updated`, `creator`, etc. | low | easy | low | pending |
| 7 | **Broad `except OperationalError: pass`** in migrations — masks real errors | low | easy | low | pending |
| 8 | **Dynamic SQL column interpolation** — no whitelist in insert/update builders | low | easy | low | pending |
| 9 | **Replace HTML scraper discovery with Steam Web API** — `discover_items_by_date_html()` is unreliable for anonymous users. Use `query_files_by_date()` API instead. Move filters (text, required_tags, excluded_tags) from discovery-time URL params to post-discovery gating, so they control which items receive deep enrichment (extended description + translation). All discovered items still get basic API metadata stored. | high | medium | high | pending |
| 10 | **Review and improve test suite coverage, structure, and quality** — 25 test files (~2991 lines) exist but `conftest.py` is minimal (2 fixtures), integration test is thin (31 lines), no full daemon pipeline E2E test, `test_tui.py` is a monolithic 592-line file, and mocking patterns may be brittle. Focus areas: add missing fixtures, parametrize repetitive tests, split oversized files, add integration paths, and ensure tests follow Arrange-Act-Assert conventions. | high | medium | high | pushed |
| 11 | **Refactor long functions into smaller modules for clarity** — mean function length reduced from 26 to 21 across 124 functions. Key improvements: `process_batch` 234→131, `search_items` 157→82, `discover_items_by_date_html` 87→25, `update_content` 108→84, `on_button_pressed` 81→64, `get_db_stats` 105→50. Extracted 20 new helper functions across all source files. | medium | medium | medium | pushed |

---

## Column Reference

| Column | Values |
|---|---|
| **Impact** | `low`, `medium`, `high`, `critical` |
| **Difficulty** | `easy`, `medium`, `hard`, `sisyphean` |
| **Priority** | `low`, `medium`, `high`, `critical` |
| **Status** | `pending` → `working` → `testing` → `pushed` → `validated` |
