import { fireEvent, render, screen } from "@testing-library/react";
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
    expect(onSave).toHaveBeenCalledWith({ ui_language: "zh-Hant", ui_language_mode: "system", report_language: "zh-Hant" });
    Object.defineProperty(window.navigator, "languages", { configurable: true, value: originalLanguages });
  });
});
