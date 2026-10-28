import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import type {
  BootstrapResult,
  Company,
  CompanySearchResponse,
  ConfiguredModelSummary,
  DiscoveredModel,
  ModelReference,
  OtCompileResult,
  OtDraft,
  OtSuggestion,
  OtValidationResult,
  ModelSelection,
  Market,
  Preferences,
  ProviderConnectionSummary,
  ProviderDefinition,
  ResearchJob,
  StructureRuntimeStatus,
  ResearchReport,
  RecoveryStageTarget,
  ResearchRequest,
  ResearchPackSummary,
  ThesisVersion,
  FinancialDiagnostics,
  ResearchDiagnostics,
  ResearchBudgetPreview,
  AdaptiveResearchRunEnvelope,
  MarketSnapshotPreview,
  ModelCallHistoryPage,
  ModelCallPlan,
  ConnectionCapabilityProfile,
  ConnectionTransportSettings,
  ModelTestEvent,
  ModelDirectory,
} from "./types";
import type {
  BackendMethod,
  BackendParams,
  BackendResponse,
} from "./protocol";

async function request<M extends BackendMethod>(
  method: M,
  params: BackendParams[M],
): Promise<BackendResponse<M>> {
  const minimalBootstrap = method === "app.bootstrap" && (params as { minimal?: boolean }).minimal === true;
  if (!DEFERRED_METHODS.has(method) || minimalBootstrap) return invoke<BackendResponse<M>>("backend_request", { method, params });
  const job = await invoke<{ job_id: string }>("backend_request", { method: "rpc.start", params: { method, params } });
  if (!job || typeof job.job_id !== "string" || !job.job_id) throw new Error("CORE_OPERATION_PROTOCOL");
  const started = performance.now();
  while (performance.now() - started < 600_000) {
    const status = await invoke<{ state: string; response?: { result?: BackendResponse<M>; error?: { message?: string } } }>(
      "backend_request", { method: "rpc.result", params: { job_id: job.job_id } });
    if (!status || !["pending", "ready"].includes(status.state)) throw new Error("CORE_OPERATION_PROTOCOL");
    if (status.state === "ready") {
      if (status.response?.error) throw new Error(status.response.error.message || "CORE_OPERATION_FAILED");
      if (!status.response || !("result" in status.response)) throw new Error("CORE_OPERATION_PROTOCOL");
      return status.response.result as BackendResponse<M>;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
  throw new Error("CORE_OPERATION_DEADLINE");
}

const DEFERRED_METHODS = new Set<BackendMethod>([
  "app.bootstrap", "research.get_report", "research.financial_diagnostics", "research.retry_synthesis", "research.retry_model_stages",
  "research.retry_growth", "research.retry_stage", "research.revalidate_saved", "research.refresh_financial_report",
  "research.retry_financials", "research.rebuild_financials", "research.market_snapshot", "company.search", "ot.suggest", "packs.install",
]);

export function bootstrapBackend(minimal = false): Promise<BootstrapResult> {
  return request("app.bootstrap", minimal ? { minimal: true } : {});
}

export function getBackendInitializationStatus(): Promise<{
  phase: "idle" | "starting" | "ready" | "failed";
  elapsed_seconds: number; deadline_seconds: number; safe_failure_code: string | null;
  stage?: string | null;
  completed_units?: number | null;
  total_units?: number | null;
}> {
  return invoke("backend_initialization_status");
}

export function getResearchReport(
  runId: string,
  language?: "zh-CN" | "zh-Hant" | "en",
  includeTechnical = false,
): Promise<ResearchReport> {
  return request("research.get_report", {
    run_id: runId,
    ...(language ? { language } : {}),
    ...(includeTechnical ? { include_technical: true } : {}),
  }).then((report) => {
    if (report.run_id !== runId) throw new Error("REPORT_IDENTITY_MISMATCH");
    if (
      report.report_contract_version !== "1"
      || typeof report.report_input_generation !== "string"
      || typeof report.is_substantive !== "boolean"
      || !Array.isArray(report.visible_sections)
      || typeof report.report_read_state !== "string"
      || !["ready", "partial", "diagnostic_only"].includes(report.report_read_state)
    ) {
      throw new Error("REPORT_CONTRACT_MISMATCH");
    }
    return report;
  });
}

export function updatePreferences(
  preferences: Partial<Pick<Preferences, "ui_language" | "report_language" | "sidebar_collapsed" | "parallel_agents" | "research_market" | "official_network_policy" | "local_structure_enabled">>,
): Promise<Preferences> {
  return request("settings.update", { preferences });
}

export function getStructureStatus(): Promise<StructureRuntimeStatus> {
  return request("structure.status", {});
}

export function startStructureInstall(pythonExecutable: string): Promise<ResearchJob> {
  return request("structure.install", { python_executable: pythonExecutable, confirmed: true });
}

export async function searchCompanies(query: string, market: Market, requestId: string): Promise<CompanySearchResponse> {
  const response = await request("company.search", { query, market, request_id: requestId });
  if (
    !response
    || response.contract !== "openthesis.company-search.v2"
    || response.query !== query
    || response.market !== market
    || response.request_id !== requestId
    || !["ready", "empty", "degraded", "unavailable"].includes(response.state)
    || !Array.isArray(response.companies)
    || !Array.isArray(response.diagnostics)
    || response.diagnostics.some((item) => !item || typeof item.code !== "string" || typeof item.diagnostic_id !== "string")
    || response.companies.some((company) => !company || !["official_verified", "cached_verified", "unresolved_code"].includes(company.identity_state ?? ""))
  ) {
    throw new Error("COMPANY_SEARCH_CONTRACT_MISMATCH");
  }
  return response;
}

export function captureMarketSnapshot(
  company: Company,
  valuation?: ResearchRequest["valuation"],
): Promise<MarketSnapshotPreview> {
  return request("research.market_snapshot", { company, ...(valuation ? { valuation } : {}) });
}

export function listModelProviders(): Promise<ProviderDefinition[]> {
  return invoke<ProviderDefinition[]>("model_center_list_providers");
}

export function getFinancialDiagnostics(runId: string): Promise<FinancialDiagnostics> {
  return request("research.financial_diagnostics", { run_id: runId });
}

export function getResearchDiagnostics(runId: string): Promise<ResearchDiagnostics> {
  return invoke<ResearchDiagnostics>("backend_request", { method: "research.diagnostics", params: { run_id: runId } }).then((diagnostics) => {
    if (diagnostics?.schema !== "openthesis.research-diagnostics.v1" || diagnostics.run?.run_id !== runId) {
      throw new Error("RESEARCH_DIAGNOSTICS_CONTRACT_MISMATCH");
    }
    return diagnostics;
  });
}

export function getResearchBudgetPreview(runEnvelope?: AdaptiveResearchRunEnvelope): Promise<ResearchBudgetPreview> {
  return invoke<ResearchBudgetPreview>("backend_request", {
    method: "research.budget_preview",
    params: runEnvelope ? { run_envelope: runEnvelope } : {},
  }).then((preview) => {
    if (preview?.schema !== "openthesis.research-budget-preview.v1"
        || !preview.user_envelope
        || !(preview.estimated_cost === null || typeof preview.estimated_cost === "number" && Number.isFinite(preview.estimated_cost))
        || typeof preview.price_status !== "string"
        || typeof preview.workload_status !== "string") {
      throw new Error("RESEARCH_BUDGET_PREVIEW_CONTRACT_MISMATCH");
    }
    return preview;
  });
}

export function listProviderConnections(): Promise<ProviderConnectionSummary[]> {
  return invoke<ProviderConnectionSummary[]>("model_center_list_connections");
}

export function saveProviderConnection(input: {
  protocol?: "chat_completions" | "ollama";
  capability_profile?: ConnectionCapabilityProfile;
  transport?: ConnectionTransportSettings;
  connection_id: string;
  provider_id: string;
  display_name: string;
  region: string;
  endpoint: string;
  enabled: boolean;
}): Promise<ProviderConnectionSummary> {
  return invoke<ProviderConnectionSummary>("model_center_save_connection", { input });
}

export function setProviderConnectionSecret(
  connectionId: string,
  secret: string,
): Promise<ProviderConnectionSummary> {
  return invoke<ProviderConnectionSummary>("model_center_set_connection_secret", { connectionId, secret });
}

export function rotateProviderConnectionSecret(
  connectionId: string,
  secret: string,
): Promise<ProviderConnectionSummary> {
  return invoke<ProviderConnectionSummary>("model_gateway_rotate_connection_secret", { connectionId, secret });
}

export function setProviderConnectionEnabled(
  connectionId: string,
  enabled: boolean,
): Promise<ProviderConnectionSummary> {
  return invoke<ProviderConnectionSummary>("model_center_set_connection_enabled", { connectionId, enabled });
}

export function deleteProviderConnection(connectionId: string): Promise<void> {
  return invoke<void>("model_center_delete_connection", { connectionId });
}

export function listConfiguredModels(): Promise<ConfiguredModelSummary[]> {
  return invoke<ConfiguredModelSummary[]>("model_center_list_configured_models");
}

export function saveConfiguredModel(input: {
  configured_model_id: string;
  connection_id: string;
  model_id: string;
  alias: string;
  enabled: boolean;
  capabilities: string[];
  timeout_seconds?: number;
  context_window_hint?: number | null;
  temperature?: number | null;
}): Promise<ConfiguredModelSummary> {
  return invoke<ConfiguredModelSummary>("model_center_save_configured_model", { input });
}

export function deleteConfiguredModel(configuredModelId: string): Promise<void> {
  return invoke<void>("model_center_delete_configured_model", { configuredModelId });
}

export function discoverConnectionModels(connectionId: string): Promise<DiscoveredModel[]> {
  return invoke<DiscoveredModel[]>("model_gateway_discover_connection", { connectionId });
}
export function getModelDirectory(refresh = false): Promise<ModelDirectory> {
  return invoke<ModelDirectory>("model_center_model_directory", { refresh });
}

export function testProviderConnection(connectionId: string, modelId?: string): Promise<{ ok: boolean; message: string }> {
  return invoke<{ ok: boolean; message: string }>("model_gateway_test_connection", { connectionId, modelId });
}

export function testConfiguredModel(configuredModelId: string, testVision = false, operationId?: string): Promise<{ ok: boolean; message: string; capabilities?: { capability: string; status: string; error_code?: string | null; retry_after_seconds?: number | null }[] }> {
  return invoke("model_gateway_test_model", { configuredModelId, testVision, ...(operationId ? { operationId } : {}) });
}

export function subscribeModelTestEvents(handler: (event: ModelTestEvent) => void): Promise<() => void> {
  return listen<ModelTestEvent>("openthesis-model-test", (event) => handler(event.payload));
}

export function cancelModelTest(operationId: string): Promise<void> {
  return invoke("model_gateway_cancel_test", { operationId });
}

export function validateOtDraft(draft: OtDraft): Promise<OtValidationResult> {
  return request("ot.validate", { draft });
}

export function compileOtDraft(draft: OtDraft): Promise<OtCompileResult> {
  return request("ot.compile", { draft });
}

export function suggestOtPatch(
  draft: OtDraft,
  selectedPath: string,
  instruction: string,
  model: ModelReference,
): Promise<OtSuggestion> {
  return request("ot.suggest", {
    draft,
    selected_path: selectedPath,
    instruction,
    model,
  });
}

export function exportOtPackage(
  suggestedName: string,
  dataBase64: string,
): Promise<boolean> {
  return invoke<boolean>("export_ot", { suggestedName, dataBase64 });
}
export async function installResearchPack(file: File): Promise<ResearchPackSummary> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const chunkSize = 0x8000;
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return request("packs.install", { filename: file.name, data_base64: window.btoa(binary) });
}

export function startResearch(research: ResearchRequest = { mode: "demo" }): Promise<ResearchJob> {
  return request("research.start", research);
}

export function deleteResearchRun(runId: string): Promise<{ run_id: string; deleted: boolean }> {
  return request("research.delete", { run_id: runId });
}

export function retryResearchSynthesis(
  runId: string,
  model?: ModelSelection,
): Promise<ResearchReport> {
  return request("research.retry_synthesis", { run_id: runId, ...(model ? { model } : {}) });
}

export function retryResearchModelStages(
  runId: string,
  model?: ModelSelection,
  budgetOnly = false,
): Promise<ResearchReport> {
  return request("research.retry_model_stages", {
    run_id: runId,
    ...(model ? { model } : {}),
    ...(budgetOnly ? { budget_only: true } : {}),
  });
}

export function extendResearchBudget(
  runId: string,
  planDigest: string,
  gapIds: string[],
  adaptiveChoice?: "keep" | "remove",
): Promise<ModelCallPlan> {
  return request("research.extend_budget", {
    run_id: runId,
    plan_digest: planDigest,
    gap_ids: gapIds,
    ...(adaptiveChoice ? { adaptive_choice: adaptiveChoice } : {}),
  });
}

export function getResearchCallHistory(
  runId: string,
  stageId: string,
  after?: string,
  limit = 10,
): Promise<ModelCallHistoryPage> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) {
    return Promise.reject(new Error("MODEL_HISTORY_PAGE_INVALID"));
  }
  return request("research.call_history", {
    run_id: runId,
    stage_id: stageId,
    ...(after ? { after } : {}),
    limit,
  });
}

export function retryResearchGrowth(
  runId: string,
  model: ModelSelection,
): Promise<ResearchReport> {
  return request("research.retry_growth", { run_id: runId, model });
}

export function retryResearchStage(
  runId: string,
  target: RecoveryStageTarget,
  model: ModelSelection,
  planHash: string,
  confirmNewAttempt = false,
): Promise<ResearchReport> {
  return request("research.retry_stage", {
    run_id: runId,
    target,
    model,
    plan_hash: planHash,
    ...(confirmNewAttempt ? { confirm_new_attempt: true } : {}),
  });
}

export function revalidateSavedResearch(runId: string): Promise<ResearchReport> {
  return request("research.revalidate_saved", { run_id: runId });
}

export function retryResearchFinancials(runId: string): Promise<ResearchReport> {
  return request("research.retry_financials", { run_id: runId });
}

export function refreshFinancialReport(
  runId: string,
  language?: ResearchReport["report_language"],
): Promise<ResearchReport> {
  return request("research.refresh_financial_report", { run_id: runId, language });
}

export function startResearchFinancialRetry(runId: string): Promise<ResearchJob> {
  return request("research.start_financial_retry", { run_id: runId });
}

export function rebuildResearchFinancials(runId: string): Promise<ResearchReport> {
  return request("research.rebuild_financials", { run_id: runId, confirmed: true });
}

export function startResearchFinancialRebuild(runId: string): Promise<ResearchJob> {
  return request("research.start_financial_rebuild", { run_id: runId, confirmed: true });
}

export function getResearchStatus(jobId: string): Promise<ResearchJob> {
  return request("research.status", { job_id: jobId });
}

export function cancelResearch(jobId: string): Promise<ResearchJob> {
  return request("research.cancel", { job_id: jobId });
}

export function cancelModelCall(jobId: string, requestId: string, attemptId: string): Promise<ResearchJob> {
  return request("research.cancel_model_call", { job_id: jobId, request_id: requestId, attempt_id: attemptId });
}

export function decideVisionUpload(jobId: string, approved: boolean): Promise<ResearchJob> {
  return request("research.vision_decision", { job_id: jobId, approved });
}

export function exportResearchReport(report: ResearchReport): Promise<boolean> {
  return invoke<boolean>("export_report", {
    suggestedName: `${report.ticker || "OpenThesis"}-${report.run_id.slice(0, 12)}.html`,
    markdown: report.markdown,
    html: report.html,
  });
}

export function exportFinancialDiagnostics(diagnostics: FinancialDiagnostics): Promise<boolean> {
  return invoke<boolean>("export_financial_diagnostics", {
    suggestedName: `OpenThesis-${diagnostics.company.ticker || "research"}-${diagnostics.run.run_id.slice(0, 12)}-diagnostics.json`,
    dataJson: JSON.stringify(diagnostics, null, 2),
  });
}

export function exportResearchDiagnostics(diagnostics: ResearchDiagnostics): Promise<boolean> {
  return invoke<boolean>("export_financial_diagnostics", {
    suggestedName: `OpenThesis-${diagnostics.run.run_id.slice(0, 12)}-research-diagnostics.json`,
    dataJson: JSON.stringify(diagnostics, null, 2),
  });
}

export function openExternalUrl(url: string): Promise<void> {
  return invoke<void>("open_external_url", { url });
}

export function listTheses(): Promise<ThesisVersion[]> {
  return request("thesis.list", {});
}

export function getThesis(thesisVersionId: string): Promise<ThesisVersion> {
  return request("thesis.get", { thesis_version_id: thesisVersionId });
}

export function saveThesis(
  companyCik: string,
  content: Record<string, unknown>,
  baseThesisVersionId?: string,
  company?: Company,
): Promise<ThesisVersion> {
  return request("thesis.save", { company_cik: companyCik, content, ...(company ? { company } : {}), ...(baseThesisVersionId ? { base_thesis_version_id: baseThesisVersionId } : {}) });
}

