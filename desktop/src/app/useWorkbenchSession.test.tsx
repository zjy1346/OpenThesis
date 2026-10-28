import { renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  bootstrapBackend,
  extendResearchBudget,
  getResearchCallHistory,
  getResearchReport,
  getResearchStatus,
  retryResearchModelStages,
  retryResearchSynthesis,
  startResearch,
  startResearchFinancialRetry,
  updatePreferences,
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
  extendResearchBudget: vi.fn(),
  getResearchCallHistory: vi.fn(),
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

const STORAGE_HEALTH_RECOVERY_REQUIRED = "STORAGE_HEALTH_RECOVERY_REQUIRED";

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

  it("does not let delayed full bootstrap replace a report the user selected", async () => {
    let finishFullBootstrap!: (value: BootstrapResult) => void;
    const otherRun = { ...bootstrap.recent_runs[0], run_id: "run-2", ticker: "9988", company_name: "Alibaba" };
    vi.mocked(bootstrapBackend).mockImplementation((minimal) => minimal
      ? Promise.resolve(bootstrap)
      : new Promise((resolve) => { finishFullBootstrap = resolve; }));
    vi.mocked(getResearchReport).mockImplementation(async (runId) => report(`report for ${runId}`, runId));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(bootstrapBackend).toHaveBeenCalledTimes(2));

    await act(async () => { await result.current.selectRun(bootstrap.recent_runs[0]); });
    await waitFor(() => expect(result.current.report?.run_id).toBe("run-1"));
    await act(async () => {
      finishFullBootstrap({ ...bootstrap, recent_runs: [otherRun] });
      await Promise.resolve();
    });

    expect(result.current.report?.run_id).toBe("run-1");
    expect(getResearchReport).toHaveBeenCalledTimes(1);
    expect(getResearchReport).not.toHaveBeenCalledWith("run-2", "en");
  });

  it("does not auto-open history when research starts before full bootstrap returns", async () => {
    let finishFullBootstrap!: (value: BootstrapResult) => void;
    vi.mocked(bootstrapBackend).mockImplementation((minimal) => minimal
      ? Promise.resolve({ ...bootstrap, recent_runs: [] })
      : new Promise((resolve) => { finishFullBootstrap = resolve; }));
    vi.mocked(startResearch).mockResolvedValue({ job_id: "fresh-job", state: "queued", message: "", percent: 0, run_id: null });
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(bootstrapBackend).toHaveBeenCalledTimes(2));

    await act(async () => { await result.current.beginResearch({ mode: "demo" }); });
    await act(async () => {
      finishFullBootstrap(bootstrap);
      await Promise.resolve();
    });

    expect(result.current.job?.job_id).toBe("fresh-job");
    expect(result.current.report).toBeNull();
    expect(getResearchReport).not.toHaveBeenCalled();
  });

  it("keeps preferences saved in the minimal shell when full bootstrap returns late", async () => {
    let finishFullBootstrap!: (value: BootstrapResult) => void;
    const savedPreferences = {
      ...bootstrap.preferences,
      ui_language: "zh-Hant" as const,
      report_language: "zh-Hant" as const,
    };
    vi.mocked(bootstrapBackend).mockImplementation((minimal) => minimal
      ? Promise.resolve(bootstrap)
      : new Promise((resolve) => { finishFullBootstrap = resolve; }));
    vi.mocked(updatePreferences).mockResolvedValue(savedPreferences);
    vi.mocked(getResearchReport).mockResolvedValue(report("initial report"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(bootstrapBackend).toHaveBeenCalledTimes(2));

    await act(async () => {
      await result.current.savePreferences({ ui_language: "zh-Hant", report_language: "zh-Hant" });
    });
    await act(async () => {
      finishFullBootstrap({ ...bootstrap, preferences: bootstrap.preferences });
      await Promise.resolve();
    });

    await waitFor(() => expect(getResearchReport).toHaveBeenCalledWith("run-1", "zh-Hant"));
    expect(result.current.bootstrap?.preferences).toMatchObject(savedPreferences);
  });

  it("keeps a safe database-recovery state when startup bootstrap reports confirmed corruption", async () => {
    vi.mocked(bootstrapBackend).mockRejectedValue(new Error(STORAGE_HEALTH_RECOVERY_REQUIRED));
    const { result } = renderHook(() => useWorkbenchSession());

    await waitFor(() => expect(result.current.error?.kind).toBe("storage-recovery-required"));
    expect(result.current.canRetry).toBe(false);
    expect(result.current.error).not.toHaveProperty("detail");
  });

  it("preserves the displayed report and blocks retry when a report read finds storage corruption", async () => {
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));
    const anotherRun = { ...bootstrap.recent_runs[0], run_id: "run-2", company_name: "Alibaba", ticker: "9988" };
    vi.mocked(getResearchReport).mockRejectedValueOnce(new Error(STORAGE_HEALTH_RECOVERY_REQUIRED));

    await act(async () => { await result.current.selectRun(anotherRun); });

    expect(result.current.error?.kind).toBe("storage-recovery-required");
    expect(result.current.report?.markdown).toBe("initial report");
    expect(result.current.canRetry).toBe(false);
  });

  it("stops status polling and does not offer research retry after storage health failure", async () => {
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));
    vi.useFakeTimers();
    vi.mocked(startResearch).mockResolvedValue({ job_id: "corrupt-job", state: "running", message: "", percent: 10, run_id: "run-1" });
    vi.mocked(getResearchStatus).mockRejectedValue(new Error(STORAGE_HEALTH_RECOVERY_REQUIRED));

    await act(async () => { await result.current.beginResearch({ mode: "demo" }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });

    expect(result.current.error?.kind).toBe("storage-recovery-required");
    expect(result.current.canRetry).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(1);
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

  it("resolves a usable partial financial refresh without mislabeling its result complete", async () => {
    const done = completedJob("partial");
    done.operation_result = { ...done.operation_result!, research_can_continue: true,
      accepted: ["revenue"], rejected: ["reported_roe"] };
    vi.mocked(startResearchFinancialRetry).mockResolvedValue(runningJob);
    vi.mocked(getResearchStatus).mockResolvedValue(done);
    vi.mocked(getResearchReport)
      .mockResolvedValueOnce(report("original report"))
      .mockResolvedValueOnce(report("usable partial report"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("original report"));
    let retryPromise!: Promise<void>;
    await act(async () => { retryPromise = result.current.retryFinancials(); });
    await act(async () => { await retryPromise; });
    expect(result.current.error).toBeNull();
    expect(result.current.report?.financial_retry?.status).toBe("partial");
    expect(result.current.report?.financial_retry?.research_can_continue).toBe(true);
    expect(result.current.report?.markdown).toBe("usable partial report");
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
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
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
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(3);
    expect(result.current.error?.kind).toBe("core-unavailable");

    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(getResearchStatus).toHaveBeenCalledTimes(4);
    expect(result.current.error).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
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

  it("loads only the requested history page and resumes approved budget gaps through the budget-only path", async () => {
    const order: string[] = [];
    const refreshed = { ...report("resumed gap report"), call_plan: { plan_digest: "digest-next", max_calls: 4,
      max_input_estimate: 20_000, max_output_tokens: 40_000, pending_budget_gaps: [] } };
    vi.mocked(getResearchCallHistory).mockResolvedValue({ revision: "r2", calls: [], next_cursor: "cursor-1" });
    vi.mocked(extendResearchBudget).mockImplementation(async () => {
      order.push("extend");
      return { plan_digest: "digest-next", max_calls: 4, max_input_estimate: 20_000, max_output_tokens: 40_000, pending_budget_gaps: [] };
    });
    vi.mocked(retryResearchModelStages).mockImplementation(async () => {
      order.push("retry");
      return refreshed;
    });
    vi.mocked(startResearch).mockResolvedValue({ job_id: "other-job", state: "queued", message: "", percent: 0, run_id: null });
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));

    await act(async () => {
      await result.current.loadResearchCallHistory("run-1", "growth-opportunities", "cursor-0", 10);
      await result.current.beginResearch({ mode: "demo", model: { configured_model_id: "other-run-model", configuration_version: 9, role: "primary" } });
      await result.current.approveBudgetGaps("run-1", "digest-current", ["gap-growth"]);
    });

    expect(getResearchCallHistory).toHaveBeenCalledWith("run-1", "growth-opportunities", "cursor-0", 10);
    expect(extendResearchBudget).toHaveBeenCalledWith("run-1", "digest-current", ["gap-growth"]);
    expect(order).toEqual(["extend", "retry"]);
    expect(retryResearchModelStages).toHaveBeenCalledWith("run-1", undefined, true);
    expect(result.current.report?.markdown).toBe("resumed gap report");
  });

  it("passes adaptive migration choices through the existing budget-only resume path", async () => {
    vi.mocked(extendResearchBudget).mockResolvedValue({
      plan_digest: "digest-next", max_calls: null, max_input_estimate: null, max_output_tokens: null,
      pending_budget_gaps: [], limit_source: "legacy_default", adaptive_resume_available: true,
    } as never);
    vi.mocked(retryResearchModelStages).mockResolvedValue(report("adaptive continuation"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("initial report"));

    await act(async () => {
      await result.current.approveBudgetGaps("run-1", "digest-current", [], "remove");
    });

    expect(extendResearchBudget).toHaveBeenCalledWith("run-1", "digest-current", [], "remove");
    expect(retryResearchModelStages).toHaveBeenCalledWith("run-1", undefined, true);
    expect(result.current.report?.markdown).toBe("adaptive continuation");
  });

  it("refreshes a changed plan while preserving saved report text when budget approval fails", async () => {
    const initial = { ...report("keep this saved body"), call_plan: { plan_digest: "old-digest", max_calls: 2,
      max_input_estimate: 8_000, max_output_tokens: 10_000, pending_budget_gaps: [] } };
    const refreshed = { ...initial, markdown: "refresh must not replace the existing body", call_plan: { ...initial.call_plan, plan_digest: "new-digest" } };
    vi.mocked(getResearchReport).mockResolvedValueOnce(initial).mockResolvedValueOnce(refreshed);
    vi.mocked(extendResearchBudget).mockRejectedValue(new Error("MODEL_BUDGET_PLAN_CHANGED"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.markdown).toBe("keep this saved body"));

    let approvalError: unknown;
    await act(async () => {
      try { await result.current.approveBudgetGaps("run-1", "old-digest", ["gap-old"]); }
      catch (reason) { approvalError = reason; }
    });

    expect(approvalError).toEqual(new Error("MODEL_BUDGET_PLAN_CHANGED"));
    expect(result.current.report?.markdown).toBe("keep this saved body");
    expect(result.current.report?.call_plan?.plan_digest).toBe("new-digest");
    expect(retryResearchModelStages).not.toHaveBeenCalled();
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
