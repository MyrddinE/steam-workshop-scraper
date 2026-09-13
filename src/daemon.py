import time
import math
import signal
import json
import logging
import os
from datetime import datetime, timezone
from src.database import (
    get_next_items_to_scrape, 
    insert_or_update_item, 
    count_unscraped_items, 
    count_fetchable_items, 
    insert_or_update_user, 
    get_user, 
    flag_for_translation,
    get_app_tracking,
    update_app_tracking,
    update_app_tracking_page,
    update_app_tracking_cursor,
    save_app_filter,
    get_connection,
    get_item_details,
    normalize_tags,
    _evaluate_filters,
    WORKSHOP_ITEM_COLUMNS,
)
from src.steam_api import get_workshop_details_api, query_workshop_items, get_player_summaries, query_workshop_files, set_api_delay, query_workshop_page_updated
from src.translator import TranslatorThread, is_ascii
from src.config import save_config
from src.database import flag_for_web_scrape, flag_field_for_translation, flag_for_image, translation_is_current
from src.web_worker import WebScraperThread
from src.image_worker import ImageScraperThread
from src.backup import BackupThread
from src import capture

# API statuses the fetch path has an explicit branch for. Anything else is
# captured as evidence and then treated as temporary by _settle_api_failure; it
# is never silently persisted as a success.
HANDLED_API_STATUSES = frozenset({200, 404, 500})

# The only API outcome that cannot succeed on retry. Everything else -- 500,
# transport exceptions (which get_workshop_details_api reports as 500), and any
# status without its own branch -- is retried at one priority level lower.
PERMANENT_API_STATUSES = frozenset({404})


# --- API merge allow-list ----------------------------------------------------
# Item columns that are owned by the queue-flagging helpers rather than by the
# API merge. They must NOT survive a merge: flag_for_web_scrape / flag_for_image
# set them explicitly between the merge and the insert, so carrying a stale value
# through the merge would clobber the flag that was just set.
MERGE_EXCLUDED_KEYS = frozenset({
    "is_queued_for_subscription",
    "needs_web_scrape",
    "image_extension",
    "needs_image",
})

# Keys retained from an API merge into the item record:
#   * every real column (WORKSHOP_ITEM_COLUMNS), minus the queue-owned ones above
#   * "tags", which is no longer a column -- it lives in the workshop_tags
#     junction table -- but is consumed by insert_or_update_item's tag sync and
#     so must survive the merge
# Derived rather than hand-listed: adding a column now updates this automatically.
MERGE_ITEM_KEYS = (WORKSHOP_ITEM_COLUMNS - MERGE_EXCLUDED_KEYS) | {"tags"}

# Keys the merge knows about and drops silently (no "unknown column" log line).
MERGE_IGNORED_KEYS = MERGE_EXCLUDED_KEYS | {"result"}


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    """Wilson score confidence interval lower bound for a Bernoulli parameter.
    Returns 0.0–1.0; penalizes small sample sizes."""
    if trials == 0:
        return 0.0
    p = min(float(successes) / trials, 1.0)
    z2 = z * z
    denom = 1 + z2 / trials
    numer = p + z2 / (2 * trials) - z * math.sqrt(max(0.0, p * (1 - p) / trials) + z2 / (4 * trials * trials))
    return max(0.0, min(1.0, numer / denom))


class Daemon:
    def __init__(self, config: dict, config_path: str = "config.yaml"):
        self.config = config
        self.config_path = config_path
        self.running = True
        self.last_filters = {}
        
        # Implement default fallbacks
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.api_key = config.get("api", {}).get("key", "")
        self.batch_size = config.get("daemon", {}).get("batch_size", 10)
        daemon_config = config.get("daemon", {})
        self.api_delay = daemon_config.get("api_delay_seconds") or daemon_config.get("request_delay_seconds", 1.5)
        self.item_staleness_days = int(daemon_config.get("item_staleness_days") or 30)
        self.user_staleness_days = int(daemon_config.get("user_staleness_days") or 90)
        set_api_delay(self.api_delay)
        logging.info(f"API delay={self.api_delay}s, Staleness: item={self.item_staleness_days}d, user={self.user_staleness_days}d")

        # Write default config keys if absent
        changed = False
        if "daemon" not in self.config:
            self.config["daemon"] = {}
        for key, val in [("item_staleness_days", self.item_staleness_days),
                          ("user_staleness_days", self.user_staleness_days)]:
            if key not in self.config["daemon"]:
                self.config["daemon"][key] = val
                changed = True
        if changed:
            save_config(self.config_path, self.config)
        self.pause_lock_file = ".pauselock"
        
        # Translator thread
        self.translator = TranslatorThread(config)

        # Optional database backup into a pull-outbox. The feature defaults to
        # OFF: it is only enabled when both `outbox_dir` (or `backup_dir`) and a
        # positive `backup_interval_seconds` are configured, so turning it on for
        # the live instance is a deliberate switch.
        self.outbox_dir = daemon_config.get("outbox_dir") or daemon_config.get("backup_dir")
        self.backup_interval_seconds = float(daemon_config.get("backup_interval_seconds") or 0)
        self._backup_worker = None
        if self.outbox_dir and self.backup_interval_seconds > 0:
            self._backup_worker = BackupThread(
                self.db_path, self.outbox_dir, self.backup_interval_seconds)
            logging.info(
                "Database backup enabled: outbox=%s interval=%ss",
                self.outbox_dir, self.backup_interval_seconds)

        # Failure capture rides on the same outbox but needs no interval: it is a
        # no-op unless an outbox directory is configured, like the backup above.
        capture.configure(self.outbox_dir)
        
        # State variables for dynamic delay adjustment
        self.api_successes = 0
        self.api_failures = 0
        self.api_had_streak = False

        # Page-based discovery (sort by update time) — runs once a day when eligible
        self._last_page_discovery = 0
        self._cursor_exhausted = False
        self._saw_pid_file = False  # set True once PID file is seen; prevents false trigger in tests

        # Enforce required target_appids
        self.target_appids = config.get("daemon", {}).get("target_appids")
        if not self.target_appids or not isinstance(self.target_appids, list):
            raise ValueError("Configuration error: 'daemon.target_appids' must be provided as a list.")
        
        # Pre-load initial filter state to avoid false positives on startup
        self._load_initial_filter_state()
        
        # Setup graceful shutdown
        signal.signal(signal.SIGINT, self.handle_shutdown)
        signal.signal(signal.SIGTERM, self.handle_shutdown)

    def _save_config_value(self, key: str, value):
        """Saves a daemon config key=value to the config file. Usable as callback from threads."""
        if "daemon" not in self.config:
            self.config["daemon"] = {}
        self.config["daemon"][key] = value
        save_config(self.config_path, self.config)

    def _build_user_record(self, steamid: int, personaname: str) -> dict:
        """Builds a user record dict for upsert, flagging for translation if non-ASCII."""
        record = {
            "steamid": steamid,
            "personaname": personaname,
            "api_fetched_at": int(time.time())
        }
        if not is_ascii(personaname):
            record["translation_priority"] = 1
        return record

    def _merge_and_clean_api_data(self, api_data: dict, existing_data: dict, item_id: int, now_ts: int) -> dict:
        """Merges API response into existing data, remaps column names, and filters to allowed keys."""
        merged = existing_data.copy()
        api_data.pop("publishedfileid", None)
        api_data.pop("status", None)
        merged.update(api_data)

        if "creator_app_id" in merged:
            merged["creator_appid"] = merged.pop("creator_app_id")
        if "consumer_app_id" in merged:
            merged["consumer_appid"] = merged.pop("consumer_app_id")
        if "description" in merged:
            merged["short_description"] = merged.pop("description")
        # The Steam API still calls these fields time_created / time_updated;
        # the database columns are now named steam_created_at / steam_updated_at.
        if "time_created" in merged:
            merged["steam_created_at"] = merged.pop("time_created")
        if "time_updated" in merged:
            merged["steam_updated_at"] = merged.pop("time_updated")

        clean = {}
        for k, v in merged.items():
            if k in MERGE_ITEM_KEYS:
                clean[k] = v
            elif k not in MERGE_IGNORED_KEYS:
                val_preview = str(v)[:20] + "..." if len(str(v)) > 20 else str(v)
                logger = logging.info if v is not None and str(v).strip() != "" else logging.debug
                logger(f"Discarding unknown API column: '{k}' with value '{val_preview}' for item {item_id}")

        # Success path only: this helper is called after a usable API payload has
        # arrived, so api_fetched_at means "last SUCCESSFUL content pull".
        clean["api_fetched_at"] = now_ts
        clean["api_priority"] = 0  # mark as fetched, no longer queued
        if "tags" in clean:
            clean["tags"] = normalize_tags(clean["tags"])
        return clean

    def _should_enrich(self, appid: int, item: dict) -> bool:
        """Returns True if the item matches enrichment filters for its AppID.
        Evaluates the stored filter list using the same logic as the TUI search builder."""
        if appid is None or appid not in self.target_appids:
            return True
        app_tracking = get_app_tracking(self.db_path, appid)
        if app_tracking is None:
            return True
        enrichment = app_tracking.get("enrichment_filters") or "[]"
        try:
            filters = json.loads(enrichment)
        except (json.JSONDecodeError, TypeError):
            return True
        if not filters:
            # Fallback to legacy columns for backward compat
            filter_text = (app_tracking.get("filter_text") or "").strip()
            required_tags = json.loads(app_tracking.get("required_tags") or "[]")
            excluded_tags = json.loads(app_tracking.get("excluded_tags") or "[]")
            if not filter_text and not required_tags and not excluded_tags:
                return True
            # Convert legacy columns to filter format for evaluation
            filters = []
            if filter_text:
                filters.append({"field": "Title", "op": "contains", "value": filter_text})
            for tag in required_tags:
                filters.append({"field": "Tags", "op": "contains", "value": tag})
            for tag in excluded_tags:
                filters.append({"field": "Tags", "op": "does_not_contain", "value": tag})
        return _evaluate_filters(item, filters)

    def _load_initial_filter_state(self):
        """Pre-loads filter state and page tracking from the DB on startup."""
        for appid in self.target_appids:
            app_tracking = get_app_tracking(self.db_path, appid)
            if app_tracking:
                self.last_filters[appid] = {
                    "last_page": app_tracking.get("last_page_scanned", 0) or 0,
                    "last_cursor": app_tracking.get("last_cursor") or ""
                }

    def handle_shutdown(self, signum, frame):
        """Signals the loop to stop and finishes the current batch safely."""
        logging.warning(f"Received signal {signum}, initiating shutdown...")
        self.running = False
        self.translator.running = False
        if hasattr(self, '_web_worker'):
            self._web_worker.running = False
        if hasattr(self, '_image_worker'):
            self._image_worker.running = False
        if self._backup_worker is not None:
            self._backup_worker.running = False

    def expand_user_discovery(self):
        """
        Scans workshop_items for creators who are not in the users table 
        and fetches their summaries.
        """
        conn = get_connection(self.db_path)
        # Find creators in workshop_items that aren't in users table
        sql = """
            SELECT DISTINCT creator FROM workshop_items 
            WHERE creator IS NOT NULL 
            AND creator NOT IN (SELECT steamid FROM users)
            LIMIT 100
        """
        cursor = conn.execute(sql)
        missing_ids = [int(row["creator"]) for row in cursor.fetchall() if row["creator"]]
        conn.close()

        if missing_ids:
            logging.info(f"Proactively fetching {len(missing_ids)} missing user profiles...")
            try:
                summaries = get_player_summaries(missing_ids, self.api_key)
                for sid in missing_ids:
                    if sid in summaries:
                        pdata = summaries[sid]
                        user_record = self._build_user_record(sid, pdata.get("personaname"))
                        insert_or_update_user(self.db_path, user_record)
                        logging.info(f"Updated profile for user {sid}: '{user_record['personaname']}'")
                    else:
                        insert_or_update_user(self.db_path, self._build_user_record(sid, f"SteamID:{sid}"))
            except Exception as e:
                logging.error(f"Error expanding user discovery: {e}")

    def process_batch(self):
        """Process one batch: housekeeping, acquire work, then process each item."""
        self._promote_stale_items()

        items_to_scrape = self._acquire_batch()
        if items_to_scrape is None:
            return  # database error, already logged
        if not items_to_scrape:
            self._wait_for_work()
            return

        for existing_data in items_to_scrape:
            if not self.running or self._pid_file_removed():
                break
            self._process_item(existing_data)

    def _promote_stale_items(self) -> None:
        """Periodic sweep: promote stale items from API priority 0 to 1.

        Failures are swallowed deliberately (housekeeping must never stop the
        fetch loop); see notes/findings.md for the capture-on-failure follow-up.
        """
        try:
            threshold = int(time.time()) - self.item_staleness_days * 86400
            conn = get_connection(self.db_path)
            conn.execute(
                "UPDATE workshop_items SET api_priority = 1 "
                "WHERE api_priority = 0 AND status = 200 AND api_fetched_at < ? "
                "AND (status IS NULL OR status != -1)",
                (threshold,)
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            pass
            logging.warning("Stale-item promotion failed; housekeeping skipped this sweep: %s", exc)

    def _acquire_batch(self):
        """Return the next batch of items, refilling the queue when it is empty.

        Returns None when this iteration should be abandoned: a database error, or
        the daemon stopping during discovery.
        """
        items_to_scrape = self._fetch_batch()
        if items_to_scrape is None or items_to_scrape:
            return items_to_scrape

        logging.debug("No items to scrape. Expanding discovery...")
        if self._page_discovery_eligible():
            self._run_page_discovery()
            # Fall through to cursor mode if page mode didn't fill the queue
            if not self.running:
                return None
        self.seed_database()
        return self._fetch_batch("Database error after seeding")

    def _fetch_batch(self, error_message: str = "Database error in process_batch"):
        """Read one batch from the database. Returns None on database error."""
        try:
            return get_next_items_to_scrape(self.db_path, limit=self.batch_size,
                                            staleness_days=self.item_staleness_days)
        except Exception as e:
            logging.error(f"{error_message}: {e}")
            time.sleep(5)
            return None

    def _wait_for_work(self) -> None:
        """Idle poll: wait up to ten minutes for work to appear."""
        for _ in range(600):
            if not self.running:
                return
            if self._pid_file_removed():
                return
            time.sleep(1)

    def _process_item(self, existing_data: dict) -> None:
        """Fetch, merge, score, flag and persist a single workshop item."""
        now_ts = int(time.time())
        item_id = existing_data['workshop_id']

        # Step 1: Query API
        api_data = get_workshop_details_api(item_id, self.api_key)
        api_status = api_data.get("status", 0)

        if api_status not in HANDLED_API_STATUSES:
            # No branch below handles this code, so the item would be persisted as
            # status 200 and counted as a success. Capture the evidence; changing
            # that flow is a separate decision.
            capture.record_failure(
                kind="api_unhandled_status",
                stage="api_fetch",
                workshop_id=item_id,
                http_status=api_status,
                body=json.dumps(api_data, default=str),
                content_type="application/json",
                context={"handled_statuses": sorted(HANDLED_API_STATUSES)},
            )

        merged_data = existing_data.copy()
        # Attempt clock: set unconditionally, before the status branches, so a
        # 404, a 500 and a success all persist it. This is not optional
        # bookkeeping: get_next_items_to_scrape orders by api_fetched_at ASC,
        # and api_fetched_at now only moves on success, so without the attempt
        # clock a just-failed item keeps its stale api_fetched_at and is retried
        # at the front of its priority band in a tight loop. get_db_stats also
        # reports fetch recency from this column.
        merged_data["last_fetch_attempted_at"] = now_ts
        # The pre-fetch priority is what a temporary failure steps down from, so
        # take it before the queue fields are rewritten below.
        previous_priority = existing_data.get("api_priority") or 0
        merged_data["api_priority"] = 0
        merged_data["status"] = api_status

        if api_status != 200:
            self._settle_api_failure(merged_data, item_id, api_status, previous_priority)
            return

        # Step 2: Merge, score, and queue follow-up work
        merged_data = self._merge_and_clean_api_data(api_data, merged_data, item_id, now_ts)
        display_title = merged_data.get('title_en') or merged_data.get('title', 'Unknown Title')

        # Capture the pre-fetch priority to inherit for image/web/translation flagging
        inherited_prio = existing_data.get("api_priority", 0)

        self._score_wilson(merged_data)
        enriched = self._flag_scrape_and_image(merged_data, existing_data, item_id, inherited_prio)

        merged_data["status"] = 200
        insert_or_update_item(self.db_path, merged_data)

        self._flag_translations(merged_data, item_id, enriched, inherited_prio)

        # Step 3: Fetch User/Creator details (only for enriched items)
        self._refresh_creator(merged_data, enriched)

        logging.info(f"[A:{item_id}] \"{display_title}\"{' — \033[31mignored\033[0m' if not enriched else ''}")
        self._record_api_success()

    def _settle_api_failure(self, merged_data: dict, item_id: int, api_status: int,
                            previous_priority: int) -> None:
        """Persist a failed API outcome: dequeue if permanent, step down if not.

        A temporary failure keeps the item queued one priority level lower rather
        than clearing its priority. Clearing it left the item in no queue at all,
        and _promote_stale_items promotes only status 200, so a transient 500
        became permanent. The floor is 1 because priority 0 means "not queued".

        Statuses with no branch of their own reach here too, on purpose: falling
        through to the success path would persist them as 200 and count them as a
        success. The evidence is captured by the caller before this runs.
        """
        if api_status in PERMANENT_API_STATUSES:
            logging.warning(
                f"[A:{item_id}] Item not found ({api_status}) via API. "
                "Recording the failure and marking it dead (status=-1)."
            )
            merged_data["status"] = -1
            merged_data["api_priority"] = 0
            insert_or_update_item(self.db_path, merged_data)
            return

        retry_priority = max(1, previous_priority - 1)
        merged_data["api_priority"] = retry_priority
        insert_or_update_item(self.db_path, merged_data)
        logging.error(
            f"[A:{item_id}] API request failed ({api_status}). "
            f"Requeued at priority {retry_priority} to retry after the current queue."
        )
        self._record_api_failure()

    def _score_wilson(self, merged_data: dict) -> None:
        """Populate the derived Wilson scores from the freshly fetched counts."""
        merged_data["wilson_favorite_score"] = wilson_lower(
            merged_data.get("favorited", 0) or 0,
            merged_data.get("lifetime_subscriptions", 0) or 0)
        merged_data["wilson_subscription_score"] = wilson_lower(
            merged_data.get("subscriptions", 0) or 0,
            merged_data.get("lifetime_subscriptions", 0) or 0)

    def _flag_scrape_and_image(self, merged_data: dict, existing_data: dict,
                               item_id: int, inherited_prio: int) -> bool:
        """Queue web-scrape and image work for this item.

        Returns whether the item was enriched. Note the mixed sources: the
        unchanged-check compares the pre-fetch record against the merged one, so
        both must be passed.
        """
        appid = merged_data.get("consumer_appid")
        enriched = False
        if self._should_enrich(appid, merged_data):
            old_steam_updated = existing_data.get("steam_updated_at")
            new_steam_updated = merged_data.get("steam_updated_at")
            unchanged = (existing_data.get("extended_description") is not None
                         and old_steam_updated is not None
                         and old_steam_updated == new_steam_updated)

            if unchanged:
                merged_data["extended_description"] = existing_data["extended_description"]
                enriched = True
            else:
                flag_for_web_scrape(self.db_path, item_id, max(3, inherited_prio))
                enriched = True
        else:
            flag_for_web_scrape(self.db_path, item_id, max(1, inherited_prio))

        # Flag for image download if preview URL is present
        if merged_data.get("preview_url"):
            flag_for_image(self.db_path, item_id, max(3, inherited_prio) if enriched else max(1, inherited_prio))
        return enriched

    def _flag_translations(self, merged_data: dict, item_id: int,
                           enriched: bool, inherited_prio: int) -> None:
        """Flag title and short description for translation.

        The non-ASCII test lives in flag_field_for_translation. What this adds is
        the freshness test: a field whose translation was taken at the item's
        current Steam revision is left alone, so the staleness sweep does not
        re-translate unchanged text, while a field left behind by a source edit
        is re-queued. See translation_is_current.
        """
        if not enriched:
            return
        t_prio = max(3, inherited_prio)
        version = merged_data.get("translate_version")
        steam_updated = merged_data.get("steam_updated_at")
        for field, text, translated in [
            ("title_en", merged_data.get("title"), merged_data.get("title_en")),
            ("short_description_en", merged_data.get("short_description"),
             merged_data.get("short_description_en")),
        ]:
            if text and not translation_is_current(translated, version, steam_updated):
                flag_field_for_translation(self.db_path, "item", item_id, field, text, t_prio)

    def _refresh_creator(self, merged_data: dict, enriched: bool) -> None:
        """Refresh the creator's persona name if it is missing or stale."""
        creator_id = merged_data.get("creator")
        if not (creator_id and enriched):
            return
        try:
            creator_id = int(creator_id)
            existing_user = get_user(self.db_path, creator_id)
            should_update_user = True
            if existing_user and existing_user.get("api_fetched_at"):
                staleness = int(time.time()) - existing_user["api_fetched_at"]
                if staleness < self.user_staleness_days * 86400:
                    should_update_user = False
            if should_update_user:
                summaries = get_player_summaries([creator_id], self.api_key)
                if creator_id in summaries:
                    insert_or_update_user(self.db_path, self._build_user_record(creator_id, summaries[creator_id].get("personaname")))
        # Optional creator-persona enrichment; an unparseable creator value is skipped
        # and the persona is retried on a later cycle.
        except (ValueError, TypeError):
            pass

    def _record_api_failure(self) -> None:
        """Count a failed fetch and back off once a success streak has ended."""
        self.api_failures += 1
        self.api_successes = 0
        if self.api_failures >= 2 and self.api_had_streak:
            old_delay = self.api_delay
            self.api_delay = min(round(self.api_delay * (1.05 ** 10), 3),2)
            set_api_delay(self.api_delay)
            logging.info(f"Multiple consecutive API failures! Increasing API delay from {old_delay} to {self.api_delay}s.")
            self._save_config_value("api_delay_seconds", self.api_delay)
            self.api_had_streak = False

    def _record_api_success(self) -> None:
        """Count a good fetch and speed up after a long success streak."""
        self.api_successes += 1
        self.api_failures = 0
        if self.api_successes >= 5:
            self.api_had_streak = True
        if self.api_successes >= 100:
            old_delay = self.api_delay
            self.api_delay = max(0.01, round(self.api_delay / 1.05, 3))
            if old_delay != self.api_delay:
                set_api_delay(self.api_delay)
                logging.info(f"100 consecutive API successes! Decreasing API delay from {old_delay} to {self.api_delay} seconds.")
                self._save_config_value("api_delay_seconds", self.api_delay)
            self.api_successes = 0

    def _pid_file_removed(self) -> bool:
        if not self.running:
            return False
        exists = os.path.exists(".daemon.pid")
        if exists and not self._saw_pid_file:
            self._saw_pid_file = True
        if self._saw_pid_file and not exists:
            logging.info("PID file removed — initiating graceful shutdown")
            self.running = False
            return True
        return False

    def run(self):
        """Main loop that continuously queries and scrapes."""
        logging.info("Starting daemon loop...")
        self.translator.start()
        self._web_worker = WebScraperThread(self.db_path, self.pause_lock_file, daemon_config=self.config.get("daemon", {}), save_callback=self._save_config_value)
        self._web_worker.start()
        self._image_worker = ImageScraperThread(self.db_path, self.pause_lock_file, daemon_config=self.config.get("daemon", {}), save_callback=self._save_config_value)
        self._image_worker.start()
        if self._backup_worker is not None:
            self._backup_worker.start()
        while self.running:
            self.process_batch()
            self._pid_file_removed()
        logging.info("Daemon gracefully exited.")
        self._web_worker.running = False
        self._web_worker.join(timeout=5)
        logging.info("Web scraper thread stopped.")
        self._image_worker.running = False
        self._image_worker.join(timeout=5)
        logging.info("Image download thread stopped.")
        self.translator.running = False
        self.translator.join(timeout=5)
        logging.info("Translator thread stopped.")
        if self._backup_worker is not None:
            self._backup_worker.running = False
            self._backup_worker.join(timeout=5)
            logging.info("Backup thread stopped.")
            # Final synchronous snapshot, taken only after every writer thread
            # has been joined so no writer can be mid-transaction. Backup
            # failures are logged and swallowed: shutdown must still complete.
            try:
                self._backup_worker.run_now()
            except Exception as e:
                logging.error(f"Final database backup failed: {e}")

        # Counters are flushed on a timer during the run; this catches whatever
        # was recorded since the last flush. Failures are logged, not raised:
        # shutdown must still complete.
        try:
            capture.flush()
        except Exception as e:
            logging.error(f"Final failure-capture flush failed: {e}")

    def seed_database(self, target_new: int = 100):
        """
        Discovers workshop items via IPublishedFileService/QueryFiles API
        using cursor-based pagination (unlimited depth).
        """
        if not self.api_key:
            logging.error("No Steam API key configured. Discovery cannot run. "
                          "Set STEAM_API_KEY environment variable or add api.key to config.yaml.")
            return

        for appid in self.target_appids:
            # The guard must measure work the fetch queue can actually hand out.
            # It used to test count_unscraped_items -- items never successfully
            # fetched -- which is a disjoint population: on production this read
            # 890 while the fetch queue held 1, so discovery was suppressed
            # permanently and the queue could never refill.
            fetchable = count_fetchable_items(self.db_path)
            if fetchable >= target_new:
                logging.info(
                    "Queue appropriately filled (%d fetchable, >= %d) for AppID %s. "
                    "Skipping discovery. (%d items have never been fetched but are not queued.)",
                    fetchable, target_new, appid, count_unscraped_items(self.db_path),
                )
                continue

            app_tracking = get_app_tracking(self.db_path, appid)
            cursor = (app_tracking or {}).get("last_cursor") or "*"
            new_discovered_count = 0
            pages = 0

            logging.info(f"Discovering items for AppID {appid}, resuming from cursor...")
            while cursor and self.running:
                if self._pid_file_removed():
                    break
                result = query_workshop_files(appid, cursor=cursor, api_key=self.api_key)
                if result.get("error"):
                    logging.error(f"API error for AppID {appid}. Halting discovery.")
                    break

                if pages == 0 and result["total"]:
                    logging.info(f"AppID {appid} has ~{result['total']} total items.")

                page_new_count = 0
                for item in result["items"]:
                    wid = int(item.get("publishedfileid", 0))
                    if wid and insert_or_update_item(self.db_path, {"workshop_id": wid}):
                        page_new_count += 1

                new_discovered_count += page_new_count
                pages += 1
                logging.info(f"Cursor page {pages} provided {page_new_count} new items. (Total new: {new_discovered_count})")

                cursor = result.get("next_cursor") or ""
                if cursor:
                    update_app_tracking_cursor(self.db_path, appid, cursor)
                time.sleep(self.api_delay)

                if new_discovered_count >= target_new:
                    logging.info(f"Discovered {new_discovered_count} new items for AppID {appid}, enough for now.")
                    break

            if pages:
                logging.info(f"Finished scanning {pages} pages for AppID {appid}. Discovered {new_discovered_count} new items.")
                if not cursor:
                    self._cursor_exhausted = True
                    logging.info("Cursor exhausted — page-based discovery now eligible.")

    def _page_discovery_eligible(self) -> bool:
        if os.path.exists('.fetch_new'):
            return True
        if self._cursor_exhausted:
            return True
        conn = get_connection(self.db_path)
        scraped = conn.execute(
            "SELECT COUNT(*) FROM workshop_items WHERE api_fetched_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        return scraped >= 500

    def _run_page_discovery(self):
        if not self.api_key:
            return
        if int(time.time()) - self._last_page_discovery < 86400:
            if not os.path.exists('.fetch_new'):
                return
            logging.info("Fetch-new trigger file detected — bypassing 24h cooldown")
            try:
                os.remove('.fetch_new')
            except OSError as exc:
                pass
                logging.warning(
                    "Could not remove .fetch_new (%s); page discovery will keep bypassing the 24h cooldown.",
                    exc,
                )

        logging.info("Running page-based discovery (sort-by-update-time)...")
        self._last_page_discovery = int(time.time())

        for appid in self.target_appids:
            if not self.running:
                break
            cursor = "*"
            page = 0
            while cursor and self.running and page < 500:
                result = query_workshop_page_updated(appid, cursor, self.api_key)
                if result.get("error"):
                    logging.error(f"Page discovery error for AppID {appid}.")
                    break

                items = result.get("items", [])
                if page == 0:
                    logging.info(f"Page mode for AppID {appid}: ~{result.get('total', '?')} total items by update time.")

                page_new = 0
                wid_to_api_updated = {int(it.get("publishedfileid", 0)): it.get("time_updated") or 0 for it in items if it.get("publishedfileid")}

                # Fetch existing steam_updated_at in one query
                conn = get_connection(self.db_path)
                placeholders = ",".join("?" * len(wid_to_api_updated))
                existing_rows = {}
                if wid_to_api_updated:
                    rows = conn.execute(
                        f"SELECT workshop_id, steam_updated_at FROM workshop_items WHERE workshop_id IN ({placeholders})",
                        list(wid_to_api_updated.keys()),
                    ).fetchall()
                    existing_rows = {r["workshop_id"]: r["steam_updated_at"] for r in rows}

                for wid, api_updated in wid_to_api_updated.items():
                    db_updated = existing_rows.get(wid)
                    if db_updated is None:
                        # Wholly new item
                        page_new += 1
                    elif api_updated and (not db_updated or api_updated > db_updated):
                        # Existing item with a fresher update time on Steam
                        page_new += 1

                    insert_or_update_item(self.db_path, {"workshop_id": wid, "api_priority": 5})

                conn.close()
                page += 1

                if page_new == 0:
                    logging.info(f"Page mode: no new or updated items on page {page}, stopping for AppID {appid}.")
                    break

                logging.info(f"Page mode: page {page} added/updated {page_new} items for AppID {appid}.")
                cursor = result.get("next_cursor") or ""
                time.sleep(self.api_delay)

        logging.info("Page-based discovery complete.")
