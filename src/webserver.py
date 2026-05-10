"""Embedded web server for Steam Workshop Scraper."""

import json
import os
import re
import queue
from flask import Flask, request, jsonify, render_template, send_from_directory, Response
from src.database import search_items, get_item_details, get_db_stats, get_all_authors, save_app_filter, compute_wilson_cutoffs, bump_web_priority_for_list, bump_web_priority_for_detail, bump_translation_for_detail, bump_image_priority_for_list, bump_image_priority_for_detail, flag_for_image, get_connection
from src.analysis import view_window_analysis

app = Flask(__name__, template_folder='../templates')
_db_path = "workshop.db"
_config = {}
_event_queues: list[queue.Queue] = []


def _notify_web_clients(event_type: str, data: dict):
    """Thread-safe: pushes an event to all connected SSE clients."""
    payload = json.dumps({"type": event_type, **data})
    for q in _event_queues[:]:  # iterate a copy since queues can be removed
        try:
            q.put_nowait(payload)
        except Exception:
            _event_queues.remove(q)


def init_webserver(db_path: str, config: dict):
    global _db_path, _config
    _db_path = db_path
    _config = config


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


@app.route('/api/events')
def api_events():
    """SSE endpoint: streams real-time notifications to web clients."""
    q = queue.Queue()
    _event_queues.append(q)

    def generator():
        yield "data: {\"type\":\"connected\"}\n\n"
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield ":keepalive\n\n"

    response = Response(generator(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.call_on_close(lambda: _event_queues.remove(q) if q in _event_queues else None)
    return response


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/search', methods=['POST', 'GET'])
def api_search():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    sort_by = data.get('sort_by', 'title')
    sort_order = data.get('sort_order', 'ASC')
    offset = data.get('offset', 0)
    limit = data.get('limit', 50)

    results = search_items(
        _db_path,
        filters=filters,
        sort_by=sort_by,
        sort_order=sort_order,
        summary_only=True,
        limit=limit,
        offset=offset,
    )
    return jsonify(results)


@app.route('/api/item/<int:workshop_id>')
def api_item(workshop_id):
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


@app.route('/api/bump_web_list/<int:workshop_id>', methods=['POST'])
def api_bump_web_list(workshop_id):
    bump_web_priority_for_list(_db_path, workshop_id)
    return jsonify({"ok": True})


@app.route('/api/bump_web_detail/<int:workshop_id>', methods=['POST'])
def api_bump_web_detail(workshop_id):
    bump_web_priority_for_detail(_db_path, workshop_id)
    return jsonify({"ok": True})


@app.route('/api/bump_translation_detail/<int:workshop_id>', methods=['POST'])
def api_bump_translation_detail(workshop_id):
    bump_translation_for_detail(_db_path, workshop_id)
    return jsonify({"ok": True})


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


@app.route('/api/bump_image_list/<int:workshop_id>', methods=['POST'])
def api_bump_image_list(workshop_id):
    _ensure_image_flagged(workshop_id, 5)
    bump_image_priority_for_list(_db_path, workshop_id)
    return jsonify({"ok": True})


@app.route('/api/bump_image_detail/<int:workshop_id>', methods=['POST'])
def api_bump_image_detail(workshop_id):
    _ensure_image_flagged(workshop_id, 10)
    bump_image_priority_for_detail(_db_path, workshop_id)
    return jsonify({"ok": True})
