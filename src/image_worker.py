"""Background thread for downloading Steam Workshop preview images."""

import time
import os
import logging
import threading
import requests
from datetime import datetime, timezone
from src.database import get_next_image_item, insert_or_update_item


MIME_MAP = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/jpg": "jpg",
}


class ImageScraperThread(threading.Thread):
    def __init__(self, db_path: str, pause_lock_file: str, daemon_config: dict = None, save_callback = None):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file
        self._save_cb = save_callback
        self.running = True
        self.image_delay = float((daemon_config or {}).get("image_delay_seconds") or 2.0)
        self.image_successes = 0
        self.image_failures = 0
        self.image_had_streak = False

    def run(self):
        logging.info("Image download thread started.")
        os.makedirs("images", exist_ok=True)

        while self.running:
            while os.path.exists(self.pause_lock_file) and self.running:
                time.sleep(1)

            item = get_next_image_item(self.db_path)
            if not item:
                time.sleep(10)
                continue

            wid = item["workshop_id"]
            url = item.get("preview_url")
            if not url:
                # No URL — clear the flag
                conn = self._get_conn()
                conn.execute("UPDATE workshop_items SET needs_image=0 WHERE workshop_id=?", (wid,))
                conn.commit()
                conn.close()
                continue

            try:
                resp = requests.get(url, allow_redirects=True, timeout=15, stream=True)
                if resp.status_code != 200:
                    raise Exception(f"HTTP {resp.status_code}")

                ct = resp.headers.get("Content-Type", "")
                mime = ct.split(";")[0].strip().lower()
                ext = MIME_MAP.get(mime, "")

                if not ext:
                    raise Exception(f"Unknown MIME type: {mime}")

                img_path = os.path.join("images", f"{wid}.{ext}")
                with open(img_path, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)

                now_iso = datetime.now(timezone.utc).isoformat()
                insert_or_update_item(self.db_path, {
                    "workshop_id": wid,
                    "image_extension": ext,
                    "needs_image": 0,
                    "dt_attempted": now_iso,
                })

                title = item.get("title_en") or item.get("title") or str(wid)
                logging.info(f"[I:{wid}] Downloaded preview ({ext}) for \"{title}\"")
                self._notify("image", {"workshop_id": wid, "ext": ext})
                self.image_successes += 1
                self.image_failures = 0
                if self.image_successes >= 5:
                    self.image_had_streak = True
                if self.image_successes >= 100:
                    old = self.image_delay
                    self.image_delay = max(0.5, round(self.image_delay / 1.05, 3))
                    if old != self.image_delay:
                        logging.info(f"100 consecutive image successes! Decreasing delay from {old} to {self.image_delay}s.")
                        self._save_cb("image_delay_seconds", self.image_delay)
                    self.image_successes = 0

            except Exception as e:
                logging.warning(f"[I:{wid}] Image download failed: {e}")
                new_pri = max(0, (item.get("needs_image") or 1) - 1)
                conn = self._get_conn()
                conn.execute("UPDATE workshop_items SET needs_image=? WHERE workshop_id=?", (new_pri, wid))
                conn.commit()
                conn.close()
                self.image_failures += 1
                self.image_successes = 0
                if self.image_failures >= 2 and self.image_had_streak:
                    old = self.image_delay
                    self.image_delay = round(self.image_delay * (1.05 ** 10), 3)
                    logging.info(f"Multiple consecutive image failures! Increasing delay from {old} to {self.image_delay}s.")
                    self._save_cb("image_delay_seconds", self.image_delay)
                    self.image_had_streak = False

            time.sleep(self.image_delay)

        self._save_cb("image_delay_seconds", self.image_delay)
        logging.info("Image download thread stopped.")

    def _notify(self, event_type, data):
        try:
            from src.webserver import _notify_web_clients
            _notify_web_clients(event_type, data)
        except Exception:
            pass

    def _get_conn(self):
        from src.database import get_connection
        return get_connection(self.db_path)
