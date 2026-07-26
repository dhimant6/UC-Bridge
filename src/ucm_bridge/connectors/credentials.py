"""Credential resolution.

Connectors never receive raw secrets in their constructor and never read
configuration files themselves. They are handed a :class:`CredentialRef` and a
:class:`CredentialProvider`, and the provider is the only thing that ever touches
secret material.

Two properties matter more than convenience here:

1. **Scope is part of the reference.** A connector operating as a discovery
   source is handed a ``READ_ONLY`` reference, and the connector base class
   refuses to execute writes with one. This is defence in depth behind the real
   control, which is a read-only service account on the vendor side.
2. **Secrets do not leak through logs.** :class:`SecretBundle` redacts under
   ``repr``, ``str``, and Pydantic serialisation. Tracebacks are the most common
   way credentials end up in a log aggregator.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from ucm_bridge.connectors.errors import CredentialError


class CredentialScope(StrEnum):
    READ_ONLY = "READ_ONLY"
    """Discovery and validation. The connector base refuses writes under this scope."""
    READ_WRITE = "READ_WRITE"


class CredentialKind(StrEnum):
    USERNAME_PASSWORD = "USERNAME_PASSWORD"
    CLIENT_CREDENTIALS = "CLIENT_CREDENTIALS"
    """Entra ID app registration or equivalent. Preferred over static passwords."""
    CERTIFICATE = "CERTIFICATE"
    API_TOKEN = "API_TOKEN"
    SSH_KEY = "SSH_KEY"
    """Needed for Avaya CM SAT/OSSI sessions."""


class CredentialRef(BaseModel):
    """A pointer to a secret. Safe to persist, log, and put in an audit record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(description="Registered provider name, e.g. 'vault', 'env', 'file'.")
    path: str = Field(description="Provider-specific location, e.g. 'kv/ucm/contoso/cucm-ro'.")
    kind: CredentialKind = CredentialKind.USERNAME_PASSWORD
    scope: CredentialScope = CredentialScope.READ_ONLY
    version: str | None = Field(
        default=None, description="Pin a secret version so a rotation cannot change a replay."
    )
    tenant_id: str | None = Field(
        default=None, description="Enforces tenant isolation at resolution time."
    )


class SecretBundle(BaseModel):
    """Resolved secret material. Never persisted, never logged, never serialised."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CredentialKind
    scope: CredentialScope
    values: dict[str, str] = Field(
        default_factory=dict,
        description="Kind-specific fields: username/password, client_id/client_secret, "
        "token, private_key, certificate_pem.",
    )

    def require(self, key: str) -> str:
        try:
            return self.values[key]
        except KeyError:
            raise CredentialError(
                f"Credential of kind {self.kind} is missing required field {key!r}. "
                f"Present fields: {sorted(self.values)}"
            ) from None

    @field_serializer("values")
    def _redact(self, _values: dict[str, str]) -> dict[str, str]:
        return {"__redacted__": "SecretBundle values are never serialised"}

    def __repr__(self) -> str:
        return f"SecretBundle(kind={self.kind.value}, scope={self.scope.value}, values=<redacted>)"

    __str__ = __repr__


class CredentialProvider(ABC):
    """Resolves a :class:`CredentialRef` into a :class:`SecretBundle`."""

    name: str

    @abstractmethod
    async def resolve(self, ref: CredentialRef) -> SecretBundle:
        """Fetch secret material. Must raise :class:`CredentialError` on any failure."""

    @abstractmethod
    async def health(self) -> bool:
        """True when the backing store is reachable and this provider can serve."""

    def _check_provider(self, ref: CredentialRef) -> None:
        if ref.provider != self.name:
            raise CredentialError(
                f"{type(self).__name__} cannot resolve a ref for provider {ref.provider!r}"
            )


class EnvCredentialProvider(CredentialProvider):
    """Reads from environment variables. Intended for CI and local development.

    ``ref.path`` is used as an environment-variable prefix: a path of
    ``CUCM_RO`` reads ``CUCM_RO_USERNAME``, ``CUCM_RO_PASSWORD``, and so on.
    """

    name = "env"

    _FIELDS: ClassVar[dict[CredentialKind, tuple[str, ...]]] = {
        CredentialKind.USERNAME_PASSWORD: ("USERNAME", "PASSWORD"),
        CredentialKind.CLIENT_CREDENTIALS: ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET"),
        CredentialKind.CERTIFICATE: ("CERTIFICATE_PEM", "PRIVATE_KEY"),
        CredentialKind.API_TOKEN: ("TOKEN",),
        CredentialKind.SSH_KEY: ("USERNAME", "PRIVATE_KEY"),
    }

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else dict(os.environ)

    async def resolve(self, ref: CredentialRef) -> SecretBundle:
        self._check_provider(ref)
        prefix = ref.path.upper().replace("-", "_").replace("/", "_")
        values: dict[str, str] = {}
        missing: list[str] = []
        for field in self._FIELDS[ref.kind]:
            var = f"{prefix}_{field}"
            if var in self._environ:
                values[field.lower()] = self._environ[var]
            else:
                missing.append(var)
        if missing:
            raise CredentialError(
                f"Environment credential {ref.path!r} is incomplete; missing: {missing}"
            )
        return SecretBundle(kind=ref.kind, scope=ref.scope, values=values)

    async def health(self) -> bool:
        return True


class LocalFileCredentialProvider(CredentialProvider):
    """Reads a JSON file of credentials. **Development only.**

    Refuses to operate unless ``UCM_BRIDGE_ENV`` is ``dev``, so it cannot be
    switched on accidentally in production by editing a config file.
    """

    name = "file"

    def __init__(self, path: Path, *, environment: str | None = None) -> None:
        self._path = path
        self._environment = environment if environment is not None else os.environ.get(
            "UCM_BRIDGE_ENV", "unset"
        )

    def _assert_dev(self) -> None:
        if self._environment != "dev":
            raise CredentialError(
                "LocalFileCredentialProvider is development-only and refuses to run with "
                f"UCM_BRIDGE_ENV={self._environment!r}. Use the Vault provider."
            )

    async def resolve(self, ref: CredentialRef) -> SecretBundle:
        self._check_provider(ref)
        self._assert_dev()
        try:
            payload: dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CredentialError(f"Cannot read credential file {self._path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CredentialError(f"Credential file {self._path} is not valid JSON: {exc}") from exc

        entry = payload.get(ref.path)
        if entry is None:
            raise CredentialError(f"No credential entry {ref.path!r} in {self._path}")
        return SecretBundle(
            kind=ref.kind, scope=ref.scope, values={k: str(v) for k, v in entry.items()}
        )

    async def health(self) -> bool:
        return self._environment == "dev" and self._path.is_file()


class VaultCredentialProvider(CredentialProvider):
    """HashiCorp Vault KV-v2 provider.

    Deliberately unimplemented rather than guessed. Filling this in requires
    confirming the deployment's Vault version, KV mount path, and auth method
    (AppRole vs Kubernetes vs token) against that deployment's own
    documentation. Inventing a plausible-looking Vault path would produce a
    provider that fails at the worst possible moment.
    """

    name = "vault"

    def __init__(self, address: str, *, mount: str = "secret", namespace: str | None = None):
        self.address = address
        self.mount = mount
        self.namespace = namespace

    async def resolve(self, ref: CredentialRef) -> SecretBundle:
        self._check_provider(ref)
        raise NotImplementedError(
            "VaultCredentialProvider is not implemented. Implement against the target "
            "deployment's documented Vault version, KV mount, and auth method."
        )

    async def health(self) -> bool:
        return False


class CredentialBroker:
    """Routes refs to registered providers and enforces tenant isolation."""

    def __init__(self, providers: list[CredentialProvider]) -> None:
        self._providers = {p.name: p for p in providers}

    def register(self, provider: CredentialProvider) -> None:
        self._providers[provider.name] = provider

    async def resolve(self, ref: CredentialRef, *, tenant_id: str | None = None) -> SecretBundle:
        if tenant_id is not None and ref.tenant_id is not None and ref.tenant_id != tenant_id:
            raise CredentialError(
                f"Credential {ref.path!r} belongs to tenant {ref.tenant_id!r} and cannot be "
                f"resolved on behalf of tenant {tenant_id!r}."
            )
        provider = self._providers.get(ref.provider)
        if provider is None:
            raise CredentialError(
                f"No credential provider registered for {ref.provider!r}. "
                f"Registered: {sorted(self._providers)}"
            )
        return await provider.resolve(ref)

    async def health(self) -> dict[str, bool]:
        return {name: await p.health() for name, p in self._providers.items()}
