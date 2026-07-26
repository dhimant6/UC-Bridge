# ADR-0001: A canonical UC model as the migration intermediate representation

- **Status**: Accepted
- **Date**: 2026-07-26
- **Phase**: 0

## Context

UCM-Bridge moves configuration and identity state between Cisco CUCM/Unity, Avaya
Aura, Skype for Business Server, Mitel, Cisco UCCX/UCCE, Microsoft Teams Phone,
Slack, and Genesys Cloud CX — in **both** directions, including cloud-to-on-prem
repatriation.

Taken pairwise and directionally, that is a large number of possible migration
paths, and the set grows quadratically with each platform added. Writing a
migrator per pair is not a scaling problem to be solved later; it is a design
that fails on the third platform.

Two further constraints shape the decision:

1. **Repatriation is a first-class path**, not a reversal bolted on afterwards.
   Anything that treats "legacy → cloud" as the primary direction will encode
   that assumption in a hundred places.
2. **Fidelity must be knowable before the customer commits.** The product's
   value is largely in telling the truth about what will not migrate. That
   requires a representation in which "what was lost" is a first-class,
   inspectable property rather than an artefact of a particular code path.

## Decision

Adopt a hub-and-spoke architecture with a versioned canonical model as the hub:

```
Source Connector ──Extract──▶ Canonical UC Model ──Apply──▶ Target Connector
```

Every connector implements the same two directions against the same model.
Adding a platform is one connector. Reverse migration is the identical pipeline
with source and target swapped — there is no separate reverse code path, and
therefore no reverse code path that can rot.

Specifically:

- **74 canonical entity kinds across 10 domains** (identity, numbering, dial
  plan, endpoints, call handling, trunking, messaging, contact centre,
  collaboration, policy), defined as Pydantic v2 models and published as JSON
  Schema (see ADR-0002).
- **Every entity carries** `canonicalId`, `sourceRef`, `targetRef`, `fidelity`,
  `transformLog[]`, and `checksum`, per the brief.
- **Snapshots are the unit of persistence**, replay, diff, and audit.

### Four design choices worth defending

**1. Deterministic identity.** `canonical_id` is a UUIDv5 over
`(platform, instance_id, kind, native_key)` with a fixed namespace. Re-extracting
the same object from the same cluster always yields the same id. This is what
makes discovery re-runs diffable, plans regenerable, and interrupted applies
resumable without creating duplicates. The cost is that changing the namespace
constant invalidates every persisted id, so it is treated as immutable.

**2. Two digests, not one.** `checksum` covers content *and* provenance and is
the basis for snapshot diffing. `semantic_digest()` strips identity and
provenance and is the basis for the within-estate idempotency check. Both exclude
`target_ref`, `transform_log`, `fidelity`, and `tags`, because applying an entity,
logging a transform, or re-running the assessment engine are not content changes
and must not invalidate a digest.

There is a limit worth stating plainly: `semantic_digest()` is **not**
comparable across estates, because reference fields still hold canonical ids and
those are platform-scoped. Cross-estate reconciliation therefore resolves
references to *natural keys* first (`pipeline/reconcile.py`). A Munich user's
`primary_number_ref` becomes `+498912345101` on both sides, and only then are the
two estates comparable. This was found while building the reference connector,
and it is the reason reconciliation is a separate module rather than a digest
comparison.

**3. Fidelity is pessimistic by construction.** See
[`docs/fidelity-taxonomy.md`](../fidelity-taxonomy.md). The short version: a
fresh entity is `DEGRADED`/unassessed, `LOSSLESS` is a claim requiring evidence,
and the validation rules make an unjustified claim impossible to construct.

**4. Unmapped source data is retained, not dropped.** Raw source attributes live
verbatim in `source_ref.native_attributes`. The canonical model not understanding
an attribute is a *reportable state*, not a silent loss.

## Fidelity trade-offs of the canonical model itself

The hub is a lossy abstraction, and pretending otherwise would undermine the
whole product. The specific costs:

**Concepts that fit no common shape.** Avaya vectors, UCCX scripts, and RGS
workflows are Turing-complete-ish flow logic. `AutoAttendant` and
`RoutingStrategy` model the declarative subset and carry
`source_flow_reference` plus `complexity_score` pointing back at the original.
A vector will essentially never be `LOSSLESS`, and the model says so rather than
implying otherwise.

**Double translation loses more than single.** CUCM CSS → canonical
`CallingPermission` → Teams voice routing policy can lose detail at both hops
where a direct CUCM→Teams mapper might have preserved it. This is the central
trade of the architecture. It is accepted because: the losses are *declared* at
each hop in `transform_log`, N+M connectors stay maintainable where N×M does not,
and `derived_from` on the canonical entity records the native construct so a
reverse transform can aim back at the right one.

**Deliberate non-collapsing.** `HuntGroup` and `CallQueue` are kept as separate
entities even though the brief lists them on one row. A CUCM hunt pilot plus line
group has no agent-presence model and no queued-caller experience; a Teams call
queue has no line-group chaining. Collapsing them would hide exactly the
degradation the fidelity report exists to surface. The same reasoning keeps
`Line` distinct from `Extension` (a dialable string versus one appearance of it)
and `RecordingPolicy` distinct from `ComplianceRecordingPolicy` (an operational
setting versus a regulatory obligation).

**Ordering semantics.** Reachability is expressed as an *ordered* partition list,
because CUCM CSS, Avaya COR, and Teams voice routing policies are all
order-sensitive and an unordered set would silently change call routing.

**One addition beyond the brief's minimum set.** `VoiceRoute` was added to the
trunking domain. `PSTNUsage` references routes, and the Teams → CUCM transform
(voice routing policy + PSTN usage + online voice routes → route patterns over a
route list) cannot be expressed without them.

## Alternatives considered

**Point-to-point migrators.** Rejected: quadratic growth, and every pair would
re-derive its own fidelity reporting, so the fidelity report — the product's main
differentiator — would be inconsistent across paths.

**Use a target vendor's model as the hub** (e.g. everything through the Teams
model). Rejected: it makes repatriation second-class by construction, and any
concept the chosen vendor lacks becomes invisible rather than `UNMAPPABLE`. That
is precisely the failure mode this product exists to prevent.

**A generic property-bag model** (entities as untyped key-value maps). Rejected:
no schema means no validation, no meaningful diff, no static typing, and no way
to state that an emergency location requires a dispatchable civic address. The
safety-critical invariants would become convention.

**Model only what the first two connectors need, grow later.** Tempting, and
partly what Phase 0 does in depth-of-attribute terms. Rejected at the level of
*entity kinds*: the brief's build order puts the whole model in Phase 0 precisely
because retrofitting entity kinds later invalidates persisted snapshots and
forces a model version bump.

## Consequences

**Good.** One connector per platform. Reverse migration free. Fidelity reporting
uniform across every path. Snapshots replayable and diffable. Split-target
migrations (voice to Teams, collaboration to Slack) fall out of capability
manifests rather than needing special-case code.

**Costs.** Double translation loses more than a bespoke pairwise mapper would.
The model is large (74 kinds) and every new entity kind is a versioned, breaking
change. Connector authors must understand the canonical model as well as their
own platform.

**Accepted risks.** `CANONICAL_ID_NAMESPACE` and the `canonical_json` encoding are
effectively immutable — changing either invalidates every persisted id or digest.
Both are isolated in `canonical/base.py` with comments saying so.

## Verification

The contract is proven, not asserted:
`User + Line + E164Number + EmergencyLocation` round-trip source → canonical →
target → canonical and reconcile attribute-by-attribute, with zero writes to the
source, a mandatory dry run, an idempotent re-run, checkpoint resumption, and a
rollback bundle that empties the target again.

73 tests, `tests/test_reference_roundtrip.py` in particular.
