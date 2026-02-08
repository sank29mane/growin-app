"""
Audit Logging System
Provides tamper-evident structured logging for sensitive financial operations.
"""

from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import logging
import uuid
import os

logger = logging.getLogger(__name__)

class AuditJSONEncoder(json.JSONEncoder):
    """Custom encoder to handle Decimal and datetime for canonical hashing."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

class AuditEntry(BaseModel):
    """Immutable audit log entry with cryptographic linking."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action: str
    actor: str
    details: Dict[str, Any]
    previous_hash: str
    hash: str = ""

    def compute_hash(self) -> str:
        """Compute SHA256 hash of the entry content including previous hash."""
        payload = {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
            "previous_hash": self.previous_hash
        }
        # innovative Canonical JSON representation for consistent hashing
        serialized = json.dumps(payload, sort_keys=True, separators=(',', ':'), cls=AuditJSONEncoder)
        return hashlib.sha256(serialized.encode()).hexdigest()

class AuditLogger:
    """
    Manages the audit log file and ensures integrity chaining.
    """
    def __init__(self, log_path: str = "audit.log"):
        self.log_path = log_path
        # If file doesn't exist, create it (touch)
        if not os.path.exists(self.log_path):
            with open(self.log_path, 'w') as f:
                pass 
        self.last_hash = self._get_last_hash()

    def _get_last_hash(self) -> str:
        """Read the last entry from the log file to get the previous hash."""
        if os.path.getsize(self.log_path) == 0:
             return "0" * 64

        try:
            with open(self.log_path, 'rb') as f:
                # Seek to end
                f.seek(0, 2)
                file_size = f.tell()
                
                # If file is small, just read it all
                if file_size < 1024:
                    f.seek(0)
                    lines = f.readlines()
                    if not lines:
                        return "0" * 64
                    last_line = lines[-1].decode().strip()
                else:
                    # Read last 1KB chunk
                    chunk_size = 1024
                    f.seek(file_size - chunk_size)
                    last_chunk = f.read(chunk_size)
                    # Split by newline and take the last complete line
                    lines = last_chunk.split(b'\n')
                    # If the last byte was \n, the last split is empty string
                    if not lines[-1]:
                         lines.pop()
                    last_line = lines[-1].decode().strip()

            if not last_line:
                 return "0" * 64

            last_entry = json.loads(last_line)
            return last_entry.get("hash", "0" * 64)
            
        except Exception as e:
            # Fallback to safe genesis hash if corrupted
            logger.error(f"Failed to read last audit hash: {e}")
            return "0" * 64

    def log_event(self, action: str, actor: str, details: Dict[str, Any]) -> str:
        """
        Log an audit event.
        Returns the ID of the new entry.
        """
        entry = AuditEntry(
            action=action,
            actor=actor,
            details=details,
            previous_hash=self.last_hash
        )
        entry.hash = entry.compute_hash()
        
        try:
            # Use AuditJSONEncoder for the final write as well
            with open(self.log_path, 'a') as f:
                f.write(entry.model_dump_json() + '\n')
            
            self.last_hash = entry.hash
            return entry.id
        except Exception as e:
            logger.critical(f"FATAL: Failed to write to audit log: {e}")
            raise # Audit failure should stop the world in secure systems
            
# Singleton instance
_audit_logger = None

def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        # Determine strict path (e.g. absolute path or env var)
        log_path = os.environ.get("AUDIT_LOG_PATH", "audit.log")
        _audit_logger = AuditLogger(log_path)
    return _audit_logger

def log_audit(action: str, actor: str, details: Dict[str, Any]):
    """Convenience function to log an audit event."""
    return get_audit_logger().log_event(action, actor, details)
