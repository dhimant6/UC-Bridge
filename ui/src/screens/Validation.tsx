/**
 * Screen 7 — Validation.
 *
 * Eight checks that go beyond "the API returned 200". Reconciliation passing and
 * fidelity reporting a loss are both true at once, and the screen has to hold
 * both without implying one contradicts the other: the object arrived, and some
 * of what it meant did not.
 */

import { api } from "../api";
import type { ReconciliationReport } from "../api";
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

export function ValidationScreen() {
  const { estateId, refresh } = useApp();
  const gate = useGate("READ_ESTATE");
  const validation = useAsync(() => api.validation(estateId), [estateId]);

  const report = validation.data;

  return (
    <>
      <PageHead title="Validation">
        Run after the writes, against a freshly extracted target snapshot. A missing emergency
        location is a hard failure, not a warning, because the consequence of getting it wrong is not
        a support ticket.
      </PageHead>

      <Panel
        title="Post-migration checks"
        note={report ? `Run ${report.run_id}` : undefined}
        actions={
          <ActionButton
            label={report ? "Re-validate" : "Validate"}
            run={() => api.validate(estateId)}
            onDone={() => {
              validation.reload();
              refresh();
            }}
            disabled={!gate.allowed}
            reason={gate.reason}
          />
        }
      >
        {validation.error ? (
          <ApiProblem error={validation.error} />
        ) : report ? (
          <div className="stack">
            <div className="stats" style={{ borderRadius: 4 }}>
              {Object.entries(report.counts).map(([outcome, count]) => (
                <Stat
                  key={outcome}
                  label={outcome}
                  value={count}
                  tone={
                    outcome === "PASS"
                      ? "good"
                      : outcome === "HARD_FAIL" || outcome === "FAIL"
                        ? count > 0
                          ? "bad"
                          : undefined
                        : undefined
                  }
                />
              ))}
            </div>

            {report.safe_to_sign_off ? (
              <Callout tone="ok" title="Safe to sign off">
                No hard failures. Sign-off is a human decision, but nothing here blocks it.
              </Callout>
            ) : (
              <Callout tone="refused" title="Not safe to sign off">
                At least one hard failure. Fix it or roll back — do not sign off around it.
              </Callout>
            )}
          </div>
        ) : (
          <Empty title="No validation yet">Apply a plan, then validate the result.</Empty>
        )}
      </Panel>

      {report && (
        <>
          <Panel title="Checks" flush>
            <table>
              <thead>
                <tr>
                  <th>Check</th>
                  <th>Outcome</th>
                  <th className="num">Expected</th>
                  <th className="num">Actual</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {report.checks.map((check) => (
                  <tr key={check.check_id}>
                    <td>
                      <div>{check.title}</div>
                      <div className="mono faint" style={{ fontSize: 11 }}>
                        {check.check_id}
                      </div>
                    </td>
                    <td>
                      <Pill kind={check.outcome} />
                    </td>
                    <td className="num">{check.expected ?? "—"}</td>
                    <td className="num">{check.actual ?? "—"}</td>
                    <td className="muted">
                      {check.detail}
                      {check.affected_sample.length > 0 && (
                        <div className="row" style={{ gap: 4, marginTop: 4 }}>
                          {check.affected_sample.map((id) => (
                            <span key={id} className="pill plain mono">
                              {id}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          {report.reconciliation && <Reconciliation report={report.reconciliation} />}

          <Panel title="Sign-off pack" note="Rendered by the library">
            <Document text={report.markdown} />
          </Panel>
        </>
      )}
    </>
  );
}

const STATUS_TONE: Record<string, "good" | "bad" | "warn" | undefined> = {
  MATCHED: "good",
  MISSING_ON_TARGET: "bad",
  EXTRA_ON_TARGET: "warn",
  MISMATCHED: "warn",
};

function Reconciliation({ report }: { report: ReconciliationReport }) {
  const failures = report.results.filter((result) => result.status !== "MATCHED");
  const kinds = Array.from(
    new Set([...Object.keys(report.source_counts), ...Object.keys(report.target_counts)]),
  ).sort();

  return (
    <>
      <Panel title="Reconciliation" note={report.summary}>
        <div className="stack">
          <div className="stats" style={{ borderRadius: 4 }}>
            {Object.entries(report.counts_by_status).map(([status, count]) => (
              <Stat key={status} label={status} value={count} tone={STATUS_TONE[status]} />
            ))}
          </div>

          {report.passed ? (
            <Callout tone="info" title="Reconciled and still lossy — both are true">
              Reconciliation asks whether every object arrived. Fidelity asks how much of what it
              meant survived. A shared line that became a call group reconciles perfectly and is
              still a degradation, which is why the two are reported separately rather than collapsed
              into one green tick.
            </Callout>
          ) : (
            <Callout tone="refused" title={`${failures.length} object(s) did not reconcile`}>
              Counts alone would have looked fine. This is the attribute-level comparison.
            </Callout>
          )}
        </div>
      </Panel>

      <div className="grid two">
        <Panel title="Object counts, source versus target" flush>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Kind</th>
                  <th className="num">Source</th>
                  <th className="num">Target</th>
                  <th className="num">Δ</th>
                </tr>
              </thead>
              <tbody>
                {kinds.map((kind) => {
                  const source = report.source_counts[kind] ?? 0;
                  const target = report.target_counts[kind] ?? 0;
                  const delta = target - source;
                  return (
                    <tr key={kind}>
                      <td>{kind}</td>
                      <td className="num">{source}</td>
                      <td className="num">{target}</td>
                      <td
                        className="num"
                        style={{ color: delta === 0 ? "var(--text-faint)" : "var(--degraded)" }}
                      >
                        {delta === 0 ? "—" : delta > 0 ? `+${delta}` : delta}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title={failures.length > 0 ? "Discrepancies" : "Per-object results"}
          note={`${report.results.length} objects compared`}
          flush
        >
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Natural key</th>
                  <th>Status</th>
                  <th>Attribute mismatches</th>
                </tr>
              </thead>
              <tbody>
                {(failures.length > 0 ? failures : report.results).slice(0, 200).map((result) => (
                  <tr key={`${result.kind}:${result.natural_key}`}>
                    <td>{result.kind}</td>
                    <td className="mono faint">{result.natural_key}</td>
                    <td>
                      <Pill kind={result.status === "MATCHED" ? "PASS" : "FAIL"}>
                        {result.status}
                      </Pill>
                    </td>
                    <td className="muted">
                      {result.mismatches.length === 0 ? (
                        <span className="faint">none</span>
                      ) : (
                        result.mismatches.map((mismatch) => (
                          <div key={mismatch.attribute}>
                            <span className="mono">{mismatch.attribute}</span>:{" "}
                            {String(mismatch.source_value)} → {String(mismatch.target_value)}
                          </div>
                        ))
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </>
  );
}
