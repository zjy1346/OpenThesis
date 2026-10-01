import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SettingsView } from "./SettingsView";

const copy = {
  settingsTitle: "Language settings",
  settingsBody: "Choose the interface and report language.",
  languageSettingsTitle: "Interface and report language",
  languageSettingsBody: "Choose the language for the app interface and research reports separately.",
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
  it("gives the language section its own precise title and explanation", () => {
    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={vi.fn().mockResolvedValue({})}
      />,
    );

    expect(screen.getByRole("heading", { name: "Language settings" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Interface and report language" })).toBeVisible();
    expect(screen.getByText("Choose the language for the app interface and research reports separately.")).toBeVisible();
  });

  it("hydrates async preference updates while clean without creating a false dirty state", () => {
    const onSave = vi.fn().mockResolvedValue({});
    const { rerender } = render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );

    rerender(
      <SettingsView
        language="en"
        preferences={{ ui_language: "zh-CN", report_language: "zh-Hant", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );

    expect(screen.getByLabelText("Report language")).toHaveValue("zh-Hant");
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  });

  it("does not overwrite edits when preferences arrive while the form is dirty", () => {
    const onSave = vi.fn().mockResolvedValue({});
    const { rerender } = render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText("Report language"), { target: { value: "zh-Hant" } });
    rerender(
      <SettingsView
        language="en"
        preferences={{ ui_language: "zh-CN", report_language: "zh-CN", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );

    expect(screen.getByLabelText("Report language")).toHaveValue("zh-Hant");
    expect(screen.getByText("Unsaved changes")).toBeVisible();
  });

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

  it("keeps one save action after every editable section and can discard dirty changes", () => {
    const onSave = vi.fn().mockResolvedValue({});
    const { container } = render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={{
          ...copy,
          visionSettingsTitle: "Vision fallback",
          visionEnable: "Enable vision fallback",
          discardChanges: "Discard changes",
          unsavedChanges: "Unsaved changes",
        }}
        onSave={onSave}
      />,
    );

    expect(screen.getAllByRole("button", { name: "Save settings" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Discard changes" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Enable vision fallback"));
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeVisible();
    const footer = container.querySelector(".settings-actions");
    const vision = container.querySelector("[data-testid='vision-fallback-settings']");
    expect(Boolean(footer && vision && (vision.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.getByLabelText("Enable vision fallback")).not.toBeChecked();
  });
});
