"""On-prem collector agent (§5, air-gap support).

Most customers will not let anything cloud-hosted reach their CUCM publisher, so
the collector runs inside their network and **pulls** work from the control
plane. Pull, not push: an agent that only makes outbound connections needs no
inbound firewall rule, which is the difference between a two-week security
review and a six-month one.

The agent is deliberately dumb. It leases a job, runs a connector, uploads the
result, and reports. It holds no policy and makes no decisions: the guardrails
live in the connector base class and the control plane, so a compromised
collector cannot approve its own writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ucm_bridge.canonical.base import digest_of, utcnow
from ucm_bridge.canonical.snapshot import EstateSnapshot
from ucm_bridge.connectors.base import Connector
from ucm_bridge.connectors.contracts import ExtractRequest


class JobKind(StrEnum):
    DISCOVERY = "DISCOVERY"
    VALIDATION_READ = "VALIDATION_READ"
    CONNECTIVITY_TEST = "CONNECTIVITY_TEST"
    APPLY = "APPLY"


class JobState(StrEnum):
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    LEASE_EXPIRED = "LEASE_EXPIRED"


class CollectorJob(BaseModel):
    """A unit of work leased from the control plane."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    tenant_id: str
    estate_id: str
    kind: JobKind
    connector_id: str
    lease_expires_at: datetime
    entity_kinds: list[str] | None = None
    page_size: int = 500
    run_id: str | None = None
    #: Present only for APPLY jobs. The control plane has already validated the
    #: authorization; the agent does not re-derive it, but the connector still
    #: enforces every guardrail locally.
    payload: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.lease_expires_at


class JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    state: JobState
    started_at: datetime
    finished_at: datetime | None = None
    entity_count: int = 0
    snapshot_digest: str | None = None
    payload_digest: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class ControlPlaneClient(Protocol):
    """The narrow outbound-only surface the agent needs."""

    async def lease_job(self, *, agent_id: str, capabilities: list[str]) -> CollectorJob | None: ...

    async def upload_snapshot(self, *, job_id: str, snapshot: EstateSnapshot) -> None: ...

    async def report(self, result: JobResult) -> None: ...

    async def heartbeat(self, *, agent_id: str, job_id: str | None) -> None: ...


class InMemoryControlPlane:
    """A control plane stub for tests and offline development."""

    def __init__(self, jobs: list[CollectorJob] | None = None) -> None:
        self.pending: list[CollectorJob] = list(jobs or [])
        self.snapshots: dict[str, EstateSnapshot] = {}
        self.results: list[JobResult] = []
        self.heartbeats: list[tuple[str, str | None]] = []

    async def lease_job(self, *, agent_id: str, capabilities: list[str]) -> CollectorJob | None:
        for job in list(self.pending):
            if job.connector_id in capabilities:
                self.pending.remove(job)
                return job
        return None

    async def upload_snapshot(self, *, job_id: str, snapshot: EstateSnapshot) -> None:
        self.snapshots[job_id] = snapshot

    async def report(self, result: JobResult) -> None:
        self.results.append(result)

    async def heartbeat(self, *, agent_id: str, job_id: str | None) -> None:
        self.heartbeats.append((agent_id, job_id))


class CollectorAgent:
    """Runs inside the customer network and pulls work outbound."""

    def __init__(
        self,
        *,
        agent_id: str,
        control_plane: ControlPlaneClient,
        connectors: dict[str, Connector],
        allow_writes: bool = False,
    ) -> None:
        self.agent_id = agent_id
        self.control_plane = control_plane
        self.connectors = connectors
        #: Off by default. A collector that can write is a much larger blast
        #: radius inside a customer network, so enabling it is a deployment
        #: decision made explicitly.
        self.allow_writes = allow_writes

    @property
    def capabilities(self) -> list[str]:
        return sorted(self.connectors)

    async def run_once(self) -> JobResult | None:
        """Lease and execute one job. Returns None when there is nothing to do."""
        job = await self.control_plane.lease_job(
            agent_id=self.agent_id, capabilities=self.capabilities
        )
        if job is None:
            return None

        started = utcnow()
        await self.control_plane.heartbeat(agent_id=self.agent_id, job_id=job.job_id)

        if job.is_expired():
            result = JobResult(
                job_id=job.job_id,
                state=JobState.LEASE_EXPIRED,
                started_at=started,
                finished_at=utcnow(),
                error=(
                    "The lease expired before work began. The job returns to the queue "
                    "rather than being run late against a stale plan."
                ),
            )
            await self.control_plane.report(result)
            return result

        connector = self.connectors.get(job.connector_id)
        if connector is None:
            result = JobResult(
                job_id=job.job_id,
                state=JobState.FAILED,
                started_at=started,
                finished_at=utcnow(),
                error=f"This agent has no {job.connector_id!r} connector configured.",
            )
            await self.control_plane.report(result)
            return result

        if job.kind is JobKind.APPLY and not self.allow_writes:
            result = JobResult(
                job_id=job.job_id,
                state=JobState.FAILED,
                started_at=started,
                finished_at=utcnow(),
                error=(
                    "This collector is configured read-only and refused an APPLY job. "
                    "Enable writes deliberately if the deployment intends it."
                ),
            )
            await self.control_plane.report(result)
            return result

        try:
            return await self._execute(job, connector, started)
        except Exception as exc:
            result = JobResult(
                job_id=job.job_id,
                state=JobState.FAILED,
                started_at=started,
                finished_at=utcnow(),
                error=f"{type(exc).__name__}: {exc}",
            )
            await self.control_plane.report(result)
            return result

    async def _execute(
        self, job: CollectorJob, connector: Connector, started: datetime
    ) -> JobResult:
        if job.kind is JobKind.CONNECTIVITY_TEST:
            test = await connector.test_connection()
            result = JobResult(
                job_id=job.job_id,
                state=JobState.COMPLETED if test.ok else JobState.FAILED,
                started_at=started,
                finished_at=utcnow(),
                warnings=list(test.messages),
                error=None if test.ok else "Connectivity test failed.",
            )
            await self.control_plane.report(result)
            return result

        snapshot = await connector.extract_snapshot(
            ExtractRequest(
                run_id=job.run_id or job.job_id,
                tenant_id=job.tenant_id,
                estate_id=job.estate_id,
                entity_kinds=job.entity_kinds,
                page_size=job.page_size,
            )
        )
        await self.control_plane.upload_snapshot(job_id=job.job_id, snapshot=snapshot)

        result = JobResult(
            job_id=job.job_id,
            state=JobState.COMPLETED,
            started_at=started,
            finished_at=utcnow(),
            entity_count=len(snapshot),
            snapshot_digest=snapshot.snapshot_digest,
            payload_digest=digest_of(snapshot.counts_by_kind()),
            warnings=list(snapshot.warnings),
        )
        await self.control_plane.report(result)
        return result


def lease(
    *,
    job_id: str,
    tenant_id: str,
    estate_id: str,
    connector_id: str,
    kind: JobKind = JobKind.DISCOVERY,
    lease_seconds: int = 3600,
    **extra: Any,
) -> CollectorJob:
    """Convenience constructor for a leased job."""
    return CollectorJob(
        job_id=job_id,
        tenant_id=tenant_id,
        estate_id=estate_id,
        kind=kind,
        connector_id=connector_id,
        lease_expires_at=utcnow() + timedelta(seconds=lease_seconds),
        **extra,
    )
