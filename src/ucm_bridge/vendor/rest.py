"""Shared REST transport for Graph, Slack, Genesys, and Avaya SMGR.

Two behaviours are centralised here because every cloud connector needs them and
getting either wrong is expensive:

* **429 handling that honours ``Retry-After``.** Guessing a backoff when the
  platform has told you exactly how long to wait is how a migration gets an
  application throttled tenant-wide.
* **Pagination.** Each platform spells "next page" differently; the cursor
  extraction is declared per API rather than reimplemented per call site.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.connectors.errors import (
    AuthenticationError,
    AuthorizationError,
    ConnectorError,
    ObjectConflict,
    RateLimited,
    TransientPlatformError,
)
from ucm_bridge.vendor.cassette import Cassette, raise_recorded


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PATCH = "PATCH"
    PUT = "PUT"
    DELETE = "DELETE"


class RestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: HttpMethod = HttpMethod.GET
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    def key(self) -> dict[str, Any]:
        """Cassette-matching form. Headers are excluded: they carry tokens."""
        return {
            "method": self.method.value,
            "path": self.path,
            "query": self.query,
            "body": self.body,
        }


class RestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_code: int = 200
    body: Any = None
    headers: dict[str, str] = Field(default_factory=dict)


class PaginationStyle(BaseModel):
    """How one API expresses "there is more"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    next_link_field: str | None = Field(
        default=None,
        description="Absolute next-page URL in the body, e.g. Graph's '@odata.nextLink'.",
    )
    cursor_field: str | None = Field(
        default=None,
        description="Opaque cursor in the body, e.g. Slack response_metadata.next_cursor.",
    )
    cursor_parameter: str | None = Field(
        default=None, description="Query parameter the cursor is sent back in."
    )
    items_field: str | None = Field(
        default=None, description="Where the page's items live, e.g. 'value' or 'entities'."
    )


GRAPH_PAGINATION = PaginationStyle(
    next_link_field="@odata.nextLink", items_field="value"
)
SLACK_PAGINATION = PaginationStyle(
    cursor_field="response_metadata.next_cursor", cursor_parameter="cursor"
)
GENESYS_PAGINATION = PaginationStyle(items_field="entities")


def _dig(payload: Any, dotted: str) -> Any:
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


class RestTransport(Protocol):
    async def request(self, request: RestRequest) -> RestResponse: ...


class BaseRestTransport(ABC):
    def __init__(self, *, base_url: str, pagination: PaginationStyle) -> None:
        self.base_url = base_url.rstrip("/")
        self.pagination = pagination

    async def request(self, request: RestRequest) -> RestResponse:
        response = await self._dispatch(request)
        if response.status_code >= 400:
            raise translate_http_status(response, request)
        return response

    @abstractmethod
    async def _dispatch(self, request: RestRequest) -> RestResponse: ...

    async def paginate(
        self,
        request: RestRequest,
        *,
        max_pages: int = 10_000,
        on_page: Callable[[RestResponse], None] | None = None,
    ) -> AsyncIterator[list[Any]]:
        """Yield each page's items, following whichever cursor style this API uses."""
        current = request
        for _ in range(max_pages):
            response = await self.request(current)
            if on_page is not None:
                on_page(response)

            body = response.body
            items = body
            if self.pagination.items_field and isinstance(body, dict):
                items = body.get(self.pagination.items_field, [])
            yield list(items or [])

            next_request = self._next_request(current, body)
            if next_request is None:
                return
            current = next_request

    def _next_request(self, current: RestRequest, body: Any) -> RestRequest | None:
        if not isinstance(body, dict):
            return None

        if self.pagination.next_link_field:
            next_link = body.get(self.pagination.next_link_field)
            if next_link:
                path = str(next_link)
                if path.startswith(self.base_url):
                    path = path[len(self.base_url) :]
                return current.model_copy(update={"path": path, "query": {}})

        if self.pagination.cursor_field and self.pagination.cursor_parameter:
            cursor = _dig(body, self.pagination.cursor_field)
            if cursor:
                return current.model_copy(
                    update={
                        "query": {**current.query, self.pagination.cursor_parameter: cursor}
                    }
                )
        return None


class CassetteRestTransport(BaseRestTransport):
    def __init__(self, cassette: Cassette, *, base_url: str, pagination: PaginationStyle):
        super().__init__(base_url=base_url, pagination=pagination)
        self.cassette = cassette

    async def _dispatch(self, request: RestRequest) -> RestResponse:
        interaction = self.cassette.lookup(f"{request.method.value} {request.path}", request.key())
        raise_recorded(interaction)
        payload = interaction.response
        if isinstance(payload, dict) and "status_code" in payload and "body" in payload:
            return RestResponse.model_validate(payload)
        return RestResponse(status_code=interaction.status_code or 200, body=payload)


class HttpxRestTransport(BaseRestTransport):
    """Live REST over httpx. Requires the 'http' extra."""

    def __init__(
        self,
        *,
        base_url: str,
        pagination: PaginationStyle,
        token_provider: Callable[[], Any],
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(base_url=base_url, pagination=pagination)
        self._token_provider = token_provider
        self.timeout_seconds = timeout_seconds
        self._client: Any = None

    async def _dispatch(self, request: RestRequest) -> RestResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConnectorError(
                "The live REST transport needs the 'http' extra: pip install 'ucm-bridge[http]'."
            ) from exc

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout_seconds
            )

        token = self._token_provider()
        if hasattr(token, "__await__"):
            token = await token

        response = await self._client.request(
            request.method.value,
            request.path,
            params=request.query or None,
            json=request.body,
            headers={**request.headers, "Authorization": f"Bearer {token}"},
        )
        try:
            body = response.json()
        except ValueError:
            body = response.text
        return RestResponse(
            status_code=response.status_code, body=body, headers=dict(response.headers)
        )


def translate_http_status(response: RestResponse, request: RestRequest) -> ConnectorError:
    """Map an HTTP status onto the connector error taxonomy.

    ``Retry-After`` is honoured verbatim where present — the platform knows how
    long it wants to be left alone better than any backoff curve does.
    """
    where = f"{request.method.value} {request.path}"
    detail = str(response.body)[:500]

    if response.status_code == 401:
        return AuthenticationError(f"{where}: unauthorised. {detail}", status_code=401)
    if response.status_code == 403:
        return AuthorizationError(
            f"{where}: forbidden — usually a missing API scope or admin role. {detail}",
            status_code=403,
        )
    if response.status_code == 409:
        return ObjectConflict(f"{where}: conflict. {detail}", status_code=409)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
        return RateLimited(
            f"{where}: throttled. {detail}",
            retry_after_seconds=float(retry_after) if retry_after else None,
            status_code=429,
        )
    if 500 <= response.status_code < 600:
        return TransientPlatformError(
            f"{where}: server error {response.status_code}. {detail}",
            status_code=response.status_code,
        )
    return ConnectorError(
        f"{where}: HTTP {response.status_code}. {detail}", status_code=response.status_code
    )
