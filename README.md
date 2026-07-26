# UCM-Bridge

Bidirectional migration and coexistence platform for Unified Communications
estates.

Moves configuration, identity, telephony, and collaboration workloads between
legacy on-prem platforms (Cisco CUCM/Unity, Avaya Aura, Skype for Business
Server, Mitel, UCCX/UCCE) and cloud platforms (Microsoft Teams Phone, Slack,
Genesys Cloud CX) — **in both directions**. Cloud-to-on-prem repatriation is a
first-class path, not a reversal bolted on afterwards.

It is not a media or signalling gateway. It carries no RTP and no SIP. It reads,
models, transforms, validates, and writes *configuration and identity state*,
plus a bounded set of user data where source APIs allow export.

## Status: Phase 0 complete

| Phase | Deliverable | State |
|---|---|---|
| **0** | Canonical model + JSON Schema + fidelity taxonomy | **Done** |
| 1 | CUCM connector (Extract) + Discovery + Estate report | Not started |
| 2 | Teams connector + Mapping workbench + dry-run engine | Not started |
| 3 | Execution engine (Temporal) + Validation + Audit | Not started |
| 4 | Avaya Aura and Skype for Business connectors | Not started |
| 5 | Reverse direction, port-order modelling, licence reclaim | Not started |
| 6 | Slack and Genesys connectors, split-target migrations | Not started |
| 7 | Wave planner, runbooks, multi-tenancy, collector agent | Not started |

## Architecture

```
Source Connector ──Extract──▶ Canonical UC Model ──Apply──▶ Target Connector
```

Not N×M point-to-point migrators. Every connector implements the same two
directions against one versioned, vendor-neutral model. Adding a platform is one
connector. **Reverse migration is the same pipeline with source and target
swapped** — there is no separate reverse code path.

See [ADR-0001](docs/adr/0001-canonical-model.md) for the reasoning and the
fidelity trade-offs, and [ADR-0002](docs/adr/0002-technical-stack.md) for the
stack decisions.

## What exists

```
src/ucm_bridge/
├── canonical/          74 entity kinds across 10 domains
│   ├── base.py         CanonicalEntity, fidelity taxonomy, deterministic digests
│   ├── registry.py     kind -> class registry, discriminated union
│   ├── snapshot.py     EstateSnapshot: versioned, diffable, replayable
│   └── identity | numbering | dialplan | endpoints | callhandling
│       | trunking | messaging | contactcenter | collaboration | policy
├── connectors/
│   ├── base.py         the Connector ABC (Extract / Apply / Capabilities / TestConnection)
│   ├── capabilities.py capability manifests
│   ├── contracts.py    plans, authorization, dry-run receipts, results
│   ├── credentials.py  pluggable credential providers
│   ├── errors.py       guardrail / capability / platform error taxonomy
│   └── reference/      MemoryPBX: a worked example proving the contract
├── pipeline/
│   ├── planner.py      dependency-ordered plan building, reference resolution
│   └── reconcile.py    cross-estate attribute-level reconciliation
└── tooling/
    └── emit_schema.py  Pydantic -> JSON Schema, with drift checking

schemas/v1/             80 generated JSON Schema documents (committed)
docs/
├── fidelity-taxonomy.md
└── adr/
```

## Guardrails, enforced in code

These are not conventions. Each is validated where it cannot be bypassed, and
each has a test asserting the refusal.

| Guardrail | Where it is enforced |
|---|---|
| Zero writes to any source system | `Connector.apply()` refuses a production write under a `READ_ONLY` credential scope |
| Dry run is mandatory and is the default | `ApplyAuthorization` cannot be constructed in `PRODUCTION` mode without a dry-run receipt |
| The dry run must cover *this* plan | Matched by plan digest, so editing a plan after approval invalidates the approval |
| Two-person approval | Two distinct approvers required for production |
| Change-window enforcement | Outside the window requires an attributed override with a reason |
| Emergency config is never migrated silently | Per-site confirmation required for every affected site; a missing one is a hard failure |
| `UNMAPPABLE` entities are never written | Rejected by the planner and again by the connector base |
| Nothing is `LOSSLESS` by default | Default fidelity is `DEGRADED`/unassessed; `LOSSLESS` requires evidence |
| Connectors cannot skip the gate | `extract` and `apply` are final; overriding them raises `ContractViolation` |
| Secrets never reach a log | `SecretBundle` redacts under `repr`, `str`, and serialisation |

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate && pip install -e ".[dev]"
```

Run the suite:

```bash
pytest
```

Regenerate the JSON Schema after changing any canonical model:

```bash
python -m ucm_bridge.tooling.emit_schema
```

Verify the committed schema is not stale (CI does this):

```bash
python -m ucm_bridge.tooling.emit_schema --check
```

## The Phase 0 proof

`tests/test_reference_roundtrip.py` takes a `User`, `Line`, `E164Number`, and
`EmergencyLocation` through the full pipeline against a fake platform, and
asserts the properties the acceptance criteria demand:

- extract → canonical → plan → dry run → apply → extract → **reconcile clean**
- **zero writes to the source**, verified against a deep copy of its state
- **re-running an identical plan changes nothing** (every op `SKIPPED_NO_CHANGE`)
- a run **resumes** cleanly after a checkpoint
- the **rollback bundle** restores the target to empty, in reverse dependency order
- a transient failure is **retried**; a permanent one is **quarantined** while the
  rest of the wave continues
- writes to an eventually consistent platform are **confirmed by re-reading**
  before being called success

One test is worth reading on its own:
`test_reconciliation_passes_while_fidelity_still_reports_the_shared_line_loss`.
Reconciliation passes while a shared-line appearance was genuinely lost — which
is exactly why the fidelity report exists alongside it. See
[the fidelity taxonomy](docs/fidelity-taxonomy.md).

## Adding a connector

1. Subclass `Connector`, declare `connector_id` and `platform`.
2. Implement `capabilities()`, `test_connection()`, `_extract_batches()`,
   `_preview_operation()`, `_execute_operation()`.
3. Optionally implement `natural_key_for()`, `_capture_pre_state()`,
   `_invert_operation()`, `_confirm_operation()`.
4. Declare honestly in the manifest: known gaps, required permissions, rate
   limits, and whether the platform is eventually consistent.
5. Record in `APISurface` when and how the API version was verified. **Do not
   assert an unverified version** — `unverified_api_surfaces()` reports the gap.
6. Write tests against recorded API fixtures. No connector merges without them.

`connectors/reference/connector.py` is the worked example.
