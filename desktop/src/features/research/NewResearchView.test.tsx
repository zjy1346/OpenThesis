import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { captureMarketSnapshot, listConfiguredModels, searchCompanies } from "../../backend";
import { COPY } from "../../i18n";
import type { BootstrapResult } from "../../types";
import { NewResearchView } from "./NewResearchView";

vi.mock("../../backend", () => ({
  installResearchPack: vi.fn(),
  listConfiguredModels: vi.fn(),
  openExternalUrl: vi.fn(),
  searchCompanies: vi.fn(),
  captureMarketSnapshot: vi.fn(),
}));

const bootstrap: BootstrapResult = {
  contract_version: "2", app_version: "2.0.0", capabilities: [], interrupted_runs: 0,
  preferences: { ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false", research_market: "HK" },
  recent_runs: [], common_companies: [{ cik: "x", ticker: "700", name: "Tencent", exchange: "SEHK", market: "HK", security_id: "HK:700", listing_currency: "HKD", reporting_currency: "CNY" }],
  market_catalog: [{ market: "HK", label_zh: "", label_en: "Hong Kong", exchanges: ["SEHK"], default_currency: "HKD", requires_sec_identity: false, disclosure_home: "https://example.test" }],
  research_packs: [],
};

const configuredModels = [
  {
    configured_model_id: "primary-model",
    connection_id: "primary-connection",
    model_id: "reasoner",
    alias: "Primary",
    free_tier: false,
    billing_class: "paid" as const,
    enabled: true,
    health_status: "ready",
    configuration_version: 4,
    free_source_url: null,
    free_verified_at: null,
    last_discovered_at: null,
    context_window_hint: null,
    temperature: null,
    timeout_seconds: 180,
    capabilities: ["text_chat", "structured_json"],
  },
  {
    configured_model_id: "vision-model",
    connection_id: "vision-connection",
    model_id: "vision",
    alias: "Vision",
    free_tier: true,
    billing_class: "free_tier" as const,
    enabled: true,
    health_status: "ready",
    configuration_version: 2,
    free_source_url: null,
    free_verified_at: null,
    last_discovered_at: null,
    context_window_hint: null,
    temperature: null,
    timeout_seconds: 180,
    capabilities: ["text_chat", "vision"],
  },
];

describe("NewResearch configured models", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listConfiguredModels).mockResolvedValue(configuredModels);
    vi.mocked(captureMarketSnapshot).mockResolvedValue({ status: "UNAVAILABLE", error_code: "QUOTE_UNAVAILABLE" });
  });

  it("receives packs after minimal bootstrap without resetting the user's selection", async () => {
    const props = { bootstrap, copy: COPY.en, onOpenModelCenter: vi.fn(),
      onSavePreferences: vi.fn().mockResolvedValue(bootstrap.preferences), onStart: vi.fn() };
    const view = render(<NewResearchView {...props} />);
    const pack = { pack_id: "pack-one", name: "Full research", version: "1", content_hash: "a" };
    const other = { ...pack, pack_id: "pack-two", name: "Second research" };
    view.rerender(<NewResearchView {...props} bootstrap={{ ...bootstrap, research_packs: [pack, other] }} />);
    const selection = screen.getByRole("combobox", { name: COPY.en.researchPack });
    await waitFor(() => expect(selection).toHaveValue("pack-one"));
    fireEvent.change(selection, { target: { value: "pack-two" } });
    view.rerender(<NewResearchView {...props} bootstrap={{ ...bootstrap, research_packs: [pack, other] }} />);
    expect(selection).toHaveValue("pack-two");
  });

  it.each(["zh-CN", "zh-Hant", "en"] as const)("keeps the cache and token tradeoff visible and accessible in %s", async (locale) => {
    const copy = COPY[locale];
    render(<NewResearchView bootstrap={bootstrap} copy={copy} onOpenModelCenter={vi.fn()} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={vi.fn()} />);
    const toggle = await screen.findByRole("checkbox", { name: copy.parallelAgents });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(copy.parallelAgentsHint)).toBeVisible();
    expect(toggle).toHaveAccessibleDescription(copy.parallelAgentsHint);
    fireEvent.click(toggle);
    expect(toggle).toBeChecked();
    expect(toggle).toHaveAccessibleDescription(copy.parallelAgentsHint);
  });

  it("sends a request id and ignores a search result after the query changes", async () => {
    let resolveSearch!: (value: Awaited<ReturnType<typeof searchCompanies>>) => void;
    vi.mocked(searchCompanies).mockImplementation(() => new Promise((resolve) => { resolveSearch = resolve; }));
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={vi.fn()} />);

    const input = screen.getByRole("textbox", { name: COPY.en.companySearch });
    fireEvent.change(input, { target: { value: " Tencent " } });
    fireEvent.click(screen.getByRole("button", { name: COPY.en.searchAction }));
    await waitFor(() => expect(searchCompanies).toHaveBeenCalledTimes(1));
    const [query, market, requestId] = vi.mocked(searchCompanies).mock.calls[0];
    expect([query, market]).toEqual(["Tencent", "HK"]);
    expect(requestId).toEqual(expect.any(String));

    fireEvent.change(input, { target: { value: "Alibaba" } });
    await act(async () => resolveSearch({
      contract: "openthesis.company-search.v2", request_id: requestId, query: "Tencent", market: "HK", state: "ready",
      companies: [{ cik: "700", ticker: "0700", name: "Tencent Holdings", exchange: "SEHK", market: "HK", identity_state: "official_verified" }],
      diagnostics: [],
    }));
    expect(screen.queryByText("Tencent Holdings")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("times out once, ignores a late result, and allows a manual retry", async () => {
    vi.useFakeTimers();
    let resolveFirst!: (value: Awaited<ReturnType<typeof searchCompanies>>) => void;
    vi.mocked(searchCompanies)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(async (query, market, requestId) => ({
        contract: "openthesis.company-search.v2", request_id: requestId, query, market, state: "ready",
        companies: [{ cik: "700", ticker: "0700", name: "Fresh result", exchange: "SEHK", market: "HK", identity_state: "official_verified" }],
        diagnostics: [],
      }));
    try {
      render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={vi.fn()} />);
      fireEvent.change(screen.getByRole("textbox", { name: COPY.en.companySearch }), { target: { value: "Tencent" } });
      fireEvent.click(screen.getByRole("button", { name: COPY.en.searchAction }));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      expect(searchCompanies).toHaveBeenCalledTimes(1);

      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByRole("alert")).toHaveTextContent(COPY.en.searchFailed);
      const [query, market, requestId] = vi.mocked(searchCompanies).mock.calls[0];
      await act(async () => resolveFirst({
        contract: "openthesis.company-search.v2", request_id: requestId, query, market, state: "ready",
        companies: [{ cik: "700", ticker: "0700", name: "Late result", exchange: "SEHK", market: "HK", identity_state: "official_verified" }],
        diagnostics: [],
      }));
      expect(screen.queryByText("Late result")).not.toBeInTheDocument();
      expect(searchCompanies).toHaveBeenCalledTimes(1);

      fireEvent.click(screen.getByRole("button", { name: COPY.en.searchRetry }));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
      expect(screen.getByText("Fresh result")).toBeVisible();
      expect(searchCompanies).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("starts the deadline before saving preferences and lets the user retry without a late save starting search", async () => {
    vi.useFakeTimers();
    let resolveLateSave!: (value: typeof bootstrap.preferences) => void;
    const onSavePreferences = vi.fn()
      .mockImplementationOnce(() => new Promise<typeof bootstrap.preferences>((resolve) => { resolveLateSave = resolve; }))
      .mockResolvedValue(bootstrap.preferences);
    vi.mocked(searchCompanies).mockImplementation(async (query, market, requestId) => ({
      contract: "openthesis.company-search.v2",
      request_id: requestId,
      query,
      market,
      state: "ready",
      companies: [{ cik: "700", ticker: "0700", name: "Retry result", exchange: "SEHK", market: "HK", identity_state: "official_verified" }],
      diagnostics: [],
    }));
    try {
      render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={onSavePreferences} onStart={vi.fn()} />);
      fireEvent.change(screen.getByRole("textbox", { name: COPY.en.companySearch }), { target: { value: "Tencent" } });
      fireEvent.click(screen.getByRole("button", { name: COPY.en.searchAction }));
      expect(onSavePreferences).toHaveBeenCalledTimes(1);
      expect(searchCompanies).not.toHaveBeenCalled();

      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByRole("alert")).toHaveTextContent(COPY.en.searchFailed);
      expect(searchCompanies).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole("button", { name: COPY.en.searchRetry }));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
      expect(onSavePreferences).toHaveBeenCalledTimes(2);
      expect(searchCompanies).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Retry result")).toBeVisible();

      await act(async () => { resolveLateSave(bootstrap.preferences); await Promise.resolve(); });
      expect(searchCompanies).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Retry result")).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("invalidates a pending preference save and clears its deadline on unmount", async () => {
    vi.useFakeTimers();
    let resolveSave!: (value: typeof bootstrap.preferences) => void;
    const onSavePreferences = vi.fn(() => new Promise<typeof bootstrap.preferences>((resolve) => { resolveSave = resolve; }));
    try {
      const { unmount } = render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={onSavePreferences} onStart={vi.fn()} />);
      fireEvent.change(screen.getByRole("textbox", { name: COPY.en.companySearch }), { target: { value: "Tencent" } });
      fireEvent.click(screen.getByRole("button", { name: COPY.en.searchAction }));
      expect(vi.getTimerCount()).toBe(1);

      unmount();
      expect(vi.getTimerCount()).toBe(0);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
        resolveSave(bootstrap.preferences);
        await Promise.resolve();
      });
      expect(searchCompanies).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears the deadline and ignores a late search response after unmount", async () => {
    vi.useFakeTimers();
    let resolveSearch!: (value: Awaited<ReturnType<typeof searchCompanies>>) => void;
    vi.mocked(searchCompanies).mockImplementation(() => new Promise((resolve) => { resolveSearch = resolve; }));
    try {
      const { unmount } = render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={vi.fn()} />);
      fireEvent.change(screen.getByRole("textbox", { name: COPY.en.companySearch }), { target: { value: "Tencent" } });
      fireEvent.click(screen.getByRole("button", { name: COPY.en.searchAction }));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
      expect(searchCompanies).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(1);

      unmount();
      expect(vi.getTimerCount()).toBe(0);
      const [, market, requestId] = vi.mocked(searchCompanies).mock.calls[0];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
        resolveSearch({
          contract: "openthesis.company-search.v2", request_id: requestId, query: "Tencent", market, state: "ready",
          companies: [{ cik: "700", ticker: "0700", name: "Late after unmount", exchange: "SEHK", market: "HK", identity_state: "official_verified" }],
          diagnostics: [],
        });
        await Promise.resolve();
      });
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps unavailable identities unselectable and offers network settings", async () => {
    const requestId = "search-unavailable-1";
    vi.mocked(searchCompanies).mockResolvedValue({
      contract: "openthesis.company-search.v2", request_id: requestId, query: "ABC", market: "HK", state: "unavailable",
      companies: [{ cik: "", ticker: "ABC", name: "Unverified code", exchange: "", market: "HK", identity_state: "unresolved_code" }],
      diagnostics: [{ code: "SOURCE_NETWORK_UNAVAILABLE", diagnostic_id: "diag-123" }],
    });
    const onOpenNetworkSettings = vi.fn();
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onOpenVisionSettings={onOpenNetworkSettings} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={vi.fn()} />);

    fireEvent.change(screen.getByRole("textbox", { name: COPY.en.companySearch }), { target: { value: "ABC" } });
    fireEvent.click(screen.getByRole("button", { name: COPY.en.searchAction }));
    const unresolved = (await screen.findByText("ABC", { selector: "strong" })).closest("button");
    expect(unresolved).not.toBeNull();
    expect(unresolved).toBeDisabled();
    expect(screen.getByRole("button", { name: COPY.en.chooseCompany })).toBeDisabled();
    expect(screen.getByText(COPY.en.searchUnavailable)).toBeVisible();
    expect(screen.getByText("diag-123")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: COPY.en.openNetworkSettings }));
    expect(onOpenNetworkSettings).toHaveBeenCalledOnce();
  });

  it("allows a cached verified candidate in degraded mode and warns before selection", async () => {
    vi.mocked(searchCompanies).mockResolvedValue({
      contract: "openthesis.company-search.v2", request_id: "search-degraded-1", query: "Tencent", market: "HK", state: "degraded",
      companies: [{ cik: "700", ticker: "0700", name: "Tencent Holdings", exchange: "SEHK", market: "HK", identity_state: "cached_verified" }],
      diagnostics: [],
    });
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={onStart} />);

    fireEvent.change(screen.getByRole("textbox", { name: COPY.en.companySearch }), { target: { value: "Tencent" } });
    fireEvent.click(screen.getByRole("button", { name: COPY.en.searchAction }));
    const candidate = (await screen.findByText("0700", { selector: "strong" })).closest("button");
    expect(candidate).not.toBeNull();
    expect(candidate).toBeEnabled();
    expect(screen.getByText(COPY.en.searchDegraded)).toBeVisible();
    fireEvent.click(candidate!);
    fireEvent.click(screen.getByRole("button", { name: COPY.en.launchResearch }));
    await waitFor(() => expect(onStart).toHaveBeenCalledWith(expect.objectContaining({ company: expect.objectContaining({ identity_state: "cached_verified" }) })));
  });

  it("keeps launch failures in the form instead of leaving an unhandled rejection", async () => {
    const onStart = vi.fn().mockRejectedValue(new Error("SOURCE_NETWORK_UNAVAILABLE"));
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={onStart} />);
    await screen.findByRole("combobox", { name: "Primary model" });
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByRole("button", { name: COPY.en.launchResearch }));
    expect(await screen.findByRole("alert")).toHaveTextContent(COPY.en.searchErrorNetwork);
    expect(screen.getByRole("button", { name: COPY.en.launchResearch })).toBeEnabled();
  });

  it("localizes market snapshot status labels in all supported languages", () => {
    for (const language of ["zh-CN", "zh-Hant", "en"] as const) {
      expect(COPY[language].marketSnapshotLoading).toBeTruthy();
      expect(COPY[language].marketSnapshotVerified).toBeTruthy();
      expect(COPY[language].marketSnapshotUnavailable).toBeTruthy();
    }
    expect(COPY["zh-CN"].marketSnapshotLoading).not.toContain("Market snapshot");
    expect(COPY["zh-Hant"].marketSnapshotUnavailable).not.toContain("Market data");
  });

  it("uses the persisted vision policy as a per-run snapshot", async () => {
    const policyBootstrap = {
      ...bootstrap,
      preferences: {
        ...bootstrap.preferences,
        vision_fallback_policy: JSON.stringify({
          schema_version: 1, policy_version: 1, scope_version: 1,
          enabled: true, provider: "configured_model", approval_mode: "review_each_plan",
          standing_authorization: true, authorization_scope: "financial_failed_pages",
          provider_terms_version: 1,
          model: { configured_model_id: "vision-model", configuration_version: 2, role: "vision" },
        }),
      },
    };
    const onSavePreferences = vi.fn().mockResolvedValue(bootstrap.preferences);
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<NewResearchView bootstrap={policyBootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={onSavePreferences} onStart={onStart} />);

    await screen.findByRole("combobox", { name: "Primary model" });
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));

    await waitFor(() => expect(onStart).toHaveBeenCalled());
    const request = onStart.mock.calls[0][0];
    expect(request.model).toEqual({ configured_model_id: "primary-model", connection_id: "primary-connection", configuration_version: 4, role: "primary", context_window_hint: null });
    expect(request.vision_fallback.model).toEqual({ configured_model_id: "vision-model", connection_id: "vision-connection", configuration_version: 2, role: "vision" });
    expect(request.vision_fallback.provider).toBe("configured_model");
    expect(request.vision_fallback.approval_mode).toBe("review_each_plan");
    expect(JSON.stringify(request)).not.toMatch(/api_key|token|base_url|preset_id/i);
  });

  it("does not expose per-run vision controls when no persistent policy is enabled", async () => {
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<NewResearchView
      bootstrap={bootstrap}
      copy={COPY.en}
      onOpenModelCenter={vi.fn()}
      onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)}
      onStart={onStart}
    />);

    await screen.findByRole("combobox", { name: "Primary model" });
    expect(screen.queryByLabelText("Enable vision fallback")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Vision path" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));

    await waitFor(() => expect(onStart).toHaveBeenCalled());
    const request = onStart.mock.calls[0][0];
    expect(request.vision_fallback).toBeUndefined();
    expect(JSON.stringify(request)).not.toMatch(/api_key|token|base_url|preset_id/i);
  });

  it("requires renewed authorization when a persisted policy is stale", async () => {
    const staleBootstrap = {
      ...bootstrap,
      preferences: {
        ...bootstrap.preferences,
        vision_fallback_policy: JSON.stringify({
          schema_version: 1, policy_version: 0, scope_version: 1,
          enabled: true, provider: "mineru_flash", approval_mode: "review_each_plan",
          standing_authorization: true, authorization_scope: "financial_failed_pages", provider_terms_version: 1,
        }),
      },
    };
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<NewResearchView
      bootstrap={staleBootstrap}
      copy={COPY.en}
      onOpenModelCenter={vi.fn()}
      onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)}
      onStart={onStart}
    />);

    await screen.findByRole("combobox", { name: "Primary model" });
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));
    expect(onStart).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(COPY.en.visionMissing);
  });
  it("keeps editing available and links to Model Center when no tested model exists", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([]);
    const onOpenModelCenter = vi.fn();
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={onOpenModelCenter} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={vi.fn()} />);

    const buttons = await screen.findAllByRole("button", { name: "Model Center" });
    fireEvent.click(buttons[0]);
    expect(onOpenModelCenter).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText(/API Key/i)).not.toBeInTheDocument();
  });
});
