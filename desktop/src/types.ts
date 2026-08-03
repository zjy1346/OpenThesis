export type Preferences = {
  ui_language: "zh-CN" | "en";
  report_language: "zh-CN" | "en";
  sidebar_collapsed: string;
  [key: string]: string;
};

export type ResearchRunSummary = {
  run_id: string;
  ticker: string;
  company_name: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  report_language: "zh-CN" | "en";
};

export type BootstrapResult = {
  contract_version: string;
  app_version: string;
  capabilities: string[];
  preferences: Preferences;
  recent_runs: ResearchRunSummary[];
};

export type ResearchReport = {
  run_id: string;
  ticker: string;
  company_name: string;
  status: string;
  report_language: "zh-CN" | "en";
  markdown: string;
};

export type ResearchJob = {
  job_id: string;
  state: "queued" | "running" | "cancelling" | "completed" | "failed" | "cancelled";
  message: string;
  percent: number;
  run_id: string | null;
};
