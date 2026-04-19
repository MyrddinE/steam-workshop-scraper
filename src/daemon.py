import time
import signal
import json
import logging
from datetime import datetime, timezone
from src.database import (
    get_next_items_to_scrape, 
    insert_or_update_item, 
    count_unscraped_items, 
    get_app_page, 
    update_app_page,
    insert_or_update_user,
    get_user,
    get_connection,
    get_app_tracking,
    update_app_tracking
)
from src.steam_api import get_workshop_details_api, query_workshop_items, get_player_summaries, query_files_by_date
from src.web_scraper import scrape_extended_details, discover_ids_html
from src.translator import TranslatorThread, is_ascii

class Daemon:
    def __init__(self, config: dict):
        self.config = config
        self.running = True
        
        # Implement default fallbacks
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.api_key = config.get("api", {}).get("key", "")
        self.batch_size = config.get("daemon", {}).get("batch_size", 10)
        self.delay = config.get("daemon", {}).get("request_delay_seconds", 1.5)
        
        # Translator thread
        self.translator = TranslatorThread(config)
        
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
                    logging.info(f"User {sid} not found in API, inserted placeholder.")

    def process_batch(self):
        """Processes a single batch of workshop items."""
        # Proactive user discovery check
        self.expand_user_discovery()
        
        # Seeding check: If we have fewer than 100 unscraped items, fetch the next page
        unscraped = count_unscraped_items(self.db_path)
        if unscraped < 100:
            logging.info(f"Low unscraped queue ({unscraped}). Expanding discovery...")
            self.seed_database()

        items_to_scrape = get_next_items_to_scrape(self.db_path, limit=self.batch_size)
        
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
            if not api_data:
                base_data["status"] = 500
                insert_or_update_item(self.db_path, base_data)
                logging.warning(f"[{item_id}] Failed to fetch from Steam API.")
                time.sleep(self.delay)
                continue

            # Merge API data
            # Map keys that differ from our schema
            if "publishedfileid" in api_data:
                api_data["workshop_id"] = int(api_data.pop("publishedfileid"))
            if "creator_app_id" in api_data:
                api_data["creator_appid"] = api_data.pop("creator_app_id")
            if "consumer_app_id" in api_data:
                api_data["consumer_appid"] = api_data.pop("consumer_app_id")
            if "description" in api_data:
                api_data["short_description"] = api_data.pop("description")

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
            known_ignored_keys = {"result"}
            
            for k, v in api_data.items():
                if k in allowed_keys:
                    clean_api_data[k] = v
                elif k not in known_ignored_keys:
                    val_preview = str(v)[:20] + "..." if len(str(v)) > 20 else str(v)
                    if v is not None and str(v).strip() != "":
                        logging.info(f"Discarding unknown API column: '{k}' with value '{val_preview}' for item {item_id}")
                    else:
                        logging.debug(f"Discarding unknown (empty) API column: '{k}' for item {item_id}")
                
            base_data.update(clean_api_data)
            base_data["dt_updated"] = now_iso

            # Check if tags were provided as list, JSON stringify for SQLite
            if "tags" in base_data and isinstance(base_data["tags"], list):
                base_data["tags"] = json.dumps(base_data["tags"], ensure_ascii=False)

            # Step 2: Scrape Extended Details
            url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
            scrape_data = scrape_extended_details(url)
            
            if not scrape_data:
                base_data["status"] = 206 # Partial Content
                insert_or_update_item(self.db_path, base_data)
                
                # Assemble detailed log message
                successful_fields = [k for k, v in base_data.items() if v is not None]
                failed_fields = ["extended_description", "tags"] # Known scrape targets
                logging.warning(
                    f"[{item_id}] '{base_data.get('title', 'Unknown')}' | Scraper failed, partial data saved. "
                    f"Successfully pulled from API: {successful_fields}. "
                    f"Failed to scrape from web: {failed_fields}."
                )
                
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

            populated_fields = [k for k, v in base_data.items() if v is not None and v != ""]
            logging.info(f"[{item_id}] \"{base_data.get('title', 'Unknown Title')}\" | Populated: {populated_fields}")
            
            # Polite delay between items
            time.sleep(self.delay)

    def run(self):
        """Main loop that continuously queries and scrapes."""
        logging.info("Starting daemon loop...")
        self.translator.start()
        while self.running:
            self.process_batch()
        logging.info("Daemon gracefully exited.")

    def _find_initial_start_date(self, appid: int) -> int:
        """
        Performs a binary search to find the Unix timestamp of the oldest 
        date range containing workshop items for the appid.
        Returns a timestamp just before the first items appear.
        """
        logging.info(f"AppID {appid} has no tracking history. Performing binary search to find the very first mod release...")
        
        low = 1063324800 # ~September 12, 2003 (Steam Launch)
        now = int(time.time())
        high = now
        
        best_found_start = now - (10 * 365 * 24 * 3600) # Default fallback
        
        while high - low > 86400: # 1-day resolution
            mid = low + (high - low) // 2
            logging.info(f"Binary search checking range up to {mid} ({datetime.fromtimestamp(mid, timezone.utc).date()})...")
            
            result = query_files_by_date(appid, low, mid, self.api_key, page=1)
            
            if result.get("total", 0) > 0:
                # Items found in this left half. The first item is somewhere in here.
                # So we move our upper bound down to mid.
                high = mid
                best_found_start = mid # Keep track in case we abort early
            else:
                # No items found in the left half. The first item must be after mid.
                # So we move our lower bound up to mid.
                low = mid
                
            time.sleep(1) # Be polite to API during search
            
        # Move back exactly one window size just to be safe, but no earlier than Steam Launch
        final_start = max(1063324800, low - 86400)
        logging.info(f"Binary search complete. First items appeared around {datetime.fromtimestamp(final_start, timezone.utc).date()}.")
        return final_start

    def seed_database(self, target_new: int = 100):
        """
        Historical forward scraping strategy. Uses IPublishedFileService/QueryFiles
        to find items within dynamic date ranges.
        """
        now = int(time.time())
        
        for appid in self.target_appids:
            last_scanned = get_app_tracking(self.db_path, appid)
            
            if last_scanned:
                start_time = last_scanned
            else:
                start_time = self._find_initial_start_date(appid)
            
            # If we are within 24h of present, do nothing (wait for daily refresh)
            if now - start_time < 86400:
                logging.info(f"AppID {appid} is up to date (last scanned within 24h). Skipping discovery.")
                continue
                
            logging.info(f"Discovering items for AppID {appid} starting from timestamp {start_time}...")
            
            # Start with a wide window, e.g., 30 days
            window_size = 30 * 24 * 3600 
            
            new_discovered_count = 0
            
            while new_discovered_count < target_new and start_time < now:
                end_time = min(start_time + window_size, now)
                
                logging.info(f"Querying window: {start_time} to {end_time} ({round((end_time-start_time)/86400, 1)} days)")
                
                # Fetch page 1 to check total results
                result = query_files_by_date(appid, start_time, end_time, self.api_key, page=1)
                total_items = result["total"]
                
                if total_items > 0:
                    pages_needed = (total_items + 99) // 100
                else:
                    pages_needed = 0
                
                # Max page threshold check (aim for < 450 pages to be safe from 500 limit)
                # If we exceed, abort this window and narrow it.
                if pages_needed > 450:
                    logging.warning(f"Window returned {pages_needed} pages (exceeds 450 limit). Narrowing window.")
                    # Halve the window size and retry this exact same start_time
                    window_size = max(window_size // 2, 3600) # Don't go smaller than 1 hour
                    continue
                
                # Process the pages
                page_new_count = 0
                if total_items > 0:
                    for page in range(1, pages_needed + 1):
                        if page > 1:
                            # We already have page 1 from the threshold check
                            result = query_files_by_date(appid, start_time, end_time, self.api_key, page=page)
                            
                        for item in result["items"]:
                            wid = int(item["publishedfileid"])
                            if insert_or_update_item(self.db_path, {"workshop_id": wid}):
                                page_new_count += 1
                                
                        time.sleep(0.5) # Polite delay between pages
                
                new_discovered_count += page_new_count
                logging.info(f"Window provided {page_new_count} new items. (Total new this seed: {new_discovered_count})")
                
                # Crucial: Full date range successfully scanned, update tracking
                update_app_tracking(self.db_path, appid, end_time)
                
                # Move window forward
                start_time = end_time
                
                # Dynamic adjustment of next window size based on density
                # Target: ~10 pages (1000 items) per window.
                if pages_needed == 0:
                    # Nothing found, aggressively widen window (max 1 year)
                    window_size = min(window_size * 4, 365 * 24 * 3600)
                elif pages_needed < 5:
                    window_size = min(window_size * 2, 365 * 24 * 3600)
                elif pages_needed > 20:
                    window_size = max(window_size // 2, 3600)
            
            logging.info(f"Finished discovery cycle for AppID {appid}. Added {new_discovered_count} new items.")
