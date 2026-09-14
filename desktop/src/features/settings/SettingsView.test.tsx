import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SettingsView } from "./SettingsView";

const copy = {
  settingsTitle: "Language settings",
  settingsBody: "Choose the interface and report language.",
  interfaceLanguage: "Interface language",
  reportLanguage: "Report language",
  chinese: "Simplified Chinese",
  english: "English",
  saveSettings: "Save settings",
  saving: "Saving…",
  saved: "Saved.",
  settingsFailed: "Could not save.",
  followSystem: "Follow system",
};

describe("SettingsView language controls", () => {
  it("uses one system/manual interface selector and keeps report language independent", () => {
    const originalLanguages = window.navigator.languages;
    Object.defineProperty(window.navigator, "languages", { configurable: true, value: ["zh-Hant-TW"] });
    const onSave = vi.fn().mockResolvedValue({ ui_language: "en", ui_language_mode: "system", report_language: "zh-Hant" });
    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", ui_language_mode: "system", report_language: "zh-CN", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );
    expect(screen.getAllByRole("combobox")).toHaveLength(2);
    expect(screen.getByRole("option", { name: "Follow system (Traditional Chinese)" })).toBeVisible();
    fireEvent.change(screen.getByLabelText("Report language"), { target: { value: "zh-Hant" } });
    fireEvent.change(screen.getByLabelText("Interface language"), { target: { value: "system" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ ui_language: "zh-Hant", ui_language_mode: "system", report_language: "zh-Hant" }));
    const saved = onSave.mock.calls[0][0].vision_fallback_policy;
    expect(JSON.parse(saved)).toMatchObject({ enabled: false, provider: "mineru_flash", standing_authorization: false, authorization_scope: "financial_failed_pages" });
    Object.defineProperty(window.navigator, "languages", { configurable: true, value: originalLanguages });
  });

  it("persists a standing authorization policy without exposing credentials", async () => {
    const onSave = vi.fn().mockResolvedValue({});
    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={{ ...copy, visionSettingsTitle: "Vision fallback", visionEnable: "Enable vision fallback", visionStandingAuthorization: "Authorize future runs", visionRevoke: "Revoke" }}
        onSave={onSave}
      />,
    );
    fireEvent.click(screen.getByLabelText("Enable vision fallback"));
    fireEvent.click(screen.getByLabelText("Authorize future runs"));
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const policy = JSON.parse(onSave.mock.calls[0][0].vision_fallback_policy);
    expect(policy).toMatchObject({ enabled: true, provider: "mineru_flash", standing_authorization: true, schema_version: 1 });
    expect(JSON.stringify(policy)).not.toMatch(/api[_-]?key|token|secret/i);
  });
});
