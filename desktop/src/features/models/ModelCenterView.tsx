import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Cable, Check, ExternalLink, Plus, RefreshCw, Search, ShieldCheck, Trash2, X } from "lucide-react";

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
  cancelModelTest,
  rotateProviderConnectionSecret,
  setProviderConnectionEnabled,
  setProviderConnectionSecret,
  testConfiguredModel,
  subscribeModelTestEvents,
  getModelDirectory,
} from "../../backend";
import type {
  ConfiguredModelSummary,
  ConnectionTransportSettings,
  DiscoveredModel,
  Language,
  ProviderConnectionSummary,
  ProviderDefinition,
  ConnectionCapabilityProfile,
  ModelDirectory,
  ModelTestEvent,
} from "../../types";
import { COMPATIBLE_DEFAULT_PROFILE, compatibleDefaultProfile, ConnectionOptions, DEFAULT_TRANSPORT, isSameOriginModelsEndpoint, normalizeConnectionEndpoint, UNKNOWN_PROFILE } from "./ConnectionOptions";

type DialogState =
  | { kind: "rotate"; connection: ProviderConnectionSummary }
  | { kind: "delete-connection"; connection: ProviderConnectionSummary }
  | { kind: "delete-model"; model: ConfiguredModelSummary }
  | { kind: "models"; connectionId: string };

type ModelCandidate = DiscoveredModel & { source: "api" | "manual" };
type ModelTestProgress = {
  operationId: string;
  configuredModelId: string;
  connectionId: string;
  alias: string;
  startedAt: number;
  lastSubstantiveAt: number | null;
  activity: "waiting" | "connected" | "heartbeat" | "reasoning" | "receiving";
  phase: "running" | "succeeded" | "failed" | "stopped";
  finishedAt?: number;
  message?: string;
};

const EN = {
  addPreset: "Add a preset connection", providerPreset: "Provider preset", choosePreset: "Choose a provider…", startPreset: "Continue",
  connectionsTitle: "Connections", connectionSearch: "Search connections", enabledState: "Enabled", emptyConnections: "No saved connections yet.", newConnection: "New connection", compatibleApi: "Compatible API", detailEmpty: "Select a connection or add a new one.", back: "Back to connections",
  searchModels: "Search models", getModels: "Get model list", manageModels: "Manage models", saveAndGetModels: "Save and get models", saveOnly: "Save only", cancelEdit: "Cancel",
  advanced: "Advanced settings", apiListSource: "Returned by API", manualSource: "Manual model ID", catalogSource: "Catalog metadata", alreadyAdded: "Already added", addOneModel: "Add model", selectVisible: "Select visible", clearVisible: "Clear visible", emptyModelList: "No models found. You can enter a model ID below.",
  endpointMode: "Request address format", baseAddress: "Base URL (append chat/completions)", fullAddress: "Full request URL", listAddress: "Model list address override", network: "Network route", systemNetwork: "Follow system proxy settings", directNetwork: "Connect directly", proxyNetwork: "Use an HTTP(S) proxy", proxyUrl: "Proxy URL", proxySystemHint: "Windows static system proxy and supported environment settings are followed. PAC scripts are not executed; configure an explicit proxy if your system uses PAC.", sameOriginHint: "The model-list override must use the same origin so credentials are never sent to a different host.", compatibleDefault: "Use compatible defaults", compatibleDefaultHint: "SSE streaming, prompted JSON with local parsing, and a max_tokens request limit. This is a safe request default, not a verified provider contract.",
  customEntry: "Add custom interface", editConnection: "Edit connection", listHint: "Model list lookup is metadata only. A missing list does not prevent manual entry.", testHint: "Text test: one short generation request, may incur cost. No automatic retry. JSON parsing does not certify JSON mode/schema or full context capacity.", visionTest: "Test vision (2 requests)", site: "Service site", optionalModel: "Model ID (optional; can be added later)", directory: "Model specifications (models.dev)", refreshDirectory: "Refresh directory metadata", directoryHint: "Catalog declarations, not endpoint verification. Prices are USD per million tokens; route-specific values only. Unknown/private models can always be entered manually.", context: "Declared context", output: "Declared output", useContext: "Use this context declaration", capabilities: "Capability observations", promptJson: "Prompted JSON parsing", unknown: "Unknown / not tested",
  intro: "Configure connections once, then select model references in research and OT Studio. Secrets are stored only in Windows Credential Manager; existing keys are never returned to this page.",
  testWaiting: "Waiting for provider response", testConnected: "Connected; waiting for response", testHeartbeat: "Connection remains active", testReasoning: "Thinking", testReceiving: "Receiving response", testCompleted: "Test completed", testFailed: "Test failed", testCapabilityFailed: "Text connection completed, but the selected capability check did not pass. Review this model's capability check results.", testStopped: "Stopped waiting; no retry was sent.", testElapsed: "Elapsed", testSinceActivity: "Since last substantive activity", testNoActivity: "No substantive activity yet", stopWaiting: "Stop waiting",
  siteHint: "Choose the service site where your API key was created. Accounts and keys may differ between sites; your location does not select the site automatically.",
  textCapability: "Text",
  overrideContract: "Set this connection's protocol and budget policy", overrideHint: "Existing preset contracts are retained unless you enable this. An unlisted model with an unknown output contract needs an explicit policy before generation.",
  search: "Search providers", all: "All", cloud: "Cloud", local: "Local", free: "Free paths",
  connection: "connection", connections: "connections", ready: "Connection checked", untested: "Untested", disabled: "Disabled", unauthorized: "Unauthorized", rateLimited: "Rate limited", unavailable: "Unavailable",
  configure: "Configure provider", close: "Close", registration: "Registration and API keys", addConnection: "Add connection",
  displayName: "Connection name", endpoint: "Endpoint", region: "Region", apiKey: "API key",
  apiKeyHint: "After saving, only the configured state remains visible.", save: "Save connection", saving: "Saving…",
  customConfirm: "I confirm that the key and research content will be sent to the full Origin shown above.",
  localPrivacy: "Connected to Ollama on this computer. The selected model may run locally or forward to a cloud service; execution location is not inferred from localhost.",
  remotePrivacy: "Remote Ollama: research content is sent to that address and is not labelled local.",
  existing: "Existing connections", enable: "Enable", disable: "Disable", replaceKey: "Replace key", delete: "Delete",
  models: "Configured models", discover: "Discover models", discovering: "Discovering…", testConnection: "Test connection", testingConnection: "Testing connection…", testUsing: "Uses", endpointProbe: "Endpoint probe (no model)", advancedModels: "Advanced: custom model ID", manualModel: "Manual model ID",
  addManual: "Add candidate", selectModels: "Select one or more models", addModels: "Add selected models",
  selectModel: "Select model", visionCapability: "Vision input", visionCapabilityHint: "Enable image input only for the selected model. Text testing makes one request; the separate vision test makes one text and one image request. Declarations alone are not verification.",
  timeout: "Request timeout (seconds)", timeoutHint: "Set how long OpenThesis waits for this model request (5–600 seconds).", saveTimeout: "Save timeout", timeoutSaved: "Timeout saved.", timeoutInvalid: "Enter a whole number from 5 to 600 seconds.",
  testing: "Testing…", test: "Test", noConnections: "No connections yet. Add an account or an existing Ollama service.",
  noModels: "No configured models yet.", noProviders: "No providers match this filter.",
  credentialSaved: "Credential saved securely.", connectionSaved: "Connection saved.", modelsSaved: "Models added.", testSucceeded: "Connection check succeeded; full research capacity was not benchmarked.",
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
  addPreset: "添加预设连接", providerPreset: "服务商预设", choosePreset: "选择服务商…", startPreset: "继续",
  connectionsTitle: "连接", connectionSearch: "搜索连接", enabledState: "已启用", emptyConnections: "还没有已保存的连接。", newConnection: "新建连接", compatibleApi: "通用兼容接口", detailEmpty: "选择一个连接，或添加新连接。", back: "返回连接列表",
  searchModels: "搜索模型", getModels: "获取模型列表", manageModels: "管理模型", saveAndGetModels: "保存并获取模型", saveOnly: "仅保存", cancelEdit: "取消",
  advanced: "高级设置", apiListSource: "接口返回", manualSource: "手动模型 ID", catalogSource: "目录元数据", alreadyAdded: "已添加", addOneModel: "添加模型", selectVisible: "全选当前筛选", clearVisible: "清除当前筛选", emptyModelList: "没有找到模型；可以在下方手动填写模型 ID。",
  endpointMode: "请求地址格式", baseAddress: "基础 URL（自动补全 chat/completions）", fullAddress: "完整请求 URL", listAddress: "模型列表地址覆盖", network: "网络路径", systemNetwork: "跟随系统代理设置", directNetwork: "直连", proxyNetwork: "使用 HTTP(S) 代理", proxyUrl: "代理 URL", proxySystemHint: "跟随 Windows 静态系统代理及受支持的环境设置；不会执行 PAC 脚本。如系统使用 PAC，请填写明确的代理地址。", sameOriginHint: "模型列表覆盖地址必须与接口同源，避免将密钥发送到其他主机。", compatibleDefault: "使用兼容默认值", compatibleDefaultHint: "SSE 流式、提示词 JSON＋本地解析、max_tokens 请求上限。这是安全的请求默认值，不代表服务商契约已验证。",
  customEntry: "添加自定义接口", editConnection: "编辑连接", listHint: "获取列表仅查询元数据；列表不可用时仍可手填模型。", testHint: "文本测试：1 次短生成请求，可能收费，不自动重试。JSON 可解析不等于支持 API JSON／schema 或完整上下文容量。", visionTest: "测试视觉（2 次请求）", site: "服务站点", optionalModel: "模型 ID（可选，也可稍后添加）", directory: "模型规格目录（models.dev）", refreshDirectory: "刷新目录元数据", directoryHint: "目录声明不等于接口验证；参考价格为每百万 Token 的美元价格，只使用对应路由数据，未知或私有模型始终可手填。", context: "声明上下文容量", output: "声明输出容量", useContext: "采用此上下文声明", capabilities: "能力观察", promptJson: "提示词 JSON 解析", unknown: "未知／尚未测试",
  intro: "先配置模型连接，再在研究和 OT 创作工作室中按引用选择。密钥只写入 Windows 凭据管理器，页面无法读取旧密钥明文。",
  testWaiting: "等待服务响应", testConnected: "已连接，等待响应", testHeartbeat: "连接保持中", testReasoning: "正在思考", testReceiving: "正在接收响应", testCompleted: "测试完成", testFailed: "测试失败", testCapabilityFailed: "文字连接已完成，但所选能力检查未通过，请查看该模型的能力检查结果。", testStopped: "已停止等待；没有重试请求。", testElapsed: "已用时间", testSinceActivity: "距离上次实质活动", testNoActivity: "尚无实质活动", stopWaiting: "停止等待",
  siteHint: "请选择创建 API Key 时使用的服务站点；不同站点的账户和密钥可能不通用，不按你所在地自动切换。",
  textCapability: "文本",
  overrideContract: "设置此连接的协议与预算策略", overrideHint: "未开启时保留现有预设契约；未列出模型的输出契约未知时，需要明确设置策略才能调用。",
  search: "搜索服务商", all: "全部", cloud: "云端", local: "本地", free: "免费路径",
  connection: "个连接", connections: "个连接", ready: "连接已检查", untested: "未测试", disabled: "已禁用", unauthorized: "未授权", rateLimited: "已限流", unavailable: "不可用",
  configure: "配置服务商", close: "关闭", registration: "注册与获取密钥", addConnection: "添加连接",
  displayName: "连接名称", endpoint: "接口地址", region: "区域", apiKey: "API Key",
  apiKeyHint: "保存后只显示“已配置”，不会回显明文。", save: "保存连接", saving: "正在保存…",
  customConfirm: "我确认密钥和研究内容将发送到上方完整 Origin。",
  localPrivacy: "连接这台电脑上的 Ollama；所选模型可能本地运行，也可能转发云端，不能仅凭 localhost 判断执行位置。",
  remotePrivacy: "远程 Ollama：研究内容会发送到该远程地址，且不会标记为本地。",
  existing: "已有连接", enable: "启用", disable: "禁用", replaceKey: "替换密钥", delete: "删除",
  models: "已配置模型", discover: "发现模型", discovering: "正在发现…", testConnection: "测试连接", testingConnection: "正在测试连接…", testUsing: "测试模型", endpointProbe: "接口探测（无需模型）", advancedModels: "高级：自定义模型 ID", manualModel: "手动模型 ID",
  addManual: "加入候选", selectModels: "选择一个或多个模型", addModels: "添加所选模型",
  selectModel: "选择模型", visionCapability: "视觉输入", visionCapabilityHint: "仅为所选模型声明图像输入。文本测试只发 1 次；单独的视觉测试发送 1 次文本和 1 次图片请求。声明不等于已验证。",
  timeout: "请求超时（秒）", timeoutHint: "设置 OpenThesis 等待该模型响应的时间（5–600 秒）。", saveTimeout: "保存超时设置", timeoutSaved: "超时设置已保存。", timeoutInvalid: "请输入 5–600 之间的整数秒数。",
  testing: "正在测试…", test: "测试", noConnections: "还没有连接。先添加一个账户或本地 Ollama。",
  noModels: "还没有已配置模型。", noProviders: "没有匹配的服务商。",
  credentialSaved: "凭据已安全保存。", connectionSaved: "连接已保存。", modelsSaved: "模型已添加。", testSucceeded: "连接检查成功；尚未测试完整研究容量。",
  rotateTitle: "替换已保存的 API Key", rotateBody: "新密钥会先经过连接测试，再切换为当前密钥；测试失败时旧密钥保持不变。",
  deleteConnectionTitle: "删除这个连接？", confirmDelete: "此操作会永久删除连接、其中的已配置模型和 Windows 系统凭据。",
  deleteModelTitle: "删除这个已配置模型？", confirmDeleteModel: "连接及其系统凭据仍会保留。",
  cancel: "取消", confirm: "确认", confirmReplace: "测试并替换", working: "处理中…",
  freeBadge: "免费", localBadge: "本地", costUnknown: "费用未知", visionBadge: "视觉",
  manualUnverified: "手动添加的模型在测试前标记为未验证。",
  ollamaGuide: "OpenThesis 只连接你已经安装并启动的 Ollama，不会安装 Ollama，也不会下载模型。",
  status: "状态",
};

const ZH_HANT: ModelCenterText = {
  ...ZH,
  addPreset: "新增預設連線", providerPreset: "服務商預設", choosePreset: "選擇服務商…", startPreset: "繼續",
  connectionsTitle: "連線", connectionSearch: "搜尋連線", enabledState: "已啟用", emptyConnections: "尚無已儲存的連線。", newConnection: "新增連線", compatibleApi: "通用相容介面", detailEmpty: "選擇一個連線，或新增連線。", back: "返回連線清單",
  searchModels: "搜尋模型", getModels: "取得模型清單", manageModels: "管理模型", saveAndGetModels: "儲存並取得模型", saveOnly: "僅儲存", cancelEdit: "取消",
  advanced: "進階設定", apiListSource: "介面回傳", manualSource: "手動模型 ID", catalogSource: "目錄中繼資料", alreadyAdded: "已新增", addOneModel: "新增模型", selectVisible: "全選目前篩選", clearVisible: "清除目前篩選", emptyModelList: "找不到模型；可以在下方手動填寫模型 ID。",
  endpointMode: "請求位址格式", baseAddress: "基礎 URL（自動補全 chat/completions）", fullAddress: "完整請求 URL", listAddress: "模型清單位址覆寫", network: "網路路徑", systemNetwork: "跟隨系統代理設定", directNetwork: "直連", proxyNetwork: "使用 HTTP(S) 代理", proxyUrl: "代理 URL", proxySystemHint: "跟隨 Windows 靜態系統代理及受支援的環境設定；不會執行 PAC 指令碼。如系統使用 PAC，請填寫明確的代理位址。", sameOriginHint: "模型清單覆寫位址必須與介面同源，避免將金鑰傳送至其他主機。", compatibleDefault: "使用相容預設值", compatibleDefaultHint: "SSE 串流、提示詞 JSON＋本機解析、max_tokens 請求上限。這是安全的請求預設值，不代表服務商契約已驗證。",
  customEntry: "新增自訂介面", editConnection: "編輯連線", listHint: "取得清單僅查詢中繼資料；清單不可用時仍可手填模型。", testHint: "文字測試：1 次短生成請求，可能收費，不自動重試。JSON 可解析不等於支援 API JSON／schema 或完整上下文容量。", visionTest: "測試視覺（2 次請求）", site: "服務站點", optionalModel: "模型 ID（可選，也可稍後新增）", directory: "模型規格目錄（models.dev）", refreshDirectory: "更新目錄中繼資料", directoryHint: "目錄宣告不等於介面驗證；參考價格為每百萬 Token 的美元價格，僅使用對應路由資料，未知或私有模型始終可手填。", context: "宣告上下文容量", output: "宣告輸出容量", useContext: "採用此上下文宣告", capabilities: "能力觀察", promptJson: "提示詞 JSON 解析", unknown: "未知／尚未測試",
  localPrivacy: "連接這台電腦上的 Ollama；所選模型可能本機執行，也可能轉送雲端，不能僅憑 localhost 判斷執行位置。", remotePrivacy: "遠端 Ollama：研究內容會傳送至該位址，並不代表本機執行。",
  visionCapabilityHint: "僅為所選模型宣告圖像輸入。文字測試只發送 1 次；獨立的視覺測試發送 1 次文字與 1 次圖片請求。宣告不等於已驗證。",
  timeout: "請求逾時（秒）", timeoutHint: "設定 OpenThesis 等待此模型回應的時間（5–600 秒）。", saveTimeout: "儲存逾時設定", timeoutSaved: "逾時設定已儲存。", timeoutInvalid: "請輸入 5–600 之間的整數秒數。",
  testWaiting: "等待服務回應", testConnected: "已連線，等待回應", testHeartbeat: "連線保持中", testReasoning: "正在思考", testReceiving: "正在接收回應", testCompleted: "測試完成", testFailed: "測試失敗", testCapabilityFailed: "文字連線已完成，但所選能力檢查未通過，請查看此模型的能力檢查結果。", testStopped: "已停止等待；沒有重試請求。", testElapsed: "已用時間", testSinceActivity: "距離上次實質活動", testNoActivity: "尚無實質活動", stopWaiting: "停止等待",
  intro: "先設定模型連線，再在研究與 OT 創作工作室中按引用選擇。金鑰只寫入 Windows 認證管理員，頁面無法讀取舊金鑰明文。",
  siteHint: "請選擇建立 API Key 時使用的服務站點；不同站點的帳戶與金鑰可能不通用，不依所在地自動切換。",
  textCapability: "文字", freeBadge: "免費", localBadge: "本機", test: "測試", testing: "正在測試…",
  apiKey: "API Key", close: "關閉", delete: "刪除", region: "區域", registration: "註冊與取得金鑰",
  disabled: "已停用", untested: "尚未測試", unauthorized: "未授權", rateLimited: "受到速率限制", unavailable: "不可用", selectModel: "選擇模型",
  overrideContract: "設定此連線的協定與預算策略", overrideHint: "未開啟時保留現有預設契約；未列出模型的輸出契約未知時，需要明確設定策略才能呼叫。",
  search: "搜尋服務商", local: "本機", configure: "設定服務商", addConnection: "新增連線",
  displayName: "連線名稱", endpoint: "介面位址", apiKeyHint: "儲存後只顯示「已設定」，不會回顯明文。",
  save: "儲存連線", saving: "正在儲存…", existing: "現有連線", enable: "啟用", disable: "停用",
  replaceKey: "替換金鑰", models: "已設定模型", discover: "探索模型", discovering: "正在探索…",
  selectModels: "選擇一個或多個模型", addModels: "新增所選模型", endpointProbe: "介面探測（無需模型）", noConnections: "尚未建立連線。",
  noModels: "尚未設定模型。", credentialSaved: "憑據已安全儲存。", connectionSaved: "連線已儲存。",
  modelsSaved: "模型已新增。", ready: "連線已檢查", testSucceeded: "連線檢查成功；尚未測試完整研究容量。", rotateTitle: "替換已儲存的 API Key",
  rotateBody: "新金鑰會先經過連線測試，再切換為目前金鑰；測試失敗時舊金鑰保持不變。",
  deleteConnectionTitle: "刪除這個連線？", confirmDelete: "此操作會永久刪除連線、其中已設定的模型和 Windows 系統憑據。",
  deleteModelTitle: "刪除這個已設定模型？", confirmDeleteModel: "連線及其系統憑據仍會保留。",
  cancel: "取消", confirm: "確認", confirmReplace: "測試並替換", working: "處理中…",
  costUnknown: "費用未知", status: "狀態",
};

function errorMessage(error: unknown, language: Language = "en"): string {
  const code = error && typeof error === "object" && "code" in error ? String(error.code) : "";
  const fixes: Record<string, [string, string, string]> = {
    MODEL_OUTPUT_BUDGET_POLICY_REQUIRED: ["No model request was sent. Edit this connection to declare an output limit or explicitly accept that no verified hard cap is available.", "尚未发送模型请求。请编辑此连接，设置输出上限参数，或明确选择没有经验证硬上限的预算策略。", "尚未傳送模型請求。請編輯此連線，設定輸出上限參數，或明確選擇沒有經驗證硬上限的預算策略。"],
    MODEL_CONFIGURATION_CHANGED: ["No model request was sent. The connection or capability settings changed; select the updated configuration and test again.", "尚未发送模型请求。连接或能力配置已变化，请选择更新后的配置再测试。", "尚未傳送模型請求。連線或能力設定已變更，請選擇更新後的設定再測試。"],
    MODEL_CONNECTION_AUTH_PAUSED: ["This model request was not sent. Authentication failures paused the connection; update its key or explicitly test again in Model Center.", "尚未发送本次模型请求。连接因鉴权失败暂停，请更新密钥或在模型中心明确重新测试。", "尚未傳送本次模型請求。連線因驗證失敗暫停，請更新金鑰或在模型中心明確重新測試。"],
    MODEL_SUBMISSION_UNKNOWN: ["It is unclear whether the request completed. It will not be resent automatically to avoid duplicate charges; check the call record and provider status.", "请求是否完成尚不确定，不会自动重发以免重复计费。请查看本次调用记录和供应商状态。", "請求是否完成尚不確定，不會自動重送以免重複計費。請查看本次呼叫紀錄與供應商狀態。"],
    MODEL_PROXY_PAC_UNSUPPORTED: ["This system only exposes a PAC proxy, which this version does not execute. Configure a static proxy or a supported proxy environment variable.", "系统仅提供 PAC 代理，而此版本不会执行 PAC。请配置静态代理或受支持的代理环境变量。", "系統僅提供 PAC 代理，而此版本不會執行 PAC。請設定靜態代理或受支援的代理環境變數。"],
    MODEL_PROXY_INVALID: ["The configured proxy URL is invalid. Check its scheme, host, and port.", "代理 URL 无效，请检查协议、主机和端口。", "代理 URL 無效，請檢查協定、主機與連接埠。"],
    MODEL_DNS_ERROR: ["The endpoint host could not be resolved. Check the address and network settings.", "无法解析接口主机名，请检查地址和网络设置。", "無法解析介面主機名稱，請檢查位址與網路設定。"],
    MODEL_TLS_ERROR: ["A secure connection to the endpoint could not be established. Check the endpoint certificate and system trust settings.", "无法与接口建立安全连接，请检查接口证书和系统信任设置。", "無法與介面建立安全連線，請檢查介面憑證與系統信任設定。"],
    MODEL_PROXY_ERROR: ["The configured network route could not reach the endpoint. Check the proxy or direct-connection settings.", "配置的网络路径无法连接接口，请检查代理或直连设置。", "設定的網路路徑無法連線至介面，請檢查代理或直連設定。"],
    MODEL_ENDPOINT_FORBIDDEN_TARGET: ["This endpoint targets a local or private network address that the connection policy does not allow.", "此接口指向连接策略不允许的本地或私有网络地址。", "此介面指向連線策略不允許的本機或私有網路位址。"],
    MODEL_MODELS_ENDPOINT_ORIGIN: ["The model-list override must use the same origin as the request endpoint. Create a separate connection for another host.", "模型列表覆盖地址必须与请求接口同源；其他主机请另建连接。", "模型清單覆寫位址必須與請求介面同源；其他主機請另建連線。"],
    MODEL_TRANSPORT_INVALID: ["The endpoint or network-route settings are invalid. Check the advanced transport fields.", "接口或网络路径设置无效，请检查高级传输字段。", "介面或網路路徑設定無效，請檢查進階傳輸欄位。"],
    MODEL_TEST_BUSY: ["A test for this connection is already running. Wait for it to finish before starting another.", "此连接已有测试正在进行，请等待其结束后再启动另一项测试。", "此連線已有測試正在進行，請等待其結束後再啟動另一項測試。"],
    MODEL_CALL_CANCELLED: ["The model test was cancelled. No retry was sent.", "模型测试已取消，没有发送重试请求。", "模型測試已取消，沒有傳送重試請求。"],
    MODEL_DISCOVERY_UNSUPPORTED: ["This service does not provide a model list. Enter the exact model ID manually; existing configured models are retained.", "此服务不提供模型列表。请手动输入准确的模型 ID；已有模型配置会保留。", "此服務未提供模型清單。請手動輸入正確的模型 ID；既有模型設定會保留。"],
    MODEL_STREAM_UNSUPPORTED: ["No retry was sent. Disable streaming or choose a stream format supported by this service before testing again.", "没有发送重试请求。请关闭流式协议，或选择此服务支持的流式格式后再测试。", "沒有傳送重試請求。請關閉串流協定，或選擇此服務支援的串流格式後再測試。"],
    MODEL_OUTPUT_PARAMETER_UNSUPPORTED: ["No follow-up request was sent. Change the output limit parameter to one supported by this service before testing again.", "没有发送后续请求。请将输出上限参数改为此服务支持的参数后再测试。", "沒有傳送後續請求。請將輸出上限參數改為此服務支援的參數後再測試。"],
    MODEL_MODEL_NOT_FOUND: ["The exact model ID was not found. Check its spelling and the provider's model list, or enter the correct ID manually.", "未找到准确的模型 ID。请核对拼写和服务商模型列表，或手动输入正确的 ID。", "找不到正確的模型 ID。請核對拼寫與服務商模型清單，或手動輸入正確的 ID。"],
  };
  if (fixes[code]) return fixes[code][language === "en" ? 0 : language === "zh-Hant" ? 2 : 1] + ` (${code})`;
  if (typeof error === "string") return error;
  if (error && typeof error === "object" && "message" in error) return String(error.message);
  return "Unexpected model-center error.";
}

function discoveryErrorMessage(error: unknown, language: Language): string {
  const raw = errorMessage(error, language);
  const searchable = `${raw} ${error && typeof error === "object" && "code" in error ? String(error.code) : ""}`.toLowerCase();
  if (/\b404\b|\b405\b|models?_list_unavailable/.test(searchable)) {
    return language === "zh-Hant" ? "服務平台未提供模型清單；請手動輸入模型 ID。既有設定的模型仍保留。" : language === "zh-CN" ? "服务平台未提供模型列表；请手动输入模型 ID。已有模型配置仍会保留。" : "This provider does not offer a model list. Enter the model ID manually; existing configured models are retained.";
  }
  if (/\b401\b|\b403\b|unauthori[sz]ed|forbidden/.test(searchable)) {
    return language === "zh-Hant" ? "取得模型清單失敗：請檢查 API Key 與帳戶權限。已儲存的連線和模型不受影響。" : language === "zh-CN" ? "获取模型列表失败：请检查 API Key 和账户权限。已保存的连接和模型不受影响。" : "Could not get the model list. Check the API key and account permissions; the saved connection and models are unchanged.";
  }
  return raw;
}

function localizedModelTestError(code: unknown, language: Language): string | null {
  if (typeof code !== "string" || !code.trim()) return null;
  const message = errorMessage({ code: code.trim() }, language);
  return message === "Unexpected model-center error." ? null : message;
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
  let hash=2166136261; for (const char of modelId) hash=Math.imul(hash ^ char.charCodeAt(0),16777619);
  return [connectionId.slice(0,55), slug, (hash >>> 0).toString(36)].join(".").slice(0, 128);
}

function normalizeTimeout(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) && seconds >= 5 && seconds <= 600 ? seconds : null;
}

function elapsedLabel(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function statusLabel(connection: Pick<ProviderConnectionSummary, "enabled" | "status">, text: ModelCenterText): string {
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
  const [showForm, setShowForm] = useState(false);
  const [showPresetPicker, setShowPresetPicker] = useState(false);
  const [presetChoice, setPresetChoice] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("");
  const [secret, setSecret] = useState("");
  const [draftConnectionId, setDraftConnectionId] = useState("");
  const [protocol, setProtocol] = useState<"chat_completions" | "ollama">("chat_completions");
  const [profile, setProfile] = useState<ConnectionCapabilityProfile>({...UNKNOWN_PROFILE});
  const [transport, setTransport] = useState<ConnectionTransportSettings>({...DEFAULT_TRANSPORT});
  const [overrideContract, setOverrideContract] = useState(false);
  const [directory, setDirectory] = useState<ModelDirectory | null>(null);
  const [contextDraft, setContextDraft] = useState("");
  const [directoryRoute, setDirectoryRoute] = useState("");
  const [discovered, setDiscovered] = useState<ModelCandidate[]>([]);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [visionModelIds, setVisionModelIds] = useState<Set<string>>(new Set());
  const [modelQuery, setModelQuery] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [newModelTimeout, setNewModelTimeout] = useState("180");
  const [timeoutDrafts, setTimeoutDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [modelTestProgress, setModelTestProgress] = useState<ModelTestProgress | null>(null);
  const [modelTestClock, setModelTestClock] = useState(() => Date.now());
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [dialogSecret, setDialogSecret] = useState("");
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const connectionRef = useRef("");
  const discoveryRevisionRef = useRef(0);
  const activeModelTestRef = useRef<{ operationId: string; connectionId: string; unlisten?: () => void; terminal: boolean } | null>(null);
  const connectionBusyRef = useRef(new Set<string>());
  const [connectionBusy, setConnectionBusy] = useState<Record<string, string>>({});

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

  const reload = useCallback(async (preferredConnectionId?: string) => {
    const result = await Promise.all([listModelProviders(), listProviderConnections(), listConfiguredModels()]);
    setProviders(result[0]); setConnections(result[1]); setModels(result[2]);
    const selectedId = preferredConnectionId && result[1].some((item) => item.connection_id === preferredConnectionId)
      ? preferredConnectionId
      : result[1].some((item) => item.connection_id === connectionRef.current)
        ? connectionRef.current
        : result[1][0]?.connection_id || "";
    connectionRef.current = selectedId;
    setConnectionId(selectedId);
    setProviderId(result[1].find((item) => item.connection_id === selectedId)?.provider_id || null);
  }, []);
  useEffect(() => { void getModelDirectory().then(setDirectory).catch(() => undefined); }, []);

  useEffect(() => { void reload().catch((reason) => setError(errorMessage(reason))); }, [reload]);

  const provider = providers.find((item) => item.provider_id === providerId);
  const providerConnections = connections.filter((item) => item.provider_id === providerId);
  const activeConnection = providerConnections.find((item) => item.connection_id === connectionId);
  const modelsEndpointOverride = activeConnection?.transport?.models_endpoint?.trim() ?? "";
  const modelsRequestUrl = activeConnection
    ? normalizeConnectionEndpoint(activeConnection.endpoint, activeConnection.protocol ?? (provider?.provider_id === "ollama" ? "ollama" : "chat_completions"), activeConnection.transport?.endpoint_mode ?? "base")?.request_url
    : undefined;
  const canDiscoverModels = Boolean(provider?.supports_discovery || (modelsEndpointOverride && modelsRequestUrl && isSameOriginModelsEndpoint(modelsEndpointOverride, modelsRequestUrl)));
  const connectionModels = models.filter((item) => item.connection_id === connectionId);
  const busyForConnection = (id: string) => Boolean(connectionBusy[id]) || busy === "connection:" + id || busy.startsWith("dialog:");
  const busyForModel = (id: string) => {
    const model = connectionModels.find((item) => item.configured_model_id === id);
    return Boolean((model && connectionBusy[model.connection_id]) || busy === "timeout:" + id || busy.startsWith("dialog:"));
  };
  const invalidateDiscovery = () => { discoveryRevisionRef.current += 1; };
  const selectConnection = (connection: ProviderConnectionSummary) => {
    ignoreActiveModelTest();
    invalidateDiscovery();
    connectionRef.current = connection.connection_id;
    setConnectionId(connection.connection_id); setProviderId(connection.provider_id);
    setShowForm(false); setDraftConnectionId(""); setDiscovered([]); setSelectedModels(new Set()); setVisionModelIds(new Set()); setModelQuery(""); setDirectoryRoute(connection.provider_id);
    setManualModel(""); setContextDraft(""); setError(""); setNotice("");
  };
  const chooseProvider = (item: ProviderDefinition) => {
    ignoreActiveModelTest();
    invalidateDiscovery();
    connectionRef.current = "";
    setProviderId(item.provider_id); setConnectionId("");
    setDisplayName(""); setEndpoint(item.provider_id === "custom" ? "" : item.base_url); setRegion(item.region);
    setDraftConnectionId(""); setProtocol(item.provider_id === "ollama" ? "ollama" : "chat_completions");
    setProfile(item.provider_id === "custom" ? { ...COMPATIBLE_DEFAULT_PROFILE } : { ...UNKNOWN_PROFILE });
    setTransport({ ...DEFAULT_TRANSPORT }); setManualModel(""); setContextDraft("");
    setOverrideContract(item.provider_id === "custom");
    setDirectoryRoute(item.provider_id === "custom" ? "" : item.provider_id);
    setSecret(""); setDiscovered([]); setSelectedModels(new Set()); setVisionModelIds(new Set()); setShowForm(true); setShowPresetPicker(false);
    setError(""); setNotice("");
  };

  const beginConnectionBusy = (id: string, operation: string) => {
    if (connectionBusyRef.current.has(id)) return false;
    connectionBusyRef.current.add(id);
    setConnectionBusy((current) => ({ ...current, [id]: operation }));
    return true;
  };
  const endConnectionBusy = (id: string) => {
    connectionBusyRef.current.delete(id);
    setConnectionBusy((current) => { const next = { ...current }; delete next[id]; return next; });
  };
  const ignoreActiveModelTest = () => {
    const current = activeModelTestRef.current;
    activeModelTestRef.current = null;
    current?.unlisten?.();
    setModelTestProgress(null);
  };
  useEffect(() => {
    if (modelTestProgress?.phase !== "running") return undefined;
    setModelTestClock(Date.now());
    const timer = window.setInterval(() => setModelTestClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [modelTestProgress?.operationId, modelTestProgress?.phase]);
  useEffect(() => () => {
    const current = activeModelTestRef.current;
    activeModelTestRef.current = null;
    current?.unlisten?.();
  }, []);

  const defaultConnectionName = (target: string, fallback: string) => {
    try { return new URL(target).hostname.replace(/^www\./i, "") || fallback; } catch { return fallback; }
  };

  const saveConnection = async (discoverAfterSave = false) => {
    if (!provider) return;
    const normalized = normalizeConnectionEndpoint(endpoint, protocol, transport.endpoint_mode);
    if (!normalized) { setError(text.endpoint); return; }
    if (!isSameOriginModelsEndpoint(transport.models_endpoint, normalized.request_url)) { setError(text.sameOriginHint); return; }
    if (provider.requires_api_key && !secret.trim() && !draftConnectionId) { setError(text.apiKey); return; }
    if (transport.network_mode === "proxy" && !transport.proxy_url?.trim()) { setError(text.proxyUrl); return; }
    setBusy("save-connection"); setError(""); setNotice("");
    try {
      const saved = await saveProviderConnection({
        connection_id: draftConnectionId || provider.provider_id + "-" + Date.now().toString(36),
        provider_id: provider.provider_id,
        display_name: displayName.trim() || defaultConnectionName(normalized.endpoint, provider.display_name),
        region: region.trim() || provider.region,
        endpoint: normalized.endpoint,
        enabled: activeConnection?.enabled ?? true,
        protocol,
        transport: { ...transport, endpoint_mode: normalized.endpoint_mode, proxy_url: transport.network_mode === "proxy" ? transport.proxy_url?.trim() || null : null, models_endpoint: transport.models_endpoint?.trim() || null },
        ...(overrideContract ? { capability_profile: profile } : {}),
      });
      setDraftConnectionId(saved.connection_id);
      connectionRef.current = saved.connection_id;
      setConnectionId(saved.connection_id); setProviderId(saved.provider_id);
      await reload(saved.connection_id);
      const hasNewSecret = Boolean(secret.trim());
      if (hasNewSecret) await setProviderConnectionSecret(saved.connection_id, secret.trim());
      setSecret(""); setShowForm(false); setShowPresetPicker(false);
      setNotice(hasNewSecret ? text.credentialSaved : text.connectionSaved);
      await reload(saved.connection_id);
      if (discoverAfterSave) await runDiscovery(saved.connection_id, true);
    } catch (reason) { setError(errorMessage(reason, language)); } finally { setBusy(""); }
  };
  const editConnection = (connection: ProviderConnectionSummary) => {
    ignoreActiveModelTest();
    invalidateDiscovery(); connectionRef.current = connection.connection_id;
    setConnectionId(connection.connection_id); setProviderId(connection.provider_id);
    setDraftConnectionId(connection.connection_id); setDisplayName(connection.display_name); setRegion(connection.region); setDirectoryRoute(connection.provider_id);
    setEndpoint(connection.endpoint); setProtocol(connection.protocol || (connection.provider_id === "ollama" ? "ollama" : "chat_completions"));
    setProfile(connection.capability_profile ? { ...connection.capability_profile } : { ...UNKNOWN_PROFILE });
    setTransport(connection.transport || { ...DEFAULT_TRANSPORT }); setSecret(""); setManualModel(""); setContextDraft(""); setShowForm(true);
    setOverrideContract(Boolean(connection.capability_profile));
  };
  const directoryEntry = directory?.models.find(m=>m.provider_id === directoryRoute && m.model_id === (manualModel.trim() || Array.from(selectedModels)[0]));

  const runDiscovery = async (targetConnectionId = connectionId, openResults = true, trigger?: HTMLElement) => {
    if (!targetConnectionId || !beginConnectionBusy(targetConnectionId, "discover")) return;
    const requestRevision = ++discoveryRevisionRef.current;
    setError(""); setNotice("");
    try {
      const result = await discoverConnectionModels(targetConnectionId);
      if (discoveryRevisionRef.current !== requestRevision || connectionRef.current !== targetConnectionId) return;
      const candidates = result.map((item) => ({ ...item, source: "api" as const }));
      setDiscovered(candidates);
      setSelectedModels(new Set());
      setVisionModelIds(new Set(result.filter((item) => item.capabilities.includes("vision")).map((item) => item.model_id)));
      setModelQuery("");
      if (openResults) openDialog({ kind: "models", connectionId: targetConnectionId }, trigger);
      if (!result.length) setNotice(text.emptyModelList);
    }
    catch (reason) {
      if (discoveryRevisionRef.current === requestRevision && connectionRef.current === targetConnectionId) setError(discoveryErrorMessage(reason, language));
    } finally { endConnectionBusy(targetConnectionId); }
  };
  const discover = (trigger?: HTMLElement) => void runDiscovery(connectionId, true, trigger);

  const queueManualCandidate = () => {
    const value = manualModel.trim(); if (!value) return;
    if (!discovered.some((item) => item.model_id === value)) {
      setDiscovered((items) => items.concat({
        model_id: value, alias: value,
        billing_class: "unknown",
        capabilities: ["text_chat", "structured_json"],
        source: "manual",
      }));
    }
    setSelectedModels((items) => new Set(items).add(value)); setManualModel("");
    setModelQuery("");
  };
  const addManual = (trigger?: HTMLElement) => {
    if (!connectionId) return;
    queueManualCandidate();
    openDialog({ kind: "models", connectionId }, trigger);
  };

  const persistCandidate = async (targetConnectionId: string, modelId: string) => {
    const timeoutSeconds = normalizeTimeout(newModelTimeout);
    if (timeoutSeconds === null) throw new Error(text.timeoutInvalid);
    const candidate = discovered.find((item) => item.model_id === modelId);
    const existing = models.find((item) => item.connection_id === targetConnectionId && item.model_id === modelId);
    let capabilities = existing?.capabilities ?? ["text_chat", "structured_json"];
    if (visionModelIds.has(modelId) && !capabilities.includes("vision")) capabilities = [...capabilities, "vision"];
    await saveConfiguredModel({
      configured_model_id: existing?.configured_model_id || modelReferenceId(targetConnectionId, modelId),
      connection_id: targetConnectionId,
      model_id: modelId,
      alias: existing ? existing.alias : candidate?.alias || modelId,
      enabled: existing?.enabled ?? true,
      capabilities,
      timeout_seconds: existing?.timeout_seconds ?? timeoutSeconds,
      context_window_hint: contextDraft.trim() ? Number(contextDraft) : existing?.context_window_hint ?? null,
      ...(existing ? { temperature: existing.temperature } : {}),
    });
  };
  const saveCandidate = async (modelId: string) => {
    const targetConnectionId = dialog?.kind === "models" ? dialog.connectionId : connectionId;
    if (!targetConnectionId) return;
    setBusy("save-models"); setError("");
    try {
      await persistCandidate(targetConnectionId, modelId);
      if (connectionRef.current === targetConnectionId) setNotice(text.modelsSaved);
      await reload(targetConnectionId);
    } catch (reason) { setError(errorMessage(reason, language)); } finally { setBusy(""); }
  };
  const addModels = async () => {
    if (!selectedModels.size) return;
    const targetConnectionId = dialog?.kind === "models" ? dialog.connectionId : connectionId;
    if (!targetConnectionId) return;
    setBusy("save-models"); setError("");
    try {
      for (const modelId of selectedModels) {
        if (models.some((model) => model.connection_id === targetConnectionId && model.model_id === modelId)) continue;
        await persistCandidate(targetConnectionId, modelId);
      }
      setSelectedModels(new Set()); setNotice(text.modelsSaved); await reload(); dismissDialog();
    } catch (reason) { setError(errorMessage(reason, language)); await reload(); }
    finally { setBusy(""); }
  };

  const saveModelTimeout = async (model: ConfiguredModelSummary) => {
    const timeout = normalizeTimeout(timeoutDrafts[model.configured_model_id] ?? String(model.timeout_seconds || 180));
    if (timeout === null) { setError(text.timeoutInvalid); return; }
    setBusy("timeout:" + model.configured_model_id); setError(""); setNotice("");
    try {
      await saveConfiguredModel({
        configured_model_id: model.configured_model_id,
        connection_id: model.connection_id,
        model_id: model.model_id,
        alias: model.alias,
        enabled: model.enabled,
        capabilities: model.capabilities,
        timeout_seconds: timeout,
        context_window_hint: model.context_window_hint,
        temperature: model.temperature,
      });
      setNotice(text.timeoutSaved);
      await reload();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(""); }
  };

  const replaceKey = (connection: ProviderConnectionSummary, trigger: HTMLElement) => {
    openDialog({ kind: "rotate", connection }, trigger);
  };

  const toggleConnection = async (connection: ProviderConnectionSummary) => {
    if (!beginConnectionBusy(connection.connection_id, "enable")) return;
    try { await setProviderConnectionEnabled(connection.connection_id, !connection.enabled); await reload(); }
    catch (reason) { setError(errorMessage(reason, language)); }
    finally { endConnectionBusy(connection.connection_id); }
  };

  const removeConnection = (connection: ProviderConnectionSummary, trigger: HTMLElement) => {
    openDialog({ kind: "delete-connection", connection }, trigger);
  };

  const testModel = async (model: ConfiguredModelSummary, vision = false) => {
    if (!beginConnectionBusy(model.connection_id, "test:" + model.configured_model_id)) return;
    const operationId = `model-test-${model.configuration_version}-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    const startedAt = Date.now();
    activeModelTestRef.current = { operationId, connectionId: model.connection_id, terminal: false };
    setModelTestProgress({ operationId, configuredModelId: model.configured_model_id, connectionId: model.connection_id, alias: model.alias, startedAt, lastSubstantiveAt: null, activity: "waiting", phase: "running" });
    setModelTestClock(startedAt); setError(""); setNotice("");
    let unlisten: (() => void) | undefined;
    let subscriptionReleased = false;
    const releaseSubscription = () => {
      if (!unlisten || subscriptionReleased) return;
      subscriptionReleased = true;
      unlisten();
      const current = activeModelTestRef.current;
      if (current?.operationId === operationId) current.unlisten = undefined;
    };
    try {
      unlisten = await subscribeModelTestEvents((event: ModelTestEvent) => {
        const current = activeModelTestRef.current;
        if (!current || current.operationId !== operationId || current.terminal) return;
        if (event.protocol !== "model-events-v1" || event.context.request_id !== operationId || event.context.attempt_id !== operationId || event.context.run_id !== model.configured_model_id || event.context.generation !== model.configuration_version) return;
        const eventState = typeof event.data.state === "string" ? event.data.state : "";
        const substantive = event.kind === "content" || eventState === "reasoning" || eventState === "content";
        if (event.kind === "result" || event.kind === "error") current.terminal = true;
        setModelTestProgress((previous) => {
          if (!previous || previous.operationId !== operationId) return previous;
          if (event.kind === "result") {
            const succeeded = event.data.ok === true;
            return { ...previous, phase: succeeded ? "succeeded" : "failed", message: succeeded ? text.testCompleted : text.testFailed, finishedAt: Date.now() };
          }
          if (event.kind === "error") return { ...previous, phase: "failed", message: localizedModelTestError(event.data.code, language) ?? text.testFailed, finishedAt: Date.now() };
          if (eventState === "heartbeat") return { ...previous, activity: "heartbeat" };
          const activity = event.kind === "content" || eventState === "content" ? "receiving"
            : eventState === "reasoning" ? "reasoning"
              : eventState === "connected" ? "connected" : "waiting";
          return { ...previous, activity, lastSubstantiveAt: substantive ? Date.now() : previous.lastSubstantiveAt };
        });
      });
      const current = activeModelTestRef.current;
      if (!current || current.operationId !== operationId) { releaseSubscription(); return; }
      current.unlisten = releaseSubscription;
      const result = await testConfiguredModel(model.configured_model_id, vision, operationId);
      if (activeModelTestRef.current?.operationId !== operationId) return;
      const finishedAt = Date.now();
      activeModelTestRef.current.terminal = true;
      if (result.ok) setNotice(text.testSucceeded);
      const capabilityChecks = result.capabilities as { status?: string; error_code?: unknown }[] | undefined;
      const capabilityErrors = [...new Set((capabilityChecks ?? [])
        .filter((check) => check.status === "error")
        .map((check) => localizedModelTestError(check.error_code, language))
        .filter((message): message is string => Boolean(message)))];
      const resultMessage = result.ok ? text.testCompleted : [text.testCapabilityFailed, ...capabilityErrors].join("\n");
      setModelTestProgress((previous) => previous?.operationId === operationId ? { ...previous, phase: result.ok ? "succeeded" : "failed", message: resultMessage, finishedAt } : previous);
      await reload();
    }
    catch (reason) {
      if (activeModelTestRef.current?.operationId === operationId) {
        const finishedAt = Date.now(); activeModelTestRef.current.terminal = true;
        setModelTestProgress((previous) => previous?.operationId === operationId ? { ...previous, phase: "failed", message: errorMessage(reason, language), finishedAt } : previous);
      }
    } finally { releaseSubscription(); endConnectionBusy(model.connection_id); }
  };

  const stopWaitingForModelTest = async () => {
    const current = activeModelTestRef.current;
    if (!current) return;
    activeModelTestRef.current = null;
    current.unlisten?.();
    const finishedAt = Date.now();
    setModelTestProgress((previous) => previous?.operationId === current.operationId ? { ...previous, phase: "stopped", message: text.testStopped, finishedAt } : previous);
    try { await cancelModelTest(current.operationId); }
    catch (reason) { setError(errorMessage(reason, language)); }
    finally { endConnectionBusy(current.connectionId); }
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
      } else if (current.kind === "delete-model") {
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
    : dialog?.kind === "delete-connection" ? text.deleteConnectionTitle
      : dialog?.kind === "delete-model" ? text.deleteModelTitle : text.manageModels;
  const dialogBody = dialog?.kind === "rotate"
    ? text.rotateBody
    : dialog?.kind === "delete-connection" ? text.confirmDelete
      : dialog?.kind === "delete-model" ? text.confirmDeleteModel : "";
  const dialogTarget = dialog?.kind === "delete-model"
    ? dialog.model.alias
    : dialog?.kind === "models" ? connections.find((item) => item.connection_id === dialog.connectionId)?.display_name
      : dialog?.kind === "rotate" || dialog?.kind === "delete-connection" ? dialog.connection.display_name : undefined;
  const visibleCandidates = discovered.filter((item) => {
    const normalizedQuery = modelQuery.trim().toLowerCase();
    return !normalizedQuery || `${item.alias} ${item.model_id}`.toLowerCase().includes(normalizedQuery);
  });

  const visibleConnections = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return connections.filter((item) => !normalizedQuery || `${item.display_name} ${item.endpoint} ${item.provider_id}`.toLowerCase().includes(normalizedQuery));
  }, [connections, query]);
  const currentProvider = provider || providers.find((item) => item.provider_id === activeConnection?.provider_id);
  const sameConnectionModels = dialog?.kind === "models" ? models.filter((item) => item.connection_id === dialog.connectionId) : [];
  const visibleSelectable = visibleCandidates.filter((item) => !sameConnectionModels.some((model) => model.model_id === item.model_id));

  return <div className="model-center">
    <header className="model-center-intro"><ShieldCheck size={20} aria-hidden="true" /><div><h1>{text.connectionsTitle}</h1><p>{text.intro}</p></div></header>
    {error && <div className="model-center-message" data-tone="error" role="alert">{error}</div>}
    {notice && <div className="model-center-message" role="status"><Check size={15} />{notice}</div>}

    <div className="model-center-shell" data-show-detail={Boolean(showForm || activeConnection) ? "true" : "false"}>
      <aside className="connection-list-pane" aria-label={text.connectionsTitle}>
        <div className="connection-list-header"><h2>{text.connectionsTitle}</h2><button className="primary-action" type="button" onClick={() => { const custom = providers.find((item) => item.provider_id === "custom"); if (custom) chooseProvider(custom); }}><Plus size={15} />{text.addConnection}</button></div>
        <label className="connection-search"><Search size={15} aria-hidden="true" /><input aria-label={text.connectionSearch} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text.connectionSearch} /></label>
        {showPresetPicker && <div className="preset-picker">
          <label>{text.providerPreset}<select aria-label={text.providerPreset} value={presetChoice} onChange={(event) => setPresetChoice(event.target.value)}><option value="">{text.choosePreset}</option>{providers.filter((item) => item.provider_id !== "custom").map((item) => <option key={item.provider_id} value={item.provider_id}>{item.display_name}</option>)}</select></label>
          <button type="button" disabled={!presetChoice} onClick={() => { const picked = providers.find((item) => item.provider_id === presetChoice); if (picked) chooseProvider(picked); }}>{text.startPreset}</button>
        </div>}
        <button className="secondary-button preset-toggle" type="button" onClick={() => setShowPresetPicker((value) => !value)}><Plus size={14} />{text.addPreset}</button>
        <div className="connection-list-scroll">
          {visibleConnections.map((connection) => {
            const itemProvider = providers.find((item) => item.provider_id === connection.provider_id);
            return <article key={connection.connection_id} className="connection-list-item" data-active={connectionId === connection.connection_id || undefined}>
              <button className="connection-list-select" type="button" aria-pressed={connectionId === connection.connection_id} onClick={() => selectConnection(connection)}>
                {itemProvider && <ProviderLogo provider={itemProvider} />}
                <span className="connection-list-copy"><strong>{connection.display_name}</strong><small>{connection.endpoint}</small><span className="connection-list-status"><span className="connection-status" data-status={connection.enabled ? connection.status || "untested" : "disabled"}>{statusLabel(connection, text)}</span><span>{connection.enabled ? text.enabledState : text.disabled}</span></span></span>
              </button>
              <div className="connection-list-actions"><button type="button" disabled={busyForConnection(connection.connection_id)} onClick={() => editConnection(connection)}>{text.editConnection}</button><button type="button" disabled={busyForConnection(connection.connection_id)} aria-label={`${text.delete}: ${connection.display_name}`} onClick={(event) => removeConnection(connection, event.currentTarget)}><Trash2 size={13} /></button></div>
            </article>;
          })}
          {!visibleConnections.length && <p className="model-empty">{connections.length ? text.noProviders : text.emptyConnections}</p>}
        </div>
      </aside>

      <main className="model-detail-pane" data-show-detail={Boolean(showForm || activeConnection) || undefined}>
        {showForm && currentProvider ? <section className="model-detail-content" role="region" aria-label={`${text.newConnection}: ${currentProvider.provider_id === "custom" ? text.compatibleApi : currentProvider.display_name}`}>
          <header className="model-detail-heading"><div className="model-detail-title"><ProviderLogo provider={currentProvider} /><div><h2>{draftConnectionId ? displayName || text.editConnection : currentProvider.provider_id === "custom" ? text.compatibleApi : currentProvider.display_name}</h2><p>{draftConnectionId ? text.editConnection : text.newConnection}</p></div></div>
            {activeConnection && <button className="model-back-button" type="button" onClick={() => setShowForm(false)}>{text.cancelEdit}</button>}
            <button className="model-back-button model-mobile-back" type="button" onClick={() => { setShowForm(false); setConnectionId(""); connectionRef.current = ""; }}>{text.back}</button>
          </header>
          <div className="model-detail-scroll">
            {provider?.help_url && <button className="text-link" type="button" onClick={() => void openExternalUrl(provider.help_url)}>{text.registration}<ExternalLink size={14} /></button>}
            <fieldset className="connection-form" disabled={busy === "save-connection"}>
              <label>{text.displayName}<input aria-label={text.displayName} placeholder={defaultConnectionName(endpoint, currentProvider.display_name)} value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
              <label>{text.endpoint}<input aria-label={text.endpoint} placeholder="https://api.example.com" value={endpoint} onChange={(event) => { ignoreActiveModelTest(); invalidateDiscovery(); setEndpoint(event.target.value); }} spellCheck={false} /></label>
              {(currentProvider.requires_api_key || currentProvider.provider_id === "custom") && <label>{text.apiKey}<input aria-label={text.apiKey} type="password" value={secret} onChange={(event) => setSecret(event.target.value)} autoComplete="new-password" placeholder={draftConnectionId && activeConnection?.has_secret ? "••••••••" : ""} /><small>{text.apiKeyHint}</small></label>}
              {provider?.provider_id === "ollama" && <p className="privacy-note">{/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::|\/|$)/i.test(endpoint.trim()) ? text.localPrivacy : text.remotePrivacy}</p>}
            </fieldset>
            <details className="connection-advanced"><summary>{text.advanced}</summary>
              {currentProvider.provider_id === "qwen" && <label>{text.site}<select value={endpoint.includes("dashscope-intl") ? "international" : "cn"} onChange={(event) => { const intl = event.target.value === "international"; setEndpoint(intl ? "https://dashscope-intl.aliyuncs.com/compatible-mode/v1" : "https://dashscope.aliyuncs.com/compatible-mode/v1"); setRegion(intl ? "international" : "cn"); }}><option value="cn">中国站 · China</option><option value="international">国际站 · International</option></select><small>{text.siteHint}</small></label>}
              <label className="advanced-region">{text.region}<input value={region} onChange={(event) => setRegion(event.target.value)} /></label>
              <ConnectionOptions language={language} endpoint={endpoint} protocol={protocol} profile={profile} transport={transport} protocolLocked={currentProvider.provider_id !== "custom"} onProtocol={(value) => { ignoreActiveModelTest(); invalidateDiscovery(); setProtocol(value); setProfile(compatibleDefaultProfile(value)); setOverrideContract(true); }} onChange={(value) => { setProfile(value); setOverrideContract(true); }} onTransport={(value) => { ignoreActiveModelTest(); invalidateDiscovery(); setTransport(value); }} onCompatibleDefault={() => { ignoreActiveModelTest(); setProfile(compatibleDefaultProfile(protocol)); setOverrideContract(true); }} />
            </details>
          </div>
          <footer className="model-detail-footer connection-save-bar">
            <button type="button" disabled={busy === "save-connection"} onClick={() => { setShowForm(false); setError(""); if (!draftConnectionId) { setProviderId(null); connectionRef.current = ""; } }}>{text.cancelEdit}</button>
            <button type="button" disabled={busy === "save-connection"} onClick={() => void saveConnection(false)}>{busy === "save-connection" ? text.saving : text.saveOnly}</button>
            <button className="primary-action" type="button" disabled={busy === "save-connection"} onClick={() => void saveConnection(true)}>{busy === "save-connection" ? text.saving : text.saveAndGetModels}</button>
          </footer>
        </section> : activeConnection && provider ? <section className="model-detail-content" role="region" aria-label={`Connection details: ${activeConnection.display_name}`}>
          <header className="model-detail-heading"><div className="model-detail-title"><ProviderLogo provider={provider} /><div><h2>{activeConnection.display_name}</h2><p>{activeConnection.endpoint}</p></div></div>
            <div className="model-detail-heading-actions"><span className="connection-status" data-status={activeConnection.enabled ? activeConnection.status || "untested" : "disabled"}>{statusLabel(activeConnection, text)}</span><button type="button" disabled={busyForConnection(activeConnection.connection_id)} onClick={() => void toggleConnection(activeConnection)}>{activeConnection.enabled ? text.disable : text.enable}</button>{provider.requires_api_key && <button type="button" disabled={busyForConnection(activeConnection.connection_id)} onClick={(event) => replaceKey(activeConnection, event.currentTarget)}>{text.replaceKey}</button>}<button type="button" disabled={busyForConnection(activeConnection.connection_id)} aria-label={`${text.delete}: ${activeConnection.display_name}`} onClick={(event) => removeConnection(activeConnection, event.currentTarget)}><Trash2 size={14} /></button></div>
            <button className="model-back-button model-mobile-back" type="button" onClick={() => { connectionRef.current = ""; setConnectionId(""); }}>{text.back}</button>
          </header>
          <div className="model-detail-scroll">
            {provider.help_url && <button className="text-link" type="button" onClick={() => void openExternalUrl(provider.help_url)}>{text.registration}<ExternalLink size={14} /></button>}
            <div className="model-panel-section"><div className="panel-section-title"><h3>{text.models}</h3><div className="model-toolbar-actions"><button type="button" disabled={Boolean(connectionBusy[activeConnection.connection_id]) || !canDiscoverModels} onClick={(event) => void runDiscovery(activeConnection.connection_id, true, event.currentTarget)}><RefreshCw size={14} />{connectionBusy[activeConnection.connection_id] === "discover" ? text.discovering : text.getModels}</button></div></div>
              <p className="field-caption">{text.listHint} {activeConnection.has_secret ? text.credentialSaved : text.apiKey}</p>
              <div className="manual-model-row"><label>{text.manualModel}<input aria-label={text.manualModel} value={manualModel} onChange={(event) => setManualModel(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addManual(event.currentTarget); } }} /></label><button type="button" disabled={!manualModel.trim()} onClick={(event) => addManual(event.currentTarget)}>{text.addManual}</button></div>
            </div>
            <div className="model-panel-section">
              <div className="panel-section-title"><h3>{text.models}</h3></div>
              <label className="model-timeout-editor">{text.context}<input type="number" min={1} max={2147483647} value={contextDraft} onChange={(event) => setContextDraft(event.target.value)} placeholder={text.unknown} /></label>
              <p className="field-caption">{text.testHint}</p>
              {modelTestProgress?.connectionId === activeConnection.connection_id && <div className="model-test-progress" role="status" aria-live="polite" data-phase={modelTestProgress.phase}>
                <strong>{modelTestProgress.alias}</strong>
                <span>{modelTestProgress.phase === "running"
                  ? modelTestProgress.activity === "reasoning" ? text.testReasoning : modelTestProgress.activity === "receiving" ? text.testReceiving : modelTestProgress.activity === "heartbeat" ? text.testHeartbeat : modelTestProgress.activity === "connected" ? text.testConnected : text.testWaiting
                  : modelTestProgress.phase === "stopped" ? text.testStopped : modelTestProgress.phase === "failed" ? text.testFailed : text.testCompleted}</span>
                <small>{text.testElapsed}: {elapsedLabel((modelTestProgress.finishedAt ?? modelTestClock) - modelTestProgress.startedAt)}</small>
                <small>{text.testSinceActivity}: {modelTestProgress.lastSubstantiveAt === null ? text.testNoActivity : elapsedLabel((modelTestProgress.finishedAt ?? modelTestClock) - modelTestProgress.lastSubstantiveAt)}</small>
                {modelTestProgress.message && modelTestProgress.phase === "failed" && <small>{modelTestProgress.message}</small>}
                {modelTestProgress.phase === "running" && <button type="button" onClick={() => void stopWaitingForModelTest()}>{text.stopWaiting}</button>}
              </div>}
              <div className="configured-model-list">
                {connectionModels.map((model) => <article key={model.configured_model_id}>
                  <div><strong>{model.alias}</strong><code className="raw-model-id">{model.model_id}</code><small>{costLabel(model, text)} · {text.status}: {statusLabel({ enabled: model.enabled, status: model.health_status }, text)}</small><small>{text.context}: {model.context_window_hint ?? text.unknown}</small><small>{text.capabilities}: {model.capability_checks?.length ? model.capability_checks.map((check) => `${check.capability === "structured_json" ? text.promptJson : check.capability === "vision" ? text.visionBadge : text.textCapability}: ${check.status === "ready" ? text.ready : check.status === "error" ? text.unavailable : text.unknown}`).join(" · ") : text.unknown}</small></div>
                  <label className="model-timeout-editor">{text.timeout}<input aria-label={`${text.timeout}: ${model.alias}`} type="number" min={5} max={600} step={1} value={timeoutDrafts[model.configured_model_id] ?? String(model.timeout_seconds || 180)} onChange={(event) => setTimeoutDrafts((current) => ({ ...current, [model.configured_model_id]: event.target.value }))} /><small>{text.timeoutHint}</small><button type="button" disabled={busyForModel(model.configured_model_id)} onClick={() => void saveModelTimeout(model)}>{busy === "timeout:" + model.configured_model_id ? text.working : text.saveTimeout}</button></label>
                  <div className="configured-model-actions"><button type="button" aria-label={`${text.test}: ${model.alias}`} title={text.testHint} disabled={busyForModel(model.configured_model_id) || !activeConnection.enabled} onClick={() => void testModel(model)}>{connectionBusy[model.connection_id] === "test:" + model.configured_model_id ? text.testing : text.test}</button>{model.capabilities.includes("vision") && <button type="button" aria-label={`${text.visionTest}: ${model.alias}`} disabled={busyForModel(model.configured_model_id) || !activeConnection.enabled} onClick={() => void testModel(model, true)}>{text.visionTest}</button>}<button type="button" aria-label={`${text.delete}: ${model.alias}`} disabled={busyForModel(model.configured_model_id)} onClick={(event) => removeModel(model, event.currentTarget)}><Trash2 size={14} /></button></div>
                </article>)}
                {!connectionModels.length && <p className="model-empty">{text.noModels}</p>}
              </div>
            </div>
            <details className="model-directory"><summary>{text.directory}</summary>
              <p className="field-caption">{text.directoryHint}</p><button type="button" disabled={busy === "directory"} onClick={() => { setBusy("directory"); void getModelDirectory(true).then(setDirectory).catch((reason) => setError(errorMessage(reason, language))).finally(() => setBusy("")); }}>{text.refreshDirectory}</button>
              {directory && <><small>{directory.source} · {directory.retrieved_at}</small><select aria-label={text.directory} value={directoryRoute} onChange={(event) => setDirectoryRoute(event.target.value)}><option value="">{text.unknown}</option>{Array.from(new Set(directory.models.map((item) => item.provider_id))).sort().map((id) => <option key={id} value={id}>{id}</option>)}</select>
                {directoryEntry && <p>{directoryEntry.name} · {text.context}: {directoryEntry.context ?? text.unknown} · {text.output}: {directoryEntry.output ?? text.unknown} · USD/1M: {directoryEntry.input_price ?? "?"} / {directoryEntry.output_price ?? "?"} {directoryEntry.context && <button type="button" onClick={() => setContextDraft(String(directoryEntry.context))}>{text.useContext}</button>}</p>}
              </>}
            </details>
          </div>
        </section> : <section className="model-detail-empty" aria-label={text.detailEmpty}><p>{text.detailEmpty}</p></section>}
      </main>
    </div>

    {dialog && <div className="history-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy.startsWith("dialog:")) dismissDialog(); }}>
      <section ref={dialogRef} className={`history-dialog model-center-dialog${dialog.kind === "models" ? " model-picker-dialog" : ""}`} role="dialog" aria-modal="true" aria-labelledby="model-dialog-title" aria-describedby={dialog.kind === "models" ? undefined : "model-dialog-description"}>
        <div className="model-dialog-header"><div><small>{dialogTarget}</small><h2 id="model-dialog-title">{dialogTitle}</h2></div><button className="model-icon-button" type="button" aria-label={text.close} disabled={busy.startsWith("dialog:")} onClick={dismissDialog}><X size={18} /></button></div>
        {dialog.kind === "models" ? <>
          <div className="model-picker-toolbar">
            <label className="model-picker-search"><Search size={15} aria-hidden="true" /><input autoFocus aria-label={text.searchModels} value={modelQuery} onChange={(event) => setModelQuery(event.target.value)} placeholder={text.searchModels} /></label>
            <div className="manual-model-row model-picker-manual"><label>{text.manualModel}<input aria-label={`${text.manualModel} in model picker`} value={manualModel} onChange={(event) => setManualModel(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); queueManualCandidate(); } }} /></label><button type="button" disabled={!manualModel.trim()} onClick={queueManualCandidate}>{text.addManual}</button></div>
            <div className="model-picker-selection-tools"><button type="button" disabled={!visibleSelectable.length} onClick={() => setSelectedModels((current) => new Set([...current, ...visibleSelectable.map((item) => item.model_id)]))}>{text.selectVisible}</button><button type="button" disabled={!visibleSelectable.some((item) => selectedModels.has(item.model_id))} onClick={() => setSelectedModels((current) => { const next = new Set(current); visibleSelectable.forEach((item) => next.delete(item.model_id)); return next; })}>{text.clearVisible}</button></div>
          </div>
          <div className="model-picker-list" role="region" aria-label={text.selectModels}>
            {visibleCandidates.map((candidate) => {
              const existing = sameConnectionModels.find((item) => item.model_id === candidate.model_id);
              const catalog = directory?.models.find((item) => item.provider_id === directoryRoute && item.model_id === candidate.model_id);
              return <article key={candidate.model_id} className="model-picker-row">
                <label className="model-picker-choice"><input type="checkbox" aria-label={`${text.selectModel}: ${candidate.alias}`} disabled={Boolean(existing)} checked={selectedModels.has(candidate.model_id)} onChange={(event) => setSelectedModels((current) => { const next = new Set(current); if (event.target.checked) next.add(candidate.model_id); else next.delete(candidate.model_id); return next; })} /><span><strong>{candidate.alias}</strong><code className="raw-model-id">{candidate.model_id}</code><small>{candidate.source === "api" ? text.apiListSource : text.manualSource} · {catalog ? `${text.catalogSource} (${directory?.source})` : costLabel(candidate, text)}</small>{catalog && <small>{text.context}: {catalog.context ?? text.unknown} · {text.output}: {catalog.output ?? text.unknown}</small>}</span></label>
                <label className="model-capability-check"><input type="checkbox" aria-label={`${text.visionCapability}: ${candidate.alias}`} checked={visionModelIds.has(candidate.model_id)} onChange={(event) => setVisionModelIds((current) => { const next = new Set(current); if (event.target.checked) next.add(candidate.model_id); else next.delete(candidate.model_id); return next; })} /><span>{text.visionCapability}</span></label>
                <button type="button" disabled={Boolean(existing) || busy === "save-models"} onClick={() => void saveCandidate(candidate.model_id)}>{existing ? text.alreadyAdded : text.addOneModel}</button>
              </article>;
            })}
            {!visibleCandidates.length && <p className="model-empty">{text.emptyModelList}</p>}
          </div>
          <footer className="model-picker-footer"><label className="model-timeout-editor">{text.timeout}<input aria-label={text.timeout} type="number" min={5} max={600} step={1} value={newModelTimeout} onChange={(event) => setNewModelTimeout(event.target.value)} /><small>{text.timeoutHint}</small></label><button type="button" className="secondary-button" onClick={dismissDialog}>{text.cancel}</button><button type="button" className="primary-action" disabled={!selectedModels.size || busy === "save-models"} onClick={() => void addModels()}>{busy === "save-models" ? text.saving : `${text.addModels}${selectedModels.size ? ` (${selectedModels.size})` : ""}`}</button></footer>
        </> : <>
          <p id="model-dialog-description">{dialogBody}</p>
          {dialog.kind === "rotate" && <label className="model-dialog-secret">{text.apiKey}<input autoFocus aria-label={text.apiKey} type="password" value={dialogSecret} onChange={(event) => setDialogSecret(event.target.value)} autoComplete="new-password" /><small>{text.apiKeyHint}</small></label>}
          <div className="history-dialog-actions"><button autoFocus={dialog.kind !== "rotate"} className="secondary-button" type="button" disabled={busy.startsWith("dialog:")} onClick={dismissDialog}>{text.cancel}</button><button className={dialog.kind === "rotate" ? "primary-button" : "danger-button"} type="button" disabled={busy.startsWith("dialog:") || (dialog.kind === "rotate" && !dialogSecret.trim())} onClick={() => void confirmDialog()}>{busy.startsWith("dialog:") ? text.working : dialog.kind === "rotate" ? text.confirmReplace : text.confirm}</button></div>
        </>}
      </section>
    </div>}
  </div>;
}



