"""Wave planning with dependency integrity (§4.4)."""

from ucm_bridge.waves.planner import (
    CoexistenceRequirement,
    DependencyCluster,
    DependencyKind,
    DependencyViolation,
    GroupingStrategy,
    Wave,
    WavePlan,
    coexistence_requirements,
    find_dependency_clusters,
    merge_clusters,
    move_user,
    plan_waves,
    render_wave_plan_markdown,
    validate_waves,
)

__all__ = [
    "CoexistenceRequirement",
    "DependencyCluster",
    "DependencyKind",
    "DependencyViolation",
    "GroupingStrategy",
    "Wave",
    "WavePlan",
    "coexistence_requirements",
    "find_dependency_clusters",
    "merge_clusters",
    "move_user",
    "plan_waves",
    "render_wave_plan_markdown",
    "validate_waves",
]
