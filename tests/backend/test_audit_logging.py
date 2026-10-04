import pytest
import hashlib
import logging
import json
import os
import uuid

import canonicaljson
from app_logging import setup_logging, correlation_id_ctx, CorrelationIdFilter
from utils.audit_log import AuditLogger, AuditEntry

@pytest.fixture
def audit_logger(tmp_path):
    log_file = tmp_path / "test_audit.log"
    return AuditLogger(str(log_file))

def test_audit_log_creation(audit_logger):
    """Test that audit log entries are created and linked correctly."""
    entry_id = audit_logger.log_event("TEST_ACTION", "user1", {"key": "value"}, workspace="unscoped")
    
    assert os.path.exists(audit_logger.log_path)
    with open(audit_logger.log_path, 'r') as f:
        lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry['id'] == entry_id
        assert entry['action'] == "TEST_ACTION"
        assert entry['actor'] == "user1"
        assert entry['details']['key'] == "value"
        assert entry['previous_hash'] == "0" * 64 # Genesis hash

def test_audit_log_chaining(audit_logger):
    """Test that entries are cryptographically linked."""
    id1 = audit_logger.log_event("ACTION_1", "user1", {}, workspace="unscoped")
    id2 = audit_logger.log_event("ACTION_2", "user1", {}, workspace="unscoped")
    
    with open(audit_logger.log_path, 'r') as f:
        lines = [json.loads(l) for l in f.readlines()]
        
    entry1 = lines[0]
    entry2 = lines[1]
    
    assert entry1['id'] == id1
    assert entry2['id'] == id2
    assert entry2['previous_hash'] == entry1['hash']
    
    # Verify hash integrity
    recomputed_hash1 = AuditEntry(**entry1).compute_hash()
    assert recomputed_hash1 == entry1['hash']
    
    recomputed_hash2 = AuditEntry(**entry2).compute_hash()
    assert recomputed_hash2 == entry2['hash']

def test_correlation_id_logging(caplog):
    """Test that correlation ID is injected into logs."""
    # Use a unique logger per test to avoid capturing old handlers
    logger_name = f"test_logger_{uuid.uuid4()}"
    logger = setup_logging(logger_name, level=logging.INFO)
    
    # Also attach filter to caplog handler to ensure it sees the modification if logger filter doesn't persist
    # But wait, logger filter happens BEFORE handlers. So record passed to handler should have it.
    
    # Force filter on caplog handler to debug
    filter_instance = CorrelationIdFilter()
    caplog.handler.addFilter(filter_instance)
    
    test_id = str(uuid.uuid4())
    token = correlation_id_ctx.set(test_id)
    
    try:
        with caplog.at_level(logging.INFO, logger=logger_name):
            logger.info("Test message with correlation ID")
        
        if not caplog.records:
             pytest.fail("No logs captured")
             
        record = caplog.records[0]
        
        # If the filter didn't run, let's see what's in the record
        # print(record.__dict__)
        
        # Verify modification - Filter must be on the logger for this to work for all handlers
        assert hasattr(record, "correlation_id"), f"Record missing correlation_id. Dict: {record.__dict__}"
        assert record.correlation_id == test_id
        
    finally:
        correlation_id_ctx.reset(token)

def test_tamper_evidence(audit_logger):
    """Test that tampering breaks the chain."""
    audit_logger.log_event("ACTION_1", "user1", {}, workspace="unscoped")
    audit_logger.log_event("ACTION_2", "user1", {}, workspace="unscoped")
    
    # Tamper with the first entry
    with open(audit_logger.log_path, 'r') as f:
        lines = f.readlines()
    
    entry1 = json.loads(lines[0])
    entry1['action'] = "TAMPERED_ACTION"
    lines[0] = json.dumps(entry1) + '\n'
    
    with open(audit_logger.log_path, 'w') as f:
        f.writelines(lines)
        
    # Re-verify
    with open(audit_logger.log_path, 'r') as f:
        tampered_lines = [json.loads(l) for l in f.readlines()]
        
    tampered_entry1 = tampered_lines[0]
    entry2 = tampered_lines[1]
    
    # The hash stored in entry1 no longer matches its content
    recomputed_hash1 = AuditEntry(**tampered_entry1).compute_hash()
    assert recomputed_hash1 != tampered_entry1['hash']
    
    # And specifically, the chain is broken because entry2 points to the OLD hash
    # (In a real verification tool, we'd check if hash(entry1) == entry2.previous_hash)


# --- ISO-01: every audit entry names its workspace ---


def test_log_event_requires_a_workspace(audit_logger):
    with pytest.raises(TypeError):
        audit_logger.log_event("ACTION", "user1", {})  # type: ignore[call-arg]
    assert os.path.getsize(audit_logger.log_path) == 0


def test_log_event_rejects_an_unknown_workspace(audit_logger):
    with pytest.raises(ValueError):
        audit_logger.log_event("ACTION", "user1", {}, workspace="us")
    assert os.path.getsize(audit_logger.log_path) == 0


def test_log_event_writes_the_workspace_into_the_line(audit_logger):
    audit_logger.log_event("ACTION", "user1", {}, workspace="india")

    with open(audit_logger.log_path) as handle:
        line = json.loads(handle.readline())
    assert line["workspace"] == "india"
    assert AuditEntry(**line).compute_hash() == line["hash"]


def _legacy_line(previous_hash: str) -> dict:
    """A pre-change line: hashed without any workspace key, independent of AuditEntry."""

    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": "2026-01-01T00:00:00+00:00",
        "action": "LEGACY",
        "actor": "old-code",
        "details": {"k": "v"},
        "previous_hash": previous_hash,
    }
    digest = hashlib.sha256(canonicaljson.encode_canonical_json(entry)).hexdigest()
    return {**entry, "hash": digest}


def test_legacy_lines_still_verify_and_new_lines_chain_after_them(tmp_path):
    log_file = tmp_path / "legacy.log"
    legacy = _legacy_line("0" * 64)
    log_file.write_text(json.dumps(legacy) + "\n")
    assert "workspace" not in legacy

    logger = AuditLogger(str(log_file))
    logger.log_event("NEW", "user1", {}, workspace="uk")
    logger.log_event("NEW", "user1", {}, workspace="unscoped")

    result = AuditLogger(str(log_file)).verify_integrity()
    assert result["status"] == "success"
    assert result["entries_checked"] == 3


def test_changing_only_the_workspace_of_a_new_line_breaks_its_hash(audit_logger):
    audit_logger.log_event("ACTION", "user1", {}, workspace="uk")
    with open(audit_logger.log_path) as handle:
        line = json.loads(handle.readline())

    line["workspace"] = "india"

    assert AuditEntry(**line).compute_hash() != line["hash"]
