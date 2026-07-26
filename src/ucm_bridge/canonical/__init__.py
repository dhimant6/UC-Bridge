"""The canonical UC model: a vendor-neutral intermediate representation.

Source connectors extract into this model; target connectors apply from it.
Adding a platform is one connector, not one connector per platform pair, and
reverse migration is the same pipeline with the ends swapped.
"""

from ucm_bridge.canonical.base import (
    CANONICAL_MODEL_VERSION,
    E164,
    CanonicalEntity,
    CanonicalId,
    DegradedAttribute,
    FidelityAssessment,
    FidelityLevel,
    Platform,
    SourceRef,
    TargetRef,
    TransformLogEntry,
    TransformOperation,
    digest_of,
    utcnow,
)
from ucm_bridge.canonical.registry import (
    ENTITY_REGISTRY,
    all_entity_types,
    canonical_entity,
    entity_adapter,
    entity_class,
)
from ucm_bridge.canonical.snapshot import (
    ChangeType,
    EntityChange,
    EstateSnapshot,
    SnapshotDiff,
    SnapshotKind,
)

__all__ = [
    "CANONICAL_MODEL_VERSION",
    "E164",
    "ENTITY_REGISTRY",
    "CanonicalEntity",
    "CanonicalId",
    "ChangeType",
    "DegradedAttribute",
    "EntityChange",
    "EstateSnapshot",
    "FidelityAssessment",
    "FidelityLevel",
    "Platform",
    "SnapshotDiff",
    "SnapshotKind",
    "SourceRef",
    "TargetRef",
    "TransformLogEntry",
    "TransformOperation",
    "all_entity_types",
    "canonical_entity",
    "digest_of",
    "entity_adapter",
    "entity_class",
    "utcnow",
]
