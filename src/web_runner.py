"""Standalone entry point for the web UI."""
import sys
import socket
import logging
from src.config import load_config
from src.database import initialize_database
from src.webserver import app, init_webserver


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    config_path = "config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    config = load_config(config_path)
    db_path = config.get("database", {}).get("path", "workshop.db")
    initialize_database(db_path)
    init_webserver(db_path, config)

    web_config = config.get("web", {})
    port = web_config.get("port", 8080)
    host = web_config.get("host", "0.0.0.0")

    # Find a free port if the configured one is in use
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((host, port))
        s.close()
    except OSError:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((host, 0))
        port = s.getsockname()[1]
        s.close()
        logging.warning(f"Port {web_config.get('port', 8080)} in use, using {port}")

    logging.info(f"Starting web server on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
