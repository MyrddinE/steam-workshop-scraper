import time
import signal
import json
import logging
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
    get_connection # Added
)
from src.steam_api import get_workshop_details_api, query_workshop_items, get_player_summaries, query_files_by_date
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
        
        # Setup graceful shutdown
        signal.signal(signal.SIGINT, self.handle_shutdown)
        signal.signal(signal.SIGTERM, self.handle_shutdown)

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
                        user_record = {
                            "steamid": sid,
                            "personaname": pdata.get("personaname"),
                            "dt_updated": datetime.now(timezone.utc).isoformat()
                        }
                        if not is_ascii(user_record["personaname"]):
                            user_record["translation_priority"] = 1
                            logging.info(f"User {sid} ('{user_record['personaname']}') flagged for translation.")
                        
                        insert_or_update_user(self.db_path, user_record)
                        logging.info(f"Updated profile for user {sid}: '{user_record['personaname']}'")
                    else:
                        # Insert a placeholder so we don't keep trying every batch
                        insert_or_update_user(self.db_path, {
                            "steamid": sid, 
                            "personaname": f"SteamID:{sid}",
                            "dt_updated": datetime.now(timezone.utc).isoformat()
                        })
            except Exception as e:
                logging.error(f"Error expanding user discovery: {e}")

    def process_batch(self):
        """Processes a single batch of workshop items."""
        # Proactive user discovery check
        try:
            self.expand_user_discovery()
            
            # Seeding check: If we have fewer than 100 unscraped items, fetch the next page
            unscraped = count_unscraped_items(self.db_path)
            if unscraped < 100:
                logging.info(f"Low unscraped queue ({unscraped}). Expanding discovery...")
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

        for item_id in items_to_scrape:
            if not self.running:
                break # Exit early if shutting down

            now_iso = datetime.now(timezone.utc).isoformat()
            base_data = {
                "workshop_id": item_id,
                "dt_attempted": now_iso
            }

            # Step 1: Query API
            api_data = get_workshop_details_api(item_id, self.api_key)

            # Initialize base_data with item_id, attempt timestamp, and API-provided status
            base_data = {
                "workshop_id": item_id,
                "dt_attempted": now_iso,
                "status": api_data.get("status", 0) # Default to 0 if API doesn't provide one
            }
            
            if base_data["status"] == 404:
                logging.warning(f"[{item_id}] Item not found (404) via API. Marking as such.")
                insert_or_update_item(self.db_path, base_data)
                continue # Skip to next item
            elif base_data["status"] == 500:
                logging.error(f"[{item_id}] API request failed (500). Retrying later.")
                insert_or_update_item(self.db_path, base_data) # Store 500 status
                continue # Skip to next item

            # If API call was successful, proceed with processing its data
            # Merge API data (it will contain actual item details if status != 404/500)
            # Ensure the API data does not override workshop_id, dt_attempted, status which are already set in base_data
            api_data.pop("publishedfileid", None) # Remove it if present, as it's mapped to workshop_id
            api_data.pop("status", None) # Remove status as we explicitly set it in base_data
            base_data.update(api_data)

            # Map keys that differ from our schema (these keys were already removed in steam_api.py, but for safety.)
            if "creator_app_id" in base_data:
                base_data["creator_appid"] = base_data.pop("creator_app_id")
            if "consumer_app_id" in base_data:
                base_data["consumer_appid"] = base_data.pop("consumer_app_id")
            if "description" in base_data:
                base_data["short_description"] = base_data.pop("description")

            # Remove keys that don't match our schema
            allowed_keys = {
                "workshop_id", "dt_found", "dt_updated", "dt_attempted", "status", "title",
                "creator", "creator_appid", "consumer_appid", "filename", "file_size", "preview_url",
                "hcontent_file", "hcontent_preview", "short_description", "time_created",
                "time_updated", "visibility", "banned", "ban_reason", "app_name", "file_type",
                "subscriptions", "favorited", "views", "tags", "extended_description", "language",
                "lifetime_subscriptions", "lifetime_favorited", "translation_priority"
            }
            
            clean_api_data = {}
            known_ignored_keys = {"result"} # 'result' is handled by steam_api.py, should not appear here
            
            for k, v in base_data.items():
                if k in allowed_keys:
                    clean_api_data[k] = v
                elif k not in known_ignored_keys:
                    val_preview = str(v)[:20] + "..." if len(str(v)) > 20 else str(v)
                    if v is not None and str(v).strip() != "":
                        logging.info(f"Discarding unknown API column: '{k}' with value '{val_preview}' for item {item_id}")
                    else:
                        logging.debug(f"Discarding unknown (empty) API column: '{k}' for item {item_id}")
                
            base_data = clean_api_data # Overwrite base_data with only allowed keys
            base_data["dt_updated"] = now_iso

            # Check if tags were provided as list, JSON stringify for SQLite
            if "tags" in base_data and isinstance(base_data["tags"], list):
                base_data["tags"] = json.dumps(base_data["tags"], ensure_ascii=False)

            # Step 2: Scrape Extended Details
            url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
            scrape_data = scrape_extended_details(url)
            
            if not scrape_data:
                base_data["status"] = 206 # Partial Content
                logging.debug(f"DEBUG: Calling insert_or_update_item with base_data: {base_data}") # Debug print
                insert_or_update_item(self.db_path, base_data)

                # Assemble detailed log message
                successful_fields = [k for k, v in base_data.items() if v is not None]

                failed_fields = ["extended_description", "tags"] # Known scrape targets
                logging.warning(
                    f"[{item_id}] '{base_data.get('title', 'Unknown')}' | Scraper failed, partial data saved. "
                    f"Successfully pulled from API: {successful_fields}. "
                    f"Failed to scrape from web: {failed_fields}."
                )
                
                # Record Failure and Adjust Delay
                self.consecutive_failures += 1
                self.consecutive_successes = 0
                
                if self.consecutive_failures >= 2 and self.had_recent_success_streak:
                    old_delay = self.delay
                    increase = max(1.0, round(self.delay * 0.10))
                    self.delay += increase
                    logging.info(f"Multiple consecutive failures after a success streak! Increasing delay from {old_delay} to {self.delay} seconds.")
                    if "daemon" not in self.config:
                        self.config["daemon"] = {}
                    self.config["daemon"]["request_delay_seconds"] = self.delay
                    save_config(self.config_path, self.config)
                    
                    self.had_recent_success_streak = False # Reset so we don't spam increases
                
                time.sleep(self.delay)
                continue

            # Merge Scrape Data
            base_data["extended_description"] = scrape_data.get("description")
            
            # Combine tags from scrape if not present from API
            if scrape_data.get("tags"):
                try:
                    existing_tags = json.loads(base_data.get("tags", "[]"))
                except json.JSONDecodeError:
                    existing_tags = []
                
                # Normalize existing and new tags to strings for unique merging
                def normalize_tags(tlist):
                    norm = []
                    for t in tlist:
                        if isinstance(t, dict) and "tag" in t:
                            norm.append(str(t["tag"]))
                        elif isinstance(t, str):
                            norm.append(t)
                    return norm

                merged_tags = list(set(normalize_tags(existing_tags) + normalize_tags(scrape_data["tags"])))
                base_data["tags"] = json.dumps(merged_tags, ensure_ascii=False)

            # Check if translation is needed (contains non-ASCII)
            needs_trans = (
                not is_ascii(base_data.get("title", "")) or 
                not is_ascii(base_data.get("short_description", "")) or 
                not is_ascii(base_data.get("extended_description", ""))
            )
            if needs_trans:
                base_data["translation_priority"] = 1

            base_data["status"] = 200 # OK
            insert_or_update_item(self.db_path, base_data)
            
            # Step 3: Fetch User/Creator details
            creator_id = base_data.get("creator")
            if creator_id:
                try:
                    creator_id = int(creator_id)
                    # Check if we need to update user (missing or older than 7 days)
                    existing_user = get_user(self.db_path, creator_id)
                    should_update_user = True
                    if existing_user and existing_user.get("dt_updated"):
                        last_upd = datetime.fromisoformat(existing_user["dt_updated"])
                        if (datetime.now(timezone.utc) - last_upd).days < 7:
                            should_update_user = False
                    
                    if should_update_user:
                        user_summaries = get_player_summaries([creator_id], self.api_key)
                        if creator_id in user_summaries:
                            pdata = user_summaries[creator_id]
                            user_record = {
                                "steamid": creator_id,
                                "personaname": pdata.get("personaname"),
                                "dt_updated": datetime.now(timezone.utc).isoformat()
                            }
                            if not is_ascii(user_record["personaname"]):
                                user_record["translation_priority"] = 1
                            insert_or_update_user(self.db_path, user_record)
                except (ValueError, TypeError):
                    pass # Not a numeric SteamID

            # populated_fields = [k for k, v in base_data.items() if v is not None and v != ""]
            logging.info(f"[{item_id}] \"{base_data.get('title', 'Unknown Title')}\"") # | Populated: {populated_fields}")
            
            # Record Success and Adjust Delay
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            if self.consecutive_successes >= 5:
                self.had_recent_success_streak = True
            
            if self.consecutive_successes >= 100:
                old_delay = self.delay
                self.delay = max(1.0, self.delay - 0.1)
                if old_delay != self.delay:
                    logging.info(f"100 consecutive successes! Decreasing delay from {old_delay} to {self.delay} seconds.")
                    if "daemon" not in self.config:
                        self.config["daemon"] = {}
                    self.config["daemon"]["request_delay_seconds"] = self.delay
                    save_config(self.config_path, self.config)
                self.consecutive_successes = 0
            
            # Polite delay between items
            time.sleep(self.delay)

    def run(self):
        """Main loop that continuously queries and scrapes."""
        logging.info("Starting daemon loop...")
        self.translator.start()
        while self.running:
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
            app_tracking = get_app_tracking(self.db_path, appid)
            last_scanned_date = app_tracking["last_historical_date_scanned"] if app_tracking else 0
            saved_filter_text = app_tracking["filter_text"] if app_tracking else ""
            saved_required_tags = json.loads(app_tracking["required_tags"]) if app_tracking and app_tracking["required_tags"] else []
            saved_excluded_tags = json.loads(app_tracking["excluded_tags"]) if app_tracking and app_tracking["excluded_tags"] else []

            # Compare current filter with last used filter (stored in daemon instance)
            current_filter = {
                "text": saved_filter_text,
                "req_tags": sorted(saved_required_tags),
                "excl_tags": sorted(saved_excluded_tags)
            }
            current_filter_hash = json.dumps(current_filter, sort_keys=True)

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
                update_app_tracking(self.db_path, appid, start_time) # Also update DB to reflect new start
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
            window_size = 30 * 24 * 3600 # Start with a 30-day window

            # Continue looping while we're finding new items or haven't reached current time
            # And we haven't been explicitly told to stop
            while start_time < now and self.running:
                end_time = min(start_time + window_size, now)
                if end_time == start_time: # Avoid infinite loop if start_time catches up to now exactly
                    break

                logging.info(f"Querying window: {start_time} to {end_time} ({round((end_time-start_time)/86400, 1)} days)")

                window_new_count = 0
                page = 1
                while self.running:
                    found_items = discover_items_by_date_html(appid, start_time, end_time, page=page, search_text=saved_filter_text, required_tags=saved_required_tags, excluded_tags=saved_excluded_tags)

                    if not found_items:
                        break

                    page_new_count = 0
                    for item_id in found_items:
                        # discover_items_by_date_html now returns a list of IDs
                        if insert_or_update_item(self.db_path, {"workshop_id": item_id}):
                            page_new_count += 1

                    window_new_count += page_new_count
                    logging.info(f"Page {page} provided {page_new_count} new items.")

                    # If we got fewer than 30 items, it's likely the last page
                    if len(found_items) < 30:
                        break

                    # Safety limit for paging
                    if page >= 100: # 3000 items per window max
                        logging.warning("Hit safety limit of 100 pages for a single date window.")
                        break

                    page += 1
                    time.sleep(self.delay) # Be polite

                new_discovered_count += window_new_count
                logging.info(f"Window provided {window_new_count} new items. (Total new this seed: {new_discovered_count})")

                # Crucial: Full date range successfully scanned, update tracking
                # This means we've processed up to end_time for the current filter.
                # Only update last_historical_date_scanned here, filter is tracked separately.
                update_app_tracking(self.db_path, appid, end_time)

                # Move window forward
                start_time = end_time

                # Dynamic adjustment of next window size based on density
                if window_new_count == 0:
                    # Nothing found, aggressively widen window (max 1 year)
                    window_size = min(window_size * 4, 365 * 24 * 3600)
                elif window_new_count < 100:
                    window_size = min(window_size * 2, 365 * 24 * 3600)
                elif window_new_count > 500:
                    window_size = max(window_size // 2, 3600)

            # Interrupt if we've found enough new items
            if new_discovered_count >= target_new:
                logging.info(f"Target ({target_new}) of new items reached for AppID {appid}. Interrupting discovery.")
                break # Break out of the appid discovery loop

            logging.info(f"Finished discovery cycle for AppID {appid}. Added {new_discovered_count} new items.")
