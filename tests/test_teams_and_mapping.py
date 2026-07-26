"""Phase 2: Teams connector, number normalisation, rule DSL, and auto-mapping."""

from __future__ import annotations

from pathlib import Path

import pytest

from ucm_bridge.canonical.base import CanonicalEntity, Platform, SourceRef
from ucm_bridge.canonical.dialplan import CallingPermission, PermissionClass
from ucm_bridge.canonical.identity import LicenseAssignment, User
from ucm_bridge.canonical.numbering import E164Number, Extension
from ucm_bridge.canonical.policy import EmergencyLocation
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.canonical.trunking import VoiceRoutingPolicy
from ucm_bridge.connectors.contracts import ExtractRequest
from ucm_bridge.connectors.teams import TeamsConnector
from ucm_bridge.mapping import (
    MappingDecision,
    MappingOverride,
    MappingProfile,
    MappingRule,
    NormalisationOutcome,
    NumberPlan,
    RuleMatch,
    RuleSet,
    SiteNumberRule,
    UnknownPlaceholder,
    apply_profile,
    render_template,
    suggest_mapping,
)
from ucm_bridge.vendor.cassette import Cassette
from ucm_bridge.vendor.msgraph import TEAMS_CMDLETS, assign_licence, unverified_cmdlets
from ucm_bridge.vendor.powershell import CassettePowerShellBridge, PowerShellCommand
from ucm_bridge.vendor.rest import GRAPH_PAGINATION, CassetteRestTransport

CASSETTE_PATH = Path(__file__).parent / "cassettes" / "teams-tenant.json"


@pytest.fixture
def teams() -> TeamsConnector:
    cassette = Cassette.load(CASSETTE_PATH)
    return TeamsConnector(
        graph=CassetteRestTransport(
            cassette, base_url="https://graph.microsoft.com/v1.0", pagination=GRAPH_PAGINATION
        ),
        powershell=CassettePowerShellBridge(TEAMS_CMDLETS, cassette),
        instance_id="contoso.onmicrosoft.com",
        tenant_id="contoso",
    )


async def extract(teams: TeamsConnector) -> EstateSnapshot:
    return await teams.extract_snapshot(
        ExtractRequest(run_id="r", tenant_id="contoso", estate_id="contoso-teams")
    )


# --------------------------------------------------------------------------- #
# Verified API surface
# --------------------------------------------------------------------------- #


def test_assign_licence_always_sends_remove_licenses() -> None:
    """Verified against the v1.0 reference: removeLicenses is required even when empty."""
    request = assign_licence("u-1", add_sku_ids=["sku-a"])
    assert request.path == "/users/u-1/assignLicense"
    assert request.body == {
        "addLicenses": [{"skuId": "sku-a", "disabledPlans": []}],
        "removeLicenses": [],
    }


def test_phone_number_cmdlet_uses_the_verified_parameter_names() -> None:
    """The parameters are -TelephoneNumber and -NumberType, not PhoneNumber/PhoneNumberType."""
    cmdlet = TEAMS_CMDLETS["Set-CsPhoneNumberAssignment"]
    assert set(cmdlet.required_parameters) == {"Identity", "TelephoneNumber", "NumberType"}
    assert cmdlet.verified_source is not None

    with pytest.raises(Exception, match="not on the reviewed signature"):
        cmdlet.validate_call(
            {"Identity": "a", "TelephoneNumber": "+1", "NumberType": "CallingPlan", "Bogus": 1}
        )


def test_unverified_cmdlets_are_declared_as_such() -> None:
    unverified = unverified_cmdlets(list(TEAMS_CMDLETS))
    assert "New-CsOnlineLisLocation" in unverified
    assert "Set-CsPhoneNumberAssignment" not in unverified


def test_powershell_commands_are_structured_not_strings() -> None:
    """A display name containing a quote must not be able to become a command."""
    command = PowerShellCommand(
        cmdlet="Set-CsPhoneNumberAssignment",
        parameters={"Identity": 'evil"; Remove-Item C:\\ -Recurse; #'},
    )
    assert command.as_request()["parameters"]["Identity"].startswith('evil"')
    # The rendered form is presentation only; the bridge passes typed parameters.
    assert "Remove-Item" in command.preview()
    assert isinstance(command.parameters, dict)


# --------------------------------------------------------------------------- #
# Teams extraction
# --------------------------------------------------------------------------- #


async def test_teams_extraction_assembles_users_from_graph_and_powershell(teams) -> None:
    snapshot = await extract(teams)
    anna = next(
        e
        for e in snapshot.entities
        if isinstance(e, User) and e.user_principal_name == "anna.mueller@contoso.example"
    )
    assert anna.department == "Finance"        # from Graph
    assert anna.telephony_enabled is True       # from Teams PowerShell
    assert anna.site_code == "MUC-HQ"


async def test_on_premises_sourced_numbers_are_flagged_as_unwritable(teams) -> None:
    snapshot = await extract(teams)
    cerys = next(
        e
        for e in snapshot.entities
        if isinstance(e, User) and e.user_principal_name == "cerys.jones@contoso.example"
    )
    degraded = {d.attribute for d in cerys.fidelity.degraded_attributes}
    assert "primary_number_ref" in degraded
    assert any("on-premises" in w for w in snapshot.warnings)


async def test_numbers_without_an_emergency_location_are_reported(teams) -> None:
    snapshot = await extract(teams)
    number = next(
        e for e in snapshot.entities if isinstance(e, E164Number) and e.e164 == "+498912345102"
    )
    assert number.emergency_location_ref is None
    assert any("no emergency location" in w for w in snapshot.warnings)


async def test_licence_assignments_record_what_they_unlock(teams) -> None:
    snapshot = await extract(teams)
    licences = [e for e in snapshot.entities if isinstance(e, LicenseAssignment)]
    phone_system = next(lic for lic in licences if lic.sku_name == "MCOEV")
    assert "phone_system" in phone_system.required_for


async def test_teams_is_declared_eventually_consistent(teams) -> None:
    """The single most consequential line in the manifest."""
    policy = teams.capabilities().eventual_consistency
    assert policy.is_eventually_consistent
    assert "E164Number" in policy.confirm_required_for_kinds
    assert "LicenseAssignment" in policy.confirm_required_for_kinds


async def test_emergency_location_validation_state_carries(teams) -> None:
    snapshot = await extract(teams)
    locations = {e.site_code: e for e in snapshot.entities if isinstance(e, EmergencyLocation)}
    assert locations["MUC-HQ"].is_validated
    assert not locations["LON-BR"].is_validated
    assert any(
        d.attribute == "is_validated" for d in locations["LON-BR"].fidelity.degraded_attributes
    )


# --------------------------------------------------------------------------- #
# Number normalisation
# --------------------------------------------------------------------------- #


def munich_plan() -> NumberPlan:
    return NumberPlan(
        name="contoso",
        rules=[
            SiteNumberRule(
                site_code="MUC-HQ", internal_pattern=r"5\d{3}", e164_prefix="+498912345"
            ),
            SiteNumberRule(
                site_code="LON-BR", internal_pattern=r"7\d{3}", e164_prefix="+442071838"
            ),
        ],
    )


def test_extension_normalises_to_e164() -> None:
    result = munich_plan().normalise("5101", "MUC-HQ")
    assert result.outcome is NormalisationOutcome.NORMALISED
    assert result.e164 == "+4989123455101"


def test_an_extension_with_no_rule_is_reported_not_guessed() -> None:
    result = munich_plan().normalise("9999", "MUC-HQ")
    assert result.outcome is NormalisationOutcome.NO_RULE
    assert result.e164 is None
    assert "no external number" in (result.detail or "")


def test_ambiguous_rules_refuse_to_pick() -> None:
    plan = NumberPlan(
        rules=[
            SiteNumberRule(site_code="MUC", internal_pattern=r"5\d{3}", e164_prefix="+4989"),
            SiteNumberRule(site_code="MUC", internal_pattern=r"\d{4}", e164_prefix="+4930"),
        ]
    )
    result = plan.normalise("5101", "MUC")
    assert result.outcome is NormalisationOutcome.AMBIGUOUS
    assert result.e164 is None
    assert len(result.matched_rules) == 2


def test_overlapping_rules_are_detected_structurally() -> None:
    plan = NumberPlan(
        rules=[
            SiteNumberRule(site_code="MUC", internal_pattern=r"5\d{3}", e164_prefix="+4989"),
            SiteNumberRule(site_code="MUC", internal_pattern=r"\d{4}", e164_prefix="+4930"),
        ]
    )
    overlaps = plan.detect_overlaps()
    assert overlaps
    assert overlaps[0].site_code == "MUC"


def test_collisions_across_sites_are_detected() -> None:
    """Two extensions producing one number means one of them loses its calls.

    Site A's 1234 and site B's 9234 both land on +49891234 — a plausible mistake
    when one site's rule strips a leading digit and the other does not.
    """
    plan = NumberPlan(
        rules=[
            SiteNumberRule(site_code="A", internal_pattern=r"1\d{3}", e164_prefix="+4989"),
            SiteNumberRule(
                site_code="B", internal_pattern=r"9\d{3}", e164_prefix="+49891", strip_digits=1
            ),
        ]
    )
    assert plan.normalise("1234", "A").e164 == "+49891234"
    assert plan.normalise("9234", "B").e164 == "+49891234"

    collisions = plan.detect_collisions([("1234", "A"), ("9234", "B"), ("1567", "A")])
    assert len(collisions) == 1
    assert collisions[0].e164 == "+49891234"
    assert sorted(collisions[0].sources) == ["A:1234", "B:9234"]


def test_distinct_extensions_do_not_report_a_collision() -> None:
    plan = munich_plan()
    assert plan.detect_collisions([("5101", "MUC-HQ"), ("5102", "MUC-HQ")]) == []


def test_an_invalid_prefix_combination_is_caught() -> None:
    plan = NumberPlan(
        rules=[
            SiteNumberRule(
                site_code="A", internal_pattern=r"\d{4}", e164_prefix="+4989", strip_digits=9
            )
        ]
    )
    result = plan.normalise("5101", "A")
    assert result.outcome is NormalisationOutcome.INVALID_RESULT


# --------------------------------------------------------------------------- #
# Rule DSL
# --------------------------------------------------------------------------- #


def test_templates_substitute_named_placeholders() -> None:
    assert render_template("+4989{{ digits }}", {"digits": "5101"}) == "+49895101"


def test_an_unknown_placeholder_raises_rather_than_producing_a_broken_number() -> None:
    with pytest.raises(UnknownPlaceholder, match="not available"):
        render_template("+4989{{ nope }}", {"digits": "5101"})


def test_the_dsl_does_not_evaluate_code() -> None:
    """A profile is customer-supplied config; executing it would be an RCE vector."""
    context = {"digits": "5101"}
    assert render_template("{{ digits }}__import__('os')", context) == "5101__import__('os')"


def _extension(digits: str, site: str, owner: str | None = None) -> Extension:
    return Extension(
        canonical_id=CanonicalEntity.mint_canonical_id(
            Platform.CISCO_CUCM, "Extension", digits, instance_id="c1"
        ),
        digits=digits,
        site_code=site,
        owner_ref=owner,
        source_ref=SourceRef(
            platform=Platform.CISCO_CUCM, instance_id="c1", native_type="line", native_key=digits
        ),
    )


def test_rules_match_on_site_and_pattern() -> None:
    ruleset = RuleSet(
        rules=[
            MappingRule(
                id="muc-e164",
                when=RuleMatch(entity="Extension", site="MUC-HQ", pattern=r"5\d{3}"),
                then={"description": "Munich {{ digits }}"},
            )
        ]
    )
    outcomes = ruleset.evaluate(_extension("5101", "MUC-HQ"))
    assert outcomes[0].assignments == {"description": "Munich 5101"}

    assert ruleset.evaluate(_extension("5101", "LON-BR")) == []
    assert ruleset.evaluate(_extension("7301", "MUC-HQ")) == []


def test_first_matching_rule_wins_per_field() -> None:
    ruleset = RuleSet(
        rules=[
            MappingRule(
                id="specific",
                priority=10,
                when=RuleMatch(entity="Extension", pattern=r"5101"),
                then={"description": "specific"},
            ),
            MappingRule(
                id="general",
                priority=50,
                when=RuleMatch(entity="Extension", pattern=r"5\d{3}"),
                then={"description": "general"},
            ),
        ]
    )
    outcomes = ruleset.evaluate(_extension("5101", "MUC-HQ"))
    assert [o.rule_id for o in outcomes] == ["specific"]


def test_duplicate_rule_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate rule id"):
        RuleSet(
            rules=[
                MappingRule(id="x", when=RuleMatch(entity="Extension"), then={}),
                MappingRule(id="x", when=RuleMatch(entity="Extension"), then={}),
            ]
        )


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def snapshot_with_extensions() -> EstateSnapshot:
    return EstateSnapshot.build(
        snapshot_id="snap-src",
        tenant_id="contoso",
        estate_id="contoso-cucm",
        entities=[
            _extension("5101", "MUC-HQ"),
            _extension("5102", "MUC-HQ"),
            _extension("9999", "MUC-HQ"),  # no rule matches
        ],
    )


def profile(**overrides) -> MappingProfile:
    return MappingProfile(
        profile_id="contoso-teams",
        name="Contoso to Teams",
        tenant_id="contoso",
        target_platform="microsoft.teams",
        number_plan=munich_plan(),
        **overrides,
    )


def test_applying_a_profile_mints_numbers_and_logs_why() -> None:
    result = apply_profile(snapshot_with_extensions(), profile())

    assert result.numbers_created == 2
    numbers = {e.e164 for e in result.snapshot.entities if isinstance(e, E164Number)}
    assert numbers == {"+4989123455101", "+4989123455102"}

    extension = next(
        e for e in result.snapshot.entities if isinstance(e, Extension) and e.digits == "5101"
    )
    assert extension.e164_ref is not None
    assert any(entry.operation.value == "NORMALISE" for entry in extension.transform_log)


def test_derived_numbers_are_never_marked_lossless() -> None:
    """Nobody has confirmed the carrier actually delivers a computed DID."""
    result = apply_profile(snapshot_with_extensions(), profile())
    number = next(e for e in result.snapshot.entities if isinstance(e, E164Number))
    assert number.fidelity.level.value == "DEGRADED"
    assert "carrier" in number.fidelity.degraded_attributes[0].target_behaviour


def test_extensions_with_no_rule_become_issues_not_silent_gaps() -> None:
    result = apply_profile(snapshot_with_extensions(), profile())
    assert not result.is_clean
    problem = next(i for i in result.issues if i.problem == "NO_RULE")
    assert "9999" in (problem.detail or "")


def test_profiles_are_deterministic() -> None:
    first = apply_profile(snapshot_with_extensions(), profile())
    second = apply_profile(snapshot_with_extensions(), profile())
    assert first.snapshot.snapshot_digest == second.snapshot.snapshot_digest


def test_a_human_override_beats_a_rule_and_is_attributed() -> None:
    source = snapshot_with_extensions()
    extension = next(e for e in source.entities if isinstance(e, Extension) and e.digits == "5101")

    ruled = profile(
        rules=RuleSet(
            rules=[
                MappingRule(
                    id="desc",
                    when=RuleMatch(entity="Extension"),
                    then={"description": "set by rule"},
                )
            ]
        ),
        overrides=[
            MappingOverride(
                canonical_id=extension.canonical_id,
                attribute="description",
                value="set by hand",
                set_by="planner@contoso.example",
                reason="business owner asked for this label",
            )
        ],
    )
    result = apply_profile(source, ruled)
    updated = next(
        e for e in result.snapshot.entities if isinstance(e, Extension) and e.digits == "5101"
    )
    assert updated.description == "set by hand"

    override_entry = next(
        entry for entry in updated.transform_log if entry.operation.value == "OVERRIDE"
    )
    assert override_entry.actor == "planner@contoso.example"
    assert override_entry.before == "set by rule"


def test_an_override_for_a_missing_object_is_reported() -> None:
    result = apply_profile(
        snapshot_with_extensions(),
        profile(
            overrides=[
                MappingOverride(
                    canonical_id="does-not-exist",
                    attribute="description",
                    value="x",
                    set_by="planner@contoso.example",
                )
            ]
        ),
    )
    assert any(i.problem == "ORPHAN_OVERRIDE" for i in result.issues)


# --------------------------------------------------------------------------- #
# Auto-mapping
# --------------------------------------------------------------------------- #


def _css(name: str, klass: PermissionClass, members: int = 2) -> CallingPermission:
    return CallingPermission(
        canonical_id=f"css-{name}",
        name=name,
        permission_class=klass,
        permitted_partition_refs=[f"p{i}" for i in range(members)],
    )


def _vrp(name: str, usages: int = 2) -> VoiceRoutingPolicy:
    return VoiceRoutingPolicy(
        canonical_id=f"vrp-{name}",
        name=name,
        pstn_usage_refs=[f"u{i}" for i in range(usages)],
    )


def test_a_clear_name_match_is_auto_mapped() -> None:
    candidate = suggest_mapping(
        _css("CSS_EMEA_International", PermissionClass.INTERNATIONAL),
        [_vrp("EMEA-International"), _vrp("APAC-Local")],
    )
    assert candidate.decision is MappingDecision.AUTO
    assert candidate.target_label == "EMEA-International"
    assert candidate.confidence >= 0.75
    assert any(s.name == "name_similarity" for s in candidate.signals)


def test_a_near_tie_is_downgraded_and_says_so() -> None:
    """An ambiguous best answer is worse than a confident low score."""
    candidate = suggest_mapping(
        _css("CSS_National", PermissionClass.NATIONAL),
        [_vrp("National-A"), _vrp("National-B")],
    )
    assert candidate.needs_review
    assert any(s.name == "ambiguous" for s in candidate.signals)


def test_no_plausible_target_is_reported_as_none() -> None:
    candidate = suggest_mapping(
        _css("CSS_Munich_Internal", PermissionClass.INTERNAL_ONLY),
        [_vrp("Zzzzzz")],
    )
    assert candidate.decision is MappingDecision.NONE
    assert candidate.target_id is None


def test_confidence_is_explainable() -> None:
    candidate = suggest_mapping(
        _css("EMEA-International", PermissionClass.INTERNATIONAL),
        [_vrp("EMEA-International")],
    )
    assert candidate.rationale
    assert sum(s.weight for s in candidate.signals) >= candidate.confidence - 0.001
