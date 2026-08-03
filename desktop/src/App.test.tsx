import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  bootstrapBackend,
  getResearchReport,
  getResearchStatus,
  startResearch,
  updatePreferences,
} from "./backend";

vi.mock("./backend", () => ({
  bootstrapBackend: vi.fn(),
  getResearchReport: vi.fn(),
  updatePreferences: vi.fn(),
  startResearch: vi.fn(),
  getResearchStatus: vi.fn(),
  cancelResearch: vi.fn(),
}));

describe("report-first workbench", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(bootstrapBackend).mockResolvedValue({
      contract_version: "1.0",
      app_version: "1.0.0-alpha.1",
      capabilities: [],
      preferences: {
        ui_language: "en",
        report_language: "zh-CN",
        sidebar_collapsed: "false",
      },
      recent_runs: [],
    });
  });

  it("keeps the report workspace primary when no history exists", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Research workspace" })).toBeVisible();
    expect(screen.getByText("No research reports yet")).toBeVisible();
    expect(screen.getByRole("button", { name: "Collapse navigation" })).toBeVisible();
  });

  it("saves interface and report languages independently", async () => {
    vi.mocked(updatePreferences).mockResolvedValue({
      ui_language: "en",
      report_language: "zh-CN",
      sidebar_collapsed: "false",
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    fireEvent.change(screen.getByLabelText("Interface language"), {
      target: { value: "en" },
    });
    fireEvent.change(screen.getByLabelText("Report language"), {
      target: { value: "zh-CN" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(updatePreferences).toHaveBeenCalledWith({
      ui_language: "en",
      report_language: "zh-CN",
    });
  });

  it("loads the report when a research job completes", async () => {
    vi.mocked(startResearch).mockResolvedValue({
      job_id: "job-1",
      state: "queued",
      message: "",
      percent: 0,
      run_id: null,
    });
    vi.mocked(getResearchStatus).mockResolvedValue({
      job_id: "job-1",
      state: "completed",
      message: "Done",
      percent: 100,
      run_id: "run-1",
    });
    vi.mocked(getResearchReport).mockResolvedValue({
      run_id: "run-1",
      ticker: "DEMO",
      company_name: "Synthetic Demo",
      status: "partial",
      report_language: "zh-CN",
      markdown: "# Completed report",
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));

    expect(await screen.findByRole("heading", { name: "Synthetic Demo" }, { timeout: 2000 })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Completed report" })).toBeVisible();
  });
});
