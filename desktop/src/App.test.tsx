import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  bootstrapBackend,
  exportResearchReport,
  installResearchPack,
  listConfiguredModels,
  openExternalUrl,
  searchCompanies,
  getResearchReport,
  getResearchStatus,
  startResearch,
  updatePreferences,
} from "./backend";

vi.mock("./backend", () => ({
  bootstrapBackend: vi.fn(),
  deleteResearchRun: vi.fn(),
  getResearchReport: vi.fn(),
  getThesis: vi.fn(),
  listTheses: vi.fn(),
  saveThesis: vi.fn(),
  updatePreferences: vi.fn(),
  exportResearchReport: vi.fn(),
  installResearchPack: vi.fn(),
  listConfiguredModels: vi.fn(),
  listModelProviders: vi.fn(),
  listProviderConnections: vi.fn(),
  openExternalUrl: vi.fn(),
  searchCompanies: vi.fn(),
  startResearch: vi.fn(),
  getResearchStatus: vi.fn(),
  cancelResearch: vi.fn(),
}));

describe("report-first workbench", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listConfiguredModels).mockResolvedValue([
      {
        configured_model_id: "openai-primary",
        connection_id: "openai-main",
        model_id: "gpt-test",
        alias: "Primary GPT",
        free_tier: false,
        billing_class: "paid",
        enabled: true,
        health_status: "ready",
        configuration_version: 3,
        free_source_url: null,
        free_verified_at: null,
        last_discovered_at: null,
        context_window_hint: null,
        temperature: null,
        timeout_seconds: 180,
        capabilities: ["text_chat", "structured_json"],
      },
      {
        configured_model_id: "openrouter-free",
        connection_id: "openrouter-main",
        model_id: "openrouter/free",
        alias: "OpenRouter Free",
        free_tier: true,
        billing_class: "free_tier",
        enabled: true,
        health_status: "ready",
        configuration_version: 1,
        free_source_url: null,
        free_verified_at: null,
        last_discovered_at: null,
        context_window_hint: null,
        temperature: null,
        timeout_seconds: 180,
        capabilities: ["text_chat", "structured_json"],
      },
    ]);
    vi.mocked(bootstrapBackend).mockResolvedValue({
      contract_version: "2",
      app_version: "2.0.0",
      capabilities: [],
      preferences: {
        ui_language: "en",
        report_language: "zh-CN",
        sidebar_collapsed: "true",
        parallel_agents: "false",
        research_market: "US",
      },
      recent_runs: [],
      common_companies: [
        { cik: "0000320193", ticker: "AAPL", name: "Apple Inc.", exchange: "Nasdaq" },
        { cik: "CN_A:BSE:832982.BJ", ticker: "832982.BJ", name: "Jinbo Bio", exchange: "BSE", market: "CN_A", listing_currency: "CNY" },
      ],
      market_catalog: [
        { market: "US", label_zh: "美股", label_en: "US equities", exchanges: ["NASDAQ", "NYSE"], default_currency: "USD", requires_sec_identity: true, disclosure_home: "https://www.sec.gov/edgar/search/" },
        { market: "CN_A", label_zh: "A 股", label_en: "China A-shares", exchanges: ["SSE", "SZSE", "BSE"], default_currency: "CNY", requires_sec_identity: false, disclosure_home: "https://www.cninfo.com.cn/new/index" },
        { market: "HK", label_zh: "港股", label_en: "Hong Kong equities", exchanges: ["HKEX"], default_currency: "HKD", requires_sec_identity: false, disclosure_home: "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh" },
      ],
      research_packs: [
        { pack_id: "official.long-term-fundamentals", name: "Long-term", version: "1", content_hash: "hash" },
      ],
      interrupted_runs: 0,
    });
  });

  it("keeps the report workspace primary when no history exists", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Research report" })).toBeVisible();
    expect(screen.getByText("No research reports yet")).toBeVisible();
    expect(screen.getByRole("button", { name: "Open navigation" })).toBeVisible();
  });

  it("expands navigation on hover and can pin it open without moving the icon rail", async () => {
    vi.mocked(updatePreferences).mockResolvedValue({
      ui_language: "en",
      report_language: "zh-CN",
      sidebar_collapsed: "false",
      parallel_agents: "false",
    });
    render(<App />);

    await screen.findByRole("heading", { name: "Research report" });
    const navigation = screen.getByRole("complementary", { name: "OpenThesis primary navigation" });
    const labels = screen.getByRole("navigation", { name: "Navigation labels", hidden: true });
    const drawer = labels.closest(".nav-drawer");
    expect(drawer).toHaveAttribute("aria-hidden", "true");

    fireEvent.mouseEnter(navigation);
    expect(drawer).toHaveAttribute("aria-hidden", "false");
    fireEvent.mouseLeave(navigation);
    expect(drawer).toHaveAttribute("aria-hidden", "true");

    fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    fireEvent.mouseLeave(navigation);
    expect(drawer).toHaveAttribute("aria-hidden", "false");
    expect(updatePreferences).toHaveBeenCalledWith({ sidebar_collapsed: "false" });
  });

  it("uses the active feature title and gives history its own page", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Research history" }));
    expect(screen.getByRole("heading", { name: "Research history" })).toBeVisible();
    expect(screen.getByText(/No research history yet/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("heading", { name: "Settings" })).toBeVisible();
  });

  it("opens the bilingual help center with both built-in guides", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Help" }));
    expect(screen.getByRole("heading", { name: "Help" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "From model setup to a first research report" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Author .ot files in OT Studio" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Research US, mainland China, and Hong Kong listings" })).toBeVisible();
  });

  it("gates the first frame until a manual language preference is applied", async () => {
    const originalLanguages = window.navigator.languages;
    Object.defineProperty(window.navigator, "languages", { configurable: true, value: ["zh-Hant-TW"] });
    render(<App />);
    expect(screen.queryByRole("button", { name: "研究歷史" })).toBeNull();
    expect(await screen.findByRole("heading", { name: "Research report" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "研究歷史" })).toBeNull();
    expect(document.documentElement.lang).toBe("en");
    Object.defineProperty(window.navigator, "languages", { configurable: true, value: originalLanguages });
  });

  it("searches BSE without requiring an SEC email", async () => {
    vi.mocked(searchCompanies).mockResolvedValue([
      { cik: "CN_A:BSE:832982.BJ", ticker: "832982.BJ", name: "Jinbo Bio", exchange: "BSE", market: "CN_A", listing_currency: "CNY" },
    ]);
    vi.mocked(updatePreferences).mockImplementation(async (value) => ({
      ui_language: "en",
      report_language: "zh-CN",
      sidebar_collapsed: "true",
      parallel_agents: "false",
      research_market: "CN_A",
      ...Object.fromEntries(Object.entries(value).map(([key, item]) => [key, String(item)])),
    }));
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.change(screen.getByLabelText("Listing market"), { target: { value: "CN_A" } });
    expect(screen.queryByLabelText("SEC contact email")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search ticker or company name"), { target: { value: "832982" } });
    fireEvent.click(screen.getByRole("button", { name: "Search companies" }));

    await waitFor(() => expect(searchCompanies).toHaveBeenCalledWith("832982", "CN_A"));
    expect(await screen.findByText("Jinbo Bio")).toBeVisible();
    expect(screen.getByLabelText("Listing market").closest(".market-source-row")).not.toBeNull();
  });

  it("shows classified no-filing actions without exposing technical errors", async () => {
    vi.mocked(startResearch).mockResolvedValue({
      job_id: "job-no-filings",
      state: "queued",
      message: "",
      percent: 0,
      run_id: null,
    });
    vi.mocked(getResearchStatus).mockResolvedValue({
      job_id: "job-no-filings",
      state: "failed",
      message: "The official disclosure platform does not currently provide a usable financial report for this company.",
      percent: 5,
      run_id: null,
      error_code: "NO_FILINGS_AVAILABLE",
      market: "CN_A",
      disclosure_url: "https://www.cninfo.com.cn/new/index",
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));

    expect(await screen.findByText(/does not currently provide a usable financial report/, {}, { timeout: 2000 })).toBeVisible();
    expect(screen.queryByText(/NoneType/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try fetching again" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Open official disclosure platform" }));
    expect(openExternalUrl).toHaveBeenCalledWith("https://www.cninfo.com.cn/new/index");
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
      ui_language_mode: "manual",
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
      retryable_synthesis: true,
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));

    expect(await screen.findByRole("heading", { name: "Synthetic Demo" }, { timeout: 2000 })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Completed report" })).toBeVisible();
    expect(screen.getByRole("article")).toHaveAttribute("data-report-status", "partial");
    expect(screen.getByText(/Partial report:/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Regenerate synthesized report" })).toHaveTextContent("Regenerate synthesized report");
    expect(screen.queryByLabelText("Elapsed research time")).not.toBeInTheDocument();
    expect(screen.queryByText(/OpenThesis is taking a few minutes/)).not.toBeInTheDocument();

    const reportDocument = screen.getByRole("article");
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(reportDocument).toHaveStyle({ "--report-scale": "1.1" });

    fireEvent.click(screen.getByRole("button", { name: "Show technical details" }));
    await waitFor(() => {
      expect(getResearchReport).toHaveBeenLastCalledWith("run-1", "zh-CN", true);
    });
    expect(screen.getByRole("button", { name: "Hide technical details" })).toHaveAttribute("aria-pressed", "true");

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

  it("shows only configured tested models and no credential fields in research", async () => {
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    const modelSelect = await screen.findByRole("combobox", { name: "Primary model" });
    expect(within(modelSelect).getByRole("option", { name: "Primary GPT · gpt-test" })).toBeInTheDocument();
    expect(within(modelSelect).getByRole("option", { name: "OpenRouter Free · openrouter/free · Free" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/API Key/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Endpoint")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Model provider")).not.toBeInTheDocument();
  });

  it("supports explicitly selecting multiple configured comparison models", async () => {
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.click(await screen.findByLabelText("Enable second-model comparison"));
    expect(screen.getByText("Comparison model")).toBeVisible();
    expect(screen.getByText("OpenRouter Free")).toBeVisible();
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
    await screen.findByRole("heading", { name: "Research report" });

    fireEvent.click(screen.getByRole("button", { name: "Research history" }));
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

    expect(screen.getByRole("heading", { name: "About OpenThesis", level: 1 })).toBeVisible();
    expect(screen.getByText("2.0.0")).toBeVisible();
    expect(screen.getByText("JSON-RPC 2")).toBeVisible();
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

    const file = new File(["pack"], "custom.ot", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText("Import .ot research pack"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(installResearchPack).toHaveBeenCalled());
    expect(await screen.findByRole("option", { name: "Custom Pack · 1" })).toBeVisible();
  });
});
