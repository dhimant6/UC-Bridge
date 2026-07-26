"""Wave planning (§4.4) with dependency integrity.

Users are grouped into waves by site, department, cost centre, or hand. The part
that matters is what the planner *refuses*.

Some users cannot be separated. Two people holding appearances of the same
directory number, the members of a hunt group, and a boss/assistant pair are
each a **cluster**: migrate half of one and the shared line rings in the wrong
estate, the hunt group answers from two platforms, or the assistant loses the
executive's calls. So clusters are computed from the canonical graph, and a plan
that splits one is rejected rather than warned about.

Coexistence is the second half. While waves are in flight, the estate is split
across two platforms and calls between them have to keep working. The planner
derives, per wave, exactly which numbers need interop routing in place before
that wave runs.
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.callhandling import (
    CallQueue,
    Delegation,
    HuntGroup,
    LineGroup,
)
from ucm_bridge.canonical.endpoints import Line, SharedLineAppearance
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.snapshot import EstateSnapshot


class GroupingStrategy(StrEnum):
    SITE = "SITE"
    DEPARTMENT = "DEPARTMENT"
    COST_CENTRE = "COST_CENTRE"
    MANUAL = "MANUAL"


class DependencyKind(StrEnum):
    SHARED_LINE = "SHARED_LINE"
    HUNT_GROUP = "HUNT_GROUP"
    CALL_QUEUE = "CALL_QUEUE"
    DELEGATION = "DELEGATION"


class DependencyCluster(BaseModel):
    """A set of users that must migrate together, and why."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    kind: DependencyKind
    user_keys: set[str] = Field(default_factory=set)
    reason: str

    @property
    def size(self) -> int:
        return len(self.user_keys)


class Wave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wave_id: str
    name: str
    sequence: int = Field(ge=1)
    user_keys: list[str] = Field(default_factory=list)
    notes: str | None = None

    @property
    def size(self) -> int:
        return len(self.user_keys)


class DependencyViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    kind: DependencyKind
    reason: str
    split_across: dict[str, list[str]] = Field(
        default_factory=dict, description="wave_id -> the cluster's users in that wave."
    )
    consequence: str


class CoexistenceRequirement(BaseModel):
    """What must keep working between the two estates while a wave is in flight."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    migrated_user_count: int
    remaining_user_count: int
    interop_numbers: list[str] = Field(
        default_factory=list,
        description="Numbers that will be reachable from the other estate during this wave.",
    )
    detail: str


class WavePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_name: str
    strategy: GroupingStrategy
    waves: list[Wave] = Field(default_factory=list)
    clusters: list[DependencyCluster] = Field(default_factory=list)
    violations: list[DependencyViolation] = Field(default_factory=list)
    unassigned_user_keys: list[str] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """A plan that splits a dependency cluster is rejected, not warned about."""
        return not self.violations

    def wave_of(self, user_key: str) -> Wave | None:
        for wave in self.waves:
            if user_key in wave.user_keys:
                return wave
        return None

    def summary(self) -> str:
        sizes = ", ".join(f"{w.name}={w.size}" for w in self.waves)
        state = "valid" if self.is_valid else f"{len(self.violations)} violation(s)"
        return f"{len(self.waves)} wave(s) [{sizes}] - {state}"


# --------------------------------------------------------------------------- #
# Dependency discovery
# --------------------------------------------------------------------------- #


def find_dependency_clusters(snapshot: EstateSnapshot) -> list[DependencyCluster]:
    """Derive the user sets that cannot be separated, from the canonical graph."""
    users = {u.canonical_id: u for u in snapshot.entities if isinstance(u, User)}
    lines = {line.canonical_id: line for line in snapshot.entities if isinstance(line, Line)}

    def user_key(canonical_id: str | None) -> str | None:
        user = users.get(canonical_id or "")
        return user.user_principal_name if user else None

    def owner_of_line(line_ref: str) -> str | None:
        line = lines.get(line_ref)
        return user_key(line.owner_ref) if line else None

    clusters: list[DependencyCluster] = []

    for entity in snapshot.entities:
        if isinstance(entity, SharedLineAppearance):
            keys = {k for k in (user_key(r) for r in entity.user_refs) if k}
            keys |= {k for k in (owner_of_line(r) for r in entity.line_refs) if k}
            if len(keys) > 1:
                clusters.append(
                    DependencyCluster(
                        cluster_id=f"shared-line:{entity.directory_number}",
                        kind=DependencyKind.SHARED_LINE,
                        user_keys=keys,
                        reason=(
                            f"{len(keys)} users hold an appearance of directory number "
                            f"{entity.directory_number}."
                        ),
                    )
                )

        elif isinstance(entity, LineGroup):
            keys = {k for k in (owner_of_line(r) for r in entity.member_line_refs) if k}
            if len(keys) > 1:
                clusters.append(
                    DependencyCluster(
                        cluster_id=f"line-group:{entity.name}",
                        kind=DependencyKind.HUNT_GROUP,
                        user_keys=keys,
                        reason=f"{len(keys)} users are members of line group {entity.name}.",
                    )
                )

        elif isinstance(entity, HuntGroup):
            keys = {k for k in (owner_of_line(r) for r in entity.line_group_refs) if k}
            if len(keys) > 1:
                clusters.append(
                    DependencyCluster(
                        cluster_id=f"hunt-group:{entity.pilot_pattern}",
                        kind=DependencyKind.HUNT_GROUP,
                        user_keys=keys,
                        reason=f"{len(keys)} users answer hunt pilot {entity.pilot_pattern}.",
                    )
                )

        elif isinstance(entity, CallQueue):
            keys = {k for k in (user_key(r) for r in entity.agent_refs) if k}
            if len(keys) > 1:
                clusters.append(
                    DependencyCluster(
                        cluster_id=f"call-queue:{entity.name}",
                        kind=DependencyKind.CALL_QUEUE,
                        user_keys=keys,
                        reason=f"{len(keys)} agents serve call queue {entity.name}.",
                    )
                )

        elif isinstance(entity, Delegation):
            keys = {k for k in (user_key(entity.principal_ref),) if k}
            keys |= {k for k in (user_key(r) for r in entity.delegate_refs) if k}
            if len(keys) > 1:
                principal = user_key(entity.principal_ref) or "?"
                clusters.append(
                    DependencyCluster(
                        cluster_id=f"delegation:{principal}",
                        kind=DependencyKind.DELEGATION,
                        user_keys=keys,
                        reason=(
                            f"{principal} has {len(keys) - 1} delegate(s) who answer on their "
                            "behalf."
                        ),
                    )
                )

    return sorted(clusters, key=lambda c: c.cluster_id)


def merge_clusters(clusters: list[DependencyCluster]) -> list[DependencyCluster]:
    """Merge overlapping clusters transitively.

    If A shares a line with B, and B is in a hunt group with C, then all three
    must move together. Treating the clusters independently would let a plan put
    A and C in different waves while satisfying each rule on its own.
    """
    remaining = list(clusters)
    merged: list[DependencyCluster] = []

    while remaining:
        current = remaining.pop(0)
        keys = set(current.user_keys)
        reasons = [current.reason]
        kinds = {current.kind}

        changed = True
        while changed:
            changed = False
            for candidate in list(remaining):
                if keys & candidate.user_keys:
                    keys |= candidate.user_keys
                    reasons.append(candidate.reason)
                    kinds.add(candidate.kind)
                    remaining.remove(candidate)
                    changed = True

        merged.append(
            DependencyCluster(
                cluster_id=current.cluster_id,
                kind=current.kind if len(kinds) == 1 else DependencyKind.SHARED_LINE,
                user_keys=keys,
                reason=" ".join(reasons),
            )
        )

    return sorted(merged, key=lambda c: c.cluster_id)


# --------------------------------------------------------------------------- #
# Planning and validation
# --------------------------------------------------------------------------- #

_CONSEQUENCE = {
    DependencyKind.SHARED_LINE: (
        "The shared line would exist on two platforms at once. Calls ring in one estate "
        "only, and the other appearance goes dead without warning anyone."
    ),
    DependencyKind.HUNT_GROUP: (
        "Half the group would answer from each platform. Callers reach whichever half the "
        "pilot still points at, and the rest silently stop receiving queue calls."
    ),
    DependencyKind.CALL_QUEUE: (
        "Agents split across platforms cannot share queue state. Calls would be offered "
        "twice or not at all."
    ),
    DependencyKind.DELEGATION: (
        "The assistant would lose the executive's calls mid-migration. This is the most "
        "visible possible failure and it happens to the most visible people."
    ),
}


def validate_waves(
    waves: list[Wave], clusters: list[DependencyCluster]
) -> list[DependencyViolation]:
    """Find clusters split across waves."""
    wave_of: dict[str, str] = {
        key: wave.wave_id for wave in waves for key in wave.user_keys
    }
    violations: list[DependencyViolation] = []

    for cluster in clusters:
        placement: dict[str, list[str]] = defaultdict(list)
        for key in sorted(cluster.user_keys):
            placement[wave_of.get(key, "<unassigned>")].append(key)

        if len(placement) > 1:
            violations.append(
                DependencyViolation(
                    cluster_id=cluster.cluster_id,
                    kind=cluster.kind,
                    reason=cluster.reason,
                    split_across=dict(placement),
                    consequence=_CONSEQUENCE[cluster.kind],
                )
            )

    return violations


def _grouping_key(user: User, strategy: GroupingStrategy) -> str:
    match strategy:
        case GroupingStrategy.SITE:
            return user.site_code or "unsited"
        case GroupingStrategy.DEPARTMENT:
            return user.department or "no-department"
        case GroupingStrategy.COST_CENTRE:
            return user.cost_centre or "no-cost-centre"
        case _:
            return "all"


def plan_waves(
    snapshot: EstateSnapshot,
    *,
    strategy: GroupingStrategy = GroupingStrategy.SITE,
    plan_name: str = "default",
    max_wave_size: int | None = None,
    repair_violations: bool = True,
) -> WavePlan:
    """Group users into waves and keep dependency clusters intact.

    With ``repair_violations`` the planner pulls a split cluster back into the
    earliest wave that holds any of its members, which is the behaviour a
    planner actually wants. With it off, the split is reported so a human can
    decide — some customers would rather resequence than merge.
    """
    users = [u for u in snapshot.entities if isinstance(u, User)]
    clusters = merge_clusters(find_dependency_clusters(snapshot))

    buckets: dict[str, list[str]] = defaultdict(list)
    for user in sorted(users, key=lambda u: u.user_principal_name):
        buckets[_grouping_key(user, strategy)].append(user.user_principal_name)

    waves: list[Wave] = []
    sequence = 0
    for group_name, members in sorted(buckets.items()):
        chunks = (
            [members[i : i + max_wave_size] for i in range(0, len(members), max_wave_size)]
            if max_wave_size
            else [members]
        )
        for index, chunk in enumerate(chunks, start=1):
            sequence += 1
            suffix = f" ({index})" if len(chunks) > 1 else ""
            waves.append(
                Wave(
                    wave_id=f"wave-{sequence:03d}",
                    name=f"{group_name}{suffix}",
                    sequence=sequence,
                    user_keys=list(chunk),
                )
            )

    if repair_violations:
        waves = _repair(waves, clusters)

    return WavePlan(
        plan_name=plan_name,
        strategy=strategy,
        waves=waves,
        clusters=clusters,
        violations=validate_waves(waves, clusters),
        unassigned_user_keys=[],
    )


def _repair(waves: list[Wave], clusters: list[DependencyCluster]) -> list[Wave]:
    """Pull each split cluster into the earliest wave holding one of its members."""
    by_id = {wave.wave_id: list(wave.user_keys) for wave in waves}
    order = [wave.wave_id for wave in sorted(waves, key=lambda w: w.sequence)]

    for cluster in clusters:
        homes = [wid for wid in order if set(by_id[wid]) & cluster.user_keys]
        if len(homes) <= 1:
            continue
        destination = homes[0]
        for wave_id in homes[1:]:
            moving = [k for k in by_id[wave_id] if k in cluster.user_keys]
            by_id[wave_id] = [k for k in by_id[wave_id] if k not in cluster.user_keys]
            by_id[destination].extend(moving)

    repaired = [
        wave.model_copy(update={"user_keys": sorted(by_id[wave.wave_id])})
        for wave in waves
    ]
    # An emptied wave is noise in the console; drop it and resequence.
    kept = [wave for wave in repaired if wave.user_keys]
    return [
        wave.model_copy(update={"sequence": index})
        for index, wave in enumerate(sorted(kept, key=lambda w: w.sequence), start=1)
    ]


def move_user(plan: WavePlan, *, user_key: str, to_wave_id: str) -> WavePlan:
    """Move one user between waves, re-validating. The drag-and-drop operation."""
    waves = [
        wave.model_copy(
            update={
                "user_keys": sorted(
                    [k for k in wave.user_keys if k != user_key]
                    + ([user_key] if wave.wave_id == to_wave_id else [])
                )
            }
        )
        for wave in plan.waves
    ]
    return plan.model_copy(
        update={"waves": waves, "violations": validate_waves(waves, plan.clusters)}
    )


# --------------------------------------------------------------------------- #
# Coexistence
# --------------------------------------------------------------------------- #


def coexistence_requirements(
    plan: WavePlan, snapshot: EstateSnapshot
) -> list[CoexistenceRequirement]:
    """Per wave, what must be routable between the two estates while it runs."""
    users = {u.user_principal_name: u for u in snapshot.entities if isinstance(u, User)}
    numbers_by_ref = {
        e.canonical_id: getattr(e, "e164", None)
        for e in snapshot.entities
        if e.kind == "E164Number"
    }
    total = len(users)

    requirements: list[CoexistenceRequirement] = []
    migrated: set[str] = set()

    for wave in sorted(plan.waves, key=lambda w: w.sequence):
        migrated |= set(wave.user_keys)
        remaining = [key for key in users if key not in migrated]

        # Numbers still on the old platform that the newly-migrated users will
        # need to reach, and vice versa.
        interop = sorted(
            {
                number
                for key in remaining
                if (user := users.get(key)) and user.primary_number_ref
                and (number := numbers_by_ref.get(user.primary_number_ref))
            }
        )

        requirements.append(
            CoexistenceRequirement(
                wave_id=wave.wave_id,
                migrated_user_count=len(migrated),
                remaining_user_count=len(remaining),
                interop_numbers=interop,
                detail=(
                    f"After {wave.name}, {len(migrated)} of {total} user(s) are on the target "
                    f"and {len(remaining)} remain on the source. Interop routing must carry "
                    f"calls between them in both directions for {len(interop)} number(s) "
                    "until the final wave completes."
                )
                if remaining
                else (
                    f"{wave.name} is the final wave. Interop routing can be withdrawn once "
                    "validation passes and the numbers have ported."
                ),
            )
        )

    return requirements


def render_wave_plan_markdown(plan: WavePlan) -> str:
    lines = [
        f"# Wave plan: {plan.plan_name}",
        "",
        f"Grouped by **{plan.strategy.value}** — {plan.summary()}",
        "",
    ]

    if not plan.is_valid:
        lines += ["## PLAN REJECTED", "", "Dependency clusters are split across waves:", ""]
        for violation in plan.violations:
            lines += [
                f"### {violation.cluster_id} ({violation.kind.value})",
                "",
                violation.reason,
                "",
                f"**Consequence.** {violation.consequence}",
                "",
                "Split across:",
                "",
            ]
            lines += [
                f"- `{wave_id}`: {', '.join(keys)}"
                for wave_id, keys in sorted(violation.split_across.items())
            ]
            lines.append("")

    lines += ["## Waves", "", "| # | Wave | Users |", "|---:|---|---:|"]
    lines += [
        f"| {wave.sequence} | {wave.name} | {wave.size} |"
        for wave in sorted(plan.waves, key=lambda w: w.sequence)
    ]
    lines.append("")

    if plan.clusters:
        lines += ["## Dependency clusters kept intact", ""]
        lines += [
            f"- **{c.cluster_id}** ({c.size} users): {c.reason}"
            for c in plan.clusters
        ]
        lines.append("")

    return "\n".join(lines)
