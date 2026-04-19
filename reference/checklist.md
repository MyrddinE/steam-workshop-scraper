# Steam Workshop Scraper - Historical API Refactor Checklist

## Phase 1: API Exploration & Strategy Validation
- [x] **Test API Sorting:** Tested `IPublishedFileService/QueryFiles`. It does not support sorting by oldest first (RankedByPublicationDate is newest-first).
- [ ] **Implement Binary Search (Fallback):** Implement a binary search algorithm (using Unix timestamps) to find the earliest date range containing 0 workshop items for the target AppID, establishing the exact starting point instead of hardcoding 10 years ago.
- [x] **Determine Pagination Limits:** Tested endpoint. Max page is 500 (or roughly 50,000 items).
- [x] **Design Dynamic Range Algorithm:** Created logic to target returning roughly 10 pages. Exceeding 90% of max limit (450 pages) aborts and narrows the window.

## Phase 2: Database Schema Updates
- [x] **TDD Tests:** Wrote tests verifying the new schema and accessor functions.
- [x] **Create `app_tracking` Table:** Added `app_tracking` table to store game-specific details.
- [x] **Add Tracking Columns:** Added `appid` and `last_historical_date_scanned`.
- [x] **Implement DB Accessors:** Added `get_app_tracking` and `update_app_tracking`.

## Phase 3: Steam API Wrapper Updates
- [x] **TDD Tests:** Wrote tests for wrapper.
- [x] **Add `query_files_by_date`:** Added `query_files_by_date` in `src/steam_api.py`.

## Phase 4: Daemon Discovery Refactoring
- [x] **TDD Tests:** Wrote tests mocking the new discovery flow.
- [x] **Refactor `seed_database`:** Overhauled discovery to use "historical forward".
- [x] **Implement State Resumption:** Read `last_historical_date_scanned`.
- [x] **Implement Dynamic Query Loop:** Queries dynamic window, evaluates pages, dynamically adjusts width, updates DB tracking safely.
- [x] **Implement Daily Refresh:** Uses the historical loop to naturally wait 24h.

## Phase 5: Re-querying & Updating Old Content
- [x] **TDD Tests:** Wrote priority test.
- [x] **Update Priority Logic:** Modified `get_next_items_to_scrape` to prioritize NULL, then != 200, then 200 (stalest first).
