"""Background thread for downloading Steam Workshop preview images."""

import time
import os
import logging
import threading
import requests
from datetime import datetime, timezone
from src import capture
from src.database import get_next_image_item, insert_or_update_item, get_image_path


MIME_MAP = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/jpg": "jpg",
}

MAGIC_EXT_MAP = {
    ".jpeg": "jpg",
    ".jpg": "jpg",
    ".jfif": "jpg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
    ".bmp": "bmp",
}


def _response_metadata(resp) -> dict:
    """The response facts a capture records. Never includes the body.

    ``resp`` is ``None`` for a transport failure, so the caller still gets a
    complete keyword set and the clear record that there was no response.
    """
    if resp is None:
        return {"http_status": None, "final_url": None, "headers": None,
                "content_type": None, "content_length": None}
    headers = dict(resp.headers)
    return {
        "http_status": resp.status_code,
        # Redirects are allowed, so the URL actually downloaded from is worth
        # recording alongside the preview_url that was requested.
        "final_url": resp.url,
        "headers": headers,
        "content_type": headers.get("Content-Type"),
        "content_length": headers.get("Content-Length"),
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

            # The response, when there was one, is the evidence a capture needs.
            # None distinguishes a transport failure from a refused status.
            resp = None
            try:
                resp = requests.get(url, allow_redirects=True, timeout=15, stream=True)
                if resp.status_code != 200:
                    raise Exception(f"HTTP {resp.status_code}")

                ct = resp.headers.get("Content-Type", "")
                mime = ct.split(";")[0].strip().lower()
                ext = MIME_MAP.get(mime, "")
                magic_header = b""

                if not ext:
                    magic_ext = ""
                    try:
                        import puremagic
                        magic_header = next(resp.iter_content(8192), b"")
                        magic_ext = puremagic.from_string(magic_header)
                        ext = MAGIC_EXT_MAP.get(magic_ext, "")
                        if ext:
                            logging.info(f"[I:{wid}] Puremagic detected {magic_ext} → .{ext}")
                    except Exception:
                        logging.debug("[I:%s] Puremagic detection failed, falling back to URL extension", wid)
                        pass

                if not ext:
                    logging.warning(f"[I:{wid}] Unknown MIME: {mime}, puremagic guess: {magic_ext or 'none'}")
                    # A served response we cannot classify is still a failure
                    # with evidence worth keeping: status and headers explain it.
                    capture.record_image_download(
                        wid, url, False, **_response_metadata(resp),
                        error=f"unknown MIME type {mime!r}",
                        error_type="UnknownMimeType")
                    conn = self._get_conn()
                    conn.execute("UPDATE workshop_items SET needs_image=0 WHERE workshop_id=?", (wid,))
                    conn.commit()
                    conn.close()
                    time.sleep(self.image_delay)
                    continue

                img_path = get_image_path("images", wid, ext)
                os.makedirs(os.path.dirname(img_path), exist_ok=True)
                written = 0
                with open(img_path, "wb") as f:
                    if magic_header:
                        f.write(magic_header)
                        written += len(magic_header)
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
                        written += len(chunk)

                insert_or_update_item(self.db_path, {
                    "workshop_id": wid,
                    "image_extension": ext,
                    "needs_image": 0,
                    "scrape_version": item.get("steam_updated_at", 0),
                })

                # Metadata only, and only under the debug switch: the image bytes
                # are the file just written, never a second copy in the outbox.
                capture.record_image_download(
                    wid, url, True, **_response_metadata(resp),
                    bytes_written=written, saved_path=img_path)

                title = item.get("title_en") or item.get("title") or str(wid)
                logging.info(f"[I:{wid}] Downloaded preview ({ext}) for \"{title}\"")
                self.image_successes += 1
                self.image_failures = 0
                if self.image_successes >= 5:
                    self.image_had_streak = True
                if self.image_successes >= 100:
                    old = self.image_delay
                    self.image_delay = max(0.5, round(self.image_delay / 1.05, 3))
                    if old != self.image_delay:
                        logging.info(f"100 consecutive image successes! Decreasing delay from {old} to {self.image_delay}s.")
                        if self._save_cb:
                            self._save_cb("image_delay_seconds", self.image_delay)
                    self.image_successes = 0

            except Exception as e:
                logging.warning(f"[I:{wid}] Image download failed: {e}")
                # Failures are always captured when the outbox is on: the status
                # and headers of the response, or the exception text when there
                # was none. This is what lets the owner review the 404 loop.
                capture.record_image_download(
                    wid, url, False, **_response_metadata(resp),
                    error=str(e), error_type=type(e).__name__)
                new_pri = max(0, (item.get("needs_image") or 1) - 1)
                conn = self._get_conn()
                conn.execute(
                    "UPDATE workshop_items SET needs_image=?, api_priority = CASE WHEN api_priority < 2 THEN 2 ELSE api_priority END WHERE workshop_id=?",
                    (new_pri, wid)
                )
                conn.commit()
                conn.close()
                self.image_failures += 1
                self.image_successes = 0
                if self.image_failures >= 2 and self.image_had_streak:
                    old = self.image_delay
                    self.image_delay = min(round(self.image_delay * (1.05 ** 10), 3),20)
                    logging.info(f"Multiple consecutive image failures! Increasing delay from {old} to {self.image_delay}s.")
                    if self._save_cb:
                        self._save_cb("image_delay_seconds", self.image_delay)
                    self.image_had_streak = False

            time.sleep(self.image_delay)

        if self._save_cb:
            self._save_cb("image_delay_seconds", self.image_delay)
        logging.info("Image download thread stopped.")

    def _get_conn(self):
        from src.database import get_connection
        return get_connection(self.db_path)
