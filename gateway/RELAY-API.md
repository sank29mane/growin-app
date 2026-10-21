# Relay API: contract growin-relay/1

Repo copy of the contract between the Mac and the Mumbai VM relay, plus what the
upstream actually did when it was probed. The server is `gateway/vm/gateway_vm/`
(`contract.py` holds its constants, `app.py` the routes). The only Mac-side client
is `RelayClient` in `backend/brokers/breeze_gateway/client.py`; later phases import
it and build no other. `tests/backend/test_gateway_relay_client.py` checks that the
client and the VM constants cannot drift apart.

Status: the VM serves health, login arm and complete, revoke and bars today. The
client already implements budget, quotes, preview-order and security-master, tested
against canned relay answers, because those four routes arrive on the VM side in a
later change. If any text below disagrees with `gateway_vm/contract.py`, fix the
disagreement in one change; the contract string only moves on a breaking change (C8).

## Contract growin-relay/1

### C1. Transport and trust
- The relay listens on the VM's nic0 internal IPv4 address, port 8443, and nowhere else (OD-16). The address is read from the metadata server at start and must be RFC 1918; the relay refuses 0.0.0.0, loopback, link-local and every public address. One VPC firewall rule allows tcp:8443 only from the IAP range 35.235.240.0/20, and no other effective firewall rule covers that port. Daily use needs no SSH and no shell.
- The Mac reaches the relay only through an IAP tunnel that `RelayClient.open()` starts itself: `gcloud compute start-iap-tunnel <instance> 8443 --local-host-port=127.0.0.1:<random free port> --zone=<zone> --project=<project> --account=<daily account>`. The client never connects to a port its own tunnel child did not open (anti-squatter rule). The daily identity holds only `roles/iap.tunnelResourceAccessor` with the IAM condition `destination.port == 8443`: it cannot SSH, has no shell and cannot mint the VM service account's token (OD-16, OD-8).
- Reuse (OD-3, OD-7): one tunnel per `RelayClient` working session, keep-alive connections over it, and at most 4 requests in flight.
- Relay token (OD-16): the relay mints a 256-bit token at `POST /v1/login/arm` and returns it once. It is bound to that attempt, becomes the session's token when `login/complete` succeeds, and dies with the session (revoke, restart, IST deadline, session-dead error). Header form: `Authorization: Bearer <43 characters of [A-Za-z0-9_-]>`. Token column in C3: `GET /v1/health` needs none. `POST /v1/login/arm` needs the session token only while a session is active. `POST /v1/login/complete` needs the token returned by the arm it completes. Every other route needs the session token whenever a session is bound; with no session bound, the Breeze-backed routes answer with the 503 codes of C6 and budget and revoke answer 200 without a token. A missing, malformed or wrong token is 401 UNAUTHORIZED, compared in constant time, and a failed check never consumes a login attempt. The relay keeps only a sha256 digest of each token and never logs it.
- Every request carries `X-Growin-Relay-Contract: growin-relay/1`. Missing: 403 FORBIDDEN_CLIENT. Any other value: 400 CONTRACT_MISMATCH. `Host` must be `127.0.0.1`, `localhost` or `[::1]`, with or without `:<port>` (the tunnel is a TCP pass-through, so the Mac client's own Host header arrives unchanged); otherwise 403 FORBIDDEN_CLIENT. POST bodies must be `Content-Type: application/json`; otherwise 422 VALIDATION. No CORS headers, redirects or cookies, ever.
- The relay stores no market data. Its only file is the day counter (C7). No cache and no upstream body on disk: each upstream body is held in memory for one request only (OD-17). The Mac caller stores what it fetches, on the Mac.

### C2. Headers on every response
- `X-Growin-Relay-Contract: growin-relay/1`
- `X-Growin-Relay-Request-Id: <16 lowercase hex>`
- `Cache-Control: no-store`

The client checks `X-Growin-Relay-Contract` before it looks at the status or the body. Missing or different: `RelayContractMismatch`, no retry.

### C3. Routes

| Method | Path | Parameters | Token | Success |
|--------|------|------------|-------|---------|
| GET | /v1/health | none | none | 200 JSON health |
| GET | /v1/budget | none | session | 200 JSON budget |
| POST | /v1/login/arm | body `{}` | session, only while a session is active | 200 JSON arm |
| POST | /v1/login/complete | body `{"attempt_id": str, "apisession": str}` | the arm's token | 200 JSON session |
| POST | /v1/session/revoke | body `{}` | session | 200 JSON session |
| GET | /v1/bars/daily | `stock_code`, `from`, `to`, `exchange` (optional) | session | 200 upstream bytes |
| GET | /v1/quotes | `stock_code`, `exchange` (optional) | session | 200 upstream bytes |
| GET | /v1/preview-order | `stock_code`, `action`, `quantity`, `price`, `exchange` (optional) | session | 200 upstream bytes |
| GET | /v1/security-master | none | session | 200 upstream bytes (zip) |

"session" means the session token whenever a session is bound (C1); with none bound, the Breeze-backed routes answer with the 503 codes of C6, while budget and revoke answer 200 without a token.

Parameter rules (any violation is 422 VALIDATION with `fields` listing names only):
- Only the listed query names, each at most once. JSON bodies reject unknown keys.
- `stock_code`: `^[A-Z0-9]{1,10}$` (the Breeze ShortName, for example RELIND, not RELIANCE).
- `exchange`: exactly `NSE` when present.
- `from`, `to`: `YYYY-MM-DD` calendar dates, `from <= to`, `to - from <= 3000` days.
- `action`: `buy` or `sell`. `quantity`: integer 1 to 100000. `price`: decimal string, greater than 0, at most 2 decimal places, at most 1000000.
- `attempt_id`: 16 to 64 characters of `[A-Za-z0-9_-]`. `apisession`: the relay's API_Session filter, `^[A-Za-z0-9]{6,64}$` (OD-14, tightened from `{6,256}` by the 61-04 smoke: 8 digits observed). Mac and VM use the same pattern.

Route semantics:
- bars: Breeze v2 historicalcharts only (never v1), interval daily, product cash. The relay sends `from_date=<from>T00:00:00.000Z` and `to_date=<to>T23:59:59.000Z`, widened by one day on a side where the 61-04 smoke showed that boundary day missing; the exact values are in `X-Growin-Upstream-Request`. Callers request a wider window and filter rows by parsed trading date. Observed upstream behaviour (datetime format, raw or adjusted, 1000-row cap) is in `gateway/RELAY-API.md`, section "Observed upstream behaviour".
- quotes: Breeze v1 quotes, product cash. preview-order: Breeze v1 preview_order, product cash, order_type limit; it is a brokerage calculator and places nothing.
- security-master: the ICICI SecurityMaster.zip download; needs a fresh egress check but no Breeze session; at most 6 per rolling hour.
- No route places, modifies or cancels an order, and there are no funds, holdings, positions or trades routes. Order routes arrive in Phase 63 under a new contract version.

### C4. Upstream byte responses (bars, quotes, preview-order, security-master)
The relay checks the Breeze JSON `Status` on every Breeze response (OD-18). Only a good body (Status 200, Error null or empty, Success present and not an empty string) is returned, as the exact upstream bytes, unmodified. Content-Type is `application/json` for the three Breeze routes and `application/zip` for the security master. Headers:
- `X-Growin-Upstream-Sha256`: 64 lowercase hex, sha256 of the body as sent.
- `X-Growin-Upstream-Fetched-At`: UTC time the upstream body finished arriving, `YYYY-MM-DDTHH:MM:SS.mmmZ`.
- `X-Growin-Upstream-Api`: `breeze-historicalcharts-v2`, `breeze-quotes-v1`, `breeze-preview-order-v1` or `icici-security-master-zip`.
- `X-Growin-Upstream-Request`: compact JSON (sorted keys, ASCII only) of the exact non-secret parameters sent upstream (for the master: `{"url": "<url>"}`). Never contains the AppKey, session token, checksum or secret.
- `X-Growin-Upstream-Interval: 1day` (bars only; the semantic interval, whatever wire value the smoke chose).
- `X-Growin-Breeze-Status: 200` (Breeze routes only).
- `X-Growin-Empty: true` or `false` (Breeze routes only): true when Success is an empty list or object. Callers treat true as "flag and retry once", never as zero volume.
- `X-Growin-Upstream-Last-Modified` (security master only, copied when upstream sent it).

The client recomputes sha256 over the received body and compares it with the header using `hmac.compare_digest` (mismatch: `RelayHashMismatch`), requires every header listed for the route (missing or malformed: `RelayProtocolError`), and returns an `UpstreamPayload`.

### C5. JSON response bodies (each carries `"contract": "growin-relay/1"`)
- health: `{"contract", "state": "none" | "active" | "expired" | "suspended_egress", "login_pending": bool, "expires_at_ist": ISO 8601 or null, "egress": {"ok": bool or null, "checked_at_utc": ISO 8601 or null}, "server_time_utc": ISO 8601, "version": str}`. No token, id, IP or name.
- budget: `{"contract", "calls_last_minute", "calls_today_ist", "per_minute_limit", "per_day_limit", "remaining_today", "ist_date"}` (integers, except `ist_date` as `YYYY-MM-DD`).
- arm: `{"contract", "attempt_id", "login_url", "relay_token", "expires_at_utc"}`. `login_url` contains the AppKey (OD-9) and `relay_token` is the bearer secret (C1): the client never logs, prints or stores either except the token file of C9, and their reprs hide them.
- session (complete and revoke): `{"contract", "state", "expires_at_ist"}` (`expires_at_ist` is null after revoke).

### C6. Errors
Body: `{"contract": "growin-relay/1", "error": {"code", "reason", "breeze_status", "retryable", "retry_after_s", "fields"}}`. `reason` is a closed enum or null; `breeze_status` is the numeric JSON Status or null; `fields` lists parameter names (422 only) or null. There is no free-text message. No error carries a credential, token, account id, IP, request body or Breeze Error text.

| HTTP | code | When | reason values | retryable |
|------|------|------|---------------|-----------|
| 401 | UNAUTHORIZED | relay token missing, malformed or wrong while a session is bound, or wrong on login/complete, login/arm (session active) or revoke | null | false |
| 503 | NO_SESSION | no session (never logged in, revoked, relay restarted) | null | false |
| 503 | SESSION_EXPIRED | past min(24 h, next midnight IST), or Breeze reported the session dead on a data route | deadline, invalid_session, resource_not_available | false |
| 503 | EGRESS_MISMATCH | egress check failed or unavailable, or Breeze reported an IP mismatch | egress_mismatch, egress_unavailable, ip_mismatch | false |
| 503 | BUDGET_UNAVAILABLE | the persisted day counter cannot be read or written; Breeze not called (OD-4) | counter_unreadable | false |
| 429 | BUDGET_EXHAUSTED | relay ceiling reached; Breeze not called | budget_minute, budget_day, security_master_hourly | true, with retry_after_s |
| 502 | UPSTREAM_ERROR | upstream non-200 HTTP, unparsable body, or Breeze logical error (on login/complete this includes a rejected code) | upstream_http, upstream_shape, no_data, invalid_session, public_key_missing, other | true only for no_data |
| 504 | UPSTREAM_TIMEOUT | upstream timed out, was unreachable, or no upstream connection freed up within 10 s | upstream_timeout, upstream_unreachable | true |
| 422 | VALIDATION | bad parameters or body | null | false |
| 409 | LOGIN_ATTEMPT_INVALID | attempt missing, wrong, expired, already used, or superseded by a later arm, a revoke or a drop | null | false |
| 403 | IDENTITY_MISMATCH | CustomerDetails user id differs, or Equity or Trading not enabled | identity_mismatch, segment_disabled | false |
| 403 | FORBIDDEN_CLIENT | missing contract header or non-loopback Host | null | false |
| 400 | CONTRACT_MISMATCH | request contract header has another value | null | false |
| 404, 405, 500 | NOT_FOUND, METHOD_NOT_ALLOWED, INTERNAL | as named | null | false |

### C7. Limits and timeouts
- Relay ceilings (OD-4): 80 Breeze calls per rolling minute (in memory) and 4,500 per IST day, counting every Breeze call including the login exchange. The day count is authoritative across every caller and survives restarts: the relay persists the IST date and the count in `counter.json` under its systemd StateDirectory, writes it before the Breeze call, and refuses every Breeze call with 503 BUDGET_UNAVAILABLE when the file cannot be read or written. The caller's own counter (Phase 59 paces at 50 per minute and 3,500 per day) is advisory pacing. Security master: 6 per rolling hour (in memory), not counted as Breeze calls. A restart resets the minute window and the hourly count, not the day count.
- Relay upstream (OD-3, OD-7): persistent HTTPS connections in a pool of at most 4 per upstream host, so at most 4 Breeze calls are in flight; a fifth waits up to 10 s, then 504 UPSTREAM_TIMEOUT. Route handlers are plain `def` (run in the threadpool), so no blocking HTTP call runs on the server event loop. Timeouts: connect 5 s, read 30 s, total 35 s for Breeze; total 120 s and 64 MiB for the security master; 8 MiB for Breeze JSON.
- Client: one tunnel and one `httpx.Client` per `RelayClient`, `max_connections=4` and `max_keepalive_connections=4`, a `threading.BoundedSemaphore(4)` around every request; timeouts connect 5 s, read 150 s, write 10 s, pool 5 s; body cap 64 MiB; `follow_redirects=False`; `trust_env=False`.

### C8. Versioning
The string becomes `growin-relay/2` on any breaking change: removing or renaming a route, parameter, header, field or code; changing a meaning; adding authentication. Adding a new route or a new optional response header keeps version 1. The client fails closed on any other version and never negotiates down. The relay token, the internal-IP transport and the persisted counter (OD-16, OD-4) landed before the first deployment or consumer, so they are part of version 1.

### C9. Python client API
Import from `brokers.breeze_gateway` (the backend root is on `sys.path` in tests and CLI runs: `PYTHONPATH=backend`). Phase 59 part 2 uses `RelayClient.open()`, `daily_bars`, `security_master`, `health` and `budget`, and maps `UpstreamPayload.provenance()` into its own source records (source, symbol, ISIN, fetch time, hash; PDAT-01, PDAT-02).

```python
CONTRACT = "growin-relay/1"

class RelayClient:
    CONTRACT = CONTRACT
    @classmethod
    def open(cls, config: GatewayConfig | None = None, *, token_store: TokenStore | None = None) -> "RelayClient": ...
        # loads ~/.config/growin/gateway.json when config is None, starts its own IAP tunnel
        # (gcloud compute start-iap-tunnel, pinned to the daily account) on a random free loopback port,
        # waits for /v1/health, loads a saved relay token from the token store (FileTokenStore by default)
        # while it is still valid, returns a client that owns the tunnel (use `with`)
    def __init__(self, *, tunnel: TunnelHandle | None = None, http: httpx.Client | None = None,
                 token_store: TokenStore | None = None) -> None: ...
        # exactly one of tunnel or http. http is the test seam: fastapi.testclient.TestClient(app,
        # base_url="http://127.0.0.1:8443") or httpx.Client(transport=httpx.MockTransport(fn),
        # base_url="http://127.0.0.1:8443"). token_store defaults to an in-memory store for the http seam.
        # A client holding no token sends no Authorization header.
    def health(self) -> RelayHealth: ...                        # no token
    def budget(self) -> RelayBudget: ...                        # session token
    def login_arm(self) -> LoginAttempt: ...                    # session token only if it holds one; keeps the new relay token in memory
    def login_complete(self, attempt_id: str, apisession: str) -> SessionStatus: ...
        # sends the kept relay token; on success saves it to the token store with the IST expiry
    def revoke(self) -> SessionStatus: ...                      # session token; clears the stored token
    def daily_bars(self, stock_code: str, from_date: date, to_date: date, *, exchange: str = "NSE") -> UpstreamPayload: ...
    def quote(self, stock_code: str, *, exchange: str = "NSE") -> UpstreamPayload: ...
    def preview_order(self, stock_code: str, *, action: str, quantity: int, price: Decimal,
                      exchange: str = "NSE") -> UpstreamPayload: ...
    def security_master(self) -> UpstreamPayload: ...
    def close(self) -> None: ...                 # also stops the tunnel it owns
    def __enter__(self) -> "RelayClient": ...
    def __exit__(self, *exc: object) -> None: ...
    # A 401, NO_SESSION or SESSION_EXPIRED response clears the stored token, but only when the store still
    # holds the token this client sent.

class TokenStore(Protocol):
    def load(self) -> "StoredToken | None": ...      # StoredToken(token: str = field(repr=False), expires_at_ist: datetime)
    def save(self, token: str, expires_at_ist: datetime) -> None: ...
    def clear(self) -> None: ...
class MemoryTokenStore: ...
class FileTokenStore:                              # default path ~/.config/growin/relay-session.json
    def __init__(self, path: Path | None = None) -> None: ...
        # accepts a file only when it is mode 0600, owned by the current user, in a 0700 directory, and unexpired

@dataclass(frozen=True)
class UpstreamPayload:
    body: bytes; sha256: str; fetched_at_utc: datetime; upstream_api: str
    upstream_request: Mapping[str, str]; upstream_interval: str | None; breeze_status: int | None
    empty: bool; upstream_last_modified: str | None; request_id: str; route: str
    query: Mapping[str, str]; contract: str
    def provenance(self) -> dict: ...
        # {"source": "breeze_relay", "contract", "route", "query", "upstream_api", "upstream_request",
        #  "fetched_at_utc" (ISO Z), "sha256", "request_id"}

@dataclass(frozen=True)
class RelayHealth:
    state: str; login_pending: bool; expires_at_ist: datetime | None
    egress_ok: bool | None; egress_checked_at_utc: datetime | None; server_time_utc: datetime; version: str
@dataclass(frozen=True)
class RelayBudget:
    calls_last_minute: int; calls_today_ist: int; per_minute_limit: int; per_day_limit: int
    remaining_today: int; ist_date: date
@dataclass(frozen=True)
class LoginAttempt:
    attempt_id: str; login_url: str = field(repr=False); relay_token: str = field(repr=False); expires_at_utc: datetime
@dataclass(frozen=True)
class SessionStatus:
    state: str; expires_at_ist: datetime | None

class RelayError(Exception):
    code: str; http_status: int | None; reason: str | None; breeze_status: int | None
    retryable: bool; retry_after_s: int | None; request_id: str | None
    # str(err) == f"{code} (request {request_id})"; never a header, body or parameter value
# One subclass per server code: Unauthorized, NoSession, SessionExpired, EgressMismatch, BudgetUnavailable,
# BudgetExhausted, UpstreamError, UpstreamTimeout, RelayValidationError, LoginAttemptInvalid, IdentityMismatch,
# ForbiddenClient, ContractMismatch, NotFound, MethodNotAllowed, InternalRelayError.
# Client-side: RelayContractMismatch (response header missing or different), RelayHashMismatch,
# RelayProtocolError (missing or malformed header, bad JSON, redirect, body over the cap, unknown code),
# RelayUnavailable (tunnel child gone, connect error, client timeout), TunnelError (tunnel would not start).
```

The client never retries (retry policy belongs to the caller) and logs nothing.

### Client rules

Rules a caller of growin-relay/1 follows. Each is checked against the VM at `gateway/vm/gateway_vm/`.

- A 409 LOGIN_ATTEMPT_INVALID from `login/complete` ends that `attempt_id` for good (expired, already used, or cut off by a revoke or a drop while the exchange ran). A complete superseded by a later arm answers 401 or 409: the VM checks the relay token before the attempt id, so a second arm before the first complete shows up as 401 UNAUTHORIZED (the token differs), and 409 when no attempt is pending. Both mean re-arm. Never arm twice in parallel: the VM keeps one pending attempt, so a second arm kills the first. `RelayClient` enforces both: it serialises arm and complete, and refuses a spent or non-current attempt locally without sending anything.
- EGRESS_MISMATCH is mapped on the code. Its reason can be `egress_mismatch`, `egress_unavailable` or `ip_mismatch`; do not branch on one value of it.
- `health`, `login/arm`, `login/complete` and `session/revoke` answer 422 VALIDATION for any query string, before any auth check. Never add a query to them, not even a cache-buster.
- `bars/daily` checks in this order: 401 (token), then 422 (query), then 503 (no session or expired). A 422 never means the session is bad, so it never means log in again.

## Observed upstream behaviour

Source: the Breeze smoke run of 2026-10-21 (probe 61-01b, 13 of 20 Breeze calls, a
weekday afternoon just after the close), validated. Nothing below is an account
value. The relay passes upstream bytes through unmodified, so everything here is
what a caller will see in the body.

### Daily bars (breeze-historicalcharts-v2)
- Envelope keys: `Error`, `Status`, `Success`. Row keys: `close`, `datetime`,
  `exchange_code`, `high`, `low`, `open`, `stock_code`, `volume`. Types: OHLC are
  floats, `volume` an int, `datetime` and the codes strings.
- The wire interval is `1day`. `X-Growin-Upstream-Interval` reports the same value.
- `datetime` is `YYYY-MM-DD HH:MM:SS` and every daily bar is stamped `00:00:00`.
  Callers filter rows by the parsed trading date, not by the timestamp.
- Prices are RAW (unadjusted). The BAJFI 1:10 split of 2025-06-16 shows as a close
  ratio of 0.1005 (9331.0 before, 938.0 after) and the RELIND 1:1 bonus of
  2024-10-28 as 0.5024 (2655.7 before, 1334.35 after). Backfill must apply
  corporate actions itself.
- Boundary days are included on both sides: the first and the last requested day
  came back in both split probes. The relay therefore pads nothing (0 days on each
  side) and sends `from_date=<from>T00:00:00.000Z`, `to_date=<to>T23:59:59.000Z`.
- The 1000-row cap keeps the NEWEST rows. A five-year RELIND request returned
  exactly 1000 rows, 2022-09-20 to 2026-09-30, and lost the oldest days (its first
  requested day was missing). Chunk windows below 1000 trading days and treat a
  capped answer as incomplete, never as the whole window.
- HTTP 200 with a `Status` 500 body is an upstream error, not data. An earlier run
  (2026-10-16, a Saturday night) got `Status` 500 on every bars call with the same
  request shape and the 2026-10-21 weekday run did not; the likeliest reading is an
  off-hours upstream problem, not proven. The relay maps it to 502 UPSTREAM_ERROR
  and never relays that body, and it is never read as "no data".
- No request variant (User-Agent or parameter-name change) was needed: the fallback
  variants did not run.

### Quotes (breeze-quotes-v1)
- RELIND, BAJFI and NIFBEE all answered with HTTP 200 and `Status` 200. NIFBEE
  answered on this run; on 2026-10-16 it did not, so a quote for an ETF can still be
  an upstream error.
- 21 keys: `best_bid_price`, `best_bid_quantity`, `best_offer_price`,
  `best_offer_quantity`, `exchange_code`, `expiry_date`, `high`, `low`,
  `lower_circuit`, `ltp`, `ltp_percent_change`, `ltt`, `open`, `previous_close`,
  `product_type`, `right`, `spot_price`, `stock_code`, `strike_price`,
  `total_quantity_traded`, `upper_circuit`. Circuit limits are present.
- `ltt` is `DD-Mon-YYYY HH:MM:SS`.
- Tick sizes inferred from a handful of prices agreed with the NSE table for RELIND
  (10 paise) and disagreed for BAJFI (inferred 20, table 5) and NIFBEE (inferred 1,
  table 5). Inference is not a tick source; the NSE rules table stays authoritative.

### Charge preview (breeze-preview-order-v1)
- Four previews (RELIND, buy and sell, quantity 1 and 41, limit) all answered 200.
  The nine line keys are `brokerage`, `exchange_turnover_charges`, `stamp_duty`,
  `stt`, `sebi_charges`, `gst`, `total_turnover_and_sebi_charges`,
  `total_other_charges`, `total_brokerage`. There is no DP-charge line. Buys carry
  stamp duty and sells carry none. It is a calculator and places nothing.

### Session
- Replaying the same API session code a second time succeeded, returned an equal
  Breeze token, and the first token kept working. A concurrent login therefore does
  not kill an earlier session.
- The API session code was 8 digits. The relay filter is `^[A-Za-z0-9]{6,64}$`, the
  looser safe superset of what was observed.
- Customer details report a last-login time as `%d-%b-%Y %H:%M:%S` with an IST hint.
  The relay still ends a session at min(24 h, next midnight IST) and does not trust
  that hint: Breeze documents the session key as lasting 24 hours or until midnight,
  whichever is first, so the conservative deadline can only be early, never late.

## Consumers

- Phase 59 part 2 (backfill): `RelayClient.open()`, `daily_bars`, `security_master`,
  `health`, `budget`, and `UpstreamPayload.provenance()` mapped into its own source
  records (source, symbol, ISIN, fetch time, hash). It chunks windows below 1000
  rows, applies corporate actions locally and rejects a capped chunk as incomplete.
- Phase 60 (costs): `preview_order` for the brokerage cross-check.
- Phase 63 (orders): a new contract version (`growin-relay/2`). Version 1 has no
  order, funds, holdings, positions or trades route.

## Daily use

Run from the repo root. Each command opens its own IAP tunnel as the daily identity
and closes it on exit. No SSH and no shell are involved.

```
export PYTHONPATH=backend
uv run --project backend --no-sync python -m brokers.breeze_gateway.cli health
uv run --project backend --no-sync python -m brokers.breeze_gateway.cli login --paste
uv run --project backend --no-sync python -m brokers.breeze_gateway.cli budget
uv run --project backend --no-sync python -m brokers.breeze_gateway.cli probe-daily --stock-code RELIND --from 2025-06-02 --to 2025-06-13
uv run --project backend --no-sync python -m brokers.breeze_gateway.cli revoke
uv run --project backend --no-sync python -m brokers.breeze_gateway.cli tunnel
```

- `health` prints `state`, `login_pending`, `expires_at_ist` and `egress_ok`, one per line.
- `login --paste` arms the relay, opens the ICICI login in the browser (`--no-open`
  skips that and only prints an instruction), then reads the API session code at a
  hidden prompt and sends it as a JSON body over the tunnel. It needs a terminal.
  The code, the login URL and the relay token are never printed or logged.
- `probe-daily` fetches one window, verifies the sha256 header against the bytes,
  prints the request id, the row count, the empty flag and `sha256 verified`, and
  writes nothing to disk.
- `tunnel` prints `RELAY_URL=http://127.0.0.1:<port>` for manual inspection and waits
  for Ctrl-C. The relay token is not printed, so only health answers without it.
- Exit codes: 0 ok, 1 relay error (`<CODE> (request <id>)`), 2 usage or no terminal,
  3 config error, 4 tunnel error.
