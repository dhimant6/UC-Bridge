"""Multi-tenancy and RBAC (§6)."""

from ucm_bridge.tenancy.context import (
    ROLE_PERMISSIONS,
    CrossTenantAccess,
    Permission,
    PermissionDenied,
    Role,
    TenantContext,
    scoped,
    two_person_rule_satisfied,
)

__all__ = [
    "ROLE_PERMISSIONS",
    "CrossTenantAccess",
    "Permission",
    "PermissionDenied",
    "Role",
    "TenantContext",
    "scoped",
    "two_person_rule_satisfied",
]
