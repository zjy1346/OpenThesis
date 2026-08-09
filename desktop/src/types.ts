export type Language = "zh-CN" | "en";

export type Preferences = {
  ui_language: Language;
  report_language: Language;
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
  label_en: string;
  exchanges: string[];
  default_currency: string;
  requires_sec_identity: boolean;
  disclosure_home: string;
};

export type ModelPreset = {
  preset_id: string;
  label: string;
  region: string;
  protocol: string;
  base_url: string;
  recommended_models: string[];
  models_path: string | null;
  help_url: string;
  requires_api_key: boolean;
  temperature: number | null;
};

export type ResearchPackSummary = {
  pack_id: string;
  name: string;
  version: string;
  content_hash: string;
};

export type ModelSelection = {
  preset_id: string;
  model: string;
  base_url: string;
  api_key: string;
  timeout_seconds?: number;
};

export type ResearchRequest = {
  mode: "demo" | "company";
  company?: Company;
  download_filings?: boolean;
  pack_id?: string;
  model?: ModelSelection;
  compare_enabled?: boolean;
  comparison_model?: ModelSelection;
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
  model_catalog: ModelPreset[];
  research_packs: ResearchPackSummary[];
  interrupted_runs: number;
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
  industry_support?: "standard" | "financial_beta";
  market_snapshot?: ResearchRequest["market_snapshot"] | null;
  retryable_synthesis?: boolean;
  markdown: string;
  html: string;
};

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
  error_code?: string | null;
  market?: Market | null;
  disclosure_url?: string | null;
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
