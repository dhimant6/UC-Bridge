"""Cisco AXL transport and method catalogue.

Every AXL fact here was verified against the Cisco AXL Developer Guide
(https://developer.cisco.com/docs/axl/axl-developer-guide/) on 2026-07-26:

* Endpoint is ``https://{host}:8443/axl/`` — **the trailing slash is required**,
  and configuration writes go to the **publisher** node only.
* SOAPAction header format is ``"CUCM:DB ver=<X.X> <OperationName>"``.
* Namespace URI is ``http://www.cisco.com/AXL/API/<X.X>``, and the version in the
  namespace must match the version in the SOAPAction.
* Authentication is HTTP Basic. AXL supports a read-only role, which is what a
  discovery-scoped service account should hold.
* Each CUCM major/minor release ships a new schema version; three major releases
  are supported concurrently. Maintenance releases do not add schemas — which is
  why ``schema_version`` is negotiated at connect time rather than hardcoded.
* Oversized reads are refused with "Query request too large", and the service
  returns ``503 Service Unavailable`` under load, to be retried after several
  seconds.

Nothing about the *field-level* schema of each object is asserted here. That
varies per release and is read from the live WSDL by the zeep transport.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.connectors.errors import (
    AuthenticationError,
    AuthorizationError,
    ConnectorError,
    RateLimited,
    TransientPlatformError,
)
from ucm_bridge.vendor.cassette import Cassette, raise_recorded

AXL_ENDPOINT_TEMPLATE = "https://{host}:8443/axl/"
AXL_NAMESPACE_TEMPLATE = "http://www.cisco.com/AXL/API/{version}"
AXL_SOAPACTION_TEMPLATE = 'CUCM:DB ver={version} {operation}'

#: Schema versions this connector has been written against. AXL negotiates at
#: runtime; this list only bounds what we will attempt.
KNOWN_SCHEMA_VERSIONS: tuple[str, ...] = ("12.5", "14.0", "15.0")

DEFAULT_SCHEMA_VERSION = "14.0"


def axl_endpoint(host: str) -> str:
    return AXL_ENDPOINT_TEMPLATE.format(host=host)


def axl_soap_action(operation: str, version: str) -> str:
    return AXL_SOAPACTION_TEMPLATE.format(version=version, operation=operation)


def axl_namespace(version: str) -> str:
    return AXL_NAMESPACE_TEMPLATE.format(version=version)


class AxlOperation(BaseModel):
    """One AXL method this connector is allowed to call.

    An explicit allow-list rather than dynamic dispatch: it means the full set of
    calls a connector can make against a production publisher is reviewable in
    one place, and a typo becomes an error at plan time rather than a SOAP fault
    against a live cluster.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    writes: bool = False
    is_raw_sql: bool = Field(
        default=False,
        description="Thin AXL. Bypasses the typed API and its backward-compatibility "
        "guarantees, so every use is flagged in the audit log.",
    )
    description: str = ""


def _read(name: str, description: str) -> AxlOperation:
    return AxlOperation(name=name, writes=False, description=description)


def _write(name: str, description: str) -> AxlOperation:
    return AxlOperation(name=name, writes=True, description=description)


#: Read operations used by discovery. Names follow the documented
#: get/list/add/update/remove convention.
AXL_READ_OPERATIONS: dict[str, AxlOperation] = {
    op.name: op
    for op in (
        _read("getCCMVersion", "Cluster version, used to negotiate the schema version"),
        _read("listUser", "End users"),
        _read("listPhone", "Registered and configured devices"),
        _read("getPhone", "Full device detail including lines"),
        _read("listLine", "Directory numbers"),
        _read("listRoutePartition", "Partitions"),
        _read("listCss", "Calling search spaces"),
        _read("listRoutePattern", "Route patterns"),
        _read("listTransPattern", "Translation patterns"),
        _read("listDevicePool", "Device pools"),
        _read("listHuntPilot", "Hunt pilots"),
        _read("listLineGroup", "Line groups"),
        _read("listHuntList", "Hunt lists"),
        _read("listCallPickupGroup", "Call pickup groups"),
        _read("listSipTrunk", "SIP trunks"),
        _read("listGateway", "Gateways"),
        _read("listRouteList", "Route lists"),
        _read("listRouteGroup", "Route groups"),
        _read("listDeviceProfile", "Extension mobility device profiles"),
        _read("listCallPark", "Call park ranges"),
        _read("listTimeSchedule", "Time schedules"),
    )
}

#: Write operations. Deliberately small: this connector writes only what the
#: reverse-direction (Teams -> CUCM) transform needs.
AXL_WRITE_OPERATIONS: dict[str, AxlOperation] = {
    op.name: op
    for op in (
        _write("addPhone", "Create a device"),
        _write("updatePhone", "Update a device"),
        _write("removePhone", "Delete a device"),
        _write("addLine", "Create a directory number"),
        _write("updateLine", "Update a directory number"),
        _write("removeLine", "Delete a directory number"),
        _write("addUser", "Create an end user"),
        _write("updateUser", "Update an end user"),
        _write("addRoutePartition", "Create a partition"),
        _write("addCss", "Create a calling search space"),
        _write("addRoutePattern", "Create a route pattern"),
        _write("updateRoutePattern", "Update a route pattern"),
        _write("removeRoutePattern", "Delete a route pattern"),
        _write("addSipTrunk", "Create a SIP trunk"),
        _write("addRouteList", "Create a route list"),
        _write("addRouteGroup", "Create a route group"),
    )
}

EXECUTE_SQL_QUERY = AxlOperation(
    name="executeSQLQuery",
    writes=False,
    is_raw_sql=True,
    description="Thin AXL read. Used only where the typed API cannot answer the question.",
)

ALL_AXL_OPERATIONS: dict[str, AxlOperation] = {
    **AXL_READ_OPERATIONS,
    **AXL_WRITE_OPERATIONS,
    EXECUTE_SQL_QUERY.name: EXECUTE_SQL_QUERY,
}


class UnknownAxlOperation(ConnectorError):
    """A call was attempted that is not on the reviewed allow-list."""


class AxlTransport(Protocol):
    """Anything that can issue an AXL call and return the parsed response body."""

    async def call(self, operation: str, request: dict[str, Any]) -> Any: ...

    @property
    def schema_version(self) -> str: ...


class BaseAxlTransport(ABC):
    """Shared allow-list enforcement and raw-SQL accounting."""

    def __init__(self, *, schema_version: str = DEFAULT_SCHEMA_VERSION) -> None:
        self._schema_version = schema_version
        self.raw_sql_calls: list[str] = []

    @property
    def schema_version(self) -> str:
        return self._schema_version

    async def call(self, operation: str, request: dict[str, Any]) -> Any:
        declared = ALL_AXL_OPERATIONS.get(operation)
        if declared is None:
            raise UnknownAxlOperation(
                f"AXL operation {operation!r} is not on the reviewed allow-list. "
                f"Add it to ucm_bridge.vendor.axl with a description, or use one of: "
                f"{sorted(ALL_AXL_OPERATIONS)}"
            )
        if declared.is_raw_sql:
            self.raw_sql_calls.append(str(request.get("sql", ""))[:500])
        return await self._dispatch(declared, request)

    @abstractmethod
    async def _dispatch(self, operation: AxlOperation, request: dict[str, Any]) -> Any: ...


class CassetteAxlTransport(BaseAxlTransport):
    """Replays a recorded conversation. The only transport used in tests."""

    def __init__(self, cassette: Cassette, *, schema_version: str = DEFAULT_SCHEMA_VERSION):
        super().__init__(schema_version=schema_version)
        self.cassette = cassette

    async def _dispatch(self, operation: AxlOperation, request: dict[str, Any]) -> Any:
        interaction = self.cassette.lookup(operation.name, request)
        raise_recorded(interaction)
        return interaction.response


class ZeepAxlTransport(BaseAxlTransport):
    """Live AXL over SOAP via ``zeep``, reading the per-release WSDL.

    Not exercised by the test suite — it cannot be, without a cluster. It is
    written to fail loudly rather than approximately: the WSDL must be present
    for the negotiated schema version, and no field names are assumed.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        wsdl_path: str,
        schema_version: str = DEFAULT_SCHEMA_VERSION,
        verify_tls: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(schema_version=schema_version)
        if schema_version not in KNOWN_SCHEMA_VERSIONS:
            raise ConnectorError(
                f"AXL schema version {schema_version!r} has not been reviewed for this "
                f"connector. Known: {list(KNOWN_SCHEMA_VERSIONS)}. Negotiate with "
                "getCCMVersion and add the version deliberately rather than guessing."
            )
        self.host = host
        self.endpoint = axl_endpoint(host)
        self.wsdl_path = wsdl_path
        self.verify_tls = verify_tls
        self.timeout_seconds = timeout_seconds
        self._username = username
        self._password = password
        self._client: Any = None

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import requests
            from zeep import Client, Settings
            from zeep.transports import Transport
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConnectorError(
                "The live AXL transport needs the 'cucm' extra: pip install "
                "'ucm-bridge[cucm]' (zeep + requests)."
            ) from exc

        session = requests.Session()
        session.auth = (self._username, self._password)
        session.verify = self.verify_tls
        transport = Transport(session=session, timeout=self.timeout_seconds)
        client = Client(
            wsdl=self.wsdl_path, settings=Settings(strict=False, xml_huge_tree=True),
            transport=transport,
        )
        self._client = client.create_service(
            "{http://www.cisco.com/AXLAPIService/}AXLAPIBinding", self.endpoint
        )
        return self._client

    async def _dispatch(self, operation: AxlOperation, request: dict[str, Any]) -> Any:
        service = self._connect()
        method = getattr(service, operation.name, None)
        if method is None:
            raise UnknownAxlOperation(
                f"The WSDL for AXL schema {self.schema_version} does not expose "
                f"{operation.name!r}. This usually means the cluster runs a different "
                "release than the WSDL was taken from."
            )
        try:
            return method(**request)
        except Exception as exc:
            raise translate_axl_fault(exc) from exc


def translate_axl_fault(exc: Exception) -> ConnectorError:
    """Map a SOAP fault or HTTP error onto the connector error taxonomy.

    Retryability matters here: AXL's documented 503 is transient and should be
    retried after several seconds, while an authentication failure must never be
    retried in a loop because that locks the service account out.
    """
    text = str(exc)
    status = getattr(getattr(exc, "response", None), "status_code", None)

    if status == 401 or "HTTP Status 401" in text:
        return AuthenticationError(f"AXL rejected the credentials: {text}", status_code=401)
    if status == 403 or "HTTP Status 403" in text:
        return AuthorizationError(
            f"AXL authenticated but denied the operation; check the AXL API Access role "
            f"on the service account: {text}",
            status_code=403,
        )
    if status == 503 or "503" in text:
        # Documented: retry after several seconds.
        return RateLimited(f"AXL service unavailable (throttled): {text}", retry_after_seconds=5.0)
    if "Query request too large" in text:
        return ConnectorError(
            "AXL refused the read as too large. Reduce the page size or narrow "
            f"returnedTags: {text}"
        )
    if status is not None and 500 <= status < 600:
        return TransientPlatformError(f"AXL server error: {text}", status_code=status)
    return ConnectorError(f"AXL call failed: {text}", status_code=status)
