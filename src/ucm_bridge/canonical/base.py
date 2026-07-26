"""Foundations of the canonical UC model.

Everything in :mod:`ucm_bridge.canonical` derives from :class:`CanonicalEntity`.
The invariants defended here are the ones the rest of the platform relies on:

* **Deterministic identity.** ``canonical_id`` is a UUIDv5 of
  ``(platform, kind, native_key)``. Re-extracting the same object from the same
  estate always yields the same id, which is what makes runs replayable and
  re-runs idempotent.
* **Deterministic content digests.** Two digests, deliberately:
  ``checksum`` (this extraction, including where it came from) and
  ``semantic_digest()`` (platform-neutral content only). Reconciliation and
  idempotency proofs compare the latter; snapshot diffing compares the former.
* **Fidelity is never optimistic.** A freshly constructed entity is
  ``DEGRADED``/unassessed. ``LOSSLESS`` is a claim that must be justified and is
  rejected by validation if any attribute is recorded as unmapped or degraded.
  See ``docs/fidelity-taxonomy.md``.
* **Nothing is silently dropped.** Source attributes the model does not
  understand are retained verbatim in ``source_ref.native_attributes`` so the
  assessment engine can report on them rather than losing them.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CANONICAL_MODEL_VERSION = "1.0.0"
"""Semantic version of the canonical model. Bumped on any breaking entity change.

Persisted into every snapshot so an old snapshot can always be read back by the
version of the code that understands it.
"""

CANONICAL_ID_NAMESPACE = uuid.UUID("6b1f5a2c-9d43-4f8e-9a17-0c2e7b4d5a91")
"""Fixed UUIDv5 namespace. Changing this invalidates every persisted canonical id."""


# --------------------------------------------------------------------------- #
# Shared scalar types
# --------------------------------------------------------------------------- #

CanonicalId = Annotated[
    str,
    Field(
        min_length=1,
        description="canonical_id of another entity in the same snapshot (a reference).",
    ),
]

E164 = Annotated[
    str,
    Field(
        pattern=r"^\+[1-9]\d{1,14}$",
        description="Number in strict E.164 form, e.g. +442071838750.",
    ),
]


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(payload: Any) -> str:
    """Stable JSON encoding: sorted keys, no incidental whitespace.

    This is the only serialisation permitted as digest input. Any change to it
    changes every digest in the system, so it is deliberately boring.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def digest_of(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Platform and provenance
# --------------------------------------------------------------------------- #


class Platform(StrEnum):
    """Platforms the canonical model can describe.

    Adding a member here does not imply a working connector exists; the
    connector's capability manifest is the authority on what is actually
    supported.
    """

    CISCO_CUCM = "cisco.cucm"
    CISCO_UNITY_CONNECTION = "cisco.unity_connection"
    CISCO_UCCX = "cisco.uccx"
    CISCO_UCCE = "cisco.ucce"
    AVAYA_AURA = "avaya.aura"
    AVAYA_AURA_MESSAGING = "avaya.aura_messaging"
    MICROSOFT_SFB_SERVER = "microsoft.sfb_server"
    MICROSOFT_TEAMS = "microsoft.teams"
    SLACK = "slack"
    GENESYS_CLOUD = "genesys.cloud"
    MITEL = "mitel"
    GENERIC_SIP = "generic.sip"
    REFERENCE_MEMORYPBX = "reference.memorypbx"
    """Fake in-memory platform used by the reference connector and test fixtures."""


class SourceRef(BaseModel):
    """Where this entity came from. Populated by ``Extract``, never by ``Apply``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    platform: Platform
    instance_id: str = Field(
        description="Cluster / tenant / workspace identifier. Distinguishes two CUCM "
        "clusters or two Teams tenants belonging to the same customer."
    )
    native_type: str = Field(
        description="Vendor's own name for the object type, e.g. 'Phone', 'CsOnlineUser', "
        "'station'. Kept verbatim for audit."
    )
    native_key: str = Field(
        description="Vendor primary key: AXL pkid, Graph objectId, SMGR uid, Slack user id."
    )
    native_attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw source attributes retained verbatim, including ones the canonical "
        "model does not map. Nothing is silently discarded; the assessment engine "
        "reports on the unmapped remainder.",
    )
    api_surface: str | None = Field(
        default=None,
        description="Which API produced this, e.g. 'AXL:getPhone', 'Graph:/users', "
        "'SAT:display station'. Needed to reproduce an extraction.",
    )
    raw_sql_used: bool = Field(
        default=False,
        description="True if this object was read via an untyped escape hatch such as AXL "
        "executeSQLQuery. Surfaced in the audit log because raw SQL bypasses "
        "the vendor's own schema guarantees.",
    )
    extracted_at: datetime = Field(default_factory=utcnow)


class TargetRef(BaseModel):
    """Where this entity landed. Populated by ``Apply`` only."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    platform: Platform
    instance_id: str
    native_type: str
    native_key: str
    applied_at: datetime = Field(default_factory=utcnow)
    dry_run: bool = Field(
        default=True,
        description="True when the reference was produced by a dry run and therefore "
        "describes an object that does not exist on the target.",
    )
    correlation_id: str | None = Field(
        default=None, description="Ties this write back to a run and an audit record."
    )


# --------------------------------------------------------------------------- #
# Fidelity taxonomy
# --------------------------------------------------------------------------- #


class FidelityLevel(StrEnum):
    """How faithfully an entity survives the transform. See docs/fidelity-taxonomy.md."""

    LOSSLESS = "LOSSLESS"
    """Every semantically significant source attribute has an equivalent on the target
    and behaviour is preserved. Must be justified; never a default."""

    DEGRADED = "DEGRADED"
    """The entity migrates but behaviour or configuration changes in a way a user or
    administrator could notice. The degradation must be described."""

    UNMAPPABLE = "UNMAPPABLE"
    """No target equivalent exists. The entity cannot be applied and becomes manual
    work; ``manual_effort_minutes`` estimates that work."""


class DegradedAttribute(BaseModel):
    """One specific way an entity loses fidelity. Vague entries are worse than none."""

    model_config = ConfigDict(extra="forbid")

    attribute: str = Field(
        description="Canonical attribute path, e.g. 'shared_appearance.privacy'."
    )
    reason: str = Field(description="Why the target cannot represent it faithfully.")
    source_value: str | None = None
    target_behaviour: str = Field(
        description="What will actually happen on the target instead. This is the sentence "
        "the customer reads in the fidelity report, so write it for them."
    )


UNASSESSED_RATIONALE = "Not yet assessed: no fidelity rule has evaluated this entity."


class FidelityAssessment(BaseModel):
    """A fidelity claim, with the evidence required to make it.

    The guardrail 'never mark an entity LOSSLESS by default' is enforced here
    rather than left to connector authors' discipline.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    level: FidelityLevel = FidelityLevel.DEGRADED
    rationale: str = Field(default=UNASSESSED_RATIONALE, min_length=1)
    unmapped_source_attributes: list[str] = Field(
        default_factory=list,
        description="Keys present in source_ref.native_attributes with no canonical home.",
    )
    degraded_attributes: list[DegradedAttribute] = Field(default_factory=list)
    manual_effort_minutes: int | None = Field(
        default=None,
        ge=0,
        description="Estimated manual remediation effort. Required for UNMAPPABLE so the "
        "assessment report can total up the human cost of a migration.",
    )
    assessed_by: str | None = Field(
        default=None, description="Connector id or mapping-rule id that made this claim."
    )
    assessed_at: datetime | None = None

    @property
    def is_assessed(self) -> bool:
        return self.rationale != UNASSESSED_RATIONALE

    @model_validator(mode="after")
    def _lossless_must_be_earned(self) -> Self:
        if self.level is FidelityLevel.LOSSLESS:
            if self.rationale == UNASSESSED_RATIONALE:
                raise ValueError(
                    "LOSSLESS requires an explicit rationale; an unassessed entity is DEGRADED."
                )
            if self.unmapped_source_attributes or self.degraded_attributes:
                raise ValueError(
                    "LOSSLESS contradicts recorded unmapped/degraded attributes: "
                    f"unmapped={self.unmapped_source_attributes}, "
                    f"degraded={[d.attribute for d in self.degraded_attributes]}"
                )
        if (
            self.level is FidelityLevel.DEGRADED
            and self.is_assessed
            and not self.degraded_attributes
        ):
            raise ValueError(
                "An assessed DEGRADED entity must describe at least one degraded "
                "attribute; otherwise it is either LOSSLESS or the assessment is empty."
            )
        if self.level is FidelityLevel.UNMAPPABLE and self.manual_effort_minutes is None:
            raise ValueError(
                "UNMAPPABLE requires manual_effort_minutes so the assessment report can "
                "quantify the manual work it creates."
            )
        return self

    @classmethod
    def unassessed(cls) -> FidelityAssessment:
        """The only permitted default. Pessimistic by construction."""
        return cls(level=FidelityLevel.DEGRADED, rationale=UNASSESSED_RATIONALE)

    @classmethod
    def lossless(cls, rationale: str, *, assessed_by: str) -> FidelityAssessment:
        return cls(
            level=FidelityLevel.LOSSLESS,
            rationale=rationale,
            assessed_by=assessed_by,
            assessed_at=utcnow(),
        )

    @classmethod
    def degraded(
        cls,
        rationale: str,
        attributes: list[DegradedAttribute],
        *,
        assessed_by: str,
        manual_effort_minutes: int | None = None,
    ) -> FidelityAssessment:
        return cls(
            level=FidelityLevel.DEGRADED,
            rationale=rationale,
            degraded_attributes=attributes,
            manual_effort_minutes=manual_effort_minutes,
            assessed_by=assessed_by,
            assessed_at=utcnow(),
        )

    @classmethod
    def unmappable(
        cls, rationale: str, *, assessed_by: str, manual_effort_minutes: int
    ) -> FidelityAssessment:
        return cls(
            level=FidelityLevel.UNMAPPABLE,
            rationale=rationale,
            manual_effort_minutes=manual_effort_minutes,
            assessed_by=assessed_by,
            assessed_at=utcnow(),
        )


# --------------------------------------------------------------------------- #
# Transform log
# --------------------------------------------------------------------------- #


class TransformOperation(StrEnum):
    EXTRACT = "EXTRACT"
    NORMALISE = "NORMALISE"
    MAP = "MAP"
    OVERRIDE = "OVERRIDE"
    """A human overrode an automatic mapping in the mapping workbench."""
    APPLY = "APPLY"
    ROLLBACK = "ROLLBACK"
    QUARANTINE = "QUARANTINE"


class TransformLogEntry(BaseModel):
    """One recorded step in an entity's journey. Append-only; never rewritten."""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(default_factory=utcnow)
    operation: TransformOperation
    actor: str = Field(description="Connector id, rule id, or user principal that acted.")
    summary: str
    attribute: str | None = None
    before: Any = None
    after: Any = None
    rule_ref: str | None = Field(
        default=None, description="Mapping-profile rule that caused this, if any."
    )
    fidelity_impact: FidelityLevel | None = None
    correlation_id: str | None = None


# --------------------------------------------------------------------------- #
# The entity base
# --------------------------------------------------------------------------- #

_CHECKSUM_EXCLUDE: dict[str, Any] = {
    # Populated downstream of extraction; must not perturb the content digest.
    "target_ref": True,
    "transform_log": True,
    "checksum": True,
    # Fidelity is a derived judgement about content, not content itself. Re-running
    # the assessment engine with better rules must not invalidate every checksum.
    "fidelity": True,
    "tags": True,
    "source_ref": {"extracted_at"},
}

_SEMANTIC_EXCLUDE: dict[str, Any] = {
    **{k: v for k, v in _CHECKSUM_EXCLUDE.items() if k != "source_ref"},
    # Platform-neutral by definition: strips both provenance and identity so the
    # same logical object extracted from CUCM and from Teams compares equal.
    "source_ref": True,
    "canonical_id": True,
}


class CanonicalEntity(BaseModel):
    """Base class for every entity in the canonical UC model."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        ser_json_timedelta="iso8601",
    )

    #: Set by each concrete subclass to a ``Literal`` so the union is discriminated.
    kind: str

    #: Domain this entity belongs to, for reporting and schema grouping.
    domain: ClassVar[str] = "unspecified"

    canonical_id: CanonicalId
    display_name: str | None = Field(
        default=None, description="Human label for grids and reports. Not an identifier."
    )
    source_ref: SourceRef | None = None
    target_ref: TargetRef | None = None
    fidelity: FidelityAssessment = Field(default_factory=FidelityAssessment.unassessed)
    transform_log: list[TransformLogEntry] = Field(default_factory=list)
    checksum: str | None = Field(
        default=None,
        description="Digest of extraction-scoped content. Set by seal(); verified on load.",
    )
    tags: dict[str, str] = Field(
        default_factory=dict, description="Operator-assigned labels: wave, owner, exception id."
    )

    # -- identity ---------------------------------------------------------- #

    @staticmethod
    def mint_canonical_id(
        platform: Platform | str, kind: str, native_key: str, *, instance_id: str = ""
    ) -> str:
        """Deterministic UUIDv5 identity.

        Same estate + same object => same id, forever. This is what lets a
        discovery re-run diff cleanly against the previous snapshot and what lets
        an interrupted apply resume without creating duplicates.
        """
        platform_value = platform.value if isinstance(platform, Platform) else platform
        name = f"{platform_value}|{instance_id}|{kind}|{native_key}"
        return str(uuid.uuid5(CANONICAL_ID_NAMESPACE, name))

    @classmethod
    def id_for(cls, source: SourceRef, kind: str, native_key: str | None = None) -> str:
        return cls.mint_canonical_id(
            source.platform,
            kind,
            native_key if native_key is not None else source.native_key,
            instance_id=source.instance_id,
        )

    # -- digests ----------------------------------------------------------- #

    def _payload(self, exclude: dict[str, Any]) -> Any:
        return self.model_dump(mode="json", exclude=exclude, exclude_none=True)

    def compute_checksum(self) -> str:
        """Digest over content + provenance, excluding volatile/derived fields."""
        return digest_of(self._payload(_CHECKSUM_EXCLUDE))

    def semantic_digest(self) -> str:
        """Content digest with provenance and identity stripped.

        Used for the idempotency check within one estate: if what is on the
        target already digests to what we intend to write, the write is a no-op.

        **Not comparable across estates.** Reference fields still hold
        ``canonical_id`` values, and those are scoped to a platform and instance,
        so the same logical user extracted from CUCM and from Teams will not
        match here. Cross-estate reconciliation resolves references to natural
        keys first - see :func:`ucm_bridge.pipeline.reconcile.neutral_digest`.
        """
        return digest_of(self._payload(_SEMANTIC_EXCLUDE))

    def reference_fields(self) -> dict[str, list[str]]:
        """Canonical reference fields (``*_ref`` / ``*_refs``) and their values."""
        found: dict[str, list[str]] = {}
        for name in type(self).model_fields:
            if not (name.endswith("_ref") or name.endswith("_refs")):
                continue
            value = getattr(self, name, None)
            if isinstance(value, str):
                found[name] = [value]
            elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                found[name] = list(value)
        return found

    def content_view(self) -> dict[str, Any]:
        """Non-reference content fields, with envelope and derived fields removed."""
        payload = self._payload(_SEMANTIC_EXCLUDE)
        if not isinstance(payload, dict):  # pragma: no cover - defensive
            return {}
        return {
            k: v
            for k, v in payload.items()
            if k != "kind" and not (k.endswith("_ref") or k.endswith("_refs"))
        }

    def seal(self) -> Self:
        """Populate ``checksum``. Called by the connector base after extraction."""
        self.checksum = self.compute_checksum()
        return self

    def verify_checksum(self) -> bool:
        """False if the entity was mutated after sealing, or was never sealed."""
        return self.checksum is not None and self.checksum == self.compute_checksum()

    # -- provenance helpers ------------------------------------------------ #

    def log(
        self,
        operation: TransformOperation,
        actor: str,
        summary: str,
        **fields: Any,
    ) -> Self:
        self.transform_log.append(
            TransformLogEntry(operation=operation, actor=actor, summary=summary, **fields)
        )
        return self

    def unmapped_source_attributes(self) -> list[str]:
        """Source keys retained verbatim but never claimed by a canonical field.

        Deliberately conservative: a connector declares what it mapped via
        ``fidelity.unmapped_source_attributes``; this is the raw floor under that.
        """
        if self.source_ref is None:
            return []
        return sorted(self.source_ref.native_attributes)

    @field_validator("canonical_id")
    @classmethod
    def _id_is_not_placeholder(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("canonical_id must not be blank")
        return value

    def __str__(self) -> str:  # pragma: no cover - debugging affordance
        return f"{self.kind}({self.display_name or self.canonical_id})"
