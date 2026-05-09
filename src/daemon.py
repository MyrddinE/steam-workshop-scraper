import time
import math
import signal
import json
import logging
import os
import random
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
    save_app_filter,
    get_connection,
    get_item_details,
    normalize_tags
)
from src.steam_api import get_workshop_details_api, query_workshop_items, get_player_summaries, query_workshop_files
from src.web_scraper import scrape_extended_details, discover_items_by_date_html
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

    @staticmethod
    def _compute_filter_hash(filter_text: str, required_tags: list[str], excluded_tags: list[str]) -> str:
        """Returns a deterministic hash of the current filter state for detecting changes."""
        current_filter = {
            "text": filter_text,
            "req_tags": sorted(required_tags or []),
            "excl_tags": sorted(excluded_tags or [])
        }
        return json.dumps(current_filter, sort_keys=True)

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

    def _load_initial_filter_state(self):
        """Pre-loads the last known filter state from the DB on startup."""
        logging.info("Loading initial filter state from database...")
        for appid in self.target_appids:
            app_tracking = get_app_tracking(self.db_path, appid)
            if app_tracking:
                saved_filter_text = app_tracking.get("filter_text", "")
                saved_required_tags = json.loads(app_tracking.get("required_tags", "[]"))
                saved_excluded_tags = json.loads(app_tracking.get("excluded_tags", "[]"))
                current_filter_hash = self._compute_filter_hash(saved_filter_text, saved_required_tags, saved_excluded_tags)
                self.last_filters[appid] = {"hash": current_filter_hash, "start_time": app_tracking.get("last_historical_date_scanned")}

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

            # Step 2: Scrape Extended Details
            url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
            scrape_data = scrape_extended_details(url)
            display_title = merged_data.get('title_en') or merged_data.get('title', 'Unknown Title')
            
            if not scrape_data:
                merged_data["status"] = 206 # Partial Content
                logging.debug(f"DEBUG: Calling insert_or_update_item with merged_data: {merged_data}")
                insert_or_update_item(self.db_path, merged_data)

                # Assemble detailed log message
                successful_fields = [k for k, v in merged_data.items() if v is not None]

                failed_fields = ["extended_description", "tags"] # Known scrape targets
                logging.warning(
                    f"[{item_id}] '{display_title}' | Scraper failed, partial data saved. "
                    f"Successfully pulled from API: {successful_fields}. "
                    f"Failed to scrape from web: {failed_fields}."
                )
                
                # Record Failure and Adjust Delay
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

            merged_data["status"] = 200 # OK
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

    def _find_initial_start_date(self, appid: int, search_text: str = "", required_tags: list[str] = None, excluded_tags: list[str] = None) -> int:
        """
        Performs a binary search to find the Unix timestamp of the oldest 
        date range containing workshop items for the appid, given filter criteria.
        Returns a timestamp just before the first items appear.
        """
        logging.info(f"AppID {appid} has no tracking history. Performing binary search to find the very first mod release...")
        
        low = 1317484800 # October 1st, 2011 (Workshop Release)
        now = int(time.time())
        high = now
        
        while high - low > 86400: # 1-day resolution
            if not self.running:
                logging.info("Binary search interrupted by shutdown signal.")
                break
            mid = low + (high - low) // 2
            logging.info(f"Binary search checking range up to {mid} ({datetime.fromtimestamp(mid, timezone.utc).date()})...")
            
            # Use web scraper since API doesn't support date filtering
            found_items = discover_items_by_date_html(appid, low, mid, page=1, search_text=search_text, required_tags=required_tags, excluded_tags=excluded_tags)
            
            if len(found_items) > 0:
                # Items found in this left half. The first item is somewhere in here.
                # So we move our upper bound down to mid.
                high = mid
            else:
                # No items found in the left half. The first item must be after mid.
                # So we move our lower bound up to mid.
                low = mid
                
            time.sleep(1) # Be polite to API during search
            
        # Move back exactly one window size just to be safe, but no earlier than Workshop release
        final_start = max(1317484800, low - 86400)
        logging.info(f"Binary search complete. First items appeared around {datetime.fromtimestamp(final_start, timezone.utc).date()}.")
        return final_start

    def seed_database(self, target_new: int = 100):
        """
        Historical forward scraping strategy. Uses IPublishedFileService/QueryFiles
        to find items within dynamic date ranges.
        """
        now = int(time.time())

        # AppID-specific discovery loop for initial seeding
        for appid in self.config["daemon"]["target_appids"]:
            # Check if queue is already appropriately filled before starting discovery for this appid
            if count_unscraped_items(self.db_path) >= target_new:
                logging.info(f"Queue appropriately filled (>= {target_new}) for AppID {appid}. Skipping discovery.")
                continue # Skip to next appid

            app_tracking = get_app_tracking(self.db_path, appid)
            if app_tracking is None:
                app_tracking = {}
            last_scanned_date = app_tracking.get("last_historical_date_scanned") or 1274400000
            initial_start_time = last_scanned_date # Store this for the log message on early exit
            saved_filter_text = app_tracking.get("filter_text") or ""
            saved_required_tags = json.loads(app_tracking.get("required_tags") or "[]")
            saved_excluded_tags = json.loads(app_tracking.get("excluded_tags") or "[]")
            window_size = app_tracking.get("window_size") or 30 * 24 * 3600 # Start with a 30-day window

            current_filter_hash = self._compute_filter_hash(saved_filter_text, saved_required_tags, saved_excluded_tags)

            if appid not in self.last_filters:
                self.last_filters[appid] = {"hash": None, "start_time": None}

            filter_changed = (self.last_filters[appid]["hash"] != current_filter_hash)

            if filter_changed:
                logging.info(f"Filter changed for AppID {appid}. Resetting historical scan.")
                # If filter changed, reset the scan to the very beginning.
                # The binary search will find the first item under the new filter.
                start_time = self._find_initial_start_date(appid, saved_filter_text, saved_required_tags, saved_excluded_tags)
                last_scanned_date = start_time # Update tracking to new starting point
                self.last_filters[appid]["hash"] = current_filter_hash
                self.last_filters[appid]["start_time"] = start_time
                update_app_tracking(self.db_path, appid, start_time, window_size) # Also update DB to reflect new start
            else:
                start_time = last_scanned_date
                self.last_filters[appid]["hash"] = current_filter_hash
                self.last_filters[appid]["start_time"] = start_time

            # If the app was scanned recently, skip for now.
            if (now - last_scanned_date) < (24 * 3600) and not filter_changed:
                logging.info(f"AppID {appid} is up to date (last scanned within 24h). Skipping discovery.")
                continue

            logging.info(f"Discovering items for AppID {appid} starting from timestamp {start_time}...")
            new_discovered_count = 0
            last_successful_window_end_time = start_time # Track the furthest point successfully scanned

            # Continue looping while we're finding new items or haven't reached current time
            page = 1
            while start_time < now and self.running:
                end_time = min(start_time + window_size, now)
                if end_time == start_time: # Avoid infinite loop if start_time catches up to now exactly
                    break

                logging.info(f"Querying window: {datetime.fromtimestamp(start_time, timezone.utc).date()} to {datetime.fromtimestamp(end_time, timezone.utc).date()} ({round((end_time-start_time)/86400, 1)} days)")

                window_new_count = 0
                window_has_errors = False
                known_total_pages = 1

                while self.running:
                    found_items, current_total_pages = discover_items_by_date_html(appid, start_time, end_time, page=page, search_text=saved_filter_text, required_tags=saved_required_tags, excluded_tags=saved_excluded_tags)

                    if current_total_pages == -1:
                        window_has_errors = True
                        logging.warning(f"Page {page} failed to fetch due to an error. Flagging window for retry.")
                    else:
                        known_total_pages = current_total_pages

                    if known_total_pages == 0:
                        # Legitimately empty window
                        break

                    if current_total_pages != -1 and len(found_items) < 30 and page < known_total_pages:
                        window_has_errors = True
                        logging.warning(f"Page {page}/{known_total_pages} returned only {len(found_items)} items. Flagging window for retry.")

                    page_new_count = 0
                    for item_id in found_items:
                        # discover_items_by_date_html now returns a list of IDs
                        if insert_or_update_item(self.db_path, {"workshop_id": item_id}):
                            page_new_count += 1

                    if current_total_pages != -1:
                        logging.info(f"Page {page}/{known_total_pages} provided {page_new_count} new items.")
                    
                    window_new_count += page_new_count
                    time.sleep(self.delay) # Be polite
                    
                    # Stop if we have reached or exceeded the total number of pages
                    if page >= known_total_pages:
                        break

                    page += 1

                new_discovered_count += window_new_count
                logging.info(f"Window provided {window_new_count} new items. (Total new this seed: {new_discovered_count})")

                if not window_has_errors:
                    last_successful_window_end_time = end_time
                    # Move window forward
                    start_time = end_time
                else:
                    logging.warning(f"Window encountered errors or partial pages. Halting discovery for AppID {appid} to retry later.")
                    last_successful_window_end_time -= window_size * random.random()
                    break # Break out of discovery loop for this appid

                # Dynamic adjustment of next window size based on density
                window_adjustment = 2/max(math.log2(page),0.5)
                window_size *= window_adjustment
                window_size = min(window_size, 240 * 24 * 3600) # cap at 240 day window
                window_size = max(window_size, 3600) # no lower than 1 hour window

                page = 1

                if new_discovered_count > 100:
                    break

            # After the discovery loop for this appid, update tracking to the furthest successful point
            if last_successful_window_end_time != last_scanned_date: # Only update if we made progress
                logging.info(f"Updating last scanned date for AppID {appid} to {datetime.fromtimestamp(last_successful_window_end_time, timezone.utc).date()}.")
                update_app_tracking(self.db_path, appid, last_successful_window_end_time, window_size)
            else:
                logging.info(f"Discovery for AppID {appid} completed naturally, but no new full windows scanned. Last scanned date remains {datetime.fromtimestamp(last_scanned_date, timezone.utc).date()}.")
