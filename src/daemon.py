import time
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
    save_app_filter,
    get_connection,
    get_item_details,
    normalize_tags
)
from src.steam_api import get_workshop_details_api, query_workshop_items, get_player_summaries, query_workshop_files
from src.web_scraper import scrape_extended_details
from src.translator import TranslatorThread, is_ascii
from src.config import save_config


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
        self.delay = config.get("daemon", {}).get("request_delay_seconds", 5)
        self.pause_lock_file = ".pauselock"
        
        # Translator thread
        self.translator = TranslatorThread(config)
        
        # State variables for dynamic delay adjustment
        self.consecutive_successes = 0
        self.consecutive_failures = 0
        self.had_recent_success_streak = False

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
        self.config["daemon"]["request_delay_seconds"] = self.delay
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
            "lifetime_subscriptions", "lifetime_favorited", "translation_priority"
        }
        known_ignored_keys = {"result", "is_queued_for_subscription"}
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
        Non-matching items get basic API metadata stored but skip extended scraping."""
        if appid is None or appid not in self.target_appids:
            return True
        app_tracking = get_app_tracking(self.db_path, appid)
        if app_tracking is None:
            return True
        filter_text = (app_tracking.get("filter_text") or "").strip()
        required_tags = json.loads(app_tracking.get("required_tags") or "[]")
        excluded_tags = json.loads(app_tracking.get("excluded_tags") or "[]")
        if not filter_text and not required_tags and not excluded_tags:
            return True
        if filter_text:
            title = (item.get("title") or "").lower()
            desc = (item.get("short_description") or "").lower()
            if filter_text.lower() not in title and filter_text.lower() not in desc:
                return False
        if required_tags:
            item_tags = []
            try:
                item_tags = json.loads(item.get("tags") or "[]")
            except: pass
            if not all(t in item_tags for t in required_tags):
                return False
        if excluded_tags:
            item_tags = []
            try:
                item_tags = json.loads(item.get("tags") or "[]")
            except: pass
            if any(t in item_tags for t in excluded_tags):
                return False
        return True

    def _load_initial_filter_state(self):
        """Pre-loads filter state and page tracking from the DB on startup."""
        for appid in self.target_appids:
            app_tracking = get_app_tracking(self.db_path, appid)
            if app_tracking:
                self.last_filters[appid] = {"last_page": app_tracking.get("last_page_scanned", 0) or 0}

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
        # Proactive user discovery check
        try:
            self.expand_user_discovery()
                
            # Seeding check: If we have fewer than 100 unscraped items, fetch the next page
            unscraped = count_unscraped_items(self.db_path)
            if unscraped < 10:
                logging.debug(f"Low unscraped queue ({unscraped}). Expanding discovery...")
                self.seed_database()

            items_to_scrape = get_next_items_to_scrape(self.db_path, limit=self.batch_size)
        except Exception as e:
            logging.error(f"Database error in process_batch: {e}")
            time.sleep(5)
            return
        
        if not items_to_scrape:
            # If still nothing, sleep
            time.sleep(self.delay * 5)
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
                time.sleep(self.delay)
                continue # Skip to next item
            elif api_status == 500:
                logging.error(f"[{item_id}] API request failed (500). Retrying later.")
                insert_or_update_item(self.db_path, merged_data)
                time.sleep(self.delay*2)
                continue # Skip to next item

            merged_data = self._merge_and_clean_api_data(api_data, merged_data, item_id, now_iso)
            display_title = merged_data.get('title_en') or merged_data.get('title', 'Unknown Title')

            appid = merged_data.get("consumer_appid")
            if self._should_enrich(appid, merged_data):
                url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
                scrape_data = scrape_extended_details(url)

                if not scrape_data:
                    merged_data["status"] = 206
                    insert_or_update_item(self.db_path, merged_data)
                    logging.warning(
                        f"[{item_id}] '{display_title}' | Scraper failed, partial data saved. "
                        f"API data saved, but extended description could not be scraped."
                    )
                    self.consecutive_failures += 1
                    self.consecutive_successes = 0
                    if self.consecutive_failures >= 2 and self.had_recent_success_streak:
                        old_delay = self.delay
                        self.delay += max(1.0, round(self.delay * 0.10))
                        logging.info(f"Multiple consecutive failures after a success streak! Increasing delay from {old_delay} to {self.delay} seconds.")
                        self._persist_delay()
                        self.had_recent_success_streak = False
                    time.sleep(self.delay)
                    continue

                merged_data["extended_description"] = scrape_data.get("description")
                merged_data = self._evaluate_translation_needs(merged_data, existing_data)
            else:
                logging.info(f"[{item_id}] '{display_title}' does not match enrichment filters for AppID {appid}. Storing API data only.")

            merged_data["status"] = 200
            insert_or_update_item(self.db_path, merged_data)
            
            # Step 3: Fetch User/Creator details
            creator_id = merged_data.get("creator")
            if creator_id:
                try:
                    creator_id = int(creator_id)
                    existing_user = get_user(self.db_path, creator_id)
                    should_update_user = True
                    if existing_user and existing_user.get("dt_updated"):
                        last_upd = datetime.fromisoformat(existing_user["dt_updated"])
                        if (datetime.now(timezone.utc) - last_upd).days < 7:
                            should_update_user = False
                    if should_update_user:
                        summaries = get_player_summaries([creator_id], self.api_key)
                        if creator_id in summaries:
                            insert_or_update_user(self.db_path, self._build_user_record(creator_id, summaries[creator_id].get("personaname")))
                except (ValueError, TypeError):
                    pass

            logging.info(f"[{item_id}] \"{display_title}\"")
            
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            if self.consecutive_successes >= 5:
                self.had_recent_success_streak = True
            if self.consecutive_successes >= 100:
                old_delay = self.delay
                self.delay = max(1.0, self.delay - 0.1)
                if old_delay != self.delay:
                    logging.info(f"100 consecutive successes! Decreasing delay from {old_delay} to {self.delay} seconds.")
                    self._persist_delay()
                self.consecutive_successes = 0
            
            # Polite delay between items
            time.sleep(self.delay)

    def run(self):
        """Main loop that continuously queries and scrapes."""
        logging.info("Starting daemon loop...")
        self.translator.start()
        while self.running:
            while os.path.exists(self.pause_lock_file):
                logging.info("Scraping paused by TUI...")
                time.sleep(5)
            
            self.process_batch()
        logging.info("Daemon gracefully exited.")

    def seed_database(self, target_new: int = 100):
        """
        Discovers workshop items via IPublishedFileService/QueryFiles API.
        Paginates from page 1 (newest first) until target_new new items found.
        """
        for appid in self.target_appids:
            if count_unscraped_items(self.db_path) >= target_new:
                logging.info(f"Queue appropriately filled (>= {target_new}) for AppID {appid}. Skipping discovery.")
                continue

            app_tracking = get_app_tracking(self.db_path, appid)
            last_page = (app_tracking or {}).get("last_page_scanned", 0) or 0
            max_pages = 500

            logging.info(f"Discovering items for AppID {appid}, starting from page {last_page + 1}...")
            new_discovered_count = 0
            total = None
            page = last_page + 1

            while page <= max_pages and self.running:
                result = query_workshop_files(appid, page=page, api_key=self.api_key)
                if not result["items"] and total is None:
                    break

                if total is None and result["total"]:
                    total = result["total"]
                    max_pages = min(max_pages, max(1, (total + 99) // 100))
                    logging.info(f"AppID {appid} has ~{total} total items across {max_pages} pages.")

                page_new_count = 0
                for item in result["items"]:
                    wid = int(item.get("publishedfileid", 0))
                    if wid and insert_or_update_item(self.db_path, {"workshop_id": wid}):
                        page_new_count += 1

                new_discovered_count += page_new_count
                logging.info(f"Page {page}/{max_pages} provided {page_new_count} new items. (Total new: {new_discovered_count})")
                update_app_tracking_page(self.db_path, appid, page)
                time.sleep(self.delay)

                if new_discovered_count >= target_new:
                    logging.info(f"Discovered {new_discovered_count} new items for AppID {appid}, enough for now.")
                    break

                if len(result["items"]) == 0:
                    break

                page += 1

            if page > last_page + 1:
                logging.info(f"Finished scanning pages {last_page + 1}-{page} for AppID {appid}. Discovered {new_discovered_count} new items.")
