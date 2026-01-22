import logging
import sys
from collections import deque

# Global buffer for real-time logs (last 100 lines)
log_buffer = deque(maxlen=100)

class MemoryHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_buffer.append(msg)
        except Exception:
            self.handleError(record)

def setup_logging(name: str = "growin_backend", level: int = logging.INFO) -> logging.Logger:
    """
    Configures a centralized logger with console output and specific formatting.
    """
    logger = logging.getLogger(name)
    
    # avoid adding handlers if they already exist
    if logger.hasHandlers():
        return logger
        
    logger.setLevel(level)

    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Memory Handler for Intelligent Console
    memory_handler = MemoryHandler()
    memory_handler.setLevel(level)
    memory_handler.setFormatter(formatter)
    logger.addHandler(memory_handler)
    
    return logger

def get_recent_logs():
    """Returns the last 100 logs as a list of strings."""
    return list(log_buffer)
