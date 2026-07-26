"""Mapping and transformation (§4.3): number plan, rule DSL, auto-mapping, profiles."""

from ucm_bridge.mapping.automap import (
    REVIEW_THRESHOLD,
    WEAK_THRESHOLD,
    MappingCandidate,
    MappingDecision,
    mapping_summary,
    suggest_all,
    suggest_mapping,
)
from ucm_bridge.mapping.normalisation import (
    Collision,
    NormalisationOutcome,
    NormalisationResult,
    NumberPlan,
    Overlap,
    SiteNumberRule,
)
from ucm_bridge.mapping.profile import (
    MappingOverride,
    MappingProfile,
    TransformIssue,
    TransformResult,
    apply_profile,
)
from ucm_bridge.mapping.rules import (
    MappingRule,
    RuleMatch,
    RuleSet,
    UnknownPlaceholder,
    load_ruleset,
    render_template,
)

__all__ = [
    "REVIEW_THRESHOLD",
    "WEAK_THRESHOLD",
    "Collision",
    "MappingCandidate",
    "MappingDecision",
    "MappingOverride",
    "MappingProfile",
    "MappingRule",
    "NormalisationOutcome",
    "NormalisationResult",
    "NumberPlan",
    "Overlap",
    "RuleMatch",
    "RuleSet",
    "SiteNumberRule",
    "TransformIssue",
    "TransformResult",
    "UnknownPlaceholder",
    "apply_profile",
    "load_ruleset",
    "mapping_summary",
    "render_template",
    "suggest_all",
    "suggest_mapping",
]
