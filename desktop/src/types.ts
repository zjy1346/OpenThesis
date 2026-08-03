export type Language = "zh-CN" | "en";

export type Preferences = {
  ui_language: Language;
  report_language: Language;
  sidebar_collapsed: string;
  parallel_agents: string;
  [key: string]: string;
};

export type Company = {
  cik: string;
  ticker: string;
  name: string;
  exchange: string;
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
};

export type ResearchRunSummary = {
  run_id: string;
  ticker: string;
  company_name: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  report_language: Language;
};

export type BootstrapResult = {
  contract_version: string;
  app_version: string;
  capabilities: string[];
  preferences: Preferences;
  recent_runs: ResearchRunSummary[];
  common_companies: Company[];
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
