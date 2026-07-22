import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from execution import ApprovalService, ExecutionLedger, ExecutionService, PaperDispatcher


def key_material():
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return key, public


def test_workspace_switch_isolated_and_clear_requires_purpose_bound_signature(tmp_path):
    uk_db = tmp_path / "uk.sqlite3"
    india_db = tmp_path / "india.sqlite3"
    with ExecutionLedger(uk_db, workspace="uk", require_approval=True) as uk:
        with ExecutionLedger(india_db, workspace="india", require_approval=True) as india:
            uk_approval = ApprovalService(uk)
            key, public = key_material()
            uk_approval.enroll_key(public, uk_approval.enrollment_token_path.read_bytes())
            assert uk.engage_workspace_control("MANUAL_KILL").engaged
            assert india.get_workspace_control().engaged is False
            challenge = uk_approval.create_control_challenge()
            with pytest.raises(Exception, match="invalid"):
                uk_approval.clear_workspace_control(challenge, b"bad")
            signature = key.sign(challenge.signed_payload, ec.ECDSA(hashes.SHA256()))
            uk_approval.clear_workspace_control(challenge, signature)
            assert uk.get_workspace_control().engaged is False
            assert india.get_workspace_control().engaged is False


def test_engaged_workspace_blocks_admission_and_reservation(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        ledger.configure_paper_budget("invest", "GBP", "1000")
        service = ExecutionService(PaperDispatcher(), ledger)
        ledger.engage_workspace_control("MANUAL_KILL")
        with pytest.raises(Exception, match="control"):
            service.admit(
                {
                "proposal_id": "blocked",
                "workspace": "uk",
                "account": "invest",
                "broker": "paper",
                "mode": "PAPER",
                "ticker": "VUSA",
                "action": "BUY",
                "quantity": "1",
                },
                currency="GBP",
                price="10",
                simulator_evidence={"simulated_fill_price": "10"},
                risk_evidence={"scaled_size": "1"},
            )
