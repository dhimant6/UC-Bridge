"""Number porting: the long pole of any repatriation.

Ports are the one part of a migration the platform cannot make go faster, and
the one where a wrong date strands a customer with numbers in neither estate.
So this models the order as an explicit state machine with legal transitions,
rather than a status string that any code path can set to anything.

The dual-homed window is modelled deliberately. Between the FOC time and the
carrier actually cutting over, a number legitimately exists in both estates. It
is a real, time-boxed state, and coexistence routing has to know about it — if
the platform pretended a number belonged to exactly one estate at all times,
calls would fork or blackhole during the window.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import utcnow
from ucm_bridge.canonical.numbering import PortingRecord, PortOrderState

#: Legal transitions. Anything not listed is refused.
ALLOWED_TRANSITIONS: dict[PortOrderState, frozenset[PortOrderState]] = {
    PortOrderState.DRAFT: frozenset(
        {PortOrderState.LOA_PENDING, PortOrderState.CANCELLED}
    ),
    PortOrderState.LOA_PENDING: frozenset(
        {PortOrderState.SUBMITTED, PortOrderState.CANCELLED}
    ),
    PortOrderState.SUBMITTED: frozenset(
        {PortOrderState.CARRIER_VALIDATING, PortOrderState.REJECTED, PortOrderState.CANCELLED}
    ),
    PortOrderState.CARRIER_VALIDATING: frozenset(
        {PortOrderState.FOC_RECEIVED, PortOrderState.REJECTED, PortOrderState.CANCELLED}
    ),
    # A rejected order goes back to draft to be corrected and resubmitted.
    PortOrderState.REJECTED: frozenset({PortOrderState.DRAFT, PortOrderState.CANCELLED}),
    PortOrderState.FOC_RECEIVED: frozenset(
        {PortOrderState.SCHEDULED, PortOrderState.REJECTED, PortOrderState.CANCELLED}
    ),
    PortOrderState.SCHEDULED: frozenset(
        {PortOrderState.IN_CUTOVER, PortOrderState.CANCELLED}
    ),
    # Once the carrier has started, cancelling is no longer ours to do.
    PortOrderState.IN_CUTOVER: frozenset({PortOrderState.COMPLETED, PortOrderState.REJECTED}),
    PortOrderState.COMPLETED: frozenset(),
    PortOrderState.CANCELLED: frozenset(),
}

#: Fields carriers universally require on a Customer Service Record. Names vary,
#: so these are canonical keys the connector maps onto a carrier's own form.
REQUIRED_CSR_FIELDS: tuple[str, ...] = (
    "billing_telephone_number",
    "account_number",
    "service_address",
    "authorised_signatory",
    "company_name",
)


class IllegalPortTransition(ValueError):
    """A port order was moved to a state it cannot legally reach."""


class PortReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_reference: str | None = None
    ready_to_submit: bool = False
    missing_csr_fields: list[str] = Field(default_factory=list)
    missing_loa: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LoaPacket(BaseModel):
    """The Letter of Authority data a carrier needs to accept a port."""

    model_config = ConfigDict(extra="forbid")

    order_reference: str
    losing_carrier: str
    gaining_carrier: str
    company_name: str
    authorised_signatory: str
    billing_telephone_number: str
    account_number: str
    service_address: str
    numbers: list[str] = Field(default_factory=list)
    requested_foc_date: date | None = None
    generated_at: datetime = Field(default_factory=utcnow)

    def as_text(self) -> str:
        """Human-readable LOA body for the customer to sign."""
        lines = [
            f"LETTER OF AUTHORITY - {self.order_reference}",
            "",
            f"Company: {self.company_name}",
            f"Account number: {self.account_number}",
            f"Billing telephone number: {self.billing_telephone_number}",
            f"Service address: {self.service_address}",
            "",
            f"{self.company_name} authorises {self.gaining_carrier} to act as its agent to "
            f"port the numbers listed below away from {self.losing_carrier}.",
            "",
            f"Numbers ({len(self.numbers)}):",
        ]
        lines.extend(f"  {number}" for number in self.numbers)
        if self.requested_foc_date:
            lines += ["", f"Requested FOC date: {self.requested_foc_date.isoformat()}"]
        lines += [
            "",
            f"Authorised signatory: {self.authorised_signatory}",
            "Signature: ______________________",
            "Date: ______________________",
        ]
        return "\n".join(lines)


def transition(
    record: PortingRecord, to_state: PortOrderState, *, reason: str | None = None
) -> PortingRecord:
    """Move a port order to a new state, refusing anything illegal."""
    allowed = ALLOWED_TRANSITIONS[record.state]
    if to_state not in allowed:
        raise IllegalPortTransition(
            f"Port order {record.order_reference or record.canonical_id} cannot move from "
            f"{record.state.value} to {to_state.value}. Legal next states: "
            f"{sorted(s.value for s in allowed) or 'none (terminal)'}."
        )

    updates: dict[str, object] = {"state": to_state}
    if to_state is PortOrderState.REJECTED and reason:
        updates["rejection_reason"] = reason
    if to_state is PortOrderState.IN_CUTOVER:
        # The number now exists in both estates until the carrier finishes.
        updates["dual_homed_during_cutover"] = True
    if to_state is PortOrderState.COMPLETED:
        updates["dual_homed_during_cutover"] = False

    return record.model_copy(update=updates)


def assess_readiness(record: PortingRecord) -> PortReadiness:
    """Can this order be submitted? Says exactly what is missing."""
    missing = [field for field in REQUIRED_CSR_FIELDS if not record.csr_fields.get(field)]
    blocking: list[str] = []
    warnings: list[str] = []

    if not record.numbers and not record.number_refs:
        blocking.append("The order contains no numbers.")
    if not record.losing_carrier:
        blocking.append("No losing carrier recorded; the port cannot be addressed.")
    if not record.gaining_carrier:
        blocking.append("No gaining carrier recorded.")
    if missing:
        blocking.append(f"Customer Service Record is incomplete: {missing}")
    if not record.loa_reference:
        blocking.append("No signed Letter of Authority on file.")

    if record.requested_foc_date and record.requested_foc_date < date.today():
        warnings.append(
            f"Requested FOC date {record.requested_foc_date.isoformat()} is in the past."
        )

    return PortReadiness(
        order_reference=record.order_reference,
        ready_to_submit=not blocking,
        missing_csr_fields=missing,
        missing_loa=record.loa_reference is None,
        blocking_reasons=blocking,
        warnings=warnings,
    )


def build_loa_packet(record: PortingRecord) -> LoaPacket:
    """Generate the LOA data packet. Refuses on incomplete data rather than guessing."""
    readiness = assess_readiness(record)
    fatal = [r for r in readiness.blocking_reasons if "Letter of Authority" not in r]
    if fatal:
        raise ValueError(
            "Cannot generate an LOA packet from an incomplete order:\n"
            + "\n".join(f"  - {reason}" for reason in fatal)
        )

    return LoaPacket(
        order_reference=record.order_reference or record.canonical_id,
        losing_carrier=record.losing_carrier or "",
        gaining_carrier=record.gaining_carrier or "",
        company_name=record.csr_fields["company_name"],
        authorised_signatory=record.csr_fields["authorised_signatory"],
        billing_telephone_number=record.csr_fields["billing_telephone_number"],
        account_number=record.csr_fields["account_number"],
        service_address=record.csr_fields["service_address"],
        numbers=list(record.numbers),
        requested_foc_date=record.requested_foc_date,
    )


class CutoverWindow(BaseModel):
    """The period in which numbers exist in both estates."""

    model_config = ConfigDict(extra="forbid")

    order_reference: str
    start: datetime
    end: datetime
    numbers: list[str] = Field(default_factory=list)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def plan_cutover_window(
    record: PortingRecord, *, duration_hours: int = 4
) -> CutoverWindow:
    """Derive the dual-homed window from the confirmed FOC date."""
    if record.confirmed_foc_date is None:
        raise ValueError(
            f"Port order {record.order_reference} has no confirmed FOC date. A cutover "
            "window cannot be planned before the losing carrier commits to one."
        )
    start = datetime.combine(record.confirmed_foc_date, datetime.min.time()).replace(
        tzinfo=utcnow().tzinfo
    )
    return CutoverWindow(
        order_reference=record.order_reference or record.canonical_id,
        start=start,
        end=start + timedelta(hours=duration_hours),
        numbers=list(record.numbers),
    )


def numbers_in_flight(records: list[PortingRecord]) -> list[str]:
    """Numbers currently dual-homed. Coexistence routing must account for these."""
    return sorted(
        {
            number
            for record in records
            if record.dual_homed_during_cutover
            for number in record.numbers
        }
    )


def schedule_risk(records: list[PortingRecord], *, lead_time_days: int) -> list[str]:
    """Orders that cannot make their date given the carrier's stated lead time."""
    risks: list[str] = []
    today = date.today()
    for record in records:
        target = record.confirmed_foc_date or record.requested_foc_date
        if target is None:
            if record.state not in (PortOrderState.DRAFT, PortOrderState.CANCELLED):
                risks.append(
                    f"{record.order_reference or record.canonical_id}: submitted with no "
                    "target date."
                )
            continue
        if record.state in (PortOrderState.COMPLETED, PortOrderState.CANCELLED):
            continue
        if (target - today).days < lead_time_days:
            risks.append(
                f"{record.order_reference or record.canonical_id}: target {target.isoformat()} "
                f"is inside the carrier's {lead_time_days}-day lead time."
            )
    return risks
