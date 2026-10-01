import { renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  bootstrapBackend,
  getResearchReport,
  getResearchStatus,
  retryResearchModelStages,
  retryResearchSynthesis,
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
  retryResearchModelStages: vi.fn(),
  retryResearchSynthesis: vi.fn(),
  refreshFinancialReport: vi.fn(),
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

const report = (
  markdown: string,
  runId = "run-1",
  ticker = "700",
  companyName = "Tencent",
): ResearchReport => ({
  run_id: runId,
  status: "completed",
  ticker,
  company_name: companyName,
  report_language: "en",
  markdown,
  html: "",
  report_contract_version: "1",
  report_revision_id: null,
  report_input_generation: `legacy:${runId}`,
  report_read_state: markdown ? "partial" : "diagnostic_only",
  is_substantive: Boolean(markdown.trim()),
  visible_sections: markdown.trim() ? [{
    section_id: "executive-summary",
    title: "Executive summary",
    source_artifact_ids: ["artifact-1"],
    verification_state: "verified",
    is_substantive: true,
  }] : [],
  report_read_diagnostics: [],
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

  afterEach(() => {
    vi.useRealTimers();
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

  it("keeps polling after a transient status failure and completes the financial retry", async () => {
    vi.mocked(startResearchFinancialRetry).mockResolvedValue(runningJob);
    vi.mocked(getResearchStatus)
      .mockRejectedValueOnce(new Error("temporary status outage"))
      .mockResolvedValueOnce(completedJob("succeeded"));
    vi.mocked(getResearchReport)
      .mockResolvedValueOnce(report("initial report"))
      .mockResolvedValueOnce(report("refreshed report"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));

    vi.useFakeTimers();
    let retryPromise!: Promise<void>;
    await act(async () => {
      retryPromise = result.current.retryFinancials();
      await Promise.resolve();
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(getResearchStatus).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    await act(async () => { await retryPromise; });
    expect(getResearchStatus).toHaveBeenCalledTimes(2);
    expect(result.current.report?.markdown).toBe("refreshed report");
  });

  it("shows a polling error after repeated failures and clears it after recovery", async () => {
    vi.mocked(startResearchFinancialRetry).mockResolvedValue(runningJob);
    vi.mocked(getResearchStatus)
      .mockRejectedValueOnce(new Error("status outage 1"))
      .mockRejectedValueOnce(new Error("status outage 2"))
      .mockRejectedValueOnce(new Error("status outage 3"))
      .mockResolvedValueOnce(runningJob)
      .mockResolvedValueOnce(completedJob("succeeded"));
    vi.mocked(getResearchReport)
      .mockResolvedValueOnce(report("initial report"))
      .mockResolvedValueOnce(report("recovered report"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));

    vi.useFakeTimers();
    let retryPromise!: Promise<void>;
    await act(async () => {
      retryPromise = result.current.retryFinancials();
      await Promise.resolve();
    });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    }
    expect(getResearchStatus).toHaveBeenCalledTimes(3);
    expect(result.current.error?.kind).toBe("core-unavailable");

    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(getResearchStatus).toHaveBeenCalledTimes(4);
    expect(result.current.error).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    await act(async () => { await retryPromise; });
    expect(getResearchStatus).toHaveBeenCalledTimes(5);
    expect(result.current.report?.markdown).toBe("recovered report");
  });

  it("retries synthesis from history without an in-memory research request", async () => {
    vi.mocked(retryResearchSynthesis).mockResolvedValue(report("retried synthesis"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));

    await act(async () => { await result.current.retrySynthesis(); });

    expect(retryResearchSynthesis).toHaveBeenCalledWith("run-1", undefined);
    expect(result.current.report?.markdown).toBe("retried synthesis");
  });

  it("resumes model stages from history without repeating financial work", async () => {
    vi.mocked(retryResearchModelStages).mockResolvedValue(report("resumed model stages"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));

    await act(async () => { await result.current.retryModelStages(); });

    expect(retryResearchModelStages).toHaveBeenCalledWith("run-1", undefined);
    expect(result.current.report?.markdown).toBe("resumed model stages");
  });

  it("does not let a late response from a previously selected run replace the current report", async () => {
    let resolveFirst!: (value: ResearchReport) => void;
    let resolveSecond!: (value: ResearchReport) => void;
    vi.mocked(getResearchReport)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(getResearchReport).toHaveBeenCalledWith("run-1", "en"));

    const secondRun = { ...bootstrap.recent_runs[0], run_id: "run-2", ticker: "9988", company_name: "Alibaba" };
    let secondSelection!: Promise<void>;
    await act(async () => { secondSelection = result.current.selectRun(secondRun); });
    await waitFor(() => expect(getResearchReport).toHaveBeenCalledWith("run-2", "en"));
    await act(async () => {
      resolveSecond(report("Alibaba report", "run-2", "9988", "Alibaba"));
      await secondSelection;
    });
    await act(async () => { resolveFirst(report("stale Tencent report")); });

    expect(result.current.report?.run_id).toBe("run-2");
    expect(result.current.report?.company_name).toBe("Alibaba");
    expect(result.current.report?.markdown).toBe("Alibaba report");
    expect(result.current.reportLoadState).toEqual({ status: "loaded", run_id: "run-2" });
  });

  it("distinguishes a report read failure from empty history and retries the selected run", async () => {
    vi.mocked(getResearchReport)
      .mockRejectedValueOnce(new Error("REPORT_STORAGE_UNAVAILABLE"))
      .mockResolvedValueOnce(report("report recovered after retry"));
    const { result } = renderHook(() => useWorkbenchSession());

    await waitFor(() => expect(result.current.reportLoadState.status).toBe("failed"));
    expect(result.current.bootstrap?.recent_runs).toHaveLength(1);
    expect(result.current.report).toBeNull();
    expect(result.current.error).toMatchObject({
      kind: "report-unavailable",
      run_id: "run-1",
      code: "REPORT_STORAGE_UNAVAILABLE",
    });

    await act(async () => { await result.current.retryReportRead(); });

    expect(getResearchReport).toHaveBeenCalledTimes(2);
    expect(result.current.report?.markdown).toBe("report recovered after retry");
    expect(result.current.reportLoadState).toEqual({ status: "loaded", run_id: "run-1" });
  });
});
