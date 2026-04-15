import logging
from src.daemon import Daemon
from src.config import load_config
import sys

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    config_path = "config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
        
    daemon = Daemon(config)
    daemon.run()

if __name__ == "__main__":
    main()
