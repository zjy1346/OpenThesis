import { useState } from "react";

import type { Language, Preferences } from "../../types";
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
  followSystem?: string;
  manualLanguage?: string;
};

export function SettingsView({ language, preferences, copy, onSave }: {
  language: Language;
  preferences: Preferences;
  copy: SettingsCopy;
  onSave: (value: Partial<Preferences>) => Promise<Preferences>;
}) {
  const [uiLanguage, setUiLanguage] = useState<Language>(preferences.ui_language);
  const [reportLanguage, setReportLanguage] = useState<Language>(preferences.report_language);
  const initialLanguageMode = preferences.ui_language_mode;
  const [languageMode, setLanguageMode] = useState<"system" | "manual">(initialLanguageMode ?? "manual");
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const systemLanguage = resolveSystemLanguage(typeof navigator === "undefined" ? [] : navigator.languages);

  const submit = async () => {
    setState("saving");
    try {
      const updates: Partial<Preferences> = { ui_language: uiLanguage, ui_language_mode: languageMode, report_language: reportLanguage };
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
      <small className="settings-locale">{language}</small>
    </div>
  );
}
