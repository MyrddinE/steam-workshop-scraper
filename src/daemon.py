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
    _evaluate_filters
)
from src.steam_api import get_workshop_details_api, query_workshop_items, get_player_summaries, query_workshop_files, set_api_delay
from src.translator import TranslatorThread, is_ascii
from src.config import save_config
from src.database import flag_for_web_scrape, flag_field_for_translation
from src.web_worker import WebScraperThread


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
        
        # State variables for dynamic delay adjustment
        self.api_successes = 0
        self.api_failures = 0
        self.api_had_streak = False

        # Enforce required target_appids
        self.target_appids = config.get("daemon", {}).get("target_appids")
        if not self.target_appids or not isinstance(self.target_appids, list):
            raise ValueError("Configuration error: 'daemon.target_appids' must be provided as a list.")
        
        # Pre-load initial filter state to avoid false positives on startup
        self._load_initial_filter_state()
        
        # Setup graceful shutdown
        signal.signal(signal.SIGINT, self.handle_shutdown)
        signal.signal(signal.SIGTERM, self.handle_shutdown)

    def _persist_delay(self):
        """Saves the current delay to config file."""
        if "daemon" not in self.config:
            self.config["daemon"] = {}
        self.config["daemon"]["api_delay_seconds"] = self.api_delay
        save_config(self.config_path, self.config)

    def _build_user_record(self, steamid: int, personaname: str) -> dict:
        """Builds a user record dict for upsert, flagging for translation if non-ASCII."""
        record = {
            "steamid": steamid,
            "personaname": personaname,
            "dt_updated": datetime.now(timezone.utc).isoformat()
        }
        if not is_ascii(personaname):
            record["translation_priority"] = 1
        return record

    def _merge_and_clean_api_data(self, api_data: dict, existing_data: dict, item_id: int, now_iso: str) -> dict:
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

        allowed_keys = {
            "workshop_id", "dt_found", "dt_updated", "dt_attempted", "dt_translated", "status", "title", "title_en",
            "creator", "creator_appid", "consumer_appid", "filename", "file_size", "preview_url",
            "hcontent_file", "hcontent_preview", "short_description", "short_description_en", "time_created",
            "time_updated", "visibility", "banned", "ban_reason", "app_name", "file_type",
            "subscriptions", "favorited", "views", "tags", "extended_description", "extended_description_en", "language",
            "lifetime_subscriptions", "lifetime_favorited", "translation_priority",
            "wilson_favorite_score", "wilson_subscription_score",
        }
        known_ignored_keys = {"result", "is_queued_for_subscription", "needs_web_scrape"}
        clean = {}
        for k, v in merged.items():
            if k in allowed_keys:
                clean[k] = v
            elif k not in known_ignored_keys:
                val_preview = str(v)[:20] + "..." if len(str(v)) > 20 else str(v)
                logger = logging.info if v is not None and str(v).strip() != "" else logging.debug
                logger(f"Discarding unknown API column: '{k}' with value '{val_preview}' for item {item_id}")

        clean["dt_attempted"] = now_iso
        if "tags" in clean:
            clean["tags"] = normalize_tags(clean["tags"])
        return clean

    def _evaluate_translation_needs(self, merged_data: dict, existing_data: dict) -> dict:
        """Sets translation_priority on merged_data if fields contain non-ASCII and are new/changed."""
        is_unicode = (
            not is_ascii(merged_data.get("title", ""))
            or not is_ascii(merged_data.get("short_description", ""))
            or not is_ascii(merged_data.get("extended_description", ""))
        )
        is_translated = merged_data.get("dt_translated") is not None
        is_changed = (
            merged_data.get("title") != existing_data.get("title")
            or merged_data.get("short_description") != existing_data.get("short_description")
            or merged_data.get("extended_description") != existing_data.get("extended_description")
        )
        if is_unicode and (not is_translated or is_changed):
            merged_data["translation_priority"] = 1
        return merged_data

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
        """Processes a single batch of workshop items."""
        try:
            items_to_scrape = get_next_items_to_scrape(self.db_path, limit=self.batch_size,
                                                       staleness_days=self.item_staleness_days)
        except Exception as e:
            logging.error(f"Database error in process_batch: {e}")
            time.sleep(5)
            return
        
        if not items_to_scrape:
            logging.debug("No items to scrape. Expanding discovery...")
            self.seed_database()
            try:
                items_to_scrape = get_next_items_to_scrape(self.db_path, limit=self.batch_size,
                                                           staleness_days=self.item_staleness_days)
            except Exception as e:
                logging.error(f"Database error after seeding: {e}")
                time.sleep(5)
                return
        
        if not items_to_scrape:
            for _ in range(600):
                if not self.running:
                    return
                time.sleep(1)
            return

        for existing_data in items_to_scrape:
            if not self.running:
                break # Exit early if shutting down

            now_iso = datetime.now(timezone.utc).isoformat()
            item_id = existing_data['workshop_id']
            if existing_data.get('title') is None or existing_data.get('creator') is None:
                log_action = 'Add'
            elif existing_data.get('extended_description') is None:
                log_action = 'Complete'
            else:
                log_action = 'Update'

            # Step 1: Query API
            api_data = get_workshop_details_api(item_id, self.api_key)
            api_status = api_data.get("status", 0)

            merged_data = existing_data.copy()
            merged_data["dt_attempted"] = now_iso
            merged_data["status"] = api_status
            
            if api_status == 404:
                logging.warning(f"[{item_id}] Item not found (404) via API, it may have been deleted.")
                insert_or_update_item(self.db_path, merged_data)
                self.api_failures += 1
                self.api_successes = 0
                if self.api_failures >= 2 and self.api_had_streak:
                    old_delay = self.api_delay
                    self.api_delay = round(self.api_delay * (1.05 ** 10), 3)
                    set_api_delay(self.api_delay)
                    logging.info(f"Multiple consecutive API failures! Increasing API delay from {old_delay} to {self.api_delay}s.")
                    self._persist_delay()
                    self.api_had_streak = False
                continue
            elif api_status == 500:
                logging.error(f"[{item_id}] API request failed (500). Retrying later.")
                insert_or_update_item(self.db_path, merged_data)
                self.api_failures += 1
                self.api_successes = 0
                if self.api_failures >= 2 and self.api_had_streak:
                    old_delay = self.api_delay
                    self.api_delay = round(self.api_delay * (1.05 ** 10), 3)
                    set_api_delay(self.api_delay)
                    logging.info(f"Multiple consecutive API failures! Increasing API delay from {old_delay} to {self.api_delay}s.")
                    self._persist_delay()
                    self.api_had_streak = False
                continue

            merged_data = self._merge_and_clean_api_data(api_data, merged_data, item_id, now_iso)
            display_title = merged_data.get('title_en') or merged_data.get('title', 'Unknown Title')

            views = merged_data.get("views", 0) or 0
            merged_data["wilson_favorite_score"] = wilson_lower(
                merged_data.get("favorited", 0) or 0, views)
            merged_data["wilson_subscription_score"] = wilson_lower(
                merged_data.get("subscriptions", 0) or 0,
                merged_data.get("lifetime_subscriptions", 0) or 0)

            appid = merged_data.get("consumer_appid")
            enriched = False
            if self._should_enrich(appid, merged_data):
                old_time_updated = existing_data.get("time_updated")
                new_time_updated = merged_data.get("time_updated")
                unchanged = (existing_data.get("extended_description") is not None
                             and old_time_updated is not None
                             and old_time_updated == new_time_updated)

                if unchanged:
                    merged_data["extended_description"] = existing_data["extended_description"]
                    enriched = True
                else:
                    flag_for_web_scrape(self.db_path, item_id, 3)
                    enriched = True
            else:
                flag_for_web_scrape(self.db_path, item_id, 1)

            merged_data["status"] = 200
            insert_or_update_item(self.db_path, merged_data)

            # Flag translation for title and short description (ASCII check)
            if enriched:
                for field in [("title_en", merged_data.get("title")),
                              ("short_description_en", merged_data.get("short_description"))]:
                    if field[1]:
                        flag_field_for_translation(self.db_path, "item", item_id, field[0], field[1], 3)
            
            # Step 3: Fetch User/Creator details (only for enriched items)
            creator_id = merged_data.get("creator")
            if creator_id and enriched:
                try:
                    creator_id = int(creator_id)
                    existing_user = get_user(self.db_path, creator_id)
                    should_update_user = True
                    if existing_user and existing_user.get("dt_updated"):
                        last_upd = datetime.fromisoformat(existing_user["dt_updated"])
                        if (datetime.now(timezone.utc) - last_upd).days < self.user_staleness_days:
                            should_update_user = False
                    if should_update_user:
                        summaries = get_player_summaries([creator_id], self.api_key)
                        if creator_id in summaries:
                            insert_or_update_user(self.db_path, self._build_user_record(creator_id, summaries[creator_id].get("personaname")))
                except (ValueError, TypeError):
                    pass

            logging.info(f"[{item_id}] \"{display_title}\"{' — \033[31mignored\033[0m' if not enriched else ''}")
            
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
                    self._persist_delay()
                self.api_successes = 0

    def run(self):
        """Main loop that continuously queries and scrapes."""
        logging.info("Starting daemon loop...")
        self.translator.start()
        web_worker = WebScraperThread(self.db_path, self.pause_lock_file, daemon_config=self.config.get("daemon", {}))
        web_worker.start()
        while self.running:
            self.process_batch()
        logging.info("Daemon gracefully exited.")
        web_worker.running = False
        web_worker.join(timeout=5)
        logging.info("Web scraper thread stopped.")
        self.translator.running = False
        self.translator.join(timeout=5)
        logging.info("Translator thread stopped.")

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
            if count_unscraped_items(self.db_path) >= target_new:
                logging.info(f"Queue appropriately filled (>= {target_new}) for AppID {appid}. Skipping discovery.")
                continue

            app_tracking = get_app_tracking(self.db_path, appid)
            cursor = (app_tracking or {}).get("last_cursor") or "*"
            new_discovered_count = 0
            pages = 0

            logging.info(f"Discovering items for AppID {appid}, resuming from cursor...")
            while cursor and self.running:
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
