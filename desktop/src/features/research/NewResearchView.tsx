import { useEffect, useMemo, useState } from "react";
import { Building2, ExternalLink } from "lucide-react";

import {
  installResearchPack,
  listConfiguredModels,
  openExternalUrl,
  searchCompanies,
} from "../../backend";
import type {
  BootstrapResult,
  Company,
  Market,
  ConfiguredModelSummary,
  ModelReference,
  Preferences,
  ResearchPackSummary,
  ResearchRequest,
} from "../../types";

type ResearchCopy = {
  setupTitle: string;
  setupBody: string;
  market: string;
  usMarket: string;
  aShareMarket: string;
  hkMarket: string;
  officialDisclosure: string;
  selectedCompany: string;
  secProfile: string;
  secHelp: string;
  secHelpBody: string;
  openSecDocs: string;
  personal: string;
  independent: string;
  organization: string;
  secEmail: string;
  companySearch: string;
  searchAction: string;
  searching: string;
  commonCompanies: string;
  coreUnavailable: string;
  secRequired: string;
  chooseCompany: string;
  primaryModel: string;
  modelCenter: string;
  compareModels: string;
  parallelAgents: string;
  parallelAgentsHint: string;
  requestTimeout: string;
  comparisonModel: string;
  researchPack: string;
  downloadFilings: string;
  importPack: string;
  packInstalled: string;
  packFailed: string;
  advancedValuation: string;
  marketCap: string;
  manualPrice: string;
  manualCurrency: string;
  manualAsOf: string;
  manualDataHint: string;
  financeBeta: string;
  discountRate: string;
  terminalGrowth: string;
  launchResearch: string;
  modelProvider: string;
  offlineModel: string;
  modelName: string;
  customModel: string;
  modelsUpdated: string;
  modelsEmpty: string;
  kimiKeyHint: string;
  refreshModels: string;
  refreshing: string;
  testConnection: string;
  testingConnection: string;
  getKeyHelp: string;
  endpoint: string;
  apiKey: string;
  refreshFailed: string;
  visionFallback: string;
  visionFallbackTitle: string;
  visionFallbackBody: string;
  visionEnable: string;
  visionConsent: string;
  visionProvider: string;
  visionLiteHint: string;
  visionPrecisionHint: string;
  visionCustomHint: string;
  visionToken: string;
  visionEndpoint: string;
  visionModel: string;
  visionApiKey: string;
  visionTimeout: string;
  visionMissing: string;
};

const SEC_DEVELOPER_DOCS = "https://www.sec.gov/search-filings/edgar-application-programming-interfaces";

export function NewResearchView({ bootstrap, copy, onOpenModelCenter, onSavePreferences, onStart }: {
  bootstrap: BootstrapResult;
  copy: ResearchCopy;
  onOpenModelCenter: () => void;
  onSavePreferences: (value: Partial<Preferences>) => Promise<Preferences>;
  onStart: (request: ResearchRequest) => Promise<void>;
}) {
  const [profile, setProfile] = useState(bootstrap.preferences.sec_contact_profile || "personal");
  const [email, setEmail] = useState(bootstrap.preferences.sec_contact_email || "");
  const [market, setMarket] = useState<Market>(bootstrap.preferences.research_market || "US");
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Company[]>([]);
  const [selected, setSelected] = useState<Company | null>(null);
  const [searchError, setSearchError] = useState("");
  const [configuredModels, setConfiguredModels] = useState<ConfiguredModelSummary[]>([]);
  const [modelLoadError, setModelLoadError] = useState("");
  const [primaryModelId, setPrimaryModelId] = useState("");
  const [comparisonModelIds, setComparisonModelIds] = useState<string[]>([]);
  const [compareEnabled, setCompareEnabled] = useState(false);
  const [parallelAgents, setParallelAgents] = useState(
    bootstrap.preferences.parallel_agents === "true",
  );
  const [downloadFilings, setDownloadFilings] = useState(true);
  const [packId, setPackId] = useState(bootstrap.research_packs[0]?.pack_id ?? "");
  const [packs, setPacks] = useState<ResearchPackSummary[]>(bootstrap.research_packs);
  const [packMessage, setPackMessage] = useState("");
  const [marketCap, setMarketCap] = useState("");
  const [marketPrice, setMarketPrice] = useState("");
  const [marketCurrency, setMarketCurrency] = useState("USD");
  const [marketAsOf, setMarketAsOf] = useState(() => new Date().toISOString().slice(0, 10));
  const [discountRate, setDiscountRate] = useState("10");
  const [terminalGrowth, setTerminalGrowth] = useState("3");
  const [visionEnabled, setVisionEnabled] = useState(false);
  const [visionSource, setVisionSource] = useState<"mineru_flash" | "configured_model">("mineru_flash");
  const [visionConsent, setVisionConsent] = useState(false);
  const [visionModelId, setVisionModelId] = useState("");
  const marketCatalog = bootstrap.market_catalog ?? [
    { market: "US" as const, label_zh: "美股", label_en: "US equities", exchanges: ["NASDAQ", "NYSE"], default_currency: "USD", requires_sec_identity: true, disclosure_home: SEC_DEVELOPER_DOCS },
  ];
  const marketProfile = marketCatalog.find((item) => item.market === market);
  const requiresSecIdentity = marketProfile?.requires_sec_identity ?? market === "US";
  const usableModels = useMemo(
    () => configuredModels.filter((model) => model.enabled && model.health_status === "ready"),
    [configuredModels],
  );
  const visionModels = useMemo(
    () => usableModels.filter((model) => model.capabilities.includes("vision")),
    [usableModels],
  );

  useEffect(() => {
    let cancelled = false;
    void listConfiguredModels()
      .then((items) => {
        if (cancelled) return;
        setConfiguredModels(items);
        const usable = items.filter((model) => model.enabled && model.health_status === "ready");
        setPrimaryModelId((current) => current || usable[0]?.configured_model_id || "");
        const vision = usable.find((model) => model.capabilities.includes("vision"));
        setVisionModelId((current) => current || vision?.configured_model_id || "");
      })
      .catch((reason) => {
        if (!cancelled) setModelLoadError(reason instanceof Error ? reason.message : copy.coreUnavailable);
      });
    return () => { cancelled = true; };
  }, [copy.coreUnavailable]);

  const modelReference = (configuredModelId: string, role: ModelReference["role"]): ModelReference | undefined => {
    const model = configuredModels.find((item) => item.configured_model_id === configuredModelId);
    return model ? {
      configured_model_id: model.configured_model_id,
      configuration_version: model.configuration_version ?? 1,
      role,
    } : undefined;
  };

  const chooseMarket = (next: Market) => {
    setMarket(next);
    setSelected(null);
    setResults([]);
    setSearchError("");
    setMarketCurrency(marketCatalog.find((item) => item.market === next)?.default_currency ?? "USD");
  };

  const findCompanies = async () => {
    if (requiresSecIdentity && !email.includes("@")) {
      setSearchError(copy.secRequired);
      return;
    }
    setSearching(true);
    setSearchError("");
    try {
      await onSavePreferences({
        research_market: market,
        ...(requiresSecIdentity ? { sec_contact_profile: profile, sec_contact_email: email } : {}),
      });
      setResults(await searchCompanies(query, market));
    } catch (reason) {
      setSearchError(reason instanceof Error ? reason.message : copy.coreUnavailable);
    } finally {
      setSearching(false);
    }
  };

  const launch = async () => {
    if (!selected || (requiresSecIdentity && !email.includes("@"))) {
      setSearchError(selected ? copy.secRequired : copy.chooseCompany);
      return;
    }
    const primary = modelReference(primaryModelId, "primary");
    if (!primary) {
      setSearchError(modelLoadError || copy.modelsEmpty);
      return;
    }
    const comparisons = compareEnabled
      ? comparisonModelIds
          .map((id) => modelReference(id, "comparison"))
          .filter((item): item is ModelReference => Boolean(item))
      : [];
    let visionFallback: ResearchRequest["vision_fallback"];
    if (visionEnabled) {
      if (!visionConsent) {
        setSearchError(copy.visionMissing);
        return;
      }
      if (visionSource === "configured_model") {
        const vision = modelReference(visionModelId, "vision");
        if (!vision) {
          setSearchError(copy.visionMissing);
          return;
        }
        visionFallback = {
          enabled: true, consent: true, provider: "configured_model", model: vision,
          require_page_approval: true, language: market === "CN_A" ? "ch" : "en",
        };
      } else {
        visionFallback = {
          enabled: true, consent: true, provider: "mineru_flash",
          require_page_approval: true, language: market === "CN_A" ? "ch" : "en",
        };
      }
    }
    await onSavePreferences({
      sec_contact_profile: profile,
      sec_contact_email: email,
      parallel_agents: String(parallelAgents),
      research_market: market,
    });
    await onStart({
      mode: "company",
      company: selected,
      download_filings: downloadFilings,
      pack_id: packId,
      model: primary,
      compare_enabled: comparisons.length > 0,
      comparison_models: comparisons,
      parallel_agents: parallelAgents,
      valuation: {
        market_cap_billions: Number(marketCap) || 0,
        discount_rate_percent: Number(discountRate) || 10,
        terminal_growth_percent: Number(terminalGrowth) || 3,
      },
      market_snapshot: {
        price: Number(marketPrice) || 0,
        market_cap_billions: Number(marketCap) || 0,
        currency: marketCurrency,
        as_of: marketAsOf,
      },
      ...(visionFallback ? { vision_fallback: visionFallback } : {}),
    });
  };

  const importPack = async (file: File | undefined) => {
    if (!file) return;
    setPackMessage("");
    try {
      const installed = await installResearchPack(file);
      setPacks((current) => [
        ...current.filter((item) => item.pack_id !== installed.pack_id),
        installed,
      ]);
      setPackId(installed.pack_id);
      setPackMessage(copy.packInstalled);
    } catch {
      setPackMessage(copy.packFailed);
    }
  };

  const openSecHelp = async () => {
    try {
      await openExternalUrl(SEC_DEVELOPER_DOCS);
    } catch {
      setSearchError(copy.coreUnavailable);
    }
  };

  const openDisclosureHome = async () => {
    if (!marketProfile?.disclosure_home) return;
    try {
      await openExternalUrl(marketProfile.disclosure_home);
    } catch {
      setSearchError(copy.coreUnavailable);
    }
  };

  return (
    <div className="research-setup">
      <header className="setup-heading"><span className="eyebrow">OpenThesis</span><h2>{copy.setupTitle}</h2><p>{copy.setupBody}</p></header>
      <div className="setup-grid">
        <section className="setup-card">
          <span className="step-label">01</span><h3>{copy.selectedCompany}</h3>
          <div className="market-source-row">
            <label className="market-field"><span>{copy.market}</span><select value={market} onChange={(event) => chooseMarket(event.target.value as Market)}>
              <option value="US">{copy.usMarket}</option><option value="CN_A">{copy.aShareMarket}</option><option value="HK">{copy.hkMarket}</option>
            </select></label>
            {marketProfile?.disclosure_home && <button className="disclosure-button" type="button" onClick={() => void openDisclosureHome()}><ExternalLink size={14} />{copy.officialDisclosure}</button>}
          </div>
          {requiresSecIdentity && <>
          <div className="field-grid two-column">
            <label>{copy.secProfile}<select value={profile} onChange={(event) => setProfile(event.target.value)}>
              <option value="personal">{copy.personal}</option><option value="independent">{copy.independent}</option><option value="organization">{copy.organization}</option>
            </select></label>
            <label>{copy.secEmail}<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.com" /></label>
          </div>
          <details className="sec-help">
            <summary>{copy.secHelp}</summary>
            <p>{copy.secHelpBody}</p>
            <button className="refresh-button" type="button" onClick={() => void openSecHelp()}><ExternalLink size={15} />{copy.openSecDocs}</button>
          </details>
          </>}
          <div className="search-row"><input aria-label={copy.companySearch} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={copy.companySearch} />
            <button type="button" onClick={() => void findCompanies()} disabled={searching || !query.trim()}>{searching ? copy.searching : copy.searchAction}</button></div>
          <span className="field-caption">{copy.commonCompanies}</span>
          <div className="company-chips">{bootstrap.common_companies.filter((company) => (company.market ?? "US") === market).map((company) => <button key={company.security_id || company.cik} type="button" data-selected={selected?.cik === company.cik || undefined} onClick={() => { setSelected(company); setMarketCurrency(company.listing_currency || marketProfile?.default_currency || "USD"); }}>{company.ticker}</button>)}</div>
          {results.length > 0 && <div className="company-results">{results.map((company) => <button key={company.security_id || company.cik} type="button" onClick={() => { setSelected(company); setMarketCurrency(company.listing_currency || marketProfile?.default_currency || "USD"); }}><Building2 size={16} /><span><strong>{company.ticker}</strong>{company.name}</span></button>)}</div>}
          {selected && <div className="selected-company"><Building2 size={18} /><span><strong>{selected.ticker}</strong>{selected.name}</span></div>}
          {selected?.industry_support === "financial_beta" && <p className="provider-hint">{copy.financeBeta}</p>}
          {searchError && <p className="inline-error" role="alert">{searchError}</p>}
        </section>

        <section className="setup-card">
          <span className="step-label">02</span><h3>{copy.primaryModel}</h3>
          {usableModels.length ? <>
            <ConfiguredModelPicker
              id="primary-configured-model"
              label={copy.primaryModel}
              models={usableModels}
              value={primaryModelId}
              onChange={setPrimaryModelId}
            />
            <label className="check-row"><input type="checkbox" checked={compareEnabled} onChange={(event) => setCompareEnabled(event.target.checked)} />{copy.compareModels}</label>
            {compareEnabled && <fieldset className="comparison-model-picker">
              <legend>{copy.comparisonModel}</legend>
              {usableModels.filter((model) => model.configured_model_id !== primaryModelId).map((model) => <label key={model.configured_model_id}>
                <input
                  type="checkbox"
                  checked={comparisonModelIds.includes(model.configured_model_id)}
                  onChange={(event) => setComparisonModelIds((current) => event.target.checked
                    ? Array.from(new Set([...current, model.configured_model_id]))
                    : current.filter((id) => id !== model.configured_model_id))}
                />
                <span><strong>{model.alias}</strong><small>{model.model_id}{isFreeModel(model) ? " · Free" : ""}</small></span>
              </label>)}
            </fieldset>}
            <label className="check-row"><input type="checkbox" checked={parallelAgents} onChange={(event) => setParallelAgents(event.target.checked)} />{copy.parallelAgents}</label>
            <p className="field-caption">{copy.parallelAgentsHint}</p>
          </> : <div className="model-picker-empty" role="status">
            <p>{modelLoadError || copy.modelsEmpty}</p>
            <button className="secondary-button" type="button" onClick={onOpenModelCenter}>{copy.modelCenter}</button>
          </div>}
        </section>

        <section className="setup-card wide-card">
          <span className="step-label">03</span><h3>{copy.researchPack}</h3>
          <div className="field-grid two-column">
            <label>{copy.researchPack}<select value={packId} onChange={(event) => setPackId(event.target.value)}>{packs.map((pack) => <option value={pack.pack_id} key={pack.pack_id}>{pack.name} · {pack.version}</option>)}</select></label>
            <label className="check-row standalone"><input type="checkbox" checked={downloadFilings} onChange={(event) => setDownloadFilings(event.target.checked)} />{copy.downloadFilings}</label>
          </div>
          <label className="pack-import">{copy.importPack}<input type="file" accept=".ot" onChange={(event) => void importPack(event.target.files?.[0])} /></label>
          {packMessage && <p className="pack-message" role="status">{packMessage}</p>}
          <details className="advanced-settings"><summary>{copy.visionFallbackTitle}</summary>
            <p className="field-caption">{copy.visionFallbackBody}</p>
            <label className="check-row"><input type="checkbox" checked={visionEnabled} onChange={(event) => { setVisionEnabled(event.target.checked); if (!event.target.checked) setVisionConsent(false); }} />{copy.visionEnable}</label>
            {visionEnabled && <div className="vision-source-panel">
              <label className="configured-model-picker" htmlFor="vision-source">
                <span>{copy.visionProvider}</span>
                <select id="vision-source" value={visionSource} onChange={(event) => setVisionSource(event.target.value as "mineru_flash" | "configured_model")}>
                  <option value="mineru_flash">{copy.visionToken}</option>
                  <option value="configured_model" disabled={!visionModels.length}>{copy.visionModel}</option>
                </select>
              </label>
              <p className="field-caption">{visionSource === "mineru_flash" ? copy.visionLiteHint : copy.visionPrecisionHint}</p>
              {visionSource === "configured_model" && (visionModels.length
                ? <ConfiguredModelPicker id="vision-configured-model" label={copy.visionModel} models={visionModels} value={visionModelId} onChange={setVisionModelId} />
                : <div className="model-picker-empty"><p>{copy.modelsEmpty}</p><button className="secondary-button" type="button" onClick={onOpenModelCenter}>{copy.modelCenter}</button></div>)}
              <p className="field-caption">{copy.visionCustomHint}</p>
              <label className="vision-consent check-row"><input type="checkbox" checked={visionConsent} onChange={(event) => setVisionConsent(event.target.checked)} />{copy.visionConsent}</label>
            </div>}
          </details>
          <details className="advanced-settings"><summary>{copy.advancedValuation}</summary><p className="field-caption">{copy.manualDataHint}</p><div className="field-grid three-column">
            <label>{copy.manualPrice}<input inputMode="decimal" value={marketPrice} onChange={(event) => setMarketPrice(event.target.value)} /></label>
            <label>{copy.manualCurrency}<select value={marketCurrency} onChange={(event) => setMarketCurrency(event.target.value)}>{[marketProfile?.default_currency, selected?.listing_currency, selected?.reporting_currency, "USD", "CNY", "HKD"].filter((item, index, all): item is string => Boolean(item) && all.indexOf(item) === index).map((currency) => <option key={currency} value={currency}>{currency}</option>)}</select></label>
            <label>{copy.manualAsOf}<input type="date" value={marketAsOf} onChange={(event) => setMarketAsOf(event.target.value)} /></label>
            <label>{copy.marketCap}<input inputMode="decimal" value={marketCap} onChange={(event) => setMarketCap(event.target.value)} /></label>
            <label>{copy.discountRate}<input inputMode="decimal" value={discountRate} onChange={(event) => setDiscountRate(event.target.value)} /></label>
            <label>{copy.terminalGrowth}<input inputMode="decimal" value={terminalGrowth} onChange={(event) => setTerminalGrowth(event.target.value)} /></label>
          </div></details>
          <button className="primary-button launch-button" type="button" disabled={!selected} onClick={() => void launch()}>{selected ? copy.launchResearch : copy.chooseCompany}</button>
        </section>
      </div>
    </div>
  );
}

function isFreeModel(model: ConfiguredModelSummary): boolean {
  return model.billing_class === "free_tier"
    || model.billing_class === "local_no_provider_fee"
    || model.free_tier;
}

function ConfiguredModelPicker({ id, label, models, value, onChange }: {
  id: string;
  label: string;
  models: ConfiguredModelSummary[];
  value: string;
  onChange: (value: string) => void;
}) {
  return <label className="configured-model-picker" htmlFor={id}>
    <span>{label}</span>
    <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
      {models.map((model) => <option key={model.configured_model_id} value={model.configured_model_id}>
        {model.alias} · {model.model_id}{isFreeModel(model) ? " · Free" : ""}
      </option>)}
    </select>
  </label>;
}