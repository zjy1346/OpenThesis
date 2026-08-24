import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { listConfiguredModels } from "../../backend";
import { COPY } from "../../i18n";
import type { BootstrapResult } from "../../types";
import { NewResearchView } from "./NewResearchView";

vi.mock("../../backend", () => ({
  installResearchPack: vi.fn(),
  listConfiguredModels: vi.fn(),
  openExternalUrl: vi.fn(),
  searchCompanies: vi.fn(),
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
  });

  it("sends only configured-model references for research and vision", async () => {
    const onSavePreferences = vi.fn().mockResolvedValue(bootstrap.preferences);
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onOpenModelCenter={vi.fn()} onSavePreferences={onSavePreferences} onStart={onStart} />);

    await screen.findByRole("combobox", { name: "Primary model" });
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByText("Financial-page vision parsing"));
    fireEvent.click(screen.getByLabelText("Enable vision fallback"));
    fireEvent.change(screen.getByLabelText("Vision path"), { target: { value: "configured_model" } });
    fireEvent.click(screen.getByLabelText("I understand and consent to sending locally failed pages to the selected third-party vision service after page-by-page preview"));
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));

    await waitFor(() => expect(onStart).toHaveBeenCalled());
    const request = onStart.mock.calls[0][0];
    expect(request.model).toEqual({ configured_model_id: "primary-model", configuration_version: 4, role: "primary" });
    expect(request.vision_fallback.model).toEqual({ configured_model_id: "vision-model", configuration_version: 2, role: "vision" });
    expect(request.vision_fallback.provider).toBe("configured_model");
    expect(JSON.stringify(request)).not.toMatch(/api_key|token|base_url|preset_id/i);
  });


  it("offers the no-token MinerU Flash path without a configured vision model", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([configuredModels[0]]);
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<NewResearchView
      bootstrap={bootstrap}
      copy={COPY.en}
      onOpenModelCenter={vi.fn()}
      onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)}
      onStart={onStart}
    />);

    await screen.findByRole("combobox", { name: "Primary model" });
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByText("Financial-page vision parsing"));
    fireEvent.click(screen.getByLabelText("Enable vision fallback"));
    expect(screen.getByRole("option", { name: "MinerU Flash · Free · No registration" })).toBeVisible();
    expect(screen.queryByLabelText(/API Key/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("I understand and consent to sending locally failed pages to the selected third-party vision service after page-by-page preview"));
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));

    await waitFor(() => expect(onStart).toHaveBeenCalled());
    const request = onStart.mock.calls[0][0];
    expect(request.vision_fallback).toMatchObject({ enabled: true, consent: true, provider: "mineru_flash", require_page_approval: true });
    expect(request.vision_fallback.model).toBeUndefined();
    expect(JSON.stringify(request)).not.toMatch(/api_key|token|base_url|preset_id/i);
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
