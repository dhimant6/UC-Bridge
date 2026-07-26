/**
 * Screen 2 — Assessment.
 *
 * Eighteen rules, sorted by how much they should stop you. The screen's job is
 * to make the difference between "waivable risk" and "blocker" impossible to
 * miss, and to refuse the waiver on a blocker in the place where someone would
 * try it.
 */

import { useState } from "react";
import { api } from "../api";
import type { Finding, Severity } from "../api";
import { PageHead } from "../components/Layout";
import {
  ActionButton,
  ApiProblem,
  Callout,
  Empty,
  Panel,
  Pill,
  Stat,
  useAsync,
} from "../components/Bits";
import { useApp, useGate } from "../state";

const ORDER: Severity[] = ["BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO"];

export function AssessmentScreen() {
  const { estateId, estate, refresh } = useApp();
  const read = useGate("READ_ESTATE");
  const approve = useGate("APPROVE_PLAN");
  const assessment = useAsync(() => api.assessment(estateId), [estateId]);
  const [severity, setSeverity] = useState<Severity | "">("");
  const [showWaived, setShowWaived] = useState(true);

  const findings = (assessment.data?.findings ?? [])
    .filter((finding) => (severity ? finding.severity === severity : true))
    .filter((finding) => (showWaived ? true : finding.status !== "WAIVED"))
    .sort((a, b) => ORDER.indexOf(a.severity) - ORDER.indexOf(b.severity));

  const counts = assessment.data?.counts_by_severity ?? {};

  return (
    <>
      <PageHead title="Assessment">
        Rules run against the discovered snapshot for a specific target platform, because "is this
        ready" only means something once you know what it is going to. An emergency-calling gap is
        an unwaivable blocker: if a customer genuinely accepts one, that decision belongs outside
        this tool, in writing.
      </PageHead>

      <Panel
        title="Run the rules"
        note={
          assessment.data
            ? `${assessment.data.findings.length} findings against ${assessment.data.target_platform ?? "the target"}`
            : undefined
        }
        actions={
          <ActionButton
            label={assessment.data ? "Re-assess" : "Assess"}
            run={() => api.assess(estateId)}
            onDone={() => {
              assessment.reload();
              refresh();
            }}
            disabled={!read.allowed}
            reason={read.reason}
          />
        }
      >
        {assessment.error ? (
          <ApiProblem error={assessment.error} />
        ) : assessment.data ? (
          <div className="stack">
            <div className="stats" style={{ borderRadius: 4 }}>
              {ORDER.map((level) => (
                <Stat
                  key={level}
                  label={level}
                  value={counts[level] ?? 0}
                  tone={level === "BLOCKER" && (counts[level] ?? 0) > 0 ? "bad" : undefined}
                />
              ))}
            </div>

            {assessment.data.is_ready_to_plan ? (
              <Callout tone="ok" title="Ready to plan">
                No open blockers. Every remaining finding is either waived or accepted risk.
              </Callout>
            ) : (
              <Callout tone="refused" title="Not ready to plan">
                {counts.BLOCKER ?? 0} open blocker(s). The planner will still let you build a plan
                so you can see its shape, but the objects behind a blocker are the ones to fix or
                exclude first.
              </Callout>
            )}

            {estate?.target_may_write_to_production === false && (
              <Callout tone="refused" title={`Target connector is ${estate.target_readiness}`}>
                Even a clean assessment will not get a production write through. The readiness gate
                blocks it because the connector's cassettes are hand-authored rather than captured
                from a real system.
              </Callout>
            )}
          </div>
        ) : null}
      </Panel>

      {assessment.data && (
        <Panel
          title="Findings"
          actions={
            <div className="actions">
              <select value={severity} onChange={(event) => setSeverity(event.target.value as Severity | "")}>
                <option value="">All severities</option>
                {ORDER.map((level) => (
                  <option key={level} value={level}>
                    {level}
                  </option>
                ))}
              </select>
              <label className="row" style={{ gap: 5, fontSize: 11 }}>
                <input
                  type="checkbox"
                  checked={showWaived}
                  onChange={(event) => setShowWaived(event.target.checked)}
                />
                <span className="faint">Show waived</span>
              </label>
            </div>
          }
          flush
        >
          {findings.length === 0 ? (
            <Empty title="No findings match" />
          ) : (
            <div>
              {findings.map((finding) => (
                <FindingRow
                  key={finding.rule_id}
                  estateId={estateId}
                  finding={finding}
                  canWaive={approve.allowed}
                  waiveReason={approve.reason}
                  onWaived={() => assessment.reload()}
                />
              ))}
            </div>
          )}
        </Panel>
      )}
    </>
  );
}

function FindingRow({
  estateId,
  finding,
  canWaive,
  waiveReason,
  onWaived,
}: {
  estateId: string;
  finding: Finding;
  canWaive: boolean;
  waiveReason: string;
  onWaived: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("Accepted as a known risk for this wave.");
  const isBlocker = finding.severity === "BLOCKER";

  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <div
        className="row"
        style={{ padding: "9px 13px", cursor: "pointer" }}
        onClick={() => setOpen(!open)}
      >
        <span className="faint mono" style={{ width: 12 }}>
          {open ? "−" : "+"}
        </span>
        <Pill kind={finding.severity} />
        <span className="mono faint" style={{ width: 62 }}>
          {finding.rule_id}
        </span>
        <strong style={{ fontWeight: 550 }}>{finding.title}</strong>
        {finding.affected_count > 0 && (
          <span className="pill plain">{finding.affected_count} affected</span>
        )}
        {finding.status === "WAIVED" && <Pill kind="WAIVED">waived</Pill>}
        <span style={{ marginLeft: "auto" }} className="muted">
          {finding.detail.slice(0, 70)}
          {finding.detail.length > 70 ? "…" : ""}
        </span>
      </div>

      {open && (
        <div style={{ padding: "0 13px 13px 37px" }} className="stack">
          <div>
            <h3 style={{ marginBottom: 3 }}>Detail</h3>
            <div>{finding.detail}</div>
          </div>
          <div>
            <h3 style={{ marginBottom: 3 }}>Remediation</h3>
            <div>{finding.remediation}</div>
          </div>

          {finding.affected_sample.length > 0 && (
            <div>
              <h3 style={{ marginBottom: 5 }}>
                Affected sample ({finding.affected_sample.length} of {finding.affected_count})
              </h3>
              <div className="row" style={{ gap: 5 }}>
                {finding.affected_sample.map((id) => (
                  <span key={id} className="pill plain mono">
                    {id}
                  </span>
                ))}
              </div>
            </div>
          )}

          {finding.status === "WAIVED" ? (
            <Callout tone="ok" title={`Waived by ${finding.waived_by}`}>
              {finding.waived_reason}
            </Callout>
          ) : isBlocker ? (
            <Callout tone="refused" title="This blocker cannot be waived in the tool">
              Blockers are resolved, or the affected objects are excluded from the plan. The waive
              button is still here — press it and the library refuses, which is the point.
              <div className="actions" style={{ marginTop: 8 }}>
                <ActionButton
                  label="Attempt to waive"
                  variant="danger"
                  run={() => api.waive(estateId, finding.rule_id, "approver@contoso.example", reason)}
                  onDone={onWaived}
                  disabled={!canWaive}
                  reason={waiveReason}
                />
              </div>
            </Callout>
          ) : (
            <div className="actions">
              <input
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                style={{ width: 340 }}
                placeholder="Reason (recorded on the audit chain)"
              />
              <ActionButton
                label="Waive"
                variant="plain"
                run={() => api.waive(estateId, finding.rule_id, "approver@contoso.example", reason)}
                onDone={onWaived}
                disabled={!canWaive || reason.trim().length === 0}
                reason={waiveReason || "A reason is required."}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
