# Connecting to real systems

The console ships in **DEMO** mode: every connector runs against recorded
cassettes, reaches no network, and holds no credential. This is how you change
that, and what each platform still needs first.

## The two questions, kept apart

Conflating these is how someone writes to a production PBX with a connector
nobody verified, so the code keeps them separate and so does this page.

**Can the connection be opened?** Does a live transport exist for that platform,
and is it configured. Answered by the connection profile and the factory.

**May the connector write to production?** Answered by the readiness gate, from
API-surface verification and cassette provenance —
`vendor/readiness.py::assess_readiness`.

**The mode switch only affects the first.** A connection can be perfectly
openable and still refused every write. That is the normal case today.

## Turning live mode on

```bash
export UCM_BRIDGE_MODE=LIVE
```

```bash
export UCM_BRIDGE_CONNECTIONS=/etc/ucm-bridge/connections.json
```

```bash
export UCM_BRIDGE_STATE_DIR=/var/lib/ucm-bridge
```

Anything other than the exact string `LIVE` (case and surrounding whitespace
forgiven) is DEMO. A typo must never be the reason something dials a PBX.

`UCM_BRIDGE_STATE_DIR` is not optional in practice: without it the audit chain
and run checkpoints are in-process, and a restart mid-run loses the record of
writes that have already happened on a customer's system.

### Live mode refuses header identity

`X-UCM-Roles` is a role anyone who can reach the port can grant themselves. Fine
for a demo with no credentials behind it; not fine for a process that can write
to a PBX. In live mode it returns **401** unless you set:

```bash
export UCM_BRIDGE_ALLOW_HEADER_AUTH=1
```

Only set that when the process is behind an authenticating proxy that sets those
headers itself and strips whatever the client sent. The real fix is to resolve
identity from an OIDC token in `tenant_context()` — one function,
`src/ucm_bridge/api/app.py`.

CORS also tightens automatically: same-origin only in live mode, overridable with
`UCM_BRIDGE_ALLOWED_ORIGINS`.

## Writing a connection file

See [connections.example.json](connections.example.json). A profile is an
address plus a *reference* to a credential — never a secret, so the file is safe
to commit and safe to show on screen. Secrets resolve through the broker at call
time, from the environment (`EnvCredentialProvider`) or a dev-only file.

Enum values are upper case: `READ_ONLY`, `USERNAME_PASSWORD`, `AXL_SOAP`.

A live profile is refused at load time if it has no endpoint, no credential, or
a READ_ONLY credential with `"intended_use": "READ_WRITE"` — the intent and the
credential's own scope cannot disagree silently.

## Where each platform stands

| Platform | Transport | Exists | What it still needs |
|---|---|---|---|
| **Genesys Cloud** | REST | ✅ | Nothing. Set the regional address and an OAuth client. Extract-only by design. |
| **Slack** | REST | ✅ | Nothing to connect. Stays UNVERIFIED until the Discovery API surface is verified. Extract-only. |
| **Cisco CUCM** | AXL SOAP | ✅ | `pip install 'ucm-bridge[cucm]'`, an AXL-enabled service account, and the per-release `AXLAPI.wsdl` at `wsdl_path`. **Never executed against a real cluster** — expect to debug it. |
| **Microsoft Teams** | Graph REST | ✅ | Extraction works. |
| **Microsoft Teams** | PowerShell | ❌ | **The blocker.** Every Teams write is a cmdlet. Needs a containerised PowerShell 7 sidecar and its HTTP contract. |
| **Skype for Business** | PowerShell | ❌ | Same sidecar, over WinRM. |
| **Avaya Aura** | SSH SAT | ❌ | `SshSatSession` is unimplemented; the terminal dialogue, pagination and timeouts need pinning against a real CM. SMGR REST is also unverified. |

### The critical path

**The PowerShell sidecar.** Teams is the write target for five of six estates,
so until it exists nothing migrates *into* Teams — regardless of how many source
systems you connect. Everything else is extraction and assessment, which is
genuinely useful on its own and is not a migration.

## The order that works

1. **Connect a read-only source.** Genesys or Slack — pure REST, no extra
   dependency, no write risk. Confirms credentials, the broker, and the preflight
   path end to end.
2. **Preflight it.** Connections → Preflight. `test_connection()` is required by
   the connector contract to be read-only, so it is safe against production. It
   is the difference between "the config looks right" and "the credential works
   and carries the permissions we need".
3. **Discover, and compare.** Run discovery live and read the estate report
   against what you know is in that system. This is where a wrong assumption in a
   connector surfaces, cheaply.
4. **Capture cassettes from the live traffic.** These are what replace the
   hand-authored fixtures, and what lifts LAB_ONLY. Record from a lab, never from
   a production system holding real personal data.
5. **Re-verify the API surfaces** against the release you actually connected to,
   and record `verified_at`. Now `assess_readiness` returns PRODUCTION_READY on
   its own — there is no flag for this and there should not be.
6. **Only then**, a write. Against a lab. With the change window, the two
   approvers and the emergency confirmations that the authorization model
   already demands.

## What is deliberately still missing

- **`VaultCredentialProvider`** raises `NotImplementedError` naming what has to be
  confirmed: Vault version, KV mount path, auth method. Env and file providers
  work today.
- **`TemporalRunStore`** likewise. `JsonFileRunStore` is wired and persists.
- **A crashed run's rollback bundle is still lost.** The bundle is assembled by
  the run performing the writes, so operations completed before a crash are not
  covered by the resumed run's bundle. Persisting it incrementally alongside the
  checkpoint is the fix, and it has not been made.
