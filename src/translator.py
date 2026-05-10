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
                    time.sleep(1)
                else:
                    time.sleep(10)
            except Exception as e:
                logging.error(f"Translator thread error: {e}")
                time.sleep(30)

        logging.info("Translation thread stopped.")

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
Translate the following Steam Workshop fields into English.
Maintain the tone and any formatting (like [b] tags).
Return the result as a raw JSON array of objects matching this schema:
[{{"id": "item_123_title", "field": "title_en", "translated": "Hello"}}]
If a field is already in English, return it unchanged.
Do NOT include any text outside the JSON array.

{json.dumps(items, ensure_ascii=False)}
"""

        now_iso = datetime.now(timezone.utc).isoformat()
        conn = get_connection(self.db_path)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a helpful translator for Steam Workshop content. Output valid JSON arrays only."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
            )
            content = response.choices[0].message.content
            translated = json.loads(content)

            # If the response wraps in {"translations": [...]}, handle it
            if isinstance(translated, dict):
                for v in translated.values():
                    if isinstance(v, list):
                        translated = v
                        break

            if not isinstance(translated, list):
                raise ValueError(f"Expected JSON array, got {type(translated)}")

            # Build a lookup from id to translated text
            trans_map = {}
            for t in translated:
                tid = t.get("id", "")
                ttext = t.get("translated", "")
                if tid and ttext:
                    trans_map[tid] = ttext

            for row in batch:
                tid = f"{row['item_type']}_{row['item_id']}_{row['field']}"
                trans_text = trans_map.get(tid, "")

                if not trans_text:
                    logging.warning(f"No translation returned for {tid}")
                    continue

                # Determine target table and column
                if row["item_type"] == "user":
                    table = "users"
                    id_col = "steamid"
                else:
                    table = "workshop_items"
                    id_col = "workshop_id"

                conn.execute(
                    f"UPDATE {table} SET {row['field']} = ?, dt_translated = ? WHERE {id_col} = ?",
                    (trans_text, now_iso, row["item_id"])
                )
                conn.execute(
                    "DELETE FROM translation_queue WHERE id = ?",
                    (row["id"],)
                )

                old = row["original_text"][:40]
                new = trans_text[:40]
                logging.info(f"[{row['item_id']}] ({row['item_type']}) {row['field']}: \"{old}\" → \"{new}\"")
            conn.commit()

        except Exception as e:
            logging.error(f"Batch translation failed: {e}")
        finally:
            conn.close()
