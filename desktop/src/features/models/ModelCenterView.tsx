import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Cable, Check, ExternalLink, KeyRound, Plus, RefreshCw, Search, ShieldCheck, Trash2, X } from "lucide-react";

import deepseekLogo from "../../assets/providers/deepseek.png";
import geminiLogo from "../../assets/providers/gemini.jpg";
import glmLogo from "../../assets/providers/glm.png";
import kimiLogo from "../../assets/providers/kimi.jpg";
import ollamaLogo from "../../assets/providers/ollama.png";
import openaiLogo from "../../assets/providers/openai.png";
import openrouterLogo from "../../assets/providers/openrouter.jpg";
import qwenLogo from "../../assets/providers/qwen.png";

import {
  deleteConfiguredModel,
  deleteProviderConnection,
  discoverConnectionModels,
  listConfiguredModels,
  listModelProviders,
  listProviderConnections,
  openExternalUrl,
  saveConfiguredModel,
  saveProviderConnection,
  rotateProviderConnectionSecret,
  setProviderConnectionEnabled,
  setProviderConnectionSecret,
  testConfiguredModel,
  testProviderConnection,
} from "../../backend";
import type {
  ConfiguredModelSummary,
  DiscoveredModel,
  Language,
  ProviderConnectionSummary,
  ProviderDefinition,
} from "../../types";

type Filter = "all" | "cloud" | "local" | "free";
type DialogState =
  | { kind: "rotate"; connection: ProviderConnectionSummary }
  | { kind: "delete-connection"; connection: ProviderConnectionSummary }
  | { kind: "delete-model"; model: ConfiguredModelSummary };

const EN = {
  intro: "Configure connections once, then select model references in research and OT Studio. Secrets are stored only in Windows Credential Manager; existing keys are never returned to this page.",
  search: "Search providers", all: "All", cloud: "Cloud", local: "Local", free: "Free paths",
  connection: "connection", connections: "connections", ready: "Ready", untested: "Untested", disabled: "Disabled", unauthorized: "Unauthorized", rateLimited: "Rate limited", unavailable: "Unavailable",
  configure: "Configure provider", close: "Close", registration: "Registration and API keys", addConnection: "Add connection",
  displayName: "Connection name", endpoint: "Endpoint", region: "Region", apiKey: "API key",
  apiKeyHint: "After saving, only the configured state remains visible.", save: "Save connection", saving: "Saving…",
  customConfirm: "I confirm that the key and research content will be sent to the full Origin shown above.",
  localPrivacy: "Local Ollama: research content stays in the Ollama service on this computer.",
  remotePrivacy: "Remote Ollama: research content is sent to that address and is not labelled local.",
  existing: "Existing connections", enable: "Enable", disable: "Disable", replaceKey: "Replace key", delete: "Delete",
  models: "Configured models", discover: "Discover models", discovering: "Discovering…", testConnection: "Test connection", testingConnection: "Testing connection…", testUsing: "Uses", advancedModels: "Advanced: custom model ID", manualModel: "Manual model ID",
  addManual: "Add candidate", selectModels: "Select one or more models", addModels: "Add selected models",
  selectModel: "Select model", visionCapability: "Vision input", visionCapabilityHint: "Enable only when this exact model accepts image input. Testing will verify both text and vision before it becomes available in research.",
  testing: "Testing…", test: "Test", noConnections: "No connections yet. Add an account or an existing Ollama service.",
  noModels: "No configured models yet.", noProviders: "No providers match this filter.",
  credentialSaved: "Credential saved securely.", connectionSaved: "Connection saved.", modelsSaved: "Models added.", testSucceeded: "Connection succeeded.",
  rotateTitle: "Replace the saved API key", rotateBody: "The new key is tested before it becomes active. If testing fails, the current key remains unchanged.",
  deleteConnectionTitle: "Delete this connection?", confirmDelete: "This permanently removes the connection, its configured models, and its Windows credential.",
  deleteModelTitle: "Delete this configured model?", confirmDeleteModel: "The connection and saved credential remain available.",
  cancel: "Cancel", confirm: "Confirm", confirmReplace: "Test and replace", working: "Working…",
  freeBadge: "Free", localBadge: "Local", costUnknown: "Cost unknown", visionBadge: "Vision",
  manualUnverified: "Manually added models remain unverified until tested.",
  ollamaGuide: "OpenThesis only connects to an Ollama installation you already run. It never installs Ollama or downloads models.",
  status: "Status",
};

type ModelCenterText = { [Key in keyof typeof EN]: string };

const ZH: ModelCenterText = {
  intro: "先配置模型连接，再在研究和 OT 创作工作室中按引用选择。密钥只写入 Windows 凭据管理器，页面无法读取旧密钥明文。",
  search: "搜索服务商", all: "全部", cloud: "云端", local: "本地", free: "免费路径",
  connection: "个连接", connections: "个连接", ready: "可用", untested: "未测试", disabled: "已禁用", unauthorized: "未授权", rateLimited: "已限流", unavailable: "不可用",
  configure: "配置服务商", close: "关闭", registration: "注册与获取密钥", addConnection: "添加连接",
  displayName: "连接名称", endpoint: "接口地址", region: "区域", apiKey: "API Key",
  apiKeyHint: "保存后只显示“已配置”，不会回显明文。", save: "保存连接", saving: "正在保存…",
  customConfirm: "我确认密钥和研究内容将发送到上方完整 Origin。",
  localPrivacy: "本地 Ollama：研究内容留在这台电脑上的 Ollama 服务中。",
  remotePrivacy: "远程 Ollama：研究内容会发送到该远程地址，且不会标记为本地。",
  existing: "已有连接", enable: "启用", disable: "禁用", replaceKey: "替换密钥", delete: "删除",
  models: "已配置模型", discover: "发现模型", discovering: "正在发现…", testConnection: "测试连接", testingConnection: "正在测试连接…", testUsing: "测试模型", advancedModels: "高级：自定义模型 ID", manualModel: "手动模型 ID",
  addManual: "加入候选", selectModels: "选择一个或多个模型", addModels: "添加所选模型",
  selectModel: "选择模型", visionCapability: "视觉输入", visionCapabilityHint: "仅当该具体模型支持图像输入时开启。测试会同时验证文本与视觉请求，全部通过后才会出现在研究页。",
  testing: "正在测试…", test: "测试", noConnections: "还没有连接。先添加一个账户或本地 Ollama。",
  noModels: "还没有已配置模型。", noProviders: "没有匹配的服务商。",
  credentialSaved: "凭据已安全保存。", connectionSaved: "连接已保存。", modelsSaved: "模型已添加。", testSucceeded: "连接测试成功。",
  rotateTitle: "替换已保存的 API Key", rotateBody: "新密钥会先经过连接测试，再切换为当前密钥；测试失败时旧密钥保持不变。",
  deleteConnectionTitle: "删除这个连接？", confirmDelete: "此操作会永久删除连接、其中的已配置模型和 Windows 系统凭据。",
  deleteModelTitle: "删除这个已配置模型？", confirmDeleteModel: "连接及其系统凭据仍会保留。",
  cancel: "取消", confirm: "确认", confirmReplace: "测试并替换", working: "处理中…",
  freeBadge: "Free", localBadge: "Local", costUnknown: "费用未知", visionBadge: "视觉",
  manualUnverified: "手动添加的模型在测试前标记为未验证。",
  ollamaGuide: "OpenThesis 只连接你已经安装并启动的 Ollama，不会安装 Ollama，也不会下载模型。",
  status: "状态",
};

const ZH_HANT: ModelCenterText = {
  ...ZH,
  intro: "先設定模型連線，再在研究與 OT 創作工作室中按引用選擇。金鑰只寫入 Windows 認證管理員，頁面無法讀取舊金鑰明文。",
  search: "搜尋服務商", local: "本機", configure: "設定服務商", addConnection: "新增連線",
  displayName: "連線名稱", endpoint: "介面位址", apiKeyHint: "儲存後只顯示「已設定」，不會回顯明文。",
  save: "儲存連線", saving: "正在儲存…", existing: "現有連線", enable: "啟用", disable: "停用",
  replaceKey: "替換金鑰", models: "已設定模型", discover: "探索模型", discovering: "正在探索…",
  selectModels: "選擇一個或多個模型", addModels: "新增所選模型", noConnections: "尚未建立連線。",
  noModels: "尚未設定模型。", credentialSaved: "憑據已安全儲存。", connectionSaved: "連線已儲存。",
  modelsSaved: "模型已新增。", testSucceeded: "連線測試成功。", rotateTitle: "替換已儲存的 API Key",
  rotateBody: "新金鑰會先經過連線測試，再切換為目前金鑰；測試失敗時舊金鑰保持不變。",
  deleteConnectionTitle: "刪除這個連線？", confirmDelete: "此操作會永久刪除連線、其中已設定的模型和 Windows 系統憑據。",
  deleteModelTitle: "刪除這個已設定模型？", confirmDeleteModel: "連線及其系統憑據仍會保留。",
  cancel: "取消", confirm: "確認", confirmReplace: "測試並替換", working: "處理中…",
  costUnknown: "費用未知", status: "狀態",
};

function errorMessage(error: unknown): string {
  if (typeof error === "string") return error;
  if (error && typeof error === "object" && "message" in error) return String(error.message);
  return "Unexpected model-center error.";
}

function mark(provider: ProviderDefinition): string {
  const value = provider.display_name.replace(/[^A-Za-z0-9 ]/g, " ").split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("");
  return (value || provider.provider_id.slice(0, 2)).toUpperCase();
}


const PROVIDER_LOGOS: Record<string, string> = {
  deepseek: deepseekLogo,
  qwen: qwenLogo,
  kimi_cn: kimiLogo,
  kimi_global: kimiLogo,
  glm: glmLogo,
  openai: openaiLogo,
  gemini: geminiLogo,
  openrouter: openrouterLogo,
  ollama: ollamaLogo,
};

function ProviderLogo({ provider }: { provider: ProviderDefinition }) {
  const [failed, setFailed] = useState(false);
  const source = PROVIDER_LOGOS[provider.provider_id];
  return <span
    className="provider-logo"
    data-provider={provider.provider_id}
    role="img"
    aria-label={`${provider.display_name} logo`}
  >
    {source && !failed
      ? <img src={source} alt="" onError={() => setFailed(true)} />
      : provider.provider_id === "custom"
        ? <Cable size={25} aria-hidden="true" />
        : <span aria-hidden="true">{mark(provider)}</span>}
  </span>;
}
function modelReferenceId(connectionId: string, modelId: string): string {
  const slug = modelId.toLowerCase().replace(/[^a-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 54) || "model";
  return [connectionId, slug, Math.random().toString(36).slice(2, 8)].join(".").slice(0, 128);
}

function presetModels(provider: ProviderDefinition): DiscoveredModel[] {
  return (provider.recommended_models ?? []).map((model) => ({ model_id: model.model_id, alias: model.alias, billing_class: model.billing_class, capabilities: model.capabilities }));
}

function statusLabel(connection: ProviderConnectionSummary, text: ModelCenterText): string {
  if (!connection.enabled) return text.disabled;
  switch (connection.status) {
    case "ready": return text.ready;
    case "unauthorized": return text.unauthorized;
    case "rate_limited": return text.rateLimited;
    case "disabled": return text.disabled;
    case "unavailable": return text.unavailable;
    default: return text.untested;
  }
}

function costLabel(model: ConfiguredModelSummary | DiscoveredModel, text: ModelCenterText): string {
  const billing = "billing_class" in model ? model.billing_class : undefined;
  if (billing === "local_no_provider_fee") return text.localBadge + " · " + text.freeBadge;
  if (billing === "free_tier" || ("free_tier" in model && model.free_tier)) return text.freeBadge;
  return text.costUnknown;
}

export function ModelCenterView({ language }: { language: Language }) {
  const text = language === "en" ? EN : language === "zh-Hant" ? ZH_HANT : ZH;
  const [providers, setProviders] = useState<ProviderDefinition[]>([]);
  const [connections, setConnections] = useState<ProviderConnectionSummary[]>([]);
  const [models, setModels] = useState<ConfiguredModelSummary[]>([]);
  const [providerId, setProviderId] = useState<string | null>(null);
  const [connectionId, setConnectionId] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [showForm, setShowForm] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("");
  const [secret, setSecret] = useState("");
  const [originConfirmed, setOriginConfirmed] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredModel[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [visionModelIds, setVisionModelIds] = useState<Set<string>>(new Set());
  const [manualModel, setManualModel] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [dialogSecret, setDialogSecret] = useState("");
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);

  const openDialog = (next: DialogState, trigger?: HTMLElement) => {
    returnFocusRef.current = trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    setDialogSecret("");
    setDialog(next);
  };
  const dismissDialog = () => {
    setDialog(null);
    setDialogSecret("");
    window.setTimeout(() => returnFocusRef.current?.focus(), 0);
  };

  useEffect(() => {
    if (!dialog) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy.startsWith("dialog:")) {
        event.preventDefault();
        dismissDialog();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])") ?? [])];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [busy, dialog]);

  const reload = useCallback(async () => {
    const result = await Promise.all([listModelProviders(), listProviderConnections(), listConfiguredModels()]);
    setProviders(result[0]); setConnections(result[1]); setModels(result[2]);
  }, []);

  useEffect(() => { void reload().catch((reason) => setError(errorMessage(reason))); }, [reload]);

  const provider = providers.find((item) => item.provider_id === providerId);
  const providerConnections = connections.filter((item) => item.provider_id === providerId);
  const activeConnection = providerConnections.find((item) => item.connection_id === connectionId);
  const connectionModels = models.filter((item) => item.connection_id === connectionId);
  const visibleProviders = useMemo(() => {
    const search = query.trim().toLowerCase();
    return providers.filter((item) => {
      const matchesSearch = !search || (item.display_name + " " + item.provider_id).toLowerCase().includes(search);
      const matchesFilter = filter === "all" || (filter === "free" ? item.free_hint : item.category === filter);
      return matchesSearch && matchesFilter;
    });
  }, [filter, providers, query]);

  const chooseProvider = (item: ProviderDefinition) => {
    const first = connections.find((connection) => connection.provider_id === item.provider_id);
    setProviderId(item.provider_id); setConnectionId(first?.connection_id || "");
    setDisplayName(item.display_name); setEndpoint(item.base_url); setRegion(item.region);
    setSecret(""); setDiscovered(first ? presetModels(item) : []); setSelectedModels(new Set()); setVisionModelIds(new Set()); setShowForm(!first);
    setOriginConfirmed(false); setError(""); setNotice("");
  };

  const saveConnection = async () => {
    if (!provider) return;
    const target = endpoint.trim();
    const loopback = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::|\/|$)/i.test(target);
    if (provider.provider_id === "custom" && !loopback && !originConfirmed) { setError(text.customConfirm); return; }
    if (provider.requires_api_key && !secret.trim()) { setError(text.apiKey); return; }
    setBusy("save-connection"); setError(""); setNotice("");
    try {
      const saved = await saveProviderConnection({
        connection_id: provider.provider_id + "-" + Date.now().toString(36),
        provider_id: provider.provider_id,
        display_name: displayName.trim() || provider.display_name,
        region: region.trim() || provider.region,
        endpoint: target,
        enabled: true,
      });
      const hasNewSecret = Boolean(secret.trim());
      if (hasNewSecret) await setProviderConnectionSecret(saved.connection_id, secret.trim());
      setSecret(""); setConnectionId(saved.connection_id); setDiscovered(presetModels(provider)); setShowForm(false);
      setNotice(hasNewSecret ? text.credentialSaved : text.connectionSaved);
      await reload();
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(""); }
  };

  const discover = async () => {
    if (!connectionId) return;
    setBusy("discover"); setError(""); setNotice("");
    try {
      const result = await discoverConnectionModels(connectionId);
      const builtins = provider ? presetModels(provider) : [];
      const merged = [...builtins, ...result].filter((item, index, all) => all.findIndex((candidate) => candidate.model_id === item.model_id) === index);
      setDiscovered(merged);
      setSelectedModels(new Set());
      setVisionModelIds(new Set(result.filter((item) => item.capabilities.includes("vision")).map((item) => item.model_id)));
    }
    catch (reason) { setError(errorMessage(reason)); setDiscovered(provider ? presetModels(provider) : []); }
    finally { setBusy(""); }
  };

  const addManual = () => {
    const value = manualModel.trim(); if (!value) return;
    if (!discovered.some((item) => item.model_id === value)) {
      setDiscovered((items) => items.concat({
        model_id: value, alias: value,
        billing_class: providerId === "ollama" ? "local_no_provider_fee" : "unknown",
        capabilities: ["text_chat", "structured_json"],
      }));
    }
    setSelectedModels((items) => new Set(items).add(value)); setManualModel("");
  };

  const addModels = async () => {
    if (!connectionId || !selectedModels.size) return;
    setBusy("save-models"); setError("");
    try {
      for (const modelId of selectedModels) {
        const candidate = discovered.find((item) => item.model_id === modelId);
        const capabilities = new Set(candidate?.capabilities || ["text_chat", "structured_json"]);
        if (visionModelIds.has(modelId)) capabilities.add("vision");
        else capabilities.delete("vision");
        await saveConfiguredModel({
          configured_model_id: modelReferenceId(connectionId, modelId), connection_id: connectionId,
          model_id: modelId, alias: candidate?.alias || modelId,
          enabled: true, capabilities: [...capabilities],
        });
      }
      setSelectedModels(new Set()); setNotice(text.modelsSaved); await reload();
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(""); }
  };

  const replaceKey = (connection: ProviderConnectionSummary, trigger: HTMLElement) => {
    openDialog({ kind: "rotate", connection }, trigger);
  };

  const toggleConnection = async (connection: ProviderConnectionSummary) => {
    setBusy("connection:" + connection.connection_id);
    try { await setProviderConnectionEnabled(connection.connection_id, !connection.enabled); await reload(); }
    catch (reason) { setError(errorMessage(reason)); } finally { setBusy(""); }
  };

  const removeConnection = (connection: ProviderConnectionSummary, trigger: HTMLElement) => {
    openDialog({ kind: "delete-connection", connection }, trigger);
  };

  const connectionTestModelId = (connection: ProviderConnectionSummary): string | undefined => {
    const configured = models.find((item) => item.connection_id === connection.connection_id && item.enabled)?.model_id;
    if (configured) return configured;
    if (connection.connection_id !== connectionId) return undefined;
    if (provider?.provider_id === "ollama") return manualModel.trim() || undefined;
    return Array.from(selectedModels)[0] || discovered[0]?.model_id || manualModel.trim() || undefined;
  };

  const testConnection = async (connection: ProviderConnectionSummary) => {
    setBusy("test-connection:" + connection.connection_id); setError(""); setNotice("");
    try {
      const modelId = connectionTestModelId(connection);
      const result = provider?.default_test_model_id
        ? await testProviderConnection(connection.connection_id)
        : modelId
          ? await testProviderConnection(connection.connection_id, modelId)
          : await testProviderConnection(connection.connection_id);
      setNotice(result.message);
      await reload();
    } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(""); }
  };

  const testModel = async (model: ConfiguredModelSummary) => {
    setBusy("test:" + model.configured_model_id); setError("");
    try { await testConfiguredModel(model.configured_model_id); setNotice(text.testSucceeded); await reload(); }
    catch (reason) { setError(errorMessage(reason)); } finally { setBusy(""); }
  };

  const removeModel = (model: ConfiguredModelSummary, trigger: HTMLElement) => {
    openDialog({ kind: "delete-model", model }, trigger);
  };

  const confirmDialog = async () => {
    if (!dialog) return;
    if (dialog.kind === "rotate" && !dialogSecret.trim()) return;
    const current = dialog;
    setBusy("dialog:" + current.kind); setError(""); setNotice("");
    try {
      if (current.kind === "rotate") {
        await rotateProviderConnectionSecret(current.connection.connection_id, dialogSecret.trim());
        setNotice(text.credentialSaved);
      } else if (current.kind === "delete-connection") {
        await deleteProviderConnection(current.connection.connection_id);
        if (connectionId === current.connection.connection_id) setConnectionId("");
      } else {
        await deleteConfiguredModel(current.model.configured_model_id);
      }
      await reload();
      dismissDialog();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  };

  const dialogTitle = dialog?.kind === "rotate"
    ? text.rotateTitle
    : dialog?.kind === "delete-connection" ? text.deleteConnectionTitle : text.deleteModelTitle;
  const dialogBody = dialog?.kind === "rotate"
    ? text.rotateBody
    : dialog?.kind === "delete-connection" ? text.confirmDelete : text.confirmDeleteModel;
  const dialogTarget = dialog?.kind === "delete-model"
    ? dialog.model.alias
    : dialog?.connection.display_name;

  return <div className="model-center">
    <header className="model-center-intro"><ShieldCheck size={22} aria-hidden="true" /><p>{text.intro}</p></header>
    <div className="model-center-tools">
      <label className="model-search"><Search size={16} aria-hidden="true" /><input aria-label={text.search} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text.search} /></label>
      <div className="model-filters" role="group" aria-label={text.search}>
        {(["all", "cloud", "local", "free"] as Filter[]).map((item) => <button key={item} type="button" data-active={filter === item || undefined} onClick={() => setFilter(item)}>{text[item]}</button>)}
      </div>
    </div>
    {error && <div className="model-center-message" data-tone="error" role="alert">{error}</div>}
    {notice && <div className="model-center-message" role="status"><Check size={15} />{notice}</div>}

    <div className="model-center-layout" data-panel-open={Boolean(provider) || undefined}>
      <section className="provider-grid" aria-label={text.configure}>
        {visibleProviders.map((item) => {
          const count = connections.filter((connection) => connection.provider_id === item.provider_id).length;
          const ready = connections.filter((connection) => connection.provider_id === item.provider_id && connection.status === "ready").length;
          return <button className="provider-card" type="button" key={item.provider_id} data-selected={item.provider_id === providerId || undefined} onClick={() => chooseProvider(item)}>
            <ProviderLogo provider={item} /><strong>{item.display_name}</strong>
            <span>{count} {count === 1 ? text.connection : text.connections}{ready ? " · " + ready + " " + text.ready : ""}</span>
            {item.free_hint && <em className="free-badge">{text.freeBadge}</em>}
          </button>;
        })}
        {!visibleProviders.length && <p className="model-empty">{text.noProviders}</p>}
      </section>

      {provider && <aside className="provider-panel" aria-label={text.configure + ": " + provider.display_name}>
        <header><ProviderLogo provider={provider} /><div><h2>{provider.display_name}</h2><p>{provider.region}</p></div><button className="model-icon-button" type="button" aria-label={text.close} onClick={() => setProviderId(null)}><X size={18} /></button></header>
        {provider.help_url && <button className="text-link" type="button" onClick={() => void openExternalUrl(provider.help_url)}>{text.registration}<ExternalLink size={14} /></button>}
        {provider.provider_id === "ollama" && <p className="provider-guidance">{text.ollamaGuide}</p>}

        <div className="panel-section-title"><h3>{text.existing}</h3><button type="button" onClick={() => setShowForm(true)}><Plus size={14} />{text.addConnection}</button></div>
        {!providerConnections.length && !showForm && <p className="model-empty">{text.noConnections}</p>}
        <div className="connection-list">
          {providerConnections.map((connection) => <article key={connection.connection_id} className="connection-card" data-active={connectionId === connection.connection_id || undefined}>
            <button className="connection-main" type="button" onClick={() => { setConnectionId(connection.connection_id); setDiscovered(provider ? presetModels(provider) : []); setSelectedModels(new Set()); setVisionModelIds(new Set()); }}>
              <strong>{connection.display_name}</strong><span>{connection.endpoint}</span>
              <small><span className="connection-status" data-status={connection.enabled ? connection.status || "untested" : "disabled"}>{statusLabel(connection, text)}</span>{connection.has_secret && <><KeyRound size={12} />{text.apiKey}</>}</small>
            </button>
            <div className="connection-actions">
              <button type="button" title={`${text.testUsing}: ${provider.default_test_model_id || connectionTestModelId(connection) || text.manualModel}`} aria-label={`${text.testConnection} · ${text.testUsing}: ${provider.default_test_model_id || connectionTestModelId(connection) || text.manualModel}`} disabled={Boolean(busy) || !connection.enabled || (!provider.default_test_model_id && provider.provider_id !== "ollama" && !connectionTestModelId(connection))} onClick={() => void testConnection(connection)}>{busy === "test-connection:" + connection.connection_id ? text.testingConnection : text.testConnection}</button>
              {provider.requires_api_key && <button type="button" disabled={Boolean(busy)} onClick={(event) => replaceKey(connection, event.currentTarget)}>{text.replaceKey}</button>}
              <button type="button" disabled={Boolean(busy)} onClick={() => void toggleConnection(connection)}>{connection.enabled ? text.disable : text.enable}</button>
              <button type="button" disabled={Boolean(busy)} aria-label={text.delete} onClick={(event) => removeConnection(connection, event.currentTarget)}><Trash2 size={14} /></button>
            </div>
          </article>)}
        </div>

        {showForm && <fieldset className="connection-form" disabled={busy === "save-connection"}>
          <legend>{text.addConnection}</legend>
          <label>{text.displayName}<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
          <label>{text.endpoint}<input value={endpoint} onChange={(event) => { setEndpoint(event.target.value); setOriginConfirmed(false); }} spellCheck={false} /></label>
          <label>{text.region}<input value={region} onChange={(event) => setRegion(event.target.value)} /></label>
          {provider.requires_api_key && <label>{text.apiKey}<input type="password" value={secret} onChange={(event) => setSecret(event.target.value)} autoComplete="new-password" /><small>{text.apiKeyHint}</small></label>}
          {provider.provider_id === "custom" && endpoint.trim() && !/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::|\/|$)/i.test(endpoint.trim()) && <label className="model-check"><input type="checkbox" checked={originConfirmed} onChange={(event) => setOriginConfirmed(event.target.checked)} />{text.customConfirm}</label>}
          {provider.provider_id === "ollama" && <p className="privacy-note">{/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::|\/|$)/i.test(endpoint.trim()) ? text.localPrivacy : text.remotePrivacy}</p>}
          <div className="form-actions"><button type="button" onClick={() => setShowForm(false)}>{text.close}</button><button className="primary-action" type="button" onClick={() => void saveConnection()}>{busy === "save-connection" ? text.saving : text.save}</button></div>
        </fieldset>}

        {activeConnection && <section className="connection-models">
          <div className="panel-section-title"><h3>{text.models}</h3>{provider.supports_discovery && <button type="button" disabled={Boolean(busy)} onClick={() => void discover()}><RefreshCw size={14} />{busy === "discover" ? text.discovering : text.discover}</button>}</div>
          <p className="connection-test-model">{text.testUsing}: {provider.default_test_model_id || text.manualModel}</p>
          <details className="advanced-model-entry"><summary>{text.advancedModels}</summary>
            <div className="manual-model-row"><label>{text.manualModel}<input value={manualModel} onChange={(event) => setManualModel(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addManual(); } }} /></label><button type="button" onClick={addManual}>{text.addManual}</button></div>
            <p className="field-caption">{text.manualUnverified}</p>
          </details>
          {discovered.length > 0 && <fieldset className="discovered-models"><legend>{text.selectModels}</legend>
            <p className="field-caption">{text.visionCapabilityHint}</p>
            {discovered.map((model) => <div className="discovered-model-option" key={model.model_id}>
              <label className="discovered-model-select"><input aria-label={`${text.selectModel}: ${model.alias}`} type="checkbox" checked={selectedModels.has(model.model_id)} onChange={(event) => setSelectedModels((current) => { const next = new Set(current); if (event.target.checked) next.add(model.model_id); else next.delete(model.model_id); return next; })} /><span><strong>{model.alias}</strong><small>{model.model_id} · {costLabel(model, text)}</small></span></label>
              <label className="model-capability-check"><input aria-label={`${text.visionCapability}: ${model.alias}`} type="checkbox" checked={visionModelIds.has(model.model_id)} onChange={(event) => setVisionModelIds((current) => { const next = new Set(current); if (event.target.checked) next.add(model.model_id); else next.delete(model.model_id); return next; })} /><span>{text.visionCapability}</span></label>
            </div>)}
            <button className="primary-action" type="button" disabled={!selectedModels.size || Boolean(busy)} onClick={() => void addModels()}>{text.addModels}</button>
          </fieldset>}
          <div className="configured-model-list">
            {connectionModels.map((model) => <article key={model.configured_model_id}><div><strong>{model.alias}</strong><span>{model.model_id}</span><small>{costLabel(model, text)} · {text.status}: {model.health_status || text.untested}{model.capabilities.includes("vision") ? ` · ${text.visionBadge}` : ""}</small></div><div><button type="button" disabled={Boolean(busy)} onClick={() => void testModel(model)}>{busy === "test:" + model.configured_model_id ? text.testing : text.test}</button><button type="button" aria-label={text.delete} disabled={Boolean(busy)} onClick={(event) => removeModel(model, event.currentTarget)}><Trash2 size={14} /></button></div></article>)}
            {!connectionModels.length && <p className="model-empty">{text.noModels}</p>}
          </div>
        </section>}
      </aside>}
    </div>

    {dialog && <div className="history-dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy.startsWith("dialog:")) dismissDialog();
    }}>
      <section ref={dialogRef} className="history-dialog model-center-dialog" role="dialog" aria-modal="true" aria-labelledby="model-dialog-title" aria-describedby="model-dialog-description">
        <div className="model-dialog-header">
          <div>
            <small>{dialogTarget}</small>
            <h2 id="model-dialog-title">{dialogTitle}</h2>
          </div>
          <button className="model-icon-button" type="button" aria-label={text.close} disabled={busy.startsWith("dialog:")} onClick={dismissDialog}><X size={18} /></button>
        </div>
        <p id="model-dialog-description">{dialogBody}</p>
        {dialog.kind === "rotate" && <label className="model-dialog-secret">{text.apiKey}
          <input autoFocus aria-label={text.apiKey} type="password" value={dialogSecret} onChange={(event) => setDialogSecret(event.target.value)} autoComplete="new-password" />
          <small>{text.apiKeyHint}</small>
        </label>}
        <div className="history-dialog-actions">
          <button autoFocus={dialog.kind !== "rotate"} className="secondary-button" type="button" disabled={busy.startsWith("dialog:")} onClick={dismissDialog}>{text.cancel}</button>
          <button

            className={dialog.kind === "rotate" ? "primary-button" : "danger-button"}
            type="button"
            disabled={busy.startsWith("dialog:") || (dialog.kind === "rotate" && !dialogSecret.trim())}
            onClick={() => void confirmDialog()}
          >
            {busy.startsWith("dialog:") ? text.working : dialog.kind === "rotate" ? text.confirmReplace : text.confirm}
          </button>
        </div>
      </section>
    </div>}
  </div>;
}



