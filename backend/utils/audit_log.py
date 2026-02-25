"""
Audit Logging System
Provides tamper-evident structured logging for sensitive financial operations.
Standard: SOTA 2026 Financial Logging (VeritasChain/RFC 8785)
"""

from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import uuid
import os
import canonicaljson

logger = logging.getLogger(__name__)

def _prepare_for_canonical(data: Any) -> Any:
    """Recursively converts Decimals and datetimes to strings for canonical JSON."""
    if isinstance(data, dict):
        return {k: _prepare_for_canonical(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_prepare_for_canonical(i) for i in data]
    elif isinstance(data, Decimal):
        return str(data)
    elif isinstance(data, datetime):
        return data.isoformat()
    return data

class AuditEntry(BaseModel):
    """Immutable audit log entry with cryptographic linking."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: str
    actor: str
    details: Dict[str, Any]
    previous_hash: str
    hash: str = ""

    def compute_hash(self) -> str:
        """Compute SHA256 hash of the entry using RFC 8785 Canonical JSON."""
        payload = {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "actor": self.actor,
            "details": _prepare_for_canonical(self.details),
            "previous_hash": self.previous_hash
        }
        # SOTA 2026: Use canonical JSON encoding for consistent hashing
        serialized = canonicaljson.encode_canonical_json(payload)
        return hashlib.sha256(serialized).hexdigest()

class AuditLogger:
    """
    Manages the audit log file and ensures integrity chaining.
    """
    GENESIS_HASH = "0" * 64

    def __init__(self, log_path: str = "backend/data/audit.log"):
        self.log_path = log_path
        # Ensure data directory exists
        log_dir = os.path.dirname(self.log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
            
        # If file doesn't exist, create it (touch)
        if not os.path.exists(self.log_path):
            with open(self.log_path, 'w') as f:
                pass 
        self.last_hash = self._get_last_hash()

    def _get_last_hash(self) -> str:
        """Read the last entry from the log file to get the previous hash."""
        if not os.path.exists(self.log_path) or os.path.getsize(self.log_path) == 0:
             return self.GENESIS_HASH

        try:
            with open(self.log_path, 'rb') as f:
                f.seek(0, 2)
                file_size = f.tell()
                
                # Read last 2KB to ensure we get a full JSON line
                chunk_size = min(2048, file_size)
                f.seek(file_size - chunk_size)
                last_chunk = f.read(chunk_size)
                lines = last_chunk.split(b'\n')
                # Filter out empty lines from split
                valid_lines = [l for l in lines if l.strip()]
                if not valid_lines:
                    return self.GENESIS_HASH
                
                last_line = valid_lines[-1].decode().strip()
                last_entry = json.loads(last_line)
                return last_entry.get("hash", self.GENESIS_HASH)
            
        except Exception as e:
            logger.error(f"Failed to read last audit hash: {e}")
            return self.GENESIS_HASH

    def log_event(self, action: str, actor: str, details: Dict[str, Any]) -> str:
        """
        Log an audit event. Returns the ID of the new entry.
        """
        entry = AuditEntry(
            action=action,
            actor=actor,
            details=details,
            previous_hash=self.last_hash
        )
        entry.hash = entry.compute_hash()
        
        try:
            # Append entry as a single JSON line
            with open(self.log_path, 'a') as f:
                # model_dump_json doesn't guarantee canonical order, 
                # but we use it for storage convenience. 
                # The hash was computed on canonical data.
                f.write(entry.model_dump_json() + '\n')
            
            self.last_hash = entry.hash
            return entry.id
        except Exception as e:
            logger.critical(f"FATAL: Failed to write to audit log: {e}")
            raise 

    def verify_integrity(self) -> Dict[str, Any]:
        """
        Verifies the hash chain of the entire log file.
        """
        if not os.path.exists(self.log_path) or os.path.getsize(self.log_path) == 0:
            return {"status": "success", "message": "Log is empty", "entries_checked": 0}

        entries_checked = 0
        current_prev_hash = self.GENESIS_HASH
        
        try:
            with open(self.log_path, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line: continue
                    
                    data = json.loads(line)
                    entry = AuditEntry(**data)
                    
                    # 1. Verify previous_hash link
                    if entry.previous_hash != current_prev_hash:
                        return {
                            "status": "failed",
                            "message": f"Broken chain at line {line_num}",
                            "entry_id": entry.id,
                            "expected_prev": current_prev_hash,
                            "actual_prev": entry.previous_hash
                        }
                    
                    # 2. Re-compute and verify current hash
                    computed_hash = entry.compute_hash()
                    if entry.hash != computed_hash:
                        return {
                            "status": "failed",
                            "message": f"Hash mismatch at line {line_num}",
                            "entry_id": entry.id,
                            "stored_hash": entry.hash,
                            "computed_hash": computed_hash
                        }
                    
                    current_prev_hash = entry.hash
                    entries_checked += 1
                    
            return {
                "status": "success",
                "message": "Integrity verified",
                "entries_checked": entries_checked,
                "head_hash": current_prev_hash
            }
        except Exception as e:
            return {"status": "error", "message": str(e), "entries_checked": entries_checked}

# Singleton instance
_audit_logger = None

def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        log_path = os.environ.get("AUDIT_LOG_PATH", "backend/data/audit.log")
        _audit_logger = AuditLogger(log_path)
    return _audit_logger

def log_audit(action: str, actor: str, details: Dict[str, Any]):
    """Convenience function to log an audit event."""
    return get_audit_logger().log_event(action, actor, details)
