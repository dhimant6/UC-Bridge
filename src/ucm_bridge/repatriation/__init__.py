"""Cloud-to-on-prem repatriation (§3.7): ports, licence reclaim, export limits."""

from ucm_bridge.repatriation.direct_routing import (
    DirectRoutingTransformResult,
    PatternTranslation,
    transform_direct_routing_to_on_prem,
    translate_number_pattern,
)
from ucm_bridge.repatriation.porting import (
    ALLOWED_TRANSITIONS,
    REQUIRED_CSR_FIELDS,
    CutoverWindow,
    IllegalPortTransition,
    LoaPacket,
    PortReadiness,
    assess_readiness,
    build_loa_packet,
    numbers_in_flight,
    plan_cutover_window,
    schedule_risk,
    transition,
)
from ucm_bridge.repatriation.reclaim import (
    ExportAudit,
    ExportFinding,
    ExportRisk,
    ReclaimPlan,
    ReclaimStep,
    audit_export_limits,
    build_reclaim_plan,
    reclaim_plan_to_apply_plan,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "REQUIRED_CSR_FIELDS",
    "CutoverWindow",
    "DirectRoutingTransformResult",
    "ExportAudit",
    "ExportFinding",
    "ExportRisk",
    "IllegalPortTransition",
    "LoaPacket",
    "PatternTranslation",
    "PortReadiness",
    "ReclaimPlan",
    "ReclaimStep",
    "assess_readiness",
    "audit_export_limits",
    "build_loa_packet",
    "build_reclaim_plan",
    "numbers_in_flight",
    "plan_cutover_window",
    "reclaim_plan_to_apply_plan",
    "schedule_risk",
    "transform_direct_routing_to_on_prem",
    "transition",
    "translate_number_pattern",
]
