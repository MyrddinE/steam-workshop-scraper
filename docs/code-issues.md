# Codebase Issues

Problems identified while documenting the codebase. Some are bugs, some are design issues, some are maintenance concerns. Grouped by severity.

---

## Bugs

### 1. Detail poll never fires for translations

`_detailNeedsPoll` (web UI, `index.html`) checks `translation_priority > 0`. But `translation_priority` on `workshop_items` is never set to a non-zero value — the daemon no longer calls `_evaluate_translation_needs`, and `bump_translation_for_list/detail` only inserts into `translation_queue` without touching the column. The only non-zero values come from `_build_user_record` (users, not items). Result: the detail poll never starts for translation-completion detection. The poll's image-check path was recently removed when the detail-pane image was deleted, which makes the poll a complete no-op for items.

**Impact**: Translations arrive but the detail pane never refreshes automatically. Users must manually re-select an item to see translated text.

### 2. `_evaluate_translation_needs` is dead code

Defined in `daemon.py` but never called. It was designed to set `translation_priority = 1` on items with non-ASCII text and flag them for the translator. Without it, items enter the translation pipeline only through the `flag_field_for_translation` calls on enriched items, which populate `translation_queue` but don't set the `translation_priority` column.

**Impact**: The `translation_priority` column on `workshop_items` is vestigial. Any code that reads it (e.g., `_detailNeedsPoll`, `_listNeedsPoll` for translations, `_classify_translation_status`) gets misleading values.

### 3. `_classify_translation_status` uses vestigial column

This function in `database.py` checks `item["translation_priority"]` to classify items as "Queued." Since the column is always 0 for items, no items are ever classified as "Queued" in the stats screen's translation breakdown. The classification logic is correct in intent but fed bad data.

**Impact**: The stats screen's translation-status breakdown is incomplete.

---

## Design Issues

### 4. Race condition in web server port binding

`_start_webserver` (TUI, `tui.py`) binds to a test socket to check port availability, closes it, then passes the port to Waitress. Between `s.close()` and `serve(...)`, another process could grab the port (TOCTOU). Low probability for random high ports but a design flaw regardless.

**Suggested fix**: Pass port 0 to Waitress and read the actual bound port from the server object.

### 5. `compute_wilson_cutoffs` doesn't handle Full Text field

The filter loop in `compute_wilson_cutoffs` routes `tags` through `_build_json_tag_clause` but uses `_build_filter_clause` for all other fields — including `full_text`. A Full Text filter in the cutoff query would fail (the clause references FTS5 MATCH against `workshop_fts`, which is not logically compatible with NTILE computation). The result is silently caught by `except Exception: return {}`.

**Impact**: If a user searches with a Full Text filter and clicks the stats screen, Wilson score cutoffs return empty, and all scores show as gray.

### 6. Inconsistent priority semantics

- `needs_image`: upgrades with `MAX()`, downgrades via direct UPDATE (failure path decrements by 1)
- `needs_web_scrape`: only uses `MAX()`, no failure/decrement path exists

**Impact**: Failed image downloads decay in priority; failed web scrapes don't.

### 7. Legacy config key confusion

`daemon.request_delay_seconds` is read as a fallback for `daemon.api_delay_seconds`. This is a legacy key that may persist in user configs after the rename. No migration or deprecation warning exists.

---

## Maintenance Issues

### 8. Silent catch blocks

Many `except Exception: pass` or bare `except:` blocks throughout the codebase (detailed list in a prior session). While some are intentional (poll loops, optional enhancements like puremagic), others hide real failures (config parsing, state restoration, search cutoff computation). The lack of logging makes debugging difficult.

### 9. Button handlers were lost in an edit

The `$J` handler was removed: `btn-search`, `btn-and`, `btn-or`, and `btn-save-filter` button click handlers had no JavaScript listeners defined. These were restored but the loss pattern suggests fragile event binding in the template.

### 10. Manual version synchronization

The `userscript-version` meta tag in `index.html` and the `@version` directive in `userscripts/steam_subscribe.user.js` must be manually kept in sync. If one is updated without the other, the userscript version-check will block or allow incorrectly.

---

## Cosmetic

### 11. `_SafeStreamHandler` startup log is misleading

The startup message "Daemon starting — handlers: ..." lists handler class names. If `_SafeStreamHandler` never appears (e.g., due to bytecode caching), the user has no indication that Unicode log entries may crash the daemon.

### 12. JSON column dropped but leftover migration references tags

Earlier migrations (v1→v2, v2→v3) reference `workshop_items.tags`. On a fresh install, these migrations stay idempotent because the column was included in the CREATE TABLE for fresh databases and dropped later in v6.
