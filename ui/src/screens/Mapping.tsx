/**
 * Screen 3 — Mapping.
 *
 * Three things that are easy to conflate and must not be: the rules that derive
 * missing source attributes, the number plan that mints E.164 from extensions,
 * and the auto-mapper that guesses which target object an existing source
 * object corresponds to. The last one is a suggestion with a confidence, never
 * a decision.
 */

import { api } from "../api";
import type { MappingCandidate } from "../api";
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
import { VirtualTable } from "../components/VirtualTable";
import type { Column } from "../components/VirtualTable";
import { useApp, useGate } from "../state";

export function MappingScreen() {
  const { estateId, refresh } = useApp();
  const gate = useGate("EDIT_MAPPING");
  const mapping = useAsync(() => api.mapping(estateId), [estateId]);

  const candidates: Column<MappingCandidate>[] = [
    { key: "source", header: "Source object", width: "auto", render: (row) => row.source_label },
    {
      key: "target",
      header: "Best target match",
      width: "auto",
      render: (row) => row.target_label ?? <span className="faint">no candidate</span>,
    },
    {
      key: "confidence",
      header: "Confidence",
      width: 130,
      render: (row) => (
        <span className="row" style={{ gap: 6 }}>
          <span style={{ width: 46 }} className="mono">
            {(row.confidence * 100).toFixed(0)}%
          </span>
          <span style={{ flex: 1 }}>
            <Bar
              value={row.confidence}
              tone={row.decision === "AUTO" ? "good" : row.confidence < 0.4 ? "bad" : undefined}
            />
          </span>
        </span>
      ),
    },
    { key: "decision", header: "Decision", width: 90, render: (row) => <Pill kind={row.decision} /> },
    {
      key: "why",
      header: "Signals",
      width: "auto",
      render: (row) => (
        <span className="faint">{row.signals.map((signal) => signal.name).join(", ") || "—"}</span>
      ),
    },
  ];

  return (
    <>
      <PageHead title="Mapping">
        Rules fill in what the source does not have, the number plan derives E.164 from internal
        extensions, and neither claims to be lossless just because it succeeded. A minted number is
        DEGRADED until someone assesses it.
      </PageHead>

      <Panel
        title="Transform"
        actions={
          <ActionButton
            label={mapping.data?.transform ? "Re-apply profile" : "Apply mapping profile"}
            run={() => api.map(estateId)}
            onDone={() => {
              mapping.reload();
              refresh();
            }}
            disabled={!gate.allowed}
            reason={gate.reason}
          />
        }
      >
        {mapping.error ? (
          <ApiProblem error={mapping.error} />
        ) : mapping.loading ? (
          <span className="spinner" />
        ) : !mapping.data?.has_profile ? (
          <Callout tone="info" title="This estate has no mapping profile">
            Source and target are the same platform here, so there is nothing to transform. The
            snapshot goes to the planner as it came out of discovery.
          </Callout>
        ) : mapping.data.transform ? (
          <div className="stack">
            <div className="stats" style={{ borderRadius: 4 }}>
              <Stat label="Entities after transform" value={mapping.data.transform.entity_count} />
              <Stat
                label="Numbers minted"
                value={mapping.data.transform.numbers_created}
                hint="Derived, so DEGRADED"
              />
              <Stat
                label="Issues"
                value={
                  mapping.data.transform.issues.length +
                  mapping.data.transform.overlaps.length +
                  mapping.data.transform.collisions.length
                }
                tone={mapping.data.transform.is_clean ? "good" : "warn"}
                hint={mapping.data.transform.is_clean ? "clean" : "planner refuses"}
              />
              <Stat
                label="Lossless"
                value={sumFidelity(mapping.data.transform.fidelity_by_kind, "LOSSLESS")}
                tone="good"
              />
              <Stat
                label="Degraded"
                value={sumFidelity(mapping.data.transform.fidelity_by_kind, "DEGRADED")}
                tone="warn"
              />
              <Stat
                label="Unmappable"
                value={sumFidelity(mapping.data.transform.fidelity_by_kind, "UNMAPPABLE")}
                tone="bad"
              />
            </div>

            {Object.keys(mapping.data.transform.rules_fired).length > 0 && (
              <div>
                <h3 style={{ marginBottom: 5 }}>Rules that fired</h3>
                <div className="row" style={{ gap: 5 }}>
                  {Object.entries(mapping.data.transform.rules_fired).map(([rule, count]) => (
                    <span key={rule} className="pill plain mono">
                      {rule} × {count}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {mapping.data.transform.collisions.length > 0 && (
              <Callout tone="refused" title="Two extensions normalise to the same E.164">
                {mapping.data.transform.collisions.map((collision) => (
                  <div key={collision.e164}>
                    <span className="mono">{collision.e164}</span> ←{" "}
                    {collision.sources.join(" and ")}
                  </div>
                ))}
              </Callout>
            )}

            {mapping.data.transform.issues.length > 0 && (
              <div>
                <h3 style={{ marginBottom: 6 }}>Transform issues</h3>
                <div className="table-scroll" style={{ maxHeight: 260 }}>
                  <table>
                    <thead>
                      <tr>
                        <th>Kind</th>
                        <th>Attribute</th>
                        <th>Problem</th>
                        <th>Detail</th>
                      </tr>
                    </thead>
                    <tbody>
                      {mapping.data.transform.issues.map((issue, index) => (
                        <tr key={index}>
                          <td>{issue.kind}</td>
                          <td className="mono faint">{issue.attribute ?? "—"}</td>
                          <td>{issue.problem}</td>
                          <td className="muted">{issue.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        ) : (
          <Callout tone="info" title="Profile not applied yet">
            The estate has a profile ({mapping.data.profile?.name}) but it has not been run. Apply it
            to see what it derives and what it costs in fidelity.
          </Callout>
        )}
      </Panel>

      {mapping.data?.profile && (
        <div className="grid two">
          <Panel title="Rules" note={mapping.data.profile.profile_id} flush>
            <table>
              <thead>
                <tr>
                  <th>Id</th>
                  <th>When</th>
                  <th>Then</th>
                </tr>
              </thead>
              <tbody>
                {mapping.data.profile.rules.rules.map((rule) => (
                  <tr key={rule.id}>
                    <td className="mono">{rule.id}</td>
                    <td className="mono faint">
                      {rule.when.entity ?? "any"}
                      {rule.when.pattern ? ` ~ /${rule.when.pattern}/` : ""}
                    </td>
                    <td className="mono">
                      {Object.entries(rule.then)
                        .map(([key, value]) => `${key}=${value}`)
                        .join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          {mapping.data.number_plan && (
            <Panel title="Number plan" note="Site prefix table" flush>
              <table>
                <thead>
                  <tr>
                    <th>Site</th>
                    <th>Internal pattern</th>
                    <th>E.164 prefix</th>
                  </tr>
                </thead>
                <tbody>
                  {mapping.data.number_plan.rules.map((rule) => (
                    <tr key={`${rule.site_code}:${rule.internal_pattern}`}>
                      <td>{rule.site_code}</td>
                      <td className="mono faint">
                        /{rule.internal_pattern}/
                        {rule.strip_digits > 0 && (
                          <span className="faint"> strip {rule.strip_digits}</span>
                        )}
                      </td>
                      <td className="mono">{rule.e164_prefix}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {mapping.data.number_plan.overlaps.length > 0 && (
                <div style={{ padding: 13 }}>
                  <Callout tone="refused" title="Overlapping site rules">
                    {mapping.data.number_plan.overlaps.map((overlap) => (
                      <div key={`${overlap.site_code}:${overlap.first_pattern}`}>
                        At {overlap.site_code}, /{overlap.first_pattern}/ and /
                        {overlap.second_pattern}/ both match{" "}
                        <span className="mono">{overlap.example}</span>. {overlap.detail}
                      </div>
                    ))}
                  </Callout>
                </div>
              )}
            </Panel>
          )}
        </div>
      )}

      <Panel
        title="Auto-mapping suggestions"
        note="Confidence, not a decision"
        flush
        actions={
          mapping.data ? (
            <div className="row" style={{ gap: 6 }}>
              {Object.entries(mapping.data.automap.summary).map(([decision, count]) => (
                <span key={decision} className="row" style={{ gap: 4 }}>
                  <Pill kind={decision}>{decision}</Pill>
                  <span className="mono faint">{count}</span>
                </span>
              ))}
            </div>
          ) : undefined
        }
      >
        <VirtualTable
          rows={mapping.data?.automap.candidates ?? []}
          columns={candidates}
          rowKey={(row) => row.source_id}
          height={360}
          empty={
            <Empty title="No suggestions">
              Nothing to match against — the target inventory is empty until something is written to
              it.
            </Empty>
          }
        />
      </Panel>
    </>
  );
}

function sumFidelity(report: Record<string, Record<string, number>>, level: string): number {
  return Object.values(report).reduce((total, buckets) => total + (buckets[level] ?? 0), 0);
}
