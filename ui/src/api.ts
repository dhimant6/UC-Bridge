/**
 * Typed client for the control plane.
 *
 * One rule shapes everything here: a guardrail refusal is not a transport
 * failure. The API returns 4xx with `{error, message, guardrail}` for refusals
 * it *meant* to send, so `ApiError` carries that structure through and screens
 * render it as a result rather than as a broken request.
 */

export type Fidelity = "LOSSLESS" | "DEGRADED" | "UNMAPPABLE";
export type Severity = "BLOCKER" | "HIGH" | "MEDIUM" | "LOW" | "INFO";
export type Role = "VIEWER" | "PLANNER" | "APPROVER" | "OPERATOR" | "ADMIN";

export class ApiError extends Error {
  readonly status: number;
  readonly kind: string;
  readonly guardrail: boolean;
  readonly needs?: string;
  readonly before?: string;

  constructor(status: number, body: Record<string, unknown>) {
    const message =
      typeof body.message === "string"
        ? body.message
        : typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body);
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = typeof body.error === "string" ? body.error : `HTTP ${status}`;
    this.guardrail = body.guardrail === true;
    if (typeof body.needs === "string") this.needs = body.needs;
    if (typeof body.before === "string") this.before = body.before;
  }

  /** A refusal the product intends, as opposed to something going wrong. */
  get isRefusal(): boolean {
    return this.guardrail || this.status === 403 || this.status === 409;
  }
}

let activeRoles: Role[] = ["PLANNER"];

export function setRoles(roles: Role[]): void {
  activeRoles = roles;
}

export function getRoles(): Role[] {
  return activeRoles;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-UCM-Roles": activeRoles.join(","),
      ...(init.headers ?? {}),
    },
  });

  const isJson = (response.headers.get("content-type") ?? "").includes("json");
  if (!response.ok) {
    const body = isJson ? await response.json() : { message: await response.text() };
    throw new ApiError(response.status, body as Record<string, unknown>);
  }
  return (isJson ? await response.json() : await response.text()) as T;
}

const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

/* ------------------------------------------------------------------ */
/* Shapes                                                              */
/* ------------------------------------------------------------------ */

export interface SessionInfo {
  principal: string;
  tenant_id: string;
  roles: Role[];
  permissions: string[];
  role_catalogue: Record<Role, string[]>;
  version: string;
}

export type StageName =
  | "discovery"
  | "assessment"
  | "mapping"
  | "waves"
  | "plan"
  | "dry_run"
  | "run"
  | "validation";

export interface EstateState {
  estate_id: string;
  name: string;
  summary: string;
  tenant_id: string;
  direction: string;
  source_connector_id: string;
  target_connector_id: string;
  source_estate_id: string;
  target_estate_id: string;
  write_verb: string;
  has_mapping_profile: boolean;
  /** Why this source has no write path into this target, when it has none. */
  no_write_path: string | null;
  stages: Record<StageName, boolean>;
  headline: string | null;
  source_readiness: string;
  target_readiness: string;
  target_may_write_to_production: boolean;
  run_ids: string[];
}

export interface ModelBreakdown {
  model: string;
  count: number;
  device_type: string;
  replacement_required: boolean;
}

export interface OrphanFinding {
  kind: string;
  canonical_id: string;
  display_name: string | null;
  reason: string;
}

export interface EstateReport {
  estate_id: string;
  snapshot_id: string;
  generated_at: string;
  entity_counts: Record<string, number>;
  user_count: number;
  telephony_enabled_user_count: number;
  device_count: number;
  device_models: ModelBreakdown[];
  devices_requiring_replacement: number;
  analogue_endpoint_count: number;
  extension_count: number;
  extensions_without_e164: number;
  non_e164_numbers: string[];
  duplicate_directory_numbers: string[];
  dormant_extensions: string[];
  unused_partitions: string[];
  orphans: OrphanFinding[];
  dial_plan_complexity_score: number;
  complexity_drivers: Record<string, number>;
  fidelity_by_kind: Record<string, Record<string, number>>;
  unassessed_count: number;
  estimated_manual_effort_minutes: number;
  raw_sql_reads: number;
  warnings: string[];
}

export interface EntityRow {
  canonical_id: string;
  kind: string;
  domain: string;
  display_name: string | null;
  fidelity: Fidelity;
  is_assessed: boolean;
  degraded_count: number;
  unmapped_count: number;
  manual_effort_minutes: number | null;
  native_key: string | null;
  native_type: string | null;
  platform: string | null;
}

export interface EntityPage {
  total: number;
  offset: number;
  limit: number;
  kinds: string[];
  rows: EntityRow[];
}

export interface DegradedAttribute {
  attribute: string;
  reason: string;
  source_value: string | null;
  target_behaviour: string;
}

export interface EntityDetail {
  entity: {
    kind: string;
    canonical_id: string;
    display_name: string | null;
    fidelity: {
      level: Fidelity;
      rationale: string;
      unmapped_source_attributes: string[];
      degraded_attributes: DegradedAttribute[];
      manual_effort_minutes: number | null;
    };
    transform_log: Array<{
      at: string;
      operation: string;
      actor: string;
      summary: string;
      attribute: string | null;
      rule_ref: string | null;
    }>;
  };
  content_view: Record<string, unknown>;
  references: Record<string, string[]>;
  history: AuditRecord[];
}

export interface Finding {
  rule_id: string;
  title: string;
  severity: Severity;
  detail: string;
  remediation: string;
  affected_sample: string[];
  affected_count: number;
  status: "OPEN" | "ACKNOWLEDGED" | "RESOLVED" | "WAIVED";
  waived_by: string | null;
  waived_reason: string | null;
}

export interface AssessmentReport {
  snapshot_id: string;
  estate_id: string;
  target_platform: string | null;
  findings: Finding[];
  is_ready_to_plan: boolean;
  counts_by_severity: Record<string, number>;
}

export interface MappingCandidate {
  source_id: string;
  source_label: string;
  target_id: string | null;
  target_label: string | null;
  confidence: number;
  decision: "AUTO" | "SUGGEST" | "WEAK" | "NONE";
  signals: Array<{ name: string; weight: number; detail: string }>;
}

/** Two rules at one site that can both match the same extension. */
export interface Overlap {
  site_code: string;
  first_pattern: string;
  second_pattern: string;
  example: string;
  detail: string;
}

export interface MappingView {
  has_profile: boolean;
  profile: {
    profile_id: string;
    name: string;
    target_platform: string;
    rules: {
      rules: Array<{
        id: string;
        description: string | null;
        when: { entity: string | null; pattern: string | null };
        then: Record<string, string>;
      }>;
    };
  } | null;
  number_plan?: {
    rules: Array<{
      site_code: string;
      internal_pattern: string;
      e164_prefix: string;
      strip_digits: number;
      description: string | null;
    }>;
    overlaps: Overlap[];
  };
  transform: {
    is_clean: boolean;
    issues: Array<{
      canonical_id: string;
      kind: string;
      attribute: string | null;
      problem: string;
      detail: string;
    }>;
    overlaps: Overlap[];
    collisions: Array<{ e164: string; sources: string[] }>;
    numbers_created: number;
    rules_fired: Record<string, number>;
    fidelity_by_kind: Record<string, Record<string, number>>;
    entity_count: number;
  } | null;
  automap: {
    summary: Record<string, number>;
    candidates: MappingCandidate[];
  };
}

export interface Wave {
  wave_id: string;
  name: string;
  sequence: number;
  user_keys: string[];
  notes: string | null;
}

export interface WaveView {
  is_valid: boolean;
  summary: string;
  plan: {
    plan_name: string;
    strategy: string;
    waves: Wave[];
    clusters: Array<{
      cluster_id: string;
      kind: string;
      user_keys: string[];
      reason: string;
    }>;
    violations: Array<{
      cluster_id: string;
      kind: string;
      reason: string;
      split_across: Record<string, string[]>;
      consequence: string;
    }>;
    unassigned_user_keys: string[];
  };
  coexistence: Array<{
    wave_id: string;
    migrated_user_count: number;
    remaining_user_count: number;
    interop_numbers: string[];
    detail: string;
  }>;
}

export interface WriteOperation {
  op_id: string;
  verb: string;
  entity_kind: string;
  canonical_id: string;
  idempotency_key: string;
  depends_on: string[];
  site_code: string | null;
  fidelity: Fidelity;
  description: string | null;
  payload: Record<string, unknown>;
}

export interface PlanView {
  plan: {
    plan_id: string;
    tenant_id: string;
    estate_id: string;
    wave_id: string | null;
    created_at: string;
    operations: WriteOperation[];
    plan_digest: string | null;
  };
  plan_digest: string;
  operation_count: number;
  emergency_sites: string[];
  unmappable_operations: WriteOperation[];
  unresolved_references: Array<{
    canonical_id: string;
    kind: string;
    field: string;
    referenced_id: string;
    reason: string;
  }>;
  skipped_unmappable: string[];
  is_fully_resolved: boolean;
}

export interface OperationPreview {
  op_id: string;
  verb: string;
  target_native_type: string | null;
  target_native_key: string | null;
  api_call: string;
  current_target_state: Record<string, unknown> | null;
  proposed_state: Record<string, unknown>;
  would_change: boolean;
  warnings: string[];
}

export interface DryRunReceipt {
  receipt_id: string;
  plan_id: string;
  plan_digest: string;
  connector_id: string;
  produced_at: string;
  previews: OperationPreview[];
  would_change_count: number;
  no_change_count: number;
  warnings: string[];
}

export interface RunSummary {
  run_id: string;
  plan_id: string;
  tenant_id: string;
  connector_id: string;
  mode: string;
  state: string;
  started_at: string;
  finished_at: string | null;
  total_operations: number;
  completed_operations: number;
  counts: Record<string, number>;
  checkpoint_op_id: string | null;
  failure_reason: string | null;
  progress: number;
  succeeded: boolean;
  has_rollback_bundle: boolean;
}

export interface RunRecordRow {
  run_id: string;
  plan_id: string;
  tenant_id: string;
  connector_id: string;
  mode: string;
  state: string;
  started_at: string;
  finished_at: string | null;
  total_operations: number;
  completed_op_ids: string[];
  checkpoint_op_id: string | null;
  counts: Record<string, number>;
  pause_requested: boolean;
  failure_reason: string | null;
  has_rollback_bundle: boolean;
  estate_id: string | null;
}

export interface CheckResult {
  check_id: string;
  title: string;
  outcome: "PASS" | "FAIL" | "HARD_FAIL" | "SKIPPED" | "NOT_APPLICABLE";
  detail: string;
  affected_sample: string[];
  expected: number | null;
  actual: number | null;
}

export interface EntityReconciliation {
  kind: string;
  natural_key: string;
  /** MATCHED, MISSING_ON_TARGET, EXTRA_ON_TARGET, or MISMATCHED. */
  status: string;
  source_canonical_id: string | null;
  target_canonical_id: string | null;
  mismatches: Array<{
    attribute: string;
    source_value: unknown;
    target_value: unknown;
    explanation?: string | null;
  }>;
}

export interface ReconciliationReport {
  results: EntityReconciliation[];
  source_counts: Record<string, number>;
  target_counts: Record<string, number>;
  /** Added by the API: `passed` is a property and does not survive model_dump. */
  passed: boolean;
  counts_by_status: Record<string, number>;
  summary: string;
}

export interface ValidationView {
  run_id: string;
  tenant_id: string;
  generated_at: string;
  checks: CheckResult[];
  reconciliation: ReconciliationReport | null;
  passed: boolean;
  safe_to_sign_off: boolean;
  counts: Record<string, number>;
  markdown: string;
}

export interface AuditRecord {
  sequence: number;
  at: string;
  tenant_id: string;
  actor: string;
  action: string;
  correlation_id: string | null;
  run_id: string | null;
  plan_id: string | null;
  entity_kind: string | null;
  canonical_id: string | null;
  target_native_key: string | null;
  before: unknown;
  after: unknown;
  detail: string | null;
  dry_run: boolean;
  raw_sql_used: boolean;
  previous_hash: string;
  record_hash: string;
}

export interface AuditPage {
  total: number;
  head_hash: string;
  chain_length: number;
  records: AuditRecord[];
}

export interface ApiSurface {
  name: string;
  version: string | null;
  transport: string | null;
  documentation_url: string | null;
  verified_at: string | null;
  verification_method: string | null;
  notes: string | null;
}

export interface ConnectorEntry {
  manifest: {
    connector_id: string;
    connector_version: string;
    platform: string;
    display_name: string;
    api_surfaces: ApiSurface[];
    entities: Array<{
      entity_kind: string;
      can_extract: boolean;
      can_apply: boolean;
      supported_verbs: string[];
      expected_fidelity: Fidelity;
      fidelity_notes: string | null;
      known_gaps: string[];
      required_permissions: string[];
    }>;
    rate_limits: Record<string, unknown>;
    eventual_consistency: { is_eventually_consistent: boolean };
    supports_dry_run: boolean;
    supports_rollback: boolean;
    air_gap_capable: boolean;
    requires_publisher_node: boolean;
    notes: string | null;
  };
  readiness: {
    connector_id: string;
    level: "PRODUCTION_READY" | "LAB_ONLY" | "UNVERIFIED";
    verified_surfaces: string[];
    unverified_surfaces: string[];
    synthetic_cassettes: string[];
    oldest_verification: string | null;
    notes: string[];
  };
  may_write_to_production: boolean;
  extractable_kinds: string[];
  appliable_kinds: string[];
  unmappable_kinds: string[];
  unverified_api_surfaces: ApiSurface[];
}

export interface AuthorizationForm {
  requested_by?: string;
  approvers?: string[];
  correlation_id?: string;
  window_start?: string | null;
  window_end?: string | null;
  change_reference?: string | null;
  window_override_reason?: string | null;
  window_override_by?: string | null;
  confirmed_sites?: string[] | null;
  confirmed_by?: string | null;
  run_id?: string | null;
  resume?: boolean;
}

/* ------------------------------------------------------------------ */
/* Calls                                                               */
/* ------------------------------------------------------------------ */

export const api = {
  session: () => get<SessionInfo>("/session"),

  estates: () => get<EstateState[]>("/estates"),
  estate: (id: string) => get<EstateState>(`/estates/${id}`),
  discover: (id: string) =>
    post<{ snapshot_id: string; snapshot_digest: string; entity_count: number; report: EstateReport }>(
      `/estates/${id}/discover`,
    ),
  report: (id: string) => get<EstateReport>(`/estates/${id}/report`),
  reportMarkdown: (id: string) => get<string>(`/estates/${id}/report.md`),
  reset: (id: string) => post<EstateState>(`/estates/${id}/reset`),

  entities: (id: string, params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return get<EntityPage>(`/estates/${id}/entities?${query.toString()}`);
  },
  entity: (id: string, canonicalId: string) =>
    get<EntityDetail>(`/estates/${id}/entities/${encodeURIComponent(canonicalId)}`),

  assess: (id: string, targetPlatform?: string) =>
    post<AssessmentReport>(`/estates/${id}/assess`, { target_platform: targetPlatform ?? null }),
  assessment: (id: string) => get<AssessmentReport>(`/estates/${id}/assessment`),
  waive: (id: string, ruleId: string, by: string, reason: string) =>
    post<Finding>(`/estates/${id}/assessment/${ruleId}/waive`, { by, reason }),

  map: (id: string) => post<MappingView>(`/estates/${id}/map`),
  mapping: (id: string) => get<MappingView>(`/estates/${id}/mapping`),

  buildWaves: (id: string, strategy: string, maxWaveSize: number | null) =>
    post<WaveView>(`/estates/${id}/waves`, { strategy, max_wave_size: maxWaveSize }),
  waves: (id: string) => get<WaveView>(`/estates/${id}/waves`),
  moveUser: (id: string, userKey: string, toWaveId: string) =>
    post<WaveView>(`/estates/${id}/waves/move`, { user_key: userKey, to_wave_id: toWaveId }),
  runbook: (id: string, waveId: string) => get<string>(`/estates/${id}/waves/${waveId}/runbook`),

  buildPlan: (id: string, waveId: string | null) =>
    post<PlanView>(`/estates/${id}/plan`, { wave_id: waveId }),
  plan: (id: string) => get<PlanView>(`/estates/${id}/plan`),
  dryRun: (id: string) => post<DryRunReceipt>(`/estates/${id}/dry-run`),
  receipt: (id: string) => get<DryRunReceipt>(`/estates/${id}/dry-run`),

  execute: (id: string, form: AuthorizationForm) => post<RunSummary>(`/estates/${id}/runs`, form),
  rollback: (id: string, runId: string, form: AuthorizationForm) =>
    post<RunSummary>(`/estates/${id}/runs/${runId}/rollback`, form),
  runs: () => get<RunRecordRow[]>("/runs"),
  run: (runId: string) => get<RunRecordRow & { audit: AuditRecord[] }>(`/runs/${runId}`),

  validate: (id: string) => post<ValidationView>(`/estates/${id}/validate`),
  validation: (id: string) => get<ValidationView>(`/estates/${id}/validation`),

  audit: (params: Record<string, string | number | boolean | undefined>) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return get<AuditPage>(`/audit?${query.toString()}`);
  },
  verifyAudit: () => get<{ verified: boolean; chain_length: number; head_hash: string }>("/audit/verify"),
  evidence: (runId: string) => get<Record<string, unknown>>(`/audit/evidence/${runId}`),

  connectors: () => get<ConnectorEntry[]>("/connectors"),
};
