import logging
import sys
from collections import deque
from utils.secret_masker import SecretMasker

# Global buffer for real-time logs (last 100 lines)
log_buffer = deque(maxlen=100)

class MemoryHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_buffer.append(msg)
        except Exception:
            self.handleError(record)

class SecretMaskingFormatter(logging.Formatter):
    """Custom formatter that masks secrets in log messages and args."""
    
    def format(self, record):
        # 1. Mask the main message string
        if isinstance(record.msg, str):
            record.msg = SecretMasker.mask_string(record.msg)
        
        # 2. Mask arguments if present (e.g. logger.info("User: %s", user_data))
        if record.args:
            # record.args can be a tuple or dict
            if isinstance(record.args, dict):
                record.args = SecretMasker.mask_structure(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    SecretMasker.mask_structure(arg) for arg in record.args
                )
        
        # 3. Format using standard parent method
        return super().format(record)

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
    formatter = SecretMaskingFormatter(
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
