import time
import json
import logging
import threading
from datetime import datetime, timezone
from openai import OpenAI
from src.database import get_connection, get_next_batch_for_translation

logging.getLogger("httpx").setLevel(logging.WARNING)


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
        self.running = True

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
                batch = get_next_batch_for_translation(self.db_path, limit=20)
                if batch:
                    self._translate_batch(batch, client, model)
                    time.sleep(1 if len(batch) >= 20 else 10)
                else:
                    time.sleep(10)
            except Exception as e:
                logging.error(f"Translator thread error: {e}")
                time.sleep(30)

        # Thread lifecycle logging handled by daemon

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
        now_iso = datetime.now(timezone.utc).isoformat()
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
                else:
                    table, id_col = "workshop_items", "workshop_id"

                conn.execute(
                    f"UPDATE {table} SET {row['field']} = ?, dt_translated = ? WHERE {id_col} = ?",
                    (trans_text, now_iso, row["item_id"])
                )
                conn.execute("DELETE FROM translation_queue WHERE id = ?", (row["id"],))
                translated_count += 1
                translated_ids.add(row["item_id"])
                logging.debug(f"[{row['item_id']}] {row['field']}: \"{row['original_text'][:40]}\" → \"{trans_text[:40]}\"")
            conn.commit()

            if failed_count:
                logging.info(f"Batch translation: {translated_count} added, {failed_count} failed.")
            else:
                logging.info(f"Batch translation: {translated_count} fields translated.")

        except Exception as e:
            logging.error(f"Batch translation failed: {e}")
        finally:
            conn.close()


