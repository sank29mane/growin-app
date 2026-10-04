import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from execution import (
    ApprovalConflict,
    ApprovalService,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
    WorkspaceMismatch,
)


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
            uk_approval.enroll_key(
                public, uk_approval.enrollment_token_path.read_bytes(), workspace="uk"
            )
            assert uk.engage_workspace_control("MANUAL_KILL", workspace="uk").engaged
            assert india.get_workspace_control(workspace="india").engaged is False
            challenge = uk_approval.create_control_challenge(workspace="uk")
            with pytest.raises(Exception, match="invalid"):
                uk_approval.clear_workspace_control(challenge, b"bad", workspace="uk")
            signature = key.sign(challenge.signed_payload, ec.ECDSA(hashes.SHA256()))
            uk_approval.clear_workspace_control(challenge, signature, workspace="uk")
            assert uk.get_workspace_control(workspace="uk").engaged is False
            assert india.get_workspace_control(workspace="india").engaged is False


def test_engaged_workspace_blocks_admission_and_reservation(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        ledger.configure_paper_budget("invest", "GBP", "1000", workspace="uk")
        service = ExecutionService(PaperDispatcher(), ledger)
        ledger.engage_workspace_control("MANUAL_KILL", workspace="uk")
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


def test_ledger_kill_switch_calls_require_the_pinned_workspace(tmp_path):
    with ExecutionLedger(tmp_path / "uk.sqlite3", workspace="uk") as uk:
        with pytest.raises(WorkspaceMismatch):
            uk.engage_workspace_control("MANUAL_KILL", workspace="india")
        with pytest.raises(WorkspaceMismatch):
            uk.get_workspace_control(workspace="india")
        with pytest.raises(WorkspaceMismatch):
            uk.clear_workspace_control(workspace="india", version=1, evidence_id="e")
        control = uk.get_workspace_control(workspace="uk")
        assert control.engaged is False
        assert control.version == 0

        with pytest.raises(TypeError):
            uk.engage_workspace_control("MANUAL_KILL")
        with pytest.raises(TypeError):
            uk.get_workspace_control()
        with pytest.raises(TypeError):
            uk.clear_workspace_control(version=1, evidence_id="e")
        assert uk.get_workspace_control(workspace="uk").engaged is False


def test_service_kill_switch_refuses_the_other_workspace(tmp_path):
    with ExecutionLedger(
        tmp_path / "uk.sqlite3", workspace="uk", require_approval=True
    ) as uk:
        service = ExecutionService(PaperDispatcher(), uk, require_approval=True)

        with pytest.raises(WorkspaceMismatch):
            service.engage_workspace_control(workspace="india")
        with pytest.raises(WorkspaceMismatch):
            service.create_control_challenge(workspace="india")
        with pytest.raises(TypeError):
            service.engage_workspace_control()

        assert uk.get_workspace_control(workspace="uk").engaged is False
        assert service.engage_workspace_control(workspace="uk").engaged is True


@pytest.mark.parametrize("shared_key", [False, True])
def test_india_control_challenge_cannot_clear_the_uk_switch(tmp_path, shared_key):
    uk_key, india_key = key_material(), key_material()
    if shared_key:
        # Even if one public key were enrolled on both sides, the signed
        # payload names india, so the UK ledger must still refuse it.
        india_key = uk_key
    with ExecutionLedger(tmp_path / "uk.sqlite3", workspace="uk", require_approval=True) as uk:
        with ExecutionLedger(
            tmp_path / "india.sqlite3", workspace="india", require_approval=True
        ) as india:
            uk_approval, india_approval = ApprovalService(uk), ApprovalService(india)
            uk_approval.enroll_key(
                uk_key[1], uk_approval.enrollment_token_path.read_bytes(), workspace="uk"
            )
            india_approval.enroll_key(
                india_key[1],
                india_approval.enrollment_token_path.read_bytes(),
                workspace="india",
            )
            uk.engage_workspace_control("MANUAL_KILL", workspace="uk")
            india.engage_workspace_control("MANUAL_KILL", workspace="india")
            challenge = india_approval.create_control_challenge(workspace="india")
            signature = india_key[0].sign(
                challenge.signed_payload, ec.ECDSA(hashes.SHA256())
            )

            with pytest.raises(ApprovalConflict):
                uk_approval.clear_workspace_control(challenge, signature, workspace="uk")
            with pytest.raises(WorkspaceMismatch):
                uk_approval.clear_workspace_control(challenge, signature, workspace="india")

            assert uk.get_workspace_control(workspace="uk").engaged is True
            assert india.get_workspace_control(workspace="india").engaged is True
