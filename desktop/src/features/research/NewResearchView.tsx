import { useRef, useState } from "react";
import { Building2, ExternalLink, RefreshCw } from "lucide-react";

import {
  discoverModels,
  installResearchPack,
  openExternalUrl,
  searchCompanies,
  testModelConnection,
} from "../../backend";
import type {
  BootstrapResult,
  Company,
  Market,
  ModelPreset,
  ModelSelection,
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
};

const SEC_DEVELOPER_DOCS = "https://www.sec.gov/search-filings/edgar-application-programming-interfaces";

export function NewResearchView({ bootstrap, copy, onSavePreferences, onStart }: {
  bootstrap: BootstrapResult;
  copy: ResearchCopy;
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
  const [primary, setPrimary] = useState<ModelSelection>(() => initialModel(bootstrap));
  const [comparison, setComparison] = useState<ModelSelection>(() => initialModel(bootstrap));
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
  const marketCatalog = bootstrap.market_catalog ?? [
    { market: "US" as const, label_zh: "美股", label_en: "US equities", exchanges: ["NASDAQ", "NYSE"], default_currency: "USD", requires_sec_identity: true, disclosure_home: SEC_DEVELOPER_DOCS },
  ];
  const marketProfile = marketCatalog.find((item) => item.market === market);
  const requiresSecIdentity = marketProfile?.requires_sec_identity ?? market === "US";

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
      compare_enabled: compareEnabled,
      parallel_agents: parallelAgents,
      ...(compareEnabled ? { comparison_model: comparison } : {}),
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
          <ModelFields catalog={bootstrap.model_catalog} selection={primary} onChange={setPrimary} copy={copy} />
          <label className="check-row"><input type="checkbox" checked={compareEnabled} onChange={(event) => setCompareEnabled(event.target.checked)} />{copy.compareModels}</label>
          <label className="check-row"><input type="checkbox" checked={parallelAgents} onChange={(event) => setParallelAgents(event.target.checked)} />{copy.parallelAgents}</label>
          <p className="field-caption">{copy.parallelAgentsHint}</p>
          {compareEnabled && <div className="comparison-fields"><h4>{copy.comparisonModel}</h4><ModelFields catalog={bootstrap.model_catalog} selection={comparison} onChange={setComparison} copy={copy} idPrefix="compare" /></div>}
        </section>

        <section className="setup-card wide-card">
          <span className="step-label">03</span><h3>{copy.researchPack}</h3>
          <div className="field-grid two-column">
            <label>{copy.researchPack}<select value={packId} onChange={(event) => setPackId(event.target.value)}>{packs.map((pack) => <option value={pack.pack_id} key={pack.pack_id}>{pack.name} · {pack.version}</option>)}</select></label>
            <label className="check-row standalone"><input type="checkbox" checked={downloadFilings} onChange={(event) => setDownloadFilings(event.target.checked)} />{copy.downloadFilings}</label>
          </div>
          <label className="pack-import">{copy.importPack}<input type="file" accept=".othesis" onChange={(event) => void importPack(event.target.files?.[0])} /></label>
          {packMessage && <p className="pack-message" role="status">{packMessage}</p>}
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

function ModelFields({ catalog, selection, onChange, copy, idPrefix = "primary" }: {
  catalog: ModelPreset[];
  selection: ModelSelection;
  onChange: (value: ModelSelection) => void;
  copy: ResearchCopy;
  idPrefix?: string;
}) {
  const CUSTOM_MODEL_VALUE = "__openthesis_custom_model__";
  const [models, setModels] = useState<string[]>(() => {
    const preset = catalog.find((item) => item.preset_id === selection.preset_id);
    return preset?.recommended_models ?? [];
  });
  const [refreshing, setRefreshing] = useState(false);
  const [testing, setTesting] = useState(false);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"info" | "success" | "error">("info");
  const refreshRequestRef = useRef(0);
  const preset = catalog.find((item) => item.preset_id === selection.preset_id) ?? catalog[0];

  const choosePreset = (presetId: string) => {
    refreshRequestRef.current += 1;
    setRefreshing(false);
    const next = catalog.find((item) => item.preset_id === presetId) ?? catalog[0];
    const recommended = next?.recommended_models ?? [];
    setModels(recommended);
    setMessage("");
    setMessageTone("info");
    onChange({ preset_id: next?.preset_id ?? "none", model: recommended[0] ?? "", base_url: next?.base_url ?? "", api_key: "" });
  };

  const refresh = async () => {
    const requestId = ++refreshRequestRef.current;
    setRefreshing(true);
    setMessage("");
    try {
      const result = await discoverModels({ preset_id: selection.preset_id, base_url: selection.base_url, api_key: selection.api_key });
      if (requestId !== refreshRequestRef.current) return;
      const merged = Array.from(new Set([
        ...(preset?.recommended_models ?? []),
        ...result.models,
      ].map((model) => model.trim()).filter(Boolean)));
      setModels(merged);
      if (!selection.model.trim() && merged[0]) onChange({ ...selection, model: merged[0] });
      setMessage(result.warning || copy.modelsUpdated);
      setMessageTone(result.warning ? "error" : "success");
    } catch {
      if (requestId !== refreshRequestRef.current) return;
      setMessage(copy.refreshFailed);
      setMessageTone("error");
    } finally {
      if (requestId === refreshRequestRef.current) setRefreshing(false);
    }
  };

  const testConnection = async () => {
    setTesting(true);
    setMessage("");
    try {
      const result = await testModelConnection(selection);
      setMessage(result.message);
      setMessageTone(result.ok ? "success" : "error");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : copy.coreUnavailable);
      setMessageTone("error");
    } finally {
      setTesting(false);
    }
  };

  const openHelp = async () => {
    if (!preset?.help_url) return;
    try {
      await openExternalUrl(preset.help_url);
    } catch {
      setMessage(copy.coreUnavailable);
      setMessageTone("error");
    }
  };

  const isCustomModel = !selection.model || !models.includes(selection.model);
  const selectedModel = isCustomModel ? CUSTOM_MODEL_VALUE : selection.model;
  const modelInputId = `${idPrefix}-custom-model`;

  return (
    <div className="model-fields">
      <label htmlFor={`${idPrefix}-provider`}>{copy.modelProvider}<select id={`${idPrefix}-provider`} value={selection.preset_id} onChange={(event) => choosePreset(event.target.value)}>{catalog.map((item) => <option key={item.preset_id} value={item.preset_id}>{item.label}</option>)}</select></label>
      {preset?.preset_id === "none" ? <p className="offline-note">{copy.offlineModel}</p> : <>
        <label htmlFor={`${idPrefix}-model`}>{copy.modelName}
          {models.length > 0 ? <select id={`${idPrefix}-model`} value={selectedModel} onChange={(event) => {
            const value = event.target.value;
            onChange({ ...selection, model: value === CUSTOM_MODEL_VALUE ? (isCustomModel ? selection.model : "") : value });
          }}>
            {models.map((model) => <option key={model} value={model}>{model}</option>)}
            <option value={CUSTOM_MODEL_VALUE}>{copy.customModel}</option>
          </select> : <input id={`${idPrefix}-model`} aria-label={copy.customModel} placeholder={copy.modelsEmpty} value={selection.model} onChange={(event) => onChange({ ...selection, model: event.target.value })} />}
        </label>
        {models.length > 0 && isCustomModel && <label htmlFor={modelInputId}>{copy.customModel}<input id={modelInputId} value={selection.model} placeholder={copy.modelsEmpty} onChange={(event) => onChange({ ...selection, model: event.target.value })} /></label>}
        <div className="model-actions">
          <button className="refresh-button" type="button" onClick={() => void refresh()} disabled={refreshing || !preset?.models_path}><RefreshCw size={15} />{refreshing ? copy.refreshing : copy.refreshModels}</button>
          <button className="refresh-button" type="button" onClick={() => void testConnection()} disabled={testing || !selection.model.trim()}>{testing ? copy.testingConnection : copy.testConnection}</button>
          {preset?.help_url && <button className="refresh-button" type="button" onClick={() => void openHelp()}><ExternalLink size={15} />{copy.getKeyHelp}</button>}
        </div>
        <label>{copy.endpoint}<input value={selection.base_url} onChange={(event) => onChange({ ...selection, base_url: event.target.value })} /></label>
        <label>{copy.requestTimeout}<select value={selection.timeout_seconds ?? 180} onChange={(event) => onChange({ ...selection, timeout_seconds: Number(event.target.value) })}>
          {[60, 120, 180, 300, 600].map((seconds) => <option key={seconds} value={seconds}>{seconds}</option>)}
        </select></label>
        {preset?.requires_api_key && <label>{copy.apiKey}<input type="password" value={selection.api_key} autoComplete="off" onChange={(event) => onChange({ ...selection, api_key: event.target.value })} /></label>}
        {(preset?.preset_id === "kimi" || preset?.preset_id === "kimi-global") && <p className="provider-hint">{copy.kimiKeyHint}</p>}
        {message && <p className="catalog-message" data-tone={messageTone}>{message}</p>}
      </>}
    </div>
  );
}

function initialModel(bootstrap: BootstrapResult): ModelSelection {
  const savedBaseUrl = (bootstrap.preferences.base_url || "").trim();
  const savedPresetId = bootstrap.preferences.model_preset || "none";
  const presetId = savedPresetId === "kimi" && /api\.moonshot\.ai/i.test(savedBaseUrl)
    ? "kimi-global"
    : savedPresetId;
  const preset = bootstrap.model_catalog.find((item) => item.preset_id === presetId)
    ?? bootstrap.model_catalog.find((item) => item.preset_id === "none")
    ?? bootstrap.model_catalog[0];
  return {
    preset_id: preset?.preset_id ?? "none",
    model: bootstrap.preferences.model || preset?.recommended_models[0] || "",
    base_url: savedBaseUrl || preset?.base_url || "",
    api_key: "",
  };
}
