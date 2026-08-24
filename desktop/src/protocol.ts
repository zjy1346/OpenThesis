import type {
  BootstrapResult,
  Company,
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
  "research.start",
  "research.retry_growth",
  "research.retry_synthesis",
  "research.status",
  "research.cancel",
  "research.vision_decision",
] as const;

export type BackendMethod = typeof BACKEND_METHODS[number];

type EmptyParams = Record<string, never>;

export type BackendParams = {
  "app.bootstrap": EmptyParams;
  "settings.update": {
    preferences: Partial<Pick<Preferences, "ui_language" | "report_language" | "sidebar_collapsed" | "parallel_agents" | "research_market">>;
  };
  "company.search": { query: string; market: Market };
  "packs.install": { filename: string; data_base64: string };
  "ot.validate": { draft: OtDraft };
  "ot.compile": { draft: OtDraft };
  "ot.suggest": { draft: OtDraft; selected_path: string; instruction: string; model: ModelReference };
  "thesis.list": EmptyParams;
  "thesis.get": { thesis_version_id: string };
  "thesis.save": { company_cik: string; content: Record<string, unknown> };
  "research.list": EmptyParams;
  "research.delete": { run_id: string };
  "research.get_report": {
    run_id: string;
    language?: Language;
    include_technical?: boolean;
  };
  "research.start": ResearchRequest;
  "research.retry_growth": { run_id: string; model: ModelSelection };
  "research.retry_synthesis": { run_id: string; model: ModelSelection };
  "research.status": { job_id: string };
  "research.cancel": { job_id: string };
  "research.vision_decision": { job_id: string; approved: boolean };
};

export type BackendResult = {
  "app.bootstrap": BootstrapResult;
  "settings.update": Preferences;
  "company.search": Company[];
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
  "research.start": ResearchJob;
  "research.retry_growth": ResearchReport;
  "research.retry_synthesis": ResearchReport;
  "research.status": ResearchJob;
  "research.cancel": ResearchJob;
  "research.vision_decision": ResearchJob;
};

export type BackendRequest<M extends BackendMethod = BackendMethod> = {
  method: M;
  params: BackendParams[M];
};

export type BackendResponse<M extends BackendMethod = BackendMethod> = BackendResult[M];
