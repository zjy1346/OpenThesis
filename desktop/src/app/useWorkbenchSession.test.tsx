import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  bootstrapBackend,
  cancelModelCall,
  cancelResearch,
  decideVisionUpload,
  extendResearchBudget,
  getResearchReport,
  getResearchStatus,
  listResearchTasks,
  retryResearchGrowth,
  retryResearchModelStages,
  retryResearchSynthesis,
  startResearch,
  startResearchFinancialRebuild,
  startResearchFinancialRetry,
} from "../backend";
import { useWorkbenchSession } from "./useWorkbenchSession";
import type { BootstrapResult, ResearchJob, ResearchReport, ResearchTaskList } from "../types";

vi.mock("../backend", () => ({
  archiveResearchTask: vi.fn(),
  bootstrapBackend: vi.fn(),
  cancelModelCall: vi.fn(),
  cancelResearch: vi.fn(),
  continueResearchQueue: vi.fn(),
  decideVisionUpload: vi.fn(),
  deleteResearchRun: vi.fn(),
  extendResearchBudget: vi.fn(),
  getResearchCallHistory: vi.fn(),
  getResearchReport: vi.fn(),
  getResearchStatus: vi.fn(),
  listResearchTasks: vi.fn(),
  openExternalUrl: vi.fn(),
  retryResearchGrowth: vi.fn(),
  retryResearchModelStages: vi.fn(),
  retryResearchStage: vi.fn(),
  retryResearchSynthesis: vi.fn(),
  revalidateSavedResearch: vi.fn(),
  resumeResearchTask: vi.fn(),
  refreshFinancialReport: vi.fn(),
  startResearch: vi.fn(),
  startResearchFinancialRebuild: vi.fn(),
  startResearchFinancialRetry: vi.fn(),
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

const report = (runId = "run-1", ticker = "700", companyName = "Tencent"): ResearchReport => ({
  run_id: runId,
  status: "completed",
  ticker,
  company_name: companyName,
  report_language: "en",
  markdown: "saved report for " + runId,
  html: "",
  report_contract_version: "1",
  report_revision_id: null,
  report_input_generation: "legacy:" + runId,
  report_read_state: "partial",
  is_substantive: true,
  reproducibility: {
    model_configuration: {
      configured_model_id: "saved-" + runId,
      connection_id: "connection-" + runId,
      configuration_version: 7,
      context_window_hint: 128_000,
    },
    research_configuration: {},
    data_snapshot: {},
  },
});

const job = (jobId: string, runId: string | null = "run-1", state: ResearchJob["state"] = "queued", revision = 1): ResearchJob => ({
  job_id: jobId,
  run_id: runId,
  state,
  operation_type: "research",
  company: { name: "Company " + jobId, ticker: jobId },
  model: "model-a",
  stage: "queued",
  message: "accepted",
  percent: 0,
  revision,
  created_at: "2026-10-28T00:00:00Z",
});

const taskList = (): ResearchTaskList => ({
  items: [],
  revision: 0,
  not_modified: false,
  counts: { running: 0, queued: 0, waiting_input: 0, interrupted: 0, cancelling: 0 },
  queue_paused: false,
  has_more: false,
  next_offset: null,
});

describe("useWorkbenchSession task integration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(bootstrapBackend).mockResolvedValue(bootstrap);
    vi.mocked(getResearchReport).mockImplementation(async (runId) => report(runId));
    vi.mocked(listResearchTasks).mockResolvedValue(taskList());
    vi.mocked(getResearchStatus).mockImplementation(async (jobId) => job(jobId));
    vi.mocked(startResearch).mockResolvedValue(job("new-job", null));
    vi.mocked(retryResearchSynthesis).mockResolvedValue(job("synthesis-job"));
    vi.mocked(retryResearchModelStages).mockResolvedValue(job("stages-job"));
    vi.mocked(retryResearchGrowth).mockResolvedValue(job("growth-job"));
    vi.mocked(startResearchFinancialRetry).mockResolvedValue(job("financial-retry-job"));
    vi.mocked(startResearchFinancialRebuild).mockResolvedValue(job("financial-rebuild-job"));
  });

  it("opens a readable historical report after reload instead of the newest queued run", async () => {
    vi.mocked(bootstrapBackend).mockResolvedValue({
      ...bootstrap,
      recent_runs: [
        { ...bootstrap.recent_runs[0], run_id: "queued-run", status: "created", completed_at: null },
        { ...bootstrap.recent_runs[0], run_id: "running-run", status: "running", completed_at: null },
        ...bootstrap.recent_runs,
      ],
    });
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.run_id).toBe("run-1"));
    expect(getResearchReport).toHaveBeenCalledTimes(1);
    expect(getResearchReport).toHaveBeenCalledWith("run-1", "en");
    expect(startResearch).not.toHaveBeenCalled();
  });

  it("does not project a not-yet-executed run when the restored history only contains queued work", async () => {
    vi.mocked(bootstrapBackend).mockResolvedValue({
      ...bootstrap,
      recent_runs: [{ ...bootstrap.recent_runs[0], run_id: "queued-run", status: "created", completed_at: null }],
    });
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(bootstrapBackend).toHaveBeenCalledTimes(2));
    expect(result.current.report).toBeNull();
    expect(result.current.reportLoadState.status).toBe("idle");
    expect(getResearchReport).not.toHaveBeenCalled();
    expect(startResearch).not.toHaveBeenCalled();
  });

  it("keeps the selected report and read generation when a new research job is accepted", async () => {
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.run_id).toBe("run-1"));
    const otherRun = {
      run_id: "run-2", ticker: "9988", company_name: "Alibaba", status: "completed",
      started_at: "", completed_at: "", report_language: "en" as const, exchange: "HKEX", market: "HK" as const,
    };
    await act(async () => { await result.current.selectRun(otherRun); });
    await waitFor(() => expect(result.current.report?.run_id).toBe("run-2"));
    const savedReport = result.current.report;
    vi.mocked(startResearch).mockResolvedValue(job("new-job", null));

    let accepted: ResearchJob | undefined;
    await act(async () => {
      accepted = await result.current.beginResearch({
        mode: "company",
        company: { cik: "1", ticker: "A", name: "A", exchange: "NASDAQ" },
      });
    });
    await waitFor(() => expect(result.current.tasks.detail?.job_id).toBe("new-job"));

    expect(result.current.report).toBe(savedReport);
    expect(result.current.reportLoadState).toEqual({ status: "loaded", run_id: "run-2" });
    expect(getResearchReport).toHaveBeenCalledTimes(2);
    expect(accepted?.job_id).toBe("new-job");
    expect(vi.mocked(startResearch).mock.calls[0][0]).toMatchObject({ client_request_id: expect.any(String) });
  });

  it("reuses a client request id after submission failure without making the error global", async () => {
    const request = { mode: "demo" as const };
    vi.mocked(startResearch)
      .mockRejectedValueOnce(new Error("temporary submission failure"))
      .mockResolvedValueOnce(job("accepted-job", null));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.bootstrap).not.toBeNull());

    await act(async () => {
      await expect(result.current.beginResearch(request)).rejects.toThrow("temporary submission failure");
    });
    expect(result.current.error).toBeNull();
    await act(async () => { await result.current.beginResearch(request); });

    expect(startResearch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(startResearch).mock.calls[0][0]?.client_request_id).toBeTruthy();
    expect(vi.mocked(startResearch).mock.calls[1][0]?.client_request_id).toBe(vi.mocked(startResearch).mock.calls[0][0]?.client_request_id);
  });

  it("keeps recovery actions asynchronous job submissions and never replaces or reloads the report", async () => {
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.run_id).toBe("run-1"));
    const original = result.current.report;

    await act(async () => { await result.current.retrySynthesis(); });
    await waitFor(() => expect(result.current.tasks.detail?.job_id).toBe("synthesis-job"));

    expect(retryResearchSynthesis).toHaveBeenCalledWith("run-1", undefined);
    expect(result.current.report).toBe(original);
    expect(getResearchReport).toHaveBeenCalledTimes(1);
  });

  it("uses the selected report saved model for growth, not another company's last request", async () => {
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.run_id).toBe("run-1"));
    await act(async () => {
      await result.current.beginResearch({
        mode: "demo",
        model: { configured_model_id: "other-company", configuration_version: 99, role: "primary" },
      });
      await result.current.retryGrowth();
    });

    expect(retryResearchGrowth).toHaveBeenCalledWith("run-1", {
      configured_model_id: "saved-run-1",
      connection_id: "connection-run-1",
      configuration_version: 7,
      role: "primary",
      context_window_hint: 128_000,
    });
  });

  it("binds cancellation and vision authorization to the supplied job and revision", async () => {
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.bootstrap).not.toBeNull());

    await act(async () => {
      await result.current.stopResearch("queued-B");
      await result.current.stopModelCall("running-C", "request-3", "attempt-4");
      await result.current.decideVisionUpload("vision-D", 12, "plan-5", true);
    });

    expect(cancelResearch).toHaveBeenCalledWith("queued-B");
    expect(cancelModelCall).toHaveBeenCalledWith("running-C", "request-3", "attempt-4");
    expect(decideVisionUpload).toHaveBeenCalledWith("vision-D", true, 12, "plan-5");
  });

  it("accepts budget and financial operations as tasks without waiting for completion", async () => {
    vi.mocked(extendResearchBudget).mockResolvedValue({
      plan_digest: "next",
      max_calls: 4,
      max_input_estimate: 20_000,
      max_output_tokens: 40_000,
      pending_budget_gaps: [],
    } as never);
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.report?.run_id).toBe("run-1"));

    await act(async () => {
      await result.current.approveBudgetGaps("run-1", "current", ["gap-1"]);
      await result.current.retryFinancials();
      await result.current.rebuildFinancials();
    });

    expect(retryResearchModelStages).toHaveBeenCalledWith("run-1", undefined, true);
    expect(startResearchFinancialRetry).toHaveBeenCalledWith("run-1");
    expect(startResearchFinancialRebuild).toHaveBeenCalledWith("run-1");
    expect(getResearchReport).toHaveBeenCalledTimes(1);
  });

  it("retains global storage-health protection when a task API reports corrupted storage", async () => {
    vi.mocked(startResearch).mockRejectedValue(new Error("STORAGE_HEALTH_RECOVERY_REQUIRED"));
    const { result } = renderHook(() => useWorkbenchSession());
    await waitFor(() => expect(result.current.bootstrap).not.toBeNull());

    await act(async () => {
      await expect(result.current.beginResearch({ mode: "demo" })).rejects.toThrow("STORAGE_HEALTH_RECOVERY_REQUIRED");
    });
    expect(result.current.error).toEqual({ kind: "storage-recovery-required" });
    expect(result.current.canRetry).toBe(false);
  });
});
