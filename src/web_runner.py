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
    from waitress import serve

    # Suppress Waitress's unconditional WARNING; replace with tiered logging
    logging.getLogger('waitress.task').setLevel(logging.ERROR)
    import waitress.task
    _orig_add_task = waitress.task.ThreadedTaskDispatcher.add_task

    def _add_task_with_tiered_logging(self, task):
        _orig_add_task(self, task)
        queue_size = len(self.queue)
        idle_threads = len(self.threads) - self.stop_count - self.active_count
        depth = queue_size - idle_threads
        if depth >= 10:
            logging.warning("Task queue depth is %d", depth)
        elif depth >= 5:
            logging.info("Task queue depth is %d", depth)
        elif depth > 0:
            logging.debug("Task queue depth is %d", depth)

    waitress.task.ThreadedTaskDispatcher.add_task = _add_task_with_tiered_logging

    serve(app, host=host, port=port)


if __name__ == "__main__":
    main()
