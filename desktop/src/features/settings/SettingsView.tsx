import { useEffect, useMemo, useState } from "react";

import { listConfiguredModels } from "../../backend";
import type { ConfiguredModelSummary, Language, Preferences, VisionFallbackPolicy } from "../../types";
import {
  isCurrentVisionFallbackPolicy,
  parseVisionFallbackPolicy,
  VISION_FALLBACK_POLICY_VERSION,
  VISION_FALLBACK_PROVIDER_TERMS_VERSION,
  VISION_FALLBACK_SCOPE_VERSION,
} from "../../types";
import { languageName, languageOptions, resolveSystemLanguage } from "../../languageRegistry";

type SettingsCopy = {
  settingsTitle: string;
  settingsBody: string;
  interfaceLanguage: string;
  reportLanguage: string;
  chinese: string;
  english: string;
  saveSettings: string;
  saving: string;
  saved: string;
  settingsFailed: string;
  visionSettingsTitle?: string;
  visionSettingsBody?: string;
  visionEnable?: string;
  visionProvider?: string;
  visionMineru?: string;
  visionConfiguredModel?: string;
  visionApprovalMode?: string;
  visionApprovalEach?: string;
  visionApprovalResearch?: string;
  visionStandingAuthorization?: string;
  visionAuthorizationScope?: string;
  visionAuthorizationStale?: string;
  visionRevoke?: string;
  modelCenter?: string;
  modelsEmpty?: string;
  followSystem?: string;
  manualLanguage?: string;
};

export function SettingsView({ language, preferences, copy, onSave }: {
  language: Language;
  preferences: Preferences;
  copy: SettingsCopy;
  onSave: (value: Partial<Preferences>) => Promise<Preferences>;
  onOpenModelCenter?: () => void;
}) {
  const [uiLanguage, setUiLanguage] = useState<Language>(preferences.ui_language);
  const [reportLanguage, setReportLanguage] = useState<Language>(preferences.report_language);
  const initialLanguageMode = preferences.ui_language_mode;
  const [languageMode, setLanguageMode] = useState<"system" | "manual">(initialLanguageMode ?? "manual");
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const initialVisionPolicy = parseVisionFallbackPolicy(preferences.vision_fallback_policy);
  const [visionEnabled, setVisionEnabled] = useState(initialVisionPolicy.enabled);
  const [visionProvider, setVisionProvider] = useState(initialVisionPolicy.provider);
  const [visionApprovalMode, setVisionApprovalMode] = useState(initialVisionPolicy.approval_mode);
  const [visionStandingAuthorization, setVisionStandingAuthorization] = useState(
    initialVisionPolicy.standing_authorization && isCurrentVisionFallbackPolicy(initialVisionPolicy),
  );
  const [visionModelId, setVisionModelId] = useState(initialVisionPolicy.model?.configured_model_id ?? "");
  const [visionModels, setVisionModels] = useState<ConfiguredModelSummary[]>([]);
  const [visionLoadFailed, setVisionLoadFailed] = useState(false);
  const systemLanguage = resolveSystemLanguage(typeof navigator === "undefined" ? [] : navigator.languages);

  useEffect(() => {
    if (visionProvider !== "configured_model") return;
    let cancelled = false;
    void listConfiguredModels().then((models) => {
      if (!cancelled) setVisionModels(models.filter((model) => model.enabled && model.health_status === "ready" && model.capabilities.includes("vision")));
    }).catch(() => {
      if (!cancelled) setVisionLoadFailed(true);
    });
    return () => { cancelled = true; };
  }, [visionProvider]);

  const selectedVisionModel = useMemo(
    () => visionModels.find((model) => model.configured_model_id === visionModelId),
    [visionModels, visionModelId],
  );

  const changeVisionProvider = (provider: "mineru_flash" | "configured_model") => {
    if (provider !== visionProvider) setVisionStandingAuthorization(false);
    setVisionProvider(provider);
  };

  const changeVisionModel = (modelId: string) => {
    if (modelId !== visionModelId) setVisionStandingAuthorization(false);
    setVisionModelId(modelId);
  };

  const policyFromForm = (): VisionFallbackPolicy => ({
    schema_version: VISION_FALLBACK_POLICY_VERSION,
    policy_version: VISION_FALLBACK_POLICY_VERSION,
    scope_version: VISION_FALLBACK_SCOPE_VERSION,
    enabled: visionEnabled,
    provider: visionProvider,
    ...(selectedVisionModel ? {
      model: {
        configured_model_id: selectedVisionModel.configured_model_id,
        configuration_version: selectedVisionModel.configuration_version ?? 1,
        role: "vision" as const,
      },
    } : {}),
    approval_mode: visionApprovalMode,
    standing_authorization: visionEnabled && visionStandingAuthorization,
    authorization_scope: "financial_failed_pages",
    provider_terms_version: VISION_FALLBACK_PROVIDER_TERMS_VERSION,
    ...(visionEnabled && visionStandingAuthorization ? { authorized_at: initialVisionPolicy.authorized_at ?? new Date().toISOString() } : {}),
    ...(!visionEnabled || !visionStandingAuthorization ? { revoked_at: new Date().toISOString() } : {}),
  });

  const submit = async () => {
    setState("saving");
    try {
      if (visionEnabled && visionProvider === "configured_model" && !selectedVisionModel) {
        setState("failed");
        return;
      }
      const updates: Partial<Preferences> = {
        ui_language: uiLanguage,
        ui_language_mode: languageMode,
        report_language: reportLanguage,
        vision_fallback_policy: JSON.stringify(policyFromForm()),
      };
      await onSave(updates);
      setState("saved");
    } catch {
      setState("failed");
    }
  };

  return (
    <div className="settings-view">
      <span className="eyebrow">OpenThesis</span><h2>{copy.settingsTitle}</h2><p>{copy.settingsBody}</p>
      <div className="settings-card">
        <label htmlFor="ui-language">{copy.interfaceLanguage}</label>
        <select id="ui-language" value={languageMode === "system" ? "system" : uiLanguage} onChange={(event) => {
          if (event.target.value === "system") {
            setLanguageMode("system");
            setUiLanguage(systemLanguage);
          } else {
            setLanguageMode("manual");
            setUiLanguage(event.target.value as Language);
          }
        }}>
          <option value="system">{copy.followSystem ?? "Follow system"} ({languageName(systemLanguage, language)})</option>
          {languageOptions().map((definition) => <option key={definition.id} value={definition.id}>{languageName(definition.id, language)}</option>)}
        </select>
        <label htmlFor="report-language">{copy.reportLanguage}</label><select id="report-language" value={reportLanguage} onChange={(event) => setReportLanguage(event.target.value as Language)}>{languageOptions().map((definition) => <option key={definition.id} value={definition.id}>{languageName(definition.id, language)}</option>)}</select>
        <button className="primary-button" type="button" onClick={() => void submit()} disabled={state === "saving"}>{state === "saving" ? copy.saving : copy.saveSettings}</button>
        {state === "saved" && <p className="settings-message" role="status">{copy.saved}</p>}
        {state === "failed" && <p className="settings-message error" role="alert">{copy.settingsFailed}</p>}
      </div>
      <div className="settings-card vision-settings" data-testid="vision-fallback-settings">
        <h3>{copy.visionSettingsTitle ?? "Vision fallback"}</h3>
        <p>{copy.visionSettingsBody ?? "Configure the persistent fallback for pages that fail local financial recognition."}</p>
        <label className="check-row"><input type="checkbox" checked={visionEnabled} onChange={(event) => {
          setVisionEnabled(event.target.checked);
          if (!event.target.checked) setVisionStandingAuthorization(false);
        }} />{copy.visionEnable ?? "Enable vision fallback"}</label>
        {visionEnabled && <>
          <label>{copy.visionProvider ?? "Vision provider"}<select value={visionProvider} onChange={(event) => changeVisionProvider(event.target.value as "mineru_flash" | "configured_model")}>
            <option value="mineru_flash">{copy.visionMineru ?? "MinerU Flash"}</option>
            <option value="configured_model">{copy.visionConfiguredModel ?? "Configured vision model"}</option>
          </select></label>
          {visionProvider === "configured_model" && (visionModels.length ? <label>{copy.visionConfiguredModel ?? "Configured vision model"}<select value={visionModelId} onChange={(event) => changeVisionModel(event.target.value)}>
            <option value="">{copy.modelsEmpty ?? "Select a tested vision model"}</option>
            {visionModels.map((model) => <option key={model.configured_model_id} value={model.configured_model_id}>{model.alias} · {model.model_id}</option>)}
          </select></label> : <p className="settings-message error" role="alert">{visionLoadFailed || !visionModels.length ? (copy.modelsEmpty ?? "No tested vision model is available.") : ""}</p>)}
          <fieldset className="vision-approval-mode">
            <legend>{copy.visionApprovalMode ?? "Failed-page upload approval"}</legend>
            <label className="vision-approval-option"><input type="radio" name="settings-vision-approval-mode" value="review_each_plan" checked={visionApprovalMode === "review_each_plan"} onChange={() => setVisionApprovalMode("review_each_plan")} />{copy.visionApprovalEach ?? "Review each page when needed"}</label>
            <label className="vision-approval-option"><input type="radio" name="settings-vision-approval-mode" value="approve_current_research" checked={visionApprovalMode === "approve_current_research"} onChange={() => setVisionApprovalMode("approve_current_research")} />{copy.visionApprovalResearch ?? "Approve all necessary pages for this research"}</label>
          </fieldset>
          <label className="check-row"><input type="checkbox" checked={visionStandingAuthorization} onChange={(event) => setVisionStandingAuthorization(event.target.checked)} />{copy.visionStandingAuthorization ?? "I authorize this fallback for future research runs"}</label>
          <p className="field-caption">{copy.visionAuthorizationScope ?? "Only necessary pages that fail local recognition and the selected service. This authorization is revocable."}</p>
          {!isCurrentVisionFallbackPolicy(initialVisionPolicy) && initialVisionPolicy.enabled && <p className="settings-message" role="status">{copy.visionAuthorizationStale ?? "Policy changed; please confirm authorization again."}</p>}
          {copy.visionRevoke && <button className="secondary-button" type="button" onClick={() => { setVisionEnabled(false); setVisionStandingAuthorization(false); }}>{copy.visionRevoke}</button>}
        </>}
      </div>
      <small className="settings-locale">{language}</small>
    </div>
  );
}
