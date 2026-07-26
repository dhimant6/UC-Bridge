# UCM-Bridge

Bidirectional migration and coexistence platform for Unified Communications
estates.

Moves configuration, identity, telephony, and collaboration workloads between
legacy on-prem platforms (Cisco CUCM, Avaya Aura, Skype for Business Server) and
cloud platforms (Microsoft Teams Phone, Slack, Genesys Cloud CX) — **in both
directions**. Cloud-to-on-prem repatriation is a first-class path, not a
reversal bolted on afterwards.

It is not a media or signalling gateway. It carries no RTP and no SIP. It reads,
models, transforms, validates, and writes *configuration and identity state*.

```
Source Connector ──Extract──▶ Canonical UC Model ──Apply──▶ Target Connector
```

Not N×M point-to-point migrators. Every connector implements the same two
directions against one versioned, vendor-neutral model, so adding a platform is
one connector and reverse migration is the same pipeline with the ends swapped.

## Status

All seven phases of the build order are implemented, plus the control plane and
console. **274 tests, ruff clean, mypy `--strict` clean, `tsc` clean.**

| Phase | Deliverable | State |
|---|---|---|
| 0 | Canonical model, JSON Schema, fidelity taxonomy, connector contract | Done |
| 1 | CUCM connector, discovery, estate report, assessment engine | Done |
| 2 | Teams connector, number normalisation, rule DSL, auto-mapping | Done |
| 3 | Execution engine, hash-chained audit log, validation | Done |
| 4 | Avaya Aura (incl. SAT parser) and Skype for Business connectors | Done |
| 5 | Reverse direction: ports, Direct Routing→SIP, licence reclaim | Done |
| 6 | Slack, Genesys, split-target routing | Done |
| 7 | Wave planner, runbooks, multi-tenancy, collector agent | Done |
| 8 | FastAPI control plane and the nine React screens | Done |

## The console

Nine screens, in the order the work happens. Each one is a thin view over the
library: no business rule lives in `src/ucm_bridge/api/` or in `ui/`, because a
guardrail re-implemented in two places eventually disagrees with itself.

| # | Screen | What it is for |
|---|---|---|
| 1 | Estate | The estate report, plus a virtualised browser over every object, its fidelity assessment, and its transform log |
| 2 | Assessment | 18 rules by severity. Waiving a BLOCKER is refused *in the place someone would try it* |
| 3 | Mapping | Rules, number plan, minted numbers, and auto-map suggestions with their confidence signals |
| 4 | Waves | Cutover waves checked against dependency clusters, with per-wave runbooks |
| 5 | Plan & dry run | Operations in dependency order, and the exact vendor call each would make, current state beside proposed |
| 6 | Runs | The authorization form, every refusal it can produce, progress, and rollback |
| 7 | Validation | Eight post-migration checks, reconciliation, and the sign-off pack |
| 8 | Audit | The hash chain, filterable, with before/after per record and a verify button |
| 9 | Connectors | Every manifest and readiness verdict: what each connector may do, and how we know |

Two things the design turns on:

**A refusal is a result, not an error.** A 422 from a guardrail renders as its own
kind of callout with the library's own message, distinct from a fault. Presenting
a working safety rule in red teaches operators to ignore red.

**A role switcher, not a hidden button.** Approver and Operator permissions are
disjoint by design, so the console makes you change role to cross that line, and
a disabled action says which role holds the permission it needs.

### What is honestly not finished

Read this before pointing anything at a production system.

**No connector is cleared for production writes.** Every vendor cassette in this
repository is hand-authored from vendor documentation, not captured from a real
system. The readiness gate detects that and refuses production writes with
`NotProductionReady`. This is asserted in
`tests/test_acceptance.py::test_criterion_1_production_write_is_refused_while_cassettes_are_synthetic`.
Clearing a connector means capturing real cassettes from a lab system and
re-checking every API signature.

**Unverified API surfaces are declared, not guessed.** Where a signature could
not be checked against vendor documentation it carries no `verified_at` and the
readiness gate treats the connector as `LAB_ONLY`. Known examples:
`New-CsOnlineLisLocation` and `New-CsOnlinePSTNGateway` (Teams), and the Avaya
System Manager REST paths.

**Live transports are deliberately unimplemented.** `ZeepAxlTransport` is
written but never exercised; `SidecarPowerShellBridge`, `SshSatSession`,
`VaultCredentialProvider`, and `TemporalRunStore` raise `NotImplementedError`
with a message naming what must be decided first. Guessing them would produce
clients for servers nobody has built.

**A crashed run's rollback bundle is lost.** The bundle is assembled by the run
that performs the writes, so operations completed before a crash are not
covered by the resumed run's bundle. Persisting it incrementally alongside the
checkpoint is the obvious next change to `RunStore`. Asserted in
`test_criterion_3_a_run_resumes_and_rolls_back`.

**The console has no authentication and no persistence.** Identity comes from an
`X-UCM-Roles` header so the role switcher can demonstrate the RBAC boundaries; a
real deployment resolves it from an OIDC token, and the swap is confined to
`tenant_context()` in `src/ucm_bridge/api/app.py`. State is in-process, so a
restart loses discovery results, plans, runs, and the audit chain — storage is
still the undecided item in ADR-0002.

**The console's data is the test cassettes.** That is a limit, not a mock: every
figure on every screen is produced by the real discovery, assessment, mapping,
planning, execution, validation, and audit code paths. Nothing in
`src/ucm_bridge/api/` fabricates a result the library would not produce.

## Guardrails, enforced in code

Not conventions. Each is validated where it cannot be bypassed, and each has a
test asserting the refusal.

| Guardrail | Enforced by |
|---|---|
| Zero writes to any source system | `apply()` refuses production writes under a `READ_ONLY` credential scope |
| Unverified connectors cannot write to production | Readiness gate on API-surface verification and cassette provenance |
| Dry run is mandatory and is the default | `ApplyAuthorization` cannot be constructed in `PRODUCTION` without a receipt |
| The dry run must cover *this* plan | Matched by plan digest, so editing a plan invalidates its approval |
| Two-person approval | Two distinct approvers; Approver and Operator permissions are disjoint |
| Change-window enforcement | Outside the window requires an attributed override with a reason |
| Emergency config is never migrated silently | Per-site confirmation required; a missing emergency location is a validation `HARD_FAIL` |
| `UNMAPPABLE` entities are never written | Rejected by the planner and again by the connector base |
| Nothing is `LOSSLESS` by default | Default fidelity is `DEGRADED`/unassessed; `LOSSLESS` requires evidence |
| Connectors cannot skip the gate | `extract` and `apply` are final; overriding raises `ContractViolation` |
| Vendor calls are allow-listed | AXL operations, PowerShell cmdlets, and SAT verbs are declared and checked |
| Secrets never reach a log | `SecretBundle` redacts under `repr`, `str`, and serialisation |
| Audit tampering is detectable | Hash-chained append-only records; `verify()` finds edits and deletions |
| Wave dependency integrity | A plan splitting a shared line, hunt group, or delegation is rejected |
| Cross-tenant access | Raises rather than filtering — an empty result set hides the bug |

## Layout

```
src/ucm_bridge/
├── canonical/      74 entity kinds across 10 domains; fidelity taxonomy
├── vendor/         Verified API declarations + cassette-driven transports
│   ├── axl.py      Cisco AXL: endpoint, SOAPAction, namespace, throttling
│   ├── msgraph.py  Graph endpoints + Teams cmdlet catalogue
│   ├── sat.py      Avaya SAT terminal-form parser
│   ├── rest.py     Shared REST transport with Retry-After and pagination
│   └── readiness.py  Production-readiness gate
├── connectors/     cucm · teams · avaya · sfb · slack · genesys · reference
├── discovery/      Read-only crawl and estate report
├── assessment/     18-rule engine; emergency gaps are unwaivable BLOCKERs
├── mapping/        Number normalisation, rule DSL, auto-mapping, profiles
├── pipeline/       Planner, reconciliation, split-target routing
├── execution/      Durable resumable runs with checkpoints
├── validation/     Eight post-migration checks beyond "the API returned 200"
├── audit/          Hash-chained append-only log and evidence packs
├── repatriation/   Port orders, Direct Routing→SIP, licence reclaim
├── waves/          Wave planning with dependency-cluster integrity
├── runbook/        Per-wave cutover runbooks with pre-agreed abort criteria
├── tenancy/        RBAC and tenant isolation
├── collector/      Outbound-pull on-prem agent for air-gapped estates
└── api/            FastAPI control plane; serves the built console from static/

ui/                 React + TypeScript console, nine screens
```

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate && pip install -e ".[api,dev]"
```

```bash
pytest
```

Build the console and serve everything from one process on
<http://127.0.0.1:8000>, with OpenAPI docs at `/api/docs`:

```bash
cd ui && npm install && npm run build && cd ..
```

```bash
python -m ucm_bridge.api
```

For UI work, run them separately — Vite proxies `/api` to port 8000 and gives you
hot reload. See [deploy/README.md](deploy/README.md) for both, and for hosting.

Regenerate the JSON Schema after changing any canonical model:

```bash
python -m ucm_bridge.tooling.emit_schema
```

The test suite needs no vendor SDK and reaches no network: every connector is
driven by recorded cassettes, and a call a cassette does not know fails loudly
rather than falling through to a live system.

## The acceptance criteria

`tests/test_acceptance.py` has one test per §9 criterion, named for the claim it
proves, so each can be checked rather than taken on trust.

| Criterion | Test | Result |
|---|---|---|
| CUCM estate discovered → assessed → mapped → dry-run → validated | `test_criterion_1_*` | Passes to dry-run; production write correctly refused (synthetic cassettes) |
| Same pipeline inverted, losses declared up front | `test_criterion_2_*` | Passes |
| Any run resumes and rolls back | `test_criterion_3_*` | Passes, with the crashed-bundle limitation asserted |
| Zero writes to any source in any mode | `test_criterion_4_*` | Passes |
| Every write audited with before/after | `test_criterion_5_*` | Passes |
| Re-running an identical plan changes nothing | `test_criterion_6_*` | Passes |

## Reading suggestions

Three places where the reasoning matters more than the code:

- [docs/fidelity-taxonomy.md](docs/fidelity-taxonomy.md) — why reconciliation
  passing and fidelity reporting a loss are both true at once, and why that
  matters.
- [docs/adr/0001-canonical-model.md](docs/adr/0001-canonical-model.md) — the
  hub-and-spoke choice and what it costs.
- [src/ucm_bridge/vendor/sat.py](src/ucm_bridge/vendor/sat.py) — a structural
  parser for Avaya terminal forms, and the specific rule that stops a label
  swallowing the value to its left.

## Adding a connector

1. Subclass `Connector`; declare `connector_id` and `platform`.
2. Implement `capabilities()`, `test_connection()`, `_extract_batches()`,
   `_preview_operation()`, `_execute_operation()`.
3. Optionally implement `natural_key_for()`, `_capture_pre_state()`,
   `_invert_operation()`, `_confirm_operation()`.
4. Declare honestly: known gaps, required permissions, rate limits, and whether
   the platform is eventually consistent.
5. Record in `APISurface` when and how each API version was verified. **Do not
   assert an unverified version** — the readiness gate will keep the connector
   out of production, which is the intended outcome.
6. Write cassette tests. No connector merges without them.

`connectors/reference/connector.py` is the worked example.
