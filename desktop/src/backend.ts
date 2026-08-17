import { invoke } from "@tauri-apps/api/core";

import type {
  BootstrapResult,
  Company,
  ModelPreset,
  ModelSelection,
  Market,
  Preferences,
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

export function discoverModels(params: {
  preset_id: string;
  base_url: string;
  api_key: string;
}): Promise<{ preset_id: string; models: string[]; warning: string }> {
  return request("models.discover", params);
}

export function getModelCatalog(): Promise<ModelPreset[]> {
  return request("models.catalog", {});
}

export function testModelConnection(
  selection: ModelSelection,
): Promise<{ ok: boolean; message: string }> {
  return request("models.test", selection);
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
