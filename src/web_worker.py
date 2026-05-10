"""Background thread for web scraping Steam Workshop pages."""

import time
import os
import logging
import threading
from datetime import datetime, timezone
from src.database import get_next_web_scrape_item, insert_or_update_item, get_connection, flag_field_for_translation
from src.web_scraper import scrape_extended_details
from src.translator import is_ascii


class WebScraperThread(threading.Thread):
    def __init__(self, db_path: str, pause_lock_file: str, daemon_config: dict = None, save_callback = None):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file
        self._save_cb = save_callback
        self.running = True
        self.web_delay = float((daemon_config or {}).get("web_delay_seconds") or 5.0)
        self.web_successes = 0
        self.web_failures = 0
        self.web_had_streak = False

    def run(self):
        logging.info("Web scraper thread started.")
        while self.running:
            # Only the web thread pauses when TUI locks
            while os.path.exists(self.pause_lock_file) and self.running:
                time.sleep(1)

            item = get_next_web_scrape_item(self.db_path)
            if not item:
                time.sleep(10)
                continue

            workshop_id = item["workshop_id"]
            url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
            scrape_data = scrape_extended_details(url)

            if scrape_data:
                now_iso = datetime.now(timezone.utc).isoformat()
                update = {
                    "workshop_id": workshop_id,
                    "extended_description": scrape_data.get("description"),
                    "needs_web_scrape": 0,
                    "dt_attempted": now_iso,
                }
                insert_or_update_item(self.db_path, update)

                # Flag extended description for translation
                desc = scrape_data.get("description") or ""
                if desc and not is_ascii(desc):
                    flag_field_for_translation(self.db_path, "item", workshop_id, "extended_description_en", desc, 3)

                display = item.get("title_en") or item.get("title") or str(workshop_id)
                logging.info(f"[W:{workshop_id}] Scraped \"{display}\"")
                self._notify("web_scrape", {"workshop_id": workshop_id})
                self.web_successes += 1
                self.web_failures = 0
                if self.web_successes >= 5:
                    self.web_had_streak = True
                if self.web_successes >= 100:
                    old = self.web_delay
                    self.web_delay = max(1.0, round(self.web_delay / 1.05, 3))
                    if old != self.web_delay:
                        logging.info(f"100 consecutive web successes! Decreasing web delay from {old} to {self.web_delay}s.")
                        self._save_cb("web_delay_seconds", self.web_delay)
                    self.web_successes = 0
                time.sleep(self.web_delay)
            else:
                self.web_failures += 1
                self.web_successes = 0
                if self.web_failures >= 2 and self.web_had_streak:
                    old = self.web_delay
                    self.web_delay = round(self.web_delay * (1.05 ** 10), 3)
                    logging.info(f"Multiple consecutive web scrape failures! Increasing web delay from {old} to {self.web_delay}s.")
                    self._save_cb("web_delay_seconds", self.web_delay)
                    self.web_had_streak = False
                time.sleep(self.web_delay)

        self._save_cb("web_delay_seconds", self.web_delay)
        logging.info("Web scraper thread stopped.")

    def _notify(self, event_type, data):
        try:
            from src.webserver import _notify_web_clients
            _notify_web_clients(event_type, data)
        except Exception:
            pass
