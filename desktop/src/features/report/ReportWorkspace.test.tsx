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
});
