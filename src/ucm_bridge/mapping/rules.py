"""The site-specific mapping rule DSL.

Declarative by design. A rule is data — matched, rendered, and logged — never
evaluated as code, because a mapping profile is customer-supplied configuration
and executing it would make every profile a remote-code-execution vector.

    - when: { entity: Extension, site: "MUC-HQ", pattern: "^4\\d{4}$" }
      then: { e164: "+4989{{ digits }}", policy: "EMEA-International" }

Template substitution is limited to named placeholders drawn from a fixed
context (``digits``, ``site``, the entity's own scalar attributes, and regex
capture groups). There is no expression language, no arithmetic, and no
attribute traversal.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ucm_bridge.canonical.base import CanonicalEntity

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class UnknownPlaceholder(ValueError):
    """A template referenced something the context does not provide.

    Raised rather than substituting an empty string: a rule that silently
    produces ``+4989`` instead of ``+498912345`` is worse than one that fails.
    """


class RuleMatch(BaseModel):
    """The ``when`` clause."""

    model_config = ConfigDict(extra="forbid")

    entity: str = Field(description="Canonical entity kind this rule applies to.")
    site: str | None = None
    pattern: str | None = Field(
        default=None,
        description="Regex matched against the entity's primary value (digits, name, or number).",
    )
    attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Exact-match attribute conditions, e.g. {department: Finance}.",
    )

    @model_validator(mode="after")
    def _compilable(self) -> RuleMatch:
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"Invalid regex in `pattern`: {exc}") from exc
        return self


class MappingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable id, recorded in every transform log entry it causes.")
    when: RuleMatch
    then: dict[str, str] = Field(
        default_factory=dict, description="Attribute -> template to render."
    )
    description: str | None = None
    priority: int = Field(default=100, description="Lower runs first; first match wins per field.")
    stop: bool = Field(
        default=False, description="Stop evaluating further rules for this entity when matched."
    )


class RuleOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    rule_id: str
    assignments: dict[str, str] = Field(default_factory=dict)


class RuleSet(BaseModel):
    """An ordered, named set of rules."""

    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    rules: list[MappingRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> RuleSet:
        seen: set[str] = set()
        duplicates = sorted({r.id for r in self.rules if r.id in seen or seen.add(r.id)})  # type: ignore[func-returns-value]
        if duplicates:
            raise ValueError(f"Duplicate rule id(s): {duplicates}")
        return self

    def ordered(self) -> list[MappingRule]:
        return sorted(self.rules, key=lambda r: (r.priority, r.id))

    def evaluate(self, entity: CanonicalEntity) -> list[RuleOutcome]:
        """Apply every matching rule. First writer of a field wins."""
        outcomes: list[RuleOutcome] = []
        assigned: set[str] = set()

        for rule in self.ordered():
            context = _match_context(entity, rule.when)
            if context is None:
                continue

            assignments = {
                field: render_template(template, context)
                for field, template in rule.then.items()
                if field not in assigned
            }
            if assignments:
                assigned.update(assignments)
                outcomes.append(
                    RuleOutcome(
                        canonical_id=entity.canonical_id,
                        rule_id=rule.id,
                        assignments=assignments,
                    )
                )
            if rule.stop:
                break
        return outcomes


def render_template(template: str, context: dict[str, str]) -> str:
    """Substitute ``{{ name }}`` placeholders from ``context``.

    Unknown placeholders raise. Silence here becomes a malformed phone number
    that nobody notices until a customer cannot be called.
    """

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise UnknownPlaceholder(
                f"Template {template!r} references {{{{ {key} }}}}, which is not available. "
                f"Available: {sorted(context)}"
            )
        return context[key]

    return PLACEHOLDER.sub(replace, template)


def _primary_value(entity: CanonicalEntity) -> str:
    """The value a rule's ``pattern`` is matched against, per entity kind."""
    for attribute in ("digits", "directory_number", "e164", "pattern", "name",
                      "user_principal_name"):
        value = getattr(entity, attribute, None)
        if isinstance(value, str) and value:
            return value
    return entity.display_name or entity.canonical_id


def _match_context(entity: CanonicalEntity, when: RuleMatch) -> dict[str, str] | None:
    """Return the template context if the rule matches, else None."""
    if entity.kind != when.entity:
        return None

    site = getattr(entity, "site_code", None)
    if when.site is not None and site != when.site:
        return None

    for attribute, expected in when.attributes.items():
        actual = getattr(entity, attribute, None)
        if actual is None or str(actual) != expected:
            return None

    value = _primary_value(entity)
    context: dict[str, str] = {
        "digits": value,
        "value": value,
        "site": str(site or ""),
        "kind": entity.kind,
        "display_name": entity.display_name or "",
    }

    if when.pattern is not None:
        match = re.fullmatch(when.pattern, value)
        if match is None:
            return None
        for index, group in enumerate(match.groups(), start=1):
            context[f"group{index}"] = group or ""
        context.update({k: v or "" for k, v in (match.groupdict() or {}).items()})

    # Scalar attributes are available by name, so a rule can build a value from
    # the entity itself without an expression language.
    for name in type(entity).model_fields:
        attribute_value = getattr(entity, name, None)
        if isinstance(attribute_value, (str, int, float, bool)):
            context.setdefault(name, str(attribute_value))

    return context


def load_ruleset(payload: dict[str, Any]) -> RuleSet:
    """Load a rule set from parsed YAML/JSON."""
    return RuleSet.model_validate(payload)
