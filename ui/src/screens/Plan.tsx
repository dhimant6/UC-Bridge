/**
 * Screen 5 — Plan and dry run.
 *
 * The dry run is the artefact an approver actually signs, so this screen shows
 * the exact vendor call each operation would make and the current target state
 * beside the proposed one. A receipt is bound to a plan digest: rebuild the plan
 * and the receipt is gone, because approving a diff you did not see is the thing
 * this is designed to prevent.
 */

import { useState } from "react";
import { api } from "../api";
import type { OperationPreview, WriteOperation } from "../api";
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

export function PlanScreen() {
  const { estateId, refresh } = useApp();
  const build = useGate("BUILD_PLAN");
  const dry = useGate("RUN_DRY_RUN");
  const plan = useAsync(() => api.plan(estateId), [estateId]);
  const receipt = useAsync(() => api.receipt(estateId), [estateId]);
  const [selected, setSelected] = useState<string | null>(null);

  const operations: Column<WriteOperation>[] = [
    { key: "verb", header: "Verb", width: 90, render: (row) => <Pill kind="plain">{row.verb}</Pill> },
    { key: "kind", header: "Kind", width: 150, render: (row) => row.entity_kind },
    {
      key: "id",
      header: "Canonical id",
      width: "auto",
      render: (row) => <span className="mono faint">{row.canonical_id}</span>,
    },
    {
      key: "idem",
      header: "Idempotency key",
      width: 240,
      render: (row) => <span className="mono">{row.idempotency_key}</span>,
    },
    { key: "fidelity", header: "Fidelity", width: 110, render: (row) => <Pill kind={row.fidelity} /> },
    {
      key: "deps",
      header: "Depends on",
      width: 100,
      align: "right",
      render: (row) => (row.depends_on.length > 0 ? row.depends_on.length : <span className="faint">—</span>),
    },
  ];

  const previews: Column<OperationPreview>[] = [
    {
      key: "change",
      header: "Change",
      width: 100,
      render: (row) => (row.would_change ? <Pill kind="info">write</Pill> : <Pill kind="good">no-op</Pill>),
    },
    { key: "call", header: "Vendor call", width: "auto", render: (row) => <span className="mono">{row.api_call}</span> },
    {
      key: "key",
      header: "Target key",
      width: 220,
      render: (row) => <span className="mono faint">{row.target_native_key ?? "—"}</span>,
    },
    {
      key: "warn",
      header: "Warnings",
      width: 100,
      align: "right",
      render: (row) =>
        row.warnings.length > 0 ? (
          <span style={{ color: "var(--degraded)" }}>{row.warnings.length}</span>
        ) : (
          <span className="faint">—</span>
        ),
    },
  ];

  const chosen = receipt.data?.previews.find((preview) => preview.op_id === selected);

  return (
    <>
      <PageHead title="Plan & dry run">
        Operations are ordered by their real dependencies, and every canonical reference is resolved
        into the target's own natural key at plan time. References that cannot be carried are
        reported here rather than written as dangling pointers.
      </PageHead>

      <Panel
        title="Build the plan"
        note={plan.data ? `${plan.data.operation_count} operations` : undefined}
        actions={
          <div className="actions">
            <ActionButton
              label={plan.data ? "Rebuild plan" : "Build plan"}
              variant="plain"
              run={() => api.buildPlan(estateId, null)}
              onDone={() => {
                plan.reload();
                receipt.reload();
                refresh();
              }}
              disabled={!build.allowed}
              reason={build.reason}
            />
            <ActionButton
              label="Run dry run"
              run={() => api.dryRun(estateId)}
              onDone={() => {
                receipt.reload();
                refresh();
              }}
              disabled={!dry.allowed || !plan.data}
              reason={!plan.data ? "Build a plan first." : dry.reason}
            />
          </div>
        }
      >
        {plan.error ? (
          <ApiProblem error={plan.error} />
        ) : plan.data ? (
          <div className="stack">
            <div className="stats" style={{ borderRadius: 4 }}>
              <Stat label="Operations" value={plan.data.operation_count} />
              <Stat
                label="Unresolved refs"
                value={plan.data.unresolved_references.length}
                tone={plan.data.is_fully_resolved ? "good" : "warn"}
              />
              <Stat
                label="Unmappable"
                value={plan.data.unmappable_operations.length}
                tone={plan.data.unmappable_operations.length > 0 ? "bad" : "good"}
                hint="Never written"
              />
              <Stat
                label="Emergency sites"
                value={plan.data.emergency_sites.length}
                hint="Each needs confirming"
              />
            </div>

            <dl className="kv">
              <dt>Plan id</dt>
              <dd className="mono">{plan.data.plan.plan_id}</dd>
              <dt>Plan digest</dt>
              <dd className="mono wrap-anywhere">{plan.data.plan_digest}</dd>
              <dt>Target estate</dt>
              <dd className="mono">{plan.data.plan.estate_id}</dd>
            </dl>

            {plan.data.unresolved_references.length > 0 && (
              <Callout tone="info" title={`${plan.data.unresolved_references.length} reference(s) could not be carried`}>
                <table style={{ marginTop: 6 }}>
                  <thead>
                    <tr>
                      <th>Object</th>
                      <th>Field</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.data.unresolved_references.map((reference, index) => (
                      <tr key={index}>
                        <td className="mono faint">{reference.canonical_id}</td>
                        <td className="mono">{reference.field}</td>
                        <td>{reference.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Callout>
            )}

            {plan.data.skipped_unmappable.length > 0 && (
              <Callout tone="refused" title={`${plan.data.skipped_unmappable.length} object(s) excluded as UNMAPPABLE`}>
                Rejected by the planner and again by the connector base. They appear in the manual
                work section of the assessment instead of being half-written.
              </Callout>
            )}
          </div>
        ) : null}
      </Panel>

      {plan.data && (
        <Panel title="Operations" note="In dependency order" flush>
          <VirtualTable
            rows={plan.data.plan.operations}
            columns={operations}
            rowKey={(row) => row.op_id}
            selectedKey={selected}
            onSelect={(row) => setSelected(row.op_id)}
            height={320}
            empty={<Empty title="This plan has no operations" />}
          />
        </Panel>
      )}

      <Panel
        title="Dry-run receipt"
        note={
          receipt.data
            ? `${receipt.data.would_change_count} would change · ${receipt.data.no_change_count} already correct`
            : undefined
        }
        flush
      >
        {receipt.error ? (
          <div style={{ padding: 13 }}>
            <ApiProblem error={receipt.error} />
          </div>
        ) : receipt.data ? (
          <>
            <div style={{ padding: 13, borderBottom: "1px solid var(--border)" }}>
              <dl className="kv">
                <dt>Receipt</dt>
                <dd className="mono">{receipt.data.receipt_id}</dd>
                <dt>Covers plan digest</dt>
                <dd className="mono wrap-anywhere">
                  {receipt.data.plan_digest}
                  {plan.data && plan.data.plan_digest !== receipt.data.plan_digest && (
                    <span style={{ marginLeft: 8 }}>
                      <Pill kind="bad">stale</Pill>
                    </span>
                  )}
                </dd>
                <dt>Connector</dt>
                <dd className="mono">{receipt.data.connector_id}</dd>
              </dl>
              {receipt.data.would_change_count === 0 && receipt.data.previews.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <Callout tone="ok" title="This plan is already applied">
                    Every operation reports no change. Re-running it writes nothing — that is the
                    idempotency guarantee, observed rather than asserted.
                  </Callout>
                </div>
              )}
            </div>
            <VirtualTable
              rows={receipt.data.previews}
              columns={previews}
              rowKey={(row) => row.op_id}
              selectedKey={selected}
              onSelect={(row) => setSelected(row.op_id)}
              height={300}
              empty={<Empty title="No previews" />}
            />
          </>
        ) : (
          <div style={{ padding: 13 }}>
            <Empty title="No dry run yet">Build a plan, then dry-run it.</Empty>
          </div>
        )}
      </Panel>

      {chosen && (
        <Panel title={`Operation ${chosen.op_id}`} note={chosen.api_call}>
          {chosen.warnings.length > 0 && (
            <Callout tone="refused" title="Warnings on this operation">
              {chosen.warnings.map((warning) => (
                <div key={warning}>{warning}</div>
              ))}
            </Callout>
          )}
          <div className="split">
            <div>
              <h3 style={{ marginBottom: 6 }}>Current target state</h3>
              {chosen.current_target_state ? (
                <Json value={chosen.current_target_state} />
              ) : (
                <span className="faint">Nothing there yet — this would create it.</span>
              )}
            </div>
            <div>
              <h3 style={{ marginBottom: 6 }}>Proposed state</h3>
              <Json value={chosen.proposed_state} />
            </div>
          </div>
        </Panel>
      )}
    </>
  );
}
