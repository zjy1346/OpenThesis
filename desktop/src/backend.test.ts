import { beforeEach, describe, expect, it, vi } from "vitest";

import { invoke } from "@tauri-apps/api/core";
import { bootstrapBackend, extendResearchBudget, getResearchCallHistory, getResearchReport, retryResearchModelStages } from "./backend";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const invokeMock = vi.mocked(invoke);
describe("research call-budget and history API", () => {
  beforeEach(() => vi.resetAllMocks());

  it("adds only the explicitly selected pending gaps to the existing plan", async () => {
    const callPlan = {
      plan_digest: "digest-2",
      max_calls: 8,
      max_input_estimate: 24_000,
      max_output_tokens: 500_000,
      pending_budget_gaps: [],
    };
    invokeMock.mockResolvedValueOnce(callPlan as never);

    await expect(extendResearchBudget("run-1", "digest-1", ["gap-a", "gap-b"]))
      .resolves.toEqual(callPlan);
    expect(invokeMock).toHaveBeenCalledWith("backend_request", {
      method: "research.extend_budget",
      params: { run_id: "run-1", plan_digest: "digest-1", gap_ids: ["gap-a", "gap-b"] },
    });
  });

  it("sends adaptive migration choice with an optional empty gap list", async () => {
    const callPlan = {
      plan_digest: "digest-1", max_calls: null, max_input_estimate: null, max_output_tokens: null,
      pending_budget_gaps: [], limit_source: "legacy_default", adaptive_resume_available: true,
    };
    invokeMock.mockResolvedValueOnce(callPlan as never);

    await expect(extendResearchBudget("run-1", "digest-1", [], "remove")).resolves.toEqual(callPlan);
    expect(invokeMock).toHaveBeenCalledWith("backend_request", {
      method: "research.extend_budget",
      params: { run_id: "run-1", plan_digest: "digest-1", gap_ids: [], adaptive_choice: "remove" },
    });
  });

  it("requests one bounded history page for a single stage", async () => {
    const page = { revision: "5:9", calls: [], next_cursor: null };
    invokeMock.mockResolvedValueOnce(page as never);

    await expect(getResearchCallHistory("run-1", "financial-analyst", "cursor-1", 10))
      .resolves.toEqual(page);
    expect(invokeMock).toHaveBeenCalledWith("backend_request", {
      method: "research.call_history",
      params: { run_id: "run-1", stage_id: "financial-analyst", after: "cursor-1", limit: 10 },
    });
  });

  it("lightly enqueues retries and reuses the idempotency key after a lost acknowledgement", async () => {
    invokeMock
      .mockRejectedValueOnce(new Error("acknowledgement lost"))
      .mockResolvedValueOnce({ job_id: "job-1", state: "queued", message: "", percent: 0, run_id: "run-1" } as never)
      .mockResolvedValueOnce({ job_id: "job-2", state: "queued", message: "", percent: 0, run_id: "run-1" } as never);

    await retryResearchModelStages("run-1");
    await retryResearchModelStages("run-1", undefined, true);

    const retryCall = invokeMock.mock.calls[0];
    expect(retryCall[0]).toBe("backend_request");
    expect(retryCall[1]).toMatchObject({
      method: "research.retry_model_stages",
      params: { run_id: "run-1", client_request_id: expect.any(String) },
    });
    expect(invokeMock.mock.calls[1]).toEqual(retryCall);
    expect(invokeMock).toHaveBeenNthCalledWith(3, "backend_request", {
      method: "research.retry_model_stages",
      params: { run_id: "run-1", budget_only: true, client_request_id: expect.any(String) },
    });
    expect(invokeMock).toHaveBeenCalledTimes(3);
  });

  it("uses the current deferred RPC envelope for report reads", async () => {
    const report = {
      run_id: "run-1", ticker: "700", company_name: "Tencent", status: "partial", report_language: "en",
      report_contract_version: "1", report_input_generation: "g1", is_substantive: true,
      visible_sections: [], report_read_state: "partial", markdown: "saved report body", html: "",
    };
    invokeMock
      .mockResolvedValueOnce({ job_id: "rpc-1" } as never)
      .mockResolvedValueOnce({ state: "ready", response: { result: report } } as never);

    await expect(getResearchReport("run-1", "en")).resolves.toEqual(report);
    expect(invokeMock).toHaveBeenNthCalledWith(1, "backend_request", {
      method: "rpc.start",
      params: { method: "research.get_report", params: { run_id: "run-1", language: "en" } },
    });
    expect(invokeMock).toHaveBeenNthCalledWith(2, "backend_request", {
      method: "rpc.result", params: { job_id: "rpc-1" },
    });
  });

  it("keeps minimal startup direct and defers full bootstrap work", async () => {
    const bootstrap = { contract_version: "2", recent_runs: [], preferences: {} };
    invokeMock.mockResolvedValueOnce(bootstrap as never);
    await expect(bootstrapBackend(true)).resolves.toEqual(bootstrap);
    expect(invokeMock).toHaveBeenNthCalledWith(1, "backend_request", { method: "app.bootstrap", params: { minimal: true } });

    invokeMock
      .mockResolvedValueOnce({ job_id: "bootstrap-rpc" } as never)
      .mockResolvedValueOnce({ state: "ready", response: { result: bootstrap } } as never);
    await expect(bootstrapBackend()).resolves.toEqual(bootstrap);
    expect(invokeMock).toHaveBeenNthCalledWith(2, "backend_request", {
      method: "rpc.start", params: { method: "app.bootstrap", params: {} },
    });
    expect(invokeMock).toHaveBeenNthCalledWith(3, "backend_request", {
      method: "rpc.result", params: { job_id: "bootstrap-rpc" },
    });
  });
});
