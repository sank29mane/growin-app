-- Schema v5 execution ledger fixture, captured from real Phase 57 ledger code.
-- base commit: 3b9aafc8b1313a1ead5fffc1f5369415aedf27e6
-- execution code last changed in commit: f966ed13c17d55f5235de194d9d93d7da13f3539
-- sqlite 3.47.1; python 3.11.13
-- script: tests/backend/fixtures/capture_ledger_v5.py
-- synthetic data; throwaway key; regenerate only from a pre-Phase-58 checkout
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
INSERT INTO "approval_challenges" VALUES('3a66874f-ebf1-4b2c-a734-1eb78e619be9','fixture-approved','uk','8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec','5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4',X'7B226163636F756E74223A22696E76657374222C2261646D69747465645F7175616E74697479223A2232222C2262726F6B6572223A227061706572222C226368616C6C656E67655F6964223A2233613636383734662D656266312D346232632D613733342D316562373865363139626539222C22636C69656E745F6F726465725F6964223A2267726F77696E2D666978747572652D617070726F766564222C2263757272656E6379223A22474250222C2265766964656E63655F68617368223A2230616537323766386635383431636163666535363733626366616130353139633538623135643761353361663361346365613830623762386239363866643538222C22657870697265735F6174223A313739303934383731302C22696E74656E745F68617368223A2235643361316136633836393333613364373832363331333033626165373038323930653864316638616666613435353733623238393038643263663066636534222C226973737565645F6174223A313739303934383635302C226B65795F6964223A2238613935636433356531353836383365646336383038376535306164653663623163396363353039373633623630353439363038306630643634643661336563222C226C696D69745F7072696365223A6E756C6C2C226D6F6465223A225041504552222C226E6F6E6365223A2253526A484C4A747533492D385946364D48354A45313931546C554E7279585F4255784B4337773866785834222C226E6F74696F6E616C223A223230222C226F726465725F74797065223A6E756C6C2C227072696365223A223130222C2270726F706F73616C5F6964223A22666978747572652D617070726F766564222C22707572706F7365223A2267726F77696E2E657865637574696F6E2E6469737061746368222C227175616E74697479223A2232222C227265706C616365735F70726F706F73616C5F6964223A22222C22726571756F74655F6964223A22222C2273696465223A22425559222C227469636B6572223A2256555341222C2276657273696F6E223A312C22776F726B7370616365223A22756B227D',1790948650,1790948710,'2026-10-02T13:44:10.395401+00:00');
INSERT INTO "approval_challenges" VALUES('25bcf973-40c2-4147-b231-379b566d7f37','fixture-requote-parent','uk','8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec','43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c',X'7B226163636F756E74223A22726571756F74652D66697874757265222C2261646D69747465645F7175616E74697479223A2232222C2262726F6B6572223A227061706572222C226368616C6C656E67655F6964223A2232356263663937332D343063322D343134372D623233312D333739623536366437663337222C22636C69656E745F6F726465725F6964223A2267726F77696E2D666978747572652D726571756F74652D706172656E74222C2263757272656E6379223A22474250222C2265766964656E63655F68617368223A2262363938393237623631346336323239393562623838323032666139393934646434373438363034353731366263386465616264363133323232623762336432222C22657870697265735F6174223A313739303934383731302C22696E74656E745F68617368223A2234336265333561383663346530393930376163383864343238303437333736383863663338303735623030643465626134653438356339313039346338393963222C226973737565645F6174223A313739303934383635302C226B65795F6964223A2238613935636433356531353836383365646336383038376535306164653663623163396363353039373633623630353439363038306630643634643661336563222C226C696D69745F7072696365223A6E756C6C2C226D6F6465223A225041504552222C226E6F6E6365223A22666D335866446A365130565F37696242324C653673452D6978325F5A6530456F715055436E76477459536F222C226E6F74696F6E616C223A223230222C226F726465725F74797065223A6E756C6C2C227072696365223A223130222C2270726F706F73616C5F6964223A22666978747572652D726571756F74652D706172656E74222C22707572706F7365223A2267726F77696E2E657865637574696F6E2E6469737061746368222C227175616E74697479223A2232222C227265706C616365735F70726F706F73616C5F6964223A22222C22726571756F74655F6964223A22222C2273696465223A22425559222C227469636B6572223A2256555341222C2276657273696F6E223A312C22776F726B7370616365223A22756B227D',1790948650,1790948710,'2026-10-02T13:44:10.399190+00:00');
CREATE TABLE approval_keys (
                workspace TEXT NOT NULL,
                key_id TEXT NOT NULL,
                public_key_x963 BLOB NOT NULL CHECK(length(public_key_x963) = 65),
                created_at TEXT NOT NULL,
                PRIMARY KEY (workspace, key_id)
            );
INSERT INTO "approval_keys" VALUES('uk','8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec',X'04AD8D84498FD0B7E510E49CD2A99ABECF1A2A2AD0A891CFA137E3E10BCDDC191A1F05B439735604B5CB65CC218EDD4BB17D58116FA9BEF8F33C3C1848796AA1FD','2026-10-02T13:44:10.394311+00:00');
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
INSERT INTO "buying_power_reservations" VALUES('fixture-approved','uk','invest','GBP','5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4','20','20','0','SETTLED','2026-10-02T13:44:10.395168+00:00','2026-10-02T13:44:10.398012+00:00');
INSERT INTO "buying_power_reservations" VALUES('fixture-pending','uk','invest','GBP','3f0bb63e48b32fd16a73bbb147eabc33fcd7f822262dcc61d3560fd19bc99d60','10','0','0','ACTIVE','2026-10-02T13:44:10.398561+00:00','2026-10-02T13:44:10.398561+00:00');
INSERT INTO "buying_power_reservations" VALUES('fixture-requote-parent','uk','requote-fixture','GBP','43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c','20','0','0','ACTIVE','2026-10-02T13:44:10.399005+00:00','2026-10-02T13:44:10.399005+00:00');
CREATE TABLE dispatch_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL UNIQUE REFERENCES order_intents(proposal_id),
                approval_id TEXT REFERENCES execution_approvals(approval_id),
                state TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                completed_at TEXT,
                acknowledgment_json TEXT
            );
INSERT INTO "dispatch_attempts" VALUES(1,'fixture-approved','23c21588-f8e4-4388-b1aa-e6f3ad7d5d8d','FILLED','2026-10-02T13:44:10.397612+00:00','2026-10-02T13:44:10.397877+00:00','{"broker":"paper","broker_order_id":"paper-7a5ea1897b4e3d47c283ebd31a71762f","idempotent_replay":false,"proposal_id":"fixture-approved","raw":{},"status":"ACKNOWLEDGED"}');
INSERT INTO "dispatch_attempts" VALUES(2,'fixture-requote-parent','6d036d0a-a74c-4985-90fe-584f57eb0d81','ACKNOWLEDGED','2026-10-02T13:44:10.399448+00:00','2026-10-02T13:44:10.399686+00:00','{"broker":"paper","broker_order_id":"paper-a6a83315ca05b82a506c466ce950fafb","idempotent_replay":false,"proposal_id":"fixture-requote-parent","raw":{},"status":"ACKNOWLEDGED"}');
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
INSERT INTO "execution_admissions" VALUES('fixture-approved','5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4','uk','invest','GBP','VUSA','BUY','2','2','10','20','10','0','2','0','2026-10-02T13:44:10.394841+00:00','0ae727f8f5841cacfe5673bcfaa0519c58b15d7a53af3a4cea80b7b8b968fd58','ADMITTED','ADMITTED','2026-10-02T13:44:10.394883+00:00');
INSERT INTO "execution_admissions" VALUES('fixture-pending','3f0bb63e48b32fd16a73bbb147eabc33fcd7f822262dcc61d3560fd19bc99d60','uk','invest','GBP','VUSA','BUY','1','1','10','10','10','0','1','0','2026-10-02T13:44:10.398402+00:00','9acc16e510339cba9a7ff50c3f3848bab38ccd6e6e5ce5a1c63c8d578c161ad0','ADMITTED','ADMITTED','2026-10-02T13:44:10.398431+00:00');
INSERT INTO "execution_admissions" VALUES('fixture-requote-parent','43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c','uk','requote-fixture','GBP','VUSA','BUY','2','2','10','20','10','0','2','0','2026-10-02T13:44:10.398799+00:00','b698927b614c622995bb88202fa9994dd47486045716bc8deabd613222b7b3d2','ADMITTED','ADMITTED','2026-10-02T13:44:10.398823+00:00');
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
INSERT INTO "execution_approvals" VALUES('23c21588-f8e4-4388-b1aa-e6f3ad7d5d8d','3a66874f-ebf1-4b2c-a734-1eb78e619be9','fixture-approved','uk','8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec','5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4','5bae6e076cb9fe6ac1c930b470f2d69a1896936cf6c736c393d8b611701ea7aa',X'3045022100FABD07A618618DD5AC39E9352124BF64A742D2E5424517466C341BAE3D7E776E022064F04236DA66CE433FDE189DE7C95CFE3C5D05BAAF32532F1D77A3819B68E0E7','2026-10-02T13:44:10.397612+00:00');
INSERT INTO "execution_approvals" VALUES('6d036d0a-a74c-4985-90fe-584f57eb0d81','25bcf973-40c2-4147-b231-379b566d7f37','fixture-requote-parent','uk','8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec','43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c','870246f24ee360c023707c2cc14d67dfc0254a5db88ced2be74246aa96cdaab9',X'3046022100A8FF9868200FD012DB4D535145C4E5EA3F9E1583B03E9FCD9E90D0022BFF1F93022100CED94CA345D3744E68A54AAD2C50A6D1275B107632694411C0E06D7FAF8D9829','2026-10-02T13:44:10.399448+00:00');
CREATE TABLE execution_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
                event_type TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
INSERT INTO "execution_events" VALUES(1,'fixture-approved','INTENT_CREATED',NULL,'PENDING','{}','2026-10-02T13:44:10.394552+00:00');
INSERT INTO "execution_events" VALUES(2,'fixture-approved','ADMISSION_DECIDED','PENDING','PENDING','{"decision":"ADMITTED","evidence_hash":"0ae727f8f5841cacfe5673bcfaa0519c58b15d7a53af3a4cea80b7b8b968fd58","final_quantity":"2","intent_hash":"5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4","notional":"20","reason_code":"ADMITTED"}','2026-10-02T13:44:10.394883+00:00');
INSERT INTO "execution_events" VALUES(3,'fixture-approved','BUYING_POWER_RESERVED','PENDING','PENDING','{"account":"invest","currency":"GBP","intent_hash":"5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4","reserved":"20","workspace":"uk"}','2026-10-02T13:44:10.395168+00:00');
INSERT INTO "execution_events" VALUES(4,'fixture-approved','APPROVAL_CHALLENGE_CREATED','PENDING','PENDING','{"challenge_id":"3a66874f-ebf1-4b2c-a734-1eb78e619be9","expires_at_epoch":1790948710,"key_id":"8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec"}','2026-10-02T13:44:10.395401+00:00');
INSERT INTO "execution_events" VALUES(5,'fixture-approved','HUMAN_APPROVAL_VERIFIED','PENDING','PENDING','{"approval_id":"23c21588-f8e4-4388-b1aa-e6f3ad7d5d8d","challenge_id":"3a66874f-ebf1-4b2c-a734-1eb78e619be9","intent_hash":"5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4","key_id":"8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec"}','2026-10-02T13:44:10.397612+00:00');
INSERT INTO "execution_events" VALUES(6,'fixture-approved','DISPATCH_CLAIMED','PENDING','SUBMITTING','{"approval_id":"23c21588-f8e4-4388-b1aa-e6f3ad7d5d8d"}','2026-10-02T13:44:10.397612+00:00');
INSERT INTO "execution_events" VALUES(7,'fixture-approved','BROKER_ACKNOWLEDGED','SUBMITTING','ACKNOWLEDGED','{"broker":"paper","broker_order_id":"paper-7a5ea1897b4e3d47c283ebd31a71762f","idempotent_replay":false,"proposal_id":"fixture-approved","raw":{},"status":"ACKNOWLEDGED"}','2026-10-02T13:44:10.397877+00:00');
INSERT INTO "execution_events" VALUES(8,'fixture-approved','RECONCILIATION_APPLIED','ACKNOWLEDGED','FILLED','{"broker_order_id":"paper-7a5ea1897b4e3d47c283ebd31a71762f","cumulative_notional":"20","cumulative_quantity":"2","evidence_fingerprint":"fixture-fill-1","source":"fixture-capture"}','2026-10-02T13:44:10.398012+00:00');
INSERT INTO "execution_events" VALUES(9,'fixture-pending','INTENT_CREATED',NULL,'PENDING','{}','2026-10-02T13:44:10.398288+00:00');
INSERT INTO "execution_events" VALUES(10,'fixture-pending','ADMISSION_DECIDED','PENDING','PENDING','{"decision":"ADMITTED","evidence_hash":"9acc16e510339cba9a7ff50c3f3848bab38ccd6e6e5ce5a1c63c8d578c161ad0","final_quantity":"1","intent_hash":"3f0bb63e48b32fd16a73bbb147eabc33fcd7f822262dcc61d3560fd19bc99d60","notional":"10","reason_code":"ADMITTED"}','2026-10-02T13:44:10.398431+00:00');
INSERT INTO "execution_events" VALUES(11,'fixture-pending','BUYING_POWER_RESERVED','PENDING','PENDING','{"account":"invest","currency":"GBP","intent_hash":"3f0bb63e48b32fd16a73bbb147eabc33fcd7f822262dcc61d3560fd19bc99d60","reserved":"10","workspace":"uk"}','2026-10-02T13:44:10.398561+00:00');
INSERT INTO "execution_events" VALUES(12,'fixture-requote-parent','INTENT_CREATED',NULL,'PENDING','{}','2026-10-02T13:44:10.398695+00:00');
INSERT INTO "execution_events" VALUES(13,'fixture-requote-parent','ADMISSION_DECIDED','PENDING','PENDING','{"decision":"ADMITTED","evidence_hash":"b698927b614c622995bb88202fa9994dd47486045716bc8deabd613222b7b3d2","final_quantity":"2","intent_hash":"43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c","notional":"20","reason_code":"ADMITTED"}','2026-10-02T13:44:10.398823+00:00');
INSERT INTO "execution_events" VALUES(14,'fixture-requote-parent','BUYING_POWER_RESERVED','PENDING','PENDING','{"account":"requote-fixture","currency":"GBP","intent_hash":"43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c","reserved":"20","workspace":"uk"}','2026-10-02T13:44:10.399005+00:00');
INSERT INTO "execution_events" VALUES(15,'fixture-requote-parent','APPROVAL_CHALLENGE_CREATED','PENDING','PENDING','{"challenge_id":"25bcf973-40c2-4147-b231-379b566d7f37","expires_at_epoch":1790948710,"key_id":"8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec"}','2026-10-02T13:44:10.399190+00:00');
INSERT INTO "execution_events" VALUES(16,'fixture-requote-parent','HUMAN_APPROVAL_VERIFIED','PENDING','PENDING','{"approval_id":"6d036d0a-a74c-4985-90fe-584f57eb0d81","challenge_id":"25bcf973-40c2-4147-b231-379b566d7f37","intent_hash":"43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c","key_id":"8a95cd35e158683edc68087e50ade6cb1c9cc509763b605496080f0d64d6a3ec"}','2026-10-02T13:44:10.399448+00:00');
INSERT INTO "execution_events" VALUES(17,'fixture-requote-parent','DISPATCH_CLAIMED','PENDING','SUBMITTING','{"approval_id":"6d036d0a-a74c-4985-90fe-584f57eb0d81"}','2026-10-02T13:44:10.399448+00:00');
INSERT INTO "execution_events" VALUES(18,'fixture-requote-parent','BROKER_ACKNOWLEDGED','SUBMITTING','ACKNOWLEDGED','{"broker":"paper","broker_order_id":"paper-a6a83315ca05b82a506c466ce950fafb","idempotent_replay":false,"proposal_id":"fixture-requote-parent","raw":{},"status":"ACKNOWLEDGED"}','2026-10-02T13:44:10.399686+00:00');
CREATE TABLE order_intents (
                proposal_id TEXT PRIMARY KEY,
                client_order_id TEXT NOT NULL UNIQUE,
                intent_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
INSERT INTO "order_intents" VALUES('fixture-approved','growin-fixture-approved','5d3a1a6c86933a3d782631303bae708290e8d1f8affa45573b28908d2cf0fce4','{"account":"invest","broker":"paper","client_order_id":"growin-fixture-approved","intent_version":1,"limit_price":null,"mode":"PAPER","order_type":null,"proposal_id":"fixture-approved","quantity":"2","replaces_proposal_id":"","requote_id":"","side":"BUY","ticker":"VUSA","workspace":"uk"}','2026-10-02T13:44:10.394552+00:00');
INSERT INTO "order_intents" VALUES('fixture-pending','growin-fixture-pending','3f0bb63e48b32fd16a73bbb147eabc33fcd7f822262dcc61d3560fd19bc99d60','{"account":"invest","broker":"paper","client_order_id":"growin-fixture-pending","intent_version":1,"limit_price":null,"mode":"PAPER","order_type":null,"proposal_id":"fixture-pending","quantity":"1","replaces_proposal_id":"","requote_id":"","side":"BUY","ticker":"VUSA","workspace":"uk"}','2026-10-02T13:44:10.398288+00:00');
INSERT INTO "order_intents" VALUES('fixture-requote-parent','growin-fixture-requote-parent','43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c','{"account":"requote-fixture","broker":"paper","client_order_id":"growin-fixture-requote-parent","intent_version":1,"limit_price":null,"mode":"PAPER","order_type":null,"proposal_id":"fixture-requote-parent","quantity":"2","replaces_proposal_id":"","requote_id":"","side":"BUY","ticker":"VUSA","workspace":"uk"}','2026-10-02T13:44:10.398695+00:00');
CREATE TABLE order_projection (
                proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
                state TEXT NOT NULL,
                acknowledgment_json TEXT,
                rejection_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
INSERT INTO "order_projection" VALUES('fixture-approved','FILLED','{"broker":"paper","broker_order_id":"paper-7a5ea1897b4e3d47c283ebd31a71762f","idempotent_replay":false,"proposal_id":"fixture-approved","raw":{},"status":"ACKNOWLEDGED"}',NULL,'2026-10-02T13:44:10.394552+00:00','2026-10-02T13:44:10.398012+00:00');
INSERT INTO "order_projection" VALUES('fixture-pending','PENDING',NULL,NULL,'2026-10-02T13:44:10.398288+00:00','2026-10-02T13:44:10.398288+00:00');
INSERT INTO "order_projection" VALUES('fixture-requote-parent','ACKNOWLEDGED','{"broker":"paper","broker_order_id":"paper-a6a83315ca05b82a506c466ce950fafb","idempotent_replay":false,"proposal_id":"fixture-requote-parent","raw":{},"status":"ACKNOWLEDGED"}',NULL,'2026-10-02T13:44:10.398695+00:00','2026-10-02T13:44:10.399686+00:00');
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
INSERT INTO "paper_budgets" VALUES('uk','invest','GBP','1000','10','20','0','2026-10-02T13:44:10.395070+00:00','2026-10-02T13:44:10.398561+00:00');
INSERT INTO "paper_budgets" VALUES('uk','requote-fixture','GBP','1000','20','0','0','2026-10-02T13:44:10.398936+00:00','2026-10-02T13:44:10.399005+00:00');
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
INSERT INTO "paper_positions" VALUES('uk','invest','GBP','VUSA','2','20','2026-10-02T13:44:10.398012+00:00');
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
INSERT INTO "reconciliation_evidence" VALUES(1,'fixture-approved','paper-7a5ea1897b4e3d47c283ebd31a71762f','fixture-capture','2','20','FILLED','fixture-fill-1','2026-10-02T13:44:10.398008+00:00','2026-10-02T13:44:10.398012+00:00');
CREATE TABLE requote_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                requote_id TEXT NOT NULL REFERENCES requote_intents(requote_id),
                event_type TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
INSERT INTO "requote_events" VALUES(1,'fixture-rq-1','REQUOTE_EVALUATED',NULL,'EVALUATED','{"proposal_id":"fixture-requote-parent","snapshot_hash":"fixture-snapshot-1"}','2026-10-02T13:44:10.399861+00:00');
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
INSERT INTO "requote_intents" VALUES('fixture-rq-1','fixture-requote-parent','43be35a86c4e09907ac88d42804737688cf38075b00d4eba4e485c91094c899c','ack:paper-a6a83315ca05b82a506c466ce950fafb','fixture-requote-parent:snapshot-1','fixture-snapshot-1','{"limit_price":"10.05","lower_bound":"9.90","policy_version":"local-paper-v1","side":"BUY","upper_bound":"10.10"}','EVALUATED','','','2026-10-02T13:44:10.399861+00:00','2026-10-02T13:44:10.399861+00:00');
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
INSERT INTO "workspace_control_events" VALUES(1,'uk',1,1,'ENGAGE','MANUAL_KILL','b84ad732-216c-4608-82d5-d26e545f94a3','2026-10-02T13:44:10.400071+00:00');
INSERT INTO "workspace_control_events" VALUES(2,'uk',2,0,'growin.execution.control.clear','','de149ecb-96f9-41a8-9bf5-3628d81a2dba','2026-10-02T13:44:10.400346+00:00');
CREATE TABLE workspace_controls (
                workspace TEXT PRIMARY KEY,
                engaged INTEGER NOT NULL DEFAULT 0 CHECK (engaged IN (0, 1)),
                version INTEGER NOT NULL DEFAULT 0,
                reason_code TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
INSERT INTO "workspace_controls" VALUES('uk',0,2,'','2026-10-02T13:44:10.400346+00:00');
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
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('execution_events',18);
INSERT INTO "sqlite_sequence" VALUES('dispatch_attempts',2);
INSERT INTO "sqlite_sequence" VALUES('reconciliation_evidence',1);
INSERT INTO "sqlite_sequence" VALUES('requote_events',1);
INSERT INTO "sqlite_sequence" VALUES('workspace_control_events',2);
COMMIT;
PRAGMA user_version = 5;
