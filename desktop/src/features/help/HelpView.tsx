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

const PACK_EXAMPLE = `my.company-research.othesis
├── manifest.yaml
├── workflow.yaml
└── prompts/
    └── company-analyst.md

manifest.yaml  (JSON-compatible syntax)
{
  "api_version": "openthesis.io/v1alpha1",
  "kind": "ResearchPack",
  "metadata": {
    "id": "community.company-research",
    "name": "Company Research",
    "version": "0.1.0"
  },
  "permissions": {
    "network": false,
    "filesystem": false,
    "execute_code": false
  }
}

workflow.yaml
{
  "workflow": {
    "id": "company-research",
    "version": "0.1.0",
    "steps": [
      {
        "id": "company",
        "agent": "company-analyst",
        "prompt": "prompts/company-analyst.md"
      }
    ]
  }
}`;

const CONTENT: Partial<Record<Language, HelpArticle[]>> = {
  "zh-CN": [
    {
      id: "company-research",
      label: "入门",
      title: "如何开始一家公司研究",
      summary: "从市场与公司选择到模型与研究模块，用一条可复现的流程生成第一份长期研究报告。",
      steps: [
        { title: "选择市场与数据身份", body: "进入“发起研究”并选择美股、A 股或港股。只有美股 SEC EDGAR 需要填写能联系到你本人的邮箱；A/港股使用各自的法定披露平台。" },
        { title: "选择真实公司", body: "输入股票代码或公司名称，也可以使用常用公司快捷入口。确认公司名称、完整代码、上市市场和交易所后再继续。" },
        { title: "决定是否调用 AI", body: "选择“不调用 AI”可先生成确定性财务概览；选择模型提供方时，填写会话级 API Key、模型名称和接口地址。API Key 不会保存到本机数据库。" },
        { title: "选择研究模块与运行方式", body: "默认模块覆盖财务、商业模式、会计风险、增长机会、反方审查和长期情景。基础 Agent 默认顺序运行；只有供应商和网络稳定时才建议手动打开并行。" },
        { title: "开始并阅读报告", body: "点击“开始研究”后可查看逐 Agent 进度并随时请求取消。完成后可缩放、进入专注模式、查看技术详情或导出 HTML；所有结论仍需由你独立判断。" },
      ],
      note: "建议第一次先运行合成演示，确认界面、报告阅读和导出流程正常，再使用真实公司数据与付费模型。",
    },
    {
      id: "research-pack",
      label: "扩展",
      title: "编写自己的研究模块",
      summary: "研究模块是扩展名为 .othesis 的声明式 ZIP 包，由清单、工作流和 Prompt 文件组成，不执行任意代码。",
      steps: [
        { title: "建立最小目录", body: "包根目录必须包含 manifest.yaml 与 workflow.yaml。Prompt 放在 prompts/ 中；当前版本只接受 .yaml、.yml、.json、.md 和 .txt 文件。" },
        { title: "编写清单", body: "manifest.yaml 当前必须采用 JSON 兼容的 YAML 语法，并提供 kind=ResearchPack 以及 metadata.id、name、version。同一 ID 和版本的内容不能被覆盖，修改后请提升版本号。" },
        { title: "声明工作流", body: "workflow.yaml 的每个步骤应有稳定的 id、agent 和 prompt 路径；后续步骤可通过 depends_on 引用前置步骤。所有引用的 Prompt 文件必须真实存在。" },
        { title: "约束 Prompt 输出", body: "Prompt 应明确要求结构化 JSON、证据不足时显式说明、事实引用证据 ID，并避免把推断写成事实。OpenThesis 会在运行时追加不可覆盖的报告语言约束。" },
        { title: "打包与导入", body: "把目录内容压缩为 ZIP 后将扩展名改为 .othesis，再在“发起研究”的研究模块区域导入。包不得超过 10 MB，单个文件不得超过 2 MB，也不能包含脚本、可执行文件或路径穿越。" },
      ],
      note: "当前声明式模块不允许 network、filesystem 或 execute_code 权限。需要外部数据或代码执行的模块会被安全拒绝。",
      code: PACK_EXAMPLE,
    },
    {
      id: "a-h-share-research",
      label: "1.2 市场",
      title: "研究 A 股、北交所与港股",
      summary: "按上市证券选择市场，从法定披露平台取得财报，并正确理解手动行情与金融机构 Beta 边界。",
      steps: [
        { title: "先选择上市市场", body: "A 股包含上交所、深交所和北交所；港股包含港交所主板与 GEM。同一发行人的 A 股与 H 股会作为不同上市证券保存，币种与披露记录互不混用。" },
        { title: "核对官方披露来源", body: "A 股财报来自巨潮资讯等法定披露入口，港股财报来自披露易。搜索结果会保留证券代码、交易所与官方来源链接；来源不可用时不会用模型补造财务数字。" },
        { title: "手动输入行情", body: "1.2 不自动抓取实时价格。若使用反向 DCF，请填写当前价格或市值、币种和行情日期；报告会把它标记为用户手动输入，避免与官方财报事实混淆。" },
        { title: "留意会计口径", body: "A 股通常使用企业会计准则，港股可能使用 HKFRS、IFRS、企业会计准则或其他获准准则。报告同时保留报告币种、合并口径和页码证据；数据不足时显示缺失。" },
        { title: "理解金融机构 Beta", body: "银行、保险和券商可运行财报、商业、风险与长期情景研究，但资产负债结构不同于普通企业，当前版本不使用标准自由现金流反向 DCF。" },
      ],
      note: "OpenThesis 不提供交易、下单或券商连接。手动行情和 AI 输出都只是研究输入，不构成投资建议。",
    },
  ],
  en: [
    {
      id: "company-research",
      label: "Getting started",
      title: "Start a company research run",
      summary: "Use one reproducible path from market and company selection to a long-term research report.",
      steps: [
        { title: "Choose a market and data identity", body: "Open Start research and select US, A-share, or Hong Kong equities. Only US SEC EDGAR requires an email that reaches you; A/H shares use their statutory disclosure platforms." },
        { title: "Choose the real company", body: "Search by ticker or company name, or use a common-company shortcut. Confirm the company name, full symbol, listing market, and exchange before continuing." },
        { title: "Decide whether to use AI", body: "Choose No AI for a deterministic financial overview. For an AI provider, enter the session-only API key, model ID, and endpoint. OpenThesis does not write the key to its database." },
        { title: "Choose the pack and execution mode", body: "The built-in pack covers financials, business model, accounting risk, growth opportunities, skeptical review, and long-term scenarios. Base agents run sequentially by default; enable parallel execution only for a stable provider and network." },
        { title: "Run and read the report", body: "Start the run, follow per-agent progress, and request cancellation when needed. When complete, zoom, enter focus mode, inspect technical details, or export HTML. You remain responsible for the final judgment." },
      ],
      note: "Run the synthetic demo first to verify the interface, report reader, and export path before using real-company data or a paid model.",
    },
    {
      id: "research-pack",
      label: "Extensions",
      title: "Write your own research pack",
      summary: "A research pack is a declarative ZIP with the .othesis extension. It contains a manifest, a workflow, and prompt files, and never executes arbitrary code.",
      steps: [
        { title: "Create the minimum layout", body: "The package root must contain manifest.yaml and workflow.yaml. Put prompts under prompts/. The current version accepts only .yaml, .yml, .json, .md, and .txt files." },
        { title: "Write the manifest", body: "manifest.yaml currently uses JSON-compatible YAML syntax and must declare kind=ResearchPack plus metadata.id, name, and version. Content cannot overwrite the same ID and version, so increment the version after a change." },
        { title: "Declare the workflow", body: "Each workflow step needs a stable id, agent, and prompt path. A later step may reference earlier steps with depends_on. Every referenced prompt must exist in the archive." },
        { title: "Constrain prompt output", body: "Require structured JSON, explicit insufficient-evidence states, and evidence IDs for factual claims. Keep inference separate from fact. OpenThesis appends a non-overridable report-language rule at runtime." },
        { title: "Package and import", body: "Create a ZIP from the directory contents, rename it to .othesis, and import it from the Research pack area. The archive limit is 10 MB, each file is limited to 2 MB, and scripts, executables, or path traversal are rejected." },
      ],
      note: "Declarative packs cannot request network, filesystem, or execute_code permissions in the current version. Packs that do are rejected safely.",
      code: PACK_EXAMPLE,
    },
    {
      id: "a-h-share-research",
      label: "1.2 markets",
      title: "Research A-shares, BSE, and Hong Kong listings",
      summary: "Select the listing, collect statutory filings, and keep manual market data and Financials Beta boundaries explicit.",
      steps: [
        { title: "Select the listing market first", body: "A-shares cover SSE, SZSE, and BSE; Hong Kong covers HKEX Main Board and GEM. A- and H-share listings of one issuer remain separate securities with their own currency and disclosure history." },
        { title: "Verify the official source", body: "A-share reports come from statutory disclosure entries such as CNInfo; Hong Kong reports come from HKEXnews. Results retain the symbol, exchange, and source URL. If a source is unavailable, the model is not allowed to invent financial values." },
        { title: "Enter market data manually", body: "Version 1.2 does not fetch live prices. For reverse DCF, enter price or market cap, currency, and an as-of date. The report marks these values as user-supplied rather than filing facts." },
        { title: "Respect accounting scope", body: "A-shares generally use CAS, while Hong Kong issuers may use HKFRS, IFRS, CAS, or another accepted standard. The report retains currency, consolidated scope, and page evidence and shows missing data explicitly." },
        { title: "Understand Financials Beta", body: "Banks, insurers, and securities firms can use filing, business, risk, and scenario research. Their balance sheets differ from ordinary companies, so standard free-cash-flow reverse DCF is not applied yet." },
      ],
      note: "OpenThesis has no trading, order-entry, or broker integration. Manual quotes and AI output are research inputs, not investment advice.",
    },
  ],
};

CONTENT["zh-Hant"] = [
  {
    id: "company-research", label: "入門", title: "如何開始公司研究",
    summary: "從市場與公司選擇到模型與研究模組，以可重現流程生成第一份長期研究報告。",
    steps: [
      { title: "選擇市場與資料身分", body: "進入「發起研究」並選擇美股、A 股或港股。只有美股 SEC EDGAR 需要填寫可聯絡到您的電子郵件；A／港股使用各自的法定披露平台。" },
      { title: "選擇真實公司", body: "輸入股票代碼或公司名稱，也可以使用常用公司快捷入口。確認公司名稱、完整代碼、上市市場與交易所後再繼續。" },
      { title: "決定是否使用 AI", body: "選擇「不呼叫 AI」可先生成確定性財務概覽；選擇模型提供者時，填寫工作階段 API Key、模型名稱與端點。API Key 不會保存到本機資料庫。" },
      { title: "選擇研究模組與執行方式", body: "預設模組涵蓋財務、商業模式、會計風險、成長機會、反方審查與長期情境。基礎 Agent 預設依序執行。" },
      { title: "開始並閱讀報告", body: "點擊「開始研究」後可查看逐 Agent 進度並隨時取消。完成後可縮放、進入專注模式、查看技術詳情或匯出 HTML。" },
    ],
    note: "建議第一次先執行合成示範，確認介面、報告閱讀與匯出流程正常，再使用真實公司資料與付費模型。",
  },
  {
    id: "research-pack", label: "擴充", title: "編寫自己的研究模組",
    summary: "研究模組是 .othesis 宣告式 ZIP 套件，由清單、工作流與 Prompt 檔案組成，不執行任意程式碼。",
    steps: [
      { title: "建立最小目錄", body: "套件根目錄必須包含 manifest.yaml 與 workflow.yaml；Prompt 放在 prompts/ 中。" },
      { title: "編寫清單", body: "清單必須提供 kind=ResearchPack 以及 metadata.id、name、version。相同 ID 與版本的內容不能覆蓋。" },
      { title: "宣告工作流", body: "每個步驟應有穩定 id、agent 與 prompt 路徑；所有引用的 Prompt 檔案必須存在。" },
      { title: "限制 Prompt 輸出", body: "要求結構化 JSON、明確表示證據不足並引用證據 ID。OpenThesis 會在執行時追加不可覆蓋的報告語言規則。" },
      { title: "打包與匯入", body: "將目錄壓縮為 ZIP 並改為 .othesis 副檔名，再從研究模組區域匯入。" },
    ],
    note: "宣告式模組目前不允許 network、filesystem 或 execute_code 權限。",
    code: PACK_EXAMPLE,
  },
  {
    id: "a-h-share-research", label: "市場", title: "研究 A 股、北交所與港股",
    summary: "依上市證券選擇市場，從法定披露平台取得財報，並正確理解手動行情與金融機構 Beta 邊界。",
    steps: [
      { title: "先選擇上市市場", body: "A 股包含上交所、深交所與北交所；港股包含港交所主板與 GEM。" },
      { title: "核對官方披露來源", body: "A 股財報來自法定披露入口，港股財報來自披露易。來源不可用時不會用模型補造財務數字。" },
      { title: "手動輸入行情", body: "1.2 不自動抓取即時價格。使用反向 DCF 時請填寫價格或市值、幣種與行情日期。" },
      { title: "留意會計口徑", body: "報告保留報告幣種、合併口徑與頁碼證據；資料不足時會顯示缺失。" },
      { title: "理解金融機構 Beta", body: "銀行、保險與券商可執行財報、商業、風險與長期情境研究，但不使用標準自由現金流反向 DCF。" },
    ],
    note: "OpenThesis 不提供交易、下單或券商連線。手動行情和 AI 輸出都只是研究輸入，不構成投資建議。",
  },
];

export function HelpView({ language, copy }: { language: Language; copy: { helpBody: string } }) {
  const articles = CONTENT[language] ?? CONTENT.en ?? [];
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
              <span className="help-icon" aria-hidden="true">{articleIndex === 0 ? <BookOpenCheck size={20} /> : <Boxes size={20} />}</span>
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
