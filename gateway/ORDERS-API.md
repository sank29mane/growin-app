# growin-orders/1

The signed-order contract between the Mac and the Breeze gateway VM. Phase 63 builds the VM half
(`gateway/vm/gateway_vm/orders/`) and the shared test vectors. In Phase 63 the VM verifies and audits an
intent and then refuses to forward it: there is no code path that sends a place, modify or cancel request to
ICICI. Text below the line `## The contract` is copied from 63-CONTEXT (O1 to O8). If the two differ, 63-CONTEXT
wins and this file is wrong.

## The contract

- **O1 Transport.** Same tunnel, Host rule and relay session token as growin-relay/1 C1. Requests carry
  `X-Growin-Relay-Contract: growin-orders/1`; a data route given it, or an order route given growin-relay/1, is 400
  CONTRACT_MISMATCH. Responses carry the C2 headers with this string. JSON bodies only, unknown keys rejected.
- **O2 Routes.** `POST /v1/orders/intents` (O3 body) -> 200 `{contract, challenge_id, signed_bytes_b64,
  expires_at_utc}`. `POST /v1/orders/authorize` `{challenge_id, signature_der_b64}` -> 200 `{contract, decision:
  "VERIFIED_NOT_FORWARDED", intent_id, audit_seq, audit_sha256}`. `POST /v1/orders/halt` `{}` -> 200 `{contract,
  mac_halt: true}`. `GET /v1/orders/state` -> kill state, latch names, drawdown, peak_date, last_evaluated_session
  (numbers only). `GET /v1/orders/audit?after_seq=N` -> at most 200 O7 entries. No route clears a latch, edits
  limits, enrols a key, or places, modifies or cancels an order.
- **O3 Intent.** Exact keys: intent_id `^[A-Za-z0-9_-]{8,96}$` (the Mac client_order_id), proposal_id
  `^[A-Za-z0-9_-]{1,96}$`, workspace `india`, broker `icici-breeze`, mode `SHADOW` (LIVE: 423 `live_disabled`),
  exchange `NSE`, product `cash`, order_type `limit`, validity `day`, side `buy|sell`, stock_code `^[A-Z0-9]{1,10}$`,
  isin `^IN[A-Z0-9]{9}[0-9]$`, quantity JSON integer 1..100000 (bool refused), limit_price string
  `^[0-9]{1,7}(\.[0-9]{1,2})?$` and > 0, reason `entry|exit|halve|flatten|stop`, batch_id `^[a-z0-9-]{8,64}$` or null,
  limits_sha256, params_sha256 and key_id as 64 lowercase hex. Duplicate keys and floats refused.
- **O4 Signed bytes.** Canonical JSON (sorted keys, `,` and `:` separators, ASCII) of `{version: 1, purpose:
  "growin.relay.order", challenge_id, nonce (43 url-safe chars), issued_at, expires_at (epoch s, VM clock, +60),
  key_id, limits_sha256, intent: {O3}}`. ECDSA P-256 / SHA-256, DER. The VM accepts no other purpose; the Mac paper
  path keeps `growin.execution.dispatch` and control clear keeps `growin.execution.control.clear`.
- **O5 Authorize order.** Lock; challenge known (else 409 REPLAY `challenge_unknown`); VM clock before expires_at
  (else `challenge_expired`); mark consumed; verify DER against the pinned key over the stored bytes (else 403
  SIGNATURE_INVALID); intent_id not already consumed (else 409 `intent_consumed`); persist intent_id; re-run every
  check with fresh reads; append audit; forward step (63: refuse); respond. A second mint for an intent with a live
  challenge is 409 `challenge_outstanding`. Any persistence failure is 503 and never a success body.
- **O6 Errors.** Body shape of 61-09 C6 with this contract string. 409 REPLAY; 409 LIMIT_REJECTED (`capital_cap`,
  `per_position_cap`, `collar`, `circuit_band`, `off_tick`, `limits_hash_mismatch`, `key_mismatch`, `isin_mismatch`,
  `instrument_unsupported`, `sell_exceeds_holding`); 423 ORDERS_BLOCKED (`kill_switch`, `halt_latch`, `pilot_ended`, `stop_open`, `mac_halt`,
  `account_mismatch`, `session_closed`, `live_disabled`); 403 SIGNATURE_INVALID; 429 CHALLENGE_CAPACITY; 503
  ORDERS_UNAVAILABLE (`config_invalid`, `state_unreadable`, `state_unwritable`, `audit_broken`, `quote_unavailable`,
  `tick_reference_unavailable`, `account_read_failed`, `clock_inconsistent`); plus the inherited 401, 403, 400, 422 and 503 session, egress and budget codes.
- **O7 Audit entry.** seq, prev_sha256, entry_sha256, at_utc, route, intent_id, proposal_id, intent_sha256, key_id,
  side, stock_code, isin, quantity, limit_price, reason, batch_id, decision (`CHALLENGED`, `REFUSED`,
  `VERIFIED_NOT_FORWARDED`, `HALTED`, `RESET`, `EVALUATED`), codes, limits_sha256, kill, latches. No quote values,
  tokens, account ids or IPs.
- **O8 Versioning.** A breaking change (including a new forward decision in 64) becomes `growin-orders/2`.

## What the VM does with an intent (63-01)

- **Mint** (`POST /v1/orders/intents`) parses the body strictly, refuses a `limits_sha256` or `key_id` that is not
  the VM's own, refuses an intent id that was already authorized, refuses a second live challenge for the same intent
  and a fifth outstanding challenge, then runs every rule check with fresh reads (kill switch, risk state, account,
  quote, IST clock). Only then does it build the O4 bytes and keep its own copy.
- **Authorize** (`POST /v1/orders/authorize`) follows O5 in order. After a valid signature it persists the intent id
  and re-runs every rule check with fresh reads: nothing from mint is reused. A cap breach (including open-buy
  pending notional), a collar breach on either side, the 15:10 IST cutoff and `account_mismatch` each refuse here
  with their O6 code even though mint passed.
- **Reason codes.** A check returns every applicable code in a fixed order; the first is the answer and all of them
  are audited. Order: `kill_switch`, `mac_halt`, `account_mismatch`, `pilot_ended`, `stop_open`, `halt_latch`,
  `session_closed`, then `quote_unavailable`, `instrument_unsupported`, `isin_mismatch`, `tick_reference_unavailable`
  or `off_tick`, `circuit_band`,
  `collar`, then `capital_cap`, `per_position_cap` (buys) or `sell_exceeds_holding` (sells).
- **Codes with no extra name.** The two O6 categories without a sub-code carry their own name as the code, in
  lower case: `signature_invalid` (403 SIGNATURE_INVALID) and `challenge_capacity` (429 CHALLENGE_CAPACITY). A body
  that fails the strict parse answers 422 INTENT_INVALID with a short code (`missing_key`, `extra_key`,
  `duplicate_key`, `float_not_allowed`, `non_ascii`, `bad_type`, `bad_field:NAME` and similar); the code never
  echoes a value.
- **Clocks.** Both routes sample the VM wall clock and a monotonic clock before the reads, after the reads, and
  after the blocking audit write, and judge the 15:10 IST cutoff and the 60 s challenge lifetime on the last
  sample, so a slow write can never turn into a success. The lifetime is enforced on the wall clock (the signed
  `expires_at`) and on a monotonic deadline kept with the in-process challenge; either one expiring is
  `challenge_expired`. A wall sample earlier than the previous one or earlier than `issued_at`, a monotonic sample
  that goes backward, an unusable monotonic reading, or a challenge with no monotonic reference (lost after a
  restart) is 503 `clock_inconsistent`, audited as REFUSED. An authorize that is refused after its
  `VERIFIED_NOT_FORWARDED` entry was written is followed by a REFUSED entry; in 63 nothing is forwarded either way.
- **Latches.** `halt` (drawdown at or below -8%) refuses buys and allows sells. `ended` (at or below -15%) is sells
  only and terminal. A `stop` latch (close at or below cost x 0.88) makes that ISIN sell-only. While any stop exit is
  open, every buy on every ISIN is refused `stop_open`; the latch clears when the trade list shows the exit fill, with
  no admin reset. A verified sell never clears `halt` or a stop. An exit still open at 15:30 IST raises an operator
  alert through the same channel as halt and kill-switch alerts. `mac_halt` and `account_mismatch` refuse everything.
  For example, an open INFY stop blocks a RELIANCE buy until the stop is cleared.
  Only the admin CLI clears `halt`, `stop`, `mac_halt` and `account_mismatch`; it refuses `ended`. It also refuses
  `reset --latch halt` while drawdown is at or below -8%, because the halt would latch again at the next close.
  `--rebase-halt-anchor` overrides that: it sets a separate halt anchor (equity at the last evaluated close) that
  only the -8% test reads, and writes `rebase_halt_anchor` into the RESET audit entry. The true peak never moves,
  so the -15% end is still measured from the real high-water mark. Once `ended` is set, `reset --latch halt` is refused with or without the flag (the Mac refuses it
  the same way: "halt cannot be released while the pilot is ended").
- **Caps.** Only buys count toward `capital_cap` and `per_position_cap`: deployed = position cost basis + open buy
  pending notional + this order. The cost basis of a position is the larger of the broker holding and the VM fill
  ledger, because holdings lag fills by a day. A sell is never refused for being over a cap; it is refused only when
  it exceeds the held quantity minus open sells.
- **Tick band reference.** The tick table is keyed by the closing price on the last trading day of the previous calendar
  month (or the exchange's dated tick reference), not the quote's previous close. The guard reads it from an injected
  `TickReferencePort` on every check; if the port cannot supply it the answer is 503 `tick_reference_unavailable`.
  Every evaluator vector carries a `tick_reference` field (null means unavailable).
- **Kill switch.** The reader asks the instance metadata server for `growin-order-relay` on every check and passes
  only the exact body `enabled`. It never reads a project-level key.
- **State.** `state.json` and `audit.jsonl` live in the systemd StateDirectory. Unreadable or corrupt state blocks
  every order (503 `state_unreadable`); a failed audit write blocks it (503 `audit_broken`). The audit
  log is anchored: `state.json` carries the expected entry count and head hash, written after each entry is fsynced.
  A missing log, a log shorter than the anchor, or a rewritten history refuses every mint, authorize and admin reset
  (503 `audit_broken`); one log entry beyond the anchor and one unterminated tail line are crash artifacts and are
  tolerated, the tail being dropped by the next append.
- **Admin.** `python -m gateway_vm.orders.admin STATE_DIR status | verify-audit | reset --latch NAME [--rebase-halt-anchor] | session-end-check`,
  run from the VM shell in an admin window. No HTTP route does any of it.
- **Vectors.** `tests/backend/fixtures/relay_orders/signing_vectors.json` (canonical bytes, signatures, negatives) and
  `limits_vectors.json` (rule decisions with expected codes, drawdown paths, `limits_sha256`). Both are TEST ONLY.

## Consumers

- **63-02, Mac risk module.** Reads both vector files and must reach the same canonical bytes, the same
  `limits_sha256` and the same decision codes, using its own code.
- **63-03, Swift signer.** Reads `signing_vectors.json`: checks purpose and `key_id` first, shows fields parsed from
  the bytes it signs, signs those exact bytes with the Secure Enclave key (DER output).
- **63-05, Mac relay client and VM routes.** Wraps the pipeline in the three routes above, binds the operator alert
  channel, and sends the Mac's own signature to `/v1/orders/authorize` (P-20).
- **64, preview forwarding.** A new forward decision is a breaking change and ships as `growin-orders/2` (O8).
