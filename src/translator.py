import time
import json
import logging
import threading
from datetime import datetime, timezone
from openai import OpenAI
from src.database import get_connection, get_next_translation_item

# Silence httpx logging which openai uses internally
logging.getLogger("httpx").setLevel(logging.WARNING)

def _validate_openai_api_key(config: dict) -> str | None:
    """Returns the OpenAI API key from config, or None if not configured."""
    openai_config = config.get("openai", {})
    api_key = openai_config.get("api_key")
    if api_key and "YOUR_OPENAI_API_KEY" not in api_key:
        return api_key
    return None

def _create_openai_client(openai_config: dict) -> OpenAI:
    """Creates an OpenAI client from the config's openai section."""
    return OpenAI(
        api_key=openai_config.get("api_key"),
        base_url=openai_config.get("endpoint", "https://api.openai.com/v1")
    )

def is_ascii(s: str) -> bool:
    """Returns True if the string is entirely ASCII."""
    if not s:
        return True
    return all(ord(c) < 128 for c in s)

def translate_item(db_path: str, item_id: int, config: dict, item_type: str = "workshop_item", priority: int = 0):
    """
    Fetches the row for workshop_id or steamid, translates relevant fields via OpenAI,
    and updates the database with the results.
    """
    if not _validate_openai_api_key(config):
        return

    conn = get_connection(db_path)
    
    if item_type == "workshop_item":
        cursor = conn.execute(
            "SELECT title, short_description, extended_description FROM workshop_items WHERE workshop_id = ?",
            (item_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return
        fields_to_translate = {
            "title_en": row["title"] or "",
            "short_description_en": row["short_description"] or "",
            "extended_description_en": row["extended_description"] or ""
        }
        id_col = "workshop_id"
        table = "workshop_items"
    else:
        cursor = conn.execute(
            "SELECT personaname FROM users WHERE steamid = ?",
            (item_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return
        fields_to_translate = {
            "personaname_en": row["personaname"] or ""
        }
        id_col = "steamid"
        table = "users"

    openai_config = config.get("openai", {})
    client = _create_openai_client(openai_config)

    prompt = f"""
    Translate the following Steam Workshop {'item metadata' if item_type == 'workshop_item' else 'user name'} into English. 
    Maintain the tone and any formatting (like [b] tags).
    Return the result as a raw JSON object with these keys: {', '.join(f'"{k}"' for k in fields_to_translate.keys())}.
    If a field is already in English, return it unchanged.

    {json.dumps(fields_to_translate, ensure_ascii=False)}
    """

    try:
        response = client.chat.completions.create(
            model=openai_config.get("model", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "You are a helpful translator specializing in gaming and Steam Workshop content. You always output valid JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        
        content = response.choices[0].message.content
        translated_data = json.loads(content)
        
        now_iso = datetime.now(timezone.utc).isoformat()
        
        update_parts = [f"{k} = ?" for k in fields_to_translate.keys()]
        update_parts.append("dt_translated = ?")
        update_parts.append("translation_priority = 0")
        
        sql = f"UPDATE {table} SET {', '.join(update_parts)} WHERE {id_col} = ?"
        
        params = [translated_data.get(k) for k in fields_to_translate.keys()]
        params.append(now_iso)
        params.append(item_id)
        
        conn.execute(sql, params)
        conn.commit()

        # Success logging
        old_val = row["title"] if item_type == "workshop_item" else row["personaname"]
        new_val = translated_data.get("title_en") if item_type == "workshop_item" else translated_data.get("personaname_en")
        logging.info(f"[{item_id}] ({item_type}) Translated \"{old_val}\" to \"{new_val}\" @ p{priority}.")
        
    except Exception as e:
        logging.error(f"[{item_id}] ({item_type}) Translation failed: {e}")
        conn.execute(f"UPDATE {table} SET translation_priority = 1 WHERE {id_col} = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()

class TranslatorThread(threading.Thread):
    def __init__(self, config: dict):
        super().__init__(daemon=True)
        self.config = config
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.running = True

    def run(self):
        openai_config = self.config.get("openai", {})
        api_key = openai_config.get("api_key")
        if not api_key or "YOUR_OPENAI_API_KEY" in api_key:
            logging.warning("OpenAI API key not configured. Translation thread exiting.")
            return

        logging.info("Starting translation background thread...")
        
        while self.running:
            try:
                result = get_next_translation_item(self.db_path)
                if result:
                    item_type, item_id, priority = result
                    translate_item(self.db_path, item_id, self.config, item_type=item_type, priority=priority)
                    # Small breath between translations to be polite
                    time.sleep(1)
                else:
                    # No items to translate, wait a bit
                    time.sleep(10)
            except Exception as e:
                logging.error(f"Translator thread error: {e}")
                time.sleep(30)
        
        logging.info("Translation thread stopped.")
