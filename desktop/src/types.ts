export type Language = "zh-CN" | "zh-Hant" | "en";

export type UiLanguageMode = "system" | "manual";

export type Preferences = {
  ui_language: Language;
  report_language: Language;
  ui_language_mode?: UiLanguageMode;
  sidebar_collapsed: string;
  parallel_agents: string;
  research_market?: Market;
  [key: string]: string | undefined;
};

export type Company = {
  cik: string;
  ticker: string;
  name: string;
  exchange: string;
  issuer_id?: string;
  market?: Market;
  security_id?: string;
  listing_currency?: string;
  reporting_currency?: string;
  accounting_standard?: string;
  industry?: string;
  industry_support?: "standard" | "financial_beta";
  source_url?: string;
};

export type Market = "US" | "CN_A" | "HK";

export type MarketProfile = {
  market: Market;
  label_zh: string;
  label_zh_hant?: string;
  label_en: string;
  exchanges: string[];
  default_currency: string;
  requires_sec_identity: boolean;
  disclosure_home: string;
};

export type OtDiagnostic = {
  code: string;
  severity: "error" | "warning" | "info";
  path: string;
  message: string;
};

export type OtDraft = {
  package: {
    id: string;
    name: string;
    version: string;
    kind: string;
    description: string;
    license: string;
  };
  settings: {
    horizon_years: number;
    evidence_policy?: { annual_history_years: number };
    depth: number;
    risk_emphasis: number;
    report_language: Language;
    [key: string]: unknown;
  };
  workflow: {
    steps: Array<{
      id: string;
      role: string;
      depends_on: string[];
      prompt: string;
      output_schema: string;
    }>;
  };
  outputs: {
    formats: string[];
    include_evidence: boolean;
    deterministic_transforms: string[];
  };
  ui: Record<string, unknown>;
  model_requirements: {
    capabilities: string[];
    preferred_profile_alias: string | null;
  };
  dependencies: unknown[];
  relationships: unknown[];
  optional_extensions: Record<string, unknown>;
};

export type OtValidationResult = {
  valid: boolean;
  diagnostics: OtDiagnostic[];
};

export type OtCompileResult = OtValidationResult & {
  filename?: string;
  data_base64?: string;
  content_identity?: string;
  manifest?: Record<string, unknown>;
};

export type OtSuggestion = {
  accepted: boolean;
  path: string;
  before: unknown;
  after: unknown;
  diagnostics: OtDiagnostic[];
};
export type ResearchPackSummary = {
  pack_id: string;
  name: string;
  version: string;
  content_hash: string;
};

export type ModelRole = "primary" | "comparison" | "verification" | "vision" | "ot_assistant";

export type ModelReference = {
  configured_model_id: string;
  configuration_version: number;
  role: ModelRole;
};

export type ModelSelection = ModelReference;

export type ProviderModelPreset = {
  model_id: string;
  alias: string;
  capabilities: string[];
  billing_class: string;
};

export type ProviderDefinition = {
  provider_id: string;
  display_name: string;
  category: "cloud" | "local" | "custom";
  region: string;
  base_url: string;
  help_url: string;
  requires_api_key: boolean;
  supports_discovery: boolean;
  free_hint: boolean;
  allowed_hosts: string[];
  recommended_models?: ProviderModelPreset[];
  models_path?: string | null;
  default_test_model_id?: string | null;
};

export type ProviderConnectionSummary = {
  connection_id: string;
  provider_id: string;
  display_name: string;
  region: string;
  endpoint: string;
  credential_version: number;
  has_secret: boolean;
  enabled: boolean;
  status?: string;
  last_tested_at?: number | null;
  last_error_code?: string | null;
  configuration_version?: number;
};

export type ConfiguredModelSummary = {
  configured_model_id: string;
  connection_id: string;
  model_id: string;
  alias: string;
  free_tier: boolean;
  billing_class: "local_no_provider_fee" | "free_tier" | "paid" | "unknown";
  free_source_url: string | null;
  free_verified_at: number | null;
  enabled: boolean;
  capabilities: string[];
  health_status: "unverified" | "ready" | "error" | string;
  last_discovered_at: number | null;
  context_window_hint: number | null;
  temperature: number | null;
  timeout_seconds: number;
  configuration_version: number;
};

export type DiscoveredModel = {
  model_id: string;
  alias: string;
  billing_class: string;
  capabilities: string[];
};

export type VisionFallbackSelection = {
  enabled: boolean;
  consent: boolean;
  provider: "mineru_flash" | "configured_model";
  model?: ModelReference;
  language?: "auto" | "ch" | "en";
  require_page_approval: true;
};

export type ResearchRequest = {
  mode: "demo" | "company";
  company?: Company;
  download_filings?: boolean;
  pack_id?: string;
  model?: ModelSelection;
  compare_enabled?: boolean;
  comparison_models?: ModelSelection[];
  parallel_agents?: boolean;
  valuation?: {
    market_cap_billions: number;
    discount_rate_percent: number;
    terminal_growth_percent: number;
  };
  market_snapshot?: {
    price: number;
    market_cap_billions: number;
    currency: string;
    as_of: string;
  };
  vision_fallback?: VisionFallbackSelection;
};

export type ResearchRunSummary = {
  run_id: string;
  ticker: string;
  company_name: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  report_language: Language;
  market?: Market;
  exchange: string;
  listing_currency?: string;
  reporting_currency?: string;
  industry_support?: "standard" | "financial_beta";
};

export type BootstrapResult = {
  contract_version: string;
  app_version: string;
  capabilities: string[];
  preferences: Preferences;
  recent_runs: ResearchRunSummary[];
  common_companies: Company[];
  market_catalog?: MarketProfile[];
  research_packs: ResearchPackSummary[];
  interrupted_runs: number;
};

export type FinancialRetryResult = {
  mode: "retry" | "rebuild" | string;
  targets: string[];
  downloaded: string[];
  accepted: string[];
  rejected: string[];
  status: "succeeded" | "partial" | "failed" | string;
  error: string;
  updated_artifacts: string[];
};

export type ResearchReport = {
  run_id: string;
  ticker: string;
  company_name: string;
  status: string;
  report_language: Language;
  market?: Market;
  exchange?: string;
  listing_currency?: string;
  reporting_currency?: string;
  industry_support?: "standard" | "financial_beta";
  market_snapshot?: ResearchRequest["market_snapshot"] | null;
  retryable_synthesis?: boolean;
  retryable_growth?: boolean;
  financial_retry?: FinancialRetryResult;
  financial_report_refresh?: {
    status: "succeeded" | "failed" | string;
    updated_artifacts?: string[];
  };
  financial_status?: {
    state: "complete" | "incomplete" | "warning" | "unavailable";
    retryable: boolean;
    history_years: number;
    expected_periods: string[];
    available_periods: string[];
    missing_periods: string[];
    unverified_periods?: string[];
    nodes: Array<{ period: string; state: string; comparison_only: boolean }>;
    issues: Array<{ period: string; stage: string; code: string; status: string }>;
    attempt_count: number;
    last_stage: string;
    last_error: string;
    updated_at: string;
    next_action: string;
    model_calls: number;
    token_delta: number;
    snapshot_stale?: boolean;
  };
  reproducibility?: {
    model_configuration: Record<string, unknown>;
    research_configuration: Record<string, unknown>;
    data_snapshot: Record<string, unknown>;
  };  markdown: string;
  html: string;
};

export type FilingProgressStatus =
  | "queued"
  | "cache-check"
  | "cache-hit"
  | "indexing"
  | "local-parsing"
  | "local-validating"
  | "cloud-awaiting-approval"
  | "cloud-processing"
  | "canonical-compiling"
  | "validated"
  | "blocked"
  | "failed"
  | "cancelled"
  | "parsed"
  | (string & {});

export type ResearchJob = {
  job_id: string;
  state: "queued" | "running" | "cancelling" | "completed" | "failed" | "cancelled";
  message: string;
  percent: number;
  run_id: string | null;
  stage?: string;
  agent_states?: Record<string, string>;
  completed_agents?: number;
  total_agents?: number;
  cancel_requested?: boolean;
  elapsed_seconds?: number;
  engine_active_seconds?: number;
  external_wait_seconds?: number;
  stage_elapsed_seconds?: number;
  stage_timings?: Record<string, number>;
  stage_current?: number | null;
  stage_total?: number | null;
  filing_states?: Record<string, {
    filing_id: string;
    label: string;
    status: FilingProgressStatus;
    error_code?: string;
    elapsed_seconds?: number;
  }>;
  error_code?: string | null;
  market?: Market | null;
  disclosure_url?: string | null;
  vision_upload_preview?: {
    provider: string;
    pages: number[];
    total_bytes: number;
    source_document?: string;
    filing_hash?: string;
    document_hashes?: string[];
  } | null;
  vision_approval_pending?: boolean;
  vision_approval?: boolean | null;
  operation_result?: FinancialRetryResult | null;
};

export type ThesisVersion = {
  thesis_version_id: string;
  company_cik: string;
  run_id: string | null;
  version: number;
  content: Record<string, unknown>;
  created_at: string;
  created_by: string;
  ticker: string;
  name: string;
};
