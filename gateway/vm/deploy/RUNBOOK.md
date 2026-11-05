# Gateway relay runbook: deploy, daily login, smoke, scans (Phase 61)

Exact commands to put the relay on the Mumbai VM and to use it every day. Every value
below is a placeholder. Real values live in `~/.config/growin/gateway.json` and
`docs/icici-docs/_local/account.md` on your Mac and never go into this repo, a chat or a
prompt to an agent.

Files this runbook installs:

| File | Host path | Owner and mode |
|---|---|---|
| sources | `/opt/growin-gateway/src` | root, 0755, nothing group or other writable |
| interpreter | `/opt/growin-gateway/python` | root |
| venv | `/opt/growin-gateway/.venv` | root |
| relay config | `/etc/growin-gateway/relay.json` | root, 0600 |
| limits | `/etc/growin-gateway/limits.json` | root, 0644 (63-06) |
| order key | `/etc/growin-gateway/order-key.json` | root, 0644 (63-06) |
| unit | `/etc/systemd/system/growin-gateway.service` | root, 0644 |
| state | `/var/lib/private/growin-gateway` | the service's dynamic user, 0700 (systemd makes it) |

Nothing lives under `/home`.

## 1. Rules

- Run every command in your own terminal, never in an agent's shell.
- Never paste a secret, an account id, an IP address or a key id into chat. Type secrets only
  at hidden prompts. Report `ok` or `not ok`, never the value.
- Every `gcloud compute ssh` and `gcloud compute scp` here carries `--tunnel-through-iap`.
  `tests/backend/test_gateway_iap_only.py` fails the build if one does not.
- Restart the unit outside market hours (a restart drops the Breeze session, see section 8).
- Sections 2 to 6, 10 (VM part), 11 and 13 run inside an admin window (section 12). The daily
  identity has no SSH and no shell (OD-16), so it can only run the CLI commands in sections 7 to 9.

Variables for the Mac side. `PROJECT`, `ZONE` and `VM` come from the config file at run time:

```bash
CFG="$HOME/.config/growin/gateway.json"
getcfg() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["gcp"][sys.argv[2]])' "$CFG" "$1"; }
export PROJECT="$(getcfg project)"
export ZONE="$(getcfg zone)"
export VM="$(getcfg instance)"
export ADMIN="<ADMIN_ACCOUNT_EMAIL>"
export DAILY="<DAILY_ACCOUNT_EMAIL>"
# The relay CLI, run from the repo root as the daily identity:
growin_cli() { PYTHONPATH=backend uv run --project backend --no-sync python -m brokers.breeze_gateway.cli "$@"; }
```

## 2. Build the bundle from the reviewed main commit

Build from `main` at the commit the reviewers approved, never from a phase branch.

```bash
git switch main && git pull --ff-only origin main && git status --short
SHA=$(git rev-parse HEAD)
git archive --format=tar.gz -o "$TMPDIR/growin-gateway-$SHA.tgz" "$SHA" gateway/vm
shasum -a 256 "$TMPDIR/growin-gateway-$SHA.tgz"
tar -tzf "$TMPDIR/growin-gateway-$SHA.tgz" | grep -E 'gateway/vm/(uv.lock|pyproject.toml|deploy/growin-gateway.service)$'
tar -xzOf "$TMPDIR/growin-gateway-$SHA.tgz" gateway/vm/gateway_vm/BUILD_COMMIT
```

The last command must print `$SHA` and nothing else. `.gitattributes` makes `git archive` expand that one
file. A line that still reads `$Format:%H$` means the stamp was not applied (a copy that did not come from
`git archive`): stop, because the VM would then answer `build_unstamped` and no session could record its build.

Write the hash down. The last command must list all three files. If `uv.lock` is missing the
lock has not merged yet (plan 61-11 Task 1b waits for your "packages ok" answer): stop, do not
build a lock by hand on the VM.

## 3. Copy to the VM and compare

```bash
gcloud compute scp "$TMPDIR/growin-gateway-$SHA.tgz" "$VM:/tmp/" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --command="sha256sum /tmp/growin-gateway-$SHA.tgz"
```

The two hashes must be identical. If they differ, delete the file on the VM and start again.

## 4. Host layout, interpreter and dependencies

Open a root shell on the VM: `gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap`,
then run this inside it (set `SHA` to the same commit):

```bash
sudo install -d -o root -g root -m 0755 /opt/growin-gateway /opt/growin-gateway/src
sudo tar -xzf "/tmp/growin-gateway-$SHA.tgz" -C /opt/growin-gateway/src --strip-components=2 --no-same-owner
sudo chown -R root:root /opt/growin-gateway
sudo chmod -R go-w /opt/growin-gateway
sudo env UV_PYTHON_INSTALL_DIR=/opt/growin-gateway/python UV_CACHE_DIR=/root/.cache/uv \
  /usr/local/bin/uv python install 3.12
sudo env UV_PYTHON_INSTALL_DIR=/opt/growin-gateway/python UV_PROJECT_ENVIRONMENT=/opt/growin-gateway/.venv \
  UV_CACHE_DIR=/root/.cache/uv \
  /usr/local/bin/uv sync --frozen --no-dev --python 3.12 --project /opt/growin-gateway/src
sudo chmod -R go-w /opt/growin-gateway
```

Check the install before any unit exists:

```bash
/opt/growin-gateway/.venv/bin/python -c 'import fastapi, uvicorn, cryptography; print(fastapi.__version__, uvicorn.__version__, cryptography.__version__)'
/opt/growin-gateway/.venv/bin/python -c 'import zoneinfo; zoneinfo.ZoneInfo("Asia/Kolkata"); print("tz ok")'
```

Expected: `0.109.2 0.27.1 46.0.4`, then `tz ok`. If the versions differ, stop. If the tz line raises,
stop and tell the agent: the relay builds that zone at import and will not start. Then:

```bash
rm -f "/tmp/growin-gateway-$SHA.tgz"
```

## 5. Config

The directory is 0700 root. `relay.json` is 0600 root, copied from the template and filled by hand.

```bash
sudo install -d -o root -g root -m 0700 /etc/growin-gateway
sudo install -o root -g root -m 0600 /opt/growin-gateway/src/deploy/relay.example.json /etc/growin-gateway/relay.json
sudoedit /etc/growin-gateway/relay.json
```

Fill the two placeholders (`expected_egress_ip` and `expected_userid`) with sudoedit. Never use
`echo` or `tee` for this, and never print the file. Leave `limits_path` and `order_key_path` out:
the relay defaults to the two paths below. The relay refuses the template until both
placeholders are replaced.

The order files are installed by plan 63-06 Task 3 (ORDERS-RUNBOOK), not here. This is the
contract this unit depends on. Both go in `/etc/growin-gateway` as root-owned files:

```bash
sudo install -o root -g root -m 0644 "$LIMITS_SRC" /etc/growin-gateway/limits.json
sudo install -o root -g root -m 0644 "$ORDER_KEY_SRC" /etc/growin-gateway/order-key.json
```

- Mode 0644, never 0600: the service runs as a dynamic user that is neither owner nor group, so it
  could not read a 0600 file.
- Never group or other writable: the relay's loaders refuse that.
- Owner root: the loaders refuse anything else.
- The directory stays 0700. The unit shows the two files at the same paths through a read-only
  bind, and hides everything else in the directory (section 6 checks this).
- Restart the unit after any change to either file. The loaders read them once at start.

Until 63-06 Task 3 installs them, the order routes answer 503 `config_invalid` by design. Health
and the data routes work, and the journal shows one `config_alarm` event for it. That is expected.

## 6. The unit

```bash
sudo install -o root -g root -m 0644 /opt/growin-gateway/src/deploy/growin-gateway.service /etc/systemd/system/growin-gateway.service
sudo systemd-analyze verify /etc/systemd/system/growin-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now growin-gateway
systemctl status growin-gateway --no-pager
sudo systemd-analyze security growin-gateway --no-pager
```

Record the `systemd-analyze security` score. Then check what listens and what the service wrote:

```bash
sudo ss -ltnp
curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip; echo
sudo ls -l /var/lib/private/growin-gateway
sudo journalctl -u growin-gateway --no-pager -o cat -n 50
```

- `ss` shows the relay on the internal address from the metadata call, port 8443, besides sshd and
  resolved. Never a wildcard bind and never the loopback address.
- The state directory is empty until the first Breeze call writes `counter.json`. `state.json`,
  `audit.jsonl`, `.order.lock` and `alerts.jsonl` appear only after 63-06 Task 3.
- The journal shows `service_start` with result ok and, before 63-06 Task 3, one `config_alarm`
  with reason `config_invalid`.

Namespace check, read-only. It prints owners, modes and names only:

```bash
PID=$(systemctl show -p MainPID --value growin-gateway)
sudo nsenter -t "$PID" -m ls -la /etc/growin-gateway
sudo nsenter -t "$PID" -m stat -c '%u %a %F %n' /etc/growin-gateway/limits.json /etc/growin-gateway/order-key.json
```

Expected: the directory inside the unit is 0755 and has no `relay.json` (the config reaches the
service as a credential). Before 63-06 Task 3 it is empty and `stat` reports no such file. After
63-06 Task 3 both files report `0 644 regular file`.

Troubleshooting. The journal line `service_start` carries the reason and the process exit code:

| Reason | Exit code |
|---|---|
| `bind_invalid` | 2 |
| `config_invalid` | 3 |
| `secrets_unavailable` | 4 |
| `internal` | 1 |

If the start fails on `MemoryDenyWriteExecute` or `SystemCallFilter`, remove only that line, record
why, and tell the agent. A failure on `LoadCredential`, `TemporaryFileSystem` or `BindReadOnlyPaths`
is a stop, not a line to remove: without them the relay cannot read its config or the order files.
The first real signature check under the unit is the 63-06 Task 4 drill; a native-extension failure
under `MemoryDenyWriteExecute` may only show there.

## 7. Daily login

Run as the daily identity, in your own terminal, on the Mac. The relay holds the Breeze session in
memory only.

```bash
growin_cli login --paste
growin_cli health
```

`login --paste` opens a hidden prompt for the day's API_Session code (add `--no-open` to print the
portal note instead of opening a browser). `health` should show state `active` and tonight's IST
midnight as the expiry. There is no VM-side prompt: the daily identity has no SSH (OD-16).

The browser callback daemon is not part of the default path. If it lands, see the last section.

## 8. Revoke and restart

```bash
growin_cli revoke
```

On the VM, `sudo systemctl restart growin-gateway` also drops the session: it fails closed, and the
next call needs a fresh login.

## 9. Post-deploy smoke

Each step is a CLI command as the daily identity. A refusal prints the code and the request id, exit
code 1. Stage matters: the answers change once 63-06 Task 3 has installed the order files.

Ordering rule: a restart drops the session (section 8). After the last `systemctl restart` of any step, run `login --paste` before any order read whose answer you use.

Before login:

```bash
growin_cli health
growin_cli orders-key    # exits 1 with NO_SESSION
growin_cli orders-build  # exits 1 with NO_SESSION
```

- `health` answers with state none.
- `orders-key` and `orders-build` exit 1 with `NO_SESSION`. `orders-state` and `orders-audit` also exit 1 with `NO_SESSION` once 64-11 is in the build; without it they answer with no login.

Log in, then run the data checks:

```bash
growin_cli login --paste
growin_cli health
growin_cli budget
growin_cli probe-daily --stock-code RELIND --from 2025-06-02 --to 2025-06-06
```

- `health` shows active. `budget` shows the counters. `probe-daily` prints `sha256 verified` (one charged call).

Intraday, on a trading day during market hours, in a throwaway probe directory:

```bash
SMOKE_DIR=$(mktemp -d)
growin_cli probe-intraday step --step S-3 --probe-dir "$SMOKE_DIR"
rm -rf "$SMOKE_DIR"
```

- The step makes 3 charged intraday calls (one per probe symbol), is read-only, and no order route is reachable from it. Expect exit 0; an empty answer counts as answered.
- A `status INCONCLUSIVE` line with exit 1, or any relay error code, is a smoke failure: stop and tell the agent.
- Budget for the whole smoke is 7 charged calls worst case: 1 daily, 3 intraday slots, and one retry per slot after an empty answer or a 5xx. The relay limit is 4500 a day.
- Rerun only in a fresh directory. A rerun in the same directory is a retry and spends that ledger's retry slots. Each fresh directory has its own ledger, so a rerun never adds to an earlier smoke's ledger; it costs up to 6 more calls.
- The 64-02 Task 3 probe counts every relay call after its own S-3 baseline that day, so never run this smoke step on a day after 64-02 Task 3 has run its S-3. Before it, smoke calls do not touch its 20.
- How Breeze answers intraday outside market hours or on a holiday is not verified. If the deploy window is outside hours and the step ends INCONCLUSIVE, rerun it in a fresh directory on the next trading morning before 64-02 Task 3.
- The deploy is not smoke-complete until this step exits 0.

Order reads, before 63-06 Task 3 (no order files installed yet):

```bash
growin_cli orders-state
growin_cli orders-audit --after-seq 0
growin_cli orders-key
```

- Each of the three exits 1 with `ORDERS_UNAVAILABLE` and reason `config_invalid`.

After 63-06 Task 3 has installed the files and restarted the unit, log in again first, then read:

```bash
growin_cli login --paste
growin_cli orders-state
growin_cli orders-audit --after-seq 0
growin_cli orders-key
```

- `orders-state` shows `kill blocked`.
- `orders-key` prints a key id. Compare it to the `key_id` in `/etc/growin-gateway/order-key.json` on the VM and say "matches: yes" or "matches: no". Never paste the id.
- `orders-audit` shows no entry added by this smoke.

Build identity (needs a login, answers even before the order files are installed):

```bash
growin_cli orders-build
```

- It prints `commit` and `digest`. The commit must equal the `$SHA` from section 2; paste the commit into the run log (not the repo) and say "matches: yes" or "matches: no". `orders-build --files` adds the sha256 of every `orders/` file; the replay recomputes them from a checkout of that commit.
- Exit 1 with `ORDERS_UNAVAILABLE` and reason `build_unstamped` means the archive stamp is missing (section 2). Exit 1 with `NOT_FOUND` and reason `route_unavailable` means the VM runs a build older than this route.

Mac value scan, now, while the session is live and before `revoke` (section 10 explains the output). Have
that day's API_Session code ready for the optional prompt:

```bash
PYTHONPATH=backend uv run --project backend --no-sync python gateway/ops/scan_secrets.py mac
```

- `RESULT PASS` is the only pass. FAIL or INCOMPLETE stops the deploy: fix what it names, run it again.

Revoke:

```bash
growin_cli revoke
growin_cli health
growin_cli orders-key
```

- `health` shows state none. `orders-key` exits 1 with `NO_SESSION`.

Not in this smoke: login arm and complete run inside `login --paste`. Quotes, preview-order and
security-master have no CLI; the first use is plan 59-09 Task 2 and phase 60. The full intraday
checklist stays the 64-02 Task 3 probe.

## 10. Value scans

The scanner searches for the real values (and their URL-encoded, base64 and JSON-escaped forms) and
prints PASS, FAIL or INCOMPLETE with counts and file paths, never what it found. `RESULT PASS` is the
only pass. INCOMPLETE means something was not fully read: fix what it names and run it again.

The Mac scan runs inside the section 9 smoke, before `revoke`, because `revoke` deletes the token file and
the scan needs it. Do not run it after revoke. The VM scan:

VM, as root, from a throwaway 0700 directory:

```bash
git archive "$SHA" gateway/ops/scan_secrets.py | gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --command='sudo install -d -o root -g root -m 0700 /root/growin-scan && sudo tar -xf - -C /root/growin-scan --strip-components=2 --no-same-owner'
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --command='sudo python3 /root/growin-scan/scan_secrets.py vm'
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --command='sudo rm -rf /root/growin-scan'
```

## 11. Redeploy and rollback

Redeploy a new commit: repeat sections 2 to 6 with the new `SHA` (the unit file only if it changed),
restart the unit, then log in and run the section 9 smoke.

Rollback to nothing:

```bash
sudo systemctl disable --now growin-gateway
sudo rm -f /etc/systemd/system/growin-gateway.service
sudo systemctl daemon-reload
sudo rm -rf /opt/growin-gateway /etc/growin-gateway
```

State under `/var/lib/private/growin-gateway` is removed with `sudo rm -rf` only after you have
decided you do not need the day counter or the order audit. A rollback to a build without the order
key route makes plan 64-09 Task B refuse with `vm_key_route_unavailable`. That is by design.

## 12. Admin windows and the daily identity (OD-8, OD-16)

Open an admin window before any step that uses ssh, scp or IAM:

```bash
gcloud auth login "$ADMIN"
gcloud config set account "$ADMIN"
```

Close it when the work is done. Switching accounts is not enough; revoke the credentials:

```bash
gcloud auth revoke "$ADMIN"
gcloud auth application-default revoke
rm -f "$HOME/.config/gcloud/application_default_credentials.json"
gcloud config set account "$DAILY"
uv run --project backend --no-sync python gateway/ops/gcp_state.py check mac-credentials --config ~/.config/growin/gateway.json
```

The check must print PASS for each line. The daily identity logs in once and stays:

```bash
gcloud auth login "$DAILY"
```

NumPy for gcloud's IAP throughput (once). Check https://pypi.org/project/numpy/ first, then:

```bash
"$(gcloud info --format='value(basic.python_location)')" -m pip install numpy
```

## 13. Only if 61-10 lands

Skip this section unless the launchd callback daemon (plan 61-10) has merged and you answered DEC-1
"keep the daemon". Until then `login --paste` is the only login.

If it has landed: install mkcert with Homebrew, keep its root under a dedicated
`CAROOT=~/.config/growin/mkcert/ca`, and note that `mkcert -install` edits the system and Chrome trust
stores. Make the leaf certificate in a temporary directory, install it under
`/Library/Growin/login-callback/tls/` as `gateway/mac/INSTALL.md` says, then move the CA key offline or
delete it. Follow `gateway/mac/INSTALL.md` for the LaunchDaemon, then run `growin_cli daemon-check`.
Plain `login` through the daemon becomes the default and `--paste` stays the fallback.
