import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  bootstrapBackend,
  captureMarketSnapshot,
  exportResearchDiagnostics,
  extendResearchBudget,
  installResearchPack,
  getModelDirectory,
  listConfiguredModels,
  listModelProviders,
  listProviderConnections,
  openExternalUrl,
  searchCompanies,
  getResearchReport,
  getResearchDiagnostics,
  getResearchStatus,
  cancelResearch,
  decideVisionUpload,
  listResearchTasks,
  listenForWindowClose,
  finishWindowClose,
  startResearch,
  updatePreferences,
} from "./backend";
import type { MarketSnapshotPreview, ResearchJob, ResearchTaskList, ResearchTaskSummary } from "./types";

vi.mock("./backend", () => ({
  bootstrapBackend: vi.fn(),
  captureMarketSnapshot: vi.fn(),
  deleteResearchRun: vi.fn(),
  getResearchReport: vi.fn(),
  getThesis: vi.fn(),
  listTheses: vi.fn(),
  saveThesis: vi.fn(),
  updatePreferences: vi.fn(),
  getResearchDiagnostics: vi.fn(),
  exportResearchDiagnostics: vi.fn(),
  installResearchPack: vi.fn(),
  getModelDirectory: vi.fn(),
  listConfiguredModels: vi.fn(),
  listModelProviders: vi.fn(),
  listProviderConnections: vi.fn(),
  openExternalUrl: vi.fn(),
  searchCompanies: vi.fn(),
  startResearch: vi.fn(),
  getResearchStatus: vi.fn(),
  listResearchTasks: vi.fn(),
  archiveResearchTask: vi.fn(),
  cancelModelCall: vi.fn(),
  continueResearchQueue: vi.fn(),
  decideVisionUpload: vi.fn(),
  resumeResearchTask: vi.fn(),
  listenForWindowClose: vi.fn(),
  finishWindowClose: vi.fn(),
  cancelResearch: vi.fn(),
  extendResearchBudget: vi.fn(),
  getResearchCallHistory: vi.fn(),
}));

const verifiedMarketSnapshot = {
  status: "VERIFIED",
  price: 225.12,
  market_cap: 3_400_000_000_000,
  quote_currency: "USD",
  as_of: "2026-10-31",
  provider: "test-provider",
} satisfies MarketSnapshotPreview;

const unavailableMarketSnapshot = {
  status: "UNAVAILABLE",
  error_code: "QUOTE_UNAVAILABLE",
} satisfies MarketSnapshotPreview;

function taskRow(id: string, state: ResearchTaskSummary["state"], revision = 1): ResearchTaskSummary {
  return { job_id: id, run_id: `run-${id}`, state, revision, company: { name: `Company ${id}`, ticker: id.toUpperCase() },
    operation_type: "research", model: "Local fixture", message: "", stage: "filing-parse", elapsed_seconds: 12,
    created_at: "2026-11-08T00:00:00Z" };
}

function taskPage(items: ResearchTaskSummary[], revision = 1, more?: number): ResearchTaskList {
  return { items, revision, counts: { running: 0, queued: 0, waiting_input: 0, interrupted: 0, cancelling: 0 },
    queue_paused: false, not_modified: false, has_more: more !== undefined, next_offset: more ?? null };
}

describe("report-first workbench", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllTimers();
    vi.clearAllMocks();
    vi.mocked(captureMarketSnapshot).mockReset();
    vi.mocked(captureMarketSnapshot).mockResolvedValue(unavailableMarketSnapshot);
    vi.mocked(listResearchTasks).mockResolvedValue({
      items: [], revision: 0, not_modified: false,
      counts: { running: 0, queued: 0, waiting_input: 0, interrupted: 0, cancelling: 0 },
      queue_paused: false, has_more: false, next_offset: null,
    });
    vi.mocked(listenForWindowClose).mockResolvedValue(() => {});
    vi.mocked(finishWindowClose).mockResolvedValue(undefined);
    vi.mocked(getModelDirectory).mockResolvedValue({ source: "https://models.dev/api.json", retrieved_at: "2026-10-22", license: "MIT", models: [] });
    vi.mocked(listModelProviders).mockResolvedValue([]);
    vi.mocked(listProviderConnections).mockResolvedValue([]);
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

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("opens the full research task center directly from the sidebar", async () => {
    render(<App />);
    fireEvent.click(await screen.findByTitle("Research tasks"));
    expect(screen.getByRole("heading", { level: 1, name: "Research tasks" })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "Research tasks" })).not.toBeInTheDocument();
  });

  it("keeps a draft across ordinary navigation and clears it only on explicit request", async () => {
    render(<App />);
    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.change(await screen.findByLabelText("Search ticker or company name"), { target: { value: "draft ticker" } });
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));
    expect(screen.getByLabelText("Search ticker or company name")).toHaveValue("draft ticker");
    fireEvent.click(screen.getByRole("button", { name: "Clear draft" }));
    expect(screen.getByLabelText("Search ticker or company name")).toHaveValue("");
    expect(startResearch).not.toHaveBeenCalled();
  });

  it("uses server-filtered pages and discards a late result from the previous filter", async () => {
    let resolveOld!: (page: ResearchTaskList) => void;
    vi.mocked(listResearchTasks).mockImplementation((options = {}) => {
      if (options.states?.includes("running")) return new Promise((resolve) => { resolveOld = resolve; });
      if (options.states?.includes("completed")) return Promise.resolve(options.offset
        ? taskPage([taskRow("older-finished", "completed")])
        : taskPage([taskRow("finished", "completed")], 1, 50));
      return Promise.resolve(taskPage([taskRow("head", "running")]));
    });
    render(<App />);
    fireEvent.click(await screen.findByTitle("Research tasks"));
    await waitFor(() => expect(resolveOld).toBeTypeOf("function"));
    fireEvent.click(within(screen.getByRole("group", { name: "Filter tasks" })).getByRole("button", { name: "Ended" }));
    expect(await screen.findByRole("button", { name: /^Company finished/ })).toBeVisible();
    await act(async () => resolveOld(taskPage([taskRow("late-active", "running")], 9)));
    expect(screen.queryByRole("button", { name: /^Company late-active/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByRole("button", { name: /^Company older-finished/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Company finished/ })).not.toBeInTheDocument();
    expect(listResearchTasks).toHaveBeenCalledWith({ limit: 50, offset: 50, states: ["completed", "failed", "cancelled"] });
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(await screen.findByRole("button", { name: /^Company finished/ })).toBeVisible();
    expect(startResearch).not.toHaveBeenCalled();
  });

  it("rejects a stale approval after switching jobs and refreshes a conflicting approval without repeating it", async () => {
    const a = taskRow("vision-a", "waiting_input", 3);
    const b = taskRow("vision-b", "waiting_input", 4);
    let resolveA!: (job: ResearchJob) => void;
    let rejectDecision!: (reason: Error) => void;
    let currentB = b;
    const visionJob = (row: ResearchTaskSummary): ResearchJob => ({
      ...row, percent: 28, vision_approval_pending: true,
      vision_upload_preview: { provider: "offline fixture", plan_id: `plan-${row.job_id}-${row.revision}`,
        pages: [row.revision], total_bytes: 2048, source_document: `${row.job_id}-${row.revision}.pdf` },
    });
    vi.mocked(listResearchTasks).mockImplementation(async (options) => options?.states && !options.states.includes("waiting_input")
      ? taskPage([]) : taskPage([a, currentB], currentB.revision));
    vi.mocked(getResearchStatus).mockImplementation((id) => id === a.job_id
      ? new Promise((resolve) => { resolveA = resolve; }) : Promise.resolve(visionJob(currentB)));
    vi.mocked(decideVisionUpload).mockImplementation(() => new Promise((_, reject) => { rejectDecision = reject; }));
    render(<App />);
    fireEvent.click(await screen.findByTitle("Research tasks"));
    fireEvent.click(within(screen.getByRole("group", { name: "Filter tasks" })).getByRole("button", { name: /Needs attention/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^Company vision-a/ }));
    await waitFor(() => expect(resolveA).toBeTypeOf("function"));
    fireEvent.click(screen.getByRole("button", { name: /^Company vision-b/ }));
    const approve = await screen.findByRole("button", { name: "Approve upload" });
    await act(async () => resolveA(visionJob(a)));
    expect(screen.queryByText(/vision-a-3\.pdf/)).not.toBeInTheDocument();
    expect(screen.getByText(/vision-b-4\.pdf/)).toBeVisible();
    fireEvent.click(approve);
    fireEvent.click(approve);
    expect(decideVisionUpload).toHaveBeenCalledExactlyOnceWith(b.job_id, true, 4, "plan-vision-b-4");
    expect(approve).toBeDisabled();
    currentB = { ...b, revision: 5 };
    await act(async () => rejectDecision(new Error("RESEARCH_TASK_REVISION_CONFLICT")));
    expect(await screen.findByText(/task changed\. Review/)).toBeVisible();
    expect(await screen.findByText(/vision-b-5\.pdf/)).toBeVisible();
    expect(decideVisionUpload).toHaveBeenCalledTimes(1);
  });

  it("shows a verified market snapshot when the backend returns a valid quote", async () => {
    vi.mocked(captureMarketSnapshot).mockResolvedValue(verifiedMarketSnapshot);
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    await screen.findByLabelText("Search ticker or company name");
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "AAPL" }));

    await act(async () => { await vi.advanceTimersByTimeAsync(250); });

    expect(captureMarketSnapshot).toHaveBeenCalledWith(expect.objectContaining({ ticker: "AAPL" }));
    expect(screen.getByTestId("market-snapshot-preview")).toHaveTextContent("Market snapshot verified · 2026-10-31 · USD");
  });

  it("shows unavailable market data without making it available for valuation", async () => {
    vi.mocked(captureMarketSnapshot).mockResolvedValue(unavailableMarketSnapshot);
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    await screen.findByLabelText("Search ticker or company name");
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "AAPL" }));

    await act(async () => { await vi.advanceTimersByTimeAsync(250); });

    expect(captureMarketSnapshot).toHaveBeenCalledWith(expect.objectContaining({ ticker: "AAPL" }));
    expect(screen.getByTestId("market-snapshot-preview")).toHaveTextContent("Market data unavailable; excluded from valuation");
    expect(screen.getByText("The quote source is unavailable. Retry later.")).toBeVisible();
  });

  it("cancels a pending market snapshot request when the research view unmounts", async () => {
    const view = render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    await screen.findByLabelText("Search ticker or company name");
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "AAPL" }));
    expect(captureMarketSnapshot).not.toHaveBeenCalled();

    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });

    expect(captureMarketSnapshot).not.toHaveBeenCalled();
  });

  it("offers research diagnostics for a running job as soon as a run id exists", async () => {
    const job = {
      job_id: "job-diagnostics", state: "running" as const, message: "Working", percent: 5, run_id: "run-diagnostics",
      revision: 1, operation_type: "research", company: { name: "Demo", ticker: "DEMO" }, model: "model:test",
      stage: "preparing", elapsed_seconds: 2, model_calls: [],
    };
    vi.mocked(startResearch).mockResolvedValue(job);
    vi.mocked(getResearchStatus).mockResolvedValue(job);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));
    fireEvent.click(await screen.findByTitle("Research tasks"));
    fireEvent.click(await screen.findByRole("tab", { name: "Diagnostics" }));
    expect(await screen.findByRole("button", { name: "Export research diagnostics (cost and time)" })).toBeVisible();
    expect(getResearchDiagnostics).not.toHaveBeenCalled();
  });

  it("keeps background work observable while navigating without showing global progress", async () => {
    const active = {
      job_id: "navigation-task", run_id: "navigation-run", state: "running" as const,
      message: "Working", percent: 31, revision: 2, operation_type: "research",
      company: { name: "Background Company", ticker: "BG" }, model: "frozen-model",
      stage: "filing-parse", elapsed_seconds: 8, model_calls: [],
    };
    vi.mocked(startResearch).mockResolvedValue(active);
    vi.mocked(getResearchStatus).mockResolvedValue(active);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));
    expect(await screen.findByText("Research submitted")).toBeVisible();
    for (const name of ["Research history", "Settings", "Model Center", "Research workspace"]) {
      fireEvent.click(screen.getByRole("button", { name }));
      expect(screen.queryByRole("button", { name: "Cancel task" })).not.toBeInTheDocument();
      expect(screen.getByTitle("Research tasks")).toBeVisible();
    }
    fireEvent.click(screen.getByTitle("Research tasks"));
    expect(await screen.findByRole("heading", { name: "Background Company" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Cancel task" })).toBeVisible();
    expect(startResearch).toHaveBeenCalledTimes(1);
    expect(cancelResearch).not.toHaveBeenCalled();
  });

  it("shows concrete current-run budget gaps without approving them automatically", async () => {
    const job = {
      job_id: "job-budget", state: "waiting_input" as const, message: "Waiting", percent: 35, run_id: "run-budget",
      revision: 1, operation_type: "research", company: { name: "Demo", ticker: "DEMO" }, model: "model:test",
      stage: "growth-opportunities", elapsed_seconds: 12, blocked_reason: "MODEL_RUN_PLAN_REQUIRES_APPROVAL",
      call_plan: { plan_digest: "digest-1", max_calls: 2, max_input_estimate: 8_000, max_output_tokens: 20_000,
        pending_budget_gaps: [{ gap_id: "gap-growth", stage_id: "growth-opportunities", purpose: "generation", input_estimate: 1_234,
          output_limit: 4_096, diagnostic_code: "MODEL_RUN_CALL_BUDGET_EXHAUSTED" }] },
    };
    vi.mocked(startResearch).mockResolvedValue(job);
    vi.mocked(getResearchStatus).mockResolvedValue(job);
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));
    fireEvent.click(await screen.findByTitle("Research tasks"));

    expect(await screen.findByText("Growth opportunities · Generation")).toBeInTheDocument();
    expect(screen.getByText("1,234 input tokens (estimate) · 4,096 output tokens (limit)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve additional budget and continue gaps" })).toBeEnabled();
    expect(extendResearchBudget).not.toHaveBeenCalled();
  });

  it("keeps the report workspace primary when no history exists", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Research report" })).toBeVisible();
    expect(screen.getByText("No research reports yet")).toBeVisible();
    expect(screen.getByRole("button", { name: "Open navigation" })).toBeVisible();
  });

  it("gives Model Center its own report-stage scroll container", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Open navigation" });
    fireEvent.click(screen.getAllByRole("button", { name: "Model Center" })[0]);

    expect(await screen.findByRole("complementary", { name: "Connections" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Research report" })).toHaveClass("report-stage", "model-center-stage");
  });

  it("shows a local database recovery warning instead of research actions on startup corruption", async () => {
    vi.mocked(bootstrapBackend).mockRejectedValueOnce(new Error("STORAGE_HEALTH_RECOVERY_REQUIRED"));
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Database integrity could not be confirmed/);
    expect(screen.getByRole("button", { name: "Open navigation" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Run synthetic demo research" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Configure real company research" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Start research" }));
    expect(screen.getByRole("heading", { name: "Local database recovery check required" })).toBeVisible();
    expect(within(screen.getByRole("region", { name: "Research report" })).queryByRole("button", { name: "Start research" })).not.toBeInTheDocument();
  });

  it("keeps an already displayed report while suppressing paid recovery actions on storage failure", async () => {
    const run = {
      run_id: "run-storage", ticker: "DEMO", company_name: "Stored Company", status: "completed",
      started_at: "2026-10-20T00:00:00Z", completed_at: "2026-10-20T00:01:00Z", report_language: "en" as const,
      exchange: "NYSE", market: "US" as const,
    };
    vi.mocked(bootstrapBackend).mockResolvedValue({
      contract_version: "2", app_version: "2.0.0", capabilities: [],
      preferences: { ui_language: "en", report_language: "en", ui_language_mode: "manual", sidebar_collapsed: "true", parallel_agents: "false" },
      recent_runs: [run], common_companies: [], research_packs: [], interrupted_runs: 0,
    });
    vi.mocked(getResearchReport)
      .mockResolvedValueOnce({
        run_id: run.run_id, ticker: run.ticker, company_name: run.company_name, status: "partial", report_language: "en",
        markdown: "# Stored report body", html: "<h1>Stored report body</h1>", retryable_synthesis: true,
        retryable_model_stages: true,
        call_plan: { plan_digest: "digest", max_calls: 2, max_input_estimate: 8_000, max_output_tokens: 16_000,
          pending_budget_gaps: [{ gap_id: "gap-1", stage_id: "research-synthesizer", purpose: "generation", input_estimate: 100,
            output_limit: 1_000, diagnostic_code: "MODEL_RUN_CALL_BUDGET_EXHAUSTED" }] },
      })
      .mockRejectedValueOnce(new Error("STORAGE_HEALTH_RECOVERY_REQUIRED"));
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Stored report body" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Research history" }));
    fireEvent.click(screen.getAllByRole("button", { name: /Stored Company/ })[0]);

    expect(await screen.findByText(/Close OpenThesis and copy the local data directory/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "Stored report body" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Open navigation" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Regenerate synthesized report" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue model research" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve additional budget and continue gaps" })).not.toBeInTheDocument();
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
    vi.mocked(searchCompanies).mockImplementation(async (query, market, requestId) => ({
      contract: "openthesis.company-search.v2",
      request_id: requestId,
      query,
      market,
      state: "ready",
      companies: [
        { cik: "CN_A:BSE:832982.BJ", ticker: "832982.BJ", name: "Jinbo Bio", exchange: "BSE", market: "CN_A", listing_currency: "CNY", identity_state: "official_verified" },
      ],
      diagnostics: [],
    }));
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

    await waitFor(() => expect(searchCompanies).toHaveBeenCalledWith("832982", "CN_A", expect.any(String)));
    expect(await screen.findByText("Jinbo Bio")).toBeVisible();
    expect(screen.getByLabelText("Listing market").closest(".market-source-row")).not.toBeNull();
  });

  it("shows no-filing recovery guidance on a failed task and opens only its official disclosure link", async () => {
    const job = {
      job_id: "job-no-filings", state: "failed" as const,
      message: "The official disclosure platform does not currently provide a usable financial report for this company.",
      percent: 5, run_id: null, revision: 2, operation_type: "research",
      company: { name: "Demo Company", ticker: "DEMO" }, model: "model:test", stage: "filing-discovery",
      error_code: "NO_FILINGS_AVAILABLE", market: "CN_A" as const,
      disclosure_url: "https://www.cninfo.com.cn/new/index",
    };
    vi.mocked(startResearch).mockResolvedValue(job);
    vi.mocked(getResearchStatus).mockResolvedValue(job);
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));
    expect(await screen.findByText("Demo Company · Failed")).toBeVisible();
    expect(getResearchReport).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByTitle("Research tasks"));
    expect(await screen.findByText(/no usable official financial reports/i)).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Diagnostics" }));
    fireEvent.click(screen.getByText("Diagnostic code", { selector: "summary" }));
    expect(screen.getByText("NO_FILINGS_AVAILABLE")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Continue task" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open official disclosure page" }));
    expect(openExternalUrl).toHaveBeenCalledWith("https://www.cninfo.com.cn/new/index");
  });

  it("does not treat a completed task attempt with incomplete research as a complete report", async () => {
    const incomplete = {
      job_id: "job-incomplete", state: "completed" as const, message: "Financial data needs review before research can be considered complete.",
      percent: 100, run_id: "run-incomplete", revision: 2, operation_type: "financial-rebuild",
      company: { name: "New Company", ticker: "NEW" }, model: "model:test", stage: "filing-validation",
      attempt_complete: true, research_complete: false, result_access: "saved" as const, action_required: true, error_code: "FILING_DATA_QUALITY_FAILED",
      recovery_outcome: { state: "needs_action", diagnostic_code: "FINANCIAL_DATA_QUALITY_FAILED", next_actions: ["open_report_recovery"] },
    };
    vi.mocked(startResearch).mockResolvedValue({ ...incomplete, state: "queued", revision: 1, message: "Accepted" });
    vi.mocked(getResearchStatus).mockResolvedValue(incomplete);
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Run synthetic demo research" }));

    expect(await screen.findByText("Research submitted")).toBeVisible();
    fireEvent.click(await screen.findByTitle("Research tasks"));
    expect(await within(screen.getByRole("region", { name: "Task details" })).findByRole("button", { name: "View saved research" })).toBeVisible();
    expect(await screen.findByText(/financial data does not yet meet the research requirements/i)).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Diagnostics" }));
    fireEvent.click(screen.getByText("Diagnostic code", { selector: "summary" }));
    expect(screen.getByText("FINANCIAL_DATA_QUALITY_FAILED")).toBeVisible();
    expect(getResearchReport).not.toHaveBeenCalled();
    expect(screen.queryByText(/research completed successfully/i)).not.toBeInTheDocument();
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
      target: { value: "zh-Hant" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(updatePreferences).toHaveBeenCalledWith(expect.objectContaining({
      ui_language: "en",
      ui_language_mode: "manual",
      report_language: "zh-Hant",
    }));
  });

  it("keeps the selected report readable until the user opens a completion notification", async () => {
    const existingRun = {
      run_id: "run-existing", ticker: "OLD", company_name: "Previously Selected Company", status: "completed",
      started_at: "2026-10-20T00:00:00Z", completed_at: "2026-10-20T00:01:00Z", report_language: "en" as const,
      exchange: "NYSE", market: "US" as const,
    };
    vi.mocked(bootstrapBackend).mockResolvedValue({
      contract_version: "2", app_version: "3.3.0", capabilities: [], interrupted_runs: 0,
      preferences: { ui_language: "en", report_language: "en", ui_language_mode: "manual", sidebar_collapsed: "true", parallel_agents: "false" },
      recent_runs: [existingRun], common_companies: [], research_packs: [],
    });
    const existingReport = {
      run_id: existingRun.run_id, ticker: existingRun.ticker, company_name: existingRun.company_name,
      status: "partial" as const, report_language: "en" as const, markdown: "# Previously Selected Report\n\nVerified existing evidence.", html: "<h1>Previously Selected Report</h1><p>Verified existing evidence.</p>",
      report_contract_version: "1", report_input_generation: "gen-existing", is_substantive: true,
      visible_sections: [{ section_id: "executive_summary", title: "Executive summary", source_artifact_ids: ["artifact-existing"], verification_state: "partial", is_substantive: true }],
      report_read_state: "partial" as const,
    };
    const completedReport = {
      run_id: "run-new", ticker: "NEW", company_name: "New Company",
      status: "partial" as const, report_language: "en" as const, markdown: "# New Verified Report\n\nVerified new evidence.", html: "<h1>New Verified Report</h1><p>Verified new evidence.</p>",
      report_contract_version: "1", report_input_generation: "gen-new", is_substantive: true,
      visible_sections: [{ section_id: "executive_summary", title: "Executive summary", source_artifact_ids: ["artifact-new"], verification_state: "partial", is_substantive: true }],
      report_read_state: "partial" as const,
    };
    vi.mocked(getResearchReport).mockImplementation(async (runId) => runId === "run-existing" ? existingReport : completedReport);
    const accepted = {
      job_id: "job-1",
      state: "queued" as const,
      message: "Accepted",
      percent: 0,
      run_id: "run-new",
      revision: 1,
      ahead_count: 2,
      operation_type: "research",
      company: { name: "New Company", ticker: "NEW" },
      model: "model:test",
      stage: "preparing",
      elapsed_seconds: 0,
      created_at: "2026-10-28T12:00:00Z",
    };
    const completed = { ...accepted, state: "completed" as const, message: "Research attempt finished.", revision: 2, percent: 100, stage: "synthesis", attempt_complete: true, research_complete: false, result_access: "saved" as const, action_required: true };
    let submitted = false;
    vi.mocked(startResearch).mockImplementation(async () => { submitted = true; return accepted; });
    vi.mocked(listResearchTasks).mockImplementation(async (options) =>
      submitted && (!options?.states || options.states.includes("completed")) ? taskPage([completed], 2) : taskPage([]));
    vi.mocked(getResearchStatus).mockResolvedValue(completed);
    vi.mocked(searchCompanies).mockImplementation(async (query, market, requestId) => ({
      contract: "openthesis.company-search.v2", request_id: requestId, query, market, state: "ready",
      companies: [{ cik: "CN_A:NEW", ticker: "NEW", name: "New Company", exchange: "SSE", market: "CN_A", identity_state: "official_verified" }],
      diagnostics: [],
    }));
    vi.mocked(updatePreferences).mockImplementation(async (value) => ({
      ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false", research_market: "CN_A",
      ...Object.fromEntries(Object.entries(value).map(([key, item]) => [key, String(item)])),
    }));
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Previously Selected Report" })).toBeVisible();
    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.change(screen.getByLabelText("Listing market"), { target: { value: "CN_A" } });
    fireEvent.change(screen.getByLabelText("Search ticker or company name"), { target: { value: "NEW" } });
    fireEvent.click(screen.getByRole("button", { name: "Search companies" }));
    const candidate = (await screen.findByText("New Company")).closest("button");
    expect(candidate).not.toBeNull();
    fireEvent.click(candidate!);
    fireEvent.click(screen.getAllByRole("button", { name: "Start research" }).at(-1)!);

    expect(await screen.findByText("Added to queue · 2 ahead")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Research workspace" }));
    expect(screen.getByRole("heading", { name: "Previously Selected Report" })).toBeVisible();
    expect(getResearchReport).toHaveBeenCalledTimes(1);
    expect(getResearchReport).toHaveBeenCalledWith("run-existing", "en");

    const stage = screen.getByRole("heading", { name: "Previously Selected Report" }).closest<HTMLElement>(".report-stage")!;
    stage.scrollTop = 290;
    fireEvent.scroll(stage);
    const quick = screen.getByRole("button", { name: /^Task summary/ });
    fireEvent.click(quick);
    expect(screen.getByRole("dialog", { name: "Task summary" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Overview" })).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(quick).toHaveFocus();
    expect(stage.scrollTop).toBe(290);
    fireEvent.click(await screen.findByTitle("Research tasks"));
    fireEvent.click(screen.getByRole("button", { name: "Return to report" }));
    expect(stage.scrollTop).toBe(290);
    expect(await screen.findByText("New Company · Partial result")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Previously Selected Report" })).toBeVisible();
    expect(getResearchReport).toHaveBeenCalledTimes(1);
    const notification = screen.getByRole("region", { name: "Task notifications" });
    fireEvent.click(within(notification).getByRole("button", { name: "View saved research" }));

    expect(await screen.findByRole("heading", { name: "New Verified Report" })).toBeVisible();
    expect(screen.getByText("Verified new evidence.")).toBeVisible();
    expect(getResearchReport).toHaveBeenLastCalledWith("run-new", "en");
    expect(cancelResearch).not.toHaveBeenCalled();
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
    const initialBootstrapCalls = vi.mocked(bootstrapBackend).mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Research history" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh research history" }));

    await waitFor(() => expect(bootstrapBackend).toHaveBeenCalledTimes(initialBootstrapCalls + 1));
  });

  it("reuses the same idempotency key when a company-task acknowledgement is retried", async () => {
    vi.mocked(startResearch)
      .mockRejectedValueOnce(new Error("temporary provider failure"))
      .mockResolvedValueOnce({
        job_id: "job-retry", state: "queued", message: "Accepted", percent: 0, run_id: "run-retry",
        revision: 1, operation_type: "research", company: { name: "Retry Company", ticker: "RETRY" },
        model: "primary-model", stage: "preparing", elapsed_seconds: 0, ahead_count: 0,
      });
    vi.mocked(searchCompanies).mockImplementation(async (query, market, requestId) => ({
      contract: "openthesis.company-search.v2", request_id: requestId, query, market, state: "ready",
      companies: [{ cik: "CN_A:RETRY", ticker: "RETRY", name: "Retry Company", exchange: "SSE", market: "CN_A", identity_state: "official_verified" }],
      diagnostics: [],
    }));
    vi.mocked(updatePreferences).mockImplementation(async (value) => ({
      ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false", research_market: "CN_A",
      ...Object.fromEntries(Object.entries(value).map(([key, item]) => [key, String(item)])),
    }));
    render(<App />);

    fireEvent.click((await screen.findAllByRole("button", { name: "Start research" }))[0]);
    fireEvent.change(screen.getByLabelText("Listing market"), { target: { value: "CN_A" } });
    fireEvent.change(screen.getByLabelText("Search ticker or company name"), { target: { value: "RETRY" } });
    fireEvent.click(screen.getByRole("button", { name: "Search companies" }));
    fireEvent.click((await screen.findByText("Retry Company")).closest("button")!);
    const launchButton = () => screen.getAllByRole("button", { name: "Start research" }).at(-1)!;
    fireEvent.click(launchButton());
    await waitFor(() => expect(startResearch).toHaveBeenCalledTimes(1));
    expect(launchButton()).toBeEnabled();
    fireEvent.click(launchButton());

    await waitFor(() => expect(startResearch).toHaveBeenCalledTimes(2));
    const firstRequest = vi.mocked(startResearch).mock.calls[0]?.[0];
    const secondRequest = vi.mocked(startResearch).mock.calls[1]?.[0];
    if (!firstRequest || !secondRequest) throw new Error("Both idempotent submissions must be recorded");
    expect(firstRequest).toMatchObject({ mode: "company", company: { ticker: "RETRY" }, client_request_id: expect.any(String) });
    expect(secondRequest).toMatchObject({ mode: "company", company: { ticker: "RETRY" }, client_request_id: firstRequest.client_request_id });
    expect(JSON.stringify([firstRequest, secondRequest])).not.toMatch(/api_key|base_url|preset_id/i);
    expect(await screen.findByText("Research submitted")).toBeVisible();
  });

  it("shows architecture diagnostics in the About view", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "About OpenThesis" }));

    expect(screen.getByRole("heading", { name: "About OpenThesis", level: 1 })).toBeVisible();
    expect(screen.getByText("2.0.0")).toBeVisible();
    expect(screen.getByText("JSON-RPC 2")).toBeVisible();
  });

  it("keeps the close prompt's return and minimize actions distinct", async () => {
    let requestClose!: () => void;
    vi.mocked(listenForWindowClose).mockImplementation(async (onClose) => {
      requestClose = onClose;
      return () => {};
    });
    const active = taskPage([taskRow("active", "running")]);
    vi.mocked(listResearchTasks).mockResolvedValue({ ...active, counts: { ...active.counts, running: 1 } });
    render(<App />);
    await screen.findByTitle("Research tasks");
    await act(async () => requestClose());
    const close = () => screen.getByRole("dialog", { name: "Keep research running?" });
    fireEvent.click(within(close()).getByRole("button", { name: "Back to app" }));
    expect(screen.queryByRole("dialog", { name: "Keep research running?" })).not.toBeInTheDocument();
    expect(finishWindowClose).not.toHaveBeenCalled();

    await act(async () => requestClose());
    fireEvent.click(within(close()).getByRole("button", { name: "Minimize and continue" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keep research running?" })).not.toBeInTheDocument());
    expect(finishWindowClose).toHaveBeenCalledExactlyOnceWith("minimize");
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
