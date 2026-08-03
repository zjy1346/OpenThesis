import { BookOpenText, Settings, Sparkles } from "lucide-react";

import type { BootstrapResult } from "../../types";

type AboutCopy = {
  aboutTitle: string;
  aboutBody: string;
  versionLabel: string;
  contractLabel: string;
  capabilitiesLabel: string;
  architectureTitle: string;
  architectureBody: string;
  privacyTitle: string;
  privacyBody: string;
  scopeTitle: string;
  scopeBody: string;
};

export function AboutView({ bootstrap, copy }: { bootstrap: BootstrapResult; copy: AboutCopy }) {
  return (
    <div className="about-view">
      <header>
        <span className="eyebrow">OpenThesis</span>
        <h2>{copy.aboutTitle}</h2>
        <p>{copy.aboutBody}</p>
      </header>
      <dl className="diagnostic-strip">
        <div><dt>{copy.versionLabel}</dt><dd>{bootstrap.app_version}</dd></div>
        <div><dt>{copy.contractLabel}</dt><dd>JSON-RPC {bootstrap.contract_version}</dd></div>
        <div><dt>{copy.capabilitiesLabel}</dt><dd>{bootstrap.capabilities.length}</dd></div>
      </dl>
      <div className="about-grid">
        <section><Sparkles size={19} /><h3>{copy.architectureTitle}</h3><p>{copy.architectureBody}</p></section>
        <section><Settings size={19} /><h3>{copy.privacyTitle}</h3><p>{copy.privacyBody}</p></section>
        <section><BookOpenText size={19} /><h3>{copy.scopeTitle}</h3><p>{copy.scopeBody}</p></section>
      </div>
    </div>
  );
}
