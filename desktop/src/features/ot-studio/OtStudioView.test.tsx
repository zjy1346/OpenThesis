import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  compileOtDraft,
  exportOtPackage,
  listConfiguredModels,
  suggestOtPatch,
  validateOtDraft,
} from "../../backend";
import type { ConfiguredModelSummary } from "../../types";
import { OtStudioView } from "./OtStudioView";

vi.mock("../../backend", () => ({
  compileOtDraft: vi.fn(),
  exportOtPackage: vi.fn(),
  listConfiguredModels: vi.fn(),
  suggestOtPatch: vi.fn(),
  validateOtDraft: vi.fn(),
}));

const readyModel: ConfiguredModelSummary = {
  configured_model_id: "local-helper", connection_id: "ollama-local", model_id: "qwen-small",
  alias: "Local helper", free_tier: true, billing_class: "local_no_provider_fee",
  free_source_url: null, free_verified_at: null, enabled: true,
  capabilities: ["text_chat", "structured_json"], health_status: "ready",
  last_discovered_at: null, context_window_hint: null, temperature: null,
  timeout_seconds: 180, configuration_version: 2,
};

describe("OtStudioView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listConfiguredModels).mockResolvedValue([]);
    vi.mocked(validateOtDraft).mockResolvedValue({
      valid: true,
      diagnostics: [{ code: "OT_VALID", severity: "info", path: "/", message: "Draft is valid." }],
    });
    vi.mocked(compileOtDraft).mockResolvedValue({
      valid: true,
      diagnostics: [{ code: "OT_VALID", severity: "info", path: "/", message: "Package compiled." }],
      filename: "custom-company-research-1.0.0.ot",
      data_base64: "UEsDBAoAAAAA",
      content_identity: "sha256:test-content-identity",
    });
    vi.mocked(exportOtPackage).mockResolvedValue(true);
  });

  it("keeps manual authoring available and completes validate-and-export", async () => {
    vi.mocked(validateOtDraft).mockResolvedValue({ valid: true, diagnostics: [] });
    const onOpenModelCenter = vi.fn();
    render(<OtStudioView language="en" onOpenModelCenter={onOpenModelCenter} />);

    expect(await screen.findByText("No tested assistant model is configured. Manual editing and export remain available.")).toBeVisible();
    fireEvent.change(screen.getByLabelText("What should this package investigate?"), {
      target: { value: "Investigate durable cash generation using traceable filings." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Use as description" }));
    fireEvent.change(screen.getByLabelText("Package name"), { target: { value: "Durable cash research" } });
    const horizon = screen.getByRole("slider", { name: "Horizon" });
    fireEvent.change(horizon, { target: { value: "8" } });
    expect(horizon).toHaveValue("8");

    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(validateOtDraft).toHaveBeenCalled());
    expect(await screen.findByText("Draft is valid")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Check and export" }));
    await waitFor(() => expect(exportOtPackage).toHaveBeenCalledWith("custom-company-research-1.0.0.ot", "UEsDBAoAAAAA"));
    expect(screen.getByText("sha256:test-content-identity")).toBeVisible();

    const compiledDraft = vi.mocked(compileOtDraft).mock.calls[0][0];
    expect(compiledDraft.package.name).toBe("Durable cash research");
    expect(compiledDraft.package.kind).toBe("openthesis.research-pack");
    expect(compiledDraft.package.description).toBe("Investigate durable cash generation using traceable filings.");
    expect(compiledDraft.settings.horizon_years).toBe(8);

    fireEvent.click(screen.getByRole("button", { name: "Open Model Center" }));
    expect(onOpenModelCenter).toHaveBeenCalledTimes(1);
  });

  it("explains all three parameters through keyboard-accessible tooltips", async () => {

    render(<OtStudioView language="en" onOpenModelCenter={vi.fn()} />);
    await screen.findByText("No tested assistant model is configured. Manual editing and export remain available.");

    const cases = [
      ["About: Horizon", "It does not decide how many years of filings are downloaded"],
      ["About: Analysis depth", "they never create missing evidence"],
      ["About: Risk emphasis", "It does not change source facts"],
    ];
    for (const [name, explanation] of cases) {
      const trigger = screen.getByRole("button", { name });
      trigger.focus();
      const tooltip = await screen.findByRole("tooltip");
      expect(tooltip).toHaveTextContent(explanation);
      expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
    }
  });

  it("renders tooltip in document body and keeps action controls as a stable pair", async () => {
    render(<OtStudioView language="en" onOpenModelCenter={vi.fn()} />);
    await screen.findByText("No tested assistant model is configured. Manual editing and export remain available.");
    const trigger = screen.getByRole("button", { name: "About: Horizon" });
    trigger.focus();
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.parentElement).toBe(document.body);
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
    const actions = document.querySelector(".studio-actions");
    expect(actions).not.toBeNull();
    const buttons = Array.from((actions as HTMLElement).querySelectorAll("button"));
    expect(buttons).toHaveLength(2);
    expect(buttons[0]).toHaveClass("secondary-button");
    expect(buttons[1]).toHaveClass("primary-button");
    expect(buttons.every((button) => button.textContent?.trim().length)).toBe(true);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
  it("shows only ready assistants and applies a bounded suggestion only after acceptance", async () => {
    vi.mocked(listConfiguredModels).mockResolvedValue([
      readyModel,
      { ...readyModel, configured_model_id: "untested", alias: "Untested", health_status: "unverified" },
    ]);
    vi.mocked(suggestOtPatch).mockResolvedValue({
      accepted: false,
      path: "/package/description",
      before: "Evidence-first company research with deterministic financial analysis.",
      after: "Analyze validated filings and cite evidence IDs; preserve missing values as missing.",
      diagnostics: [],
    });

    render(<OtStudioView language="en" onOpenModelCenter={vi.fn()} />);
    const modelPicker = await screen.findByLabelText("Model");
    expect(modelPicker).toHaveTextContent("Local helper · Free");
    expect(modelPicker).not.toHaveTextContent("Untested");

    fireEvent.change(screen.getByLabelText("Instruction"), { target: { value: "Clarify evidence and missing-value rules." } });
    fireEvent.click(screen.getByRole("button", { name: "Generate suggestion" }));
    expect(await screen.findByText("Analyze validated filings and cite evidence IDs; preserve missing values as missing.")).toBeVisible();
    expect(suggestOtPatch).toHaveBeenCalledWith(
      expect.any(Object),
      "/package/description",
      "Clarify evidence and missing-value rules.",
      { configured_model_id: "local-helper", configuration_version: 2, role: "ot_assistant" },
    );

    fireEvent.click(screen.getByRole("button", { name: "Accept change" }));
    fireEvent.click(screen.getByRole("button", { name: "Professional" }));
    expect((screen.getByLabelText("Raw draft JSON") as HTMLTextAreaElement).value).toContain("preserve missing values as missing");
  });

  it("renders the complete Studio interface in Traditional Chinese", async () => {
    vi.mocked(validateOtDraft).mockResolvedValue({ valid: true, diagnostics: [] });
    render(<OtStudioView language="zh-Hant" onOpenModelCenter={vi.fn()} />);

    expect(await screen.findByText("沒有已測試的輔助模型。你仍可手動編輯和匯出。")).toBeVisible();
    expect(screen.getByRole("heading", { name: "建立可重複使用的研究套件" })).toBeVisible();
    expect(screen.getByRole("button", { name: "引導模式" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "檢查草稿" }));
    expect(await screen.findByText("草稿有效")).toBeVisible();
  });
});
