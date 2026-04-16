import time
import signal
import json
import logging
from datetime import datetime, timezone
from src.database import get_next_items_to_scrape, insert_or_update_item
from src.steam_api import get_workshop_details_api, query_workshop_items
from src.web_scraper import scrape_extended_details, discover_ids_html

class Daemon:
    def __init__(self, config: dict):
        self.config = config
        self.running = True
        
        # Implement default fallbacks
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.api_key = config.get("api", {}).get("key", "")
        self.batch_size = config.get("daemon", {}).get("batch_size", 10)
        self.delay = config.get("daemon", {}).get("request_delay_seconds", 1.5)
        
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

    def process_batch(self):
        """Processes a single batch of workshop items."""
        items_to_scrape = get_next_items_to_scrape(self.db_path, limit=self.batch_size)
        
        if not items_to_scrape:
            logging.info("No items in database queue. Attempting to seed from Steam...")
            self.seed_database()
            # Try getting items again after seeding
            items_to_scrape = get_next_items_to_scrape(self.db_path, limit=self.batch_size)
            if not items_to_scrape:
                time.sleep(self.delay * 5)
                return

        for item_id in items_to_scrape:
            if not self.running:
                break # Exit early if shutting down

            logging.info(f"Processing item {item_id}...")
            
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

            # Remove keys that don't match our schema
            allowed_keys = {
                "workshop_id", "dt_found", "dt_updated", "dt_attempted", "status", "title",
                "creator", "creator_appid", "consumer_appid", "filename", "file_size", "preview_url",
                "hcontent_file", "hcontent_preview", "short_description", "time_created",
                "time_updated", "visibility", "banned", "ban_reason", "app_name", "file_type",
                "subscriptions", "favorited", "views", "tags", "extended_description", "language"
            }
            
            clean_api_data = {}
            known_ignored_keys = {"result"}
            
            for k, v in api_data.items():
                if k in allowed_keys:
                    clean_api_data[k] = v
                elif k not in known_ignored_keys:
                    logging.warning(f"Discarding unknown API column: '{k}' with value '{v}' for item {item_id}")
                
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
                
                # Simple merge of lists
                merged_tags = list(set(existing_tags + scrape_data["tags"]))
                base_data["tags"] = json.dumps(merged_tags, ensure_ascii=False)

            base_data["status"] = 200 # OK
            insert_or_update_item(self.db_path, base_data)
            
            # Polite delay between items
            time.sleep(self.delay)

    def run(self):
        """Main loop that continuously queries and scrapes."""
        logging.info("Starting daemon loop...")
        while self.running:
            self.process_batch()
        logging.info("Daemon gracefully exited.")

    def seed_database(self):
        """Fetches a list of item IDs for target appids to populate the database."""
        for appid in self.target_appids:
            logging.info(f"Seeding items for AppID {appid}...")
            
            # Try official API first
            new_ids = query_workshop_items(appid, self.api_key, count=100)
            
            # Fallback to HTML scraping if API returned nothing
            if not new_ids:
                logging.info(f"API discovery failed for AppID {appid}. Falling back to HTML scraping...")
                new_ids = discover_ids_html(appid)
            
            for wid in new_ids:
                insert_or_update_item(self.db_path, {"workshop_id": wid})
                
            logging.info(f"Seeded {len(new_ids)} items for AppID {appid}.")
