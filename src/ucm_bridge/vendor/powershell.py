"""PowerShell bridge contract for Teams and Skype for Business Server.

The brief's rule is that the main application does not shell out ad hoc; it
talks to a containerised PowerShell 7 sidecar over an internal contract. This
module is that contract, plus a cassette implementation for tests.

Why a structured contract rather than command strings: parameters are passed as
typed values and quoted by the bridge, so a display name containing a quote
cannot become an injected command. ``PowerShellCommand`` therefore carries a
cmdlet name from an allow-list and a parameter dict — never a rendered string.
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


class PowerShellModule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    minimum_version: str | None = None
    notes: str | None = None


#: Verified 2026-07-26 against Microsoft Learn: Set-CsPhoneNumberAssignment is
#: available in Teams PowerShell 3.0.0+.
TEAMS_MODULE = PowerShellModule(
    name="MicrosoftTeams",
    minimum_version="3.0.0",
    notes="Set-CsPhoneNumberAssignment requires 3.0.0 or later.",
)
SFB_MODULE = PowerShellModule(
    name="SkypeForBusiness",
    notes="Reached via remote PowerShell to the on-prem front-end pool.",
)


class Cmdlet(BaseModel):
    """One cmdlet this platform is allowed to invoke."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    module: str
    writes: bool = False
    required_parameters: tuple[str, ...] = ()
    allowed_parameters: tuple[str, ...] = ()
    verified_source: str | None = Field(
        default=None, description="Where the signature was checked, with a date."
    )
    notes: str | None = None

    def validate_call(self, parameters: dict[str, Any]) -> None:
        missing = [p for p in self.required_parameters if p not in parameters]
        if missing:
            raise ConnectorError(
                f"{self.name} is missing required parameter(s) {missing}. "
                f"Required: {list(self.required_parameters)}"
            )
        if self.allowed_parameters:
            permitted = set(self.allowed_parameters) | set(self.required_parameters)
            unexpected = sorted(set(parameters) - permitted)
            if unexpected:
                raise ConnectorError(
                    f"{self.name} was called with parameter(s) {unexpected}, which are not on "
                    f"the reviewed signature. Permitted: {sorted(permitted)}"
                )


class PowerShellCommand(BaseModel):
    """A structured invocation. Never a rendered command string."""

    model_config = ConfigDict(extra="forbid")

    cmdlet: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    module: str | None = None

    def as_request(self) -> dict[str, Any]:
        return {"cmdlet": self.cmdlet, "parameters": self.parameters}

    def preview(self) -> str:
        """Human-readable rendering for dry-run output and runbooks.

        Presentation only — the bridge never executes this string.
        """
        parts = [self.cmdlet]
        for key, value in sorted(self.parameters.items()):
            if isinstance(value, bool):
                parts.append(f"-{key} ${str(value).lower()}")
            elif value is None:
                parts.append(f"-{key} $null")
            elif isinstance(value, (int, float)):
                parts.append(f"-{key} {value}")
            else:
                parts.append(f'-{key} "{value}"')
        return " ".join(parts)


class PowerShellBridge(Protocol):
    async def invoke(self, command: PowerShellCommand) -> Any: ...


class BasePowerShellBridge(ABC):
    """Enforces the cmdlet allow-list before anything reaches the sidecar."""

    def __init__(self, catalogue: dict[str, Cmdlet]) -> None:
        self.catalogue = catalogue

    async def invoke(self, command: PowerShellCommand) -> Any:
        declared = self.catalogue.get(command.cmdlet)
        if declared is None:
            raise ConnectorError(
                f"Cmdlet {command.cmdlet!r} is not on the reviewed allow-list. "
                f"Known: {sorted(self.catalogue)}"
            )
        declared.validate_call(command.parameters)
        return await self._dispatch(declared, command)

    @abstractmethod
    async def _dispatch(self, cmdlet: Cmdlet, command: PowerShellCommand) -> Any: ...


class CassettePowerShellBridge(BasePowerShellBridge):
    def __init__(self, catalogue: dict[str, Cmdlet], cassette: Cassette) -> None:
        super().__init__(catalogue)
        self.cassette = cassette

    async def _dispatch(self, cmdlet: Cmdlet, command: PowerShellCommand) -> Any:
        interaction = self.cassette.lookup(command.cmdlet, command.parameters)
        raise_recorded(interaction)
        return interaction.response


class SidecarPowerShellBridge(BasePowerShellBridge):
    """Talks to the containerised PowerShell 7 sidecar over HTTP.

    Unimplemented on purpose. The sidecar image, its authentication, and its
    wire format are deployment decisions that do not exist yet; inventing an
    endpoint here would produce a client for a server nobody has built.
    """

    def __init__(self, catalogue: dict[str, Cmdlet], *, base_url: str) -> None:
        super().__init__(catalogue)
        self.base_url = base_url

    async def _dispatch(self, cmdlet: Cmdlet, command: PowerShellCommand) -> Any:
        raise NotImplementedError(
            "SidecarPowerShellBridge is not implemented. Build the PowerShell 7 sidecar "
            "and define its request/response contract first; this client is the second "
            "half of that decision, not the first."
        )


def translate_powershell_error(exc: Exception, *, status_code: int | None = None) -> ConnectorError:
    text = str(exc)
    lowered = text.lower()
    if "unauthorized" in lowered or status_code == 401:
        return AuthenticationError(f"PowerShell bridge rejected credentials: {text}")
    if "forbidden" in lowered or "access denied" in lowered or status_code == 403:
        return AuthorizationError(f"Insufficient role for this cmdlet: {text}")
    if "throttl" in lowered or status_code == 429:
        return RateLimited(f"Throttled by the service: {text}")
    if status_code is not None and 500 <= status_code < 600:
        return TransientPlatformError(f"PowerShell bridge server error: {text}")
    return ConnectorError(f"PowerShell call failed: {text}", status_code=status_code)
