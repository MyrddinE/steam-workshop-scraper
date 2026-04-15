import time
import signal
import json
import logging
from datetime import datetime, timezone
from src.database import get_next_items_to_scrape, insert_or_update_item
from src.steam_api import get_workshop_details_api
from src.web_scraper import scrape_extended_details

class Daemon:
    def __init__(self, config: dict):
        self.config = config
        self.running = True
        self.db_path = config["database"]["path"]
        self.api_key = config["api"]["key"]
        self.batch_size = config.get("daemon", {}).get("batch_size", 10)
        self.delay = config.get("daemon", {}).get("request_delay_seconds", 1.5)
        
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
            # If no items are pending, sleep a bit longer before checking again
            time.sleep(self.delay * 2)
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
            # Remove keys that don't match our schema
            if "publishedfileid" in api_data:
                del api_data["publishedfileid"]
            if "result" in api_data:
                del api_data["result"]
                
            base_data.update(api_data)
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
