import time
import json
import logging
import threading
from datetime import datetime, timezone
from openai import OpenAI
from src.database import get_connection, get_next_batch_for_translation

logging.getLogger("httpx").setLevel(logging.WARNING)

# Backoff for a batch that fails. Growing the delay is what stops a persistent
# condition becoming a tight loop against the API, which is what happened before:
# the same batch was re-sent every ~1.2 s indefinitely.
#
# There are two shapes because the failures differ in kind. A transport error or
# a rate limit can clear on its own, so it starts low and caps at minutes. An
# account-level rejection -- credentials, billing, permissions -- cannot be fixed
# by waiting, so it starts at a minute and caps at an hour: a raised spend limit
# is then picked up without a restart, and the log stays quiet meanwhile.
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 300.0
ACCOUNT_BASE_SECONDS = 60.0
ACCOUNT_MAX_SECONDS = 3600.0

# Statuses meaning the account or key must change before another attempt can
# succeed. Everything else -- rate limits, server errors, connection failures --
# is worth retrying.
ACCOUNT_LEVEL_STATUSES = frozenset({401, 402, 403})

# The streak is persisted, so it is read back from a file that a crash, an
# editor or a bad disk can leave nonsense in. The exponent has to be bounded
# *before* it is used, because `2 ** (streak - 1)` on a value of a billion would
# build that integer before `min()` ever saw it. 2**63 seconds is already far
# beyond every cap here, so clamping costs nothing.
MAX_FAILURE_STREAK = 64

# The section this thread owns in the daemon state file. See src/daemon_state.py.
STATE_SECTION = "translation_backoff"


def backoff_delay(streak: int, retryable: bool) -> float:
    """Seconds to wait before the next attempt, after ``streak`` failures.

    Pure so the shape of the backoff can be tested without a thread, a clock or
    a network, and so the value that gets persisted comes from one place.
    """
    base = RETRY_BASE_SECONDS if retryable else ACCOUNT_BASE_SECONDS
    cap = RETRY_MAX_SECONDS if retryable else ACCOUNT_MAX_SECONDS
    streak = max(1, min(int(streak), MAX_FAILURE_STREAK))
    return float(min(base * (2 ** (streak - 1)), cap))


def _coerce_utc(value):
    """A timezone-aware UTC datetime from a stored value, or ``None``.

    Accepts a string or a datetime, because PyYAML resolves an ISO timestamp to
    a ``datetime`` on the way back in: both shapes arrive here from one file.
    Naive values are read as UTC rather than rejected.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_streak(value) -> int:
    """A usable streak from a stored value: 0 when unusable, never out of range."""
    try:
        streak = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(streak, MAX_FAILURE_STREAK))


def remaining_backoff(next_attempt_at, now: float, cap: float) -> float:
    """Seconds still to wait for a persisted ``next_attempt_at``.

    Zero when the moment has passed, when the value is unusable, or when it is
    further away than ``cap``: no legitimate backoff is longer than the cap, so
    a timestamp beyond it means a bad clock or an edited file, and honouring it
    would park the thread for an unexplained age.
    """
    when = _coerce_utc(next_attempt_at)
    if when is None:
        return 0.0
    return max(0.0, min(cap, when.timestamp() - now))


def retryable_failure(exc: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    Transport failures carry no status code and are retryable. Authentication,
    billing and permission rejections are not: the account or the key has to
    change first, and retrying only adds noise.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        return True
    return status not in ACCOUNT_LEVEL_STATUSES


def _validate_openai_api_key(config: dict) -> str | None:
    openai_config = config.get("openai", {})
    api_key = openai_config.get("api_key")
    if api_key and "YOUR_OPENAI_API_KEY" not in api_key:
        return api_key
    return None


def _create_openai_client(openai_config: dict) -> OpenAI:
    return OpenAI(
        api_key=openai_config.get("api_key"),
        base_url=openai_config.get("endpoint", "https://api.openai.com/v1")
    )


def is_ascii(s: str) -> bool:
    if not s:
        return True
    return all(ord(c) < 128 for c in s)


class TranslatorThread(threading.Thread):
    def __init__(self, config: dict, state_store=None):
        super().__init__(daemon=True)
        self.config = config
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.batch_size = config.get("openai", {}).get("batch", 20)
        self.running = True
        self._failure_streak = 0
        # Injected, and optional, so the thread stays constructible and testable
        # without a filesystem. The daemon passes a StateStore so a backoff
        # outlives a restart; with none, behaviour is exactly as it was.
        self.state_store = state_store

    def run(self):
        openai_config = self.config.get("openai", {})
        if not _validate_openai_api_key(self.config):
            logging.warning("OpenAI API key not configured. Translation thread exiting.")
            return

        client = _create_openai_client(openai_config)
        model = openai_config.get("model", "gpt-4o-mini")
        logging.info("Starting batched translation background thread...")

        # A backoff that was still running when the daemon stopped is resumed,
        # not restarted: the condition it was waiting on did not change just
        # because the process did.
        self._resume_persisted_backoff()

        while self.running:
            try:
                batch = get_next_batch_for_translation(self.db_path, limit=self.batch_size)
            except Exception as e:
                logging.error(f"Translator thread error: {e}")
                self._sleep(30)
                continue

            if not batch:
                self._sleep(30)
                continue

            urgent = any(row.get("priority", 0) >= 5 for row in batch)
            if len(batch) >= self.batch_size or urgent:
                # A failed batch keeps its translation_queue rows, so backing off
                # costs nothing but time and the work is picked up again later.
                try:
                    self._translate_batch(batch, client, model)
                except Exception as e:
                    self._sleep(self._register_failure(e))
                    continue
                self._register_success()
                self._sleep(1)
            else:
                self._sleep(30)

        # Thread lifecycle logging handled by daemon

    def _sleep(self, seconds: float) -> None:
        """Sleep, but stay responsive to shutdown.

        A backoff can run to an hour, and one long sleep would hold the daemon's
        graceful shutdown for as long as it had left to run.
        """
        deadline = time.monotonic() + seconds
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _resume_persisted_backoff(self) -> None:
        """Restore the recorded streak, then wait out the rest of its delay.

        Without this the delay restarts from its base on every daemon restart,
        so a condition that outlives a few restarts is never backed off far
        enough: an account-level rejection that had reached its hour would be
        retried a minute after each restart, and the log would say the same
        thing each time.
        """
        if self.state_store is None:
            return
        section = self.state_store.load().get(STATE_SECTION)
        if not isinstance(section, dict):
            return

        self._failure_streak = _coerce_streak(section.get("failure_streak"))
        if not self._failure_streak:
            return

        remaining = remaining_backoff(
            section.get("next_attempt_at"), time.time(), ACCOUNT_MAX_SECONDS
        )
        if remaining <= 0:
            logging.info(
                "Resuming translation with %d failed attempt(s) recorded; "
                "the last backoff has already expired.",
                self._failure_streak,
            )
            return
        logging.info(
            "Resuming translation backoff: %.0fs still to wait, after %d failed attempt(s).",
            remaining, self._failure_streak,
        )
        self._sleep(remaining)

    def _persist_backoff(self, delay: float, retryable: bool) -> None:
        """Record the streak and when the next attempt falls due.

        The streak is what reconstructs the delay if the timestamp is missing;
        the timestamp is what lets a restart wait only the remainder instead of
        serving the whole delay again. ``kind`` is for whoever reads the file,
        not for the arithmetic.
        """
        if self.state_store is None:
            return
        due = datetime.now(timezone.utc).timestamp() + delay
        self.state_store.save({STATE_SECTION: {
            "failure_streak": self._failure_streak,
            "next_attempt_at": datetime.fromtimestamp(due, timezone.utc).isoformat(),
            "kind": "retryable" if retryable else "account-level",
        }})

    def _register_failure(self, exc: BaseException) -> float:
        """Record a failed batch and return how long to wait before retrying."""
        retryable = retryable_failure(exc)
        self._failure_streak += 1
        delay = backoff_delay(self._failure_streak, retryable)
        kind = "retryable" if retryable else "account-level, not retryable"
        logging.error(
            "Batch translation failed (%s, attempt %d): %s — backing off %.0fs.",
            kind, self._failure_streak, exc, delay,
        )
        self._persist_backoff(delay, retryable)
        return delay

    def _register_success(self) -> None:
        """Clear the backoff once a batch gets through."""
        if not self._failure_streak:
            # Nothing outstanding. This runs after every batch, so the happy
            # path must not touch the disk.
            return
        logging.info(
            "Translation recovered after %d failed attempt(s).", self._failure_streak
        )
        self._failure_streak = 0
        if self.state_store is not None:
            self.state_store.remove(STATE_SECTION)

    def _translate_batch(self, batch: list[dict], client: OpenAI, model: str):
        """Translate a batch of fields using OpenAI and update the database."""
        items = []
        for row in batch:
            items.append({
                "id": f"{row['item_type']}_{row['item_id']}_{row['field']}",
                "field": row["field"],
                "text": row["original_text"],
            })

        prompt = f"""
Translate these Steam Workshop fields to English. Preserve BBcode tags.
Return ONLY a JSON array matching this exact format, preserving all 'id' values:

{json.dumps(items, ensure_ascii=False)}
"""
        now_ts = int(time.time())
        conn = get_connection(self.db_path)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You translate Steam Workshop text to English. Output only a raw JSON array."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
            )
            content = response.choices[0].message.content.strip()
            logging.debug(f"Raw translation response: {content[:500]}")
            translated = json.loads(content)
            if isinstance(translated, dict):
                for v in translated.values():
                    if isinstance(v, list):
                        translated = v
                        break
            if not isinstance(translated, list):
                raise ValueError(f"Expected list, got {type(translated)}: {content[:200]}")

            trans_map = {}
            for t in translated:
                tid = t.get("id", "")
                ttext = t.get("translated") or t.get("text", "")
                if tid and ttext:
                    trans_map[tid] = ttext

            translated_count = 0
            failed_count = 0
            translated_ids = set()
            for row in batch:
                tid = f"{row['item_type']}_{row['item_id']}_{row['field']}"
                trans_text = trans_map.get(tid, "")

                if not trans_text:
                    logging.warning(f"No translation returned for {tid}")
                    failed_count += 1
                    continue

                if row["item_type"] == "user":
                    table, id_col = "users", "steamid"
                    # Users have no steam_updated_at, so this column holds OUR
                    # wall-clock time and is named translated_at, not a version.
                    version_col = "translated_at"
                    version_ts = now_ts
                else:
                    table, id_col = "workshop_items", "workshop_id"
                    # Look up steam_updated_at for version tracking
                    version_col = "translate_version"
                    ver = conn.execute(
                        "SELECT steam_updated_at FROM workshop_items WHERE workshop_id = ?",
                        (row["item_id"],)
                    ).fetchone()
                    version_ts = ver["steam_updated_at"] if ver and ver["steam_updated_at"] else now_ts

                conn.execute(
                    f"UPDATE {table} SET {row['field']} = ?, {version_col} = ? WHERE {id_col} = ?",
                    (trans_text, version_ts, row["item_id"])
                )
                conn.execute("DELETE FROM translation_queue WHERE id = ?", (row["id"],))
                translated_count += 1
                translated_ids.add((row["item_type"], row["item_id"]))
                logging.debug(f"[{row['item_id']}] {row['field']}: \"{row['original_text'][:40]}\" → \"{trans_text[:40]}\"")

            for item_type, item_id in translated_ids:
                # Keyed by type as well as id: a steamid and a workshop_id are
                # both integers, so an unqualified id could count or complete a
                # row belonging to the other table.
                remaining = conn.execute(
                    "SELECT COUNT(*) as cnt FROM translation_queue "
                    "WHERE item_type=? AND item_id=?",
                    (item_type, item_id)
                ).fetchone()["cnt"]
                if remaining:
                    continue
                if item_type == "user":
                    # The per-field write above already stamped
                    # `users.translated_at` -- our clock is the only version a
                    # creator has, so completion needs no second stamp. Only the
                    # mirror is left, and clearing it here keeps
                    # `users.translation_priority` a mirror of the queue exactly
                    # as the item column is.
                    conn.execute(
                        "UPDATE users SET translation_priority = 0 WHERE steamid = ?",
                        (item_id,)
                    )
                    continue
                ver = conn.execute(
                    "SELECT steam_updated_at FROM workshop_items WHERE workshop_id = ?",
                    (item_id,)
                ).fetchone()
                version_ts = ver["steam_updated_at"] if ver and ver["steam_updated_at"] else now_ts
                # translated_at is OUR clock, stamped when the item's last
                # queued field is gone -- the point at which the stage is
                # actually complete for this item. It is written in the same
                # statement (and so the same transaction) that zeroes the
                # queue mirror, so a mirror that reads 0 and a completion
                # that reads NULL can never both be observed. The per-field
                # write above deliberately does not stamp it: an item with
                # one field still queued is a partial stage, not a
                # completion.
                conn.execute(
                    "UPDATE workshop_items SET translation_priority = 0, "
                    "translate_version = ?, translated_at = ? WHERE workshop_id = ?",
                    (version_ts, int(time.time()), item_id)
                )

            conn.commit()

            if failed_count:
                logging.info(f"Batch translation: {translated_count} added, {failed_count} failed.")
            else:
                logging.info(f"Batch translation: {translated_count} fields translated.")

        except Exception as e:
            # Deliberately not swallowed. The caller owns the backoff, and eating
            # the exception here is what let a failing batch be retried every
            # ~1.2 s: the loop could not tell failure from success.
            logging.debug(f"Batch translation error: {e}")
            raise
        finally:
            conn.close()


