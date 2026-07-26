/**
 * Screen 9 — Connectors.
 *
 * The screen to read before planning anything: what each connector is allowed to
 * do, and how we know. An unverified API surface is *declared* rather than
 * guessed, and the readiness gate keeps the connector out of production while it
 * stays that way. Showing that plainly is more useful than a green tick nobody
 * can audit.
 */

import { useState } from "react";
import { api } from "../api";
import type { ConnectorEntry } from "../api";
import { PageHead } from "../components/Layout";
import { ApiProblem, Callout, Empty, Panel, Pill, Stat, useAsync } from "../components/Bits";

export function ConnectorsScreen() {
  const connectors = useAsync(() => api.connectors(), []);
  const [open, setOpen] = useState<string | null>(null);

  if (connectors.error) return <ApiProblem error={connectors.error} />;
  if (!connectors.data) return <Empty title="Loading connectors…" />;

  const cleared = connectors.data.filter((entry) => entry.may_write_to_production);

  return (
    <>
      <PageHead title="Connectors">
        Every connector implements the same two directions against one canonical model, so adding a
        platform is one connector and reverse migration is the same pipeline with the ends swapped.
      </PageHead>

      <Panel title="Production readiness across the build">
        <div className="stack">
          <div className="stats" style={{ borderRadius: 4 }}>
            <Stat label="Connectors" value={connectors.data.length} />
            <Stat
              label="Cleared for production"
              value={cleared.length}
              tone={cleared.length > 0 ? "good" : "warn"}
            />
            <Stat
              label="Lab only"
              value={connectors.data.filter((entry) => entry.readiness.level === "LAB_ONLY").length}
              tone="warn"
            />
            <Stat
              label="Unverified surfaces"
              value={connectors.data.reduce(
                (total, entry) => total + entry.unverified_api_surfaces.length,
                0,
              )}
              tone="warn"
            />
          </div>

          <Callout tone="refused" title="No vendor connector is cleared for production writes">
            Every vendor cassette in this build is hand-authored from vendor documentation, not
            captured from a real system. The readiness gate detects that and refuses production
            writes. Clearing a connector means capturing real cassettes from a lab system and
            re-checking every API signature — not flipping a flag.
          </Callout>
        </div>
      </Panel>

      <Panel title="Connectors" flush>
        <table>
          <thead>
            <tr>
              <th>Connector</th>
              <th>Platform</th>
              <th>Readiness</th>
              <th className="num">Extract</th>
              <th className="num">Apply</th>
              <th className="num">Unmappable</th>
              <th>Dry run</th>
              <th>Rollback</th>
              <th>Air gap</th>
            </tr>
          </thead>
          <tbody>
            {connectors.data.map((entry) => (
              <tr
                key={entry.manifest.connector_id}
                className="clickable"
                onClick={() =>
                  setOpen(open === entry.manifest.connector_id ? null : entry.manifest.connector_id)
                }
              >
                <td>
                  <div>{entry.manifest.display_name}</div>
                  <div className="mono faint" style={{ fontSize: 11 }}>
                    {entry.manifest.connector_id} v{entry.manifest.connector_version}
                  </div>
                </td>
                <td className="mono faint">{entry.manifest.platform}</td>
                <td>
                  <Pill kind={entry.readiness.level} />
                </td>
                <td className="num">{entry.extractable_kinds.length}</td>
                <td className="num">{entry.appliable_kinds.length}</td>
                <td className="num">{entry.unmappable_kinds.length}</td>
                <td>{entry.manifest.supports_dry_run ? "✓" : "—"}</td>
                <td>{entry.manifest.supports_rollback ? "✓" : "—"}</td>
                <td>{entry.manifest.air_gap_capable ? "✓" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {open && (
        <ConnectorDetail
          entry={connectors.data.find((entry) => entry.manifest.connector_id === open)!}
        />
      )}
    </>
  );
}

function ConnectorDetail({ entry }: { entry: ConnectorEntry }) {
  const { manifest, readiness } = entry;

  return (
    <>
      <Panel
        title={manifest.display_name}
        note={
          <span className="row" style={{ gap: 6 }}>
            <Pill kind={readiness.level} />
            <span className="mono faint">{manifest.connector_id}</span>
          </span>
        }
      >
        <div className="stack">
          {manifest.notes && <div className="muted">{manifest.notes}</div>}

          {readiness.notes.length > 0 && (
            <Callout
              tone={entry.may_write_to_production ? "ok" : "refused"}
              title={
                entry.may_write_to_production
                  ? "Cleared for production writes"
                  : "Not cleared for production writes"
              }
            >
              {readiness.notes.map((note) => (
                <div key={note}>{note}</div>
              ))}
            </Callout>
          )}

          {readiness.synthetic_cassettes.length > 0 && (
            <div>
              <h3 style={{ marginBottom: 5 }}>Synthetic cassettes</h3>
              <div className="row" style={{ gap: 5 }}>
                {readiness.synthetic_cassettes.map((name) => (
                  <span key={name} className="pill plain mono">
                    {name}
                  </span>
                ))}
              </div>
              <div className="faint" style={{ marginTop: 5, fontSize: 11.5 }}>
                Hand-authored from documentation. This alone keeps the connector out of production.
              </div>
            </div>
          )}

          <div>
            <h3 style={{ marginBottom: 6 }}>API surfaces</h3>
            <table>
              <thead>
                <tr>
                  <th>Surface</th>
                  <th>Version</th>
                  <th>Transport</th>
                  <th>Verified</th>
                  <th>How</th>
                </tr>
              </thead>
              <tbody>
                {manifest.api_surfaces.map((surface) => (
                  <tr key={surface.name}>
                    <td>
                      {surface.documentation_url ? (
                        <a href={surface.documentation_url} target="_blank" rel="noreferrer">
                          {surface.name}
                        </a>
                      ) : (
                        surface.name
                      )}
                      {surface.notes && (
                        <div className="faint" style={{ fontSize: 11 }}>
                          {surface.notes}
                        </div>
                      )}
                    </td>
                    <td className="mono">{surface.version ?? "—"}</td>
                    <td className="faint">{surface.transport ?? "—"}</td>
                    <td>
                      {surface.verified_at ? (
                        <span className="mono">{surface.verified_at}</span>
                      ) : (
                        <Pill kind="warn">unverified</Pill>
                      )}
                    </td>
                    <td className="muted">{surface.verification_method ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="grid three">
            <Stat
              label="Eventually consistent"
              value={manifest.eventual_consistency.is_eventually_consistent ? "yes" : "no"}
              hint={
                manifest.eventual_consistency.is_eventually_consistent
                  ? "Writes are confirmed by polling"
                  : "A write is visible immediately"
              }
            />
            <Stat
              label="Requires publisher node"
              value={manifest.requires_publisher_node ? "yes" : "no"}
            />
            <Stat label="Air-gap capable" value={manifest.air_gap_capable ? "yes" : "no"} />
          </div>
        </div>
      </Panel>

      <Panel title="Entity capabilities" note="Declared honestly, gaps included" flush>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Entity</th>
                <th>Extract</th>
                <th>Apply</th>
                <th>Verbs</th>
                <th>Expected fidelity</th>
                <th>Known gaps</th>
              </tr>
            </thead>
            <tbody>
              {manifest.entities.map((capability) => (
                <tr key={capability.entity_kind}>
                  <td>{capability.entity_kind}</td>
                  <td>{capability.can_extract ? "✓" : "—"}</td>
                  <td>{capability.can_apply ? "✓" : "—"}</td>
                  <td className="mono faint">{capability.supported_verbs.join(", ") || "—"}</td>
                  <td>
                    <Pill kind={capability.expected_fidelity} />
                    {capability.fidelity_notes && (
                      <div className="faint" style={{ fontSize: 11 }}>
                        {capability.fidelity_notes}
                      </div>
                    )}
                  </td>
                  <td className="muted">
                    {capability.known_gaps.length === 0 ? (
                      <span className="faint">none declared</span>
                    ) : (
                      capability.known_gaps.map((gap) => <div key={gap}>{gap}</div>)
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
