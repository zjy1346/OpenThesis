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

const CONTENT: Record<Language, HelpArticle[]> = {
  "zh-CN": [
    {
      id: "company-research",
      label: "入门",
      title: "如何开始一家公司研究",
      summary: "从 SEC 身份、公司选择到模型与研究模块，用一条可复现的流程生成第一份长期研究报告。",
      steps: [
        { title: "填写 SEC 联系身份", body: "进入“发起研究”，选择个人投资者、独立研究者或机构研究团队，并填写能联系到你本人的邮箱。SEC EDGAR 不需要 API Key；请勿填写被研究公司的投资者关系邮箱。" },
        { title: "选择真实公司", body: "输入股票代码或公司名称并搜索 SEC，也可以使用常用公司快捷入口。确认公司名称、代码和交易所后再继续。" },
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
  ],
  en: [
    {
      id: "company-research",
      label: "Getting started",
      title: "Start a company research run",
      summary: "Use one reproducible path from SEC identity and company selection to a long-term research report.",
      steps: [
        { title: "Set your SEC identity", body: "Open Start research, choose personal investor, independent researcher, or organization, and enter an email that reaches you. SEC EDGAR needs no API key. Do not use the researched company's investor-relations address." },
        { title: "Choose the real company", body: "Search by ticker or company name, or use a common-company shortcut. Confirm the company name, ticker, and exchange before continuing." },
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
  ],
};

export function HelpView({ language, copy }: { language: Language; copy: { helpBody: string } }) {
  const articles = CONTENT[language];
  return (
    <div className="help-view">
      <header className="section-intro help-intro">
        <p>{copy.helpBody}</p>
        <nav aria-label={language === "en" ? "Help articles" : "帮助文章"}>
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
