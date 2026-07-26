/**
 * Screen 8 — Audit.
 *
 * A hash-chained, append-only log. The verify button is the point: it recomputes
 * the whole chain, so an edited or deleted record is detectable rather than
 * merely discouraged. Dry-run records are kept and marked, because "we previewed
 * this and it looked fine" is itself evidence.
 */

import { useState } from "react";
import { api } from "../api";
import type { AuditRecord } from "../api";
import { PageHead } from "../components/Layout";
import {
  ActionButton,
  ApiProblem,
  Callout,
  Empty,
  Json,
  Panel,
  Pill,
  Stat,
  useAsync,
} from "../components/Bits";
import { VirtualTable } from "../components/VirtualTable";
import type { Column } from "../components/VirtualTable";
import { useApp, useGate } from "../state";

const ACTIONS = [
  "OBJECT_WRITTEN",
  "OBJECT_SKIPPED",
  "RUN_STARTED",
  "RUN_COMPLETED",
  "RUN_RESUMED",
  "RUN_FAILED",
  "DRY_RUN_PRODUCED",
  "ROLLBACK_STARTED",
  "ROLLBACK_COMPLETED",
  "FINDING_WAIVED",
];

export function AuditScreen() {
  const { estate } = useApp();
  const gate = useGate("READ_AUDIT");
  const [action, setAction] = useState("");
  const [runId, setRunId] = useState("");
  const [realOnly, setRealOnly] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);

  const page = useAsync(
    () => api.audit({ action, run_id: runId, real_changes_only: realOnly, limit: 2000 }),
    [action, runId, realOnly],
  );

  const columns: Column<AuditRecord>[] = [
    {
      key: "seq",
      header: "Seq",
      width: 56,
      align: "right",
      render: (row) => <span className="mono">{row.sequence}</span>,
    },
    { key: "at", header: "When", width: 160, render: (row) => new Date(row.at).toLocaleTimeString() },
    { key: "action", header: "Action", width: 180, render: (row) => row.action },
    { key: "actor", header: "Actor", width: 210, render: (row) => <span className="mono faint">{row.actor}</span> },
    {
      key: "object",
      header: "Object",
      width: "auto",
      render: (row) => (
        <span className="mono faint">{row.canonical_id ?? row.detail ?? "—"}</span>
      ),
    },
    {
      key: "real",
      header: "Real",
      width: 76,
      render: (row) => (row.dry_run ? <Pill kind="muted">dry</Pill> : <Pill kind="good">yes</Pill>),
    },
  ];

  const record = page.data?.records.find((row) => row.sequence === selected);

  return (
    <>
      <PageHead title="Audit">
        Every write, every skip, every refusal, in one append-only chain. Each record carries the hash
        of the one before it, so tampering shows up as a broken link rather than as nothing at all.
      </PageHead>

      <Panel
        title="Chain integrity"
        actions={
          <ActionButton
            label="Verify the whole chain"
            run={() => api.verifyAudit()}
            disabled={!gate.allowed}
            reason={gate.reason}
          />
        }
      >
        {page.error ? (
          <ApiProblem error={page.error} />
        ) : page.data ? (
          <div className="stack">
            <div className="stats" style={{ borderRadius: 4 }}>
              <Stat label="Records in chain" value={page.data.chain_length} />
              <Stat label="Matching this filter" value={page.data.total} />
              <Stat
                label="Real changes"
                value={page.data.records.filter((row) => !row.dry_run).length}
              />
            </div>
            <dl className="kv">
              <dt>Head hash</dt>
              <dd className="mono wrap-anywhere">{page.data.head_hash}</dd>
            </dl>
            {page.data.chain_length === 0 && (
              <Callout tone="info" title="Nothing on the chain yet">
                Records appear as soon as anything is dry-run or applied.
              </Callout>
            )}
          </div>
        ) : null}
      </Panel>

      <Panel
        title="Records"
        flush
        actions={
          <div className="actions">
            <select value={action} onChange={(event) => setAction(event.target.value)}>
              <option value="">All actions</option>
              {ACTIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <select value={runId} onChange={(event) => setRunId(event.target.value)}>
              <option value="">All runs</option>
              {(estate?.run_ids ?? []).map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <label className="row" style={{ gap: 5, fontSize: 11 }}>
              <input
                type="checkbox"
                checked={realOnly}
                onChange={(event) => setRealOnly(event.target.checked)}
              />
              <span className="faint">Real changes only</span>
            </label>
          </div>
        }
      >
        <VirtualTable
          rows={page.data?.records ?? []}
          columns={columns}
          rowKey={(row) => String(row.sequence)}
          selectedKey={selected === null ? null : String(selected)}
          onSelect={(row) => setSelected(row.sequence)}
          height={400}
          empty={<Empty title="No records match" />}
        />
      </Panel>

      {record && (
        <Panel
          title={`Record ${record.sequence} — ${record.action}`}
          note={record.dry_run ? "dry run" : "real change"}
        >
          <div className="stack">
            <dl className="kv">
              <dt>At</dt>
              <dd>{new Date(record.at).toISOString()}</dd>
              <dt>Actor</dt>
              <dd className="mono">{record.actor}</dd>
              <dt>Run</dt>
              <dd className="mono">{record.run_id ?? "—"}</dd>
              <dt>Correlation</dt>
              <dd className="mono">{record.correlation_id ?? "—"}</dd>
              <dt>Object</dt>
              <dd className="mono">{record.canonical_id ?? "—"}</dd>
              <dt>Target key</dt>
              <dd className="mono">{record.target_native_key ?? "—"}</dd>
              <dt>Raw SQL used</dt>
              <dd>{record.raw_sql_used ? <Pill kind="warn">yes</Pill> : "no"}</dd>
              <dt>Previous hash</dt>
              <dd className="mono wrap-anywhere faint">{record.previous_hash}</dd>
              <dt>This hash</dt>
              <dd className="mono wrap-anywhere">{record.record_hash}</dd>
            </dl>

            {record.detail && <div className="muted">{record.detail}</div>}

            <div className="split">
              <div>
                <h3 style={{ marginBottom: 6 }}>Before</h3>
                {record.before == null ? (
                  <span className="faint">
                    Explicitly null — a creation has no prior state, and the field is present so the
                    difference between "nothing was there" and "we did not record it" stays visible.
                  </span>
                ) : (
                  <Json value={record.before} />
                )}
              </div>
              <div>
                <h3 style={{ marginBottom: 6 }}>After</h3>
                {record.after == null ? <span className="faint">None.</span> : <Json value={record.after} />}
              </div>
            </div>
          </div>
        </Panel>
      )}

      {estate && estate.run_ids.length > 0 && <EvidencePack runId={estate.run_ids[estate.run_ids.length - 1]!} />}
    </>
  );
}

function EvidencePack({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  const pack = useAsync(() => (open ? api.evidence(runId) : Promise.resolve(undefined)), [runId, open]);

  return (
    <Panel
      title="Evidence pack"
      note={runId}
      actions={
        <button className="btn tiny" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Assemble"}
        </button>
      }
    >
      {!open ? (
        <span className="muted">
          Everything an auditor needs for one run: the records, the chain state, and whether the chain
          verified at the moment the pack was made.
        </span>
      ) : pack.error ? (
        <ApiProblem error={pack.error} />
      ) : pack.data ? (
        <Json value={pack.data} />
      ) : (
        <span className="spinner" />
      )}
    </Panel>
  );
}
