"""Auto-mapping with confidence scores.

Suggests target objects for source objects — CUCM calling search space to Teams
voice routing policy, Avaya COR to a Genesys role — and says how sure it is and
why.

The confidence number is only useful if it is honest. Scores here are built from
named, additive signals that are shown to the reviewer, so a 0.62 can be
interrogated rather than trusted. Anything below ``REVIEW_THRESHOLD`` is
presented as a suggestion requiring human confirmation, and the mapping
workbench never auto-applies below it.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import CanonicalEntity

#: Below this, a suggestion is never applied without a human saying yes.
REVIEW_THRESHOLD = 0.75

#: Below this, the suggestion is not even offered as a default selection.
WEAK_THRESHOLD = 0.40

_SEPARATORS = re.compile(r"[_\-.]+")
#: Platform noise words. Stripped only after separators become spaces, otherwise
#: "CSS_EMEA" has no word boundary after "CSS" and the noise word survives.
_NOISE_WORDS = re.compile(r"(?i)\b(css|cor|cos|policy|profile|pt|rp|grp|group|tag)\b")


class MappingDecision(StrEnum):
    AUTO = "AUTO"
    """Confident enough to apply without review."""
    SUGGEST = "SUGGEST"
    """Offered as a default, needs confirmation."""
    WEAK = "WEAK"
    """Offered as an option only."""
    NONE = "NONE"
    """No plausible target found; this becomes manual work."""


class ConfidenceSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    weight: float
    detail: str


class MappingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_label: str
    target_id: str | None = None
    target_label: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    decision: MappingDecision
    signals: list[ConfidenceSignal] = Field(default_factory=list)
    rationale: str = ""

    @property
    def needs_review(self) -> bool:
        return self.decision is not MappingDecision.AUTO


def normalise_name(value: str) -> str:
    """Strip the platform noise words that make two equivalent names look different."""
    spaced = _SEPARATORS.sub(" ", value)
    return " ".join(_NOISE_WORDS.sub(" ", spaced).lower().split())


def name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalise_name(left), normalise_name(right)).ratio()


def _decision(confidence: float) -> MappingDecision:
    if confidence >= REVIEW_THRESHOLD:
        return MappingDecision.AUTO
    if confidence >= WEAK_THRESHOLD:
        return MappingDecision.SUGGEST
    return MappingDecision.WEAK


def suggest_mapping(
    source: CanonicalEntity,
    targets: list[CanonicalEntity],
    *,
    label_of: object = None,
) -> MappingCandidate:
    """Suggest the best target for one source entity, with an auditable score."""
    source_label = _label(source)

    if not targets:
        return MappingCandidate(
            source_id=source.canonical_id,
            source_label=source_label,
            confidence=0.0,
            decision=MappingDecision.NONE,
            rationale="No candidate targets of a compatible kind exist.",
        )

    scored: list[tuple[float, CanonicalEntity, list[ConfidenceSignal]]] = []
    for target in targets:
        signals = _score_signals(source, target)
        total = min(1.0, sum(s.weight for s in signals))
        scored.append((total, target, signals))

    scored.sort(key=lambda item: (-item[0], _label(item[1])))
    best_score, best_target, best_signals = scored[0]

    # An ambiguous best is a worse answer than a low-confidence one: penalise
    # near-ties so the reviewer is told the choice was close.
    if len(scored) > 1:
        runner_up = scored[1][0]
        if best_score - runner_up < 0.05 and best_score > 0:
            best_score *= 0.7
            best_signals = [
                *best_signals,
                ConfidenceSignal(
                    name="ambiguous",
                    weight=0.0,
                    detail=(
                        f"{_label(scored[1][1])!r} scored almost as well; the choice between "
                        "them is not clear-cut."
                    ),
                ),
            ]

    # Structural signals (same site, comparable member count) only *boost* a name
    # match; they must never carry a suggestion on their own. Two unrelated
    # objects that happen to have two members each tell you nothing, and offering
    # that as a mapping is worse than offering nothing.
    has_name_signal = any(s.name == "name_similarity" for s in best_signals)
    if not has_name_signal or best_score < 0.15:
        return MappingCandidate(
            source_id=source.canonical_id,
            source_label=source_label,
            confidence=round(best_score if has_name_signal else 0.0, 3),
            decision=MappingDecision.NONE,
            signals=best_signals,
            rationale=(
                "No target's name resembles the source; structural similarity alone is not "
                "evidence of a mapping."
                if not has_name_signal
                else "No target scored high enough to be worth offering."
            ),
        )

    return MappingCandidate(
        source_id=source.canonical_id,
        source_label=source_label,
        target_id=best_target.canonical_id,
        target_label=_label(best_target),
        confidence=round(best_score, 3),
        decision=_decision(best_score),
        signals=best_signals,
        rationale="; ".join(s.detail for s in best_signals) or "Name similarity only.",
    )


def _score_signals(
    source: CanonicalEntity, target: CanonicalEntity
) -> list[ConfidenceSignal]:
    signals: list[ConfidenceSignal] = []

    similarity = name_similarity(_label(source), _label(target))
    if similarity > 0.2:
        signals.append(
            ConfidenceSignal(
                name="name_similarity",
                weight=round(similarity * 0.6, 3),
                detail=f"Names are {similarity:.0%} similar after stripping platform noise.",
            )
        )

    source_site = getattr(source, "site_code", None)
    target_site = getattr(target, "site_code", None)
    if source_site and source_site == target_site:
        signals.append(
            ConfidenceSignal(
                name="same_site", weight=0.2, detail=f"Both belong to site {source_site}."
            )
        )

    source_class = getattr(source, "permission_class", None)
    target_class = getattr(target, "permission_class", None)
    if source_class is not None and source_class == target_class:
        signals.append(
            ConfidenceSignal(
                name="permission_class",
                weight=0.25,
                detail=f"Both are classified {source_class}.",
            )
        )

    # Structural similarity: comparable numbers of ordered members usually means
    # comparable reach.
    source_members = _member_count(source)
    target_members = _member_count(target)
    if source_members and target_members:
        ratio = min(source_members, target_members) / max(source_members, target_members)
        if ratio > 0.5:
            signals.append(
                ConfidenceSignal(
                    name="member_count",
                    weight=round(ratio * 0.15, 3),
                    detail=(
                        f"Comparable size: {source_members} vs {target_members} members."
                    ),
                )
            )

    return signals


def _member_count(entity: CanonicalEntity) -> int:
    for field in (
        "permitted_partition_refs",
        "pstn_usage_refs",
        "member_line_refs",
        "member_refs",
        "agent_refs",
    ):
        value = getattr(entity, field, None)
        if isinstance(value, list):
            return len(value)
    return 0


def _label(entity: CanonicalEntity) -> str:
    return (
        getattr(entity, "name", None)
        or entity.display_name
        or entity.canonical_id
    )


def suggest_all(
    sources: list[CanonicalEntity], targets: list[CanonicalEntity]
) -> list[MappingCandidate]:
    return [suggest_mapping(source, targets) for source in sources]


def mapping_summary(candidates: list[MappingCandidate]) -> dict[str, int]:
    summary = {d.value: 0 for d in MappingDecision}
    for candidate in candidates:
        summary[candidate.decision.value] += 1
    return summary
