"""Standalone entry point for the web UI."""
import sys
import socket
import logging
from src.config import ConfigError, load_config
from src.daemon_control import (
    DaemonController,
    DaemonStillRunningError,
    initialize_database_with_daemon_stopped,
)
from src.database import SchemaVersionError
from src.webserver import app, init_webserver
from src import crash


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    config_path = "config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        logging.error("%s", exc)
        sys.exit(1)
    except ConfigError as exc:
        logging.error("%s", exc)
        sys.exit(2)
    # Installed after the entry point configured logging, so the ring-buffer
    # handler survives. It does not alter the handlers above, the exit codes or
    # the order of anything below; it only adds a way for an unhandled traceback
    # to reach the outbox instead of a console nobody reads.
    crash.install("web", config, config_path=config_path)
    db_path = config.get("database", {}).get("path", "workshop.db")
    # One controller for this process: the startup gate uses it to stop a running
    # daemon if a pending migration needs it, and the web server's daemon panel
    # drives the same object once the server is up.
    controller = DaemonController(config_path, config=config)
    try:
        initialize_database_with_daemon_stopped(db_path, controller)
    except (SchemaVersionError, DaemonStillRunningError) as exc:
        # Refused before the web server is initialised: the operator gets the
        # sentence and exit 2, and no degraded server is left bound to a port
        # against a schema this build does not understand, or one a daemon is
        # still writing to.
        logging.error("%s", exc)
        sys.exit(2)
    init_webserver(db_path, config, config_path=config_path,
                   daemon_controller=controller)

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

    # Replace Waitress's single WARNING queue-depth log with tiered logging.
    # Wrapped in try/except so a future Waitress API change does not crash the server.
    try:
        import waitress.task
    # Optional waitress.task instrumentation; without it the server still runs with
    # the stock queue-depth log.
    except ImportError:
        pass
    else:
        _orig_add_task = getattr(waitress.task.ThreadedTaskDispatcher, 'add_task', None)
        if _orig_add_task is not None:
            logging.getLogger('waitress.task').setLevel(logging.ERROR)

            def _add_task_with_tiered_logging(self, task):
                _orig_add_task(self, task)
                try:
                    queue_size = len(self.queue)
                    idle = len(self.threads) - self.stop_count - self.active_count
                    depth = queue_size - idle
                    if depth >= 10:
                        logging.warning("Task queue depth is %d", depth)
                    elif depth >= 5:
                        logging.info("Task queue depth is %d", depth)
                    elif depth > 0:
                        logging.debug("Task queue depth is %d", depth)
                except Exception:
                    pass  # robust to attribute changes in future Waitress

            waitress.task.ThreadedTaskDispatcher.add_task = _add_task_with_tiered_logging

    serve(app, host=host, port=port)


if __name__ == "__main__":
    main()
