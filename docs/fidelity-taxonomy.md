# Fidelity taxonomy

Every canonical entity carries a `fidelity` assessment describing how faithfully
it survives the transform. This document defines the three levels, the evidence
each one requires, and the rules the code enforces.

The taxonomy exists because the alternative — a migration tool that reports
success and lets the customer discover the losses at cutover — is how UC
migrations acquire their reputation. A declared degradation is a project task. An
undeclared one is an incident.

## The three levels

| Level | Meaning | Evidence required |
|---|---|---|
| `LOSSLESS` | Every semantically significant source attribute has an equivalent on the target, and observable behaviour is preserved. | A non-default `rationale`, and **no** recorded unmapped or degraded attributes. |
| `DEGRADED` | The entity migrates, but behaviour or configuration changes in a way a user or administrator could notice. | At least one `DegradedAttribute`, each naming the attribute, the reason, and — most importantly — what will actually happen on the target instead. |
| `UNMAPPABLE` | No target equivalent exists. The entity cannot be applied and becomes manual work. | `manual_effort_minutes`, so the assessment report can total the human cost. |

### The fourth state: unassessed

A freshly constructed entity is `DEGRADED` with the rationale
`"Not yet assessed: no fidelity rule has evaluated this entity."`.
`FidelityAssessment.is_assessed` distinguishes this from a real `DEGRADED`
verdict.

This is the guardrail *"never mark an entity `LOSSLESS` by default"* expressed as
a default value rather than a code-review convention. An entity nobody has
examined is pessimistic, not optimistic, and
`EstateSnapshot.unassessed()` lists everything still in that state so a plan
cannot be approved on the basis of assessments that were never made.

## Rules enforced in code

These are validated on `FidelityAssessment` itself, so an invalid claim cannot be
constructed — not by a connector, not by the mapping engine, not by a test
fixture.

1. **`LOSSLESS` requires a rationale.** Constructing `LOSSLESS` while still
   carrying the unassessed rationale raises.
2. **`LOSSLESS` cannot coexist with recorded losses.** If
   `unmapped_source_attributes` or `degraded_attributes` is non-empty, the claim
   is self-contradictory and raises.
3. **An assessed `DEGRADED` must describe the degradation.** A `DEGRADED` verdict
   with no named attribute says nothing actionable, so it raises.
4. **`UNMAPPABLE` must quantify manual work.** Without
   `manual_effort_minutes` the assessment report cannot tell a customer what the
   migration actually costs them, so it raises.
5. **`UNMAPPABLE` entities are never written.** The planner excludes them from
   plans by default, and the connector base rejects any plan containing one.

## Where fidelity is *not* counted

`fidelity` is excluded from both `checksum` and `semantic_digest`. Fidelity is a
derived judgement *about* content, not content itself. Re-running the assessment
engine with better rules must not invalidate every checksum in a persisted
snapshot, and must not make an idempotent re-run look like a change.

## Fidelity is not reconciliation

They answer different questions and you need both:

- **Reconciliation** (`pipeline/reconcile.py`) asks *did every canonical object I
  intended to write arrive intact on the target?*
- **Fidelity** asks *what did the canonical model itself fail to carry?*

The reference connector demonstrates the gap deliberately. Extension `5199` is a
shared line held by two users. The connector does not yet extract
`SharedLineAppearance`, so the second appearance is lost — and reconciliation
still **passes**, because every canonical object that was planned did arrive. Only
the fidelity report says the appearance was lost. A platform reporting just the
reconciliation result would be lying by omission.

This is asserted directly in
`tests/test_reference_roundtrip.py::test_reconciliation_passes_while_fidelity_still_reports_the_shared_line_loss`.

## Nothing is silently dropped

Source attributes the canonical model does not understand are retained verbatim
in `source_ref.native_attributes`. They are not lost; they are *unmapped*, which
is a reportable state. `CanonicalEntity.unmapped_source_attributes()` exposes the
raw floor, and a connector declares what it actually mapped via
`fidelity.unmapped_source_attributes`.

## Manifest-level versus entity-level

`EntityCapability.expected_fidelity` is the **best case** for an entity kind on a
platform — useful for planning before any discovery has run. A per-object
assessment can always come out worse; it can never come out better than the
evidence supports.

Slack is the instructive case: it declares the entire numbering, dial-plan, and
trunking domains `UNMAPPABLE` at manifest level. That declaration is what lets
the planner route voice workloads to Teams or Genesys and collaboration workloads
to Slack in a split-target migration, instead of discovering the problem
per-object at apply time.

## Rolling up

`EstateSnapshot.fidelity_report()` produces the per-entity-kind breakdown that
becomes §4.2's fidelity report:

```python
{
  "E164Number": {"LOSSLESS": 5, "DEGRADED": 0, "UNMAPPABLE": 0},
  "User":       {"LOSSLESS": 0, "DEGRADED": 4, "UNMAPPABLE": 0},
}
```

`EstateSnapshot.manual_effort_minutes()` totals the manual work implied by the
whole estate.
