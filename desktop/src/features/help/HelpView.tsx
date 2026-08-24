import { BookOpenCheck, Boxes, ShieldCheck } from "lucide-react";

import type { Language } from "../../types";

type HelpArticle = {
  id: string;
  label: string;
  title: string;
  summary: string;
  steps: Array<{ title: string; body: string }>;
  note: string;
  code?: string;
};

const OT_EXAMPLE = `company-research.ot
├── manifest.json
├── ot.lock.json
└── resources/
    ├── workflow.json
    ├── output.json
    ├── ui-form.json
    └── prompts/
        ├── company-analysis.md
        └── verification.md

核心约束
- canonical JSON + SHA-256 content identity
- resources 必须逐项声明大小、媒体类型与哈希
- network: []
- filesystem: "none"
- execute_code: false
- secrets: "prohibited"`;

const CONTENT: Record<Language, HelpArticle[]> = {
  "zh-CN": [
    {
      id: "company-research",
      label: "入门",
      title: "从模型配置到第一份研究报告",
      summary: "2.0 把模型账户配置与研究任务分开：先建立安全、可复用的模型引用，再发起可复现研究。",
      steps: [
        { title: "先打开模型中心", body: "选择服务商卡片并添加连接。云端连接的 API Key 只在保存或替换时输入，之后由 Windows 凭据管理器保管；OpenThesis 不会在页面、SQLite、日志或研究记录中回显它。" },
        { title: "添加并测试模型", body: "一个连接可以添加多个模型。支持发现模型的服务商可在线读取目录，也可以手动填写模型 ID。只有启用且测试为“可用”的模型，才会出现在研究页和 OT 创作工作室。" },
        { title: "本地 Ollama 不随软件安装", body: "如果电脑上已经安装并启动 Ollama，可添加本地连接并发现现有模型。OpenThesis 不内置 Ollama、不下载模型，也不会用安装包隐藏增加模型体积。" },
        { title: "选择公司、市场与模型", body: "在“发起研究”中选择美股、A 股或港股，再选择主模型和零到多个比较模型。SEC EDGAR 仅在研究美股时需要真实联系邮箱；任务页不再接受 API Key 或 Endpoint。" },
        { title: "检查可复现性记录", body: "报告会保留模型配置版本、研究配置、数据快照哈希、证据数量、研究包 ID/版本/内容身份。记录用于追踪一次运行，不包含凭据明文。" },
      ],
      note: "建议先运行合成演示验证界面和报告导出，再连接真实数据与可能收费的模型。OpenThesis 是研究软件，不自动交易，也不保证投资结果。",
    },
    {
      id: "ot-studio",
      label: "OT 生态",
      title: "用 OT 创作工作室编写 .ot",
      summary: ".ot 是 OpenThesis 2.0 的声明式、可验证研究包。新手用表单和滑块，专业用户可直接编辑 JSON。",
      steps: [
        { title: "从目标开始", body: "在“OT 创作工作室”填写研究目标、期限、深度、风险偏好和报告语言。自然语言目标只帮助建立草稿，不会绕过结构化校验。" },
        { title: "设计工作流", body: "为每一步设置稳定 ID、角色、依赖、Prompt 与输出 Schema。多 Agent 本身不是目标；重要的是每一步都在同一套证据和确定性事实边界内工作。" },
        { title: "选择性使用模型辅助", body: "可从已经配置并测试可用的模型中选择 OT 助手。助手一次只能建议一个允许字段，结果先显示前后差异，必须由用户接受后才写入草稿。小模型不负责重写整个文件。" },
        { title: "验证、编译与导出", body: "验证器先检查 ID、依赖、秘密和结构；编译器再生成 canonical JSON、资源哈希、锁文件和稳定内容身份。导出的文件必须使用 .ot 扩展名。" },
        { title: "理解兼容边界", body: "2.0 不打开、导入、运行或通用转换用户/第三方 .othesis。项目自有的旧官方包已通过受控转换器生成官方 .ot，并保留源哈希和语义等价校验。" },
      ],
      note: ".ot 不授予网络、文件系统或任意代码执行权限，也不能携带 API Key、Token 或密码。未知必需能力在执行时会被拒绝。",
      code: OT_EXAMPLE,
    },
    {
      id: "markets",
      label: "市场与证据",
      title: "研究美股、A 股与港股",
      summary: "先确定上市证券，再从官方披露取得证据；模型不能替代缺失的财务事实。",
      steps: [
        { title: "区分上市证券", body: "同一发行人的 A 股与 H 股作为不同证券保存，交易所、币种与披露历史不混用。A 股覆盖上交所、深交所和北交所；港股覆盖港交所主板与 GEM。" },
        { title: "核对官方来源", body: "美股使用 SEC EDGAR，A 股使用法定披露入口，港股使用披露易。来源不可用或财务组验证失败时，流程会在调用模型前停止，不用模型补造数字。" },
        { title: "让确定性工作保持确定性", body: "财务汇总、期间选择、币种校验和适用的估值计算由代码完成；LLM 用于定性推理、连接证据、识别风险和情景分析。" },
        { title: "手动行情有明确来源", body: "当前不自动抓取实时价格。用户输入的价格、市值、币种与日期会标记为手动行情快照，不会冒充官方财报事实。" },
        { title: "理解 Beta 边界", body: "银行、保险和券商可进行财报、商业、风险与情景研究，但它们的资产负债结构不同，当前不使用普通公司的标准自由现金流反向 DCF。" },
      ],
      note: "证据、确定性分析、Agent 输出、综合与验证是不同层。最终报告仍需人工判断，不构成投资建议。",
    },
  ],
  en: [
    {
      id: "company-research",
      label: "Getting started",
      title: "From model setup to a first research report",
      summary: "Version 2.0 separates account setup from research: configure a secure reusable model reference first, then start a reproducible run.",
      steps: [
        { title: "Open Model Center first", body: "Choose a provider and add a connection. A cloud API key is entered only when it is saved or replaced, then Windows Credential Manager owns it. It is never returned to the page or written to SQLite, logs, or research records." },
        { title: "Add and test models", body: "A connection may expose multiple models. Discover the remote catalog where supported, or enter a model ID manually. Only enabled models that pass a connection test appear in Research and OT Studio." },
        { title: "Ollama remains your installation", body: "Connect an Ollama service that is already installed and running on your computer. OpenThesis does not bundle Ollama, download model weights, or hide a model runtime in the installer." },
        { title: "Choose the company, market, and model roles", body: "Select US, mainland China, or Hong Kong, then choose one primary model and zero or more comparison models. SEC EDGAR needs a real contact email only for US research. The task page no longer accepts keys or endpoints." },
        { title: "Inspect the reproducibility record", body: "A report records model configuration versions, research settings, data-snapshot hashes, evidence counts, and the pack ID, version, and content identity. Credentials are excluded." },
      ],
      note: "Run the synthetic demo before connecting real data or a model that may incur charges. OpenThesis is research software: it does not trade and does not promise investment outcomes.",
    },
    {
      id: "ot-studio",
      label: "OT ecosystem",
      title: "Author .ot files in OT Studio",
      summary: ".ot is the declarative and verifiable package format in OpenThesis 2.0. Beginners use guided controls; advanced authors can edit JSON directly.",
      steps: [
        { title: "Start with the research goal", body: "Set the goal, horizon, depth, risk emphasis, and report language. Natural-language input helps shape a draft but never bypasses structured validation." },
        { title: "Design the workflow", body: "Give every step a stable ID, role, dependencies, prompt, and output schema. The agent count is not the point: each step shares the same bounded evidence and deterministic fact layer." },
        { title: "Use model assistance selectively", body: "Choose an already configured and tested OT-assistant model. It can suggest one allowed field at a time. OT Studio shows the before/after diff and applies nothing until you accept it; a small free model is never asked to rewrite the whole file." },
        { title: "Validate, compile, and export", body: "Validation checks IDs, dependencies, structure, and secrets. Compilation creates canonical JSON, resource hashes, a lock file, and a stable content identity. Exports always use the .ot extension." },
        { title: "Know the compatibility boundary", body: "Version 2.0 does not open, import, execute, or generally convert user or third-party .othesis files. The project's own old official pack is converted by a controlled tool with source hashing and semantic-equivalence checks." },
      ],
      note: ".ot cannot request network, filesystem, arbitrary-code, or secret permissions. An unknown required capability blocks execution.",
      code: OT_EXAMPLE,
    },
    {
      id: "markets",
      label: "Markets and evidence",
      title: "Research US, mainland China, and Hong Kong listings",
      summary: "Resolve the listed security first, then collect official evidence. A model is never used as a substitute for missing financial facts.",
      steps: [
        { title: "Keep listings distinct", body: "A- and H-share listings of one issuer remain separate securities with their own exchange, currency, and disclosure history. Mainland coverage includes SSE, SZSE, and BSE; Hong Kong includes Main Board and GEM." },
        { title: "Verify the official source", body: "US research uses SEC EDGAR, mainland research uses statutory disclosure entries, and Hong Kong uses HKEXnews. If the source or validated financial group is unavailable, the workflow stops before model creation." },
        { title: "Keep deterministic work deterministic", body: "Code handles aggregation, period selection, currency checks, and applicable valuation calculations. LLMs handle qualitative reasoning, evidence connections, risk discovery, and scenarios." },
        { title: "Label manual market data", body: "Live prices are not fetched automatically. User-supplied price, market cap, currency, and date remain an explicit manual snapshot rather than filing facts." },
        { title: "Understand Financials Beta", body: "Banks, insurers, and brokers can use filing, business, risk, and scenario research, but their balance sheets differ from ordinary companies and standard FCF reverse DCF is not applied." },
      ],
      note: "Evidence, deterministic analysis, agent output, synthesis, and verification are separate layers. The final report still requires human judgment and is not investment advice.",
    },
  ],
  "zh-Hant": [
    {
      id: "company-research",
      label: "入門",
      title: "從模型設定到第一份研究報告",
      summary: "2.0 將模型帳戶設定與研究任務分開：先建立安全、可重用的模型引用，再發起可重現研究。",
      steps: [
        { title: "先開啟模型中心", body: "選擇服務商並新增連線。雲端 API Key 只在儲存或替換時輸入，之後由 Windows 認證管理員保管；頁面、SQLite、日誌和研究記錄都不會回顯金鑰。" },
        { title: "新增並測試模型", body: "一個連線可以新增多個模型。可探索遠端目錄或手動填寫模型 ID；只有啟用且測試為可用的模型才會出現在研究頁與 OT 創作工作室。" },
        { title: "本機 Ollama 不隨軟體安裝", body: "可連接電腦上已安裝並啟動的 Ollama。OpenThesis 不內建 Ollama、不下載模型權重，也不在安裝包中隱藏推理執行環境。" },
        { title: "選擇公司、市場與模型角色", body: "選擇美股、A 股或港股，再選一個主模型與零到多個比較模型。任務頁不再接受 API Key 或 Endpoint。" },
        { title: "檢查可重現記錄", body: "報告保留模型設定版本、研究設定、資料快照雜湊、證據數量與研究包內容身分，但不包含憑據。" },
      ],
      note: "建議先執行合成示範。OpenThesis 是研究軟體，不自動交易，也不保證投資結果。",
    },
    {
      id: "ot-studio",
      label: "OT 生態",
      title: "在 OT 創作工作室編寫 .ot",
      summary: ".ot 是 OpenThesis 2.0 的宣告式、可驗證研究包；新手使用表單，專業作者可直接編輯 JSON。",
      steps: [
        { title: "從研究目標開始", body: "設定目標、期限、深度、風險偏好與報告語言。自然語言只協助建立草稿，不會略過結構化驗證。" },
        { title: "設計工作流", body: "為每一步設定穩定 ID、角色、依賴、Prompt 與輸出 Schema。各步驟共享相同的證據與確定性事實邊界。" },
        { title: "選擇性使用模型輔助", body: "助手一次只能建議一個允許欄位；差異會先顯示，必須接受後才寫入。小模型不會被要求重寫整個檔案。" },
        { title: "驗證、編譯與匯出", body: "驗證器檢查 ID、依賴、秘密與結構；編譯器生成 canonical JSON、資源雜湊、鎖檔與穩定內容身分。" },
        { title: "理解相容邊界", body: "2.0 不開啟、匯入、執行或通用轉換使用者／第三方 .othesis；只受控轉換專案自己的舊官方包。" },
      ],
      note: ".ot 不授予網路、檔案系統或任意程式執行權限，也不能攜帶 API Key、Token 或密碼。",
      code: OT_EXAMPLE,
    },
    {
      id: "markets",
      label: "市場與證據",
      title: "研究美股、A 股與港股",
      summary: "先確定上市證券，再從官方披露取得證據；模型不能替代缺失的財務事實。",
      steps: [
        { title: "區分上市證券", body: "同一發行人的 A 股與 H 股作為不同證券保存，交易所、幣種與披露歷史不混用。" },
        { title: "核對官方來源", body: "美股使用 SEC EDGAR，A 股使用法定披露入口，港股使用披露易；來源或驗證失敗時會在呼叫模型前停止。" },
        { title: "讓確定性工作保持確定性", body: "財務彙總、期間選擇、幣種校驗與適用的估值計算由程式完成，LLM 用於定性推理與風險分析。" },
        { title: "標記手動行情", body: "目前不自動抓取即時價格；使用者輸入的行情與日期會保留為手動快照。" },
        { title: "理解金融機構 Beta", body: "銀行、保險與券商不使用普通公司的標準自由現金流反向 DCF。" },
      ],
      note: "最終報告仍需人工判斷，不構成投資建議。",
    },
  ],
};

export function HelpView({ language, copy }: { language: Language; copy: { helpBody: string } }) {
  const articles = CONTENT[language] ?? CONTENT.en;
  return (
    <div className="help-view">
      <header className="section-intro help-intro">
        <p>{copy.helpBody}</p>
        <nav aria-label={language === "en" ? "Help articles" : language === "zh-Hant" ? "說明文章" : "帮助文章"}>
          {articles.map((article) => <a key={article.id} href={`#help-${article.id}`}>{article.title}</a>)}
        </nav>
      </header>
      <div className="help-articles">
        {articles.map((article, articleIndex) => (
          <article id={`help-${article.id}`} className="help-article" key={article.id}>
            <header>
              <span className="help-icon" aria-hidden="true">
                {articleIndex === 0 ? <BookOpenCheck size={20} /> : articleIndex === 1 ? <Boxes size={20} /> : <ShieldCheck size={20} />}
              </span>
              <div><span className="eyebrow">{article.label}</span><h2>{article.title}</h2><p>{article.summary}</p></div>
            </header>
            <ol>
              {article.steps.map((step) => <li key={step.title}><span>{step.title}</span><p>{step.body}</p></li>)}
            </ol>
            {article.code && <pre><code>{article.code}</code></pre>}
            <aside><ShieldCheck size={17} /><p>{article.note}</p></aside>
          </article>
        ))}
      </div>
    </div>
  );
}
