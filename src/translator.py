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
    def __init__(self, config: dict):
        super().__init__(daemon=True)
        self.config = config
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.batch_size = config.get("openai", {}).get("batch", 20)
        self.running = True
        self._failure_streak = 0

    def run(self):
        openai_config = self.config.get("openai", {})
        if not _validate_openai_api_key(self.config):
            logging.warning("OpenAI API key not configured. Translation thread exiting.")
            return

        client = _create_openai_client(openai_config)
        model = openai_config.get("model", "gpt-4o-mini")
        logging.info("Starting batched translation background thread...")

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

    def _register_failure(self, exc: BaseException) -> float:
        """Record a failed batch and return how long to wait before retrying."""
        retryable = retryable_failure(exc)
        self._failure_streak += 1
        base = RETRY_BASE_SECONDS if retryable else ACCOUNT_BASE_SECONDS
        cap = RETRY_MAX_SECONDS if retryable else ACCOUNT_MAX_SECONDS
        delay = min(base * (2 ** (self._failure_streak - 1)), cap)
        kind = "retryable" if retryable else "account-level, not retryable"
        logging.error(
            "Batch translation failed (%s, attempt %d): %s — backing off %.0fs.",
            kind, self._failure_streak, exc, delay,
        )
        return delay

    def _register_success(self) -> None:
        """Clear the backoff once a batch gets through."""
        if self._failure_streak:
            logging.info(
                "Translation recovered after %d failed attempt(s).", self._failure_streak
            )
        self._failure_streak = 0

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
                translated_ids.add(row["item_id"])
                logging.debug(f"[{row['item_id']}] {row['field']}: \"{row['original_text'][:40]}\" → \"{trans_text[:40]}\"")

            for item_id in translated_ids:
                remaining = conn.execute(
                    "SELECT COUNT(*) as cnt FROM translation_queue WHERE item_type='item' AND item_id=?",
                    (item_id,)
                ).fetchone()["cnt"]
                if remaining == 0:
                    ver = conn.execute(
                        "SELECT steam_updated_at FROM workshop_items WHERE workshop_id = ?",
                        (item_id,)
                    ).fetchone()
                    version_ts = ver["steam_updated_at"] if ver and ver["steam_updated_at"] else now_ts
                    conn.execute(
                        "UPDATE workshop_items SET translation_priority = 0, translate_version = ? WHERE workshop_id = ?",
                        (version_ts, item_id)
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


