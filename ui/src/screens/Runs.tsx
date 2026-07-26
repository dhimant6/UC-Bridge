/**
 * Screen 6 — Runs.
 *
 * The authorization form is the interesting part. It is not validation theatre:
 * the fields are sent as they are and the library's model validator decides. Set
 * one approver and you get ApprovalRequired; move the window into the past and
 * you get ChangeWindowClosed; clear the emergency confirmations and you get
 * EmergencyConfirmationRequired. Each refusal is rendered as a result, because
 * each one is the product working.
 */

import { useState } from "react";
import { api } from "../api";
import type { AuthorizationForm, RunRecordRow } from "../api";
import { PageHead } from "../components/Layout";
import {
  ActionButton,
  ApiProblem,
  Bar,
  Callout,
  Empty,
  Panel,
  Pill,
  Stat,
  useAsync,
} from "../components/Bits";
import { useApp, useGate } from "../state";

/**
 * A value for `<input type="datetime-local">`, in *local* time.
 *
 * `toISOString()` would be wrong here: the input has no timezone and the browser
 * reads whatever it is given as local, so a UTC string makes the default window
 * appear shifted by the operator's offset — closed before they touch anything in
 * any timezone east of UTC. Submitting goes back through `new Date(value)`,
 * which reinterprets local correctly and yields the right instant.
 */
function isoLocal(offsetHours: number): string {
  const when = new Date(Date.now() + offsetHours * 3600_000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}` +
    `T${pad(when.getHours())}:${pad(when.getMinutes())}`
  );
}

export function RunsScreen() {
  const { estateId, estate, refresh } = useApp();
  const execute = useGate("EXECUTE_PRODUCTION");
  const rollbackGate = useGate("ROLLBACK");
  const runs = useAsync(() => api.runs(), [estateId]);
  const plan = useAsync(() => api.plan(estateId).catch(() => undefined), [estateId]);

  const [approvers, setApprovers] = useState("planner@contoso.example, approver@contoso.example");
  const [requestedBy, setRequestedBy] = useState("operator@contoso.example");
  const [windowStart, setWindowStart] = useState(isoLocal(-1));
  const [windowEnd, setWindowEnd] = useState(isoLocal(2));
  const [changeRef, setChangeRef] = useState("CHG0042311");
  const [overrideReason, setOverrideReason] = useState("");
  const [confirmEmergency, setConfirmEmergency] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);

  const emergencySites = plan.data?.emergency_sites ?? [];

  const form = (): AuthorizationForm => ({
    requested_by: requestedBy,
    approvers: approvers
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean),
    correlation_id: `corr-${Date.now()}`,
    window_start: new Date(windowStart).toISOString(),
    window_end: new Date(windowEnd).toISOString(),
    change_reference: changeRef || null,
    window_override_reason: overrideReason || null,
    window_override_by: overrideReason ? "cab.chair@contoso.example" : null,
    confirmed_sites: confirmEmergency ? null : [],
  });

  const estateRuns = runs.data?.filter((run) => run.estate_id === estateId) ?? [];

  return (
    <>
      <PageHead title="Runs">
        A production write needs a dry-run receipt for this exact plan, two distinct approvers, an
        open change window or an attributed override, and per-site confirmation of every emergency
        location. There is no bypass parameter, because a bypass parameter is how bypasses reach
        production.
      </PageHead>

      {estate?.target_may_write_to_production === false && (
        <Callout tone="refused" title={`${estate.target_connector_id} is ${estate.target_readiness}`}>
          You can fill this form in correctly and the write will still be refused. The readiness gate
          checks API-surface verification and cassette provenance before anything else, and this
          connector's cassettes are hand-authored rather than captured from a real system. Clearing
          it means capturing real cassettes from a lab and re-checking every API signature.
        </Callout>
      )}

      <Panel title="Authorization" note="Evidence, not a checkbox">
        <div className="stack">
          <div className="grid three">
            <label className="field">
              <span>Requested by (operator)</span>
              <input value={requestedBy} onChange={(event) => setRequestedBy(event.target.value)} />
            </label>
            <label className="field">
              <span>Approvers (comma separated)</span>
              <input value={approvers} onChange={(event) => setApprovers(event.target.value)} />
            </label>
            <label className="field">
              <span>Change reference</span>
              <input value={changeRef} onChange={(event) => setChangeRef(event.target.value)} />
            </label>
            <label className="field">
              <span>Window opens</span>
              <input
                type="datetime-local"
                value={windowStart}
                onChange={(event) => setWindowStart(event.target.value)}
              />
            </label>
            <label className="field">
              <span>Window closes</span>
              <input
                type="datetime-local"
                value={windowEnd}
                onChange={(event) => setWindowEnd(event.target.value)}
              />
            </label>
            <label className="field">
              <span>Override reason (if outside the window)</span>
              <input
                value={overrideReason}
                onChange={(event) => setOverrideReason(event.target.value)}
                placeholder="Attributed to the CAB chair"
              />
            </label>
          </div>

          <div className="row">
            <label className="row" style={{ gap: 6, fontSize: 12 }}>
              <input
                type="checkbox"
                checked={confirmEmergency}
                onChange={(event) => setConfirmEmergency(event.target.checked)}
              />
              <span>
                Confirm emergency calling for {emergencySites.length || "all"} site
                {emergencySites.length === 1 ? "" : "s"}
                {emergencySites.length > 0 && (
                  <span className="mono faint"> ({emergencySites.join(", ")})</span>
                )}
              </span>
            </label>
          </div>

          <div className="row" style={{ gap: 6 }}>
            {approvers.split(",").filter((name) => name.trim()).length < 2 && (
              <Pill kind="warn">one approver → ApprovalRequired</Pill>
            )}
            {new Date(windowEnd) < new Date() && !overrideReason && (
              <Pill kind="warn">closed window → ChangeWindowClosed</Pill>
            )}
            {!confirmEmergency && emergencySites.length > 0 && (
              <Pill kind="warn">unconfirmed sites → EmergencyConfirmationRequired</Pill>
            )}
          </div>

          <div className="actions">
            <ActionButton
              label="Execute in production"
              run={() => api.execute(estateId, form())}
              onDone={() => {
                runs.reload();
                refresh();
              }}
              disabled={!execute.allowed}
              reason={execute.reason}
            />
            {!execute.allowed && (
              <span className="faint" style={{ fontSize: 11 }}>
                {execute.reason}
              </span>
            )}
          </div>
        </div>
      </Panel>

      <Panel title="Runs" note={`${estateRuns.length} for this estate`} flush>
        {runs.error ? (
          <div style={{ padding: 13 }}>
            <ApiProblem error={runs.error} />
          </div>
        ) : estateRuns.length === 0 ? (
          <Empty title="No runs yet">Nothing has been applied to this target.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>State</th>
                <th>Mode</th>
                <th>Progress</th>
                <th>Counts</th>
                <th>Started</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {estateRuns.map((run) => (
                <tr
                  key={run.run_id}
                  className="clickable"
                  onClick={() => setSelected(selected === run.run_id ? null : run.run_id)}
                >
                  <td className="mono">{run.run_id}</td>
                  <td>
                    <Pill kind={run.state} />
                  </td>
                  <td className="faint">{run.mode}</td>
                  <td style={{ minWidth: 120 }}>
                    <RunProgress run={run} />
                  </td>
                  <td>
                    <span className="row" style={{ gap: 4 }}>
                      {Object.entries(run.counts).map(([status, count]) => (
                        <span key={status} className="pill plain">
                          {status} {count}
                        </span>
                      ))}
                    </span>
                  </td>
                  <td className="faint nowrap">{new Date(run.started_at).toLocaleString()}</td>
                  <td className="right">
                    {run.has_rollback_bundle && (
                      <ActionButton
                        label="Roll back"
                        variant="danger"
                        run={() => api.rollback(estateId, run.run_id, form())}
                        onDone={() => {
                          runs.reload();
                          refresh();
                        }}
                        disabled={!rollbackGate.allowed}
                        reason={rollbackGate.reason}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {selected && <RunDetail runId={selected} />}
    </>
  );
}

function RunProgress({ run }: { run: RunRecordRow }) {
  const done = run.completed_op_ids.length;
  const total = run.total_operations || 1;
  const failed = run.state === "FAILED";
  return (
    <span className="row" style={{ gap: 6 }}>
      <span className="mono faint" style={{ width: 52 }}>
        {done}/{run.total_operations}
      </span>
      <span style={{ flex: 1 }}>
        <Bar value={done / total} tone={failed ? "bad" : done === run.total_operations ? "good" : undefined} />
      </span>
    </span>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const detail = useAsync(() => api.run(runId), [runId]);
  if (detail.error) return <ApiProblem error={detail.error} />;
  if (!detail.data) return null;
  const run = detail.data;

  return (
    <Panel title={`Run ${run.run_id}`} note={run.state}>
      <div className="stack">
        <div className="stats" style={{ borderRadius: 4 }}>
          <Stat label="Operations" value={run.total_operations} />
          <Stat label="Completed" value={run.completed_op_ids.length} />
          <Stat label="Audit records" value={run.audit.length} />
          <Stat
            label="Rollback bundle"
            value={run.has_rollback_bundle ? "yes" : "no"}
            tone={run.has_rollback_bundle ? "good" : "warn"}
          />
        </div>

        {run.failure_reason && (
          <Callout tone="error" title="Failure reason">
            {run.failure_reason}
          </Callout>
        )}

        {run.checkpoint_op_id && (
          <Callout tone="info" title="Checkpoint">
            Last durable checkpoint at <span className="mono">{run.checkpoint_op_id}</span>. A resumed
            run continues from here rather than starting over.
          </Callout>
        )}

        {!run.has_rollback_bundle && run.state === "COMPLETED" && (
          <Callout tone="refused" title="Known limitation: a crashed run's bundle is lost">
            A rollback bundle is assembled by the run that performs the writes, so operations
            completed before a crash are not covered by the resumed run's bundle. Persisting it
            incrementally alongside the checkpoint is the fix, and it has not been made yet.
          </Callout>
        )}

        <div>
          <h3 style={{ marginBottom: 6 }}>Audit trail for this run</h3>
          <div className="table-scroll" style={{ maxHeight: 320 }}>
            <table>
              <thead>
                <tr>
                  <th className="num">Seq</th>
                  <th>Action</th>
                  <th>Object</th>
                  <th>Target key</th>
                  <th>Real</th>
                </tr>
              </thead>
              <tbody>
                {run.audit.map((record) => (
                  <tr key={record.sequence}>
                    <td className="num mono">{record.sequence}</td>
                    <td>{record.action}</td>
                    <td className="mono faint">{record.canonical_id ?? record.detail ?? "—"}</td>
                    <td className="mono faint">{record.target_native_key ?? "—"}</td>
                    <td>{record.dry_run ? <Pill kind="muted">dry</Pill> : <Pill kind="good">yes</Pill>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </Panel>
  );
}
