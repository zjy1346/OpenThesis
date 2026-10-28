import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { COPY } from "../../i18n";
import { exportFinancialDiagnostics, exportResearchDiagnostics, getFinancialDiagnostics, getResearchDiagnostics, listConfiguredModels } from "../../backend";
import { stripReportPreamble } from "./ReportWorkspace";
import { ReportWorkspace } from "./ReportWorkspace";

vi.mock("../../backend", () => ({
  getResearchReport: vi.fn(), exportResearchReport: vi.fn(),
  getFinancialDiagnostics: vi.fn(), exportFinancialDiagnostics: vi.fn(),
  getResearchDiagnostics: vi.fn(), exportResearchDiagnostics: vi.fn(),
  listConfiguredModels: vi.fn(),
}));

describe("report presentation", () => {
  it.each(["completed", "partial"] as const)("exports research diagnostics for a %s report without requiring financial diagnostics", async (status) => {
    const diagnostics = {
      schema: "openthesis.research-diagnostics.v1",
      run: { run_id: "research-run", status, started_at: "now", completed_at: null, wall_elapsed_seconds: 45 },
      totals: { input_tokens: { value: null, known_sum: 0, unknown_attempts: 1 } },
      actual_cost: { amount: null, currency: null, status: "unknown" },
    };
    vi.mocked(getResearchDiagnostics).mockResolvedValue(diagnostics as never);
    vi.mocked(exportResearchDiagnostics).mockResolvedValue(true);
    render(<ReportWorkspace report={{ run_id: "research-run", ticker: "1211", company_name: "BYD", status, report_language: "en", markdown: "Body", html: "" }} copy={COPY.en} />);
    const button = screen.getByRole("button", { name: COPY.en.researchExportDiagnostics });
    expect(button).toBeVisible();
    fireEvent.click(button);
    await waitFor(() => expect(getResearchDiagnostics).toHaveBeenCalledWith("research-run"));
    await waitFor(() => expect(exportResearchDiagnostics).toHaveBeenCalledWith(diagnostics));
    expect(screen.getByRole("status")).toHaveTextContent(COPY.en.researchDiagnosticsExported);
    expect(getFinancialDiagnostics).not.toHaveBeenCalled();
  });

  it("shows an unconfirmed reporting currency without relabelling the listing currency", () => {
    render(<ReportWorkspace copy={COPY.en} report={{ run_id: "currency", ticker: "09988.HK", company_name: "Fixture",
      status: "partial", report_language: "en", listing_currency: "HKD", reporting_currency: "", markdown: "Body", html: "" }} />);
    expect(screen.getByText("Listing currency: HKD")).toBeInTheDocument();
    expect(screen.getByText("Reporting currency: Unconfirmed")).toBeInTheDocument();
    expect(screen.queryByText("Listing and reporting currency: HKD")).not.toBeInTheDocument();
  });
  it("offers budget-gap continuation only after explicit approval and keeps the report visible on failure", async () => {
    const approve = vi.fn().mockRejectedValue(new Error("MODEL_BUDGET_PLAN_CHANGED"));
    render(<ReportWorkspace copy={COPY.en} onApproveBudgetGaps={approve} report={{ run_id: "budget-run", ticker: "1211", company_name: "Fixture",
      status: "partial", report_language: "en", markdown: "Saved report body", html: "", call_plan: {
        plan_digest: "digest-1", max_calls: 3, max_input_estimate: 8_000, max_output_tokens: 20_000,
        pending_budget_gaps: [{ gap_id: "gap-growth", stage_id: "growth-opportunities", purpose: "generation", input_estimate: 1234,
          output_limit: 4096, diagnostic_code: "MODEL_RUN_CALL_BUDGET_EXHAUSTED" }],
      } }} />);

    expect(screen.getByText("Growth opportunities · Generation")).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Approve additional budget and continue gaps" }));

    await waitFor(() => expect(approve).toHaveBeenCalledWith("budget-run", "digest-1", ["gap-growth"]));
    expect(await screen.findByText(/The plan changed or approval did not complete/)).toHaveAttribute("role", "alert");
    expect(screen.getByText("Saved report body")).toBeInTheDocument();
  });

  it("forwards an explicit adaptive migration choice through the report workspace", async () => {
    const approve = vi.fn().mockResolvedValue(undefined);
    render(<ReportWorkspace copy={COPY.en} onApproveBudgetGaps={approve} report={{ run_id: "legacy-run", ticker: "1211", company_name: "Fixture",
      status: "partial", report_language: "en", markdown: "Saved report body", html: "", call_plan: {
        plan_digest: "legacy-digest", max_calls: null, max_input_estimate: null, max_output_tokens: null,
        limit_source: "unknown", adaptive_resume_available: true, migration_choice_required: true,
        pending_budget_gaps: [],
      } }} />);

    expect(approve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Keep current limit" }));
    expect(approve).toHaveBeenCalledWith("legacy-run", "legacy-digest", [], "keep");
  });
  it("exposes saved-content revalidation without a model-retry plan or configured model", async () => {
    const local = vi.fn(async () => undefined);
    render(<ReportWorkspace copy={COPY.en} onRevalidateSaved={local} report={{ run_id: "local", ticker: "1211", company_name: "Fixture",
      status: "partial", report_language: "en", markdown: "Saved body", html: "",
      recovery_plan: { available: false, target: "model-stages", stages: [], reason: "no_retryable_stage", input_artifact_ids: [],
        plan_hash: "local", operations: [{ code: "revalidate_saved_content", requires_provider: false, requires_confirmation: false, stages: ["base"] }] } }} />);
    fireEvent.click(screen.getByRole("button", { name: COPY.en.revalidateSaved }));
    await waitFor(() => expect(local).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: COPY.en.recoveryRetry })).not.toBeInTheDocument();
  });
  it("requires explicit consent before replacing an unknown request", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([{
      configured_model_id: "m", connection_id: "c", model_id: "capable", alias: "Capable",
      free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null,
      enabled: true, capabilities: ["text_chat"], health_status: "ready", last_discovered_at: null,
      context_window_hint: null, temperature: null, timeout_seconds: 120, configuration_version: 3,
    }]);
    const resume = vi.fn(async () => undefined);
    render(<ReportWorkspace copy={COPY.en} onRetryRecoveryStage={resume} report={{
      run_id: "unknown", ticker: "1211", company_name: "BYD", status: "partial",
      report_language: "en", markdown: "# Saved results", html: "",
      recovery_plan: { target: "model-stages", stages: ["business-analyst"], reason: "base_stage_incomplete",
        input_artifact_ids: [], plan_hash: "frozen-plan", available: true,
        requires_new_attempt_authorization: true, unknown_stages: ["business-analyst"] },
    }} />);
    const button = await screen.findByRole("button", { name: COPY.en.recoveryRetry });
    await waitFor(() => expect(screen.getByLabelText(COPY.en.recoveryModel)).toHaveValue("m"));
    expect(button).toBeDisabled();
    expect(screen.getByText(COPY.en.recoveryUnknownWarning)).toBeInTheDocument();
    const consent = screen.getByRole("checkbox", { name: COPY.en.recoveryConfirmNewAttempt });
    expect(consent.closest("label")).toHaveClass("checkbox-label");
    fireEvent.click(consent);
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(resume).toHaveBeenCalledWith("model-stages", {
      configured_model_id: "m", connection_id: "c", configuration_version: 3, role: "primary", context_window_hint: null,
    }, "frozen-plan", true));
  });
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
    const recovery = screen.getByRole("combobox", { name: COPY.en.synthesisCapacityModel }).closest(".synthesis-capacity-recovery");
    expect(recovery).toHaveClass("synthesis-capacity-recovery");
    expect(recovery?.querySelector(".synthesis-capacity-explanation")).toBeInTheDocument();
    expect(recovery?.querySelector(".synthesis-capacity-control")).toBeInTheDocument();
    expect(recovery?.querySelector(".synthesis-capacity-action-button")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.en.retrySynthesis })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: COPY.en.synthesisCapacityRetry }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith({ configured_model_id: "large", connection_id: "c2", configuration_version: 4, role: "primary", context_window_hint: 200_000 }));
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
      { configured_model_id: "selected", connection_id: "c2", configuration_version: 4, role: "primary", context_window_hint: 200_000 },
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

  it("keeps local financial repair visible and only claims stale AI conclusions when research output exists", async () => {
    let resolveRetry: (() => void) | undefined;
    const retryFinancials = vi.fn(() => new Promise<void>((resolve) => { resolveRetry = resolve; }));
    render(<ReportWorkspace report={{ run_id: "run", ticker: "9988", company_name: "Alibaba", status: "completed", report_language: "en", is_substantive: true, financial_status: { state: "incomplete", retryable: true, history_years: 2, expected_periods: ["2026", "2025", "2024"], available_periods: ["2026", "2024"], missing_periods: ["2025 FY"], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-download", last_error: "temporary_timeout", updated_at: "", next_action: "retry_missing_periods", model_calls: 0, token_delta: 0, snapshot_stale: true, has_research_output: true }, markdown: "# Report", html: "" }} copy={COPY.en} onRetryFinancials={retryFinancials} />);

    expect(screen.getByText("Missing filing periods: 2025 annual filing")).toBeInTheDocument();
    expect(screen.getByText(COPY.en.financialModelBoundary)).toBeInTheDocument();
    expect(screen.getByText(COPY.en.financialSnapshotStale)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: COPY.en.retryFinancials }));
    expect(retryFinancials).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(COPY.en.retryingFinancials)).toBeInTheDocument();
    resolveRetry?.();
  });

  it.each(["en", "zh-CN", "zh-Hant"] as const)("offers the local recognition setup action for unfinished financials in %s", (language) => {
    const configure = vi.fn();
    const copy = COPY[language];
    render(<ReportWorkspace report={{
      run_id: "local-structure", ticker: "1211", company_name: "BYD", status: "partial", report_language: language,
      financial_status: {
        state: "incomplete", retryable: false, history_years: 1, expected_periods: ["2025"],
        available_periods: [], missing_periods: ["2025"], nodes: [], issues: [], attempt_count: 1,
        last_stage: "filing-parse", last_error: "", updated_at: "", next_action: "none", model_calls: 0, token_delta: 0,
        local_structure: { available: false, enabled: false, error_code: "STRUCTURE_COMPONENT_NOT_INSTALLED" },
      },
      markdown: "# Saved report", html: "",
    }} copy={copy} onConfigureCloud={configure} />);

    expect(screen.getByText(copy.localStructureConfigureBody)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: copy.localStructureConfigure }));
    expect(configure).toHaveBeenCalledTimes(1);
  });

  it("hides local recognition setup when enabled, financials are complete, or the report is legacy", () => {
    const base = {
      retryable: false, history_years: 1, expected_periods: [], available_periods: [], missing_periods: [],
      nodes: [], issues: [], attempt_count: 0, last_stage: "", last_error: "", updated_at: "", next_action: "none",
      model_calls: 0, token_delta: 0,
    };
    const configure = vi.fn();
    const { rerender } = render(<ReportWorkspace report={{
      run_id: "enabled", ticker: "1211", company_name: "BYD", status: "partial", report_language: "en",
      financial_status: { ...base, state: "incomplete", local_structure: { available: true, enabled: true, error_code: "" } },
      markdown: "# Saved report", html: "",
    }} copy={COPY.en} onConfigureCloud={configure} />);
    expect(screen.queryByRole("button", { name: COPY.en.localStructureConfigure })).not.toBeInTheDocument();

    rerender(<ReportWorkspace report={{
      run_id: "complete", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en",
      financial_status: { ...base, state: "complete", local_structure: { available: false, enabled: false, error_code: "STRUCTURE_COMPONENT_NOT_INSTALLED" } },
      markdown: "# Saved report", html: "",
    }} copy={COPY.en} onConfigureCloud={configure} />);
    expect(screen.queryByRole("button", { name: COPY.en.localStructureConfigure })).not.toBeInTheDocument();

    rerender(<ReportWorkspace report={{
      run_id: "legacy", ticker: "1211", company_name: "BYD", status: "partial", report_language: "en",
      financial_status: { ...base, state: "incomplete" },
      markdown: "# Saved report", html: "",
    }} copy={COPY.en} onConfigureCloud={configure} />);
    expect(screen.queryByRole("button", { name: COPY.en.localStructureConfigure })).not.toBeInTheDocument();
    expect(configure).not.toHaveBeenCalled();
  });

  it("hides local recognition setup when no settings-navigation callback is available", () => {
    render(<ReportWorkspace report={{
      run_id: "no-settings", ticker: "1211", company_name: "BYD", status: "partial", report_language: "en",
      financial_status: {
        state: "warning", retryable: false, history_years: 1, expected_periods: [], available_periods: [], missing_periods: [],
        nodes: [], issues: [], attempt_count: 0, last_stage: "", last_error: "", updated_at: "", next_action: "none",
        model_calls: 0, token_delta: 0,
        local_structure: { available: false, enabled: false, error_code: "STRUCTURE_COMPONENT_NOT_INSTALLED" },
      },
      markdown: "# Saved report", html: "",
    }} copy={COPY.en} />);

    expect(screen.queryByRole("button", { name: COPY.en.localStructureConfigure })).not.toBeInTheDocument();
    expect(screen.queryByText(COPY.en.localStructureConfigureBody)).not.toBeInTheDocument();
  });

  it.each([false, undefined] as const)("does not call a deterministic financial overview stale AI research when research output is %s", (hasResearchOutput) => {
    render(<ReportWorkspace report={{
      run_id: "financial-only", ticker: "9988", company_name: "Alibaba", status: "completed", report_language: "en",
      is_substantive: true,
      financial_status: { state: "warning", retryable: false, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "artifact-rebuild", last_error: "",
        updated_at: "", next_action: "none", model_calls: 0, token_delta: 0, snapshot_stale: true,
        ...(hasResearchOutput === undefined ? {} : { has_research_output: hasResearchOutput }) },
      markdown: "# Deterministic financial overview", html: "",
    }} copy={COPY.en} />);

    expect(screen.queryByText(COPY.en.financialSnapshotStale)).not.toBeInTheDocument();
  });

  it("does not show the AI stale banner when the report itself is not substantive", () => {
    render(<ReportWorkspace report={{
      run_id: "stage-only", ticker: "9988", company_name: "Alibaba", status: "partial", report_language: "en",
      is_substantive: false,
      financial_status: { state: "warning", retryable: false, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "artifact-rebuild", last_error: "",
        updated_at: "", next_action: "none", model_calls: 0, token_delta: 0, snapshot_stale: true,
        has_research_output: true },
      markdown: "", html: "",
    }} copy={COPY.en} />);

    expect(screen.queryByText(COPY.en.financialSnapshotStale)).not.toBeInTheDocument();
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

    expect(screen.getByText("Failed filing periods: 2025 annual filing")).toBeInTheDocument();
    expect(screen.getByText("Failed fields: Revenue")).toBeInTheDocument();
    expect(screen.getByText("Failed stage: Financial validation")).toBeInTheDocument();
    expect(screen.getByText(/Official filing retrieval or parsing did not complete/)).toBeInTheDocument();
    expect(screen.queryByText(/FILING_DATA_QUALITY_FAILED/)).not.toBeInTheDocument();
    const accession = screen.getByText("acc-2025");
    const details = accession.closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(accession.closest("summary")).toBeNull();
    fireEvent.click(details?.querySelector("summary") as HTMLElement);
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("acc-2025")).toBeInTheDocument();
  });

  it("labels local PDF reading separately from financial validation", () => {
    render(<ReportWorkspace report={{
      run_id: "reading", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en",
      financial_status: { state: "warning", retryable: true, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-parse", last_error: "",
        updated_at: "", next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0,
        recovery_cases: [{ accession_number: "acc-reading", period: "2025 FY", fields: [], stage: "filing-parse",
          error_code: "LOCAL_READ_FAILED", attempts: 1, status: "open", next_action: "retry_local_parse" }] },
      markdown: "# Report", html: "",
    }} copy={COPY.en} />);

    expect(screen.getByText("Failed stage: Local reading")).toBeInTheDocument();
    expect(screen.getByText("Attempts: 1 · Local reading")).toBeInTheDocument();
    expect(screen.queryByText(/filing-parse/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(COPY.en.financialStageStatus)).toHaveTextContent("Local reading: Complete");
    expect(screen.getByLabelText(COPY.en.financialStageStatus)).toHaveTextContent("Financial validation: In progress");
  });

  it("hides retry, rebuild, and cloud activation for a required local runtime repair but keeps diagnostics", () => {
    const configure = vi.fn();
    const retryFinancials = vi.fn(async () => undefined);
    const rebuildFinancials = vi.fn(async () => undefined);
    const retryModelStages = vi.fn(async () => undefined);
    const retrySynthesis = vi.fn(async () => undefined);
    render(<ReportWorkspace report={{
      run_id: "runtime", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en",
      retryable_model_stages: true, retryable_synthesis: true,
      financial_status: { state: "incomplete", retryable: true, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-parse", last_error: "",
        updated_at: "", next_action: "repair_local_pdf_runtime", model_calls: 0, token_delta: 0,
        failure_layer: "runtime", runtime_repair_required: true,
        recovery_cases: [{ accession_number: "acc", period: "2025 FY", fields: [], stage: "filing-parse",
          error_code: "VISION_MODEL_REQUIRED", attempts: 1, status: "open", next_action: "configure_cloud" }] },
      markdown: "# Report", html: "",
    }} copy={COPY.en} onRetryFinancials={retryFinancials} onRebuildFinancials={rebuildFinancials}
      onRetryModelStages={retryModelStages} onRetrySynthesis={retrySynthesis} onConfigureCloud={configure} />);

    expect(screen.getByText(COPY.en.financialRuntimeRepairGuidance)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: COPY.en.financialExportDiagnostics })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.en.retryFinancials })).not.toBeInTheDocument();
    expect(screen.queryByText(COPY.en.rebuildFinancials)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.en.financialConfigureCloud })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.en.synthesisCapacityConfigure })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.en.retryModelStages })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.en.retrySynthesis })).not.toBeInTheDocument();
    expect(retryFinancials).not.toHaveBeenCalled();
    expect(rebuildFinancials).not.toHaveBeenCalled();
    expect(retryModelStages).not.toHaveBeenCalled();
    expect(retrySynthesis).not.toHaveBeenCalled();
    expect(configure).not.toHaveBeenCalled();
  });

  it("allows one local financial retry when a historical runtime failure is explicitly cleared", () => {
    const retryFinancials = vi.fn(async () => undefined);
    render(<ReportWorkspace report={{
      run_id: "runtime-cleared", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en",
      financial_status: { state: "incomplete", retryable: true, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-parse", last_error: "",
        updated_at: "", next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0,
        failure_layer: "runtime", runtime_repair_required: false },
      markdown: "# Report", html: "",
    }} copy={COPY.en} onRetryFinancials={retryFinancials} />);

    const retry = screen.getByRole("button", { name: COPY.en.retryFinancials });
    expect(retry).toBeEnabled();
    expect(screen.queryByText(COPY.en.financialRuntimeRepairGuidance)).not.toBeInTheDocument();
    fireEvent.click(retry);
    expect(retryFinancials).toHaveBeenCalledTimes(1);
  });

  it("offers the existing model-eligible resume action when materials are ready but research is empty", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([{
      configured_model_id: "m", connection_id: "c", model_id: "capable", alias: "Capable",
      free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null,
      enabled: true, capabilities: ["text_chat"], health_status: "ready", last_discovered_at: null,
      context_window_hint: null, temperature: null, timeout_seconds: 120, configuration_version: 3,
    }]);
    const resume = vi.fn(async () => undefined);
    render(<ReportWorkspace copy={COPY.en} onRetryRecoveryStage={resume} report={{
      run_id: "ready-run", ticker: "1211", company_name: "BYD", status: "failed", report_language: "en",
      is_substantive: false, markdown: "", html: "",
      financial_status: { state: "complete", retryable: false, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "artifact-rebuild", last_error: "",
        updated_at: "", next_action: "none", model_calls: 0, token_delta: 0, failure_layer: "ready" },
      recovery_plan: { target: "model-stages", stages: ["business-analyst"], reason: "base_stage_incomplete",
        input_artifact_ids: [], plan_hash: "ready-plan", available: true },
    }} />);

    expect(await screen.findByText(COPY.en.reportMaterialsReadyContinue)).toBeInTheDocument();
    expect(screen.getByText(COPY.en.recoveryResumeBody)).toBeInTheDocument();
    const resumeButton = await screen.findByRole("button", { name: COPY.en.recoveryResumeAction });
    expect(resumeButton).toBeEnabled();
    fireEvent.click(resumeButton);
    await waitFor(() => expect(resume).toHaveBeenCalledWith("model-stages", {
      configured_model_id: "m", connection_id: "c", configuration_version: 3, role: "primary", context_window_hint: null,
    }, "ready-plan"));
  });

  it("offers model resume for a deterministic financial summary without AI research output", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([{
      configured_model_id: "m", connection_id: "c", model_id: "capable", alias: "Capable",
      free_tier: false, billing_class: "paid", free_source_url: null, free_verified_at: null,
      enabled: true, capabilities: ["text_chat"], health_status: "ready", last_discovered_at: null,
      context_window_hint: null, temperature: null, timeout_seconds: 120, configuration_version: 3,
    }]);
    const resume = vi.fn(async () => undefined);
    render(<ReportWorkspace copy={COPY.en} onRetryRecoveryStage={resume} report={{
      run_id: "financial-only-run", ticker: "1211", company_name: "BYD", status: "completed", report_language: "en",
      is_substantive: true, markdown: "# Deterministic financial summary", html: "",
      financial_status: { state: "complete", retryable: false, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "artifact-rebuild", last_error: "",
        updated_at: "", next_action: "none", model_calls: 0, token_delta: 0, snapshot_stale: true,
        failure_layer: "ready", has_research_output: false },
      recovery_plan: { target: "model-stages", stages: ["business-analyst"], reason: "base_stage_incomplete",
        input_artifact_ids: [], plan_hash: "financial-only-plan", available: true },
    }} />);

    expect(screen.getByText("Deterministic financial summary")).toBeInTheDocument();
    expect(screen.getByText(COPY.en.recoveryResumeTitle)).toBeInTheDocument();
    expect(screen.getByText(COPY.en.recoveryResumeBody)).toBeInTheDocument();
    expect(screen.queryByText(COPY.en.financialSnapshotStale)).not.toBeInTheDocument();
    const resumeButton = await screen.findByRole("button", { name: COPY.en.recoveryResumeAction });
    expect(resumeButton).toBeEnabled();
    fireEvent.click(resumeButton);
    await waitFor(() => expect(resume).toHaveBeenCalledWith("model-stages", {
      configured_model_id: "m", connection_id: "c", configuration_version: 3, role: "primary", context_window_hint: null,
    }, "financial-only-plan"));
  });

  it("keeps an actionable local financial repair button in the empty-report state", () => {
    const retryFinancials = vi.fn(async () => undefined);
    render(<ReportWorkspace copy={COPY.en} onRetryFinancials={retryFinancials} report={{
      run_id: "empty-financial", ticker: "1211", company_name: "BYD", status: "failed", report_language: "en",
      is_substantive: false, markdown: "", html: "",
      financial_status: { state: "incomplete", retryable: true, history_years: 1, expected_periods: [], available_periods: [],
        missing_periods: [], nodes: [], issues: [], attempt_count: 1, last_stage: "filing-download", last_error: "",
        updated_at: "", next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0 },
    }} />);

    fireEvent.click(screen.getByRole("button", { name: COPY.en.retryFinancials }));
    expect(retryFinancials).toHaveBeenCalledTimes(1);
  });

  it.each(["en", "zh-CN", "zh-Hant"] as const)("explains localized partial financial refresh in %s", (language) => {
    const copy = COPY[language];
    render(<ReportWorkspace report={{
      run_id: "run", ticker: "688981", company_name: "Fixture", status: "partial", report_language: language,
      financial_retry: { mode: "retry", targets: ["2026H1"], downloaded: [], accepted: ["revenue"],
        rejected: ["roe"], status: "partial", error: "quality:reported_roe", research_can_continue: true,
        updated_artifacts: ["deterministic-financial-summary", "research-report"],
        scoped_issues: [{ source_accession: "filing", period_end: "2026-06-30", fiscal_period: "H1",
          concepts: ["reported_roe"], fact_ids: ["roe"], issues: ["conflict"], capability_scope: "dependent_metrics_only" }] },
      financial_status: { state: "warning", retryable: true, history_years: 1, expected_periods: [],
        available_periods: [], missing_periods: [], nodes: [], issues: [], attempt_count: 1,
        last_stage: "filing-validation", last_error: "", updated_at: "", next_action: "none",
        model_calls: 0, token_delta: 0 }, markdown: "# Retained research", html: "",
    }} copy={copy} />);
    expect(screen.getByText(copy.financialPartialResearchAvailable)).toBeInTheDocument();
    expect(screen.getByText(`${copy.financialFailedFields}: ${copy.financialMetricLabels.reported_roe}`)).toBeInTheDocument();
    expect(screen.getByLabelText(copy.financialStageStatus)).toHaveTextContent(copy.financialStagePartial);
    expect(screen.getByLabelText(copy.financialStageStatus)).toHaveTextContent(`${copy.financialStageRefresh}: ${copy.financialStageDone}`);
  });

  it("renders historical partial financial results without updated_artifacts", () => {
    const legacyFinancialRetry = {
      mode: "retry", targets: ["2025"], downloaded: ["2025"], accepted: [], rejected: ["roe"],
      status: "partial", error: "quality:reported_roe", research_can_continue: true,
    } as unknown as import("../../types").ResearchReport["financial_retry"];
    render(<ReportWorkspace report={{
      run_id: "legacy-partial", ticker: "688981", company_name: "Fixture", status: "partial",
      report_language: "en", financial_retry: legacyFinancialRetry,
      financial_status: { state: "warning", retryable: true, history_years: 1,
        expected_periods: ["2025"], available_periods: [], missing_periods: ["2025"],
        nodes: [], issues: [], attempt_count: 1, last_stage: "filing-validation", last_error: "",
        updated_at: "", next_action: "retry_failed_nodes", model_calls: 0, token_delta: 0 },
      markdown: "# Retained report", html: "",
    }} copy={COPY.en} />);

    expect(screen.getByRole("heading", { name: "Retained report" })).toBeInTheDocument();
    expect(screen.getByLabelText(COPY.en.financialStageStatus)).toHaveTextContent(
      `${COPY.en.financialStageValidation}: ${COPY.en.financialStagePartial}`,
    );
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

  it.each(["PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION", "DOCUMENT_CONTEXT_OCR_REQUIRED", "PDF_TEXT_EXTRACTION_FAILED"])(
    "offers an explicit optional visual recovery action for %s without starting an upload", (code) => {
      const configure = vi.fn();
      render(<ReportWorkspace report={{ run_id: "visual-recovery", ticker: "1211", company_name: "BYD",
        status: "partial", report_language: "en", markdown: "# Financial overview", html: "",
        financial_status: { state: "warning", retryable: true, history_years: 2, expected_periods: [],
          available_periods: [], missing_periods: [], nodes: [], issues: [], attempt_count: 1,
          last_stage: "filing-parse", last_error: code, updated_at: "",
          next_action: "retry_saved_candidate_validation_or_enable_authorized_cloud", model_calls: 0, token_delta: 0,
          failure_layer: "extraction", runtime_repair_required: false,
          recovery_cases: [{ accession_number: "acc", period: "2025 Q1", fields: [], stage: "filing-parse",
            error_code: code, attempts: 1, status: "open", next_action: "authorize_scanned_page_ocr" }] },
      }} copy={COPY.en} onConfigureCloud={configure} />);
      expect(configure).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: COPY.en.financialConfigureCloud }));
      expect(configure).toHaveBeenCalledTimes(1);
    },
  );
});
