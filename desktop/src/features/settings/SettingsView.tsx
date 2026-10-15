import { useEffect, useMemo, useRef, useState } from "react";

import { listConfiguredModels } from "../../backend";
import { modelEligibility } from "../../modelEligibility";
import type { ConfiguredModelSummary, Language, OfficialNetworkPolicy, Preferences, VisionFallbackPolicy } from "../../types";
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
  languageSettingsTitle?: string;
  languageSettingsBody?: string;
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
  discardChanges?: string;
  unsavedChanges?: string;
  visionAuthorizationTitle?: string;
  officialNetworkTitle?: string;
  officialNetworkBody?: string;
  officialNetworkProxy?: string;
  officialNetworkProxyHint?: string;
  officialNetworkProfiles?: string;
  officialNetworkProfilesField?: string;
  officialNetworkProfilesHint?: string;
  officialNetworkTlsNote?: string;
  officialNetworkProfilesInvalid?: string;
};

const DEFAULT_OFFICIAL_NETWORK_POLICY: OfficialNetworkPolicy = {
  version: "official-network-v1",
  connect_proxy: "",
  host_profiles: {},
};

function parseOfficialNetworkPolicy(value: string | undefined): OfficialNetworkPolicy {
  if (!value) return DEFAULT_OFFICIAL_NETWORK_POLICY;
  try {
    const parsed = JSON.parse(value) as Partial<OfficialNetworkPolicy>;
    if (parsed.version !== "official-network-v1" || typeof parsed.connect_proxy !== "string" || !parsed.host_profiles || typeof parsed.host_profiles !== "object" || Array.isArray(parsed.host_profiles)) {
      return DEFAULT_OFFICIAL_NETWORK_POLICY;
    }
    const entries = Object.entries(parsed.host_profiles);
    if (entries.some(([host, cidrs]) => !host.trim() || !Array.isArray(cidrs) || cidrs.some((cidr) => typeof cidr !== "string"))) {
      return DEFAULT_OFFICIAL_NETWORK_POLICY;
    }
    return {
      version: "official-network-v1",
      connect_proxy: parsed.connect_proxy,
      host_profiles: Object.fromEntries(entries.map(([host, cidrs]) => [host, [...cidrs]])),
    };
  } catch {
    return DEFAULT_OFFICIAL_NETWORK_POLICY;
  }
}

function settingsSignatureFromPreferences(preferences: Preferences): string {
  const policy = parseVisionFallbackPolicy(preferences.vision_fallback_policy);
  return JSON.stringify({
    uiLanguage: preferences.ui_language,
    reportLanguage: preferences.report_language,
    languageMode: preferences.ui_language_mode ?? "manual",
    visionEnabled: policy.enabled,
    visionProvider: policy.provider,
    visionApprovalMode: policy.approval_mode,
    visionStandingAuthorization: policy.standing_authorization && isCurrentVisionFallbackPolicy(policy),
    visionModelId: policy.model?.configured_model_id ?? "",
    officialNetworkPolicy: JSON.stringify(parseOfficialNetworkPolicy(preferences.official_network_policy)),
  });
}

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
  const initialOfficialNetworkPolicy = parseOfficialNetworkPolicy(preferences.official_network_policy);
  const [connectProxy, setConnectProxy] = useState(initialOfficialNetworkPolicy.connect_proxy);
  const [hostProfilesJson, setHostProfilesJson] = useState(() => JSON.stringify(initialOfficialNetworkPolicy.host_profiles, null, 2));
  const [networkProfilesInvalid, setNetworkProfilesInvalid] = useState(false);
  const [hostProfilesOpen, setHostProfilesOpen] = useState(false);
  const systemLanguage = resolveSystemLanguage(typeof navigator === "undefined" ? [] : navigator.languages);

  useEffect(() => {
    if (visionProvider !== "configured_model") return;
    let cancelled = false;
    void listConfiguredModels().then((models) => {
      if (!cancelled) setVisionModels(models.filter((model) => modelEligibility(model, "vision", { explicitRetry: true }).selectable));
    }).catch(() => {
      if (!cancelled) setVisionLoadFailed(true);
    });
    return () => { cancelled = true; };
  }, [visionProvider]);

  const selectedVisionModel = useMemo(
    () => visionModels.find((model) => model.configured_model_id === visionModelId),
    [visionModels, visionModelId],
  );
  const formSignature = JSON.stringify({
    uiLanguage,
    reportLanguage,
    languageMode,
    visionEnabled,
    visionProvider,
    visionApprovalMode,
    visionStandingAuthorization,
    visionModelId,
    officialNetworkPolicy: JSON.stringify({
      ...DEFAULT_OFFICIAL_NETWORK_POLICY,
      connect_proxy: connectProxy.trim(),
      host_profiles: (() => {
        try { return JSON.parse(hostProfilesJson); } catch { return hostProfilesJson; }
      })(),
    }),
  });
  const preferenceSignature = settingsSignatureFromPreferences(preferences);
  const [savedSignature, setSavedSignature] = useState(preferenceSignature);
  const lastPreferenceSignature = useRef(preferenceSignature);
  const dirty = formSignature !== savedSignature;

  useEffect(() => {
    if (preferenceSignature === lastPreferenceSignature.current) return;
    lastPreferenceSignature.current = preferenceSignature;
    if (formSignature !== savedSignature) return;
    const nextPolicy = parseVisionFallbackPolicy(preferences.vision_fallback_policy);
    const nextOfficialNetworkPolicy = parseOfficialNetworkPolicy(preferences.official_network_policy);
    setUiLanguage(preferences.ui_language);
    setReportLanguage(preferences.report_language);
    setLanguageMode(preferences.ui_language_mode ?? "manual");
    setVisionEnabled(nextPolicy.enabled);
    setVisionProvider(nextPolicy.provider);
    setVisionApprovalMode(nextPolicy.approval_mode);
    setVisionStandingAuthorization(nextPolicy.standing_authorization && isCurrentVisionFallbackPolicy(nextPolicy));
    setVisionModelId(nextPolicy.model?.configured_model_id ?? "");
    setConnectProxy(nextOfficialNetworkPolicy.connect_proxy);
    setHostProfilesJson(JSON.stringify(nextOfficialNetworkPolicy.host_profiles, null, 2));
    setNetworkProfilesInvalid(false);
    setSavedSignature(preferenceSignature);
    setState("idle");
  }, [formSignature, preferences, preferenceSignature, savedSignature]);

  useEffect(() => {
    if (dirty && state === "saved") setState("idle");
  }, [dirty, state]);

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
        connection_id: selectedVisionModel.connection_id,
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
      let hostProfiles: unknown;
      try {
        hostProfiles = JSON.parse(hostProfilesJson);
      } catch {
        setNetworkProfilesInvalid(true);
        setHostProfilesOpen(true);
        setState("failed");
        return;
      }
      if (!hostProfiles || typeof hostProfiles !== "object" || Array.isArray(hostProfiles)
        || Object.entries(hostProfiles as Record<string, unknown>).some(([host, cidrs]) => !host.trim() || !Array.isArray(cidrs) || cidrs.some((cidr) => typeof cidr !== "string"))) {
        setNetworkProfilesInvalid(true);
        setHostProfilesOpen(true);
        setState("failed");
        return;
      }
      setNetworkProfilesInvalid(false);
      const officialNetworkPolicy: OfficialNetworkPolicy = {
        version: "official-network-v1",
        connect_proxy: connectProxy.trim(),
        host_profiles: hostProfiles as Record<string, string[]>,
      };
      const updates: Partial<Preferences> = {
        ui_language: uiLanguage,
        ui_language_mode: languageMode,
        report_language: reportLanguage,
        vision_fallback_policy: JSON.stringify(policyFromForm()),
        official_network_policy: JSON.stringify(officialNetworkPolicy),
      };
      await onSave(updates);
      setSavedSignature(formSignature);
      setState("saved");
    } catch {
      setState("failed");
    }
  };

  const discard = () => {
    const nextPolicy = parseVisionFallbackPolicy(preferences.vision_fallback_policy);
    const nextOfficialNetworkPolicy = parseOfficialNetworkPolicy(preferences.official_network_policy);
    setUiLanguage(preferences.ui_language);
    setReportLanguage(preferences.report_language);
    setLanguageMode(preferences.ui_language_mode ?? "manual");
    setVisionEnabled(nextPolicy.enabled);
    setVisionProvider(nextPolicy.provider);
    setVisionApprovalMode(nextPolicy.approval_mode);
    setVisionStandingAuthorization(
      nextPolicy.standing_authorization && isCurrentVisionFallbackPolicy(nextPolicy),
    );
    setVisionModelId(nextPolicy.model?.configured_model_id ?? "");
    setConnectProxy(nextOfficialNetworkPolicy.connect_proxy);
    setHostProfilesJson(JSON.stringify(nextOfficialNetworkPolicy.host_profiles, null, 2));
    setNetworkProfilesInvalid(false);
    setSavedSignature(preferenceSignature);
    lastPreferenceSignature.current = preferenceSignature;
    setState("idle");
  };

  return (
    <div className="settings-view">
      <header className="settings-header">
        <span className="eyebrow">OpenThesis</span>
        <h2>{copy.settingsTitle}</h2>
        <p>{copy.settingsBody}</p>
      </header>

      <section className="settings-card settings-section" aria-labelledby="language-settings-title">
        <header className="settings-section-header">
          <h3 id="language-settings-title">{copy.languageSettingsTitle ?? "Interface and report language"}</h3>
          <p>{copy.languageSettingsBody ?? "Choose the language for the app interface and research reports separately."}</p>
        </header>
        <div className="settings-fields">
          <label htmlFor="ui-language"><span>{copy.interfaceLanguage}</span><select id="ui-language" value={languageMode === "system" ? "system" : uiLanguage} onChange={(event) => {
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
          </select></label>
          <label htmlFor="report-language"><span>{copy.reportLanguage}</span><select id="report-language" value={reportLanguage} onChange={(event) => setReportLanguage(event.target.value as Language)}>{languageOptions().map((definition) => <option key={definition.id} value={definition.id}>{languageName(definition.id, language)}</option>)}</select></label>
        </div>
      </section>

      <section className="settings-card settings-section vision-settings" data-testid="vision-fallback-settings" aria-labelledby="vision-settings-title">
        <header className="settings-section-header">
          <h3 id="vision-settings-title">{copy.visionSettingsTitle ?? "Vision fallback"}</h3>
          <p>{copy.visionSettingsBody ?? "Configure the persistent fallback for pages that fail local financial recognition."}</p>
        </header>
        <label className="settings-toggle-row"><span><strong>{copy.visionEnable ?? "Enable vision fallback"}</strong></span><input type="checkbox" checked={visionEnabled} onChange={(event) => {
          setVisionEnabled(event.target.checked);
          if (!event.target.checked) setVisionStandingAuthorization(false);
        }} /></label>
        {visionEnabled && <div className="vision-settings-body">
          <div className="settings-fields">
            <label><span>{copy.visionProvider ?? "Vision provider"}</span><select value={visionProvider} onChange={(event) => changeVisionProvider(event.target.value as "mineru_flash" | "configured_model")}>
              <option value="mineru_flash">{copy.visionMineru ?? "MinerU Flash"}</option>
              <option value="configured_model">{copy.visionConfiguredModel ?? "Configured vision model"}</option>
            </select></label>
            {visionProvider === "configured_model" && (visionModels.length ? <label><span>{copy.visionConfiguredModel ?? "Configured vision model"}</span><select value={visionModelId} onChange={(event) => changeVisionModel(event.target.value)}>
              <option value="">{copy.modelsEmpty ?? "Select a tested vision model"}</option>
              {visionModels.map((model) => <option key={model.configured_model_id} value={model.configured_model_id}>{model.alias} · {model.model_id}</option>)}
            </select></label> : <p className="settings-message error" role="alert">{visionLoadFailed || !visionModels.length ? (copy.modelsEmpty ?? "No tested vision model is available.") : ""}</p>)}
          </div>
          <fieldset className="vision-approval-mode">
            <legend>{copy.visionApprovalMode ?? "Failed-page upload approval"}</legend>
            <label className={`vision-approval-option ${visionApprovalMode === "review_each_plan" ? "active" : ""}`}><input type="radio" name="settings-vision-approval-mode" value="review_each_plan" checked={visionApprovalMode === "review_each_plan"} onChange={() => setVisionApprovalMode("review_each_plan")} /><span>{copy.visionApprovalEach ?? "Review each page when needed"}</span></label>
            <label className={`vision-approval-option ${visionApprovalMode === "approve_current_research" ? "active" : ""}`}><input type="radio" name="settings-vision-approval-mode" value="approve_current_research" checked={visionApprovalMode === "approve_current_research"} onChange={() => setVisionApprovalMode("approve_current_research")} /><span>{copy.visionApprovalResearch ?? "Approve all necessary pages for this research"}</span></label>
          </fieldset>
          <div className="vision-authorization-panel">
            <h4>{copy.visionAuthorizationTitle ?? copy.visionStandingAuthorization ?? "Persistent authorization"}</h4>
            <label className="check-row"><input type="checkbox" checked={visionStandingAuthorization} onChange={(event) => setVisionStandingAuthorization(event.target.checked)} />{copy.visionStandingAuthorization ?? "I authorize this fallback for future research runs"}</label>
            <p className="field-caption">{copy.visionAuthorizationScope ?? "Only necessary pages that fail local recognition and the selected service. This authorization is revocable."}</p>
            {!isCurrentVisionFallbackPolicy(initialVisionPolicy) && initialVisionPolicy.enabled && <p className="settings-message" role="status">{copy.visionAuthorizationStale ?? "Policy changed; please confirm authorization again."}</p>}
            {copy.visionRevoke && <button className="secondary-button" type="button" onClick={() => { setVisionEnabled(false); setVisionStandingAuthorization(false); }}>{copy.visionRevoke}</button>}
          </div>
        </div>}
      </section>

      <section className="settings-card settings-section official-network-settings" data-testid="official-network-settings" aria-labelledby="official-network-settings-title">
        <header className="settings-section-header">
          <h3 id="official-network-settings-title">{copy.officialNetworkTitle ?? "Official data network"}</h3>
          <p>{copy.officialNetworkBody ?? "Optional network routing for official company and filing sources."}</p>
        </header>
        <div className="settings-fields official-network-fields">
          <label htmlFor="official-network-proxy"><span>{copy.officialNetworkProxy ?? "HTTP CONNECT proxy"}</span><input id="official-network-proxy" type="url" value={connectProxy} onChange={(event) => setConnectProxy(event.target.value)} placeholder="http://127.0.0.1:7890" autoComplete="off" /></label>
          <p className="field-caption">{copy.officialNetworkProxyHint ?? "Use a no-credentials HTTP CONNECT proxy, for example http://127.0.0.1:7890."}</p>
        </div>
        <details className="official-network-profiles" open={hostProfilesOpen} onToggle={(event) => setHostProfilesOpen(event.currentTarget.open)}>
          <summary>{copy.officialNetworkProfiles ?? "Advanced official-host network profiles"}</summary>
          <label htmlFor="official-network-host-profiles"><span>{copy.officialNetworkProfilesField ?? "Host profiles (JSON)"}</span><textarea id="official-network-host-profiles" rows={5} spellCheck={false} value={hostProfilesJson} onChange={(event) => { setHostProfilesJson(event.target.value); setNetworkProfilesInvalid(false); }} /></label>
          <p className="field-caption">{copy.officialNetworkProfilesHint ?? "Disabled when empty. Only exact built-in official hosts and their explicitly listed CIDRs are accepted; this never loosens policy automatically."}</p>
          {networkProfilesInvalid && <p className="settings-message error" role="alert">{copy.officialNetworkProfilesInvalid ?? "Enter a JSON object mapping built-in official hosts to arrays of CIDRs."}</p>}
        </details>
        <p className="field-caption official-network-tls-note">{copy.officialNetworkTlsNote ?? "TLS certificate verification remains enabled. These settings are not provider credentials."}</p>
      </section>

      <footer className="settings-actions">
        <div className="settings-action-status">
          {dirty && state === "idle" && <p className="settings-message" role="status">{copy.unsavedChanges ?? "Unsaved changes"}</p>}
          {state === "saved" && <p className="settings-message" role="status">{copy.saved}</p>}
          {state === "failed" && <p className="settings-message error" role="alert">{copy.settingsFailed}</p>}
        </div>
        <div className="settings-action-buttons">
          {dirty && <button className="secondary-button" type="button" onClick={discard}>{copy.discardChanges ?? "Discard changes"}</button>}
          <button className="primary-button" type="button" onClick={() => void submit()} disabled={state === "saving" || !dirty}>{state === "saving" ? copy.saving : copy.saveSettings}</button>
        </div>
      </footer>
    </div>
  );
}
