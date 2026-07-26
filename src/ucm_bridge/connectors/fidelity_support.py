"""Shared helpers for connectors making honest fidelity claims.

Every source connector faces the same question: the platform handed me thirty
attributes, my canonical mapping consumed eleven, what do I say about the other
nineteen? These helpers answer it the same way everywhere, so fidelity reporting
is consistent across platforms rather than depending on each author's diligence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ucm_bridge.canonical.base import (
    DegradedAttribute,
    FidelityAssessment,
    FidelityLevel,
    utcnow,
)

#: Source keys that carry no migration meaning and should not count as losses.
DEFAULT_IGNORED_KEYS: frozenset[str] = frozenset(
    {
        "uuid",
        "pkid",
        "ctiid",
        "_attrs",
        "sequence",
        "@uuid",
        "@ctiid",
        "@sequence",
    }
)


def unmapped_keys(
    record: Mapping[str, Any],
    consumed: Iterable[str],
    *,
    ignore: frozenset[str] = DEFAULT_IGNORED_KEYS,
) -> list[str]:
    """Source keys with a meaningful value that no canonical field claimed."""
    taken = set(consumed) | ignore
    return sorted(
        key
        for key, value in record.items()
        if key not in taken and value not in (None, "", [], {})
    )


def assess_mapping(
    record: Mapping[str, Any],
    consumed: Iterable[str],
    *,
    assessed_by: str,
    entity_label: str,
    lossless_rationale: str,
    target_behaviour: str | None = None,
    extra_degraded: list[DegradedAttribute] | None = None,
    manual_effort_minutes: int | None = None,
    ignore: frozenset[str] = DEFAULT_IGNORED_KEYS,
) -> FidelityAssessment:
    """Produce a fidelity assessment from what the mapping actually consumed.

    LOSSLESS is only returned when nothing was left over *and* the caller has no
    additional degradations to declare — it is never the fallback.
    """
    leftover = unmapped_keys(record, consumed, ignore=ignore)
    degraded = list(extra_degraded or [])

    if leftover:
        degraded.append(
            DegradedAttribute(
                attribute=f"{entity_label}.<unmapped>",
                reason=(
                    f"{len(leftover)} source attribute(s) have no canonical field: "
                    f"{', '.join(leftover[:12])}"
                    + (" ..." if len(leftover) > 12 else "")
                ),
                target_behaviour=(
                    target_behaviour
                    or "These settings are retained in the snapshot for reference but are not "
                    "recreated on the target; review whether any of them are load-bearing."
                ),
            )
        )

    if not degraded:
        return FidelityAssessment.lossless(lossless_rationale, assessed_by=assessed_by)

    # Constructed directly rather than via the helper + model_copy, so the
    # validators actually run on the finished assessment.
    return FidelityAssessment(
        level=FidelityLevel.DEGRADED,
        rationale=f"{entity_label} carries across with declared gaps.",
        degraded_attributes=degraded,
        unmapped_source_attributes=leftover,
        manual_effort_minutes=manual_effort_minutes,
        assessed_by=assessed_by,
        assessed_at=utcnow(),
    )
