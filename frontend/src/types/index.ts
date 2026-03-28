// ─── Common ──────────────────────────────────────────────────────────────────

export type AgentType =
  | "eligibility"
  | "scheduling"
  | "claims"
  | "prior_auth"
  | "credentialing"
  | "compliance";

export const AGENT_TYPES: AgentType[] = [
  "eligibility",
  "scheduling",
  "claims",
  "prior_auth",
  "credentialing",
  "compliance",
];

export const AGENT_LABELS: Record<AgentType, string> = {
  eligibility: "Eligibility",
  scheduling: "Scheduling",
  claims: "Claims & Billing",
  prior_auth: "Prior Auth",
  credentialing: "Credentialing",
  compliance: "Compliance",
};

export type TaskStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "review"
  | "cancelled";

// ─── Auth ────────────────────────────────────────────────────────────────────

export interface LoginRequest {
  provider: "saml" | "oidc";
  redirect_url?: string;
}

export interface LoginResponse {
  redirect_url: string;
  provider: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface RefreshRequest {
  refresh_token: string;
}

export interface UserProfile {
  id: string;
  email: string;
  full_name: string;
  roles: string[];
  organization_id: string | null;
  last_login: string | null;
  is_active: boolean;
}

export interface LoginPageResponse {
  message: string;
  providers: Array<"saml" | "oidc">;
  redirect_url: string;
  login_endpoint: string;
  usage: string;
}

export interface AuthError {
  detail: string;
  error_code?: string;
}

// ─── Agent Tasks ─────────────────────────────────────────────────────────────

export interface AgentTaskCreate {
  agent_type?: string;
  input_data: Record<string, unknown>;
  patient_id?: string;
  organization_id?: string;
}

export interface AgentTaskUpdate {
  input_data?: Record<string, unknown> | null;
  patient_id?: string | null;
  organization_id?: string | null;
}

export interface AgentTaskResponse {
  id: string;
  task_id: string;
  agent_type: AgentType;
  status: TaskStatus;
  input_data: Record<string, unknown> | null;
  output_data: Record<string, unknown> | null;
  error_message: string | null;
  confidence_score: number | null;
  workflow_execution_id: string | null;
  patient_id: string | null;
  organization_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AgentTaskList {
  items: AgentTaskResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface AgentStatsResponse {
  agent_type: AgentType;
  total_tasks: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  in_review: number;
  cancelled: number;
  avg_confidence: number | null;
}

// ─── Reviews ─────────────────────────────────────────────────────────────────

export interface ReviewResponse {
  id: string;
  task_id: string;
  reviewer_id: string | null;
  status: string;
  reason: string;
  agent_decision: Record<string, unknown> | null;
  confidence_score: number | null;
  reviewer_notes: string | null;
  decided_at: string | null;
  created_at: string;
  updated_at: string | null;
  /** Denormalised from the linked AgentTask */
  agent_type?: AgentType | null;
  /** Denormalised from the linked AgentTask */
  patient_id?: string | null;
}

export interface ReviewList {
  items: ReviewResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReviewActionRequest {
  notes?: string;
}

// ─── Workflows ───────────────────────────────────────────────────────────────

export interface WorkflowExecutionResponse {
  id: string;
  workflow_id: string;
  run_id: string | null;
  agent_type: AgentType;
  status: string;
  task_queue: string | null;
  input_data: Record<string, unknown> | null;
  output_data: Record<string, unknown> | null;
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface WorkflowExecutionList {
  items: WorkflowExecutionResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface WorkflowStartRequest {
  agent_type: string;
  task_id: string;
  input_data: Record<string, unknown>;
  patient_context: Record<string, unknown>;
  payer_context: Record<string, unknown>;
  organization_id?: string | null;
  clearinghouse_config?: Record<string, unknown> | null;
  task_queue?: string | null;
}

export interface WorkflowCancelResponse {
  workflow_id: string;
  status: string;
  message: string;
}

export interface WorkflowHistoryEvent {
  event_id: number;
  event_type: string;
  timestamp: string;
  details: Record<string, unknown>;
}

export interface WorkflowHistoryResponse {
  workflow_id: string;
  events: WorkflowHistoryEvent[];
}

// ─── Payers ──────────────────────────────────────────────────────────────────

export interface PayerResponse {
  id: string;
  name: string;
  payer_id_code: string;
  payer_type: string | null;
  address: string | null;
  phone: string | null;
  electronic_payer_id: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface PayerCreate {
  name: string;
  payer_id_code: string;
  payer_type?: string | null;
  address?: string | null;
  phone?: string | null;
  electronic_payer_id?: string | null;
}

export interface PayerRuleResponse {
  id: string;
  payer_id: string;
  agent_type: AgentType;
  rule_type: string;
  description: string | null;
  conditions: Record<string, unknown>;
  actions: Record<string, unknown> | null;
  effective_date: string;
  termination_date: string | null;
  version: number;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface PayerRuleCreate {
  agent_type: string;
  rule_type: string;
  description?: string | null;
  conditions: Record<string, unknown>;
  actions?: Record<string, unknown> | null;
  effective_date: string;
  termination_date?: string | null;
  version?: number;
}

export interface PayerRuleUpdate {
  conditions?: Record<string, unknown> | null;
  actions?: Record<string, unknown> | null;
  description?: string | null;
  termination_date?: string | null;
  is_active?: boolean | null;
}

export interface PayerRuleEvaluationRequest {
  context: Record<string, unknown>;
  rule_type?: string | null;
}

// ─── Dashboard ───────────────────────────────────────────────────────────────

/** Lightweight task summary returned in the dashboard recent_tasks feed. */
export interface RecentTaskSummary {
  id: string;
  task_id: string;
  agent_type: AgentType;
  status: TaskStatus;
  confidence_score: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface DashboardSummary {
  total_tasks: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  in_review: number;
  cancelled: number;
  agents: AgentStatsResponse[];
  recent_tasks: RecentTaskSummary[];
}

export interface AgentMetrics {
  agent_type: AgentType;
  total_tasks: number;
  completed: number;
  failed: number;
  avg_confidence: number | null;
  avg_processing_time_seconds: number | null;
  tasks_by_day: Array<{ date: string; count: number }>;
}

// ─── WebSocket Events ────────────────────────────────────────────────────────

export interface WsTaskStatusChanged {
  event: "task_status_changed";
  data: {
    task_id: string;
    agent_type: AgentType;
    status: TaskStatus;
    [key: string]: unknown;
  };
}

export interface WsPong {
  event: "pong";
}

export type WsMessage = WsTaskStatusChanged | WsPong;

// ─── Audit ───────────────────────────────────────────────────────────────────

export interface AuditLogEntry {
  id: string;
  actor_id: string | null;
  actor_type: string | null;
  action: string | null;
  resource_type: string | null;
  resource_id: string | null;
  details: Record<string, unknown> | null;
  phi_accessed: boolean | null;
  ip_address: string | null;
  timestamp: string | null;
}

export interface AuditLogList {
  items: AuditLogEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditFilterOptionsResponse {
  actions: string[];
  resource_types: string[];
}

export interface PHIAccessItem {
  id: string;
  timestamp: string | null;
  user_id: string;
  patient_id: string | null;
  access_type: string | null;
  resource_type: string | null;
  resource_id: string | null;
  reason: string | null;
  phi_fields_accessed: string[] | null;
}

export interface PHIAccessListResponse {
  items: PHIAccessItem[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Backend schema name aliases.
 *
 * The backend Pydantic schemas use `AuditLogItem` / `AuditLogListResponse`
 * while the frontend historically uses `AuditLogEntry` / `AuditLogList`.
 * These aliases keep both names importable and enforce the mapping in the
 * schema-parity test.
 */
export type AuditLogItem = AuditLogEntry;
export type AuditLogListResponse = AuditLogList;

// ─── Eligibility Agent ───────────────────────────────────────────────────────

export interface EligibilityRequest {
  subscriber_id: string;
  subscriber_first_name: string;
  subscriber_last_name: string;
  subscriber_dob?: string | null;
  payer_id?: string | null;
  payer_name?: string | null;
  provider_npi?: string | null;
  provider_first_name?: string | null;
  provider_last_name?: string | null;
  date_of_service?: string | null;
  service_type_code?: string;
  patient_id?: string | null;
  organization_id?: string | null;
  /** Test flag: force confidence to 0.3 to trigger HITL review */
  force_low_confidence?: boolean;
  /** Test flag: simulate clearinghouse connection failure */
  force_clearinghouse_error?: boolean;
}

export interface EligibilityCoverageDetail {
  active: boolean;
  effective_date?: string | null;
  termination_date?: string | null;
  plan_name?: string | null;
  group_number?: string | null;
  coverage_type?: string | null;
}

export interface EligibilityResult {
  coverage_active: boolean;
  coverage_details: Record<string, unknown>;
  benefits: Array<Record<string, unknown>>;
  subscriber: Record<string, unknown>;
  payer: Record<string, unknown>;
  confidence: number;
  needs_review: boolean;
  review_reason: string;
  transaction_id: string;
}

// ─── Scheduling Agent ────────────────────────────────────────────────────────

export interface SchedulingRequest {
  request_text?: string | null;
  patient_id?: string | null;
  patient_first_name?: string | null;
  patient_last_name?: string | null;
  provider_npi?: string | null;
  provider_name?: string | null;
  specialty?: string | null;
  preferred_date_start?: string | null;
  preferred_date_end?: string | null;
  preferred_time_of_day?: string;
  urgency?: string;
  visit_type?: string;
  duration_minutes?: number;
  notes?: string;
  payer_id?: string | null;
  payer_name?: string | null;
  organization_id?: string | null;
}

export interface SchedulingSlot {
  slot_id: string;
  fhir_id?: string;
  start: string;
  end: string;
  status?: string;
  provider_npi?: string;
  provider_name?: string;
  specialty?: string;
  location?: string;
  duration_minutes?: number;
}

export interface SchedulingResult {
  appointment_id?: string | null;
  fhir_appointment_id?: string | null;
  slot?: SchedulingSlot | null;
  alternatives: SchedulingSlot[];
  parsed_intent: Record<string, unknown>;
  waitlist_id?: string | null;
  waitlist_position?: number | null;
  status: string;
  confidence: number;
  needs_review: boolean;
  review_reason: string;
}

// ─── Claims Agent ────────────────────────────────────────────────────────────

export interface ClaimsRequest {
  subscriber_id: string;
  subscriber_first_name: string;
  subscriber_last_name: string;
  subscriber_dob?: string | null;
  subscriber_gender?: string;
  payer_id?: string | null;
  payer_name?: string | null;
  billing_provider_npi?: string | null;
  billing_provider_name?: string | null;
  billing_provider_tax_id?: string | null;
  diagnosis_codes: string[];
  procedure_codes?: string[];
  service_lines?: Array<Record<string, string>> | null;
  total_charge?: string;
  date_of_service?: string | null;
  place_of_service?: string;
  claim_type?: string;
  patient_id?: string | null;
  encounter_id?: string | null;
  organization_id?: string | null;
  /** Linked eligibility task ID for cross-agent tracing */
  eligibility_task_id?: string | null;
}

export interface CodeValidationResult {
  code: string;
  valid: boolean;
  description: string;
  error?: string | null;
  warning?: string | null;
}

export interface DenialAnalysis {
  denial_code: string;
  denial_reason: string;
  category: string;
  category_description: string;
  appeal_recommendation: Record<string, unknown>;
  claim_id: string;
}

export interface ClaimsResult {
  claim_id: string;
  claim_type: string;
  submission_status: string;
  dx_validation: Record<string, unknown>;
  cpt_validation: Record<string, unknown>;
  payment_info?: Record<string, unknown> | null;
  denial_analyses: DenialAnalysis[];
  total_charge: string;
  total_paid: string;
  patient_responsibility: string;
  confidence: number;
  needs_review: boolean;
  review_reason: string;
}

// ─── Prior Authorization Agent ───────────────────────────────────────────────

export interface PriorAuthRequest {
  procedure_code: string;
  procedure_description?: string;
  diagnosis_codes?: string[];
  subscriber_id: string;
  subscriber_first_name: string;
  subscriber_last_name: string;
  subscriber_dob?: string;
  payer_id: string;
  payer_name?: string;
  provider_npi?: string;
  provider_name?: string;
  patient_id: string;
  date_of_service?: string;
  place_of_service?: string;
  submission_channel?: string;
  fhir_base_url?: string;
  payer_policy_reference?: string;
  /** Linked claims task ID for cross-agent tracing */
  related_claim_task_id?: string | null;
}

export interface PriorAuthResponse {
  task_id: string;
  status: string;
  pa_required: boolean;
  procedure_code: string;
  authorization_number: string;
  determination: string;
  submission_channel: string;
  confidence: number;
  needs_review: boolean;
  review_reason: string;
  clinical_summary: Record<string, unknown>;
  appeal_letter?: string | null;
  effective_date?: string | null;
  expiration_date?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface PriorAuthAppealResponse {
  id: string;
  prior_auth_id: string;
  appeal_level: number;
  status: string;
  appeal_letter?: string | null;
  clinical_evidence?: Record<string, unknown> | null;
  outcome?: string | null;
  outcome_details?: Record<string, unknown> | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// ─── Credentialing Agent ─────────────────────────────────────────────────────

export interface CredentialingRequest {
  provider_npi: string;
  target_organization?: string;
  target_payer_id?: string;
  credentialing_type?: string;
  state?: string;
  patient_id?: string | null;
  organization_id?: string | null;
}

export interface CredentialingResponse {
  task_id: string;
  status: string;
  provider_npi: string;
  provider_name: string;
  target_organization: string;
  documents_complete: boolean;
  missing_documents: string[];
  sanctions_clear: boolean;
  license_verified: boolean;
  tracking_number: string;
  confidence: number;
  needs_review: boolean;
  review_reason: string;
  submitted_date?: string | null;
  approved_date?: string | null;
  expiration_date?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// ─── Compliance Agent ────────────────────────────────────────────────────────

export interface ComplianceRequest {
  organization_id: string;
  measure_set?: string;
  reporting_period_start: string;
  reporting_period_end: string;
  measure_ids?: string[] | null;
  patient_id?: string | null;
}

export interface ComplianceResponse {
  task_id: string;
  status: string;
  measure_set: string;
  reporting_period: string;
  overall_score?: number | null;
  total_measures: number;
  measures_met: number;
  measures_not_met: number;
  measure_scores: Record<string, unknown>;
  total_gaps: number;
  gap_details: Array<Record<string, unknown>>;
  recommendations: Array<Record<string, unknown>>;
  confidence: number;
  needs_review: boolean;
  review_reason: string;
  created_at?: string | null;
  updated_at?: string | null;
}
