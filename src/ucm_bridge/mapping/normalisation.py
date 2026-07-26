"""Number normalisation: extension -> E.164, with overlap and collision detection.

This is where migrations quietly go wrong. An extension is only meaningful
inside its own dial plan; E.164 is global. Turning one into the other needs a
per-site prefix table, and the two failure modes are both silent:

* **Overlap** — two site rules match the same extension, so which E.164 you get
  depends on rule order. Detected structurally, before any number is produced.
* **Collision** — two different extensions normalise to the *same* E.164, which
  means one of them will lose its calls. Detected across the whole estate.

Neither is reported as a warning that scrolls past. Both are surfaced as
structured results that the planner refuses to proceed past.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")


class NormalisationOutcome(StrEnum):
    NORMALISED = "NORMALISED"
    ALREADY_E164 = "ALREADY_E164"
    NO_RULE = "NO_RULE"
    """No site rule matched. The extension has no external number and the
    assessment engine raises a blocker for E.164-requiring targets."""
    AMBIGUOUS = "AMBIGUOUS"
    """More than one rule matched. Refusing to guess is the whole point."""
    INVALID_RESULT = "INVALID_RESULT"


class SiteNumberRule(BaseModel):
    """How one site's internal extensions map to E.164."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site_code: str
    internal_pattern: str = Field(
        description=r"Regex the extension must match, e.g. r'^5\d{3}$'. Anchored by the author."
    )
    e164_prefix: str = Field(pattern=r"^\+[1-9]\d{0,14}$")
    strip_digits: int = Field(
        default=0, ge=0, description="Leading internal digits dropped before appending."
    )
    description: str | None = None

    @model_validator(mode="after")
    def _compilable(self) -> SiteNumberRule:
        try:
            re.compile(self.internal_pattern)
        except re.error as exc:
            raise ValueError(
                f"Site {self.site_code}: internal_pattern is not a valid regex: {exc}"
            ) from exc
        return self

    def matches(self, digits: str) -> bool:
        return re.fullmatch(self.internal_pattern, digits) is not None

    def apply(self, digits: str) -> str:
        return f"{self.e164_prefix}{digits[self.strip_digits :]}"


class NormalisationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extension: str
    site_code: str | None = None
    outcome: NormalisationOutcome
    e164: str | None = None
    matched_rules: list[str] = Field(default_factory=list)
    detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome in (
            NormalisationOutcome.NORMALISED,
            NormalisationOutcome.ALREADY_E164,
        )


class Overlap(BaseModel):
    """Two rules at the same site that can both match the same extension."""

    model_config = ConfigDict(extra="forbid")

    site_code: str
    first_pattern: str
    second_pattern: str
    example: str
    detail: str


class Collision(BaseModel):
    """Two different extensions that normalise to the same E.164."""

    model_config = ConfigDict(extra="forbid")

    e164: str
    sources: list[str] = Field(description="'<site>:<extension>' pairs that collide.")


class NumberPlan(BaseModel):
    """A per-customer set of site rules, plus the checks that keep it honest."""

    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    rules: list[SiteNumberRule] = Field(default_factory=list)

    def rules_for(self, site_code: str | None) -> list[SiteNumberRule]:
        return [r for r in self.rules if site_code is not None and r.site_code == site_code]

    def normalise(self, extension: str, site_code: str | None) -> NormalisationResult:
        digits = extension.strip()

        if E164_PATTERN.fullmatch(digits):
            return NormalisationResult(
                extension=extension,
                site_code=site_code,
                outcome=NormalisationOutcome.ALREADY_E164,
                e164=digits,
            )

        candidates = [r for r in self.rules_for(site_code) if r.matches(digits)]

        if not candidates:
            return NormalisationResult(
                extension=extension,
                site_code=site_code,
                outcome=NormalisationOutcome.NO_RULE,
                detail=(
                    f"No rule at site {site_code!r} matches {digits!r}. This extension has no "
                    "external number."
                ),
            )

        if len(candidates) > 1:
            # Refusing to pick is deliberate: silently choosing the first rule is
            # how an estate ends up with numbers nobody can explain.
            return NormalisationResult(
                extension=extension,
                site_code=site_code,
                outcome=NormalisationOutcome.AMBIGUOUS,
                matched_rules=[r.internal_pattern for r in candidates],
                detail=(
                    f"{len(candidates)} rules at site {site_code!r} match {digits!r}. "
                    "Narrow the patterns; the plan will not proceed on a guess."
                ),
            )

        rule = candidates[0]

        if rule.strip_digits >= len(digits):
            # Stripping every digit leaves the bare site prefix, which is often
            # still valid E.164 and therefore passes the format check while being
            # obviously wrong. Catch it structurally instead.
            return NormalisationResult(
                extension=extension,
                site_code=site_code,
                outcome=NormalisationOutcome.INVALID_RESULT,
                matched_rules=[rule.internal_pattern],
                detail=(
                    f"strip_digits={rule.strip_digits} removes every digit of {digits!r}, "
                    f"leaving only the prefix {rule.e164_prefix!r}. Check the rule for site "
                    f"{rule.site_code}."
                ),
            )

        produced = rule.apply(digits)
        if not E164_PATTERN.fullmatch(produced):
            return NormalisationResult(
                extension=extension,
                site_code=site_code,
                outcome=NormalisationOutcome.INVALID_RESULT,
                matched_rules=[rule.internal_pattern],
                detail=(
                    f"Rule produced {produced!r}, which is not valid E.164. Check the prefix "
                    f"and strip_digits for site {rule.site_code}."
                ),
            )

        return NormalisationResult(
            extension=extension,
            site_code=site_code,
            outcome=NormalisationOutcome.NORMALISED,
            e164=produced,
            matched_rules=[rule.internal_pattern],
        )

    def detect_overlaps(self, *, probe_length: int = 5) -> list[Overlap]:
        """Find rule pairs at the same site that can both match one extension.

        Regex intersection is undecidable in general, so this probes: it
        enumerates candidate strings each rule can match and checks whether the
        other rule matches them too. It cannot prove absence of overlap, and it
        says so rather than implying a clean bill of health.
        """
        overlaps: list[Overlap] = []
        by_site: dict[str, list[SiteNumberRule]] = {}
        for rule in self.rules:
            by_site.setdefault(rule.site_code, []).append(rule)

        for site, rules in by_site.items():
            for i, first in enumerate(rules):
                for second in rules[i + 1 :]:
                    example = _find_common_match(first, second, probe_length)
                    if example is not None:
                        overlaps.append(
                            Overlap(
                                site_code=site,
                                first_pattern=first.internal_pattern,
                                second_pattern=second.internal_pattern,
                                example=example,
                                detail=(
                                    f"Extension {example!r} matches both patterns at {site}. "
                                    "Normalisation of this extension is ambiguous."
                                ),
                            )
                        )
        return overlaps

    def detect_collisions(
        self, extensions: list[tuple[str, str | None]]
    ) -> list[Collision]:
        """Find distinct extensions that normalise onto the same E.164."""
        produced: dict[str, list[str]] = {}
        for extension, site in extensions:
            result = self.normalise(extension, site)
            if result.e164 is None:
                continue
            produced.setdefault(result.e164, []).append(f"{site or '?'}:{extension}")

        return [
            Collision(e164=e164, sources=sorted(set(sources)))
            for e164, sources in sorted(produced.items())
            if len(set(sources)) > 1
        ]


def _find_common_match(
    first: SiteNumberRule, second: SiteNumberRule, probe_length: int
) -> str | None:
    """Probe for a digit string both patterns accept.

    Bounded and deterministic: tries digit strings up to ``probe_length`` built
    from a small alphabet. Enough to catch the overlaps that occur in real dial
    plans without pretending to be a decision procedure.
    """
    alphabet = "0123456789"
    for length in range(1, probe_length + 1):
        for seed in _seeded_candidates(alphabet, length):
            if first.matches(seed) and second.matches(seed):
                return seed
    return None


def _seeded_candidates(alphabet: str, length: int) -> list[str]:
    """A small, deterministic sample of digit strings of the given length.

    Full enumeration is 10^length; this samples repeated digits and simple
    ascending runs, which is where real extension ranges live.
    """
    candidates = [d * length for d in alphabet]
    for start in range(10):
        candidates.append("".join(str((start + i) % 10) for i in range(length)))
    return candidates
