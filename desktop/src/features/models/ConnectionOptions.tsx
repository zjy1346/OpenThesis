import type { ConnectionCapabilityProfile, ConnectionTransportSettings, Language } from "../../types";

export const UNKNOWN_PROFILE: ConnectionCapabilityProfile = {
  source: "legacy",
  json_output: "prompt_json",
  stream: "none",
  include_usage: false,
  usage: "unknown",
  output_parameter: "unknown",
  output_semantics: "unknown",
  output_limit: null,
  allow_unbounded_output: false,
  upstream: "unknown",
};

export function compatibleDefaultProfile(protocol: "chat_completions" | "ollama"): ConnectionCapabilityProfile {
  return {
    ...UNKNOWN_PROFILE,
    source: "compatible_default",
    stream: protocol === "ollama" ? "ndjson" : "sse",
    output_parameter: protocol === "ollama" ? "num_predict" : "max_tokens",
    output_semantics: "visible_output_tokens_only",
    output_limit: null,
    allow_unbounded_output: false,
    include_usage: false,
    usage: "unknown",
    upstream: "unknown",
  };
}

export const COMPATIBLE_DEFAULT_PROFILE: ConnectionCapabilityProfile = compatibleDefaultProfile("chat_completions");

export const DEFAULT_TRANSPORT: ConnectionTransportSettings = {
  endpoint_mode: "base",
  models_endpoint: null,
  network_mode: "system",
  proxy_url: null,
};

export function normalizeConnectionEndpoint(
  endpoint: string,
  protocol: string,
  endpointMode: ConnectionTransportSettings["endpoint_mode"] = "base",
): { endpoint: string; endpoint_mode: ConnectionTransportSettings["endpoint_mode"]; request_url: string } | null {
  try {
    const url = new URL(endpoint.trim());
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    const normalizedPath = url.pathname.replace(/\/+$/, "");
    if (endpointMode === "full_request") {
      if (!normalizedPath) return null;
      url.pathname = normalizedPath;
      const request = url.toString().replace(/\/+$/, "");
      return { endpoint: request, endpoint_mode: "full_request", request_url: request };
    }

    const suffix = protocol === "ollama" ? "/api/chat" : "/chat/completions";
    let basePath = normalizedPath;
    if (basePath.endsWith(suffix)) basePath = basePath.slice(0, -suffix.length);
    if (protocol !== "ollama" && !basePath) basePath = "/v1";
    url.pathname = basePath;
    const base = url.toString().replace(/\/+$/, "");
    return { endpoint: base, endpoint_mode: "base", request_url: base + suffix };
  } catch {
    return null;
  }
}

export function connectionUrl(
  endpoint: string,
  protocol: string,
  endpointMode: ConnectionTransportSettings["endpoint_mode"] = "base",
): string | null {
  return normalizeConnectionEndpoint(endpoint, protocol, endpointMode)?.request_url ?? null;
}

export function isSameOriginModelsEndpoint(value: string | null | undefined, requestUrl: string): boolean {
  const override = value?.trim() ?? "";
  if (!override) return true;
  try {
    const base = new URL(requestUrl);
    const candidate = /^https?:\/\//i.test(override)
      ? new URL(override)
      : override.startsWith("/") && !override.startsWith("//")
        ? new URL(override, base.origin)
        : null;
    return Boolean(candidate && ["http:", "https:"].includes(candidate.protocol) && candidate.origin === base.origin && !candidate.username && !candidate.password);
  } catch {
    return false;
  }
}

const TEXT = {
  en: {
    requestAddress: "Request address",
    endpointMode: "Request address format",
    baseAddress: "Base URL (append chat/completions)",
    fullAddress: "Full request URL",
    modelsEndpoint: "Model list address override",
    sameOrigin: "The override must use the same origin; a different service requires a separate connection so the API key is not sent to another host.",
    network: "Network route",
    system: "Follow system proxy settings",
    direct: "Connect directly",
    proxy: "Use an HTTP(S) proxy",
    proxyUrl: "Proxy URL",
    systemHint: "Uses the Windows static system proxy and supported environment settings. PAC scripts are not executed; configure an explicit proxy if your system uses PAC.",
    protocol: "Interface type",
    chat: "Chat Completions compatible",
    native: "Ollama native",
    advanced: "Capability and budget policy",
    hint: "Declarations are not verification. Responses-only, Anthropic Messages and special Azure authentication require a compatible proxy. No automatic protocol switching.",
    json: "JSON output",
    prompt: "Prompted JSON + local parsing (default)",
    mode: "API JSON mode (declared support)",
    stream: "Stream framing",
    none: "Off / unknown",
    usage: "Usage contract",
    output: "Output limit parameter",
    semantics: "Limit semantics",
    total: "Total generation including reasoning",
    visible: "Visible output only; not a total cost cap",
    unknown: "Unknown",
    cap: "Requested output limit",
    policy: "I accept that this endpoint has no verified hard output/cost cap.",
    upstream: "Execution route",
    directRoute: "Direct provider",
    proxyRoute: "External proxy",
    proxyHint: "OpenThesis measures requests to the endpoint only. Disable proxy retries/fallbacks where possible; upstream billable requests and cache are not independently verified.",
    usageHint: "Returned usage is endpoint-reported; missing counters remain unknown.",
    include: "Request usage in SSE",
    preview: "Generation request",
    destination: "Research destination",
    compatibleDefault: "Use compatible defaults",
    compatibleDefaultHint: "SSE, prompted JSON with local parsing, and a max_tokens request limit. These are request defaults, not a verified provider contract.",
    source: "Contract source",
    officialSource: "Official preset",
    userSource: "User override",
    legacySource: "Legacy setting",
  },
  zh: {
    requestAddress: "请求地址",
    endpointMode: "请求地址格式",
    baseAddress: "基础 URL（自动补全 chat/completions）",
    fullAddress: "完整请求 URL",
    modelsEndpoint: "模型列表地址覆盖",
    sameOrigin: "覆盖地址必须与接口同源；其他服务需另建连接，避免将 API Key 发送到其他主机。",
    network: "网络路径",
    system: "跟随系统代理设置",
    direct: "直连",
    proxy: "使用 HTTP(S) 代理",
    proxyUrl: "代理 URL",
    systemHint: "使用 Windows 静态系统代理及受支持的环境设置；不会执行 PAC 脚本。如系统使用 PAC，请填写明确的代理地址。",
    protocol: "接口类型",
    chat: "Chat Completions 兼容接口",
    native: "Ollama 原生接口",
    advanced: "能力与预算策略",
    hint: "声明不等于验证。仅支持 Responses、Anthropic Messages 或特殊 Azure 鉴权的服务需提供兼容代理地址，不会自动切换协议。",
    json: "JSON 输出方式",
    prompt: "提示词 JSON＋本地解析（默认）",
    mode: "API JSON 模式（已声明支持）",
    stream: "流式协议",
    none: "关闭／未知",
    usage: "用量计量契约",
    output: "输出上限参数",
    semantics: "上限覆盖范围",
    total: "包括推理的全部生成",
    visible: "仅可见输出，不是总费用硬上限",
    unknown: "未知",
    cap: "请求输出上限",
    policy: "我理解该接口没有经验证的输出／费用硬上限。",
    upstream: "执行路由",
    directRoute: "直连供应商",
    proxyRoute: "外部代理",
    proxyHint: "只统计到目标接口的请求；建议关闭代理内部重试和跨模型回退，上游计费调用与缓存未经独立核实。",
    usageHint: "用量来自接口返回；缺失计数保持未知。",
    include: "请求 SSE 用量回执",
    preview: "生成请求地址",
    destination: "研究内容发送目标",
    compatibleDefault: "使用兼容默认值",
    compatibleDefaultHint: "SSE 流式、提示词 JSON＋本地解析、max_tokens 请求上限。这些是请求默认值，不代表服务商契约已验证。",
    source: "契约来源",
    officialSource: "官方预设",
    userSource: "用户自定义",
    legacySource: "旧版设置",
  },
  "zh-Hant": {
    requestAddress: "請求位址",
    endpointMode: "請求位址格式",
    baseAddress: "基礎 URL（自動補全 chat/completions）",
    fullAddress: "完整請求 URL",
    modelsEndpoint: "模型清單位址覆寫",
    sameOrigin: "覆寫位址必須與介面同源；其他服務需另建連線，避免將 API Key 傳送至其他主機。",
    network: "網路路徑",
    system: "跟隨系統代理設定",
    direct: "直連",
    proxy: "使用 HTTP(S) 代理",
    proxyUrl: "代理 URL",
    systemHint: "使用 Windows 靜態系統代理及受支援的環境設定；不會執行 PAC 指令碼。如系統使用 PAC，請填寫明確的代理位址。",
    protocol: "介面類型",
    chat: "Chat Completions 相容介面",
    native: "Ollama 原生介面",
    advanced: "能力與預算策略",
    hint: "宣告不等於驗證。僅支援 Responses、Anthropic Messages 或特殊 Azure 驗證的服務需提供相容代理位址，不會自動切換協定。",
    json: "JSON 輸出方式",
    prompt: "提示詞 JSON＋本機解析（預設）",
    mode: "API JSON 模式（已宣告支援）",
    stream: "串流協定",
    none: "關閉／未知",
    usage: "用量計量契約",
    output: "輸出上限參數",
    semantics: "上限涵蓋範圍",
    total: "包括推理的全部生成",
    visible: "僅可見輸出，並非總費用硬上限",
    unknown: "未知",
    cap: "請求輸出上限",
    policy: "我理解此介面沒有經驗證的輸出／費用硬上限。",
    upstream: "執行路由",
    directRoute: "直連供應商",
    proxyRoute: "外部代理",
    proxyHint: "僅統計到目標介面的請求；建議關閉代理內部重試與跨模型回退，上游計費呼叫與快取未經獨立核實。",
    usageHint: "用量來自介面回傳；缺失計數維持未知。",
    include: "請求 SSE 用量回執",
    preview: "生成請求位址",
    destination: "研究內容傳送目標",
    compatibleDefault: "使用相容預設值",
    compatibleDefaultHint: "SSE 串流、提示詞 JSON＋本機解析、max_tokens 請求上限。這些是請求預設值，不代表服務商契約已驗證。",
    source: "契約來源",
    officialSource: "官方預設",
    userSource: "使用者自訂",
    legacySource: "舊版設定",
  },
} as const;

export function ConnectionOptions({ language, endpoint, protocol, profile, transport, protocolLocked = false, onProtocol, onChange, onTransport, onCompatibleDefault }: {
  language: Language;
  endpoint: string;
  protocol: "chat_completions" | "ollama";
  profile: ConnectionCapabilityProfile;
  transport: ConnectionTransportSettings;
  protocolLocked?: boolean;
  onProtocol: (value: "chat_completions" | "ollama") => void;
  onChange: (value: ConnectionCapabilityProfile) => void;
  onTransport: (value: ConnectionTransportSettings) => void;
  onCompatibleDefault: () => void;
}) {
  const text = language === "en" ? TEXT.en : language === "zh-Hant" ? TEXT["zh-Hant"] : TEXT.zh;
  const destination = connectionUrl(endpoint, protocol, transport.endpoint_mode);
  const sourceLabel = profile.source === "official" ? text.officialSource : profile.source === "user" ? text.userSource : profile.source === "compatible_default" ? text.compatibleDefault : text.legacySource;
  const change = <K extends keyof ConnectionCapabilityProfile>(key: K, value: ConnectionCapabilityProfile[K]) => onChange({ ...profile, source: "user", [key]: value });
  const updateTransport = <K extends keyof ConnectionTransportSettings>(key: K, value: ConnectionTransportSettings[K]) => onTransport({ ...transport, [key]: value });
  const listAddress = transport.models_endpoint?.trim() ?? "";
  const listAddressError = listAddress && destination && !isSameOriginModelsEndpoint(listAddress, destination) ? text.sameOrigin : "";

  return <div className="connection-options">
    <details className="connection-transport-advanced">
      <summary>{text.requestAddress}</summary>
      <div className="connection-options-grid">
        <label>{text.endpointMode}<select value={transport.endpoint_mode} onChange={event => updateTransport("endpoint_mode", event.target.value as ConnectionTransportSettings["endpoint_mode"])}>
          <option value="base">{text.baseAddress}</option><option value="full_request">{text.fullAddress}</option>
        </select></label>
        <label>{text.modelsEndpoint}<input value={transport.models_endpoint ?? ""} onChange={event => updateTransport("models_endpoint", event.target.value.trim() ? event.target.value : null)} placeholder="/models" spellCheck={false} /><small>{listAddressError || text.sameOrigin}</small></label>
        <label>{text.network}<select value={transport.network_mode} onChange={event => updateTransport("network_mode", event.target.value as ConnectionTransportSettings["network_mode"])}>
          <option value="system">{text.system}</option><option value="direct">{text.direct}</option><option value="proxy">{text.proxy}</option>
        </select><small>{text.systemHint}</small></label>
        {transport.network_mode === "proxy" && <label>{text.proxyUrl}<input value={transport.proxy_url ?? ""} onChange={event => updateTransport("proxy_url", event.target.value.trim() ? event.target.value : null)} placeholder="http://proxy.example:8080" spellCheck={false} /></label>}
      </div>
    </details>
    <details className="connection-capability-advanced">
      <summary>{text.advanced}</summary>
      <p className="field-caption">{text.hint}</p>
      <p className="field-caption">{text.source}: {sourceLabel}</p>
      <button type="button" onClick={onCompatibleDefault}>{text.compatibleDefault}</button>
      <p className="field-caption">{text.compatibleDefaultHint}</p>
      <div className="connection-options-grid">
        <label>{text.protocol}<select disabled={protocolLocked} value={protocol} onChange={event => onProtocol(event.target.value as typeof protocol)}><option value="chat_completions">{text.chat}</option><option value="ollama">{text.native}</option></select></label>
        <label>{text.json}<select value={profile.json_output} onChange={event => change("json_output", event.target.value as typeof profile.json_output)}><option value="prompt_json">{text.prompt}</option><option value="json_object">{text.mode}</option></select></label>
        <label>{text.stream}<select value={profile.stream} onChange={event => onChange({ ...profile, source: "user", stream: event.target.value as typeof profile.stream, include_usage: false })}><option value="none">{text.none}</option><option value={protocol === "ollama" ? "ndjson" : "sse"}>{protocol === "ollama" ? "NDJSON" : "SSE"}</option></select></label>
        {profile.stream === "sse" && <label className="model-check"><input type="checkbox" checked={profile.include_usage} onChange={event => change("include_usage", event.target.checked)} />{text.include}</label>}
        <label>{text.usage}<select value={profile.usage} onChange={event => change("usage", event.target.value as typeof profile.usage)}><option value="unknown">{text.unknown}</option><option value="openai">Chat Completions usage</option><option value="deepseek">DeepSeek usage</option><option value="ollama">Ollama usage</option></select><small>{text.usageHint}</small></label>
        <label>{text.output}<select value={profile.output_parameter} onChange={event => change("output_parameter", event.target.value as typeof profile.output_parameter)}><option value="unknown">{text.unknown}</option>{protocol === "ollama" ? <option value="num_predict">options.num_predict</option> : <><option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option></>}</select></label>
        <label>{text.semantics}<select value={profile.output_semantics} onChange={event => change("output_semantics", event.target.value as typeof profile.output_semantics)}><option value="unknown">{text.unknown}</option><option value="total_generated_tokens_including_reasoning">{text.total}</option><option value="visible_output_tokens_only">{text.visible}</option></select></label>
        <label>{text.cap}<input type="number" min={1} max={2147483647} value={profile.output_limit ?? ""} onChange={event => change("output_limit", event.target.value ? Number(event.target.value) : null)} /></label>
        <label>{text.upstream}<select value={profile.upstream} onChange={event => change("upstream", event.target.value as typeof profile.upstream)}><option value="unknown">{text.unknown}</option><option value="direct">{text.directRoute}</option><option value="proxy">{text.proxyRoute}</option></select></label>
      </div>
      {profile.output_parameter === "unknown" && <label className="model-check"><input type="checkbox" checked={profile.allow_unbounded_output} onChange={event => change("allow_unbounded_output", event.target.checked)} />{text.policy}</label>}
      <p className="field-caption">{text.proxyHint}</p>
    </details>
    {destination && <div className="connection-destination"><strong>{text.destination}: {new URL(destination).host}</strong><span>{text.preview}</span><code>{destination}</code></div>}
  </div>;
}
