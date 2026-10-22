import { beforeEach, describe, expect, it, vi } from "vitest";

import { invoke } from "@tauri-apps/api/core";
import { bootstrapBackend, extendResearchBudget, getResearchCallHistory, getResearchReport, retryResearchModelStages } from "./backend";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const invokeMock = vi.mocked(invoke);
const retryReport = {
  run_id: "run-1", ticker: "700", company_name: "Tencent", status: "partial", report_language: "en",
  report_contract_version: "1", report_input_generation: "g1", is_substantive: true,
  visible_sections: [], report_read_state: "partial", markdown: "saved report body", html: "",
};

function mockDeferredReport() {
  invokeMock
    .mockResolvedValueOnce({ job_id: "rpc-1" } as never)
    .mockResolvedValueOnce({ state: "ready", response: { result: retryReport } } as never);
}

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

  it("uses the budget-only retry boundary only after an explicit approval flow", async () => {
    mockDeferredReport();
    await retryResearchModelStages("run-1");
    expect(invokeMock).toHaveBeenNthCalledWith(1, "backend_request", {
      method: "rpc.start",
      params: { method: "research.retry_model_stages", params: { run_id: "run-1" } },
    });

    mockDeferredReport();
    await retryResearchModelStages("run-1", undefined, true);
    expect(invokeMock).toHaveBeenNthCalledWith(3, "backend_request", {
      method: "rpc.start",
      params: { method: "research.retry_model_stages", params: { run_id: "run-1", budget_only: true } },
    });
    expect(invokeMock).toHaveBeenNthCalledWith(2, "backend_request", {
      method: "rpc.result", params: { job_id: "rpc-1" },
    });
    expect(invokeMock).toHaveBeenNthCalledWith(4, "backend_request", {
      method: "rpc.result", params: { job_id: "rpc-1" },
    });
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
