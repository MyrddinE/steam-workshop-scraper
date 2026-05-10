import logging
from src.daemon import Daemon
from src.config import load_config
from src.database import initialize_database
import sys


def main():
    config_path = "config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        logging.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
        
    log_config = config.get("logging", {})
    level_str = log_config.get("level", "INFO").upper()
    log_level = getattr(logging, level_str, logging.INFO)
    log_file = log_config.get("file")

    handlers = []
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    # stdout: everything; stderr: errors only
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    handlers.append(stdout_handler)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    handlers.append(stderr_handler)

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True
    )
    
    db_path = config.get("database", {}).get("path", "workshop.db")
    initialize_database(db_path)
    
    daemon = Daemon(config, config_path)
    daemon.run()

if __name__ == "__main__":
    main()
