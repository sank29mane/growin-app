# GCP runbook: gateway VM changes (Phase 61)

Exact commands for the operator-only GCP changes. Every command has a read-only
verify and a rollback. Account values never go into this repo.

## Rules for every command here

- Run every command in your own terminal, never in an agent's shell.
- Type secrets only into `read -rs` prompts. Never paste a secret or an account
  value into chat or into a repo file.
- Fill the variables below from `docs/icici-docs/_local/account.md` on your Mac.
- Run from the repo root so the `uv run` lines resolve.
- Sections A and B and every SSH-over-IAP command run inside an admin window
  (section 0). The window closes at the end of Stage 3 (61-13).
- Every SSH or scp command in this file carries `--tunnel-through-iap`. The test
  `tests/backend/test_gateway_iap_only.py` fails the build if one does not. No
  line tunnels the daily identity to port 22.

```bash
export PROJECT="<GCP_PROJECT_ID>"
export ZONE="<GCP_ZONE>"
export REGION="${ZONE%-*}"
export VM="<VM_INSTANCE_NAME>"
export ADDRESS="<RESERVED_ADDRESS_NAME>"
export ADMIN="<ADMIN_ACCOUNT_EMAIL>"      # the project owner, the account that is active while a window is open
export DAILY="<DAILY_ACCOUNT_EMAIL>"      # a second Google account of yours: never the owner, never a service account with keys
export SA="breeze-gateway@${PROJECT}.iam.gserviceaccount.com"
export OLD_RELAY_USER="<OLD_RELAY_USER>"  # old local account that owns the old relay venv
export OLD_KEY_USER="<OLD_KEY_USER>"      # old local account created by a gcloud transient key
```

## 0. Admin window (OD-8)

The admin gcloud credentials live on this Mac only inside a window. Switching the
active account is not enough: the credentials stay on disk and any process of the
same user can use them. Closing a window means revoking them.

### 0a. Open

```bash
gcloud auth login "$ADMIN"
gcloud config set account "$ADMIN"
```

The daily identity is logged in once, outside any window:

```bash
gcloud auth login "$DAILY"
```

### 0b. Close

```bash
gcloud auth revoke "$ADMIN"
gcloud auth application-default revoke
rm -f "$HOME/.config/gcloud/application_default_credentials.json"
gcloud config set account "$DAILY"
```

### 0c. Verify (agent-run, read-only, local state only)

```bash
uv run --project backend --no-sync python gateway/ops/gcp_state.py check mac-credentials --config ~/.config/growin/gateway.json
```

Expect `PASS active_account_is_daily`, `PASS no_other_gcloud_accounts`,
`PASS no_adc_file`.

## A. Pre-window changes, no downtime (61-06)

Admin window open. Nothing here touches the running VM.

### A0. Enable the APIs

```bash
gcloud services enable secretmanager.googleapis.com oslogin.googleapis.com iap.googleapis.com --project="$PROJECT"
```

Verify:

```bash
gcloud services list --enabled --project="$PROJECT" --filter="config.name:(secretmanager.googleapis.com OR oslogin.googleapis.com OR iap.googleapis.com)" --format='value(config.name)'
```

Rollback: none needed, an enabled API costs nothing. To undo anyway,
`gcloud services disable <api> --project="$PROJECT"`.

### A1. Service account without keys

```bash
gcloud iam service-accounts create breeze-gateway --project="$PROJECT" --display-name="Breeze gateway" --description="Reads the two Breeze secrets. Never create keys for this account."
```

Verify (the list must be empty):

```bash
gcloud iam service-accounts keys list --iam-account="$SA" --managed-by=user --project="$PROJECT"
```

Rollback:

```bash
gcloud iam service-accounts delete "$SA" --project="$PROJECT" --quiet
```

### A2. Secrets

```bash
for S in breeze-api-key breeze-api-secret; do gcloud secrets create "$S" --project="$PROJECT" --replication-policy=user-managed --locations=asia-south1; done
```

Add each value from a hidden prompt. `printf` is a shell builtin, so the value
never appears in a process list.

```bash
printf 'AppKey: '; read -rs V; echo; printf %s "$V" | gcloud secrets versions add breeze-api-key --project="$PROJECT" --data-file=-; unset V
printf 'Secret key: '; read -rs V; echo; printf %s "$V" | gcloud secrets versions add breeze-api-secret --project="$PROJECT" --data-file=-; unset V
```

Verify:

```bash
gcloud secrets versions list breeze-api-key --project="$PROJECT"
gcloud secrets versions list breeze-api-secret --project="$PROJECT"
```

Never run `gcloud secrets versions access` on the Mac. Only the VM reads secret
values.

Rollback (destroys the version, then the secret):

```bash
gcloud secrets versions destroy 1 --secret=breeze-api-key --project="$PROJECT" --quiet
gcloud secrets delete breeze-api-key --project="$PROJECT" --quiet
```

Repeat for `breeze-api-secret`.

### A3. One accessor per secret

```bash
for S in breeze-api-key breeze-api-secret; do gcloud secrets add-iam-policy-binding "$S" --project="$PROJECT" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor; done
```

Verify (exactly one binding, only the service account):

```bash
gcloud secrets get-iam-policy breeze-api-key --project="$PROJECT"
gcloud secrets get-iam-policy breeze-api-secret --project="$PROJECT"
```

Rollback:

```bash
for S in breeze-api-key breeze-api-secret; do gcloud secrets remove-iam-policy-binding "$S" --project="$PROJECT" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor; done
```

### A4. Audit logging for secret reads (DATA_READ)

The merge adds one `auditConfigs` entry and changes nothing else. It needs the
etag, so a concurrent change makes `set-iam-policy` fail instead of overwriting.

```bash
gcloud projects get-iam-policy "$PROJECT" --format=json > "$TMPDIR/policy-before.json"
uv run --project backend --no-sync python gateway/ops/gcp_state.py merge-audit-config "$TMPDIR/policy-before.json" -o "$TMPDIR/policy-after.json"
gcloud projects set-iam-policy "$PROJECT" "$TMPDIR/policy-after.json"
```

Verify, then delete both temp files:

```bash
gcloud projects get-iam-policy "$PROJECT" --flatten=auditConfigs --filter="auditConfigs.service:secretmanager.googleapis.com" --format='value(auditConfigs.auditLogConfigs)'
rm -f "$TMPDIR/policy-before.json" "$TMPDIR/policy-after.json"
```

Rollback (before you delete the files): re-apply the saved original.

```bash
gcloud projects set-iam-policy "$PROJECT" "$TMPDIR/policy-before.json"
```

Viewing these logs needs `roles/logging.privateLogViewer`.

### A5. Admin bindings

If gcloud insists on a condition choice for a project binding, add
`--condition=None`.

```bash
gcloud projects add-iam-policy-binding "$PROJECT" --member="user:$ADMIN" --role=roles/iap.tunnelResourceAccessor
gcloud projects add-iam-policy-binding "$PROJECT" --member="user:$ADMIN" --role=roles/compute.osAdminLogin
gcloud iam service-accounts add-iam-policy-binding "$SA" --member="user:$ADMIN" --role=roles/iam.serviceAccountUser
OLD_SA="$(gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" --format='value(serviceAccounts[0].email)')"
gcloud iam service-accounts add-iam-policy-binding "$OLD_SA" --member="user:$ADMIN" --role=roles/iam.serviceAccountUser
```

OS Login needs `serviceAccountUser` on the VM's current service account while it
has one.

Verify:

```bash
gcloud projects get-iam-policy "$PROJECT" --flatten=bindings --filter="bindings.members:user:$ADMIN" --format='value(bindings.role)'
```

Rollback: the same commands with `remove-iam-policy-binding`.

### A5b. The daily identity: one conditioned role, nothing else (OD-16)

```bash
gcloud projects add-iam-policy-binding "$PROJECT" --member="user:$DAILY" --role=roles/iap.tunnelResourceAccessor --condition='expression=destination.port == 8443,title=relay-port-only,description=Daily identity may tunnel to the relay port only'
```

Verify (exactly one row, with that condition):

```bash
gcloud projects get-iam-policy "$PROJECT" --flatten=bindings --filter="bindings.members:user:$DAILY" --format='value(bindings.role,bindings.condition.expression)'
```

Rollback:

```bash
gcloud projects remove-iam-policy-binding "$PROJECT" --member="user:$DAILY" --role=roles/iap.tunnelResourceAccessor --condition='expression=destination.port == 8443,title=relay-port-only,description=Daily identity may tunnel to the relay port only'
```

The daily identity gets no `osLogin`, no `osAdminLogin`, no `serviceAccountUser`,
no `serviceAccountTokenCreator`, no `secretmanager` role and no viewer role. A
shell on the VM would let it mint the VM service account's token through the
metadata server, which is the same as holding that account (OD-16).

### A5c. Only if the 61-13 check says so

Run this only if `check daily-access` reports `actas_not_required` as FAIL. The
tunnel path should not need it, and the check measures that.

```bash
gcloud iam service-accounts add-iam-policy-binding "$SA" --member="user:$DAILY" --role=roles/iam.serviceAccountUser
```

Then re-run the daily-access check. Rollback: `remove-iam-policy-binding` with
the same arguments.

### A6. Mac gateway config

```bash
install -d -m 0700 "$HOME/.config/growin"
install -m 0600 gateway/ops/gateway.example.json "$HOME/.config/growin/gateway.json"
```

Edit `~/.config/growin/gateway.json` and replace the four `<...>` values in the
`gcp` section (project, zone, instance, daily account). The file stays outside
the repo.

### A7. NumPy for gcloud's IAP throughput

Check https://pypi.org/project/numpy/ first, then:

```bash
"$(gcloud info --format='value(basic.python_location)')" -m pip install numpy
```

Rollback:

```bash
"$(gcloud info --format='value(basic.python_location)')" -m pip uninstall numpy
```

### A. Agent-run check

```bash
uv run --project backend --no-sync python gateway/ops/gcp_state.py check pre-window --config ~/.config/growin/gateway.json
```

## B. Maintenance window (61-07)

Admin window open. Pick a time outside 09:00 to 15:45 IST on trading days, with
no Phase 59 backfill running. The VM stops and starts once. The reserved address
is registered with ICICI, and that registration can be edited only once per
calendar week, so confirm it stays attached.

### B0. Baseline

```bash
uv run --project backend --no-sync python gateway/ops/gcp_state.py check post-window --config ~/.config/growin/gateway.json
```

Several FAILs are expected. `address_in_use` must PASS. Record the old service
account and scopes for rollback:

```bash
export OLD_SA="$(gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" --format='value(serviceAccounts[0].email)')"
gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" --format='value(serviceAccounts[0].scopes)'
```

Write the scopes down. The B1 rollback needs them in place of `<OLD_SCOPES>`.

### B1. Service account, scope and Secure Boot (OD-13)

```bash
gcloud compute instances stop "$VM" --zone="$ZONE" --project="$PROJECT"
gcloud compute instances set-service-account "$VM" --zone="$ZONE" --project="$PROJECT" --service-account="$SA" --scopes=cloud-platform
gcloud compute instances update "$VM" --zone="$ZONE" --project="$PROJECT" --shielded-secure-boot
gcloud compute instances start "$VM" --zone="$ZONE" --project="$PROJECT"
```

Verify, including that the address is still in use:

```bash
gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" --format='value(serviceAccounts[0].email,shieldedInstanceConfig.enableSecureBoot)'
gcloud compute addresses describe "$ADDRESS" --region="$REGION" --project="$PROJECT" --format='value(status)'
```

Rollback (use the old service account and scopes you recorded in B0):

```bash
gcloud compute instances stop "$VM" --zone="$ZONE" --project="$PROJECT"
gcloud compute instances update "$VM" --zone="$ZONE" --project="$PROJECT" --no-shielded-secure-boot
gcloud compute instances set-service-account "$VM" --zone="$ZONE" --project="$PROJECT" --service-account="$OLD_SA" --scopes=<OLD_SCOPES>
gcloud compute instances start "$VM" --zone="$ZONE" --project="$PROJECT"
```

### B2. Keep a safety session open

Terminal 1, and leave it open until B4 is done:

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap
```

### B3. Turn on OS Login

Enabling it makes the guest stop honouring metadata keys. Existing sessions
persist, which is why terminal 1 stays open.

```bash
gcloud compute instances add-metadata "$VM" --zone="$ZONE" --project="$PROJECT" --metadata=enable-oslogin=TRUE
```

In terminal 2:

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --command='id -un; sudo -n true && echo sudo-ok'
```

If terminal 2 fails, roll back from the Mac. No SSH is needed for this:

```bash
gcloud compute instances remove-metadata "$VM" --zone="$ZONE" --project="$PROJECT" --keys=enable-oslogin
```

### B4. System-wide uv and cleanup of old accounts

In an interactive terminal-2 session as the OS Login user:

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap
```

Then on the VM:

```bash
sudo install -m 0755 -o root -g root /home/<OLD_RELAY_USER>/.local/bin/uv /usr/local/bin/uv
/usr/local/bin/uv --version
sudo rm -rf /home/<OLD_RELAY_USER>/relay
```

The last line removes the old venv that holds the vendor SDK (OD-2). Close
terminal 1. For each of `<OLD_KEY_USER>` and `<OLD_RELAY_USER>` that differs from
the output of `id -un`:

```bash
sudo userdel -r <OLD_KEY_USER>
sudo userdel -r <OLD_RELAY_USER>
```

### B5. Retire the extra SSH keys (OD-16)

```bash
gcloud compute instances list --project="$PROJECT" --format='value(name)'
```

If this VM is the only one listed:

```bash
gcloud compute project-info remove-metadata --project="$PROJECT" --keys=ssh-keys
```

Otherwise block project keys on this VM only:

```bash
gcloud compute instances add-metadata "$VM" --zone="$ZONE" --project="$PROJECT" --metadata=block-project-ssh-keys=TRUE
```

Then, in both cases:

```bash
gcloud compute instances remove-metadata "$VM" --zone="$ZONE" --project="$PROJECT" --keys=ssh-keys
```

These keys are retired on purpose. There is no rollback: sign in through OS Login
instead.

### B6. Delete the public ICMP rule (OD-11)

Save it first, so the rollback is exact:

```bash
gcloud compute firewall-rules describe default-allow-icmp --project="$PROJECT" --format=json > "$TMPDIR/default-allow-icmp.json"
gcloud compute firewall-rules delete default-allow-icmp --project="$PROJECT" --quiet
```

Rollback:

```bash
gcloud compute firewall-rules create default-allow-icmp --project="$PROJECT" --network=default --allow=icmp --source-ranges=0.0.0.0/0
```

### B7. The relay-port rule (OD-16)

```bash
NETWORK="$(gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" --format='value(networkInterfaces[0].network.basename())')"
gcloud compute firewall-rules create growin-relay-from-iap --project="$PROJECT" --network="$NETWORK" --direction=INGRESS --action=ALLOW --rules=tcp:8443 --source-ranges=35.235.240.0/20 --target-service-accounts="$SA" --description='Relay port from IAP only'
```

Verify:

```bash
gcloud compute firewall-rules describe growin-relay-from-iap --project="$PROJECT" --format='value(sourceRanges,allowed)'
```

Rollback:

```bash
gcloud compute firewall-rules delete growin-relay-from-iap --project="$PROJECT" --quiet
```

### B8. default-allow-internal

That rule would let any other VM in the network reach the relay port.

```bash
gcloud compute instances list --project="$PROJECT" --format='value(name)'
```

If only this VM is listed, save the rule and delete it:

```bash
gcloud compute firewall-rules describe default-allow-internal --project="$PROJECT" --format=json > "$TMPDIR/default-allow-internal.json"
gcloud compute firewall-rules delete default-allow-internal --project="$PROJECT" --quiet
```

Rollback:

```bash
gcloud compute firewall-rules create default-allow-internal --project="$PROJECT" --network="$NETWORK" --allow=tcp:0-65535,udp:0-65535,icmp --source-ranges=10.128.0.0/9
```

If other instances exist, stop and tell the agent. The decision is yours and
nothing is deleted.

### B. Agent-run checks

```bash
uv run --project backend --no-sync python gateway/ops/gcp_state.py check post-window --config ~/.config/growin/gateway.json
uv run --project backend --no-sync python gateway/ops/gcp_state.py check external --config ~/.config/growin/gateway.json
```

After 61-12 deploys the relay, run the first one again with `--expect-relay`.

## C. Where the rest lives

- VM host layout and the relay unit: `gateway/vm/deploy/RUNBOOK.md` (written by
  61-11).
- The Mac LaunchDaemon for the login callback: `gateway/mac/INSTALL.md` (written
  by 61-10).

## D. End of Stage 3 (61-13)

1. Close the admin window with step 0b.
2. Agent-run, read-only:

```bash
uv run --project backend --no-sync python gateway/ops/gcp_state.py check mac-credentials --config ~/.config/growin/gateway.json
uv run --project backend --no-sync python gateway/ops/gcp_state.py check daily-access --config ~/.config/growin/gateway.json --daily-roles-ok
```

Pass `--daily-roles-ok` only if `daily_no_other_roles` was PASS in the last
pre-window check of the admin window. The daily-access check makes three
attempts as the daily identity: an SSH attempt, a tunnel to port 22 and a tunnel
to the relay port. The first two must fail. The SSH attempt can create an OS
Login profile for the daily identity, which is harmless without a login role.
