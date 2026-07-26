/**
 * Screen 4 — Waves.
 *
 * The screen exists for one failure mode: splitting a shared line, a hunt group,
 * or a boss-admin delegation across two waves. Both halves still "work" in
 * isolation, and the phone stops ringing for someone in the gap. Violations are
 * shown with their consequence, in words, not as a validation code.
 */

import { useState } from "react";
import { api } from "../api";
import { PageHead } from "../components/Layout";
import {
  ActionButton,
  ApiProblem,
  Callout,
  Document,
  Empty,
  Panel,
  Pill,
  Stat,
  useAsync,
} from "../components/Bits";
import { useApp, useGate } from "../state";

const STRATEGIES = ["SITE", "DEPARTMENT", "COMPLEXITY", "PILOT_FIRST", "SINGLE_WAVE"];

export function WavesScreen() {
  const { estateId, refresh } = useApp();
  const gate = useGate("BUILD_PLAN");
  const waves = useAsync(() => api.waves(estateId), [estateId]);
  const [strategy, setStrategy] = useState("SITE");
  const [maxSize, setMaxSize] = useState<string>("");
  const [openRunbook, setOpenRunbook] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState<{ user: string; wave: string } | null>(null);

  const plan = waves.data?.plan;

  return (
    <>
      <PageHead title="Waves">
        Users are grouped into cutover waves, then the grouping is checked against the dependency
        clusters discovery found. A plan that splits a cluster is rejected rather than warned about.
      </PageHead>

      <Panel
        title="Plan the waves"
        note={waves.data?.summary}
        actions={
          <div className="actions">
            <label className="field">
              <span>Strategy</span>
              <select value={strategy} onChange={(event) => setStrategy(event.target.value)}>
                {STRATEGIES.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Max wave size</span>
              <input
                type="number"
                min={1}
                placeholder="unlimited"
                value={maxSize}
                onChange={(event) => setMaxSize(event.target.value)}
                style={{ width: 96 }}
              />
            </label>
            <ActionButton
              label={plan ? "Re-plan" : "Plan waves"}
              run={() => api.buildWaves(estateId, strategy, maxSize ? Number(maxSize) : null)}
              onDone={() => {
                waves.reload();
                refresh();
              }}
              disabled={!gate.allowed}
              reason={gate.reason}
            />
          </div>
        }
      >
        {waves.error ? (
          <ApiProblem error={waves.error} />
        ) : plan ? (
          <div className="stack">
            <div className="stats" style={{ borderRadius: 4 }}>
              <Stat label="Waves" value={plan.waves.length} />
              <Stat
                label="Users assigned"
                value={plan.waves.reduce((total, wave) => total + wave.user_keys.length, 0)}
              />
              <Stat
                label="Dependency clusters"
                value={plan.clusters.length}
                hint="Must not be split"
              />
              <Stat
                label="Violations"
                value={plan.violations.length}
                tone={plan.violations.length === 0 ? "good" : "bad"}
              />
              <Stat
                label="Unassigned"
                value={plan.unassigned_user_keys.length}
                tone={plan.unassigned_user_keys.length > 0 ? "warn" : undefined}
              />
            </div>

            {waves.data?.is_valid ? (
              <Callout tone="ok" title="Dependency integrity holds">
                No cluster is split across waves. Every shared line, hunt group, and delegation moves
                as one unit.
              </Callout>
            ) : (
              <Callout tone="refused" title={`${plan.violations.length} dependency violation(s)`}>
                This plan cannot be executed as it stands.
              </Callout>
            )}
          </div>
        ) : null}
      </Panel>

      {plan && plan.violations.length > 0 && (
        <Panel title="Violations" note="What actually breaks, in words" flush>
          <table>
            <thead>
              <tr>
                <th>Cluster</th>
                <th>Kind</th>
                <th>Split across</th>
                <th>Consequence</th>
              </tr>
            </thead>
            <tbody>
              {plan.violations.map((violation) => (
                <tr key={violation.cluster_id}>
                  <td className="mono">{violation.cluster_id}</td>
                  <td>
                    <Pill kind="HIGH">{violation.kind}</Pill>
                  </td>
                  <td className="mono faint">{Object.keys(violation.split_across).join(", ")}</td>
                  <td>{violation.consequence}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      {plan && (
        <div className="grid two">
          {plan.waves.map((wave) => (
            <Panel
              key={wave.wave_id}
              title={`${wave.sequence}. ${wave.name}`}
              note={`${wave.user_keys.length} users`}
              actions={
                <button
                  className="btn tiny"
                  onClick={() => setOpenRunbook(openRunbook === wave.wave_id ? null : wave.wave_id)}
                >
                  {openRunbook === wave.wave_id ? "Hide runbook" : "Runbook"}
                </button>
              }
            >
              <div className="stack">
                {wave.notes && <span className="muted">{wave.notes}</span>}
                <div className="row" style={{ gap: 5 }}>
                  {wave.user_keys.slice(0, 40).map((user) => (
                    <button
                      key={user}
                      className="pill plain mono"
                      style={{ cursor: "pointer" }}
                      onClick={() => setMoveTarget({ user, wave: wave.wave_id })}
                      title="Move to another wave"
                    >
                      {user}
                    </button>
                  ))}
                  {wave.user_keys.length > 40 && (
                    <span className="faint">+{wave.user_keys.length - 40} more</span>
                  )}
                  {wave.user_keys.length === 0 && <span className="faint">Empty wave.</span>}
                </div>

                {moveTarget?.wave === wave.wave_id && (
                  <div className="actions">
                    <span className="mono">{moveTarget.user}</span>
                    <span className="faint">→</span>
                    <select
                      onChange={(event) => {
                        const to = event.target.value;
                        if (!to) return;
                        void api
                          .moveUser(estateId, moveTarget.user, to)
                          .then(() => {
                            setMoveTarget(null);
                            waves.reload();
                          })
                          .catch(() => waves.reload());
                      }}
                      defaultValue=""
                    >
                      <option value="">Choose wave…</option>
                      {plan.waves
                        .filter((other) => other.wave_id !== wave.wave_id)
                        .map((other) => (
                          <option key={other.wave_id} value={other.wave_id}>
                            {other.name}
                          </option>
                        ))}
                    </select>
                    <button className="btn tiny" onClick={() => setMoveTarget(null)}>
                      Cancel
                    </button>
                  </div>
                )}

                {openRunbook === wave.wave_id && <Runbook estateId={estateId} waveId={wave.wave_id} />}
              </div>
            </Panel>
          ))}
        </div>
      )}

      {plan && plan.clusters.length > 0 && (
        <Panel title="Dependency clusters" note="Discovered, not configured" flush>
          <table>
            <thead>
              <tr>
                <th>Cluster</th>
                <th>Kind</th>
                <th>Members</th>
                <th>Why they move together</th>
              </tr>
            </thead>
            <tbody>
              {plan.clusters.map((cluster) => (
                <tr key={cluster.cluster_id}>
                  <td className="mono">{cluster.cluster_id}</td>
                  <td>{cluster.kind}</td>
                  <td className="mono faint">{cluster.user_keys.join(", ")}</td>
                  <td className="muted">{cluster.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      {waves.data && waves.data.coexistence.length > 0 && (
        <Panel title="Coexistence during the migration" note="What must keep working mid-flight" flush>
          <table>
            <thead>
              <tr>
                <th>Wave</th>
                <th className="num">Migrated</th>
                <th className="num">Remaining</th>
                <th>Requirement</th>
              </tr>
            </thead>
            <tbody>
              {waves.data.coexistence.map((requirement) => (
                <tr key={requirement.wave_id}>
                  <td className="mono">{requirement.wave_id}</td>
                  <td className="num">{requirement.migrated_user_count}</td>
                  <td className="num">{requirement.remaining_user_count}</td>
                  <td>{requirement.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      {!plan && !waves.loading && !waves.error && <Empty title="No wave plan yet" />}
    </>
  );
}

function Runbook({ estateId, waveId }: { estateId: string; waveId: string }) {
  const runbook = useAsync(() => api.runbook(estateId, waveId), [estateId, waveId]);
  if (runbook.error) return <ApiProblem error={runbook.error} />;
  if (!runbook.data) return <span className="spinner" />;
  return <Document text={runbook.data} />;
}
