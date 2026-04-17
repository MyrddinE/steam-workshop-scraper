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
    get_user
)
from src.steam_api import get_workshop_details_api, query_workshop_items, get_player_summaries
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
        logging.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.running = False
        self.translator.running = False

    def process_batch(self):
        """Processes a single batch of workshop items."""
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
                        logging.debug(f"Discarding unknown API column: '{k}' with value '{val_preview}' for item {item_id}")
                
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
                logging.warning(f"[{item_id}] '{base_data.get('title', 'Unknown')}' | Scraper failed, partial data saved.")
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

    def seed_database(self, target_new: int = 100):
        """
        Fetches pages of item IDs for target appids until we have added target_new 
        actually new items to the database, or we run out of results.
        """
        for appid in self.target_appids:
            new_discovered_count = 0
            consecutive_empty_pages = 0
            
            while new_discovered_count < target_new and consecutive_empty_pages < 5:
                page = get_app_page(self.db_path, appid)
                logging.info(f"Discovering items for AppID {appid} (Page {page})...")
                
                # Try official API first
                new_ids = query_workshop_items(appid, self.api_key, count=100, page=page)
                
                # Fallback to HTML scraping if API returned nothing
                if not new_ids:
                    logging.info(f"API discovery failed for AppID {appid}. Falling back to HTML scraping...")
                    new_ids = discover_ids_html(appid, page=page)
                
                if new_ids:
                    consecutive_empty_pages = 0
                    page_new_count = 0
                    for wid in new_ids:
                        if insert_or_update_item(self.db_path, {"workshop_id": wid}):
                            page_new_count += 1
                    
                    new_discovered_count += page_new_count
                    update_app_page(self.db_path, appid, page + 1)
                    logging.info(f"Page {page} for AppID {appid} provided {page_new_count} new items. (Total new this seed: {new_discovered_count})")
                    
                    if page_new_count == 0:
                        # If a whole page of 100 items had nothing new, we're likely deep in already-scraped territory
                        consecutive_empty_pages += 1
                else:
                    logging.warning(f"No items found for AppID {appid} on page {page}. Ending discovery for this app.")
                    break
            
            logging.info(f"Finished discovery for AppID {appid}. Added {new_discovered_count} new items.")
