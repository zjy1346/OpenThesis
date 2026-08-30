import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { COPY } from "../../i18n";
import { stripReportPreamble } from "./ReportWorkspace";
import { ReportWorkspace } from "./ReportWorkspace";

vi.mock("../../backend", () => ({ getResearchReport: vi.fn(), exportResearchReport: vi.fn() }));

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
});
