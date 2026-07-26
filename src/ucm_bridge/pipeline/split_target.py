"""Split-target routing: sending each workload to the platform that can hold it.

A real migration frequently has more than one destination — voice to Teams
Phone, collaboration to Slack, contact centre to Genesys. Deciding which target
gets which entity is a planning question, and answering it from capability
manifests rather than hardcoded rules is what makes the answer maintainable.

The rule is simple and strict:

* An entity goes to a target that declares it **appliable**.
* If several can take it, the caller's declared preference order decides.
* If exactly one can, it goes there whether or not it was preferred.
* If none can, it is **orphaned** — reported loudly, never silently dropped.
* If a target declares the kind ``UNMAPPABLE``, that target is excluded even if
  it somehow also declared it appliable. Slack saying "I have no telephony" is
  the whole reason this module can be trusted with a voice workload.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity
from ucm_bridge.connectors.capabilities import CapabilityManifest


class TargetCapabilityView(BaseModel):
    """What one candidate target can take. Derived from its manifest."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str
    display_name: str
    appliable_kinds: set[str] = Field(default_factory=set)
    unmappable_kinds: set[str] = Field(default_factory=set)

    @classmethod
    def from_manifest(cls, manifest: CapabilityManifest) -> TargetCapabilityView:
        return cls(
            connector_id=manifest.connector_id,
            display_name=manifest.display_name,
            appliable_kinds=manifest.appliable_kinds(),
            unmappable_kinds=manifest.unmappable_kinds(),
        )

    def can_take(self, kind: str) -> bool:
        if kind in self.unmappable_kinds:
            return False
        return kind in self.appliable_kinds


class OrphanedWorkload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_kind: str
    canonical_ids: list[str] = Field(default_factory=list)
    count: int = 0
    reason: str
    rejected_by: dict[str, str] = Field(
        default_factory=dict, description="connector_id -> why it cannot take this kind."
    )


class SplitPlan(BaseModel):
    """Which entities go to which target, and what nothing can take."""

    model_config = ConfigDict(extra="forbid")

    assignments: dict[str, list[str]] = Field(
        default_factory=dict, description="connector_id -> canonical_ids routed there."
    )
    kinds_by_target: dict[str, list[str]] = Field(default_factory=dict)
    orphans: list[OrphanedWorkload] = Field(default_factory=list)
    ambiguous: dict[str, list[str]] = Field(
        default_factory=dict,
        description="entity kind -> connectors that could all take it; preference decided.",
    )

    @property
    def is_complete(self) -> bool:
        """True when every entity found a home."""
        return not self.orphans

    def target_for(self, canonical_id: str) -> str | None:
        for connector_id, ids in self.assignments.items():
            if canonical_id in ids:
                return connector_id
        return None

    def summary(self) -> str:
        parts = [
            f"{connector_id}={len(ids)}" for connector_id, ids in sorted(self.assignments.items())
        ]
        orphaned = sum(o.count for o in self.orphans)
        return f"{', '.join(parts) or 'nothing routed'}; {orphaned} orphaned"


def plan_split_target(
    entities: Iterable[CanonicalEntity],
    targets: list[CapabilityManifest],
    *,
    preference: list[str] | None = None,
) -> SplitPlan:
    """Route each entity to a target that can actually hold it.

    ``preference`` is an ordered list of connector ids. It only breaks ties; it
    never sends an entity to a target that cannot take it.
    """
    views = [TargetCapabilityView.from_manifest(m) for m in targets]
    by_id = {view.connector_id: view for view in views}
    order = [c for c in (preference or []) if c in by_id] + [
        view.connector_id for view in views if view.connector_id not in (preference or [])
    ]

    assignments: dict[str, list[str]] = {view.connector_id: [] for view in views}
    kinds_by_target: dict[str, set[str]] = {view.connector_id: set() for view in views}
    orphans_by_kind: dict[str, OrphanedWorkload] = {}
    ambiguous: dict[str, list[str]] = {}

    for entity in entities:
        candidates = [cid for cid in order if by_id[cid].can_take(entity.kind)]

        if not candidates:
            orphan = orphans_by_kind.get(entity.kind)
            if orphan is None:
                orphan = OrphanedWorkload(
                    entity_kind=entity.kind,
                    reason=(
                        f"No configured target can apply {entity.kind}. This workload has "
                        "nowhere to go and will be lost unless a target is added or the "
                        "objects are handled manually."
                    ),
                    rejected_by={
                        view.connector_id: (
                            f"declares {entity.kind} UNMAPPABLE"
                            if entity.kind in view.unmappable_kinds
                            else f"does not support applying {entity.kind}"
                        )
                        for view in views
                    },
                )
                orphans_by_kind[entity.kind] = orphan
            orphan.canonical_ids.append(entity.canonical_id)
            orphan.count += 1
            continue

        if len(candidates) > 1:
            ambiguous.setdefault(entity.kind, candidates)

        chosen = candidates[0]
        assignments[chosen].append(entity.canonical_id)
        kinds_by_target[chosen].add(entity.kind)

    return SplitPlan(
        assignments={k: v for k, v in assignments.items() if v},
        kinds_by_target={
            k: sorted(v) for k, v in kinds_by_target.items() if v
        },
        orphans=sorted(orphans_by_kind.values(), key=lambda o: o.entity_kind),
        ambiguous=ambiguous,
    )


def describe_split(plan: SplitPlan, targets: list[CapabilityManifest]) -> str:
    """Human-readable routing summary for the wave planner and the runbook."""
    names = {m.connector_id: m.display_name for m in targets}
    lines = ["# Split-target routing", ""]

    for connector_id, kinds in sorted(plan.kinds_by_target.items()):
        count = len(plan.assignments.get(connector_id, []))
        lines += [
            f"## {names.get(connector_id, connector_id)}",
            "",
            f"{count} object(s) across: {', '.join(kinds)}",
            "",
        ]

    if plan.ambiguous:
        lines += ["## Routed by preference", ""]
        lines += [
            f"- `{kind}` could go to {', '.join(candidates)}; "
            f"chose **{candidates[0]}**"
            for kind, candidates in sorted(plan.ambiguous.items())
        ]
        lines.append("")

    if plan.orphans:
        lines += ["## Orphaned workloads", "", "**These have nowhere to go.**", ""]
        for orphan in plan.orphans:
            lines.append(f"### {orphan.entity_kind} ({orphan.count})")
            lines.append("")
            lines.append(orphan.reason)
            lines.append("")
            lines.extend(
                f"- `{connector}`: {why}" for connector, why in sorted(orphan.rejected_by.items())
            )
            lines.append("")

    return "\n".join(lines)
