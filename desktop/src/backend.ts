import { invoke } from "@tauri-apps/api/core";

import type { BootstrapResult, Preferences, ResearchJob, ResearchReport } from "./types";

type BackendRequest = {
  method: string;
  params?: Record<string, unknown>;
};

async function request<T>({ method, params = {} }: BackendRequest): Promise<T> {
  return invoke<T>("backend_request", { method, params });
}

export function bootstrapBackend(): Promise<BootstrapResult> {
  return request({ method: "app.bootstrap" });
}

export function getResearchReport(
  runId: string,
  language?: "zh-CN" | "en",
): Promise<ResearchReport> {
  return request({
    method: "research.get_report",
    params: { run_id: runId, ...(language ? { language } : {}) },
  });
}

export function updatePreferences(
  preferences: Partial<Pick<Preferences, "ui_language" | "report_language" | "sidebar_collapsed">>,
): Promise<Preferences> {
  return request({ method: "settings.update", params: { preferences } });
}

export function startResearch(): Promise<ResearchJob> {
  return request({ method: "research.start", params: { mode: "demo" } });
}

export function getResearchStatus(jobId: string): Promise<ResearchJob> {
  return request({ method: "research.status", params: { job_id: jobId } });
}

export function cancelResearch(jobId: string): Promise<ResearchJob> {
  return request({ method: "research.cancel", params: { job_id: jobId } });
}
