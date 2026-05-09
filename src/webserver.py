"""Embedded web server for Steam Workshop Scraper."""

import json
from flask import Flask, request, jsonify, render_template
from src.database import search_items, get_item_details, get_db_stats, get_all_authors, save_app_filter
from src.analysis import view_window_analysis

app = Flask(__name__, template_folder='../templates')
_db_path = "workshop.db"
_config = {}


def init_webserver(db_path: str, config: dict):
    global _db_path, _config
    _db_path = db_path
    _config = config


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
    if item:
        return jsonify(item)
    return jsonify({"error": "not found"}), 404


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
