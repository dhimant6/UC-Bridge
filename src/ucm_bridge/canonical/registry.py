"""Entity-kind registry and the discriminated union over all canonical entities."""

from __future__ import annotations

from typing import Annotated, Any, TypeVar, Union

from pydantic import Field, TypeAdapter

from ucm_bridge.canonical.base import CanonicalEntity

ENTITY_REGISTRY: dict[str, type[CanonicalEntity]] = {}
"""kind -> concrete entity class. Populated by the @canonical_entity decorator."""

_T = TypeVar("_T", bound=CanonicalEntity)


def canonical_entity(cls: type[_T]) -> type[_T]:  # noqa: UP047 - see ADR-0002 on 3.12 syntax
    """Register a concrete canonical entity class.

    The class must pin ``kind`` to a ``Literal`` default so the union below can be
    discriminated and so JSON Schema consumers can branch on a single field.
    """
    field = cls.model_fields.get("kind")
    if field is None or field.default in (None, ...):
        raise TypeError(f"{cls.__name__} must declare `kind: Literal[...] = \"...\"`")
    kind = str(field.default)
    if kind in ENTITY_REGISTRY and ENTITY_REGISTRY[kind] is not cls:
        raise TypeError(
            f"Duplicate canonical entity kind {kind!r}: {ENTITY_REGISTRY[kind]} vs {cls}"
        )
    ENTITY_REGISTRY[kind] = cls
    return cls


def _load_all_entity_modules() -> None:
    """Import every domain module so the registry is complete.

    Kept as an explicit list rather than a package scan: a canonical model that
    silently changes shape depending on import order is not a canonical model.
    """
    from ucm_bridge.canonical import (  # noqa: F401
        callhandling,
        collaboration,
        contactcenter,
        dialplan,
        endpoints,
        identity,
        messaging,
        numbering,
        policy,
        trunking,
    )


def all_entity_types() -> dict[str, type[CanonicalEntity]]:
    _load_all_entity_modules()
    return dict(ENTITY_REGISTRY)


def entity_class(kind: str) -> type[CanonicalEntity]:
    types = all_entity_types()
    try:
        return types[kind]
    except KeyError:
        raise KeyError(
            f"Unknown canonical entity kind {kind!r}. Known kinds: {sorted(types)}"
        ) from None


_adapter_cache: dict[str, TypeAdapter[Any]] = {}


def entity_adapter() -> TypeAdapter[Any]:
    """A ``TypeAdapter`` over the discriminated union of every entity kind.

    Use this to deserialise a heterogeneous entity list from a snapshot.
    """
    if "adapter" not in _adapter_cache:
        types = all_entity_types()
        if not types:  # pragma: no cover - defensive
            raise RuntimeError("Canonical entity registry is empty")
        # Built at runtime from the registry, so neither ruff's PEP 604 rewrite
        # nor mypy's static alias check applies.
        union = Union[tuple(types.values())]  # type: ignore[valid-type] # noqa: UP007
        _adapter_cache["adapter"] = TypeAdapter(Annotated[union, Field(discriminator="kind")])
    return _adapter_cache["adapter"]
