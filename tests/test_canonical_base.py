"""Canonical model invariants: identity, digests, and the fidelity taxonomy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ucm_bridge.canonical import (
    CanonicalEntity,
    DegradedAttribute,
    EstateSnapshot,
    FidelityAssessment,
    FidelityLevel,
    Platform,
    SourceRef,
    all_entity_types,
    entity_adapter,
)
from ucm_bridge.canonical.identity import User
from ucm_bridge.canonical.numbering import E164Number


def make_user(username: str = "amueller", **overrides: object) -> User:
    return User(
        canonical_id=CanonicalEntity.mint_canonical_id(
            Platform.CISCO_CUCM, "User", username, instance_id="cluster-1"
        ),
        user_principal_name=username,
        source_ref=SourceRef(
            platform=Platform.CISCO_CUCM,
            instance_id="cluster-1",
            native_type="User",
            native_key=username,
        ),
        **overrides,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def test_canonical_id_is_deterministic_across_runs() -> None:
    first = CanonicalEntity.mint_canonical_id(
        Platform.CISCO_CUCM, "User", "amueller", instance_id="cluster-1"
    )
    second = CanonicalEntity.mint_canonical_id(
        Platform.CISCO_CUCM, "User", "amueller", instance_id="cluster-1"
    )
    assert first == second


def test_canonical_id_separates_instances_and_platforms() -> None:
    cluster_one = CanonicalEntity.mint_canonical_id(
        Platform.CISCO_CUCM, "User", "amueller", instance_id="cluster-1"
    )
    cluster_two = CanonicalEntity.mint_canonical_id(
        Platform.CISCO_CUCM, "User", "amueller", instance_id="cluster-2"
    )
    teams = CanonicalEntity.mint_canonical_id(
        Platform.MICROSOFT_TEAMS, "User", "amueller", instance_id="cluster-1"
    )
    assert len({cluster_one, cluster_two, teams}) == 3


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #


def test_checksum_is_stable_and_detects_mutation() -> None:
    user = make_user().seal()
    assert user.verify_checksum()

    user.department = "Finance"
    assert not user.verify_checksum(), "a content change must invalidate the checksum"

    user.seal()
    assert user.verify_checksum()


def test_checksum_ignores_volatile_and_derived_fields() -> None:
    """Applying, logging a transform, or reassessing fidelity is not a content change."""
    from ucm_bridge.canonical.base import TargetRef, TransformOperation

    user = make_user().seal()
    original = user.checksum

    user.target_ref = TargetRef(
        platform=Platform.MICROSOFT_TEAMS,
        instance_id="contoso.onmicrosoft.com",
        native_type="CsOnlineUser",
        native_key="8f14e45f",
    )
    user.log(TransformOperation.APPLY, actor="test", summary="applied")
    user.fidelity = FidelityAssessment.lossless("verified by hand", assessed_by="test")
    user.tags["wave"] = "wave-3"

    assert user.compute_checksum() == original


def test_semantic_digest_ignores_provenance_but_not_content() -> None:
    cucm = make_user()
    same_user_elsewhere = User(
        canonical_id=CanonicalEntity.mint_canonical_id(
            Platform.MICROSOFT_TEAMS, "User", "amueller", instance_id="tenant-1"
        ),
        user_principal_name="amueller",
        source_ref=SourceRef(
            platform=Platform.MICROSOFT_TEAMS,
            instance_id="tenant-1",
            native_type="CsOnlineUser",
            native_key="8f14e45f",
        ),
    )
    assert cucm.semantic_digest() == same_user_elsewhere.semantic_digest()

    same_user_elsewhere.department = "Finance"
    assert cucm.semantic_digest() != same_user_elsewhere.semantic_digest()


# --------------------------------------------------------------------------- #
# Fidelity taxonomy
# --------------------------------------------------------------------------- #


def test_new_entities_are_never_lossless_by_default() -> None:
    user = make_user()
    assert user.fidelity.level is FidelityLevel.DEGRADED
    assert not user.fidelity.is_assessed


def test_every_entity_kind_defaults_to_pessimistic_fidelity() -> None:
    """The guardrail must hold for all 70+ kinds, not just the ones with tests."""
    for kind, cls in all_entity_types().items():
        default = cls.model_fields["fidelity"].get_default(call_default_factory=True)
        assert default.level is not FidelityLevel.LOSSLESS, f"{kind} defaults to LOSSLESS"
        assert not default.is_assessed, f"{kind} defaults to an assessed fidelity"


def test_lossless_requires_a_rationale() -> None:
    with pytest.raises(ValidationError, match="LOSSLESS requires an explicit rationale"):
        FidelityAssessment(level=FidelityLevel.LOSSLESS)


def test_lossless_cannot_coexist_with_recorded_losses() -> None:
    with pytest.raises(ValidationError, match="contradicts recorded unmapped/degraded"):
        FidelityAssessment(
            level=FidelityLevel.LOSSLESS,
            rationale="everything mapped",
            unmapped_source_attributes=["corClass"],
        )


def test_assessed_degraded_must_describe_the_degradation() -> None:
    with pytest.raises(ValidationError, match="must describe at least one degraded attribute"):
        FidelityAssessment(level=FidelityLevel.DEGRADED, rationale="some things were lost")


def test_unmappable_must_quantify_manual_work() -> None:
    with pytest.raises(ValidationError, match="requires manual_effort_minutes"):
        FidelityAssessment(level=FidelityLevel.UNMAPPABLE, rationale="no target equivalent")

    assessment = FidelityAssessment.unmappable(
        "Extension mobility has no Teams equivalent",
        assessed_by="test",
        manual_effort_minutes=30,
    )
    assert assessment.manual_effort_minutes == 30


def test_degraded_helper_records_named_attributes() -> None:
    assessment = FidelityAssessment.degraded(
        "Shared appearances are lost",
        [
            DegradedAttribute(
                attribute="shared_appearance_ref",
                reason="no target equivalent",
                target_behaviour="Single appearance only",
            )
        ],
        assessed_by="test",
    )
    assert assessment.level is FidelityLevel.DEGRADED
    assert assessment.is_assessed
    assert assessment.degraded_attributes[0].attribute == "shared_appearance_ref"


# --------------------------------------------------------------------------- #
# Validation of domain invariants
# --------------------------------------------------------------------------- #


def test_e164_format_is_enforced() -> None:
    with pytest.raises(ValidationError):
        E164Number(canonical_id="x", e164="0208 123 4567")


def test_assigned_number_must_say_what_it_is_assigned_to() -> None:
    from ucm_bridge.canonical.numbering import NumberAssignmentState

    with pytest.raises(ValidationError, match="must say what it is assigned to"):
        E164Number(
            canonical_id="x",
            e164="+442071838750",
            assignment_state=NumberAssignmentState.ASSIGNED,
        )


def test_emergency_location_confirmation_requires_attribution() -> None:
    from ucm_bridge.canonical.policy import CivicAddress, EmergencyLocation

    address = CivicAddress(country="GB", street_name="Bishopsgate", city="London")
    with pytest.raises(ValidationError, match="never confirmed anonymously"):
        EmergencyLocation(
            canonical_id="x",
            name="London",
            site_code="LON-BR",
            civic_address=address,
            confirmed_for_migration=True,
        )


def test_emergency_location_validation_requires_an_authority() -> None:
    from ucm_bridge.canonical.policy import CivicAddress, EmergencyLocation

    address = CivicAddress(country="GB", street_name="Bishopsgate", city="London")
    with pytest.raises(ValidationError, match="not evidence"):
        EmergencyLocation(
            canonical_id="x",
            name="London",
            site_code="LON-BR",
            civic_address=address,
            is_validated=True,
        )


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def test_snapshot_round_trips_through_json_without_losing_subclass_fields() -> None:
    import json

    user = make_user(department="Finance")
    snapshot = EstateSnapshot.build(
        snapshot_id="snap-1",
        tenant_id="contoso",
        estate_id="cluster-1",
        entities=[user],
    )

    payload = json.loads(snapshot.to_json())
    assert payload["entities"][0]["department"] == "Finance"

    rebuilt = EstateSnapshot.from_obj(payload)
    restored = rebuilt.entities[0]
    assert isinstance(restored, User)
    assert restored.department == "Finance"
    assert restored.checksum == user.checksum


def test_entity_adapter_discriminates_on_kind() -> None:
    adapter = entity_adapter()
    entity = adapter.validate_python(
        {"kind": "E164Number", "canonical_id": "x", "e164": "+442071838750"}
    )
    assert isinstance(entity, E164Number)


def test_identical_snapshots_diff_to_nothing() -> None:
    user = make_user()
    first = EstateSnapshot.build(
        snapshot_id="a", tenant_id="t", estate_id="e", entities=[make_user()]
    )
    second = EstateSnapshot.build(
        snapshot_id="b", tenant_id="t", estate_id="e", entities=[user]
    )
    assert first.diff(second).is_empty
