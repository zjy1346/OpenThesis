import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  getStructureStatus: vi.fn(),
  startStructureInstall: vi.fn(),
  getResearchStatus: vi.fn(),
  cancelResearch: vi.fn(),
}));

vi.mock("../../backend", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../backend")>(),
  ...backendMock,
}));

import { cancelResearch, getResearchStatus, getStructureStatus, startStructureInstall } from "../../backend";
import { COPY } from "../../i18n";
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
  officialNetworkTitle: "Official data network",
  officialNetworkBody: "Optional official source routing.",
  officialNetworkProxy: "HTTP CONNECT proxy",
  officialNetworkProxyHint: "No credentials; for example http://127.0.0.1:7890.",
  officialNetworkProfiles: "Advanced official-host network profiles",
  officialNetworkProfilesHint: "Disabled when empty; only exact built-in official hosts and listed CIDRs are accepted.",
  officialNetworkTlsNote: "TLS verification remains enabled; these are not provider credentials.",
  officialNetworkProfilesInvalid: "Enter a JSON object mapping official hosts to CIDR arrays.",
  structureTitle: "Optional PP-StructureV3 local recognition",
  structureBody: "Pretrained inference runs locally without training or uploading documents. Python 3.12 is required; downloads may use several gigabytes.",
  structureAdvancedTitle: "Advanced experimental setup (manual install)",
  structureAdvancedBody: "Optional local inference only. Normal PDF recognition does not require this component.",
  structureEnabled: "Use local structure recognition for financial filings",
  structureEnableHint: "Install and verify the component before enabling it. Turning it off keeps it installed.",
  structurePythonPath: "Python 3.12 executable path",
  structureDownloadConsent: "I agree to download several gigabytes of local components and pretrained models.",
  structureInstallButton: "Install local structure runtime",
  structureCancelInstall: "Cancel installation",
  structureJobCancelled: "Installation cancelled. Normal local PDF recognition and existing cloud settings are unaffected.",
  structureStage: "Current stage",
  structureElapsed: "Elapsed",
  structurePhaseDownloadingModels: "Download and CPU-verify pretrained models",
  structureAvailable: "The component is installed and verified.",
  discardChanges: "Discard changes",
  unsavedChanges: "Unsaved changes",
};

describe("SettingsView language controls", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getStructureStatus).mockResolvedValue({ available: false, versions: {}, error_code: "STRUCTURE_COMPONENT_NOT_INSTALLED" });
  });

  it.each(["zh-CN", "zh-Hant", "en"] as const)("provides localized structure phases and errors for %s", (language) => {
    const localized = COPY[language];
    const phaseIds = [
      "checking-python",
      "installing-runtime",
      "downloading-dependencies",
      "downloading-models-and-verifying-cpu",
      "verifying-model-integrity",
    ];
    const phaseCopy = [
      localized.structurePhaseCheckingPython,
      localized.structurePhaseInstallingRuntime,
      localized.structurePhaseDownloadingDependencies,
      localized.structurePhaseDownloadingModels,
      localized.structurePhaseVerifyingIntegrity,
    ];
    expect(phaseCopy.every((value) => typeof value === "string" && value.length > 0)).toBe(true);
    for (const phase of phaseIds) expect(phaseCopy).not.toContain(phase);
    expect(localized.structureErrorPythonRequired).toBeTruthy();
    expect(localized.structureErrorRuntimeVersion).toBeTruthy();
    expect(localized.structureErrorModelMissing).toBeTruthy();
    expect(localized.structureErrorWorkerFailed).toBeTruthy();
    expect(localized.structureErrorWorkerTimeout).toBeTruthy();
    expect(localized.structureErrorModelIntegrity).toBeTruthy();
    expect(localized.structureErrorInstallFailed).toBeTruthy();
    expect(localized.structureErrorUnknown).toBeTruthy();
    expect(localized.structureAdvancedTitle).toBeTruthy();
    expect(localized.structureAdvancedBody).toBeTruthy();
  });

  it("requires download consent and an absolute Python path before optional installation", async () => {
    vi.mocked(startStructureInstall).mockResolvedValue({
      job_id: "structure-job-1", state: "queued", message: "Queued", percent: 0, run_id: null, stage: "queued",
    });
    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={vi.fn().mockResolvedValue({})}
      />,
    );

    fireEvent.click(screen.getByText(copy.structureAdvancedTitle));
    const install = await screen.findByRole("button", { name: "Install local structure runtime" });
    const pythonPath = screen.getByLabelText("Python 3.12 executable path");
    const consent = screen.getByLabelText(/I agree to download several gigabytes/);
    expect(install).toBeDisabled();

    fireEvent.change(pythonPath, { target: { value: "python.exe" } });
    fireEvent.click(consent);
    expect(install).toBeDisabled();
    fireEvent.change(pythonPath, { target: { value: "C:\\Python312\\python.exe" } });
    expect(install).toBeEnabled();
    fireEvent.click(install);

    await waitFor(() => expect(startStructureInstall).toHaveBeenCalledWith("C:\\Python312\\python.exe"));
  });

  it("polls install progress, localizes phase IDs, and does not enable the component after installation", async () => {
    vi.mocked(getStructureStatus)
      .mockResolvedValueOnce({ available: false, versions: {}, error_code: "STRUCTURE_COMPONENT_NOT_INSTALLED" })
      .mockResolvedValueOnce({ available: true, versions: { paddleocr: "3.7.0" }, error_code: "" });
    vi.mocked(startStructureInstall).mockResolvedValue({
      job_id: "structure-job-progress", state: "running", message: "downloading-models-and-verifying-cpu", percent: 0,
      run_id: null, stage: "structure-downloading-models-and-verifying-cpu", elapsed_seconds: 42,
    });
    vi.mocked(getResearchStatus).mockResolvedValue({
      job_id: "structure-job-progress", state: "completed", message: "completed", percent: 0, run_id: null,
      stage: "structure-verifying-model-integrity", elapsed_seconds: 43,
    });

    const onSave = vi.fn().mockResolvedValue({});
    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false", local_structure_enabled: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );

    fireEvent.click(screen.getByText(copy.structureAdvancedTitle));
    fireEvent.change(await screen.findByLabelText("Python 3.12 executable path"), { target: { value: "C:\\Python312\\python.exe" } });
    fireEvent.click(screen.getByLabelText(copy.structureDownloadConsent));
    fireEvent.click(screen.getByRole("button", { name: "Install local structure runtime" }));

    expect(await screen.findByText("Download and CPU-verify pretrained models")).toBeVisible();
    expect(screen.getByText("Elapsed: 42s")).toBeVisible();
    expect(screen.queryByText(/downloading-models-and-verifying-cpu|0%/)).not.toBeInTheDocument();
    await waitFor(() => expect(getResearchStatus).toHaveBeenCalledWith("structure-job-progress"), { timeout: 2_000 });
    await waitFor(() => expect(getStructureStatus).toHaveBeenCalledTimes(2));

    const enable = screen.getByLabelText("Use local structure recognition for financial filings");
    expect(enable).toBeEnabled();
    expect(enable).not.toBeChecked();
    fireEvent.click(enable);
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ local_structure_enabled: "true" })));
  });

  it("cancels a running install through the research job API", async () => {
    vi.mocked(startStructureInstall).mockResolvedValue({
      job_id: "structure-job-cancel", state: "running", message: "checking-python", percent: 0,
      run_id: null, stage: "structure-checking-python", elapsed_seconds: 1,
    });
    vi.mocked(cancelResearch).mockResolvedValue({
      job_id: "structure-job-cancel", state: "cancelled", message: "cancelled", percent: 0,
      run_id: null, stage: "structure-checking-python",
    });

    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={vi.fn().mockResolvedValue({})}
      />,
    );

    fireEvent.click(screen.getByText(copy.structureAdvancedTitle));
    fireEvent.change(await screen.findByLabelText("Python 3.12 executable path"), { target: { value: "C:\\Python312\\python.exe" } });
    fireEvent.click(screen.getByLabelText(copy.structureDownloadConsent));
    fireEvent.click(screen.getByRole("button", { name: "Install local structure runtime" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel installation" }));

    await waitFor(() => expect(cancelResearch).toHaveBeenCalledWith("structure-job-cancel"));
    expect(await screen.findByText(copy.structureJobCancelled)).toBeVisible();
  });

  it("includes the local component preference in dirty state, discard, and external preference updates", async () => {
    vi.mocked(getStructureStatus).mockResolvedValue({ available: true, versions: { paddleocr: "3.7.0" }, error_code: "" });
    const onSave = vi.fn().mockResolvedValue({});
    const initialPreferences = {
      ui_language: "en" as const,
      report_language: "en" as const,
      sidebar_collapsed: "true",
      parallel_agents: "false",
      local_structure_enabled: "true",
    };
    const { rerender } = render(
      <SettingsView language="en" preferences={initialPreferences} copy={copy} onSave={onSave} />,
    );

    const enable = await screen.findByLabelText("Use local structure recognition for financial filings");
    fireEvent.click(enable);
    expect(enable).not.toBeChecked();
    expect(screen.getByText("Unsaved changes")).toBeVisible();

    rerender(
      <SettingsView
        language="en"
        preferences={{ ...initialPreferences, report_language: "zh-CN" }}
        copy={copy}
        onSave={onSave}
      />,
    );
    expect(enable).not.toBeChecked();
    expect(screen.getByText("Unsaved changes")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(enable).toBeChecked();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();

    rerender(
      <SettingsView
        language="en"
        preferences={{ ...initialPreferences, report_language: "en", local_structure_enabled: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );
    await waitFor(() => expect(enable).not.toBeChecked());
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

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
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeVisible();
    const footer = container.querySelector(".settings-actions");
    const vision = container.querySelector("[data-testid='vision-fallback-settings']");
    expect(Boolean(footer && vision && (vision.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.getByLabelText("Enable vision fallback")).not.toBeChecked();
  });

  it("persists proxy and exact-host profile policy as one versioned preference", async () => {
    const onSave = vi.fn().mockResolvedValue({});
    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText("HTTP CONNECT proxy"), { target: { value: "http://127.0.0.1:7890" } });
    fireEvent.change(screen.getByLabelText("Host profiles (JSON)"), { target: { value: '{"www.sec.gov":["203.0.113.0/24"]}' } });
    expect(screen.getByText("TLS verification remains enabled; these are not provider credentials.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(Object.keys(onSave.mock.calls[0][0]).filter((key) => key === "official_network_policy")).toHaveLength(1);
    expect(JSON.parse(onSave.mock.calls[0][0].official_network_policy)).toEqual({
      version: "official-network-v1",
      connect_proxy: "http://127.0.0.1:7890",
      host_profiles: { "www.sec.gov": ["203.0.113.0/24"] },
    });
  });

  it("resets an existing network override to the strict default when cleared", async () => {
    const onSave = vi.fn().mockResolvedValue({});
    render(
      <SettingsView
        language="en"
        preferences={{
          ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false",
          official_network_policy: JSON.stringify({ version: "official-network-v1", connect_proxy: "http://127.0.0.1:7890", host_profiles: { "www.sec.gov": ["203.0.113.0/24"] } }),
        }}
        copy={copy}
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText("HTTP CONNECT proxy"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Host profiles (JSON)"), { target: { value: "{}" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(JSON.parse(onSave.mock.calls[0][0].official_network_policy)).toEqual({ version: "official-network-v1", connect_proxy: "", host_profiles: {} });
  });

  it("rejects malformed profile JSON without calling the settings backend", () => {
    const onSave = vi.fn().mockResolvedValue({});
    render(
      <SettingsView
        language="en"
        preferences={{ ui_language: "en", report_language: "en", sidebar_collapsed: "true", parallel_agents: "false" }}
        copy={copy}
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText("Host profiles (JSON)"), { target: { value: "{invalid" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("Enter a JSON object mapping official hosts to CIDR arrays.", { selector: "p[role='alert']" })).toBeVisible();
  });
});
