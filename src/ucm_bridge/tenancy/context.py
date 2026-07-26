"""Multi-tenancy and RBAC.

Consultancies and partners run several customers from one control plane, so
tenant isolation is a correctness property, not a feature. The approach here is
to make cross-tenant access *impossible to express* rather than merely
forbidden: every scoped operation takes a :class:`TenantContext`, and the guard
functions raise rather than filter.

Filtering is the wrong default. A query that silently returns nothing when the
tenant is wrong looks like "no data" and gets debugged for an hour; a raise says
what actually happened.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.connectors.errors import GuardrailViolation


class Role(StrEnum):
    """RBAC roles from §6, in increasing order of danger."""

    VIEWER = "VIEWER"
    """Read discovery, assessments, and reports. Cannot plan or write."""
    PLANNER = "PLANNER"
    """Build mapping profiles, plans, and waves. Can dry-run. Cannot approve or write."""
    APPROVER = "APPROVER"
    """Approve a plan for production. Deliberately cannot execute it."""
    OPERATOR = "OPERATOR"
    """Execute an approved plan. Deliberately cannot approve one."""
    ADMIN = "ADMIN"
    """Manage connectors, credentials, and tenants."""


class Permission(StrEnum):
    READ_ESTATE = "READ_ESTATE"
    RUN_DISCOVERY = "RUN_DISCOVERY"
    EDIT_MAPPING = "EDIT_MAPPING"
    BUILD_PLAN = "BUILD_PLAN"
    RUN_DRY_RUN = "RUN_DRY_RUN"
    APPROVE_PLAN = "APPROVE_PLAN"
    EXECUTE_PRODUCTION = "EXECUTE_PRODUCTION"
    ROLLBACK = "ROLLBACK"
    MANAGE_CONNECTORS = "MANAGE_CONNECTORS"
    MANAGE_TENANTS = "MANAGE_TENANTS"
    READ_AUDIT = "READ_AUDIT"


#: Approver and Operator are disjoint on purpose. One person holding both can
#: satisfy the two-person rule alone, which defeats it.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ_ESTATE, Permission.READ_AUDIT}),
    Role.PLANNER: frozenset(
        {
            Permission.READ_ESTATE,
            Permission.READ_AUDIT,
            Permission.RUN_DISCOVERY,
            Permission.EDIT_MAPPING,
            Permission.BUILD_PLAN,
            Permission.RUN_DRY_RUN,
        }
    ),
    Role.APPROVER: frozenset(
        {Permission.READ_ESTATE, Permission.READ_AUDIT, Permission.APPROVE_PLAN}
    ),
    Role.OPERATOR: frozenset(
        {
            Permission.READ_ESTATE,
            Permission.READ_AUDIT,
            Permission.RUN_DRY_RUN,
            Permission.EXECUTE_PRODUCTION,
            Permission.ROLLBACK,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Permission.READ_ESTATE,
            Permission.READ_AUDIT,
            Permission.MANAGE_CONNECTORS,
            Permission.MANAGE_TENANTS,
        }
    ),
}


class CrossTenantAccess(GuardrailViolation):
    """An operation touched data belonging to another tenant."""


class PermissionDenied(GuardrailViolation):
    """The principal's roles do not carry the required permission."""


class TenantContext(BaseModel):
    """Who is acting, for which tenant, with what rights."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    principal: str
    roles: frozenset[Role] = Field(default_factory=frozenset)
    #: Set for partner staff who legitimately operate several customers. Even
    #: then, each operation is scoped to one tenant at a time.
    accessible_tenant_ids: frozenset[str] = Field(default_factory=frozenset)

    @property
    def permissions(self) -> frozenset[Permission]:
        granted: set[Permission] = set()
        for role in self.roles:
            granted |= ROLE_PERMISSIONS[role]
        return frozenset(granted)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise PermissionDenied(
                f"{self.principal} holds {sorted(r.value for r in self.roles)} and cannot "
                f"{permission.value}. Required by one of: "
                f"{sorted(r.value for r, p in ROLE_PERMISSIONS.items() if permission in p)}."
            )

    def may_access(self, tenant_id: str) -> bool:
        return tenant_id == self.tenant_id or tenant_id in self.accessible_tenant_ids

    def require_tenant(self, tenant_id: str) -> None:
        if not self.may_access(tenant_id):
            raise CrossTenantAccess(
                f"{self.principal} is scoped to tenant {self.tenant_id!r} and cannot access "
                f"{tenant_id!r}."
            )

    def for_tenant(self, tenant_id: str) -> TenantContext:
        """Switch a partner principal to another accessible tenant."""
        self.require_tenant(tenant_id)
        return self.model_copy(update={"tenant_id": tenant_id})


_T = TypeVar("_T")


def scoped(  # noqa: UP047 - classic TypeVar; see ADR-0002 on 3.12 syntax
    context: TenantContext, items: list[_T], *, tenant_of: object
) -> list[_T]:
    """Return ``items`` after asserting every one belongs to the context's tenant.

    Raises on the first foreign item rather than filtering it out. If a query
    reached another tenant's data, the bug is upstream and hiding it makes the
    bug harder to find, not the system safer.
    """
    for item in items:
        owner = tenant_of(item)  # type: ignore[operator]
        if owner != context.tenant_id:
            raise CrossTenantAccess(
                f"Result set for tenant {context.tenant_id!r} contained an object owned by "
                f"{owner!r}. This is an isolation failure in the query, not a filtering "
                "opportunity."
            )
    return items


def two_person_rule_satisfied(
    *, requester: str, approvers: list[str]
) -> bool:
    """Two distinct approvers, neither of whom is the requester."""
    distinct = {a for a in approvers if a != requester}
    return len(distinct) >= 2
