import { invoke } from "@tauri-apps/api/core";

import type {
  BootstrapResult,
  Company,
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
  ResearchReport,
  ResearchRequest,
  ResearchPackSummary,
  ThesisVersion,
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
  return invoke<BackendResponse<M>>("backend_request", { method, params });
}

export function bootstrapBackend(): Promise<BootstrapResult> {
  return request("app.bootstrap", {});
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
  });
}

export function updatePreferences(
  preferences: Partial<Pick<Preferences, "ui_language" | "report_language" | "sidebar_collapsed" | "parallel_agents" | "research_market">>,
): Promise<Preferences> {
  return request("settings.update", { preferences });
}

export function searchCompanies(query: string, market: Market): Promise<Company[]> {
  return request("company.search", { query, market });
}

export function listModelProviders(): Promise<ProviderDefinition[]> {
  return invoke<ProviderDefinition[]>("model_center_list_providers");
}

export function listProviderConnections(): Promise<ProviderConnectionSummary[]> {
  return invoke<ProviderConnectionSummary[]>("model_center_list_connections");
}

export function saveProviderConnection(input: {
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
}): Promise<ConfiguredModelSummary> {
  return invoke<ConfiguredModelSummary>("model_center_save_configured_model", { input });
}

export function deleteConfiguredModel(configuredModelId: string): Promise<void> {
  return invoke<void>("model_center_delete_configured_model", { configuredModelId });
}

export function discoverConnectionModels(connectionId: string): Promise<DiscoveredModel[]> {
  return invoke<DiscoveredModel[]>("model_gateway_discover_connection", { connectionId });
}

export function testProviderConnection(connectionId: string, modelId?: string): Promise<{ ok: boolean; message: string }> {
  return invoke<{ ok: boolean; message: string }>("model_gateway_test_connection", { connectionId, modelId });
}

export function testConfiguredModel(configuredModelId: string): Promise<{ ok: boolean; message: string }> {
  return invoke<{ ok: boolean; message: string }>("model_gateway_test_model", { configuredModelId });
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
  model: ModelSelection,
): Promise<ResearchReport> {
  return request("research.retry_synthesis", { run_id: runId, model });
}

export function retryResearchGrowth(
  runId: string,
  model: ModelSelection,
): Promise<ResearchReport> {
  return request("research.retry_growth", { run_id: runId, model });
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
): Promise<ThesisVersion> {
  return request("thesis.save", { company_cik: companyCik, content });
}

