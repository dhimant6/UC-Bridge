# ADR-0002: Technical stack

- **Status**: Accepted
- **Date**: 2026-07-26
- **Phase**: 0

Records the four choices the brief marked **[DECIDE]**, as confirmed by the
project owner before Phase 0 began.

## 1. Backend: Python 3.12 + FastAPI

**Decision.** Python is the implementation language for the control plane, the
connector SDK, and the collector agent. `requires-python = ">=3.12"`.

**Why.** The vendor-integration surface decides this more than anything else.
Python has `zeep` for the AXL SOAP/WSDL work (versioned per CUCM release),
mature SSH/expect libraries for Avaya CM SAT and OSSI screen scraping, WinRM
interop for Skype for Business remote PowerShell, and first-party or mature
third-party SDKs for Graph and Genesys. Go would give better concurrency and a
single-binary collector, but AXL and SAT parsing would become substantially
hand-rolled work, and that is the riskiest code in the product.

**Caveat, stated plainly.** The development machine this was built on has Python
3.11.9 only. The project declares `>=3.12` as its contract, but the source
deliberately avoids 3.12-only syntax (PEP 695 generics, `type` statements) and
uses classic `TypeVar`/`Generic` instead. The practical effect is that the
Phase 0 test suite runs and passes on 3.11 today, while the declared floor
remains 3.12. If 3.12-only syntax is wanted, install 3.12 and the constraint can
be dropped; nothing depends on staying 3.11-compatible.

**FastAPI** for the control-plane API is the conventional pairing and is not
load-bearing for Phase 0 — no HTTP surface exists yet.

## 2. Canonical model: Pydantic v2 first, JSON Schema emitted

**Decision.** Pydantic v2 models in `src/ucm_bridge/canonical/` are the source of
truth. JSON Schema under `schemas/v1/` is a generated build artifact, committed
to the repository and drift-checked in CI.

**Why.** One definition, runtime validation for free, and the safety-critical
invariants can live *in* the model rather than in a validator someone might
forget to call — an emergency location cannot be marked confirmed without an
attributed confirmer, and a `LOSSLESS` fidelity claim cannot be constructed
alongside recorded losses. Hand-authored JSON Schema would be language-neutral
but could not enforce those, and codegen output is unpleasant to evolve.

**Cost.** Non-Python consumers read a generated artifact rather than a
hand-curated one. Mitigated by committing the schemas (80 documents: one per
entity kind, plus snapshot/plan/authorization/receipt/manifest, plus an index)
and failing the build when they drift:

```bash
python -m ucm_bridge.tooling.emit_schema --check
```

## 3. Job orchestration: Temporal

**Decision.** Temporal for the Phase 3 execution engine.

**Why.** "A run interrupted at user 8,432 of 20,000 resumes cleanly" is
Temporal's core competence. Durable execution, retries, and rollback sagas are
native rather than hand-built, and hand-building durable resumption is exactly
the kind of subtly-wrong code that loses a customer's cutover weekend.

**Cost.** A Temporal server in every air-gapped customer deployment. This is a
real deployment burden and the strongest argument for the Postgres-backed
alternative; it is accepted because the correctness properties matter more.

**Phase 0 impact.** None at runtime — no Temporal dependency exists yet. It
shaped the *interfaces*: `ApplyPlan.operations_in_dependency_order()`,
`ApplyReport.checkpoint_cursor`, `apply(resume_after_op_id=...)`, and
`RollbackBundle` are all designed to be driven by a durable workflow, and are
already exercised sequentially and under test.

## 4. Credentials: pluggable provider, Vault + env/file backends

**Decision.** Connectors receive a `CredentialRef` and a `CredentialBroker`,
never raw secrets. Backends: `EnvCredentialProvider` (CI/dev),
`LocalFileCredentialProvider` (dev only, refuses unless `UCM_BRIDGE_ENV=dev`),
`VaultCredentialProvider` (production).

**Why.** The air-gap requirement rules out cloud-KMS-only. Vault-only would mean
every developer and every POC needs a Vault instance. Pluggable gives one
interface with backends chosen per deployment.

**Two properties worth naming.** *Scope is part of the reference*: a discovery
connector holds a `READ_ONLY` ref and `Connector.apply()` refuses to execute a
production write with one, which is defence in depth behind the real control
(a read-only service account on the vendor side). And *secrets do not leak*:
`SecretBundle` redacts under `repr`, `str`, and Pydantic serialisation, because
tracebacks are the most common route from a credential to a log aggregator.

**`VaultCredentialProvider` is deliberately unimplemented.** It raises
`NotImplementedError` with a message naming what must be confirmed — Vault
version, KV mount path, auth method. Guessing a plausible-looking Vault
integration would produce something that fails at the worst possible moment. Same
principle as the brief's guardrail about inventing API endpoints.

## Not yet decided

Deferred until the phase that needs them, to avoid speculative commitment:

- **Storage.** PostgreSQL with JSONB snapshots and partitioned append-only audit
  is the presumed default from the brief; no schema exists yet.
- **Frontend.** React + TypeScript with virtualised tables. Phase 2.
- **PowerShell bridge.** A containerised PowerShell 7 sidecar behind an internal
  contract, for Teams and Skype for Business cmdlets. Needed in Phase 2; the
  container runtime is not installed on this machine.
- **Deployment topology.** Control plane plus on-prem collector agent pulling
  work. Phase 7.
