-- Schema v6 execution ledger fixture, captured from the PRE-Phase-66 ledger code.
-- base commit: 7e9a7fb846701806b7f937652bb880736597c0a4
-- sqlite 3.47.1; python 3.11.13
-- synthetic data. It proves a ledger written before 66 opens unchanged.
BEGIN TRANSACTION;
CREATE TABLE approval_challenges (
        challenge_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
        workspace TEXT NOT NULL,
        key_id TEXT NOT NULL,
        intent_hash TEXT NOT NULL,
        signed_payload BLOB NOT NULL,
        issued_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace, key_id)
            REFERENCES approval_keys(workspace, key_id)
    );
CREATE TABLE approval_keys (
        workspace TEXT NOT NULL,
        key_id TEXT NOT NULL,
        public_key_x963 BLOB NOT NULL CHECK(length(public_key_x963) = 65),
        created_at TEXT NOT NULL,
        PRIMARY KEY (workspace, key_id)
    );
CREATE TABLE buying_power_reservations (
        proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
        workspace TEXT NOT NULL,
        account TEXT NOT NULL,
        currency TEXT NOT NULL,
        intent_hash TEXT NOT NULL,
        reserved TEXT NOT NULL,
        consumed TEXT NOT NULL DEFAULT '0',
        released TEXT NOT NULL DEFAULT '0',
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
CREATE TABLE dispatch_attempts (
        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL UNIQUE REFERENCES order_intents(proposal_id),
        approval_id TEXT REFERENCES execution_approvals(approval_id),
        state TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        completed_at TEXT,
        acknowledgment_json TEXT
    );
CREATE TABLE execution_admissions (
        proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
        intent_hash TEXT NOT NULL,
        workspace TEXT NOT NULL,
        account TEXT NOT NULL,
        currency TEXT NOT NULL,
        ticker TEXT NOT NULL,
        side TEXT NOT NULL,
        original_quantity TEXT NOT NULL,
        final_quantity TEXT NOT NULL,
        price TEXT NOT NULL,
        notional TEXT NOT NULL,
        simulator_fill_price TEXT NOT NULL,
        simulator_drawdown_pct TEXT NOT NULL,
        risk_quantity TEXT NOT NULL,
        current_spread_pct TEXT NOT NULL,
        evidence_at TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        decision TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (decision IN ('ADMITTED', 'DENIED'))
    );
CREATE TABLE execution_approvals (
        approval_id TEXT PRIMARY KEY,
        challenge_id TEXT NOT NULL UNIQUE
            REFERENCES approval_challenges(challenge_id),
        proposal_id TEXT NOT NULL UNIQUE
            REFERENCES order_intents(proposal_id),
        workspace TEXT NOT NULL,
        key_id TEXT NOT NULL,
        intent_hash TEXT NOT NULL,
        signed_payload_hash TEXT NOT NULL,
        signature_der BLOB NOT NULL,
        approved_at TEXT NOT NULL,
        FOREIGN KEY (workspace, key_id)
            REFERENCES approval_keys(workspace, key_id)
    );
CREATE TABLE execution_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
INSERT INTO "execution_events" VALUES(1,'fixture-1','INTENT_CREATED',NULL,'PENDING','{}','2026-10-06T20:37:47.218971+00:00');
CREATE TABLE ledger_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            workspace TEXT NOT NULL CHECK (workspace IN ('uk', 'india')),
            pinned_at TEXT NOT NULL,
            pinned_by TEXT NOT NULL
                CHECK (pinned_by IN ('first-open', 'operator-confirmed-migration'))
        );
INSERT INTO "ledger_identity" VALUES(1,'uk','2026-10-06T20:37:47.214604+00:00','first-open');
CREATE TABLE order_intents (
        proposal_id TEXT PRIMARY KEY,
        client_order_id TEXT NOT NULL UNIQUE,
        intent_hash TEXT NOT NULL,
        canonical_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    , workspace TEXT GENERATED ALWAYS AS (json_extract(canonical_json, '$.workspace')) VIRTUAL);
INSERT INTO "order_intents" VALUES('fixture-1','growin-fixture-1','1667d1b7e3850cb6c23aa73c9a039868956e4405809195872665f216e291f49c','{"account":"invest","broker":"paper","client_order_id":"growin-fixture-1","intent_version":1,"limit_price":null,"mode":"PAPER","order_type":null,"proposal_id":"fixture-1","quantity":"2","replaces_proposal_id":"","requote_id":"","side":"BUY","ticker":"VUSA","workspace":"uk"}','2026-10-06T20:37:47.218971+00:00');
CREATE TABLE order_projection (
        proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
        state TEXT NOT NULL,
        acknowledgment_json TEXT,
        rejection_notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
INSERT INTO "order_projection" VALUES('fixture-1','PENDING',NULL,NULL,'2026-10-06T20:37:47.218971+00:00','2026-10-06T20:37:47.218971+00:00');
CREATE TABLE paper_budgets (
        workspace TEXT NOT NULL,
        account TEXT NOT NULL,
        currency TEXT NOT NULL,
        amount TEXT NOT NULL,
        reserved TEXT NOT NULL DEFAULT '0',
        consumed TEXT NOT NULL DEFAULT '0',
        released TEXT NOT NULL DEFAULT '0',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (workspace, account, currency)
    );
INSERT INTO "paper_budgets" VALUES('uk','invest','GBP','1000','0','0','0','2026-10-06T20:37:47.219699+00:00','2026-10-06T20:37:47.219699+00:00');
CREATE TABLE paper_positions (
        workspace TEXT NOT NULL,
        account TEXT NOT NULL,
        currency TEXT NOT NULL,
        ticker TEXT NOT NULL,
        quantity TEXT NOT NULL DEFAULT '0',
        notional TEXT NOT NULL DEFAULT '0',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (workspace, account, currency, ticker)
    );
CREATE TABLE reconciliation_evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
        broker_order_id TEXT NOT NULL,
        source TEXT NOT NULL,
        cumulative_quantity TEXT NOT NULL,
        cumulative_notional TEXT NOT NULL,
        status TEXT NOT NULL,
        evidence_fingerprint TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (proposal_id, evidence_fingerprint)
    );
CREATE TABLE requote_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        requote_id TEXT NOT NULL REFERENCES requote_intents(requote_id),
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
CREATE TABLE requote_intents (
        requote_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
        parent_intent_hash TEXT NOT NULL,
        parent_reconciliation_fingerprint TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        snapshot_hash TEXT NOT NULL,
        candidate_json TEXT NOT NULL,
        state TEXT NOT NULL,
        reason_code TEXT NOT NULL DEFAULT '',
        replacement_proposal_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
CREATE TABLE workspace_control_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace TEXT NOT NULL,
        version INTEGER NOT NULL,
        engaged INTEGER NOT NULL CHECK (engaged IN (0, 1)),
        purpose TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (workspace, version)
    );
CREATE TABLE workspace_controls (
        workspace TEXT PRIMARY KEY,
        engaged INTEGER NOT NULL DEFAULT 0 CHECK (engaged IN (0, 1)),
        version INTEGER NOT NULL DEFAULT 0,
        reason_code TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
CREATE UNIQUE INDEX dispatch_attempt_approval_unique
    ON dispatch_attempts(approval_id) WHERE approval_id IS NOT NULL
    ;
CREATE INDEX requote_intents_parent_state
    ON requote_intents(proposal_id, state, created_at)
    ;
CREATE TRIGGER order_intents_no_update
    BEFORE UPDATE ON order_intents
    BEGIN
        SELECT RAISE(ABORT, 'order_intents are immutable');
    END;
CREATE TRIGGER requote_intents_no_delete
    BEFORE DELETE ON requote_intents
    BEGIN
        SELECT RAISE(ABORT, 'requote intents are immutable');
    END;
CREATE TRIGGER requote_events_no_update
    BEFORE UPDATE ON requote_events
    BEGIN
        SELECT RAISE(ABORT, 'requote events are append-only');
    END;
CREATE TRIGGER requote_events_no_delete
    BEFORE DELETE ON requote_events
    BEGIN
        SELECT RAISE(ABORT, 'requote events are append-only');
    END;
CREATE TRIGGER order_intents_no_delete
    BEFORE DELETE ON order_intents
    BEGIN
        SELECT RAISE(ABORT, 'order_intents are immutable');
    END;
CREATE TRIGGER execution_events_no_update
    BEFORE UPDATE ON execution_events
    BEGIN
        SELECT RAISE(ABORT, 'execution_events are append-only');
    END;
CREATE TRIGGER execution_events_no_delete
    BEFORE DELETE ON execution_events
    BEGIN
        SELECT RAISE(ABORT, 'execution_events are append-only');
    END;
CREATE TRIGGER approval_keys_no_update
    BEFORE UPDATE ON approval_keys
    BEGIN
        SELECT RAISE(ABORT, 'approval_keys are immutable');
    END;
CREATE TRIGGER approval_keys_no_delete
    BEFORE DELETE ON approval_keys
    BEGIN
        SELECT RAISE(ABORT, 'approval_keys are immutable');
    END;
CREATE TRIGGER approval_challenges_no_update
    BEFORE UPDATE ON approval_challenges
    BEGIN
        SELECT RAISE(ABORT, 'approval_challenges are immutable');
    END;
CREATE TRIGGER approval_challenges_no_delete
    BEFORE DELETE ON approval_challenges
    BEGIN
        SELECT RAISE(ABORT, 'approval_challenges are immutable');
    END;
CREATE TRIGGER execution_approvals_no_update
    BEFORE UPDATE ON execution_approvals
    BEGIN
        SELECT RAISE(ABORT, 'execution_approvals are immutable');
    END;
CREATE TRIGGER execution_approvals_no_delete
    BEFORE DELETE ON execution_approvals
    BEGIN
        SELECT RAISE(ABORT, 'execution_approvals are immutable');
    END;
CREATE TRIGGER ledger_identity_no_update
            BEFORE UPDATE ON ledger_identity
            BEGIN
                SELECT RAISE(ABORT, 'ledger identity is immutable');
            END;
CREATE TRIGGER ledger_identity_no_delete
            BEFORE DELETE ON ledger_identity
            BEGIN
                SELECT RAISE(ABORT, 'ledger identity is immutable');
            END;
CREATE TRIGGER order_intents_workspace_insert_guard
        BEFORE INSERT ON order_intents
        WHEN json_extract(NEW.canonical_json, '$.workspace') IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
        BEGIN
            SELECT RAISE(ABORT, 'workspace does not match ledger identity');
        END;
CREATE TRIGGER approval_keys_workspace_insert_guard
            BEFORE INSERT ON approval_keys
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER approval_challenges_workspace_insert_guard
            BEFORE INSERT ON approval_challenges
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER execution_approvals_workspace_insert_guard
            BEFORE INSERT ON execution_approvals
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER execution_admissions_workspace_insert_guard
            BEFORE INSERT ON execution_admissions
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER paper_budgets_workspace_insert_guard
            BEFORE INSERT ON paper_budgets
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER buying_power_reservations_workspace_insert_guard
            BEFORE INSERT ON buying_power_reservations
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER workspace_controls_workspace_insert_guard
            BEFORE INSERT ON workspace_controls
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER workspace_control_events_workspace_insert_guard
            BEFORE INSERT ON workspace_control_events
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER paper_positions_workspace_insert_guard
            BEFORE INSERT ON paper_positions
            WHEN NEW.workspace IS NOT (SELECT workspace FROM ledger_identity WHERE singleton = 1)
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER paper_budgets_workspace_update_guard
            BEFORE UPDATE OF workspace ON paper_budgets
            WHEN NEW.workspace IS NOT OLD.workspace
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER buying_power_reservations_workspace_update_guard
            BEFORE UPDATE OF workspace ON buying_power_reservations
            WHEN NEW.workspace IS NOT OLD.workspace
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER workspace_controls_workspace_update_guard
            BEFORE UPDATE OF workspace ON workspace_controls
            WHEN NEW.workspace IS NOT OLD.workspace
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER workspace_control_events_workspace_update_guard
            BEFORE UPDATE OF workspace ON workspace_control_events
            WHEN NEW.workspace IS NOT OLD.workspace
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER paper_positions_workspace_update_guard
            BEFORE UPDATE OF workspace ON paper_positions
            WHEN NEW.workspace IS NOT OLD.workspace
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
CREATE TRIGGER execution_admissions_workspace_update_guard
            BEFORE UPDATE OF workspace ON execution_admissions
            WHEN NEW.workspace IS NOT OLD.workspace
            BEGIN
                SELECT RAISE(ABORT, 'workspace does not match ledger identity');
            END;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('execution_events',1);
COMMIT;
PRAGMA user_version = 6;
