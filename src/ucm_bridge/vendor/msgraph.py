"""Microsoft Graph and Teams PowerShell surface declarations.

Verified 2026-07-26 against Microsoft Learn:

* ``GET /subscribedSkus`` lists the tenant's subscriptions; ``skuId`` from there
  is what an assignment refers to.
* ``POST /users/{id|userPrincipalName}/assignLicense`` takes
  ``{"addLicenses": [{"skuId", "disabledPlans"}], "removeLicenses": [skuId]}``.
  ``removeLicenses`` is **required** and may be an empty collection — omitting it
  is a 400, which is exactly the kind of detail that has to be checked rather
  than remembered.
* ``Set-CsPhoneNumberAssignment`` takes ``-Identity``, ``-TelephoneNumber``
  (alias ``PhoneNumber``), ``-NumberType`` (alias ``PhoneNumberType``, one of
  DirectRouting / CallingPlan / OperatorConnect), and optional ``-LocationId``.
  Assigning a number sets ``EnterpriseVoiceEnabled`` to true automatically.
  Requires Teams PowerShell 3.0.0+.
* ``New-CsAutoAttendant`` and ``New-CsCallQueue`` exist; an auto attendant needs
  an application instance created with ``New-CsOnlineApplicationInstance`` and
  linked with ``New-CsOnlineApplicationInstanceAssociation``.

Cmdlets whose parameter signatures were **not** verified carry no
``verified_source`` and no ``allowed_parameters``. They are still refused for
production writes by the readiness gate.
"""

from __future__ import annotations

from datetime import date

from ucm_bridge.vendor.powershell import Cmdlet
from ucm_bridge.vendor.rest import GRAPH_PAGINATION, HttpMethod, PaginationStyle, RestRequest

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_PAGINATION_STYLE: PaginationStyle = GRAPH_PAGINATION
GRAPH_VERIFIED_ON = date(2026, 7, 26)
TEAMS_PS_VERIFIED_ON = date(2026, 7, 26)

LEARN = "https://learn.microsoft.com"
GRAPH_DOC = f"{LEARN}/en-us/graph/api"
TEAMS_DOC = f"{LEARN}/en-us/powershell/module/microsoftteams"


# --------------------------------------------------------------------------- #
# Graph reads
# --------------------------------------------------------------------------- #


def list_users(select: list[str] | None = None, page_size: int = 999) -> RestRequest:
    fields = select or [
        "id",
        "userPrincipalName",
        "displayName",
        "givenName",
        "surname",
        "mail",
        "department",
        "jobTitle",
        "officeLocation",
        "accountEnabled",
        "usageLocation",
        "preferredLanguage",
        "employeeId",
    ]
    return RestRequest(
        method=HttpMethod.GET,
        path="/users",
        query={"$select": ",".join(fields), "$top": page_size},
    )


def list_subscribed_skus() -> RestRequest:
    return RestRequest(method=HttpMethod.GET, path="/subscribedSkus")


def get_user_licence_details(user_id: str) -> RestRequest:
    return RestRequest(method=HttpMethod.GET, path=f"/users/{user_id}/licenseDetails")


def list_groups(page_size: int = 999) -> RestRequest:
    return RestRequest(
        method=HttpMethod.GET,
        path="/groups",
        query={
            "$select": "id,displayName,description,mail,mailEnabled,securityEnabled,groupTypes",
            "$top": page_size,
        },
    )


def assign_licence(
    user_id: str, *, add_sku_ids: list[str], remove_sku_ids: list[str] | None = None,
    disabled_plans: dict[str, list[str]] | None = None,
) -> RestRequest:
    """Build an assignLicense request.

    ``removeLicenses`` is required even when empty — verified against the v1.0
    reference. Sending only ``addLicenses`` is rejected.
    """
    plans = disabled_plans or {}
    return RestRequest(
        method=HttpMethod.POST,
        path=f"/users/{user_id}/assignLicense",
        body={
            "addLicenses": [
                {"skuId": sku, "disabledPlans": plans.get(sku, [])} for sku in add_sku_ids
            ],
            "removeLicenses": list(remove_sku_ids or []),
        },
    )


#: Least-privilege application permissions for the reads above. Discovery should
#: hold the .Read.All set only; the .ReadWrite.All scopes are apply-time.
GRAPH_READ_SCOPES: tuple[str, ...] = (
    "User.Read.All",
    "Group.Read.All",
    "Organization.Read.All",
)
GRAPH_WRITE_SCOPES: tuple[str, ...] = (
    "User.ReadWrite.All",
    "Organization.Read.All",
)


# --------------------------------------------------------------------------- #
# Teams PowerShell cmdlets
# --------------------------------------------------------------------------- #


def _verified(
    name: str,
    *,
    writes: bool = False,
    required: tuple[str, ...] = (),
    allowed: tuple[str, ...] = (),
    notes: str | None = None,
) -> Cmdlet:
    return Cmdlet(
        name=name,
        module="MicrosoftTeams",
        writes=writes,
        required_parameters=required,
        allowed_parameters=allowed,
        verified_source=(
            f"Microsoft Learn {TEAMS_DOC}/{name.lower()}, checked {TEAMS_PS_VERIFIED_ON}"
        ),
        notes=notes,
    )


def _unverified(name: str, *, writes: bool = False, notes: str) -> Cmdlet:
    """A cmdlet known to exist whose signature has not been checked.

    Declared without parameter constraints and without a verification source, so
    the readiness gate keeps it out of production.
    """
    return Cmdlet(name=name, module="MicrosoftTeams", writes=writes, notes=notes)


TEAMS_CMDLETS: dict[str, Cmdlet] = {
    c.name: c
    for c in (
        # --- reads -----------------------------------------------------
        _verified(
            "Get-CsOnlineUser",
            notes="Voice-relevant user state: EnterpriseVoiceEnabled, LineURI, policy assignments.",
        ),
        _verified(
            "Get-CsPhoneNumberAssignment",
            allowed=(
                "LocationId",
                "PstnAssignmentStatus",
                "NumberType",
                "CapabilitiesContain",
                "TelephoneNumber",
                "AssignedPstnTargetId",
            ),
            notes="Parameter set confirmed from the Set-CsPhoneNumberAssignment examples.",
        ),
        _verified(
            "Get-CsOnlineLisLocation",
            allowed=("LocationId", "City"),
            notes="LocationId/City usage seen in the Set-CsPhoneNumberAssignment examples.",
        ),
        _verified("Get-CsOnlinePstnUsage", notes="Returns the tenant's PSTN usage records."),
        _verified("Get-CsOnlineVoiceRoutingPolicy", allowed=("Identity",)),
        _verified("Get-CsTeamsCallingPolicy", allowed=("Identity",)),
        _unverified("Get-CsOnlineVoiceRoute", notes="Exists; signature not verified."),
        _unverified("Get-CsOnlinePSTNGateway", notes="Exists; signature not verified."),
        _unverified("Get-CsAutoAttendant", notes="Exists; signature not verified."),
        _unverified("Get-CsCallQueue", notes="Exists; signature not verified."),
        _unverified("Get-CsOnlineApplicationInstance", notes="Exists; signature not verified."),
        # --- writes ----------------------------------------------------
        _verified(
            "Set-CsPhoneNumberAssignment",
            writes=True,
            required=("Identity", "TelephoneNumber", "NumberType"),
            allowed=(
                "LocationId",
                "NetworkSiteId",
                "AssignmentCategory",
                "ReverseNumberLookup",
                "Notify",
            ),
            notes=(
                "NumberType is DirectRouting | CallingPlan | OperatorConnect. Assigning a "
                "number sets EnterpriseVoiceEnabled automatically, so no separate call is "
                "needed. Teams PowerShell 3.0.0+."
            ),
        ),
        _verified(
            "Remove-CsPhoneNumberAssignment",
            writes=True,
            required=("Identity", "TelephoneNumber", "NumberType"),
            notes="Used by the reverse direction to release cloud numbers after cutover.",
        ),
        _verified(
            "Grant-CsOnlineVoiceRoutingPolicy",
            writes=True,
            required=("Identity",),
            allowed=("PolicyName",),
        ),
        _verified(
            "New-CsOnlineVoiceRoutingPolicy",
            writes=True,
            required=("Identity",),
            allowed=("OnlinePstnUsages", "Description"),
            notes="OnlinePstnUsages must reference usages that already exist.",
        ),
        _unverified("New-CsAutoAttendant", writes=True,
                    notes="Exists; requires an application instance. Signature not verified."),
        _unverified("New-CsCallQueue", writes=True, notes="Exists; signature not verified."),
        _unverified("New-CsOnlineApplicationInstance", writes=True,
                    notes="Creates the resource account backing an AA or CQ."),
        _unverified("New-CsOnlineApplicationInstanceAssociation", writes=True,
                    notes="Links an application instance to an AA or CQ."),
        _unverified("New-CsOnlineLisLocation", writes=True,
                    notes="Emergency civic address. Signature NOT verified — do not use in "
                          "production until checked against the live cmdlet."),
        _unverified("New-CsOnlinePSTNGateway", writes=True, notes="Signature not verified."),
    )
}


def unverified_cmdlets(names: list[str]) -> list[str]:
    """Which of these cmdlets have no recorded signature verification."""
    return sorted(
        name
        for name in names
        if name in TEAMS_CMDLETS and TEAMS_CMDLETS[name].verified_source is None
    )
