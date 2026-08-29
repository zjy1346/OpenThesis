import { renderHook, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  bootstrapBackend,
  getResearchReport,
  getResearchStatus,
  startResearchFinancialRetry,
} from "../backend";
import { useWorkbenchSession } from "./useWorkbenchSession";
import type { BootstrapResult, ResearchJob, ResearchReport } from "../types";

vi.mock("../backend", () => ({
  bootstrapBackend: vi.fn(),
  cancelResearch: vi.fn(),
  decideVisionUpload: vi.fn(),
  deleteResearchRun: vi.fn(),
  getResearchReport: vi.fn(),
  getResearchStatus: vi.fn(),
  openExternalUrl: vi.fn(),
  retryResearchGrowth: vi.fn(),
  retryResearchSynthesis: vi.fn(),
  startResearch: vi.fn(),
  startResearchFinancialRetry: vi.fn(),
  startResearchFinancialRebuild: vi.fn(),
  updatePreferences: vi.fn(),
}));

const bootstrap: BootstrapResult = {
  contract_version: "test",
  app_version: "test",
  capabilities: [],
  preferences: {
    ui_language: "en",
    report_language: "en",
    ui_language_mode: "manual",
    sidebar_collapsed: "false",
    parallel_agents: "false",
  },
  recent_runs: [{
    run_id: "run-1",
    ticker: "700",
    company_name: "Tencent",
    status: "completed",
    started_at: "",
    completed_at: "",
    report_language: "en",
    exchange: "SEHK",
    market: "HK",
  }],
  common_companies: [],
  research_packs: [],
  interrupted_runs: 0,
};

const report = (markdown: string): ResearchReport => ({
  run_id: "run-1",
  ticker: "700",
  company_name: "Tencent",
  status: "completed",
  report_language: "en",
  markdown,
  html: "",
});

const runningJob: ResearchJob = {
  job_id: "financial-job",
  state: "running",
  message: "Refreshing financial evidence",
  percent: 10,
  run_id: "run-1",
};

const completedJob = (status: "succeeded" | "partial"): ResearchJob => ({
  ...runningJob,
  state: "completed",
  percent: 100,
  operation_result: {
    mode: "retry",
    targets: ["2025"],
    downloaded: ["2025"],
    accepted: status === "succeeded" ? ["2025"] : [],
    rejected: status === "succeeded" ? [] : ["2025"],
    status,
    error: status === "succeeded" ? "" : "quality gate rejected the filing",
    updated_artifacts: ["research-report/run-1"],
  },
});

describe("useWorkbenchSession financial jobs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(bootstrapBackend).mockResolvedValue(bootstrap);
    vi.mocked(getResearchReport).mockResolvedValue(report("initial report"));
  });

  it("keeps financial retry pending until the job completes and then refreshes the report", async () => {
    vi.mocked(startResearchFinancialRetry).mockResolvedValue(runningJob);
    vi.mocked(getResearchStatus).mockResolvedValue(completedJob("succeeded"));
    vi.mocked(getResearchReport)
      .mockResolvedValueOnce(report("initial report"))
      .mockResolvedValueOnce(report("refreshed report"));
    const { result } = renderHook(() => useWorkbenchSession());

    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));
    let settled = false;
    let retryPromise!: Promise<void>;
    await act(async () => {
      retryPromise = result.current.retryFinancials();
      retryPromise.then(() => { settled = true; });
      await new Promise((resolve) => window.setTimeout(resolve, 60));
    });
    expect(settled).toBe(false);
    expect(startResearchFinancialRetry).toHaveBeenCalledWith("run-1");
    expect(result.current.job?.job_id).toBe("financial-job");

    await act(async () => { await retryPromise; });
    expect(result.current.report?.markdown).toBe("refreshed report");
    expect(result.current.report?.financial_retry?.status).toBe("succeeded");
  });

  it("rejects partial completion while retaining the refreshed report and result", async () => {
    vi.mocked(startResearchFinancialRetry).mockResolvedValue(runningJob);
    vi.mocked(getResearchStatus).mockResolvedValue(completedJob("partial"));
    vi.mocked(getResearchReport)
      .mockResolvedValueOnce(report("initial report"))
      .mockResolvedValueOnce(report("partial refreshed report"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));

    let retryPromise!: Promise<void>;
    await act(async () => { retryPromise = result.current.retryFinancials(); });
    await waitFor(() => expect(result.current.job?.job_id).toBe("financial-job"));
    let rejected = false;
    await act(async () => {
      try {
        await retryPromise;
      } catch (error) {
        rejected = error instanceof Error && error.message.includes("quality gate rejected");
      }
    });
    expect(rejected).toBe(true);
    expect(getResearchReport).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(result.current.report?.markdown).toBe("partial refreshed report"));
    expect(result.current.report?.financial_retry?.status).toBe("partial");
  });
});
