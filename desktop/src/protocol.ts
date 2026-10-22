import type {
  BootstrapResult,
  Company,
  CompanySearchResponse,
  Language,
  Market,
  ModelSelection,
  ModelReference,
  OtCompileResult,
  OtDraft,
  OtSuggestion,
  OtValidationResult,
  Preferences,
  ResearchJob,
  ResearchPackSummary,
  ResearchReport,
  ResearchRequest,
  ThesisVersion,
  FinancialDiagnostics,
  MarketSnapshotPreview,
  ModelCallHistoryPage,
  ModelCallPlan,
} from "./types";

/**
 * The platform-neutral JSON-RPC contract shared by every desktop adapter.
 *
 * Tauri is deliberately absent from this module. A future macOS adapter can
 * satisfy the same interface without importing or branching the React code.
 */
export const BACKEND_METHODS = [
  "app.bootstrap",
  "settings.update",
  "company.search",
  "packs.install",
  "ot.validate",
  "ot.compile",
  "ot.suggest",
  "thesis.list",
  "thesis.get",
  "thesis.save",
  "research.list",
  "research.delete",
  "research.get_report",
  "research.call_history",
  "research.extend_budget",
  "rpc.start",
  "rpc.result",
  "research.financial_diagnostics",
    "research.start",
  "research.market_snapshot",
  "research.retry_growth",
  "research.retry_stage",
  "research.revalidate_saved",
  "research.retry_model_stages",
  "research.retry_financials",
  "research.refresh_financial_report",
  "research.start_financial_retry",
  "research.start_financial_rebuild",
  "research.rebuild_financials",
  "research.retry_synthesis",
  "research.status",
  "research.cancel",
  "research.cancel_model_call",
  "research.vision_decision",
] as const;

export type BackendMethod = typeof BACKEND_METHODS[number];

type EmptyParams = Record<string, never>;

export type BackendParams = {
  "app.bootstrap": { minimal?: boolean };
  "settings.update": {
    preferences: Partial<Pick<Preferences, "ui_language" | "report_language" | "sidebar_collapsed" | "parallel_agents" | "research_market" | "official_network_policy">>;
  };
  "company.search": { query: string; market: Market; request_id?: string };
  "packs.install": { filename: string; data_base64: string };
  "ot.validate": { draft: OtDraft };
  "ot.compile": { draft: OtDraft };
  "ot.suggest": { draft: OtDraft; selected_path: string; instruction: string; model: ModelReference };
  "thesis.list": EmptyParams;
  "thesis.get": { thesis_version_id: string };
  "thesis.save": { company_cik: string; content: Record<string, unknown>; company?: Company; base_thesis_version_id?: string };
  "research.list": EmptyParams;
  "research.delete": { run_id: string };
  "research.get_report": {
    run_id: string;
    language?: Language;
    include_technical?: boolean;
  };
  "rpc.start": { method: string; params: Record<string, unknown> };
  "rpc.result": { job_id: string };
  "research.financial_diagnostics": { run_id: string };
  "research.start": ResearchRequest;
  "research.market_snapshot": { company: Company; valuation?: ResearchRequest["valuation"] };
  "research.retry_growth": { run_id: string; model: ModelSelection };
  "research.retry_stage": { run_id: string; target: string; model?: ModelSelection; plan_hash: string; confirm_new_attempt?: boolean };
  "research.revalidate_saved": { run_id: string };
  "research.retry_model_stages": { run_id: string; model?: ModelSelection; budget_only?: boolean };
  "research.call_history": { run_id: string; stage_id: string; after?: string; limit?: number };
  "research.extend_budget": { run_id: string; plan_digest: string; gap_ids: string[] };
  "research.retry_financials": { run_id: string };
  "research.refresh_financial_report": { run_id: string; language?: Language };
  "research.start_financial_retry": { run_id: string };
  "research.start_financial_rebuild": { run_id: string; confirmed: boolean };
  "research.rebuild_financials": { run_id: string; confirmed: boolean };
    "research.retry_synthesis": { run_id: string; model?: ModelSelection };
  "research.status": { job_id: string };
  "research.cancel": { job_id: string };
  "research.cancel_model_call": { job_id: string; request_id: string; attempt_id: string };
  "research.vision_decision": { job_id: string; approved: boolean };
};

export type BackendResult = {
  "app.bootstrap": BootstrapResult;
  "settings.update": Preferences;
  "company.search": CompanySearchResponse;
  "packs.install": ResearchPackSummary;
  "ot.validate": OtValidationResult;
  "ot.compile": OtCompileResult;
  "ot.suggest": OtSuggestion;
  "thesis.list": ThesisVersion[];
  "thesis.get": ThesisVersion;
  "thesis.save": ThesisVersion;
  "research.list": BootstrapResult["recent_runs"];
  "research.delete": { run_id: string; deleted: boolean };
  "research.get_report": ResearchReport;
  "research.call_history": ModelCallHistoryPage;
  "research.extend_budget": ModelCallPlan;
  "rpc.start": { job_id: string };
  "rpc.result": { state: "pending" | "ready"; response?: { result?: unknown; error?: { code: number; message: string } } };
  "research.financial_diagnostics": FinancialDiagnostics;
  "research.start": ResearchJob;
  "research.market_snapshot": MarketSnapshotPreview;
  "research.retry_growth": ResearchReport;
  "research.retry_stage": ResearchReport;
  "research.revalidate_saved": ResearchReport;
  "research.retry_model_stages": ResearchReport;
  "research.retry_financials": ResearchReport;
  "research.refresh_financial_report": ResearchReport;
  "research.start_financial_retry": ResearchJob;
  "research.start_financial_rebuild": ResearchJob;
  "research.rebuild_financials": ResearchReport;
  "research.retry_synthesis": ResearchReport;
  "research.status": ResearchJob;
  "research.cancel": ResearchJob;
  "research.cancel_model_call": ResearchJob;
  "research.vision_decision": ResearchJob;
};

export type BackendRequest<M extends BackendMethod = BackendMethod> = {
  method: M;
  params: BackendParams[M];
};

export type BackendResponse<M extends BackendMethod = BackendMethod> = BackendResult[M];
