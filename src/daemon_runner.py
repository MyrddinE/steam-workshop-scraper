import logging
from src.daemon import Daemon
from src.config import load_config
from src.database import initialize_database
import sys

def main():
    # Initial basic configuration for startup errors
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    config_path = "config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
        
    # Reconfigure logging based on config file
    log_config = config.get("logging", {})
    level_str = log_config.get("level", "INFO").upper()
    log_level = getattr(logging, level_str, logging.INFO)
    log_file = log_config.get("file")
    
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
        
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
