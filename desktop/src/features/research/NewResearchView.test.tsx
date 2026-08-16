import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { COPY } from "../../i18n";
import type { BootstrapResult } from "../../types";
import { NewResearchView } from "./NewResearchView";

const bootstrap: BootstrapResult = {
  contract_version: "1", app_version: "1", capabilities: [], interrupted_runs: 0,
  preferences: { ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false", research_market: "HK" },
  recent_runs: [], common_companies: [{ cik: "x", ticker: "700", name: "Tencent", exchange: "SEHK", market: "HK", security_id: "HK:700", listing_currency: "HKD", reporting_currency: "CNY" }],
  market_catalog: [{ market: "HK", label_zh: "", label_en: "Hong Kong", exchanges: ["SEHK"], default_currency: "HKD", requires_sec_identity: false, disclosure_home: "https://example.test" }],
  model_catalog: [{ preset_id: "none", label: "Offline", region: "local", protocol: "none", base_url: "", recommended_models: [], models_path: null, help_url: "", requires_api_key: false, temperature: null }],
  research_packs: [],
};

describe("NewResearch vision fallback", () => {
  it("sends approved MinerU Lite config without persisting vision secrets", async () => {
    const onSavePreferences = vi.fn().mockResolvedValue(bootstrap.preferences);
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onSavePreferences={onSavePreferences} onStart={onStart} />);
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByText("Cloud financial-report recognition fallback"));
    fireEvent.click(screen.getByLabelText("Enable cloud fallback"));
    fireEvent.click(screen.getByLabelText("I understand and consent to uploading failed pages"));
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));
    await waitFor(() => expect(onStart).toHaveBeenCalled());
    const request = onStart.mock.calls[0][0];
    expect(request.vision_fallback).toMatchObject({ enabled: true, consent: true, provider: "mineru_lite", require_page_approval: true, timeout_seconds: 60 });
    expect(onSavePreferences.mock.calls.flat().join(" ")).not.toMatch(/vision|token|api_key/i);
  });

  it("blocks Precision fallback without a session token", async () => {
    const onStart = vi.fn();
    render(<NewResearchView bootstrap={bootstrap} copy={COPY.en} onSavePreferences={vi.fn().mockResolvedValue(bootstrap.preferences)} onStart={onStart} />);
    fireEvent.click(screen.getByRole("button", { name: "700" }));
    fireEvent.click(screen.getByText("Cloud financial-report recognition fallback"));
    fireEvent.click(screen.getByLabelText("Enable cloud fallback"));
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "mineru_precision" } });
    fireEvent.click(screen.getByLabelText("I understand and consent to uploading failed pages"));
    fireEvent.click(screen.getByRole("button", { name: "Start research" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Complete upload consent and required credentials"));
    expect(onStart).not.toHaveBeenCalled();
  });
});
