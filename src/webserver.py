"""Embedded web server for Steam Workshop Scraper."""

import json
import os
import re
import logging
import requests
from flask import Flask, request, jsonify, render_template, send_from_directory
from src.database import search_items, get_item_details, get_db_stats, get_all_authors, save_app_filter, compute_wilson_cutoffs, bump_web_priority_for_list, bump_web_priority_for_detail, bump_translation_for_list, bump_translation_for_detail, bump_image_priority_for_list, bump_image_priority_for_detail, flag_for_image, get_connection
from src.analysis import view_window_analysis

app = Flask(__name__, template_folder='../templates')
_db_path = "workshop.db"
_config = {}
_images_dir = "images"
_sessionid = ""


def init_webserver(db_path: str, config: dict):
    global _db_path, _config, _images_dir
    _db_path = db_path
    _config = config
    _images_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "images")


def _bbcode_to_html(text):
    """Converts Steam BBCode to HTML for web display."""
    if not text:
        return ""
    t = re.sub(r'\[h1\](.*?)\[/h1\]', r'<h3>\1</h3>', text, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[h2\](.*?)\[/h2\]', r'<h4>\1</h4>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[h3\](.*?)\[/h3\]', r'<h5>\1</h5>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[b\](.*?)\[/b\]', r'<b>\1</b>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[i\](.*?)\[/i\]', r'<i>\1</i>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[u\](.*?)\[/u\]', r'<u>\1</u>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[list\]', '<ul>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[/list\]', '</ul>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[\*\](.*?)\n?', r'<li>\1</li>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[table\]', '<table>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[/table\]', '</table>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[tr\]', '<tr>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[/tr\]', '</tr>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[th\](.*?)\[/th\]', r'<th>\1</th>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[td\](.*?)\[/td\]', r'<td>\1</td>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[quote\](.*?)\[/quote\]', r'<blockquote>\1</blockquote>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[quote=([^\]]*)\](.*?)\[/quote\]', r'<blockquote><b>\1:</b><br>\2</blockquote>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[code\](.*?)\[/code\]', r'<pre><code>\1</code></pre>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[img\](.*?)\[/img\]', r'<img src="\1" alt="image">', t, flags=re.IGNORECASE)
    t = re.sub(r'\[url\](.*?)\[/url\]', r'<a href="\1" target="_blank">\1</a>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[url=([^\]]*)\](.*?)\[/url\]', r'<a href="\1" target="_blank">\2</a>', t, flags=re.IGNORECASE)
    t = re.sub(r'\n', '<br>', t)
    return t


def _format_count(n):
    if not n or n == 0:
        return "0"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        if n < 10_000:
            return f"{n/1000:.2f}K"
        elif n < 100_000:
            return f"{n/1000:.1f}K"
        return f"{n/1000:.0f}K"
    v = n / 1_000_000
    if n < 10_000_000:
        return f"{v:.2f}M"
    elif n < 100_000_000:
        return f"{v:.1f}M"
    return f"{v:.0f}M"


def _format_size(size_bytes):
    if not size_bytes:
        return "N/A"
    size = float(size_bytes)
    kb = size / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb/1024:.1f} GB"


@app.template_filter('fcount')
def template_fcount(n):
    return _format_count(n)


@app.template_filter('fsize')
def template_fsize(n):
    return _format_size(n)


@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(_images_dir, filename)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/userscript/<path:filename>')
def serve_userscript(filename):
    template_path = os.path.join(os.path.dirname(__file__), '..', 'userscripts', filename)
    if not os.path.isfile(template_path):
        return jsonify({"error": "not found"}), 404

    with open(template_path, 'r', encoding='utf-8') as f:
        content = f.read()

    host = request.host
    if host and not host.startswith('127.') and not host.startswith('localhost'):
        includes = [f'// @include      http://{host}/*']
        marker = '// ==/UserScript=='
        content = content.replace(marker, '\n'.join(includes) + '\n' + marker)

    return content, 200, {'Content-Type': 'application/javascript; charset=utf-8'}


@app.route('/api/search', methods=['POST', 'GET'])
def api_search():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    sort_by = data.get('sort_by', 'title')
    sort_order = data.get('sort_order', 'ASC')
    offset = data.get('offset', 0)
    limit = data.get('limit', 50)

    try:
        results = search_items(
            _db_path,
            filters=filters,
            sort_by=sort_by,
            sort_order=sort_order,
            summary_only=True,
            limit=limit,
            offset=offset,
        )

        if results:
            image_flagged_count = 0
            for item in results:
                wid = item['workshop_id']
                bump_web_priority_for_list(_db_path, wid)
                bump_image_priority_for_list(_db_path, wid)
                if _ensure_image_flagged(wid, 5):
                    image_flagged_count += 1
                bump_translation_for_list(_db_path, wid)

            ids = [r['workshop_id'] for r in results]
            conn = get_connection(_db_path)
            placeholders = ','.join('?' * len(ids))
            updated = conn.execute(
                f"SELECT workshop_id, needs_web_scrape, needs_image, translation_priority FROM workshop_items WHERE workshop_id IN ({placeholders})",
                ids
            ).fetchall()
            conn.close()
            updated_map = {row['workshop_id']: dict(row) for row in updated}
            for r in results:
                if r['workshop_id'] in updated_map:
                    u = updated_map[r['workshop_id']]
                    r['needs_web_scrape'] = u['needs_web_scrape']
                    r['needs_image'] = u['needs_image']
                    r['translation_priority'] = u['translation_priority']

            sample = results[0] if results else {}
            logging.info(f"[Search] returned {len(results)} items, flagged {image_flagged_count} for image, sample needs_image={sample.get('needs_image')} image_extension={sample.get('image_extension')!r}")

        return jsonify(results)
    except Exception as e:
        logging.exception(f"[Search] Error processing search request")
        return jsonify({"error": str(e)}), 500


@app.route('/api/item/<int:workshop_id>')
def api_item(workshop_id):
    bump_web_priority_for_detail(_db_path, workshop_id)
    bump_image_priority_for_detail(_db_path, workshop_id)
    _ensure_image_flagged(workshop_id, 10)
    bump_translation_for_detail(_db_path, workshop_id)

    item = get_item_details(_db_path, workshop_id)
    if not item:
        return jsonify({"error": "not found"}), 404

    desc = item.get("extended_description_en") or item.get("extended_description") or ""
    item["description_html"] = _bbcode_to_html(desc)

    display_title = item.get("title_en") or item.get("title") or "N/A"
    item["display_title"] = display_title

    return jsonify(item)


@app.route('/api/state')
def api_state():
    state_path = os.path.join(os.path.dirname(_db_path), ".tui_state.yaml")
    try:
        import yaml
        with open(state_path, 'r', encoding='utf-8') as f:
            state = yaml.safe_load(f) or {}
    except Exception:
        state = {}
    return jsonify(state)


@app.route('/api/cutoffs', methods=['POST'])
def api_cutoffs():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    cutoffs = compute_wilson_cutoffs(_db_path, filters if filters else None)
    result = {}
    for k, v in cutoffs.items():
        result[k] = v
    return jsonify(result)


@app.route('/api/authors')
def api_authors():
    authors = get_all_authors(_db_path)
    return jsonify(authors)


@app.route('/api/tags')
def api_tags():
    stats = get_db_stats(_db_path)
    return jsonify(stats.get("tag_counts", {}))


@app.route('/api/stats')
def api_stats():
    stats = get_db_stats(_db_path)
    return jsonify(stats)


@app.route('/api/analysis')
def api_analysis():
    bucket = request.args.get('bucket_days', 7, type=int)
    result = view_window_analysis(_db_path, bucket_days=bucket)
    return jsonify(result)


@app.route('/api/save_filter', methods=['POST'])
def api_save_filter():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    appids = _config.get("daemon", {}).get("target_appids", [])
    appid = appids[0] if appids else None
    if appid is None:
        return jsonify({"error": "No target AppID configured"}), 400
    save_app_filter(_db_path, appid, enrichment_filters=json.dumps(filters))
    return jsonify({"ok": True, "appid": appid})


def _ensure_image_flagged(workshop_id, priority):
    """If item has preview_url but no image_extension, flag it for download."""
    conn = get_connection(_db_path)
    row = conn.execute(
        "SELECT preview_url, image_extension, needs_image FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()
    if row and row["preview_url"] and not row["image_extension"]:
        flag_for_image(_db_path, workshop_id, max(row["needs_image"] or 1, priority))
        return True
    return False


@app.route('/api/subscribe/<int:workshop_id>', methods=['POST'])
def api_subscribe(workshop_id):
    global _sessionid
    sid = _sessionid or _config.get("session", {}).get("id", "")
    logging.info(f"[Subscribe] request for workshop_id={workshop_id}, sessionid={'set' if sid else 'missing'}")
    if not sid:
        logging.warning(f"[Subscribe] No sessionid available — userscript may not have pushed one")
        return jsonify({"success": -1, "message": "No Steam session configured."}), 400

    conn = get_connection(_db_path)
    row = conn.execute(
        "SELECT consumer_appid FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"success": -1, "message": "Item not found."}), 404

    appid = row["consumer_appid"]
    if not appid:
        return jsonify({"success": -1, "message": "Item has no AppID."}), 400

    login = _config.get("session", {}).get("login_secure", "")
    if isinstance(login, list):
        login = '%7C%7C'.join(str(v) for v in login)
    logging.info(f"[Subscribe] POSTing to Steam: id={workshop_id}, appid={appid}, sessionid={sid[:6]}..., login={'set' if login else 'missing'}")
    try:
        resp = requests.post(
            "https://steamcommunity.com/sharedfiles/subscribe",
            data={
                "id": str(workshop_id),
                "appid": str(appid),
                "include_dependencies": "false",
                "sessionid": sid,
            },
            cookies={
                "sessionid": sid,
                "steamLoginSecure": login,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://steamcommunity.com",
                "Referer": f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            },
            timeout=15,
        )
        data = resp.json()
        logging.info(f"[Subscribe] Steam response: status={resp.status_code}, body={data}")
        return jsonify(data)
    except Exception as e:
        logging.warning(f"[Subscribe] failed for workshop_id={workshop_id}: {e}")
        return jsonify({"success": -1, "message": f"Subscribe request failed: {e}"}), 502


@app.route('/api/sessionid', methods=['POST'])
def api_sessionid():
    global _sessionid
    data = request.get_json(silent=True) or {}
    sid = data.get("sessionid", "").strip()
    login_secure = data.get("login_secure", "").strip()
    if sid:
        _sessionid = sid
        if login_secure:
            _config.setdefault("session", {})["login_secure"] = login_secure
        logging.info(f"SessionID updated from userscript (login_secure: {'set' if login_secure else 'missing'})")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "message": "No sessionid provided."}), 400
