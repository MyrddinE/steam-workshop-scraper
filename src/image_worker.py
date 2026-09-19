"""Background thread for downloading Steam Workshop preview images."""

import time
import os
import logging
import threading
import requests
from datetime import datetime, timezone
from src import capture, images, pacing
from src.database import get_next_image_item, insert_or_update_item, get_image_path

# Re-exported so this module still reads as the place the downloader's formats
# are defined; the definitions live in src/images.py because the reader (the URL
# guard) must agree with the writer about what an image extension is.
from src.images import MIME_MAP, MAGIC_EXT_MAP  # noqa: F401

# The slowest the decay rule will take this worker. The image queue is the one
# that has to keep up with the library, and a preview is a single small file, so
# it can probe far harder than the web scraper's shared Steam budget allows.
IMAGE_DELAY_FLOOR = 0.5


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


class ImageDownloadThread(threading.Thread):
    def __init__(self, db_path: str, pause_lock_file: str, daemon_config: dict = None, save_callback = None):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file
        self._save_callback = save_callback
        self.running = True
        self.image_delay = float((daemon_config or {}).get("image_delay_seconds") or 2.0)
        self.image_successes = 0
        self.image_failures = 0
        self.image_had_success_streak = False
        # The decay is measured in time, from a clock that exists only in this
        # process: a daemon restarted after a day must resume where it left off,
        # not treat the day as healthy operation.
        self._clock = pacing.Clock()
        self._persisted_image_delay = self.image_delay

    def run(self):
        logging.info("Image download thread started.")
        os.makedirs("images", exist_ok=True)

        while self.running:
            while os.path.exists(self.pause_lock_file) and self.running:
                time.sleep(1)

            item = get_next_image_item(self.db_path)
            if not item:
                # Responsive, so an idle worker is not deaf to a stop for the
                # whole nap. The blind ten-second sleep this replaced was why a
                # shutdown with nothing queued had to wait out the daemon's join
                # timeout: the stop flag was set, and the worker would not look
                # at it until the sleep ended.
                pacing.wait(10.0, lambda: self.running)
                continue

            wid = item["workshop_id"]
            url = item.get("preview_url")
            if not url:
                # No URL — clear the flag
                conn = self._get_conn()
                conn.execute("UPDATE workshop_items SET image_priority=0 WHERE workshop_id=?", (wid,))
                conn.commit()
                conn.close()
                continue

            # The response, when there was one, is the evidence a capture needs.
            # None distinguishes a transport failure from a refused status.
            resp = None
            # One interval per attempt, measured from the previous attempt to
            # this one and advanced whether or not this one succeeds. If a
            # failure left the interval running, the first success afterwards
            # would read the whole run as elapsed time and collapse the delay in
            # a single step -- the opposite of backing off.
            elapsed = self._clock.since()
            try:
                resp = requests.get(url, allow_redirects=True, timeout=15, stream=True)
                if resp.status_code != 200:
                    raise Exception(f"HTTP {resp.status_code}")

                content_type = resp.headers.get("Content-Type", "")
                mime = content_type.split(";")[0].strip().lower()
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
                            logging.debug(f"[I:{wid}] Puremagic detected {magic_ext} → .{ext}")
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
                    conn.execute(
                        "UPDATE workshop_items SET image_priority=0, image_answer=? WHERE workshop_id=?",
                        (images.served_type_marker(content_type), wid))
                    conn.commit()
                    conn.close()
                    time.sleep(self.image_delay)
                    continue

                img_path = get_image_path("images", wid, ext)
                os.makedirs(os.path.dirname(img_path), exist_ok=True)
                bytes_written = 0
                with open(img_path, "wb") as image_file:
                    if magic_header:
                        image_file.write(magic_header)
                        bytes_written += len(magic_header)
                    for chunk in resp.iter_content(8192):
                        image_file.write(chunk)
                        bytes_written += len(chunk)

                insert_or_update_item(self.db_path, {
                    "workshop_id": wid,
                    "image_answer": ext,
                    "image_priority": 0,
                    # Our clock, taken now that the bytes are on disk. Not a
                    # Steam value: the API supplies no image time, and the
                    # completion timestamp is what a throughput is measured
                    # from. Only this success path writes it -- a 404, an
                    # unclassifiable content type and a transport failure all
                    # leave it alone.
                    "image_fetched_at": int(time.time()),
                })

                # Metadata only, and only under the debug switch: the image bytes
                # are the file just written, never a second copy in the outbox.
                capture.record_image_download(
                    wid, url, True, **_response_metadata(resp),
                    bytes_written=bytes_written, saved_path=img_path)

                title = item.get("title_en") or item.get("title") or str(wid)
                logging.info(f"[I:{wid}] Downloaded preview ({ext}) for \"{title}\"")
                self.image_successes += 1
                self.image_failures = 0
                if self.image_successes >= 5:
                    self.image_had_success_streak = True
                self._decay_delay(elapsed)

            except Exception as e:
                logging.warning(f"[I:{wid}] Image download failed: {e}")
                # Failures are always captured when the outbox is on: the status
                # and headers of the response, or the exception text when there
                # was none. This is what lets the owner review the 404 loop.
                capture.record_image_download(
                    wid, url, False, **_response_metadata(resp),
                    error=str(e), error_type=type(e).__name__)

                status = resp.status_code if resp is not None else None
                marker = images.permanent_status_marker(status)
                conn = self._get_conn()
                if marker is not None:
                    # The server answered that the picture is not there, and
                    # asking again cannot change a fact about the item. Record
                    # the answer in image_answer, which is what BOTH the
                    # re-fetch gate and the URL guard read, and take the item out
                    # of the queue.
                    #
                    # api_priority is deliberately *not* raised. That raise asked
                    # for an API refresh, the refresh re-flagged the image, and
                    # the download 404'd again -- the cycle that fetched item
                    # 3731736934 twenty-five times in one day for a preview that
                    # never existed.
                    conn.execute(
                        "UPDATE workshop_items SET image_answer=?, image_priority=0 "
                        "WHERE workshop_id=?", (marker, wid))
                    conn.commit()
                    conn.close()
                    # A content answer says nothing about our request rate, so it
                    # is neutral for pacing: it does not grow the delay (404s
                    # alone had pinned this thread at its ceiling) and it does not
                    # break a run of successes either.
                else:
                    new_pri = max(0, (item.get("image_priority") or 1) - 1)
                    conn.execute(
                        "UPDATE workshop_items SET image_priority=?, api_priority = CASE WHEN api_priority < 2 THEN 2 ELSE api_priority END WHERE workshop_id=?",
                        (new_pri, wid)
                    )
                    conn.commit()
                    conn.close()
                    self.image_failures += 1
                    self.image_successes = 0
                    if self.image_failures >= 2 and self.image_had_success_streak:
                        old = self.image_delay
                        self.image_delay = pacing.backoff(self.image_delay)
                        logging.info(f"Multiple consecutive image failures! Increasing delay from {old} to {self.image_delay}s.")
                        # Always written: a restart during an outage must not
                        # resume at the pace that was just refused.
                        self._persist_delay(force=True)
                        self.image_had_success_streak = False

            # Responsive, so a long backoff cannot make the worker deaf to a
            # stop or a pause. It serves the delay in full; it does not shorten it.
            pacing.wait(self.image_delay, lambda: self.running)

        self._persist_delay(force=True)
        # No "Image download thread stopped." here: the daemon logs one line per
        # worker as it confirms the join, and this thread's own copy made the
        # owner's log show the same sentence twice. See Daemon._join_workers.

    def _decay_delay(self, elapsed: float) -> None:
        """Shrink the delay for the healthy time since the previous attempt.

        One healthy attempt is worth the wall-clock time it occupied, so the
        delay halves over `pacing.HALF_LIFE_SECONDS` however large it is -- the
        property a success-counted rule cannot have, since a count means a
        different amount of time at every delay.
        """
        old = self.image_delay
        self.image_delay = pacing.decay(self.image_delay, elapsed, IMAGE_DELAY_FLOOR)
        if old != self.image_delay:
            self._persist_delay()

    def _persist_delay(self, force: bool = False) -> None:
        """Write the delay back when it has moved far enough to be worth it.

        Without a step the decay -- which now runs on every success -- would
        rewrite config.yaml per download.
        """
        if not self._save_callback:
            return
        if not force and not pacing.needs_persist(
                self.image_delay, self._persisted_image_delay):
            return
        self._persisted_image_delay = self.image_delay
        self._save_callback("image_delay_seconds", pacing.persistable(self.image_delay))

    def _get_conn(self):
        from src.database import get_connection
        return get_connection(self.db_path)
