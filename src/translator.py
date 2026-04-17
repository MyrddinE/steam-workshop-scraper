import time
import json
import logging
import threading
from datetime import datetime, timezone
from openai import OpenAI
from src.database import get_connection, get_next_translation_item

def is_ascii(s: str) -> bool:
    """Returns True if the string is entirely ASCII."""
    if not s:
        return True
    return all(ord(c) < 128 for c in s)

def translate_item(db_path: str, workshop_id: int, config: dict):
    """
    Fetches the row for workshop_id, translates relevant fields via OpenAI,
    and updates the database with the results.
    """
    openai_config = config.get("openai", {})
    api_key = openai_config.get("api_key")
    if not api_key or "YOUR_OPENAI_API_KEY" in api_key:
        return

    conn = get_connection(db_path)
    cursor = conn.execute(
        "SELECT title, short_description, extended_description FROM workshop_items WHERE workshop_id = ?",
        (workshop_id,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return

    title = row["title"] or ""
    short_desc = row["short_description"] or ""
    extended_desc = row["extended_description"] or ""

    # Prepare OpenAI Client
    client = OpenAI(
        api_key=api_key,
        base_url=openai_config.get("endpoint", "https://api.openai.com/v1")
    )

    prompt = f"""
    Translate the following Steam Workshop item metadata into English. 
    Maintain the tone and any formatting (like [b] tags).
    Return the result as a raw JSON object with these keys: "title_en", "short_description_en", "extended_description_en".
    If a field is already in English, return it unchanged.

    Title: {title}
    Short Description: {short_desc}
    Extended Description: {extended_desc}
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
        
        conn.execute(
            """
            UPDATE workshop_items 
            SET title_en = ?, short_description_en = ?, extended_description_en = ?, 
                dt_translated = ?, translation_priority = 0
            WHERE workshop_id = ?
            """,
            (
                translated_data.get("title_en"),
                translated_data.get("short_description_en"),
                translated_data.get("extended_description_en"),
                now_iso,
                workshop_id
            )
        )
        conn.commit()
        logging.info(f"[{workshop_id}] Successfully translated to English.")
        
    except Exception as e:
        logging.error(f"[{workshop_id}] Translation failed: {e}")
        # Reset priority to prevent infinite immediate retries, or maybe set to 1 if we want to retry later?
        # User requested translation (10) should maybe be reset to 1 on failure.
        conn.execute("UPDATE workshop_items SET translation_priority = 1 WHERE workshop_id = ?", (workshop_id,))
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
                workshop_id = get_next_translation_item(self.db_path)
                if workshop_id:
                    translate_item(self.db_path, workshop_id, self.config)
                    # Small breath between translations to be polite
                    time.sleep(1)
                else:
                    # No items to translate, wait a bit
                    time.sleep(10)
            except Exception as e:
                logging.error(f"Translator thread error: {e}")
                time.sleep(30)
        
        logging.info("Translation thread stopped.")
