import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { captureMarketSnapshot, listConfiguredModels } from "../../backend";
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
    expect(request.model).toEqual({ configured_model_id: "primary-model", configuration_version: 4, role: "primary" });
    expect(request.vision_fallback.model).toEqual({ configured_model_id: "vision-model", configuration_version: 2, role: "vision" });
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
