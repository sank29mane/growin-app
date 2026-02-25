from backend.utils.audit_log import log_audit, get_audit_logger
from decimal import Decimal
import os
import json

# Set test log path
os.environ["AUDIT_LOG_PATH"] = "backend/data/test_audit.log"
if os.path.exists("backend/data/test_audit.log"):
    os.remove("backend/data/test_audit.log")

print("--- Testing Audit Log Chaining ---")

# 1. Log some events
log_audit("TEST_START", "system", {"msg": "Initializing audit test"})
log_audit("FINANCIAL_OP", "trader", {"price": Decimal("123.45"), "qty": 10})
log_audit("ACCOUNT_SWITCH", "user", {"from": "invest", "to": "isa"})

# 2. Verify integrity
logger = get_audit_logger()
result = logger.verify_integrity()
print(f"Integrity Result: {json.dumps(result, indent=2)}")

# 3. Simulate tampering
print("\n--- Simulating Tampering ---")
with open("backend/data/test_audit.log", "r") as f:
    lines = f.readlines()

# Modify the second line's details
data = json.loads(lines[1])
data["details"]["price"] = "999.99"
lines[1] = json.dumps(data) + "\n"

with open("backend/data/test_audit.log", "w") as f:
    f.writelines(lines)

result_tampered = logger.verify_integrity()
print(f"Tampered Integrity Result: {json.dumps(result_tampered, indent=2)}")

if result_tampered["status"] == "failed":
    print("\n✅ Success: Tampering detected!")
else:
    print("\n❌ Failure: Tampering NOT detected!")
