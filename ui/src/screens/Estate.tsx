/**
 * Screen 1 — Estate.
 *
 * What is actually out there. The estate report plus a browsable snapshot,
 * because the aggregate numbers are only trustworthy if you can click into the
 * objects behind them.
 */

import { useState } from "react";
import { api } from "../api";
import type { EntityRow, Fidelity } from "../api";
import { PageHead } from "../components/Layout";
import { ActionButton, ApiProblem, Callout, Document, Empty, Json, Panel, Pill, Stat, useAsync } from "../components/Bits";
import { VirtualTable } from "../components/VirtualTable";
import type { Column } from "../components/VirtualTable";
import { useApp, useGate } from "../state";

export function EstateScreen() {
  const { estateId, estate, refresh } = useApp();
  const gate = useGate("RUN_DISCOVERY");
  const report = useAsync(() => api.report(estateId), [estateId]);
  const [tab, setTab] = useState<"inventory" | "entities" | "markdown">("inventory");

  if (!estateId) return <Empty title="No estate selected" />;

  return (
    <>
      <PageHead title="Estate">
        {estate?.summary} Discovery is read-only and re-runnable: two crawls of an unchanged estate
        produce the same snapshot digest, which is what makes the diff between them meaningful.
      </PageHead>

      <Panel
        title="Discovery"
        note={
          estate
            ? `${estate.source_connector_id} → ${estate.target_connector_id} · ${estate.direction}`
            : undefined
        }
        actions={
          <div className="actions">
            <ActionButton
              label={report.data ? "Re-run discovery" : "Run discovery"}
              run={() => api.discover(estateId)}
              onDone={() => {
                report.reload();
                refresh();
              }}
              disabled={!gate.allowed}
              reason={gate.reason}
            />
            <ActionButton
              label="Reset estate"
              variant="plain"
              run={() => api.reset(estateId)}
              onDone={() => {
                report.reload();
                refresh();
              }}
              disabled={!gate.allowed}
              reason={gate.reason}
            />
          </div>
        }
      >
        {report.loading ? (
          <div className="row">
            <span className="spinner" /> Loading…
          </div>
        ) : report.error ? (
          <ApiProblem error={report.error} />
        ) : report.data ? (
          <div className="stack">
            <div className="stats" style={{ borderRadius: 4 }}>
              <Stat label="Users" value={report.data.user_count} hint={`${report.data.telephony_enabled_user_count} telephony-enabled`} />
              <Stat label="Devices" value={report.data.device_count} hint={`${report.data.devices_requiring_replacement} need replacement`} tone={report.data.devices_requiring_replacement > 0 ? "warn" : undefined} />
              <Stat label="Extensions" value={report.data.extension_count} hint={`${report.data.extensions_without_e164} without E.164`} tone={report.data.extensions_without_e164 > 0 ? "bad" : "good"} />
              <Stat label="Analogue" value={report.data.analogue_endpoint_count} hint="No cloud equivalent" tone={report.data.analogue_endpoint_count > 0 ? "warn" : undefined} />
              <Stat label="Dial-plan complexity" value={report.data.dial_plan_complexity_score} hint="Weighted, see drivers" />
              <Stat
                label="Manual remediation"
                value={`${(report.data.estimated_manual_effort_minutes / 60).toFixed(1)} h`}
                hint={`${report.data.estimated_manual_effort_minutes} minutes`}
              />
            </div>

            {report.data.unassessed_count > 0 && (
              <Callout tone="refused" title={`${report.data.unassessed_count} entities have no fidelity assessment`}>
                A plan cannot be approved while this is non-zero. Nothing is LOSSLESS by default —
                the claim has to be earned with evidence.
              </Callout>
            )}
            {report.data.warnings.map((warning) => (
              <Callout key={warning} tone="info" title="Warning">
                {warning}
              </Callout>
            ))}
          </div>
        ) : null}
      </Panel>

      {report.data && (
        <>
          <div className="row" style={{ marginBottom: 10 }}>
            {(["inventory", "entities", "markdown"] as const).map((option) => (
              <button
                key={option}
                className={`btn tiny${tab === option ? " primary" : ""}`}
                onClick={() => setTab(option)}
              >
                {option === "inventory" ? "Inventory" : option === "entities" ? "Objects" : "Report"}
              </button>
            ))}
          </div>

          {tab === "inventory" && <Inventory estateId={estateId} />}
          {tab === "entities" && <EntityBrowser estateId={estateId} />}
          {tab === "markdown" && <ReportMarkdown estateId={estateId} />}
        </>
      )}
    </>
  );
}

function Inventory({ estateId }: { estateId: string }) {
  const report = useAsync(() => api.report(estateId), [estateId]);
  if (!report.data) return null;
  const data = report.data;

  const fidelityRows = Object.entries(data.fidelity_by_kind);

  return (
    <div className="grid two">
      <Panel title="Entity counts" flush>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Kind</th>
                <th className="num">Count</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.entity_counts)
                .sort((a, b) => b[1] - a[1])
                .map(([kind, count]) => (
                  <tr key={kind}>
                    <td>{kind}</td>
                    <td className="num">{count}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Fidelity by kind" note="Nothing is lossless by default" flush>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Kind</th>
                <th className="num">Lossless</th>
                <th className="num">Degraded</th>
                <th className="num">Unmappable</th>
              </tr>
            </thead>
            <tbody>
              {fidelityRows.map(([kind, buckets]) => (
                <tr key={kind}>
                  <td>{kind}</td>
                  <td className="num" style={{ color: "var(--lossless)" }}>{buckets.LOSSLESS ?? 0}</td>
                  <td className="num" style={{ color: "var(--degraded)" }}>{buckets.DEGRADED ?? 0}</td>
                  <td className="num" style={{ color: "var(--unmappable)" }}>{buckets.UNMAPPABLE ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Device models" flush>
        {data.device_models.length === 0 ? (
          <Empty title="No devices in this estate" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Type</th>
                  <th className="num">Count</th>
                  <th>Replace</th>
                </tr>
              </thead>
              <tbody>
                {data.device_models.map((model) => (
                  <tr key={model.model}>
                    <td className="mono">{model.model}</td>
                    <td className="muted">{model.device_type}</td>
                    <td className="num">{model.count}</td>
                    <td>{model.replacement_required ? <Pill kind="HIGH">Yes</Pill> : <span className="faint">no</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Dial-plan complexity drivers" note={`Score ${data.dial_plan_complexity_score}`} flush>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Driver</th>
                <th className="num">Contribution</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.complexity_drivers).map(([driver, weight]) => (
                <tr key={driver}>
                  <td>{driver}</td>
                  <td className="num">{weight}</td>
                </tr>
              ))}
              {Object.keys(data.complexity_drivers).length === 0 && (
                <tr>
                  <td colSpan={2} className="faint">
                    No dial-plan objects in this estate.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>

      <ListPanel
        title="Duplicate directory numbers"
        note="Legal on CUCM, a collision on a flat cloud plan"
        items={data.duplicate_directory_numbers}
      />
      <ListPanel title="Dormant seats (no CDR activity)" items={data.dormant_extensions} />
      <ListPanel title="Unused partitions" items={data.unused_partitions} />
      <ListPanel title="Non-E.164 numbers" items={data.non_e164_numbers} />

      <Panel title="Dangling references" note="The residue of half-finished changes" flush>
        {data.orphans.length === 0 ? (
          <Empty title="No dangling references" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Object</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {data.orphans.map((orphan) => (
                  <tr key={`${orphan.canonical_id}:${orphan.reason}`}>
                    <td>{orphan.kind}</td>
                    <td>{orphan.display_name ?? <span className="mono faint">{orphan.canonical_id}</span>}</td>
                    <td className="muted">{orphan.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

function ListPanel({ title, note, items }: { title: string; note?: string; items: string[] }) {
  return (
    <Panel title={title} note={note ?? `${items.length}`}>
      {items.length === 0 ? (
        <span className="faint">None.</span>
      ) : (
        <div className="row" style={{ gap: 5 }}>
          {items.slice(0, 60).map((item) => (
            <span key={item} className="pill plain mono">
              {item}
            </span>
          ))}
          {items.length > 60 && <span className="faint">+{items.length - 60} more</span>}
        </div>
      )}
    </Panel>
  );
}

const FIDELITY_ORDER: Fidelity[] = ["LOSSLESS", "DEGRADED", "UNMAPPABLE"];

function EntityBrowser({ estateId }: { estateId: string }) {
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  const [fidelity, setFidelity] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const page = useAsync(
    () => api.entities(estateId, { kind, q: query, fidelity, limit: 2000 }),
    [estateId, kind, query, fidelity],
  );

  const columns: Column<EntityRow>[] = [
    { key: "kind", header: "Kind", width: 150, render: (row) => row.kind },
    {
      key: "name",
      header: "Name",
      width: "auto",
      render: (row) => row.display_name ?? <span className="faint mono">{row.canonical_id}</span>,
    },
    {
      key: "key",
      header: "Native key",
      width: 220,
      render: (row) => <span className="mono faint">{row.native_key ?? "—"}</span>,
    },
    {
      key: "fidelity",
      header: "Fidelity",
      width: 110,
      render: (row) =>
        row.is_assessed ? <Pill kind={row.fidelity} /> : <Pill kind="muted">unassessed</Pill>,
    },
    {
      key: "loss",
      header: "Losses",
      width: 80,
      align: "right",
      render: (row) =>
        row.degraded_count + row.unmapped_count > 0 ? (
          <span style={{ color: "var(--degraded)" }}>{row.degraded_count + row.unmapped_count}</span>
        ) : (
          <span className="faint">—</span>
        ),
    },
  ];

  return (
    <>
      <Panel
        title="Objects"
        note={page.data ? `${page.data.total} matching` : undefined}
        actions={
          <div className="actions">
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">All kinds</option>
              {page.data?.kinds.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <select value={fidelity} onChange={(e) => setFidelity(e.target.value)}>
              <option value="">Any fidelity</option>
              {FIDELITY_ORDER.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              placeholder="Search id or name"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ width: 190 }}
            />
          </div>
        }
        flush
      >
        {page.error ? (
          <div style={{ padding: 13 }}>
            <ApiProblem error={page.error} />
          </div>
        ) : (
          <VirtualTable
            rows={page.data?.rows ?? []}
            columns={columns}
            rowKey={(row) => row.canonical_id}
            selectedKey={selected}
            onSelect={(row) => setSelected(row.canonical_id)}
            height={430}
            empty={<Empty title="Nothing matches those filters" />}
          />
        )}
      </Panel>

      {selected && <EntityDetail estateId={estateId} canonicalId={selected} />}
    </>
  );
}

function EntityDetail({ estateId, canonicalId }: { estateId: string; canonicalId: string }) {
  const detail = useAsync(() => api.entity(estateId, canonicalId), [estateId, canonicalId]);
  if (detail.error) return <ApiProblem error={detail.error} />;
  if (!detail.data) return null;

  const { entity, content_view, references, history } = detail.data;
  const fidelity = entity.fidelity;

  return (
    <Panel
      title={entity.display_name ?? entity.canonical_id}
      note={
        <span className="row" style={{ gap: 6 }}>
          <Pill kind={fidelity.level} />
          <span className="mono faint">{entity.canonical_id}</span>
        </span>
      }
    >
      <div className="stack">
        <Callout tone={fidelity.level === "LOSSLESS" ? "ok" : "info"} title="Fidelity rationale">
          {fidelity.rationale}
          {fidelity.manual_effort_minutes != null && (
            <div className="faint" style={{ marginTop: 4 }}>
              Estimated manual effort: {fidelity.manual_effort_minutes} minutes.
            </div>
          )}
        </Callout>

        {fidelity.degraded_attributes.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 6 }}>Degraded attributes</h3>
            <table>
              <thead>
                <tr>
                  <th>Attribute</th>
                  <th>Why</th>
                  <th>What the target will do instead</th>
                </tr>
              </thead>
              <tbody>
                {fidelity.degraded_attributes.map((attribute) => (
                  <tr key={attribute.attribute}>
                    <td className="mono">{attribute.attribute}</td>
                    <td className="muted">{attribute.reason}</td>
                    <td>{attribute.target_behaviour}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {fidelity.unmapped_source_attributes.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 6 }}>Unmapped source attributes</h3>
            <div className="row" style={{ gap: 5 }}>
              {fidelity.unmapped_source_attributes.map((attribute) => (
                <span key={attribute} className="pill plain mono">
                  {attribute}
                </span>
              ))}
            </div>
          </div>
        )}

        {entity.transform_log.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 6 }}>Transform log</h3>
            <table>
              <thead>
                <tr>
                  <th>Operation</th>
                  <th>Actor</th>
                  <th>Summary</th>
                  <th>Rule</th>
                </tr>
              </thead>
              <tbody>
                {entity.transform_log.map((row, index) => (
                  <tr key={index}>
                    <td>{row.operation}</td>
                    <td className="mono faint">{row.actor}</td>
                    <td>{row.summary}</td>
                    <td className="mono faint">{row.rule_ref ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="split">
          <div>
            <h3 style={{ marginBottom: 6 }}>Content</h3>
            <Json value={content_view} />
          </div>
          <div>
            <h3 style={{ marginBottom: 6 }}>References</h3>
            <Json value={references} />
          </div>
        </div>

        {history.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 6 }}>Audit history for this object</h3>
            <table>
              <thead>
                <tr>
                  <th>Seq</th>
                  <th>Action</th>
                  <th>Actor</th>
                  <th>Dry run</th>
                </tr>
              </thead>
              <tbody>
                {history.map((record) => (
                  <tr key={record.sequence}>
                    <td className="num mono">{record.sequence}</td>
                    <td>{record.action}</td>
                    <td className="mono faint">{record.actor}</td>
                    <td>{record.dry_run ? <Pill kind="muted">dry run</Pill> : <Pill kind="good">real</Pill>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Panel>
  );
}

function ReportMarkdown({ estateId }: { estateId: string }) {
  const markdown = useAsync(() => api.reportMarkdown(estateId), [estateId]);
  if (markdown.error) return <ApiProblem error={markdown.error} />;
  return (
    <Panel title="Estate report" note="The CAB pack version, rendered by the library">
      {markdown.data ? <Document text={markdown.data} /> : <span className="spinner" />}
    </Panel>
  );
}
