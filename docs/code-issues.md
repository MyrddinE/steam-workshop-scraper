# Code Issues: Verified Status

The issue list this file replaces was not maintained as problems were fixed, so it was unreliable
as a backlog. Each claim below has been re-checked against the source rather than taken on trust.

- **Method**: every claim read against the source at HEAD.
- **Audited at**: `90fdcb7` "Manual changes to backoff logic" (2026-09-12).
- **Previous version last committed**: `9b7cbf8` (2026-05-16) — roughly four months and many
  commits stale.
- **Scope**: all 12 claims spot-checked, not a full re-audit.

## Verdicts

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Detail poll never fires for translations | **Fixed** | `flag_field_for_translation` now writes the column: `database.py` `UPDATE {table} SET translation_priority = MAX(translation_priority, ?)`. The web UI reads it and gets real values. |
| 2 | `_evaluate_translation_needs` is dead code | **Obsolete** | The function no longer exists anywhere in `src/` or `tests/`. |
| 3 | `_classify_translation_status` uses vestigial column | **Fixed** | The column is now populated (see #1), so the classifier in `database.py` runs against live data. |
| 4 | Race condition in web server port binding | **Still present** | `tui.py`: the probe socket is bound, closed, then Waitress binds later. TOCTOU unchanged. |
| 5 | `compute_wilson_cutoffs` doesn't handle Full Text | **Fixed** | `database.py`: `if FIELD_NAME_MAP.get(...) == "full_text": continue  # FTS5 MATCH can't be applied...` |
| 6 | Inconsistent priority semantics | **Still present** | `needs_image` decays (`image_worker.py`, `max(0, … - 1)`); `needs_web_scrape` only ever uses `MAX` (`database.py`), with no decrement path. |
| 7 | Legacy config key confusion | **Still present** | `daemon.py` reads `… or daemon_config.get("request_delay_seconds", 1.5)` with no deprecation warning. |
| 8 | Silent catch blocks | **Partially present** | An AST scan of `src/` found **24** handlers whose entire body is `pass` and **0** bare `except:`. Real, but no longer what the old doc described. |
| 9 | Button handlers were lost in an edit | **Fixed** | `index.html` binds `btn-search`, `btn-and`, `btn-or`, `btn-save-filter`. |
| 10 | Manual version synchronization | **Process risk remains** | Currently consistent — `index.html` `content="6"`, `steam_subscribe.user.js` `@version 6` — but nothing enforces it. |
| 11 | `_SafeStreamHandler` startup log is misleading | **Unverified (cosmetic)** | The handler exists (`daemon_runner.py`); log-message accuracy not assessed. |
| 12 | JSON column dropped but leftover migration references tags | **Informational** | `database.py` drops the legacy column defensively and logs the skip. |

## Issues found since this audit

Not part of the original list above; these surfaced while verifying it.

### 13. Background paths re-queue already-translated fields — present

The daemon (`daemon.py`, `_flag_translations`) and the web scraper (`web_worker.py`) flag non-ASCII
fields for translation without checking whether the `_en` counterpart is already populated. The two
user-view paths (`bump_translation_for_list` / `_for_detail`) *do* check, so the guards are
inconsistent across the four triggers.

A successful translation deletes its `translation_queue` row, so the "already queued" check inside
`flag_field_for_translation` gives no protection afterwards. Combined with the staleness sweep
(`item_staleness_days`, default 30), an enriched item with a non-ASCII title is re-flagged and then
re-translated on roughly a monthly cycle even when nothing about it has changed — repeated identical
work and repeated OpenAI spend. The live snapshot already holds 704,525 non-ASCII titles and 142,748
translations, so the recurring batch is large.

Cheapest fix: add the `not translated` guard to the two background paths, matching the UI paths.

### 14. Changed source text never triggers re-translation — gap

`scrape_version` and `translate_version` record `steam_updated_at` at scrape and translation time,
but nothing compares them against the current `steam_updated_at`. So when an author edits an item,
its existing translation is never refreshed. Fixing #13 on its own would make that permanent, which
is why the two belong together.

### Related, documented elsewhere

- **Full-text search is missing most of the library.** `workshop_fts` is content-sync with no
  triggers, and the only `'rebuild'` sits inside the `db_version < 5` migration, so rows inserted
  afterwards were never indexed: `workshop_fts_docsize` holds 640,471 documents against 1,725,544
  items — **62.9% absent**. Detail in `search-filter.md` and `schema-migrations.md`.
- `api_priority = 2` is written by the web and image worker failure paths, outside the documented
  `0/1/3/5/10` scale (`data-model.md`).
- `update_app_tracking` and `update_app_tracking_page` are imported by `daemon.py` but never called.

Like the table above, this section is a snapshot. Re-read the code before acting on it.

## Bottom Line

Five of the twelve claims were already resolved (#1, #2, #3, #5, #9) while still presented as live
problems. Three are confirmed live today: **#4** (port TOCTOU), **#6** (asymmetric priority decay),
and **#7** (undeprecated legacy config key). #8 is real but mis-described. The rest are
process or cosmetic concerns.

This list is a snapshot, not a maintained tracker. Re-read the code before acting on any entry.
