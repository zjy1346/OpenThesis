import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { COPY } from "../../i18n";
import { exportFinancialDiagnostics, getFinancialDiagnostics, listConfiguredModels } from "../../backend";
import { stripReportPreamble } from "./ReportWorkspace";
import { ReportWorkspace } from "./ReportWorkspace";

vi.mock("../../backend", () => ({
  getResearchReport: vi.fn(), exportResearchReport: vi.fn(),
  getFinancialDiagnostics: vi.fn(), exportFinancialDiagnostics: vi.fn(),
  listConfiguredModels: vi.fn(),
}));

describe("report presentation", () => {
  it("moves the generated Chinese report preamble out of the document body", () => {
    expect(stripReportPreamble([
      "# OpenThesis 长期公司研究",
      "",
      "研究运行：`run-123`",
      "",
      "> 本报告用于研究辅助，不构成投资建议或交易指令。",
      "",
      "# Tesla, Inc. 财务概览",
    ].join("\n"))).toBe("# Tesla, Inc. 财务概览");
  });

  it("preserves custom report content that has no generated preamble", () => {
    const markdown = "# Independent report\n\nOriginal content.";
    expect(stripReportPreamble(markdown)).toBe(markdown);
  });

  it("localizes distinct listing and reporting currencies", () => {
    render(<ReportWorkspace report={{ run_id: "run", ticker: "700", company_name: "Tencent", status: "completed", report_language: "en", market: "HK", exchange: "SEHK", listing_currency: "HKD", reporting_currency: "CNY", markdown: "# Report", html: "" }} copy={COPY.en} />);
    expect(screen.getByText("Listing currency: HKD")).toBeInTheDocument();
    expect(screen.getByText("Reporting currency: CNY")).toBeInTheDocument();
  });

  it("localizes a shared listing and reporting currency", () => {
    render(<ReportWorkspace report={{ run_id: "run", ticker: "700", company_name: "Tencent", status: "completed", report_language: "en", market: "HK", exchange: "SEHK", listing_currency: "CNY", reporting_currency: "CNY", markdown: "# Report", html: "" }} copy={COPY.en} />);
    expect(screen.getByText("Listing and reporting currency: CNY")).toBeInTheDocument();
    expect(screen.queryByText(/Listing currency: CNY/)).not.toBeInTheDocument();
  });

  it("uses one status region while retrying a partial synthesis", async () => {
    let resolveRetry: (() => void) | undefined;
    const retry = vi.fn(() => new Promise<void>((resolve) => { resolveRetry = resolve; }));
    render(<ReportWorkspace report={{ run_id: "run", ticker: "1211", company_name: "BYD", status: "partial", report_language: "en", market: "HK", exchange: "SEHK", listing_currency: "HKD", reporting_currency: "CNY", retryable_synthesis: true, markdown: "# Report", html: "" }} copy={COPY.en} onRetrySynthesis={retry} />);

    fireEvent.click(screen.getByRole("button", { name: COPY.en.retrySynthesis }));

    expect(await screen.findByText(COPY.en.retryingSynthesis)).toBeInTheDocument();
    expect(screen.queryByText(COPY.en.partialReport)).not.toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(1);
    resolveRetry?.();
  });

  it("shows action-required state even when the attempt itself completed", () => {
    render(<ReportWorkspace report={{ run_id: "run", ticker: "1211", company_name: "BYD", status: "completed", attempt_complete: true, research_complete: false, action_required: true, report_language: "en", markdown: "# Report", html: "" }} copy={COPY.en} />);
    expect(screen.getByRole("alert")).toHaveTextContent(COPY.en.partialReport);
  });

  it("does not present an empty action-required run as a report shell", () => {
    render(<ReportWorkspace report={{
      run_id: "empty-run", ticker: "600519", company_name: "Moutai", status: "failed",
      report_language: "en", attempt_complete: true, research_complete: false, action_required: true,
      report_contract_version: "1", report_input_generation: "snapshot-1", report_read_state: "diagnostic_only",
      is_substantive: false, visible_sections: [],
      report_readiness: {
        state: "action_required", complete: false, substantive_sections: [],
        missing_sections: ["executive_summary", "claims"], missing_stages: ["verified-research-dossier"],
        issues: ["research_report_artifact_missing"], recovery_action: "resume_or_repair_missing_research_stages",
      }, markdown: "# OpenThesis Long-term Company Research\n\nNo report content.", html: "",
    }} copy={COPY.en} />);

    expect(screen.getByRole("alert")).toHaveTextContent(COPY.en.reportReadinessActionRequired);
    expect(screen.getByText(COPY.en.reportNoSubstantiveContent)).toBeInTheDocument();
    expect(screen.getByText("Executive summary, Key conclusions")).toBeInTheDocument();
    expect(screen.getByText("Verified research dossier")).toBeInTheDocument();
    expect(screen.getByText(COPY.en.reportRecoveryResumeStages)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "OpenThesis Long-term Company Research" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.en.exportReport })).not.toBeInTheDocument();
  });

  it("preserves substantive partial research and only shows recovery details returned by the API", () => {
    render(<ReportWorkspace report={{
      run_id: "partial-run", ticker: "1211", company_name: "BYD", status: "completed",
      report_language: "en", attempt_complete: true, research_complete: false, action_required: true,
      report_readiness: {
        state: "substantive_partial", complete: false, substantive_sections: ["business_model", "claims"],
        missing_sections: ["scenarios"], missing_stages: [], issues: [], recovery_action: "complete_missing_or_unverified_sections",
      }, markdown: "# Preserved completed research\n\nUseful verified content.", html: "",
    }} copy={COPY.en} />);

    expect(screen.getByRole("alert")).toHaveTextContent(COPY.en.reportReadinessPartial);
    expect(screen.getByRole("heading", { name: "Preserved completed research" })).toBeInTheDocument();
    expect(screen.getByText("Long-term scenarios")).toBeInTheDocument();
    expect(screen.queryByText(COPY.en.reportReadinessMissingStages)).not.toBeInTheDocument();
    expect(screen.getByText(COPY.en.reportRecoveryCompleteSections)).toBeInTheDocument();
  });

  it("keeps saved sections visible when overall readiness has no complete-section list", () => {
    render(<ReportWorkspace report={{
      run_id: "partial-with-body", ticker: "1211", company_name: "BYD", status: "partial",
      report_language: "en", report_contract_version: "1", report_input_generation: "snapshot-2",
      report_read_state: "partial", is_substantive: true,
      visible_sections: [{ section_id: "executive_summary", title: "Executive summary", source_artifact_ids: ["r1"], verification_state: "partial", is_substantive: true }],
      report_readiness: { state: "action_required", complete: false, substantive_sections: [], missing_sections: ["claims"], missing_stages: [], issues: [], recovery_action: "review_run_diagnostics" },
      markdown: "# Saved research\n\nThis verified section remains readable even though required stages are incomplete.", html: "",
    }} copy={COPY.en} />);

    expect(screen.getByRole("heading", { name: "Saved research" })).toBeInTheDocument();
    expect(screen.queryByText(COPY.en.reportNoSubstantiveContent)).not.toBeInTheDocument();
  });

  it("marks a readiness-verified report as complete without partial recovery details", () => {
    render(<ReportWorkspace report={{
      run_id: "complete-run", ticker: "1211", company_name: "BYD", status: "completed",
      report_language: "en", attempt_complete: true, research_complete: true, action_required: false,
      report_readiness: { state: "complete", complete: true, substantive_sections: ["claims"], missing_sections: [], missing_stages: [], issues: [], recovery_action: "" },
      markdown: "# Complete report", html: "",
    }} copy={COPY.en} />);

    expect(screen.getByRole("status")).toHaveTextContent(COPY.en.reportReadinessComplete);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(COPY.en.reportReadinessMissingSections)).not.toBeInTheDocument();
  });

  it("offers a tested larger-context model only after an explicit capacity failure", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([
      { configured_model_id: "current", connection_id: "c1", model_id: "small", alias: "Small", free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null, enabled: true, capabilities: ["text_chat"], health_status: "ready", last_discovered_at: null, context_window_hint: 32_000, temperature: null, timeout_seconds: 120, configuration_version: 1 },
      { configured_model_id: "large", connection_id: "c2", model_id: "large", alias: "Large context", free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null, enabled: true, capabilities: ["text_chat", "structured_json"], health_status: "ready", last_discovered_at: null, context_window_hint: 200_000, temperature: null, timeout_seconds: 120, configuration_version: 4 },
    ]);
    const retry = vi.fn(async () => undefined);
    render(<ReportWorkspace report={{ run_id: "run", ticker: "1211", company_name: "BYD", status: "partial", report_language: "en", retryable_synthesis: true, synthesis_error_code: "MODEL_CONTEXT_CAPACITY", reproducibility: { model_configuration: { configured_model_id: "current" }, research_configuration: {}, data_snapshot: {} }, markdown: "# Report", html: "" }} copy={COPY.en} onRetrySynthesis={retry} />);

    expect(await screen.findByRole("combobox", { name: COPY.en.synthesisCapacityModel })).toHaveValue("large");
    expect(screen.queryByRole("button", { name: COPY.en.retrySynthesis })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: COPY.en.synthesisCapacityRetry }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith({ configured_model_id: "large", connection_id: "c2", configuration_version: 4, role: "primary" }));
  });

  it("lets any persisted failed model stage resume with the selected model and fresh plan hash", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([
      { configured_model_id: "current", connection_id: "c1", model_id: "small", alias: "Small", free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null, enabled: true, capabilities: ["text_chat"], health_status: "ready", last_discovered_at: null, context_window_hint: 32_000, temperature: null, timeout_seconds: 120, configuration_version: 1 },
      { configured_model_id: "selected", connection_id: "c2", model_id: "capable", alias: "Capable", free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null, enabled: true, capabilities: ["text_chat", "structured_json"], health_status: "ready", last_discovered_at: null, context_window_hint: 200_000, temperature: null, timeout_seconds: 120, configuration_version: 4 },
    ]);
    const resume = vi.fn(async () => undefined);
    render(<ReportWorkspace report={{
      run_id: "run", ticker: "NVDA", company_name: "NVIDIA", status: "partial", report_language: "en",
      recovery_plan: { target: "counter-analysis", stages: ["counter-analysis", "forecast-scenarios", "final-synthesis"], reason: "counter_analysis_failed_or_unverified", input_artifact_ids: ["a1"], plan_hash: "plan-current", available: true, error_code: "MODEL_RESPONSE_INVALID" },
      reproducibility: { model_configuration: { configured_model_id: "current" }, research_configuration: {}, data_snapshot: {} },
      markdown: "# Report", html: "",
    }} copy={COPY.en} onRetryRecoveryStage={resume} />);

    expect(await screen.findByRole("combobox", { name: COPY.en.recoveryModel })).toHaveValue("selected");
    fireEvent.click(screen.getByRole("button", { name: COPY.en.recoveryRetry }));
    await waitFor(() => expect(resume).toHaveBeenCalledWith(
      "counter-analysis",
      { configured_model_id: "selected", connection_id: "c2", configuration_version: 4, role: "primary" },
      "plan-current",
    ));
  });

  it("retries only the growth stage from an empty growth report", async () => {
    let resolveRetry: (() => void) | undefined;
    const retryGrowth = vi.fn(() => new Promise<void>((resolve) => { resolveRetry = resolve; }));
    const retrySynthesis = vi.fn();
    render(<ReportWorkspace report={{ run_id: "run", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en", retryable_growth: true, markdown: "# Growth Opportunities\n\nNo usable growth output.", html: "" }} copy={COPY.en} onRetrySynthesis={retrySynthesis} onRetryGrowth={retryGrowth} />);

    fireEvent.click(screen.getByRole("button", { name: COPY.en.retryGrowth }));

    expect(retryGrowth).toHaveBeenCalledTimes(1);
    expect(retrySynthesis).not.toHaveBeenCalled();
    expect(await screen.findByText(COPY.en.retryingGrowth)).toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(1);
    resolveRetry?.();
  });

  it("resumes failed base model stages without repeating financial ingestion", async () => {
    let resolveRetry: (() => void) | undefined;
    const retryModelStages = vi.fn(() => new Promise<void>((resolve) => { resolveRetry = resolve; }));
    render(<ReportWorkspace report={{ run_id: "run", ticker: "1211", company_name: "BYD", status: "partial", report_language: "en", retryable_model_stages: true, markdown: "# Report", html: "" }} copy={COPY.en} onRetryModelStages={retryModelStages} />);

    fireEvent.click(screen.getByRole("button", { name: COPY.en.retryModelStages }));

    expect(retryModelStages).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(COPY.en.retryingModelStages)).toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(1);
    resolveRetry?.();
  });

  it("keeps a zero-token financial retry visible with missing periods and no model selection", async () => {
    let resolveRetry: (() => void) | undefined;
    const retryFinancials = vi.fn(() => new Promise<void>((resolve) => { resolveRetry = resolve; }));
    render(<ReportWorkspace report={{ run_id: "run", ticker: "9988", company_name: "Alibaba", status: "completed", report_language: "en", financial_status: { state: "incomplete", retryable: true, history_years: 2, expected_periods: ["2026", "2025", "2024"], available_periods: ["2026", "2024"], missing_periods: ["2025"], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-download", last_error: "temporary_timeout", updated_at: "", next_action: "retry_missing_periods", model_calls: 0, token_delta: 0, snapshot_stale: true }, markdown: "# Report", html: "" }} copy={COPY.en} onRetryFinancials={retryFinancials} />);

    expect(screen.getByText("Missing fiscal years: 2025")).toBeInTheDocument();
    expect(screen.getByText(COPY.en.financialZeroToken)).toBeInTheDocument();
    expect(screen.getByText(COPY.en.financialSnapshotStale)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: COPY.en.retryFinancials }));
    expect(retryFinancials).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(COPY.en.retryingFinancials)).toBeInTheDocument();
    resolveRetry?.();
  });

  it("requires explicit confirmation before a full financial rebuild", () => {
    const rebuild = vi.fn(async () => undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<ReportWorkspace report={{ run_id: "run", ticker: "9988", company_name: "Alibaba", status: "completed", report_language: "en", financial_status: { state: "incomplete", retryable: true, history_years: 2, expected_periods: ["2026", "2025", "2024"], available_periods: ["2026", "2024"], missing_periods: ["2025"], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-validation", last_error: "quality", updated_at: "", next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0 }, markdown: "# Report", html: "" }} copy={COPY.en} onRebuildFinancials={rebuild} />);

    fireEvent.click(screen.getByRole("button", { name: COPY.en.rebuildFinancials }));
    expect(rebuild).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: COPY.en.rebuildFinancials }));
    expect(rebuild).toHaveBeenCalledTimes(1);
    confirm.mockRestore();
  });

  it("separates a completed financial rebuild from a failed report refresh", async () => {
    const refresh = vi.fn(async () => { throw new Error("FILING_REPORT_REFRESH_FAILED"); });
    render(<ReportWorkspace report={{
      run_id: "run", ticker: "700", company_name: "Tencent", status: "completed",
      report_language: "en", financial_retry: {
        mode: "retry", targets: ["2025"], downloaded: ["2025"], accepted: ["2025:revenue"],
        rejected: [], status: "partial", error: "FILING_REPORT_REFRESH_FAILED", updated_artifacts: [],
      }, financial_status: {
        state: "warning", retryable: true, history_years: 2, expected_periods: ["2025", "2024"],
        available_periods: ["2025"], missing_periods: [], nodes: [], issues: [], attempt_count: 1,
        last_stage: "report-refresh", last_error: "FILING_REPORT_REFRESH_FAILED", updated_at: "",
        next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0,
      }, markdown: "# Report", html: "",
    }} copy={COPY.en} onRetryFinancials={vi.fn(async () => undefined)} onRefreshFinancialReport={refresh} />);

    expect(screen.getByLabelText(COPY.en.financialStageStatus)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: COPY.en.refreshFinancialReport }));
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(COPY.en.financialReportRefreshFailed)).toBeInTheDocument();
    expect(screen.getByText(/Report refresh: Failed/)).toBeInTheDocument();
  });

  it("shows readable financial recovery details without exposing stable error codes", () => {
    render(<ReportWorkspace report={{
      run_id: "run", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en",
      financial_status: {
        state: "warning", retryable: true, history_years: 2, expected_periods: ["2025", "2024"],
        available_periods: ["2024"], missing_periods: ["2025"], nodes: [], issues: [], attempt_count: 2,
        last_stage: "filing-validation", last_error: "FILING_DATA_QUALITY_FAILED", updated_at: "",
        next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0,
        recovery_cases: [{ accession_number: "acc-2025", period: "2025", pages: [4], fields: ["revenue"], stage: "filing-validation", error_code: "FILING_DATA_QUALITY_FAILED", attempts: 2, status: "open", next_action: "retry_local_parse" }],
      }, markdown: "# Report", html: "",
    }} copy={COPY.en} onRetryFinancials={vi.fn(async () => undefined)} />);

    expect(screen.getByText("Failed reports: 2025")).toBeInTheDocument();
    expect(screen.getByText("Failed fields: revenue")).toBeInTheDocument();
    expect(screen.getByText("Failed stage: Financial validation")).toBeInTheDocument();
    expect(screen.getByText(/Official filing retrieval or parsing did not complete/)).toBeInTheDocument();
    expect(screen.queryByText(/FILING_DATA_QUALITY_FAILED/)).not.toBeInTheDocument();
  });

  it("exports a diagnostics snapshot through the dedicated JSON path", async () => {
    vi.mocked(getFinancialDiagnostics).mockResolvedValue({
      schema: "openthesis.financial-diagnostics.v1", app: { version: "2.4.2", contract_version: "2.0" },
      run: { run_id: "run", status: "completed" }, company: { ticker: "1211" },
      financial_status: { state: "complete", expected_periods: [], available_periods: [], missing_periods: [], unverified_periods: [], attempt_count: 0, last_stage: "", updated_at: "", next_action: "none", snapshot_stale: false },
      recovery_cases: [], issues: [], active_compatibility_pack: null,
    });
    vi.mocked(exportFinancialDiagnostics).mockResolvedValue(true);
    render(<ReportWorkspace report={{ run_id: "run", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en", financial_status: { state: "complete", retryable: false, history_years: 2, expected_periods: [], available_periods: [], missing_periods: [], nodes: [], issues: [], attempt_count: 0, last_stage: "", last_error: "", updated_at: "", next_action: "none", model_calls: 0, token_delta: 0 }, markdown: "# Report", html: "" }} copy={COPY.en} />);
    fireEvent.click(screen.getByRole("button", { name: COPY.en.financialExportDiagnostics }));
    await waitFor(() => expect(getFinancialDiagnostics).toHaveBeenCalledWith("run"));
    await waitFor(() => expect(exportFinancialDiagnostics).toHaveBeenCalled());
  });

  it("offers cloud configuration when visual recognition is not configured", () => {
    const configure = vi.fn();
    render(<ReportWorkspace report={{ run_id: "run", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en", financial_status: { state: "warning", retryable: true, history_years: 2, expected_periods: [], available_periods: [], missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-parse", last_error: "", updated_at: "", next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0, recovery_cases: [{ accession_number: "acc", period: "2025", fields: [], stage: "filing-parse", error_code: "VISION_MODEL_REQUIRED", attempts: 1, status: "open", next_action: "configure_cloud" }] }, markdown: "# Report", html: "" }} copy={COPY.en} onConfigureCloud={configure} />);
    fireEvent.click(screen.getByRole("button", { name: COPY.en.financialConfigureCloud }));
    expect(configure).toHaveBeenCalledTimes(1);
  });
});
