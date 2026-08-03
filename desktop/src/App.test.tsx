import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  bootstrapBackend,
  discoverModels,
  exportResearchReport,
  installResearchPack,
  openExternalUrl,
  testModelConnection,
  getResearchReport,
  getResearchStatus,
  startResearch,
  updatePreferences,
} from "./backend";

vi.mock("./backend", () => ({
  bootstrapBackend: vi.fn(),
  getResearchReport: vi.fn(),
  getThesis: vi.fn(),
  listTheses: vi.fn(),
  saveThesis: vi.fn(),
  updatePreferences: vi.fn(),
  discoverModels: vi.fn(),
  exportResearchReport: vi.fn(),
  installResearchPack: vi.fn(),
  openExternalUrl: vi.fn(),
  testModelConnection: vi.fn(),
  searchCompanies: vi.fn(),
  startResearch: vi.fn(),
  getResearchStatus: vi.fn(),
  cancelResearch: vi.fn(),
}));

describe("report-first workbench", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(bootstrapBackend).mockResolvedValue({
      contract_version: "1.0",
      app_version: "1.0.4",
      capabilities: [],
      preferences: {
        ui_language: "en",
        report_language: "zh-CN",
        sidebar_collapsed: "false",
        parallel_agents: "false",
      },
      recent_runs: [],
      common_companies: [
        { cik: "0000320193", ticker: "AAPL", name: "Apple Inc.", exchange: "Nasdaq" },
      ],
      model_catalog: [
        {
          preset_id: "none",
          label: "No AI",
          region: "Offline",
          protocol: "none",
          base_url: "",
          recommended_models: [],
          models_path: null,
          help_url: "",
          requires_api_key: false,
          temperature: null,
        },
        {
          preset_id: "openai",
          label: "OpenAI",
          region: "Global",
          protocol: "openai-compatible",
          base_url: "https://api.openai.com/v1",
          recommended_models: ["gpt-test"],
          models_path: "/models",
          help_url: "https://platform.openai.com/api-keys",
          requires_api_key: true,
          temperature: null,
        },
      ],
      research_packs: [
        { pack_id: "official.long-term-fundamentals", name: "Long-term", version: "1", content_hash: "hash" },
      ],
      interrupted_runs: 0,
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
      parallel_agents: "false",
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
      html: "<!doctype html><h1>Completed report</h1>",
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));

    expect(await screen.findByRole("heading", { name: "Synthetic Demo" }, { timeout: 2000 })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Completed report" })).toBeVisible();

    const reportDocument = screen.getByRole("article");
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(reportDocument).toHaveStyle({ "--report-scale": "1.1" });

    fireEvent.click(screen.getByRole("button", { name: "Show technical details" }));
    await waitFor(() => {
      expect(getResearchReport).toHaveBeenLastCalledWith("run-1", "zh-CN", true);
    });

    fireEvent.click(screen.getByRole("button", { name: "Enter focus mode" }));
    expect(reportDocument).toHaveAttribute("data-focus", "focused");
    fireEvent.click(screen.getByRole("button", { name: "Exit focus mode" }));
    await waitFor(() => expect(reportDocument).not.toHaveAttribute("data-focus"));

    fireEvent.keyDown(window, { key: "F11" });
    expect(reportDocument).toHaveAttribute("data-focus", "focused");
    expect(reportDocument).toHaveAttribute("data-focus-motion", "skip");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(reportDocument).not.toHaveAttribute("data-focus");

    fireEvent.click(screen.getByRole("button", { name: "Export report" }));
    await waitFor(() => expect(exportResearchReport).toHaveBeenCalled());
  });

  it("keeps API keys session-only while refreshing models", async () => {
    vi.mocked(discoverModels).mockResolvedValue({
      preset_id: "openai",
      models: ["gpt-test", "gpt-remote"],
      warning: "",
    });
    vi.mocked(testModelConnection).mockResolvedValue({ ok: true, message: "connected" });
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.change(screen.getByLabelText("Model provider"), {
      target: { value: "openai" },
    });
    fireEvent.change(screen.getByLabelText("API Key (session only)"), {
      target: { value: "sk-session" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Refresh online models" }));
    fireEvent.click(screen.getByRole("button", { name: "Test model connection" }));
    fireEvent.click(screen.getByRole("button", { name: "Get API Key help" }));

    expect(discoverModels).toHaveBeenCalledWith({
      preset_id: "openai",
      base_url: "https://api.openai.com/v1",
      api_key: "sk-session",
    });
    expect(testModelConnection).toHaveBeenCalledWith({
      preset_id: "openai",
      model: "gpt-test",
      base_url: "https://api.openai.com/v1",
      api_key: "sk-session",
    });
    expect(openExternalUrl).toHaveBeenCalledWith("https://platform.openai.com/api-keys");
    expect(updatePreferences).not.toHaveBeenCalledWith(
      expect.objectContaining({ api_key: expect.anything() }),
    );
  });

  it("shows every recommended and refreshed model in the visible selector", async () => {
    vi.mocked(discoverModels).mockResolvedValue({
      preset_id: "openai",
      models: ["gpt-test", "gpt-remote"],
      warning: "",
    });
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.change(screen.getByLabelText("Model provider"), { target: { value: "openai" } });

    const modelSelect = screen.getByRole("combobox", { name: "Model name" });
    expect(within(modelSelect).getByRole("option", { name: "gpt-test" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh online models" }));
    await waitFor(() => expect(within(modelSelect).getByRole("option", { name: "gpt-remote" })).toBeInTheDocument());
    expect(screen.getByText("Model catalog updated.")).toBeVisible();
  });

  it("opens SEC guidance through the native external-link adapter", async () => {
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.click(screen.getByText("SEC identity help"));
    fireEvent.click(screen.getByRole("button", { name: "Open official SEC developer guidance" }));

    expect(openExternalUrl).toHaveBeenCalledWith(
      "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
    );
  });

  it("refreshes history without restarting the workbench", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Research workspace" });

    fireEvent.click(screen.getByRole("button", { name: "Refresh research history" }));

    await waitFor(() => expect(bootstrapBackend).toHaveBeenCalledTimes(2));
  });

  it("keeps the last research request session-only for retry", async () => {
    vi.mocked(startResearch).mockRejectedValue(new Error("temporary provider failure"));
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run again" }));

    await waitFor(() => expect(startResearch).toHaveBeenCalledTimes(2));
    expect(startResearch).toHaveBeenLastCalledWith({ mode: "demo" });
    expect(updatePreferences).not.toHaveBeenCalledWith(
      expect.objectContaining({ api_key: expect.anything() }),
    );
  });

  it("shows architecture diagnostics in the About view", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "About OpenThesis" }));

    expect(screen.getByRole("heading", { name: "About OpenThesis" })).toBeVisible();
    expect(screen.getByText("1.0.4")).toBeVisible();
    expect(screen.getByText("JSON-RPC 1.0")).toBeVisible();
  });

  it("imports a declarative research pack without native path access", async () => {
    vi.mocked(installResearchPack).mockResolvedValue({
      pack_id: "custom.pack",
      name: "Custom Pack",
      version: "1",
      content_hash: "custom-hash",
    });
    render(<App />);
    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);

    const file = new File(["pack"], "custom.othesis", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText("Import .othesis research pack"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(installResearchPack).toHaveBeenCalled());
    expect(await screen.findByRole("option", { name: "Custom Pack · 1" })).toBeVisible();
  });
});
