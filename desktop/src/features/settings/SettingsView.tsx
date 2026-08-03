import { useState } from "react";

import type { Language, Preferences } from "../../types";

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
};

export function SettingsView({ language, preferences, copy, onSave }: {
  language: Language;
  preferences: Preferences;
  copy: SettingsCopy;
  onSave: (value: Partial<Preferences>) => Promise<Preferences>;
}) {
  const [uiLanguage, setUiLanguage] = useState<Language>(preferences.ui_language);
  const [reportLanguage, setReportLanguage] = useState<Language>(preferences.report_language);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");

  const submit = async () => {
    setState("saving");
    try {
      await onSave({ ui_language: uiLanguage, report_language: reportLanguage });
      setState("saved");
    } catch {
      setState("failed");
    }
  };

  return (
    <div className="settings-view">
      <span className="eyebrow">OpenThesis</span><h2>{copy.settingsTitle}</h2><p>{copy.settingsBody}</p>
      <div className="settings-card">
        <label htmlFor="ui-language">{copy.interfaceLanguage}</label><select id="ui-language" value={uiLanguage} onChange={(event) => setUiLanguage(event.target.value as Language)}><option value="zh-CN">{copy.chinese}</option><option value="en">{copy.english}</option></select>
        <label htmlFor="report-language">{copy.reportLanguage}</label><select id="report-language" value={reportLanguage} onChange={(event) => setReportLanguage(event.target.value as Language)}><option value="zh-CN">{copy.chinese}</option><option value="en">{copy.english}</option></select>
        <button className="primary-button" type="button" onClick={() => void submit()} disabled={state === "saving"}>{state === "saving" ? copy.saving : copy.saveSettings}</button>
        {state === "saved" && <p className="settings-message" role="status">{copy.saved}</p>}
        {state === "failed" && <p className="settings-message error" role="alert">{copy.settingsFailed}</p>}
      </div>
      <small className="settings-locale">{language}</small>
    </div>
  );
}
