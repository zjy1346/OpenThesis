from __future__ import annotations

from string import Formatter
from dataclasses import dataclass
from typing import Final, Iterable


ZH_CN: Final = "zh-CN"
ZH_HANT: Final = "zh-Hant"
EN: Final = "en"
SUPPORTED_LANGUAGES: Final = (ZH_CN, ZH_HANT, EN)
LANGUAGE_NAMES: Final = {
    ZH_CN: "\u7b80\u4f53\u4e2d\u6587",
    ZH_HANT: "\u7e41\u9ad4\u4e2d\u6587",
    EN: "English",
}


@dataclass(frozen=True, slots=True)
class LanguageDefinition:
    canonical: str
    aliases: tuple[str, ...]
    locale_prefixes: tuple[str, ...]
    html_lang: str
    direction: str = "ltr"


LANGUAGE_REGISTRY: Final[tuple[LanguageDefinition, ...]] = (
    LanguageDefinition(ZH_CN, ("zh-cn", "zh-hans", "zh-hans-cn", "zh-sg", "zh"), ("zh-hans", "zh-cn", "zh-sg", "zh"), "zh-CN"),
    LanguageDefinition(ZH_HANT, ("zh-hant", "zh-tw", "zh-hk", "zh-mo", "zh-hant-tw", "zh-hant-hk"), ("zh-hant", "zh-tw", "zh-hk", "zh-mo"), "zh-Hant"),
    LanguageDefinition(EN, ("en", "en-us", "en-gb"), ("en",), "en"),
)


def normalize_language(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("_", "-")
    # Script/region-specific Traditional aliases must win over the generic
    # ``zh`` prefix in the Simplified definition.
    for definition in (LANGUAGE_REGISTRY[1], LANGUAGE_REGISTRY[0], LANGUAGE_REGISTRY[2]):
        if normalized in definition.aliases or any(normalized.startswith(f"{prefix}-") for prefix in definition.locale_prefixes):
            return definition.canonical
    return ZH_CN


def language_name(language: str, display_language: str | None = None) -> str:
    locale = normalize_language(language)
    display = normalize_language(display_language or locale)
    names = {
        ZH_CN: {ZH_CN: "\u7b80\u4f53\u4e2d\u6587", ZH_HANT: "\u7c21\u9ad4\u4e2d\u6587", EN: "Simplified Chinese"},
        ZH_HANT: {ZH_CN: "\u7e41\u9ad4\u4e2d\u6587", ZH_HANT: "\u7e41\u9ad4\u4e2d\u6587", EN: "Traditional Chinese"},
        EN: {ZH_CN: "\u82f1\u6587", ZH_HANT: "\u82f1\u6587", EN: "English"},
    }
    return names[locale][display]


def resolve_system_language(preferred_locales: Iterable[str] | None) -> str:
    """Resolve ordered OS/browser locales without guessing unsupported languages."""
    for locale in preferred_locales or ():
        normalized = (locale or "").strip()
        if not normalized:
            continue
        folded = normalized.lower().replace("_", "-")
        if folded.startswith("zh-hant") or folded in {"zh-tw", "zh-hk", "zh-mo"} or "traditional" in folded or "taiwan" in folded or "hong kong" in folded:
            return ZH_HANT
        if folded.startswith("zh") or "simplified" in folded or "chinese" in folded:
            return ZH_CN
        if normalized.lower().replace("_", "-").startswith("en"):
            return EN
    return EN


def resolve_ui_language(mode: str | None, stored_language: str | None, preferred_locales: Iterable[str] | None = None) -> str:
    if str(mode or "").strip().lower() == "system":
        return resolve_system_language(preferred_locales)
    return normalize_language(stored_language)


OUTPUT_LANGUAGE_INSTRUCTIONS: Final = {
    ZH_CN: (
        "Write every natural-language value in Simplified Chinese. "
        "Keep JSON property names, evidence IDs, enum values, numbers, URLs, "
        "and company names unchanged."
    ),
    EN: (
        "Write every natural-language value in English. "
        "Keep JSON property names, evidence IDs, enum values, numbers, URLs, "
        "and company names unchanged."
    ),
    ZH_HANT: (
        "Write every natural-language value in Traditional Chinese. "
        "Keep JSON property names, evidence IDs, enum values, numbers, URLs, "
        "and company names unchanged."
    ),
}


UI_EN: Final[dict[str, str]] = {
    "研究公司，而不是预测短期价格": "Research companies, not short-term prices",
    "长期公司研究工作台": "Long-term Company Research",
    "模型与数据源": "Models & Data",
    "本地优先 · 不执行交易": "Local-first · No trading",
    "研究": "Research",
    "配置": "Configuration",
    "资产": "Assets",
    "应用": "Application",
    "折叠导航": "Collapse navigation",
    "展开导航": "Expand navigation",
    "公司研究": "Company Research",
    "研究历史": "Research History",
    "模型设置": "Model Settings",
    "研究模块": "Research Packs",
    "投资逻辑": "Investment Thesis",
    "设置": "Settings",
    "关于": "About",
    "就绪": "Ready",
    "尚未选择公司": "No company selected",
    "请先在下方选择一家公司。": "Select a company below first.",
    "研究流程": "Research Workflow",
    "① 选择公司   →   ② 确认配置   →   ③ 开始研究": (
        "① Select company   →   ② Confirm settings   →   ③ Start research"
    ),
    "开始研究": "Start Research",
    "模型与 SEC 设置": "Model & SEC Settings",
    "显示研究配置": "Show Research Settings",
    "隐藏研究配置": "Hide Research Settings",
    "任务进度": "Task Progress",
    "显示进度详情": "Show Progress Details",
    "隐藏进度详情": "Hide Progress Details",
    "等待开始研究": "Waiting to Start",
    "选择公司并确认配置后，任务阶段、等待时间和错误会显示在这里。": (
        "After selecting a company and confirming settings, stages, elapsed "
        "time, and errors will appear here."
    ),
    "已用时 00:00": "Elapsed 00:00",
    "取消研究": "Cancel Research",
    "重新运行": "Run Again",
    "检查模型设置": "Check Model Settings",
    "1. 选择公司": "1. Select Company",
    "搜索": "Search",
    "常用公司快捷选择": "Common Companies",
    "选择": "Select",
    "使用合成演示公司": "Use Synthetic Demo Company",
    "2. 研究配置": "2. Research Settings",
    "下载最近五份 10-K 原文": "Download the five most recent 10-K filings",
    "未配置模型时仍会生成确定性财务报告。": (
        "A deterministic financial report is still generated without a model."
    ),
    "▶ 高级设置：反向 DCF": "▶ Advanced: Reverse DCF",
    "▼ 高级设置：反向 DCF": "▼ Advanced: Reverse DCF",
    "反向 DCF 参数": "Reverse DCF Parameters",
    "当前市值（十亿美元）": "Current market cap (USD billions)",
    "折现率 %": "Discount rate %",
    "永续增长率 %": "Terminal growth %",
    "运行第二模型并比较分歧": "Run a second model and compare differences",
    "导出当前报告": "Export Current Report",
    "研究报告": "Research Report",
    "清空显示": "Clear View",
    "导出": "Export",
    "显示技术详情": "Show Technical Details",
    "隐藏技术详情": "Hide Technical Details",
    "⛶ 沉浸阅读": "⛶ Focus Mode",
    "⤢ 恢复布局": "⤢ Restore Layout",
    "沉浸阅读": "Focus Reading",
    "欢迎使用 OpenThesis。\n\n第一步：搜索或快捷选择公司；第二步：确认研究模块和模型设置；第三步：点击页面顶部始终可见的“开始研究”。\n\n可以选择“合成演示公司”离线验证完整流程。研究真实公司时，请在“模型与 SEC 设置”中填写你自己的 SEC 联系邮箱。": (
        "Welcome to OpenThesis.\n\nFirst, search for or quickly select a "
        "company. Second, confirm the research pack and model settings. Third, "
        "click the always-visible “Start Research” button at the top.\n\nUse "
        "the synthetic demo company to validate the complete workflow offline. "
        "For a real company, enter your own SEC contact email in “Model & SEC "
        "Settings”."
    ),
    "本地研究历史": "Local Research History",
    "刷新": "Refresh",
    "代码": "Ticker",
    "公司": "Company",
    "状态": "Status",
    "开始时间": "Started",
    "模型与数据源设置": "Model & Data Source Settings",
    "请选择提供方预设。": "Select a provider preset.",
    "主 AI 模型（可选）": "Primary AI Model (Optional)",
    "首次启动不会调用 AI；只有主动选择模型并开始研究时才会发送研究上下文。API Key 只保存在内存中，不写入数据库或日志。": (
        "AI is disabled on first launch. Research context is sent only after "
        "you select a model and start research. API keys stay in memory and "
        "are never written to the database or logs."
    ),
    "SEC EDGAR 财报访问": "SEC EDGAR Filing Access",
    "SEC 不需要 API Key，但要求请求者提供真实、可联系的邮箱。": (
        "SEC does not require an API key, but requests must identify a real, "
        "reachable contact email."
    ),
    "帮助：SEC 是什么，如何获取财报？": "Help: What is SEC and how are filings retrieved?",
    "常用请求身份模板": "Requester Profile",
    "联系邮箱（填写你自己的）": "Contact email (your own)",
    "发送给 SEC 的请求标识": "User-Agent sent to SEC",
    "请勿填写目标公司的投资者关系邮箱。这里标识的是数据请求者。邮箱保存在本机设置，并随 SEC 请求发送。": (
        "Do not enter the target company's investor-relations email. This "
        "identifies the data requester. The email is stored locally and sent "
        "with SEC requests."
    ),
    "保存本机设置（不保存 API Key）": "Save Local Settings (API Key Excluded)",
    "测试模型连接": "Test Model Connection",
    "▶ 可选：第二个对比模型": "▶ Optional: Second Comparison Model",
    "▼ 可选：第二个对比模型": "▼ Optional: Second Comparison Model",
    "第二个对比模型": "Second Comparison Model",
    "提供方预设": "Provider Preset",
    "模型名称": "Model Name",
    "在线目录": "Online Catalog",
    "刷新在线模型": "Refresh Online Models",
    "接口地址": "Base URL",
    "API Key（仅本次会话）": "API Key (This Session Only)",
    "帮助": "Help",
    "获取 API Key / 安装帮助": "Get API Key / Installation Help",
    ".othesis 研究模块": ".othesis Research Packs",
    "导入模块": "Import Pack",
    "v0.1 模块仅允许 Markdown、JSON 兼容 YAML、JSON Schema 和文本；不允许运行代码、访问文件系统、网络或密钥。": (
        "v0.1 packs may contain only Markdown, JSON-compatible YAML, JSON "
        "Schema, and text. They cannot execute code or access files, networks, "
        "or secrets."
    ),
    "投资逻辑版本": "Thesis Versions",
    "版本": "Version",
    "创建者": "Created By",
    "时间": "Time",
    "可编辑 Thesis JSON": "Editable Thesis JSON",
    "另存为新版本": "Save as New Version",
    "界面与报告语言": "Interface & Report Language",
    "界面语言": "Interface Language",
    "界面语言模式": "Interface Language Mode",
    "跟随系统": "Follow system",
    "手动选择": "Manual selection",
    "研究报告语言": "Research Report Language",
    "界面语言将在下次启动时生效。": (
        "The interface language takes effect after restarting the application."
    ),
    "报告语言立即用于下一次研究；历史报告只翻译程序生成的标题，AI 正文保持原文。": (
        "The report language applies to the next research run immediately. "
        "For historical reports, only application-generated headings are "
        "translated; AI-authored text remains unchanged."
    ),
    "保存语言设置": "Save Language Settings",
    "语言设置已保存。": "Language settings saved.",
    "界面语言将在重启 OpenThesis 后生效。": (
        "The interface language will change after restarting OpenThesis."
    ),
    "报告语言已应用于下一次研究。": (
        "The report language will be used for the next research run."
    ),
    "本地数据目录：{path}": "Local data directory: {path}",
    "面向个人长期投资者的开源、模型无关公司研究系统。": (
        "An open-source, model-agnostic company research system for individual "
        "long-term investors."
    ),
    "原则：每个事实都需要证据；财务计算由确定性程序完成；预测使用情景、区间和失效条件；AI 不执行任何交易。": (
        "Principles: every fact needs evidence; financial calculations are "
        "deterministic; forecasts use scenarios, ranges, and invalidation "
        "conditions; AI never executes trades."
    ),
    "SEC 联系邮箱无效": "Invalid SEC Contact Email",
    "请输入你本人或所在研究团队可正常收信的邮箱地址。": (
        "Enter a working email address belonging to you or your research team."
    ),
    "请选择一个 SEC 请求身份模板。": "Select an SEC requester profile.",
    "请选择一个内置的常用公司。": "Select one of the built-in common companies.",
    "设置已保存；API Key 未持久化": "Settings saved; API key was not persisted",
    "填写邮箱后自动生成，无需申请 SEC API Key": (
        "Generated automatically after entering an email; no SEC API key needed"
    ),
    "邮箱格式尚未完成": "Email address is incomplete",
    "SEC EDGAR 使用帮助": "SEC EDGAR Help",
    "SEC 是什么，OpenThesis 如何获取财报？": (
        "What is SEC, and how does OpenThesis retrieve filings?"
    ),
    "打开 SEC 官方开发者说明": "Open Official SEC Developer Guidance",
    "关闭": "Close",
    "当前不会调用 AI。": "AI is currently disabled.",
    "本次会话已缓存 {count} 个在线模型。": (
        "{count} online models are cached for this session."
    ),
    "此提供方使用内置模型列表，也可手动填写模型 ID。": (
        "This provider uses the built-in model list; you may also enter a model ID."
    ),
    "可刷新本机已安装模型；未安装时请先使用帮助链接。": (
        "Refresh locally installed models, or use Help if Ollama is not installed."
    ),
    "已加载内置推荐模型；可手动刷新在线目录。": (
        "Built-in recommendations loaded; refresh the online catalog manually."
    ),
    "当前未启用 AI，无需刷新。": "AI is disabled; no refresh is needed.",
    "正在后台刷新在线模型…": "Refreshing online models in the background…",
    "在线模型目录刷新失败，已保留内置列表。": (
        "Online model refresh failed; built-in models were preserved."
    ),
    "模型帮助": "Model Help",
    "自定义接口请向服务提供方获取 API Key、模型 ID 和兼容地址。": (
        "For a custom endpoint, obtain the API key, model ID, and compatible "
        "base URL from its provider."
    ),
    "搜索公司": "Search Company",
    "请输入股票代码或公司名称。": "Enter a ticker or company name.",
    "需要 SEC 联系邮箱": "SEC Contact Email Required",
    "{error}\n\n请在“模型与数据源设置”中填写后保存。": (
        "{error}\n\nEnter and save it in “Model & Data Source Settings”."
    ),
    "正在查询 SEC 公司列表…": "Searching the SEC company list…",
    "选择常用公司": "Select Common Company",
    "已选择常用公司：{ticker} · {name}。\n\n请确认研究配置，然后点击页面顶部的“开始研究”。": (
        "Selected common company: {ticker} · {name}.\n\nConfirm the research "
        "settings, then click “Start Research” at the top."
    ),
    "已选择合成演示公司。所有数据均为虚构，只用于验证软件功能。": (
        "Synthetic demo company selected. All data is fictional and used only "
        "to verify application behavior."
    ),
    "已恢复上次异常中断的任务记录（{count}）": (
        "Recovered interrupted research records ({count})"
    ),
    "上次关闭应用时仍有研究在运行，现已安全标记为“已取消”；已完成的中间产物仍可在研究历史中查看。": (
        "Research was still running when the application last closed. It has "
        "been safely marked as cancelled; completed intermediate artifacts "
        "remain available in Research History."
    ),
    "如需继续，请重新选择公司并运行研究。": (
        "To continue, select the company and run the research again."
    ),
    "正在准备 {ticker} 的研究数据": "Preparing research data for {ticker}",
    "任务正在后台运行；窗口保持响应，可以随时查看当前阶段。": (
        "The task is running in the background. The window remains responsive "
        "and the current stage is always visible."
    ),
    "完成后将在这里显示完整报告。": "The complete report will appear here when finished.",
    "研究正在进行中\n\n": "Research in progress\n\n",
    "已用时 {elapsed}": "Elapsed {elapsed}",
    "取消请求已收到；正在等待当前网络请求安全结束，不会再启动新的研究步骤。": (
        "Cancellation received. Waiting for the current network request to end "
        "safely; no new research stages will start."
    ),
    "后台仍在工作 · 当前步骤已等待 {elapsed} · 模型研究通常需要数分钟，请勿关闭应用。": (
        "Still working in the background · Current stage waiting {elapsed} · "
        "Model research often takes several minutes; keep the application open."
    ),
    "正在取消研究…": "Cancelling research…",
    "已停止启动新步骤；当前网络请求结束后会安全保存中间结果。": (
        "No new stages will start. Intermediate results will be saved after "
        "the current network request ends."
    ),
    "正在安全取消研究…": "Cancelling research safely…",
    "技术信息：{message}": "Technical details: {message}",
    "研究任务未完成\n\n": "Research task incomplete\n\n",
    "研究进行中…": "Research in Progress…",
    "正在运行多 Agent 研究流程，请查看下方任务进度。": (
        "The multi-agent workflow is running. See Task Progress below."
    ),
    "公司已选择；确认配置后即可开始。": (
        "Company selected. Confirm the settings to begin."
    ),
    "请先选择公司。": "Select a company first.",
    "{error}\n\n真实公司研究需要访问 SEC，请先完成 SEC 设置。": (
        "{error}\n\nResearching a real company requires SEC access. Complete "
        "the SEC settings first."
    ),
    "反向 DCF 输入错误": "Invalid Reverse DCF Input",
    "反向 DCF 输入必须是数字": "Reverse DCF inputs must be numeric.",
    "市值必须为正数，且折现率必须高于永续增长率": (
        "Market capitalization must be positive, and the discount rate must "
        "exceed the terminal growth rate."
    ),
    "双模型配置不完整": "Incomplete Two-Model Configuration",
    "启用模型比较时，主模型和第二模型都必须配置提供方、模型名称和接口地址。": (
        "When model comparison is enabled, both models require a provider, "
        "model name, and base URL."
    ),
    "正在加载离线演示数据": "Loading offline demo data",
    "演示数据准备完成": "Demo data ready",
    "正在获取 SEC 年报清单": "Retrieving SEC annual filing list",
    "正在下载 SEC 10-K（{index}/{total}）": (
        "Downloading SEC 10-K ({index}/{total})"
    ),
    "正在解析财报证据与表格": "Parsing filing evidence and tables",
    "正在获取 SEC Company Facts": "Retrieving SEC Company Facts",
    "研究数据准备完成，正在启动 Agent": "Research data ready; starting agents",
    "主模型研究完成，正在启动对比模型": (
        "Primary-model research complete; starting comparison model"
    ),
    "主模型：": "Primary model: ",
    "对比模型：": "Comparison model: ",
    "双模型分歧比较完成": "Two-model comparison complete",
    "研究任务正在运行…": "Research task is running…",
    "已完成确定性财务计算": "Deterministic financial calculations complete",
    "基础财务分析完成；配置模型后可运行完整研究": (
        "Basic financial analysis complete; configure a model for full research"
    ),
    "正在并行运行财务、商业与会计风险 Agent（0/3）": (
        "Running financial, business, and accounting-risk agents in parallel (0/3)"
    ),
    "基础分析 Agent 已完成 {completed}/3：{agent_id}": (
        "Base analysis agent complete {completed}/3: {agent_id}"
    ),
    "基础研究档案完成": "Base research dossier complete",
    "正在研究公司与行业增长机会": (
        "Researching company and industry growth opportunities"
    ),
    "增长机会研究完成": "Growth-opportunity research complete",
    "正在进行反方审查与压力测试": "Running skeptical review and stress test",
    "反方审查完成": "Skeptical review complete",
    "正在生成长期经营情景": "Generating long-term operating scenarios",
    "长期情景完成": "Long-term scenarios complete",
    "正在合成最终长期研究报告": "Synthesizing the final long-term research report",
    "测试模型": "Test Model",
    "当前选择 none，不会调用语言模型。": (
        "The current selection is none; no language model will be called."
    ),
    "未配置模型": "No model configured",
    "正在测试模型连接…": "Testing model connection…",
    "导入 OpenThesis 研究模块": "Import OpenThesis Research Pack",
    "研究模块验证失败": "Research Pack Validation Failed",
    "研究模块已安装": "Research Pack Installed",
    "{name}\n版本：{version}\n哈希：{hash}": (
        "{name}\nVersion: {version}\nHash: {hash}"
    ),
    "投资逻辑": "Investment Thesis",
    "请先选择一个已有版本。": "Select an existing version first.",
    "JSON 格式错误": "Invalid JSON",
    "第 {line} 行，第 {column} 列：{message}": (
        "Line {line}, column {column}: {message}"
    ),
    "已保存为 v{version}": "Saved as v{version}",
    "导出报告": "Export Report",
    "当前没有可导出的内容。": "There is no report to export.",
    "导出 OpenThesis 报告": "Export OpenThesis Report",
    "报告已导出：{path}": "Report exported: {path}",
    "找到 {count} 家公司": "Found {count} companies",
    "研究完成": "Research Complete",
    "研究完成：{status}": "Research complete: {status}",
    "全部研究阶段已完成，完整报告和中间产物已经保存。": (
        "All research stages are complete. The full report and intermediate "
        "artifacts have been saved."
    ),
    "研究已取消": "Research Cancelled",
    "研究已取消。\n\n": "Research cancelled.\n\n",
    "任务已停止；当前步骤之前完成的中间结果已经安全保存。": (
        "The task stopped. Intermediate results completed before the current "
        "stage were saved safely."
    ),
    "可以重新运行研究，或在研究历史中查看中间结果。": (
        "Run the research again or inspect intermediate results in Research History."
    ),
    "模型连接测试": "Model Connection Test",
    "已合并 {count} 个在线模型；内置推荐项保持置顶。": (
        "Merged {count} online models; built-in recommendations remain pinned."
    ),
    " 若尚未安装或启动 Ollama，请点击帮助。": (
        " If Ollama is not installed or running, open Help."
    ),
    "研究任务失败": "Research Task Failed",
    "后台任务失败": "Background Task Failed",
    "OpenThesis 后台任务失败": "OpenThesis Background Task Failed",
    "SEC 数据获取失败": "SEC Data Retrieval Failed",
    "请检查网络连接和 SEC 联系邮箱后重试；也可以先使用“合成演示公司”验证完整流程。": (
        "Check the network connection and SEC contact email, then retry. You "
        "can also validate the workflow with the synthetic demo company."
    ),
    "模型认证失败": "Model Authentication Failed",
    "请检查 API Key、提供方账号权限以及接口地址。Key 不会出现在诊断信息中。": (
        "Check the API key, provider account permissions, and base URL. The key "
        "will not appear in diagnostics."
    ),
    "模型请求受到限流": "Model Request Rate Limited",
    "服务商暂时限制了请求频率。请稍后重新运行，或检查账号额度。": (
        "The provider temporarily limited request frequency. Try again later "
        "or check the account quota."
    ),
    "模型或数据请求超时": "Model or Data Request Timed Out",
    "网络或模型响应时间超过限制。已完成的中间结果仍保存在研究历史中。": (
        "The network or model exceeded the response-time limit. Completed "
        "intermediate results remain in Research History."
    ),
    "模型或接口不存在": "Model or Endpoint Not Found",
    "请核对模型 ID 与接口地址，必要时在模型设置中刷新在线目录。": (
        "Check the model ID and base URL. Refresh the online catalog in Model "
        "Settings if needed."
    ),
    "网络连接失败": "Network Connection Failed",
    "无法连接模型或数据服务。请检查网络、代理、接口地址和本地服务状态。": (
        "Could not connect to the model or data service. Check the network, "
        "proxy, base URL, and local service status."
    ),
    "任务未能完成；已完成的中间结果仍保存在研究历史中。可检查设置后重新运行。": (
        "The task did not complete. Finished intermediate results remain in "
        "Research History. Check settings and run again."
    ),
}


UI_HANT_EXPLICIT: Final[dict[str, str]] = {
    "\u7814\u7a76\u516c\u53f8\uff0c\u800c\u4e0d\u662f\u9884\u6d4b\u77ed\u671f\u4ef7\u683c": "\u7814\u7a76\u516c\u53f8\uff0c\u800c\u4e0d\u662f\u9810\u6e2c\u77ed\u671f\u50f9\u683c",
    "\u957f\u671f\u516c\u53f8\u7814\u7a76\u5de5\u4f5c\u53f0": "\u9577\u671f\u516c\u53f8\u7814\u7a76\u5de5\u4f5c\u53f0",
    "\u6a21\u578b\u4e0e\u6570\u636e\u6e90": "\u6a21\u578b\u8207\u8cc7\u6599\u4f86\u6e90",
    "\u672c\u5730\u4f18\u5148 \u00b7 \u4e0d\u6267\u884c\u4ea4\u6613": "\u672c\u5730\u512a\u5148 \u00b7 \u4e0d\u57f7\u884c\u4ea4\u6613",
    "\u7814\u7a76": "\u7814\u7a76",
    "\u914d\u7f6e": "\u8a2d\u5b9a",
    "\u8d44\u4ea7": "\u8cc7\u7522",
    "\u5e94\u7528": "\u61c9\u7528",
    "\u6298\u53e0\u5bfc\u822a": "\u6536\u5408\u5c0e\u89bd",
    "\u5c55\u5f00\u5bfc\u822a": "\u5c55\u958b\u5c0e\u89bd",
    "\u516c\u53f8\u7814\u7a76": "\u516c\u53f8\u7814\u7a76",
    "\u7814\u7a76\u5386\u53f2": "\u7814\u7a76\u6b77\u53f2",
    "\u6a21\u578b\u8bbe\u7f6e": "\u6a21\u578b\u8a2d\u5b9a",
    "\u7814\u7a76\u6a21\u5757": "\u7814\u7a76\u6a21\u7d44",
    "\u6295\u8d44\u903b\u8f91": "\u6295\u8cc7\u908f\u8f2f",
    "\u8bbe\u7f6e": "\u8a2d\u5b9a",
    "\u5173\u4e8e": "\u95dc\u65bc",
    "\u5c31\u7eea": "\u5c31\u7dd2",
    "\u5c1a\u672a\u9009\u62e9\u516c\u53f8": "\u5c1a\u672a\u9078\u64c7\u516c\u53f8",
    "\u8bf7\u5148\u5728\u4e0b\u65b9\u9009\u62e9\u4e00\u5bb6\u516c\u53f8\u3002": "\u8acb\u5148\u5728\u4e0b\u65b9\u9078\u64c7\u4e00\u5bb6\u516c\u53f8\u3002",
    "\u7814\u7a76\u6d41\u7a0b": "\u7814\u7a76\u6d41\u7a0b",
    "\u2460 \u9009\u62e9\u516c\u53f8   \u2192   \u2461 \u786e\u8ba4\u914d\u7f6e   \u2192   \u2462 \u5f00\u59cb\u7814\u7a76": "\u2460 \u9078\u64c7\u516c\u53f8   \u2192   \u2461 \u78ba\u8a8d\u914d\u7f6e   \u2192   \u2462 \u958b\u59cb\u7814\u7a76",
    "\u5f00\u59cb\u7814\u7a76": "\u958b\u59cb\u7814\u7a76",
    "\u6a21\u578b\u4e0e SEC \u8bbe\u7f6e": "\u6a21\u578b\u4e0e SEC \u8a2d\u7f6e",
    "\u663e\u793a\u7814\u7a76\u914d\u7f6e": "\u986f\u793a\u7814\u7a76\u914d\u7f6e",
    "\u9690\u85cf\u7814\u7a76\u914d\u7f6e": "\u96b1\u85cf\u7814\u7a76\u914d\u7f6e",
    "\u4efb\u52a1\u8fdb\u5ea6": "\u4efb\u52d9\u9032\u5ea6",
    "\u663e\u793a\u8fdb\u5ea6\u8be6\u60c5": "\u986f\u793a\u9032\u5ea6\u8a73\u60c5",
    "\u9690\u85cf\u8fdb\u5ea6\u8be6\u60c5": "\u96b1\u85cf\u9032\u5ea6\u8a73\u60c5",
    "\u7b49\u5f85\u5f00\u59cb\u7814\u7a76": "\u7b49\u5f85\u958b\u59cb\u7814\u7a76",
    "\u9009\u62e9\u516c\u53f8\u5e76\u786e\u8ba4\u914d\u7f6e\u540e\uff0c\u4efb\u52a1\u9636\u6bb5\u3001\u7b49\u5f85\u65f6\u95f4\u548c\u9519\u8bef\u4f1a\u663e\u793a\u5728\u8fd9\u91cc\u3002": "\u9078\u64c7\u516c\u53f8\u4e26\u78ba\u8a8d\u914d\u7f6e\u5f8c\uff0c\u4efb\u52d9\u968e\u6bb5\u3001\u7b49\u5f85\u6642\u95f4\u548c\u932f\u8bef\u6703\u986f\u793a\u5728\u8fd9\u91cc\u3002",
    "\u5df2\u7528\u65f6 00:00": "\u5df2\u7528\u6642 00:00",
    "\u53d6\u6d88\u7814\u7a76": "\u53d6\u6d88\u7814\u7a76",
    "\u91cd\u65b0\u8fd0\u884c": "\u91cd\u65b0\u904b\u884c",
    "\u68c0\u67e5\u6a21\u578b\u8bbe\u7f6e": "\u6aa2\u67e5\u6a21\u578b\u8a2d\u7f6e",
    "1. \u9009\u62e9\u516c\u53f8": "1. \u9078\u64c7\u516c\u53f8",
    "\u641c\u7d22": "\u641c\u5c0b",
    "\u5e38\u7528\u516c\u53f8\u5feb\u6377\u9009\u62e9": "\u5e38\u7528\u516c\u53f8\u5feb\u6377\u9078\u64c7",
    "\u9009\u62e9": "\u9078\u64c7",
    "\u4f7f\u7528\u5408\u6210\u6f14\u793a\u516c\u53f8": "\u4f7f\u7528\u5408\u6210\u6f14\u793a\u516c\u53f8",
    "2. \u7814\u7a76\u914d\u7f6e": "2. \u7814\u7a76\u914d\u7f6e",
    "\u4e0b\u8f7d\u6700\u8fd1\u4e94\u4efd 10-K \u539f\u6587": "\u4e0b\u8f09\u6700\u8fd1\u4e94\u4efd 10-K \u539f\u6587",
    "\u672a\u914d\u7f6e\u6a21\u578b\u65f6\u4ecd\u4f1a\u751f\u6210\u786e\u5b9a\u6027\u8d22\u52a1\u62a5\u544a\u3002": "\u672a\u914d\u7f6e\u6a21\u578b\u6642\u4ecd\u6703\u751f\u6210\u78ba\u5b9a\u6027\u8ca1\u52d9\u5831\u544a\u3002",
    "\u25b6 \u9ad8\u7ea7\u8bbe\u7f6e\uff1a\u53cd\u5411 DCF": "\u25b6 \u9ad8\u7d1a\u8a2d\u7f6e\uff1a\u53cd\u5411 DCF",
    "\u25bc \u9ad8\u7ea7\u8bbe\u7f6e\uff1a\u53cd\u5411 DCF": "\u25bc \u9ad8\u7d1a\u8a2d\u7f6e\uff1a\u53cd\u5411 DCF",
    "\u53cd\u5411 DCF \u53c2\u6570": "\u53cd\u5411 DCF \u53c2\u6578",
    "\u5f53\u524d\u5e02\u503c\uff08\u5341\u4ebf\u7f8e\u5143\uff09": "\u5f53\u524d\u5e02\u503c\uff08\u5341\u4ebf\u7f8e\u5143\uff09",
    "\u6298\u73b0\u7387 %": "\u6298\u73fe\u7387 %",
    "\u6c38\u7eed\u589e\u957f\u7387 %": "\u6c38\u7e8c\u589e\u9577\u7387 %",
    "\u8fd0\u884c\u7b2c\u4e8c\u6a21\u578b\u5e76\u6bd4\u8f83\u5206\u6b67": "\u904b\u884c\u7b2c\u4e8c\u6a21\u578b\u4e26\u6bd4\u8f83\u5206\u6b67",
    "\u5bfc\u51fa\u5f53\u524d\u62a5\u544a": "\u5c0e\u51fa\u5f53\u524d\u5831\u544a",
    "\u7814\u7a76\u62a5\u544a": "\u7814\u7a76\u5831\u544a",
    "\u6e05\u7a7a\u663e\u793a": "\u6e05\u7a7a\u986f\u793a",
    "\u5bfc\u51fa": "\u5c0e\u51fa",
    "\u663e\u793a\u6280\u672f\u8be6\u60c5": "\u986f\u793a\u6280\u8853\u8a73\u60c5",
    "\u9690\u85cf\u6280\u672f\u8be6\u60c5": "\u96b1\u85cf\u6280\u8853\u8a73\u60c5",
    "\u26f6 \u6c89\u6d78\u9605\u8bfb": "\u26f6 \u6c89\u6d78\u95b1\u8bfb",
    "\u2922 \u6062\u590d\u5e03\u5c40": "\u2922 \u6062\u8907\u5e03\u5c40",
    "\u6c89\u6d78\u9605\u8bfb": "\u6c89\u6d78\u95b1\u8bfb",
    "\u6b22\u8fce\u4f7f\u7528 OpenThesis\u3002\n\n\u7b2c\u4e00\u6b65\uff1a\u641c\u7d22\u6216\u5feb\u6377\u9009\u62e9\u516c\u53f8\uff1b\u7b2c\u4e8c\u6b65\uff1a\u786e\u8ba4\u7814\u7a76\u6a21\u5757\u548c\u6a21\u578b\u8bbe\u7f6e\uff1b\u7b2c\u4e09\u6b65\uff1a\u70b9\u51fb\u9875\u9762\u9876\u90e8\u59cb\u7ec8\u53ef\u89c1\u7684\u201c\u5f00\u59cb\u7814\u7a76\u201d\u3002\n\n\u53ef\u4ee5\u9009\u62e9\u201c\u5408\u6210\u6f14\u793a\u516c\u53f8\u201d\u79bb\u7ebf\u9a8c\u8bc1\u5b8c\u6574\u6d41\u7a0b\u3002\u7814\u7a76\u771f\u5b9e\u516c\u53f8\u65f6\uff0c\u8bf7\u5728\u201c\u6a21\u578b\u4e0e SEC \u8bbe\u7f6e\u201d\u4e2d\u586b\u5199\u4f60\u81ea\u5df1\u7684 SEC \u8054\u7cfb\u90ae\u7bb1\u3002": "\u6b22\u8fce\u4f7f\u7528 OpenThesis\u3002\n\n\u7b2c\u4e00\u6b65\uff1a\u641c\u7d22\u6216\u5feb\u6377\u9078\u64c7\u516c\u53f8\uff1b\u7b2c\u4e8c\u6b65\uff1a\u78ba\u8a8d\u7814\u7a76\u6a21\u5757\u548c\u6a21\u578b\u8a2d\u7f6e\uff1b\u7b2c\u4e09\u6b65\uff1a\u9ede\u51fb\u9801\u9762\u9876\u90e8\u59cb\u7d42\u53ef\u89c1\u7684\u201c\u958b\u59cb\u7814\u7a76\u201d\u3002\n\n\u53ef\u4ee5\u9078\u64c7\u201c\u5408\u6210\u6f14\u793a\u516c\u53f8\u201d\u96e2\u7dda\u9a57\u8b49\u5b8c\u6574\u6d41\u7a0b\u3002\u7814\u7a76\u771f\u5be6\u516c\u53f8\u6642\uff0c\u8acb\u5728\u201c\u6a21\u578b\u4e0e SEC \u8a2d\u7f6e\u201d\u4e2d\u586b\u5199\u4f60\u81ea\u5df1\u7684 SEC \u806f\u4fc2\u90f5\u7bb1\u3002",
    "\u672c\u5730\u7814\u7a76\u5386\u53f2": "\u672c\u5730\u7814\u7a76\u6b77\u53f2",
    "\u5237\u65b0": "\u91cd\u65b0\u6574\u7406",
    "\u4ee3\u7801": "\u4ee3\u78bc",
    "\u516c\u53f8": "\u516c\u53f8",
    "\u72b6\u6001": "\u72c0\u614b",
    "\u5f00\u59cb\u65f6\u95f4": "\u958b\u59cb\u6642\u95f4",
    "\u6a21\u578b\u4e0e\u6570\u636e\u6e90\u8bbe\u7f6e": "\u6a21\u578b\u4e0e\u6578\u64da\u6e90\u8a2d\u7f6e",
    "\u8bf7\u9009\u62e9\u63d0\u4f9b\u65b9\u9884\u8bbe\u3002": "\u8acb\u9078\u64c7\u63d0\u4f9b\u65b9\u9810\u8a2d\u3002",
    "\u4e3b AI \u6a21\u578b\uff08\u53ef\u9009\uff09": "\u4e3b AI \u6a21\u578b\uff08\u53ef\u9078\uff09",
    "\u9996\u6b21\u542f\u52a8\u4e0d\u4f1a\u8c03\u7528 AI\uff1b\u53ea\u6709\u4e3b\u52a8\u9009\u62e9\u6a21\u578b\u5e76\u5f00\u59cb\u7814\u7a76\u65f6\u624d\u4f1a\u53d1\u9001\u7814\u7a76\u4e0a\u4e0b\u6587\u3002API Key \u53ea\u4fdd\u5b58\u5728\u5185\u5b58\u4e2d\uff0c\u4e0d\u5199\u5165\u6570\u636e\u5e93\u6216\u65e5\u5fd7\u3002": "\u9996\u6b21\u555f\u52d5\u4e0d\u6703\u8abf\u7528 AI\uff1b\u53ea\u6709\u4e3b\u52d5\u9078\u64c7\u6a21\u578b\u4e26\u958b\u59cb\u7814\u7a76\u6642\u624d\u6703\u767c\u9001\u7814\u7a76\u4e0a\u4e0b\u6587\u3002API Key \u53ea\u4fdd\u5b58\u5728\u5185\u5b58\u4e2d\uff0c\u4e0d\u5199\u5165\u6578\u64da\u5e93\u6216\u65e5\u5fd7\u3002",
    "SEC EDGAR \u8d22\u62a5\u8bbf\u95ee": "SEC EDGAR \u8ca1\u5831\u8bbf\u95ee",
    "SEC \u4e0d\u9700\u8981 API Key\uff0c\u4f46\u8981\u6c42\u8bf7\u6c42\u8005\u63d0\u4f9b\u771f\u5b9e\u3001\u53ef\u8054\u7cfb\u7684\u90ae\u7bb1\u3002": "SEC \u4e0d\u9700\u8981 API Key\uff0c\u4f46\u8981\u6c42\u8acb\u6c42\u8005\u63d0\u4f9b\u771f\u5be6\u3001\u53ef\u806f\u4fc2\u7684\u90f5\u7bb1\u3002",
    "\u5e2e\u52a9\uff1aSEC \u662f\u4ec0\u4e48\uff0c\u5982\u4f55\u83b7\u53d6\u8d22\u62a5\uff1f": "\u5e6b\u52a9\uff1aSEC \u662f\u4ec0\u4e48\uff0c\u5982\u4f55\u83b7\u53d6\u8ca1\u5831\uff1f",
    "\u5e38\u7528\u8bf7\u6c42\u8eab\u4efd\u6a21\u677f": "\u5e38\u7528\u8acb\u6c42\u8eab\u4efd\u6a21\u677f",
    "\u8054\u7cfb\u90ae\u7bb1\uff08\u586b\u5199\u4f60\u81ea\u5df1\u7684\uff09": "\u806f\u4fc2\u90f5\u7bb1\uff08\u586b\u5199\u4f60\u81ea\u5df1\u7684\uff09",
    "\u53d1\u9001\u7ed9 SEC \u7684\u8bf7\u6c42\u6807\u8bc6": "\u767c\u9001\u7ed9 SEC \u7684\u8acb\u6c42\u6a19\u8b58",
    "\u8bf7\u52ff\u586b\u5199\u76ee\u6807\u516c\u53f8\u7684\u6295\u8d44\u8005\u5173\u7cfb\u90ae\u7bb1\u3002\u8fd9\u91cc\u6807\u8bc6\u7684\u662f\u6570\u636e\u8bf7\u6c42\u8005\u3002\u90ae\u7bb1\u4fdd\u5b58\u5728\u672c\u673a\u8bbe\u7f6e\uff0c\u5e76\u968f SEC \u8bf7\u6c42\u53d1\u9001\u3002": "\u8acb\u52ff\u586b\u5199\u76ee\u6a19\u516c\u53f8\u7684\u6295\u8cc7\u8005\u95dc\u4fc2\u90f5\u7bb1\u3002\u8fd9\u91cc\u6a19\u8b58\u7684\u662f\u6578\u64da\u8acb\u6c42\u8005\u3002\u90f5\u7bb1\u4fdd\u5b58\u5728\u672c\u6a5f\u8a2d\u7f6e\uff0c\u4e26\u96a8 SEC \u8acb\u6c42\u767c\u9001\u3002",
    "\u4fdd\u5b58\u672c\u673a\u8bbe\u7f6e\uff08\u4e0d\u4fdd\u5b58 API Key\uff09": "\u4fdd\u5b58\u672c\u6a5f\u8a2d\u7f6e\uff08\u4e0d\u4fdd\u5b58 API Key\uff09",
    "\u6d4b\u8bd5\u6a21\u578b\u8fde\u63a5": "\u6e2c\u8a66\u6a21\u578b\u9023\u63a5",
    "\u25b6 \u53ef\u9009\uff1a\u7b2c\u4e8c\u4e2a\u5bf9\u6bd4\u6a21\u578b": "\u25b6 \u53ef\u9078\uff1a\u7b2c\u4e8c\u500b\u5bf9\u6bd4\u6a21\u578b",
    "\u25bc \u53ef\u9009\uff1a\u7b2c\u4e8c\u4e2a\u5bf9\u6bd4\u6a21\u578b": "\u25bc \u53ef\u9078\uff1a\u7b2c\u4e8c\u500b\u5bf9\u6bd4\u6a21\u578b",
    "\u7b2c\u4e8c\u4e2a\u5bf9\u6bd4\u6a21\u578b": "\u7b2c\u4e8c\u500b\u5bf9\u6bd4\u6a21\u578b",
    "\u63d0\u4f9b\u65b9\u9884\u8bbe": "\u63d0\u4f9b\u65b9\u9810\u8a2d",
    "\u6a21\u578b\u540d\u79f0": "\u6a21\u578b\u540d\u7a31",
    "\u5728\u7ebf\u76ee\u5f55": "\u5728\u7dda\u76ee\u9304",
    "\u5237\u65b0\u5728\u7ebf\u6a21\u578b": "\u5237\u65b0\u5728\u7dda\u6a21\u578b",
    "\u63a5\u53e3\u5730\u5740": "\u63a5\u53e3\u5730\u5740",
    "API Key\uff08\u4ec5\u672c\u6b21\u4f1a\u8bdd\uff09": "API Key\uff08\u50c5\u672c\u6b21\u6703\u8bdd\uff09",
    "\u5e2e\u52a9": "\u5e6b\u52a9",
    "\u83b7\u53d6 API Key / \u5b89\u88c5\u5e2e\u52a9": "\u83b7\u53d6 API Key / \u5b89\u88dd\u5e6b\u52a9",
    ".othesis \u7814\u7a76\u6a21\u5757": ".othesis \u7814\u7a76\u6a21\u5757",
    "\u5bfc\u5165\u6a21\u5757": "\u5c0e\u5165\u6a21\u5757",
    "v0.1 \u6a21\u5757\u4ec5\u5141\u8bb8 Markdown\u3001JSON \u517c\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u672c\uff1b\u4e0d\u5141\u8bb8\u8fd0\u884c\u4ee3\u7801\u3001\u8bbf\u95ee\u6587\u4ef6\u7cfb\u7edf\u3001\u7f51\u7edc\u6216\u5bc6\u94a5\u3002": "v0.1 \u6a21\u5757\u50c5\u5141\u8bb8 Markdown\u3001JSON \u517c\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u672c\uff1b\u4e0d\u5141\u8bb8\u904b\u884c\u4ee3\u78bc\u3001\u8bbf\u95ee\u6587\u4ef6\u4fc2\u7d71\u3001\u7db2\u7d61\u6216\u5bc6\u9470\u3002",
    "\u6295\u8d44\u903b\u8f91\u7248\u672c": "\u6295\u8cc7\u903b\u8f91\u7248\u672c",
    "\u7248\u672c": "\u7248\u672c",
    "\u521b\u5efa\u8005": "\u5275\u5efa\u8005",
    "\u65f6\u95f4": "\u6642\u95f4",
    "\u53ef\u7f16\u8f91 Thesis JSON": "\u53ef\u7f16\u8f91 Thesis JSON",
    "\u53e6\u5b58\u4e3a\u65b0\u7248\u672c": "\u53e6\u5b58\u4e3a\u65b0\u7248\u672c",
    "\u754c\u9762\u4e0e\u62a5\u544a\u8bed\u8a00": "\u754c\u9762\u4e0e\u5831\u544a\u8a9e\u8a00",
    "\u754c\u9762\u8bed\u8a00": "\u4ecb\u9762\u8a9e\u8a00",
    "\u7814\u7a76\u62a5\u544a\u8bed\u8a00": "\u7814\u7a76\u5831\u544a\u8a9e\u8a00",
    "\u754c\u9762\u8bed\u8a00\u5c06\u5728\u4e0b\u6b21\u542f\u52a8\u65f6\u751f\u6548\u3002": "\u754c\u9762\u8a9e\u8a00\u5c06\u5728\u4e0b\u6b21\u555f\u52d5\u6642\u751f\u6548\u3002",
    "\u62a5\u544a\u8bed\u8a00\u7acb\u5373\u7528\u4e8e\u4e0b\u4e00\u6b21\u7814\u7a76\uff1b\u5386\u53f2\u62a5\u544a\u53ea\u7ffb\u8bd1\u7a0b\u5e8f\u751f\u6210\u7684\u6807\u9898\uff0cAI \u6b63\u6587\u4fdd\u6301\u539f\u6587\u3002": "\u5831\u544a\u8a9e\u8a00\u7acb\u5373\u7528\u65bc\u4e0b\u4e00\u6b21\u7814\u7a76\uff1b\u6b77\u53f2\u5831\u544a\u53ea\u7ffb\u8bd1\u7a0b\u5e8f\u751f\u6210\u7684\u6a19\u9898\uff0cAI \u6b63\u6587\u4fdd\u6301\u539f\u6587\u3002",
    "\u4fdd\u5b58\u8bed\u8a00\u8bbe\u7f6e": "\u5132\u5b58\u8a9e\u8a00\u8a2d\u5b9a",
    "\u8bed\u8a00\u8bbe\u7f6e\u5df2\u4fdd\u5b58\u3002": "\u8a9e\u8a00\u8a2d\u7f6e\u5df2\u4fdd\u5b58\u3002",
    "\u754c\u9762\u8bed\u8a00\u5c06\u5728\u91cd\u542f OpenThesis \u540e\u751f\u6548\u3002": "\u754c\u9762\u8a9e\u8a00\u5c06\u5728\u91cd\u555f OpenThesis \u5f8c\u751f\u6548\u3002",
    "\u62a5\u544a\u8bed\u8a00\u5df2\u5e94\u7528\u4e8e\u4e0b\u4e00\u6b21\u7814\u7a76\u3002": "\u5831\u544a\u8a9e\u8a00\u5df2\u5e94\u7528\u65bc\u4e0b\u4e00\u6b21\u7814\u7a76\u3002",
    "\u672c\u5730\u6570\u636e\u76ee\u5f55\uff1a{path}": "\u672c\u5730\u6578\u64da\u76ee\u9304\uff1a{path}",
    "\u9762\u5411\u4e2a\u4eba\u957f\u671f\u6295\u8d44\u8005\u7684\u5f00\u6e90\u3001\u6a21\u578b\u65e0\u5173\u516c\u53f8\u7814\u7a76\u7cfb\u7edf\u3002": "\u9762\u5411\u500b\u4eba\u9577\u671f\u6295\u8cc7\u8005\u7684\u958b\u6e90\u3001\u6a21\u578b\u7121\u95dc\u516c\u53f8\u7814\u7a76\u4fc2\u7d71\u3002",
    "\u539f\u5219\uff1a\u6bcf\u4e2a\u4e8b\u5b9e\u90fd\u9700\u8981\u8bc1\u636e\uff1b\u8d22\u52a1\u8ba1\u7b97\u7531\u786e\u5b9a\u6027\u7a0b\u5e8f\u5b8c\u6210\uff1b\u9884\u6d4b\u4f7f\u7528\u60c5\u666f\u3001\u533a\u95f4\u548c\u5931\u6548\u6761\u4ef6\uff1bAI \u4e0d\u6267\u884c\u4efb\u4f55\u4ea4\u6613\u3002": "\u539f\u5219\uff1a\u6bcf\u500b\u4e8b\u5be6\u90fd\u9700\u8981\u8b49\u64da\uff1b\u8ca1\u52d9\u8a08\u7b97\u7531\u78ba\u5b9a\u6027\u7a0b\u5e8f\u5b8c\u6210\uff1b\u9810\u6e2c\u4f7f\u7528\u60c5\u666f\u3001\u533a\u95f4\u548c\u5931\u6548\u6761\u4ef6\uff1bAI \u4e0d\u57f7\u884c\u4efb\u4f55\u4ea4\u6613\u3002",
    "SEC \u8054\u7cfb\u90ae\u7bb1\u65e0\u6548": "SEC \u806f\u4fc2\u90f5\u7bb1\u7121\u6548",
    "\u8bf7\u8f93\u5165\u4f60\u672c\u4eba\u6216\u6240\u5728\u7814\u7a76\u56e2\u961f\u53ef\u6b63\u5e38\u6536\u4fe1\u7684\u90ae\u7bb1\u5730\u5740\u3002": "\u8acb\u8f93\u5165\u4f60\u672c\u4eba\u6216\u6240\u5728\u7814\u7a76\u5718\u968a\u53ef\u6b63\u5e38\u6536\u4fe1\u7684\u90f5\u7bb1\u5730\u5740\u3002",
    "\u8bf7\u9009\u62e9\u4e00\u4e2a SEC \u8bf7\u6c42\u8eab\u4efd\u6a21\u677f\u3002": "\u8acb\u9078\u64c7\u4e00\u500b SEC \u8acb\u6c42\u8eab\u4efd\u6a21\u677f\u3002",
    "\u8bf7\u9009\u62e9\u4e00\u4e2a\u5185\u7f6e\u7684\u5e38\u7528\u516c\u53f8\u3002": "\u8acb\u9078\u64c7\u4e00\u500b\u5185\u7f6e\u7684\u5e38\u7528\u516c\u53f8\u3002",
    "\u8bbe\u7f6e\u5df2\u4fdd\u5b58\uff1bAPI Key \u672a\u6301\u4e45\u5316": "\u8a2d\u7f6e\u5df2\u4fdd\u5b58\uff1bAPI Key \u672a\u6301\u4e45\u5316",
    "\u586b\u5199\u90ae\u7bb1\u540e\u81ea\u52a8\u751f\u6210\uff0c\u65e0\u9700\u7533\u8bf7 SEC API Key": "\u586b\u5199\u90f5\u7bb1\u5f8c\u81ea\u52d5\u751f\u6210\uff0c\u7121\u9700\u7533\u8acb SEC API Key",
    "\u90ae\u7bb1\u683c\u5f0f\u5c1a\u672a\u5b8c\u6210": "\u90f5\u7bb1\u683c\u5f0f\u5c1a\u672a\u5b8c\u6210",
    "SEC EDGAR \u4f7f\u7528\u5e2e\u52a9": "SEC EDGAR \u4f7f\u7528\u5e6b\u52a9",
    "SEC \u662f\u4ec0\u4e48\uff0cOpenThesis \u5982\u4f55\u83b7\u53d6\u8d22\u62a5\uff1f": "SEC \u662f\u4ec0\u4e48\uff0cOpenThesis \u5982\u4f55\u83b7\u53d6\u8ca1\u5831\uff1f",
    "\u6253\u5f00 SEC \u5b98\u65b9\u5f00\u53d1\u8005\u8bf4\u660e": "\u6253\u958b SEC \u5b98\u65b9\u958b\u767c\u8005\u8aaa\u660e",
    "\u5173\u95ed": "\u95dc\u9589",
    "\u5f53\u524d\u4e0d\u4f1a\u8c03\u7528 AI\u3002": "\u5f53\u524d\u4e0d\u6703\u8abf\u7528 AI\u3002",
    "\u672c\u6b21\u4f1a\u8bdd\u5df2\u7f13\u5b58 {count} \u4e2a\u5728\u7ebf\u6a21\u578b\u3002": "\u672c\u6b21\u6703\u8bdd\u5df2\u7f13\u5b58 {count} \u500b\u5728\u7dda\u6a21\u578b\u3002",
    "\u6b64\u63d0\u4f9b\u65b9\u4f7f\u7528\u5185\u7f6e\u6a21\u578b\u5217\u8868\uff0c\u4e5f\u53ef\u624b\u52a8\u586b\u5199\u6a21\u578b ID\u3002": "\u6b64\u63d0\u4f9b\u65b9\u4f7f\u7528\u5185\u7f6e\u6a21\u578b\u5217\u8868\uff0c\u4e5f\u53ef\u624b\u52d5\u586b\u5199\u6a21\u578b ID\u3002",
    "\u53ef\u5237\u65b0\u672c\u673a\u5df2\u5b89\u88c5\u6a21\u578b\uff1b\u672a\u5b89\u88c5\u65f6\u8bf7\u5148\u4f7f\u7528\u5e2e\u52a9\u94fe\u63a5\u3002": "\u53ef\u5237\u65b0\u672c\u6a5f\u5df2\u5b89\u88dd\u6a21\u578b\uff1b\u672a\u5b89\u88dd\u6642\u8acb\u5148\u4f7f\u7528\u5e6b\u52a9\u94fe\u63a5\u3002",
    "\u5df2\u52a0\u8f7d\u5185\u7f6e\u63a8\u8350\u6a21\u578b\uff1b\u53ef\u624b\u52a8\u5237\u65b0\u5728\u7ebf\u76ee\u5f55\u3002": "\u5df2\u52a0\u8f09\u5185\u7f6e\u63a8\u85a6\u6a21\u578b\uff1b\u53ef\u624b\u52d5\u5237\u65b0\u5728\u7dda\u76ee\u9304\u3002",
    "\u5f53\u524d\u672a\u542f\u7528 AI\uff0c\u65e0\u9700\u5237\u65b0\u3002": "\u5f53\u524d\u672a\u555f\u7528 AI\uff0c\u7121\u9700\u5237\u65b0\u3002",
    "\u6b63\u5728\u540e\u53f0\u5237\u65b0\u5728\u7ebf\u6a21\u578b\u2026": "\u6b63\u5728\u5f8c\u53f0\u5237\u65b0\u5728\u7dda\u6a21\u578b\u2026",
    "\u5728\u7ebf\u6a21\u578b\u76ee\u5f55\u5237\u65b0\u5931\u8d25\uff0c\u5df2\u4fdd\u7559\u5185\u7f6e\u5217\u8868\u3002": "\u5728\u7dda\u6a21\u578b\u76ee\u9304\u5237\u65b0\u5931\u6557\uff0c\u5df2\u4fdd\u7559\u5185\u7f6e\u5217\u8868\u3002",
    "\u6a21\u578b\u5e2e\u52a9": "\u6a21\u578b\u5e6b\u52a9",
    "\u81ea\u5b9a\u4e49\u63a5\u53e3\u8bf7\u5411\u670d\u52a1\u63d0\u4f9b\u65b9\u83b7\u53d6 API Key\u3001\u6a21\u578b ID \u548c\u517c\u5bb9\u5730\u5740\u3002": "\u81ea\u5b9a\u4e49\u63a5\u53e3\u8acb\u5411\u670d\u52d9\u63d0\u4f9b\u65b9\u83b7\u53d6 API Key\u3001\u6a21\u578b ID \u548c\u517c\u5bb9\u5730\u5740\u3002",
    "\u641c\u7d22\u516c\u53f8": "\u641c\u7d22\u516c\u53f8",
    "\u8bf7\u8f93\u5165\u80a1\u7968\u4ee3\u7801\u6216\u516c\u53f8\u540d\u79f0\u3002": "\u8acb\u8f93\u5165\u80a1\u7968\u4ee3\u78bc\u6216\u516c\u53f8\u540d\u7a31\u3002",
    "\u9700\u8981 SEC \u8054\u7cfb\u90ae\u7bb1": "\u9700\u8981 SEC \u806f\u4fc2\u90f5\u7bb1",
    "{error}\n\n\u8bf7\u5728\u201c\u6a21\u578b\u4e0e\u6570\u636e\u6e90\u8bbe\u7f6e\u201d\u4e2d\u586b\u5199\u540e\u4fdd\u5b58\u3002": "{error}\n\n\u8acb\u5728\u201c\u6a21\u578b\u4e0e\u6578\u64da\u6e90\u8a2d\u7f6e\u201d\u4e2d\u586b\u5199\u5f8c\u4fdd\u5b58\u3002",
    "\u6b63\u5728\u67e5\u8be2 SEC \u516c\u53f8\u5217\u8868\u2026": "\u6b63\u5728\u67e5\u8be2 SEC \u516c\u53f8\u5217\u8868\u2026",
    "\u9009\u62e9\u5e38\u7528\u516c\u53f8": "\u9078\u64c7\u5e38\u7528\u516c\u53f8",
    "\u5df2\u9009\u62e9\u5e38\u7528\u516c\u53f8\uff1a{ticker} \u00b7 {name}\u3002\n\n\u8bf7\u786e\u8ba4\u7814\u7a76\u914d\u7f6e\uff0c\u7136\u540e\u70b9\u51fb\u9875\u9762\u9876\u90e8\u7684\u201c\u5f00\u59cb\u7814\u7a76\u201d\u3002": "\u5df2\u9078\u64c7\u5e38\u7528\u516c\u53f8\uff1a{ticker} \u00b7 {name}\u3002\n\n\u8acb\u78ba\u8a8d\u7814\u7a76\u914d\u7f6e\uff0c\u7136\u5f8c\u9ede\u51fb\u9801\u9762\u9876\u90e8\u7684\u201c\u958b\u59cb\u7814\u7a76\u201d\u3002",
    "\u5df2\u9009\u62e9\u5408\u6210\u6f14\u793a\u516c\u53f8\u3002\u6240\u6709\u6570\u636e\u5747\u4e3a\u865a\u6784\uff0c\u53ea\u7528\u4e8e\u9a8c\u8bc1\u8f6f\u4ef6\u529f\u80fd\u3002": "\u5df2\u9078\u64c7\u5408\u6210\u6f14\u793a\u516c\u53f8\u3002\u6240\u6709\u6578\u64da\u5747\u4e3a\u865a\u69cb\uff0c\u53ea\u7528\u65bc\u9a57\u8b49\u8f6f\u4ef6\u529f\u80fd\u3002",
    "\u5df2\u6062\u590d\u4e0a\u6b21\u5f02\u5e38\u4e2d\u65ad\u7684\u4efb\u52a1\u8bb0\u5f55\uff08{count}\uff09": "\u5df2\u6062\u8907\u4e0a\u6b21\u5f02\u5e38\u4e2d\u65ad\u7684\u4efb\u52d9\u8a18\u9304\uff08{count}\uff09",
    "\u4e0a\u6b21\u5173\u95ed\u5e94\u7528\u65f6\u4ecd\u6709\u7814\u7a76\u5728\u8fd0\u884c\uff0c\u73b0\u5df2\u5b89\u5168\u6807\u8bb0\u4e3a\u201c\u5df2\u53d6\u6d88\u201d\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u4ea7\u7269\u4ecd\u53ef\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u67e5\u770b\u3002": "\u4e0a\u6b21\u95dc\u9589\u5e94\u7528\u6642\u4ecd\u6709\u7814\u7a76\u5728\u904b\u884c\uff0c\u73fe\u5df2\u5b89\u5168\u6a19\u8a18\u4e3a\u201c\u5df2\u53d6\u6d88\u201d\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u7522\u7269\u4ecd\u53ef\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u67e5\u770b\u3002",
    "\u5982\u9700\u7ee7\u7eed\uff0c\u8bf7\u91cd\u65b0\u9009\u62e9\u516c\u53f8\u5e76\u8fd0\u884c\u7814\u7a76\u3002": "\u5982\u9700\u7ee7\u7e8c\uff0c\u8acb\u91cd\u65b0\u9078\u64c7\u516c\u53f8\u4e26\u904b\u884c\u7814\u7a76\u3002",
    "\u6b63\u5728\u51c6\u5907 {ticker} \u7684\u7814\u7a76\u6570\u636e": "\u6b63\u5728\u6e96\u5099 {ticker} \u7684\u7814\u7a76\u6578\u64da",
    "\u4efb\u52a1\u6b63\u5728\u540e\u53f0\u8fd0\u884c\uff1b\u7a97\u53e3\u4fdd\u6301\u54cd\u5e94\uff0c\u53ef\u4ee5\u968f\u65f6\u67e5\u770b\u5f53\u524d\u9636\u6bb5\u3002": "\u4efb\u52d9\u6b63\u5728\u5f8c\u53f0\u904b\u884c\uff1b\u7a97\u53e3\u4fdd\u6301\u54cd\u5e94\uff0c\u53ef\u4ee5\u96a8\u6642\u67e5\u770b\u5f53\u524d\u968e\u6bb5\u3002",
    "\u5b8c\u6210\u540e\u5c06\u5728\u8fd9\u91cc\u663e\u793a\u5b8c\u6574\u62a5\u544a\u3002": "\u5b8c\u6210\u5f8c\u5c06\u5728\u8fd9\u91cc\u986f\u793a\u5b8c\u6574\u5831\u544a\u3002",
    "\u7814\u7a76\u6b63\u5728\u8fdb\u884c\u4e2d\n\n": "\u7814\u7a76\u6b63\u5728\u9032\u884c\u4e2d\n\n",
    "\u5df2\u7528\u65f6 {elapsed}": "\u5df2\u7528\u6642 {elapsed}",
    "\u53d6\u6d88\u8bf7\u6c42\u5df2\u6536\u5230\uff1b\u6b63\u5728\u7b49\u5f85\u5f53\u524d\u7f51\u7edc\u8bf7\u6c42\u5b89\u5168\u7ed3\u675f\uff0c\u4e0d\u4f1a\u518d\u542f\u52a8\u65b0\u7684\u7814\u7a76\u6b65\u9aa4\u3002": "\u53d6\u6d88\u8acb\u6c42\u5df2\u6536\u5230\uff1b\u6b63\u5728\u7b49\u5f85\u5f53\u524d\u7db2\u7d61\u8acb\u6c42\u5b89\u5168\u7ed3\u675f\uff0c\u4e0d\u6703\u518d\u555f\u52d5\u65b0\u7684\u7814\u7a76\u6b65\u9aa4\u3002",
    "\u540e\u53f0\u4ecd\u5728\u5de5\u4f5c \u00b7 \u5f53\u524d\u6b65\u9aa4\u5df2\u7b49\u5f85 {elapsed} \u00b7 \u6a21\u578b\u7814\u7a76\u901a\u5e38\u9700\u8981\u6570\u5206\u949f\uff0c\u8bf7\u52ff\u5173\u95ed\u5e94\u7528\u3002": "\u5f8c\u53f0\u4ecd\u5728\u5de5\u4f5c \u00b7 \u5f53\u524d\u6b65\u9aa4\u5df2\u7b49\u5f85 {elapsed} \u00b7 \u6a21\u578b\u7814\u7a76\u901a\u5e38\u9700\u8981\u6578\u5206\u949f\uff0c\u8acb\u52ff\u95dc\u9589\u5e94\u7528\u3002",
    "\u6b63\u5728\u53d6\u6d88\u7814\u7a76\u2026": "\u6b63\u5728\u53d6\u6d88\u7814\u7a76\u2026",
    "\u5df2\u505c\u6b62\u542f\u52a8\u65b0\u6b65\u9aa4\uff1b\u5f53\u524d\u7f51\u7edc\u8bf7\u6c42\u7ed3\u675f\u540e\u4f1a\u5b89\u5168\u4fdd\u5b58\u4e2d\u95f4\u7ed3\u679c\u3002": "\u5df2\u505c\u6b62\u555f\u52d5\u65b0\u6b65\u9aa4\uff1b\u5f53\u524d\u7db2\u7d61\u8acb\u6c42\u7ed3\u675f\u5f8c\u6703\u5b89\u5168\u4fdd\u5b58\u4e2d\u95f4\u7ed3\u679c\u3002",
    "\u6b63\u5728\u5b89\u5168\u53d6\u6d88\u7814\u7a76\u2026": "\u6b63\u5728\u5b89\u5168\u53d6\u6d88\u7814\u7a76\u2026",
    "\u6280\u672f\u4fe1\u606f\uff1a{message}": "\u6280\u8853\u4fe1\u606f\uff1a{message}",
    "\u7814\u7a76\u4efb\u52a1\u672a\u5b8c\u6210\n\n": "\u7814\u7a76\u4efb\u52d9\u672a\u5b8c\u6210\n\n",
    "\u7814\u7a76\u8fdb\u884c\u4e2d\u2026": "\u7814\u7a76\u9032\u884c\u4e2d\u2026",
    "\u6b63\u5728\u8fd0\u884c\u591a Agent \u7814\u7a76\u6d41\u7a0b\uff0c\u8bf7\u67e5\u770b\u4e0b\u65b9\u4efb\u52a1\u8fdb\u5ea6\u3002": "\u6b63\u5728\u904b\u884c\u591a Agent \u7814\u7a76\u6d41\u7a0b\uff0c\u8acb\u67e5\u770b\u4e0b\u65b9\u4efb\u52d9\u9032\u5ea6\u3002",
    "\u516c\u53f8\u5df2\u9009\u62e9\uff1b\u786e\u8ba4\u914d\u7f6e\u540e\u5373\u53ef\u5f00\u59cb\u3002": "\u516c\u53f8\u5df2\u9078\u64c7\uff1b\u78ba\u8a8d\u914d\u7f6e\u5f8c\u5373\u53ef\u958b\u59cb\u3002",
    "\u8bf7\u5148\u9009\u62e9\u516c\u53f8\u3002": "\u8acb\u5148\u9078\u64c7\u516c\u53f8\u3002",
    "{error}\n\n\u771f\u5b9e\u516c\u53f8\u7814\u7a76\u9700\u8981\u8bbf\u95ee SEC\uff0c\u8bf7\u5148\u5b8c\u6210 SEC \u8bbe\u7f6e\u3002": "{error}\n\n\u771f\u5be6\u516c\u53f8\u7814\u7a76\u9700\u8981\u8bbf\u95ee SEC\uff0c\u8acb\u5148\u5b8c\u6210 SEC \u8a2d\u7f6e\u3002",
    "\u53cd\u5411 DCF \u8f93\u5165\u9519\u8bef": "\u53cd\u5411 DCF \u8f93\u5165\u932f\u8bef",
    "\u53cd\u5411 DCF \u8f93\u5165\u5fc5\u987b\u662f\u6570\u5b57": "\u53cd\u5411 DCF \u8f93\u5165\u5fc5\u987b\u662f\u6578\u5b57",
    "\u5e02\u503c\u5fc5\u987b\u4e3a\u6b63\u6570\uff0c\u4e14\u6298\u73b0\u7387\u5fc5\u987b\u9ad8\u4e8e\u6c38\u7eed\u589e\u957f\u7387": "\u5e02\u503c\u5fc5\u987b\u4e3a\u6b63\u6578\uff0c\u4e14\u6298\u73fe\u7387\u5fc5\u987b\u9ad8\u65bc\u6c38\u7e8c\u589e\u9577\u7387",
    "\u53cc\u6a21\u578b\u914d\u7f6e\u4e0d\u5b8c\u6574": "\u53cc\u6a21\u578b\u914d\u7f6e\u4e0d\u5b8c\u6574",
    "\u542f\u7528\u6a21\u578b\u6bd4\u8f83\u65f6\uff0c\u4e3b\u6a21\u578b\u548c\u7b2c\u4e8c\u6a21\u578b\u90fd\u5fc5\u987b\u914d\u7f6e\u63d0\u4f9b\u65b9\u3001\u6a21\u578b\u540d\u79f0\u548c\u63a5\u53e3\u5730\u5740\u3002": "\u555f\u7528\u6a21\u578b\u6bd4\u8f83\u6642\uff0c\u4e3b\u6a21\u578b\u548c\u7b2c\u4e8c\u6a21\u578b\u90fd\u5fc5\u987b\u914d\u7f6e\u63d0\u4f9b\u65b9\u3001\u6a21\u578b\u540d\u7a31\u548c\u63a5\u53e3\u5730\u5740\u3002",
    "\u6b63\u5728\u52a0\u8f7d\u79bb\u7ebf\u6f14\u793a\u6570\u636e": "\u6b63\u5728\u52a0\u8f09\u96e2\u7dda\u6f14\u793a\u6578\u64da",
    "\u6f14\u793a\u6570\u636e\u51c6\u5907\u5b8c\u6210": "\u6f14\u793a\u6578\u64da\u6e96\u5099\u5b8c\u6210",
    "\u6b63\u5728\u83b7\u53d6 SEC \u5e74\u62a5\u6e05\u5355": "\u6b63\u5728\u83b7\u53d6 SEC \u5e74\u5831\u6e05\u55ae",
    "\u6b63\u5728\u4e0b\u8f7d SEC 10-K\uff08{index}/{total}\uff09": "\u6b63\u5728\u4e0b\u8f09 SEC 10-K\uff08{index}/{total}\uff09",
    "\u6b63\u5728\u89e3\u6790\u8d22\u62a5\u8bc1\u636e\u4e0e\u8868\u683c": "\u6b63\u5728\u89e3\u6790\u8ca1\u5831\u8b49\u64da\u4e0e\u8868\u683c",
    "\u6b63\u5728\u83b7\u53d6 SEC Company Facts": "\u6b63\u5728\u83b7\u53d6 SEC Company Facts",
    "\u7814\u7a76\u6570\u636e\u51c6\u5907\u5b8c\u6210\uff0c\u6b63\u5728\u542f\u52a8 Agent": "\u7814\u7a76\u6578\u64da\u6e96\u5099\u5b8c\u6210\uff0c\u6b63\u5728\u555f\u52d5 Agent",
    "\u4e3b\u6a21\u578b\u7814\u7a76\u5b8c\u6210\uff0c\u6b63\u5728\u542f\u52a8\u5bf9\u6bd4\u6a21\u578b": "\u4e3b\u6a21\u578b\u7814\u7a76\u5b8c\u6210\uff0c\u6b63\u5728\u555f\u52d5\u5bf9\u6bd4\u6a21\u578b",
    "\u4e3b\u6a21\u578b\uff1a": "\u4e3b\u6a21\u578b\uff1a",
    "\u5bf9\u6bd4\u6a21\u578b\uff1a": "\u5bf9\u6bd4\u6a21\u578b\uff1a",
    "\u53cc\u6a21\u578b\u5206\u6b67\u6bd4\u8f83\u5b8c\u6210": "\u53cc\u6a21\u578b\u5206\u6b67\u6bd4\u8f83\u5b8c\u6210",
    "\u7814\u7a76\u4efb\u52a1\u6b63\u5728\u8fd0\u884c\u2026": "\u7814\u7a76\u4efb\u52d9\u6b63\u5728\u904b\u884c\u2026",
    "\u5df2\u5b8c\u6210\u786e\u5b9a\u6027\u8d22\u52a1\u8ba1\u7b97": "\u5df2\u5b8c\u6210\u78ba\u5b9a\u6027\u8ca1\u52d9\u8a08\u7b97",
    "\u57fa\u7840\u8d22\u52a1\u5206\u6790\u5b8c\u6210\uff1b\u914d\u7f6e\u6a21\u578b\u540e\u53ef\u8fd0\u884c\u5b8c\u6574\u7814\u7a76": "\u57fa\u7840\u8ca1\u52d9\u5206\u6790\u5b8c\u6210\uff1b\u914d\u7f6e\u6a21\u578b\u5f8c\u53ef\u904b\u884c\u5b8c\u6574\u7814\u7a76",
    "\u6b63\u5728\u5e76\u884c\u8fd0\u884c\u8d22\u52a1\u3001\u5546\u4e1a\u4e0e\u4f1a\u8ba1\u98ce\u9669 Agent\uff080/3\uff09": "\u6b63\u5728\u4e26\u884c\u904b\u884c\u8ca1\u52d9\u3001\u5546\u696d\u4e0e\u6703\u8a08\u98ce\u9669 Agent\uff080/3\uff09",
    "\u57fa\u7840\u5206\u6790 Agent \u5df2\u5b8c\u6210 {completed}/3\uff1a{agent_id}": "\u57fa\u7840\u5206\u6790 Agent \u5df2\u5b8c\u6210 {completed}/3\uff1a{agent_id}",
    "\u57fa\u7840\u7814\u7a76\u6863\u6848\u5b8c\u6210": "\u57fa\u7840\u7814\u7a76\u6863\u6848\u5b8c\u6210",
    "\u6b63\u5728\u7814\u7a76\u516c\u53f8\u4e0e\u884c\u4e1a\u589e\u957f\u673a\u4f1a": "\u6b63\u5728\u7814\u7a76\u516c\u53f8\u4e0e\u884c\u696d\u589e\u9577\u6a5f\u6703",
    "\u589e\u957f\u673a\u4f1a\u7814\u7a76\u5b8c\u6210": "\u589e\u9577\u6a5f\u6703\u7814\u7a76\u5b8c\u6210",
    "\u6b63\u5728\u8fdb\u884c\u53cd\u65b9\u5ba1\u67e5\u4e0e\u538b\u529b\u6d4b\u8bd5": "\u6b63\u5728\u9032\u884c\u53cd\u65b9\u5be9\u67e5\u4e0e\u538b\u529b\u6e2c\u8a66",
    "\u53cd\u65b9\u5ba1\u67e5\u5b8c\u6210": "\u53cd\u65b9\u5be9\u67e5\u5b8c\u6210",
    "\u6b63\u5728\u751f\u6210\u957f\u671f\u7ecf\u8425\u60c5\u666f": "\u6b63\u5728\u751f\u6210\u9577\u671f\u7ecf\u71df\u60c5\u666f",
    "\u957f\u671f\u60c5\u666f\u5b8c\u6210": "\u9577\u671f\u60c5\u666f\u5b8c\u6210",
    "\u6b63\u5728\u5408\u6210\u6700\u7ec8\u957f\u671f\u7814\u7a76\u62a5\u544a": "\u6b63\u5728\u5408\u6210\u6700\u7d42\u9577\u671f\u7814\u7a76\u5831\u544a",
    "\u6d4b\u8bd5\u6a21\u578b": "\u6e2c\u8a66\u6a21\u578b",
    "\u5f53\u524d\u9009\u62e9 none\uff0c\u4e0d\u4f1a\u8c03\u7528\u8bed\u8a00\u6a21\u578b\u3002": "\u5f53\u524d\u9078\u64c7 none\uff0c\u4e0d\u6703\u8abf\u7528\u8a9e\u8a00\u6a21\u578b\u3002",
    "\u672a\u914d\u7f6e\u6a21\u578b": "\u672a\u914d\u7f6e\u6a21\u578b",
    "\u6b63\u5728\u6d4b\u8bd5\u6a21\u578b\u8fde\u63a5\u2026": "\u6b63\u5728\u6e2c\u8a66\u6a21\u578b\u9023\u63a5\u2026",
    "\u5bfc\u5165 OpenThesis \u7814\u7a76\u6a21\u5757": "\u5c0e\u5165 OpenThesis \u7814\u7a76\u6a21\u5757",
    "\u7814\u7a76\u6a21\u5757\u9a8c\u8bc1\u5931\u8d25": "\u7814\u7a76\u6a21\u5757\u9a57\u8b49\u5931\u6557",
    "\u7814\u7a76\u6a21\u5757\u5df2\u5b89\u88c5": "\u7814\u7a76\u6a21\u5757\u5df2\u5b89\u88dd",
    "{name}\n\u7248\u672c\uff1a{version}\n\u54c8\u5e0c\uff1a{hash}": "{name}\n\u7248\u672c\uff1a{version}\n\u54c8\u5e0c\uff1a{hash}",
    "\u8bf7\u5148\u9009\u62e9\u4e00\u4e2a\u5df2\u6709\u7248\u672c\u3002": "\u8acb\u5148\u9078\u64c7\u4e00\u500b\u5df2\u6709\u7248\u672c\u3002",
    "JSON \u683c\u5f0f\u9519\u8bef": "JSON \u683c\u5f0f\u932f\u8bef",
    "\u7b2c {line} \u884c\uff0c\u7b2c {column} \u5217\uff1a{message}": "\u7b2c {line} \u884c\uff0c\u7b2c {column} \u5217\uff1a{message}",
    "\u5df2\u4fdd\u5b58\u4e3a v{version}": "\u5df2\u4fdd\u5b58\u4e3a v{version}",
    "\u5bfc\u51fa\u62a5\u544a": "\u5c0e\u51fa\u5831\u544a",
    "\u5f53\u524d\u6ca1\u6709\u53ef\u5bfc\u51fa\u7684\u5185\u5bb9\u3002": "\u5f53\u524d\u6ca1\u6709\u53ef\u5c0e\u51fa\u7684\u5185\u5bb9\u3002",
    "\u5bfc\u51fa OpenThesis \u62a5\u544a": "\u5c0e\u51fa OpenThesis \u5831\u544a",
    "\u62a5\u544a\u5df2\u5bfc\u51fa\uff1a{path}": "\u5831\u544a\u5df2\u5c0e\u51fa\uff1a{path}",
    "\u627e\u5230 {count} \u5bb6\u516c\u53f8": "\u627e\u5230 {count} \u5bb6\u516c\u53f8",
    "\u7814\u7a76\u5b8c\u6210": "\u7814\u7a76\u5b8c\u6210",
    "\u7814\u7a76\u5b8c\u6210\uff1a{status}": "\u7814\u7a76\u5b8c\u6210\uff1a{status}",
    "\u5168\u90e8\u7814\u7a76\u9636\u6bb5\u5df2\u5b8c\u6210\uff0c\u5b8c\u6574\u62a5\u544a\u548c\u4e2d\u95f4\u4ea7\u7269\u5df2\u7ecf\u4fdd\u5b58\u3002": "\u5168\u90e8\u7814\u7a76\u968e\u6bb5\u5df2\u5b8c\u6210\uff0c\u5b8c\u6574\u5831\u544a\u548c\u4e2d\u95f4\u7522\u7269\u5df2\u7ecf\u4fdd\u5b58\u3002",
    "\u7814\u7a76\u5df2\u53d6\u6d88": "\u7814\u7a76\u5df2\u53d6\u6d88",
    "\u7814\u7a76\u5df2\u53d6\u6d88\u3002\n\n": "\u7814\u7a76\u5df2\u53d6\u6d88\u3002\n\n",
    "\u4efb\u52a1\u5df2\u505c\u6b62\uff1b\u5f53\u524d\u6b65\u9aa4\u4e4b\u524d\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u5df2\u7ecf\u5b89\u5168\u4fdd\u5b58\u3002": "\u4efb\u52d9\u5df2\u505c\u6b62\uff1b\u5f53\u524d\u6b65\u9aa4\u4e4b\u524d\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u5df2\u7ecf\u5b89\u5168\u4fdd\u5b58\u3002",
    "\u53ef\u4ee5\u91cd\u65b0\u8fd0\u884c\u7814\u7a76\uff0c\u6216\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u67e5\u770b\u4e2d\u95f4\u7ed3\u679c\u3002": "\u53ef\u4ee5\u91cd\u65b0\u904b\u884c\u7814\u7a76\uff0c\u6216\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u67e5\u770b\u4e2d\u95f4\u7ed3\u679c\u3002",
    "\u6a21\u578b\u8fde\u63a5\u6d4b\u8bd5": "\u6a21\u578b\u9023\u63a5\u6e2c\u8a66",
    "\u5df2\u5408\u5e76 {count} \u4e2a\u5728\u7ebf\u6a21\u578b\uff1b\u5185\u7f6e\u63a8\u8350\u9879\u4fdd\u6301\u7f6e\u9876\u3002": "\u5df2\u5408\u4e26 {count} \u500b\u5728\u7dda\u6a21\u578b\uff1b\u5185\u7f6e\u63a8\u85a6\u9879\u4fdd\u6301\u7f6e\u9876\u3002",
    " \u82e5\u5c1a\u672a\u5b89\u88c5\u6216\u542f\u52a8 Ollama\uff0c\u8bf7\u70b9\u51fb\u5e2e\u52a9\u3002": " \u82e5\u5c1a\u672a\u5b89\u88dd\u6216\u555f\u52d5 Ollama\uff0c\u8acb\u9ede\u51fb\u5e6b\u52a9\u3002",
    "\u7814\u7a76\u4efb\u52a1\u5931\u8d25": "\u7814\u7a76\u4efb\u52d9\u5931\u6557",
    "\u540e\u53f0\u4efb\u52a1\u5931\u8d25": "\u5f8c\u53f0\u4efb\u52d9\u5931\u6557",
    "OpenThesis \u540e\u53f0\u4efb\u52a1\u5931\u8d25": "OpenThesis \u5f8c\u53f0\u4efb\u52d9\u5931\u6557",
    "SEC \u6570\u636e\u83b7\u53d6\u5931\u8d25": "SEC \u6578\u64da\u83b7\u53d6\u5931\u6557",
    "\u8bf7\u68c0\u67e5\u7f51\u7edc\u8fde\u63a5\u548c SEC \u8054\u7cfb\u90ae\u7bb1\u540e\u91cd\u8bd5\uff1b\u4e5f\u53ef\u4ee5\u5148\u4f7f\u7528\u201c\u5408\u6210\u6f14\u793a\u516c\u53f8\u201d\u9a8c\u8bc1\u5b8c\u6574\u6d41\u7a0b\u3002": "\u8acb\u6aa2\u67e5\u7db2\u7d61\u9023\u63a5\u548c SEC \u806f\u4fc2\u90f5\u7bb1\u5f8c\u91cd\u8a66\uff1b\u4e5f\u53ef\u4ee5\u5148\u4f7f\u7528\u201c\u5408\u6210\u6f14\u793a\u516c\u53f8\u201d\u9a57\u8b49\u5b8c\u6574\u6d41\u7a0b\u3002",
    "\u6a21\u578b\u8ba4\u8bc1\u5931\u8d25": "\u6a21\u578b\u8a8d\u8b49\u5931\u6557",
    "\u8bf7\u68c0\u67e5 API Key\u3001\u63d0\u4f9b\u65b9\u8d26\u53f7\u6743\u9650\u4ee5\u53ca\u63a5\u53e3\u5730\u5740\u3002Key \u4e0d\u4f1a\u51fa\u73b0\u5728\u8bca\u65ad\u4fe1\u606f\u4e2d\u3002": "\u8acb\u6aa2\u67e5 API Key\u3001\u63d0\u4f9b\u65b9\u5e33\u865f\u6b0a\u9650\u4ee5\u53ca\u63a5\u53e3\u5730\u5740\u3002Key \u4e0d\u6703\u51fa\u73fe\u5728\u8bca\u65ad\u4fe1\u606f\u4e2d\u3002",
    "\u6a21\u578b\u8bf7\u6c42\u53d7\u5230\u9650\u6d41": "\u6a21\u578b\u8acb\u6c42\u53d7\u5230\u9650\u6d41",
    "\u670d\u52a1\u5546\u6682\u65f6\u9650\u5236\u4e86\u8bf7\u6c42\u9891\u7387\u3002\u8bf7\u7a0d\u540e\u91cd\u65b0\u8fd0\u884c\uff0c\u6216\u68c0\u67e5\u8d26\u53f7\u989d\u5ea6\u3002": "\u670d\u52d9\u5546\u66ab\u6642\u9650\u5236\u4e86\u8acb\u6c42\u9891\u7387\u3002\u8acb\u7a0d\u5f8c\u91cd\u65b0\u904b\u884c\uff0c\u6216\u6aa2\u67e5\u5e33\u865f\u989d\u5ea6\u3002",
    "\u6a21\u578b\u6216\u6570\u636e\u8bf7\u6c42\u8d85\u65f6": "\u6a21\u578b\u6216\u6578\u64da\u8acb\u6c42\u8d85\u6642",
    "\u7f51\u7edc\u6216\u6a21\u578b\u54cd\u5e94\u65f6\u95f4\u8d85\u8fc7\u9650\u5236\u3002\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u3002": "\u7db2\u7d61\u6216\u6a21\u578b\u54cd\u5e94\u6642\u95f4\u8d85\u8fc7\u9650\u5236\u3002\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u3002",
    "\u6a21\u578b\u6216\u63a5\u53e3\u4e0d\u5b58\u5728": "\u6a21\u578b\u6216\u63a5\u53e3\u4e0d\u5b58\u5728",
    "\u8bf7\u6838\u5bf9\u6a21\u578b ID \u4e0e\u63a5\u53e3\u5730\u5740\uff0c\u5fc5\u8981\u65f6\u5728\u6a21\u578b\u8bbe\u7f6e\u4e2d\u5237\u65b0\u5728\u7ebf\u76ee\u5f55\u3002": "\u8acb\u6838\u5bf9\u6a21\u578b ID \u4e0e\u63a5\u53e3\u5730\u5740\uff0c\u5fc5\u8981\u6642\u5728\u6a21\u578b\u8a2d\u7f6e\u4e2d\u5237\u65b0\u5728\u7dda\u76ee\u9304\u3002",
    "\u7f51\u7edc\u8fde\u63a5\u5931\u8d25": "\u7db2\u7d61\u9023\u63a5\u5931\u6557",
    "\u65e0\u6cd5\u8fde\u63a5\u6a21\u578b\u6216\u6570\u636e\u670d\u52a1\u3002\u8bf7\u68c0\u67e5\u7f51\u7edc\u3001\u4ee3\u7406\u3001\u63a5\u53e3\u5730\u5740\u548c\u672c\u5730\u670d\u52a1\u72b6\u6001\u3002": "\u7121\u6cd5\u9023\u63a5\u6a21\u578b\u6216\u6578\u64da\u670d\u52d9\u3002\u8acb\u6aa2\u67e5\u7db2\u7d61\u3001\u4ee3\u7406\u3001\u63a5\u53e3\u5730\u5740\u548c\u672c\u5730\u670d\u52d9\u72b6\u6001\u3002",
    "\u4efb\u52a1\u672a\u80fd\u5b8c\u6210\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u3002\u53ef\u68c0\u67e5\u8bbe\u7f6e\u540e\u91cd\u65b0\u8fd0\u884c\u3002": "\u4efb\u52d9\u672a\u80fd\u5b8c\u6210\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u3002\u53ef\u6aa2\u67e5\u8a2d\u7f6e\u5f8c\u91cd\u65b0\u904b\u884c\u3002",
}

UI_HANT: Final[dict[str, str]] = UI_HANT_EXPLICIT
UI_HANT.update({
    "\u8bbe\u7f6e": "\u8a2d\u5b9a",
    "\u7b80\u4f53\u4e2d\u6587": "\u7c21\u9ad4\u4e2d\u6587",
    "\u754c\u9762\u8bed\u8a00": "\u4ecb\u9762\u8a9e\u8a00",
    "\u7814\u7a76\u62a5\u544a\u8bed\u8a00": "\u7814\u7a76\u5831\u544a\u8a9e\u8a00",
    "\u754c\u9762\u8bed\u8a00\u6a21\u5f0f": "\u4ecb\u9762\u8a9e\u8a00\u6a21\u5f0f",
    "\u8ddf\u968f\u7cfb\u7edf": "\u8ddf\u96a8\u7cfb\u7d71",
    "\u624b\u52a8\u9009\u62e9": "\u624b\u52d5\u9078\u64c7",
})

# Explicit report vocabulary shared by Markdown, HTML and deterministic views.
UI_HANT.update({
    "v0.1 \u6a21\u5757\u4ec5\u5141\u8bb8 Markdown\u3001JSON \u517c\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u672c\uff1b\u4e0d\u5141\u8bb8\u8fd0\u884c\u4ee3\u7801\u3001\u8bbf\u95ee\u6587\u4ef6\u7cfb\u7edf\u3001\u7f51\u7edc\u6216\u5bc6\u94a5\u3002": "v0.1 \u6a21\u7d44\u50c5\u5141\u8a31 Markdown\u3001JSON \u76f8\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u5b57\uff1b\u4e0d\u5141\u8a31\u57f7\u884c\u7a0b\u5f0f\u78bc\u3001\u5b58\u53d6\u6a94\u6848\u7cfb\u7d71\u3001\u7db2\u8def\u6216\u91d1\u9470\u3002",
})
UI_HANT.update({
    "执行摘要": "執行摘要", "主要结论": "主要結論", "商业模式": "商業模式",
    "财务质量": "財務品質", "资产负债表": "資產負債表", "竞争地位": "競爭地位",
    "增长机会": "增長機會", "反方观点": "反方觀點", "长期经营情景": "長期經營情境",
    "当前估值隐含预期": "目前估值隱含預期", "投资逻辑": "投資邏輯", "逻辑失效条件": "邏輯失效條件",
    "领先指标": "領先指標", "未解决问题": "未解決問題", "确定性财务概览": "確定性財務概覽",
    "反向 DCF 隐含预期": "反向 DCF 隱含預期", "经过验证的研究档案": "經過驗證的研究檔案",
    "反方审查": "反方審查", "双模型研究分歧": "雙模型研究分歧", "投资逻辑快照": "投資邏輯快照",
    "证据不足": "證據不足", "暂无": "暫無", "置信度未提供": "置信度未提供",
    "高置信度": "高置信度", "中等置信度": "中等置信度", "低置信度": "低置信度", "条": "條",
    "时间跨度": "時間跨度", "适用情景": "適用情境", "增长机制": "增長機制", "支持证据": "支援證據",
    "相反证据": "相反證據", "失效条件": "失效條件", "营业收入": "營業收入", "同期收入增长": "同期收入增長",
    "净利润": "淨利潤", "经营现金流": "經營現金流", "年度数据连续性": "年度資料連續性",
    "当前市值输入": "目前市值輸入", "最新自由现金流": "最新自由現金流", "折现率": "折現率",
    "永续增长率": "永續增長率", "最新季度及中期数据": "最新季度及中期資料",
})


UI_HANT.update({
    "\u6a21\u578b\u4e0e SEC \u8bbe\u7f6e": "\u6a21\u578b\u8207 SEC \u8a2d\u7f6e",
    "\u9009\u62e9\u516c\u53f8\u5e76\u786e\u8ba4\u914d\u7f6e\u540e\uff0c\u4efb\u52a1\u9636\u6bb5\u3001\u7b49\u5f85\u65f6\u95f4\u548c\u9519\u8bef\u4f1a\u663e\u793a\u5728\u8fd9\u91cc\u3002": "\u9078\u64c7\u516c\u53f8\u4e26\u78ba\u8a8d\u914d\u7f6e\u5f8c\uff0c\u4efb\u52d9\u968e\u6bb5\u3001\u7b49\u5f85\u6642\u9593\u548c\u932f\u8bef\u6703\u986f\u793a\u5728\u9019\u91cc\u3002",
    "\u5f53\u524d\u5e02\u503c\uff08\u5341\u4ebf\u7f8e\u5143\uff09": "\u7576\u524d\u5e02\u503c\uff08\u5341\u4ebf\u7f8e\u5143\uff09",
    "\u5bfc\u51fa\u5f53\u524d\u62a5\u544a": "\u5c0e\u51fa\u7576\u524d\u5831\u544a",
    "\u6b22\u8fce\u4f7f\u7528 OpenThesis\u3002\n\n\u7b2c\u4e00\u6b65\uff1a\u641c\u7d22\u6216\u5feb\u6377\u9009\u62e9\u516c\u53f8\uff1b\u7b2c\u4e8c\u6b65\uff1a\u786e\u8ba4\u7814\u7a76\u6a21\u5757\u548c\u6a21\u578b\u8bbe\u7f6e\uff1b\u7b2c\u4e09\u6b65\uff1a\u70b9\u51fb\u9875\u9762\u9876\u90e8\u59cb\u7ec8\u53ef\u89c1\u7684\u201c\u5f00\u59cb\u7814\u7a76\u201d\u3002\n\n\u53ef\u4ee5\u9009\u62e9\u201c\u5408\u6210\u6f14\u793a\u516c\u53f8\u201d\u79bb\u7ebf\u9a8c\u8bc1\u5b8c\u6574\u6d41\u7a0b\u3002\u7814\u7a76\u771f\u5b9e\u516c\u53f8\u65f6\uff0c\u8bf7\u5728\u201c\u6a21\u578b\u4e0e SEC \u8bbe\u7f6e\u201d\u4e2d\u586b\u5199\u4f60\u81ea\u5df1\u7684 SEC \u8054\u7cfb\u90ae\u7bb1\u3002": "\u6b22\u8fce\u4f7f\u7528 OpenThesis\u3002\n\n\u7b2c\u4e00\u6b65\uff1a\u641c\u7d22\u6216\u5feb\u6377\u9078\u64c7\u516c\u53f8\uff1b\u7b2c\u4e8c\u6b65\uff1a\u78ba\u8a8d\u7814\u7a76\u6a21\u5757\u548c\u6a21\u578b\u8a2d\u7f6e\uff1b\u7b2c\u4e09\u6b65\uff1a\u9ede\u51fb\u9801\u9762\u9876\u90e8\u59cb\u7d42\u53ef\u89c1\u7684\u201c\u958b\u59cb\u7814\u7a76\u201d\u3002\n\n\u53ef\u4ee5\u9078\u64c7\u201c\u5408\u6210\u6f14\u793a\u516c\u53f8\u201d\u96e2\u7dda\u9a57\u8b49\u5b8c\u6574\u6d41\u7a0b\u3002\u7814\u7a76\u771f\u5be6\u516c\u53f8\u6642\uff0c\u8acb\u5728\u201c\u6a21\u578b\u8207 SEC \u8a2d\u7f6e\u201d\u4e2d\u586b\u5199\u4f60\u81ea\u5df1\u7684 SEC \u806f\u4fc2\u90f5\u7bb1\u3002",
    "\u5f00\u59cb\u65f6\u95f4": "\u958b\u59cb\u6642\u9593",
    "\u6a21\u578b\u4e0e\u6570\u636e\u6e90\u8bbe\u7f6e": "\u6a21\u578b\u8207\u6578\u64da\u6e90\u8a2d\u7f6e",
    "\u9996\u6b21\u542f\u52a8\u4e0d\u4f1a\u8c03\u7528 AI\uff1b\u53ea\u6709\u4e3b\u52a8\u9009\u62e9\u6a21\u578b\u5e76\u5f00\u59cb\u7814\u7a76\u65f6\u624d\u4f1a\u53d1\u9001\u7814\u7a76\u4e0a\u4e0b\u6587\u3002API Key \u53ea\u4fdd\u5b58\u5728\u5185\u5b58\u4e2d\uff0c\u4e0d\u5199\u5165\u6570\u636e\u5e93\u6216\u65e5\u5fd7\u3002": "\u9996\u6b21\u555f\u52d5\u4e0d\u6703\u8abf\u7528 AI\uff1b\u53ea\u6709\u4e3b\u52d5\u9078\u64c7\u6a21\u578b\u4e26\u958b\u59cb\u7814\u7a76\u6642\u624d\u6703\u767c\u9001\u7814\u7a76\u4e0a\u4e0b\u6587\u3002API Key \u53ea\u4fdd\u5b58\u5728\u5167\u5b58\u4e2d\uff0c\u4e0d\u5199\u5165\u6578\u64da\u5e93\u6216\u65e5\u5fd7\u3002",
    "SEC EDGAR \u8d22\u62a5\u8bbf\u95ee": "SEC EDGAR \u8ca1\u5831\u8bbf\u554f",
    "\u8bf7\u52ff\u586b\u5199\u76ee\u6807\u516c\u53f8\u7684\u6295\u8d44\u8005\u5173\u7cfb\u90ae\u7bb1\u3002\u8fd9\u91cc\u6807\u8bc6\u7684\u662f\u6570\u636e\u8bf7\u6c42\u8005\u3002\u90ae\u7bb1\u4fdd\u5b58\u5728\u672c\u673a\u8bbe\u7f6e\uff0c\u5e76\u968f SEC \u8bf7\u6c42\u53d1\u9001\u3002": "\u8acb\u52ff\u586b\u5199\u76ee\u6a19\u516c\u53f8\u7684\u6295\u8cc7\u8005\u95dc\u4fc2\u90f5\u7bb1\u3002\u9019\u91cc\u6a19\u8b58\u7684\u662f\u6578\u64da\u8acb\u6c42\u8005\u3002\u90f5\u7bb1\u4fdd\u5b58\u5728\u672c\u6a5f\u8a2d\u7f6e\uff0c\u4e26\u96a8 SEC \u8acb\u6c42\u767c\u9001\u3002",
    "\u25b6 \u53ef\u9009\uff1a\u7b2c\u4e8c\u4e2a\u5bf9\u6bd4\u6a21\u578b": "\u25b6 \u53ef\u9078\uff1a\u7b2c\u4e8c\u500b\u5c0d\u6bd4\u6a21\u578b",
    "\u25bc \u53ef\u9009\uff1a\u7b2c\u4e8c\u4e2a\u5bf9\u6bd4\u6a21\u578b": "\u25bc \u53ef\u9078\uff1a\u7b2c\u4e8c\u500b\u5c0d\u6bd4\u6a21\u578b",
    "\u7b2c\u4e8c\u4e2a\u5bf9\u6bd4\u6a21\u578b": "\u7b2c\u4e8c\u500b\u5c0d\u6bd4\u6a21\u578b",
    "v0.1 \u6a21\u5757\u4ec5\u5141\u8bb8 Markdown\u3001JSON \u517c\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u672c\uff1b\u4e0d\u5141\u8bb8\u8fd0\u884c\u4ee3\u7801\u3001\u8bbf\u95ee\u6587\u4ef6\u7cfb\u7edf\u3001\u7f51\u7edc\u6216\u5bc6\u94a5\u3002": "v0.1 \u6a21\u5757\u50c5\u5141\u8bb8 Markdown\u3001JSON \u517c\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u672c\uff1b\u4e0d\u5141\u8bb8\u904b\u884c\u4ee3\u78bc\u3001\u8bbf\u554f\u6587\u4ef6\u4fc2\u7d71\u3001\u7db2\u7d61\u6216\u5bc6\u9470\u3002",
    "\u65f6\u95f4": "\u6642\u9593",
    "\u53e6\u5b58\u4e3a\u65b0\u7248\u672c": "\u53e6\u5b58\u70ba\u65b0\u7248\u672c",
    "\u754c\u9762\u4e0e\u62a5\u544a\u8bed\u8a00": "\u754c\u9762\u8207\u5831\u544a\u8a9e\u8a00",
    "\u754c\u9762\u8bed\u8a00\u5c06\u5728\u4e0b\u6b21\u542f\u52a8\u65f6\u751f\u6548\u3002": "\u754c\u9762\u8a9e\u8a00\u5c07\u5728\u4e0b\u6b21\u555f\u52d5\u6642\u751f\u6548\u3002",
    "\u62a5\u544a\u8bed\u8a00\u7acb\u5373\u7528\u4e8e\u4e0b\u4e00\u6b21\u7814\u7a76\uff1b\u5386\u53f2\u62a5\u544a\u53ea\u7ffb\u8bd1\u7a0b\u5e8f\u751f\u6210\u7684\u6807\u9898\uff0cAI \u6b63\u6587\u4fdd\u6301\u539f\u6587\u3002": "\u5831\u544a\u8a9e\u8a00\u7acb\u5373\u7528\u65bc\u4e0b\u4e00\u6b21\u7814\u7a76\uff1b\u6b77\u53f2\u5831\u544a\u53ea\u7ffb\u8bd1\u7a0b\u5e8f\u751f\u6210\u7684\u6a19\u984c\uff0cAI \u6b63\u6587\u4fdd\u6301\u539f\u6587\u3002",
    "\u754c\u9762\u8bed\u8a00\u5c06\u5728\u91cd\u542f OpenThesis \u540e\u751f\u6548\u3002": "\u754c\u9762\u8a9e\u8a00\u5c07\u5728\u91cd\u555f OpenThesis \u5f8c\u751f\u6548\u3002",
    "\u62a5\u544a\u8bed\u8a00\u5df2\u5e94\u7528\u4e8e\u4e0b\u4e00\u6b21\u7814\u7a76\u3002": "\u5831\u544a\u8a9e\u8a00\u5df2\u61c9\u7528\u65bc\u4e0b\u4e00\u6b21\u7814\u7a76\u3002",
    "\u539f\u5219\uff1a\u6bcf\u4e2a\u4e8b\u5b9e\u90fd\u9700\u8981\u8bc1\u636e\uff1b\u8d22\u52a1\u8ba1\u7b97\u7531\u786e\u5b9a\u6027\u7a0b\u5e8f\u5b8c\u6210\uff1b\u9884\u6d4b\u4f7f\u7528\u60c5\u666f\u3001\u533a\u95f4\u548c\u5931\u6548\u6761\u4ef6\uff1bAI \u4e0d\u6267\u884c\u4efb\u4f55\u4ea4\u6613\u3002": "\u539f\u5219\uff1a\u6bcf\u500b\u4e8b\u5be6\u90fd\u9700\u8981\u8b49\u64da\uff1b\u8ca1\u52d9\u8a08\u7b97\u7531\u78ba\u5b9a\u6027\u7a0b\u5e8f\u5b8c\u6210\uff1b\u9810\u6e2c\u4f7f\u7528\u60c5\u666f\u3001\u533a\u9593\u548c\u5931\u6548\u6761\u4ef6\uff1bAI \u4e0d\u57f7\u884c\u4efb\u4f55\u4ea4\u6613\u3002",
    "\u8bf7\u9009\u62e9\u4e00\u4e2a\u5185\u7f6e\u7684\u5e38\u7528\u516c\u53f8\u3002": "\u8acb\u9078\u64c7\u4e00\u500b\u5167\u7f6e\u7684\u5e38\u7528\u516c\u53f8\u3002",
    "\u5f53\u524d\u4e0d\u4f1a\u8c03\u7528 AI\u3002": "\u7576\u524d\u4e0d\u6703\u8abf\u7528 AI\u3002",
    "\u6b64\u63d0\u4f9b\u65b9\u4f7f\u7528\u5185\u7f6e\u6a21\u578b\u5217\u8868\uff0c\u4e5f\u53ef\u624b\u52a8\u586b\u5199\u6a21\u578b ID\u3002": "\u6b64\u63d0\u4f9b\u65b9\u4f7f\u7528\u5167\u7f6e\u6a21\u578b\u5217\u8868\uff0c\u4e5f\u53ef\u624b\u52d5\u586b\u5199\u6a21\u578b ID\u3002",
    "\u5df2\u52a0\u8f7d\u5185\u7f6e\u63a8\u8350\u6a21\u578b\uff1b\u53ef\u624b\u52a8\u5237\u65b0\u5728\u7ebf\u76ee\u5f55\u3002": "\u5df2\u52a0\u8f09\u5167\u7f6e\u63a8\u85a6\u6a21\u578b\uff1b\u53ef\u624b\u52d5\u5237\u65b0\u5728\u7dda\u76ee\u9304\u3002",
    "\u5f53\u524d\u672a\u542f\u7528 AI\uff0c\u65e0\u9700\u5237\u65b0\u3002": "\u7576\u524d\u672a\u555f\u7528 AI\uff0c\u7121\u9700\u5237\u65b0\u3002",
    "\u5728\u7ebf\u6a21\u578b\u76ee\u5f55\u5237\u65b0\u5931\u8d25\uff0c\u5df2\u4fdd\u7559\u5185\u7f6e\u5217\u8868\u3002": "\u5728\u7dda\u6a21\u578b\u76ee\u9304\u5237\u65b0\u5931\u6557\uff0c\u5df2\u4fdd\u7559\u5167\u7f6e\u5217\u8868\u3002",
    "{error}\n\n\u8bf7\u5728\u201c\u6a21\u578b\u4e0e\u6570\u636e\u6e90\u8bbe\u7f6e\u201d\u4e2d\u586b\u5199\u540e\u4fdd\u5b58\u3002": "{error}\n\n\u8acb\u5728\u201c\u6a21\u578b\u8207\u6578\u64da\u6e90\u8a2d\u7f6e\u201d\u4e2d\u586b\u5199\u5f8c\u4fdd\u5b58\u3002",
    "\u5df2\u9009\u62e9\u5408\u6210\u6f14\u793a\u516c\u53f8\u3002\u6240\u6709\u6570\u636e\u5747\u4e3a\u865a\u6784\uff0c\u53ea\u7528\u4e8e\u9a8c\u8bc1\u8f6f\u4ef6\u529f\u80fd\u3002": "\u5df2\u9078\u64c7\u5408\u6210\u6f14\u793a\u516c\u53f8\u3002\u6240\u6709\u6578\u64da\u5747\u70ba\u865a\u69cb\uff0c\u53ea\u7528\u65bc\u9a57\u8b49\u8f6f\u4ef6\u529f\u80fd\u3002",
    "\u4e0a\u6b21\u5173\u95ed\u5e94\u7528\u65f6\u4ecd\u6709\u7814\u7a76\u5728\u8fd0\u884c\uff0c\u73b0\u5df2\u5b89\u5168\u6807\u8bb0\u4e3a\u201c\u5df2\u53d6\u6d88\u201d\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u4ea7\u7269\u4ecd\u53ef\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u67e5\u770b\u3002": "\u4e0a\u6b21\u95dc\u9589\u61c9\u7528\u6642\u4ecd\u6709\u7814\u7a76\u5728\u904b\u884c\uff0c\u73fe\u5df2\u5b89\u5168\u6a19\u8a18\u70ba\u201c\u5df2\u53d6\u6d88\u201d\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u9593\u7522\u7269\u4ecd\u53ef\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u67e5\u770b\u3002",
    "\u4efb\u52a1\u6b63\u5728\u540e\u53f0\u8fd0\u884c\uff1b\u7a97\u53e3\u4fdd\u6301\u54cd\u5e94\uff0c\u53ef\u4ee5\u968f\u65f6\u67e5\u770b\u5f53\u524d\u9636\u6bb5\u3002": "\u4efb\u52d9\u6b63\u5728\u5f8c\u53f0\u904b\u884c\uff1b\u7a97\u53e3\u4fdd\u6301\u97ff\u61c9\uff0c\u53ef\u4ee5\u96a8\u6642\u67e5\u770b\u7576\u524d\u968e\u6bb5\u3002",
    "\u5b8c\u6210\u540e\u5c06\u5728\u8fd9\u91cc\u663e\u793a\u5b8c\u6574\u62a5\u544a\u3002": "\u5b8c\u6210\u5f8c\u5c07\u5728\u9019\u91cc\u986f\u793a\u5b8c\u6574\u5831\u544a\u3002",
    "\u53d6\u6d88\u8bf7\u6c42\u5df2\u6536\u5230\uff1b\u6b63\u5728\u7b49\u5f85\u5f53\u524d\u7f51\u7edc\u8bf7\u6c42\u5b89\u5168\u7ed3\u675f\uff0c\u4e0d\u4f1a\u518d\u542f\u52a8\u65b0\u7684\u7814\u7a76\u6b65\u9aa4\u3002": "\u53d6\u6d88\u8acb\u6c42\u5df2\u6536\u5230\uff1b\u6b63\u5728\u7b49\u5f85\u7576\u524d\u7db2\u7d61\u8acb\u6c42\u5b89\u5168\u7d50\u675f\uff0c\u4e0d\u6703\u518d\u555f\u52d5\u65b0\u7684\u7814\u7a76\u6b65\u9aa4\u3002",
    "\u540e\u53f0\u4ecd\u5728\u5de5\u4f5c \u00b7 \u5f53\u524d\u6b65\u9aa4\u5df2\u7b49\u5f85 {elapsed} \u00b7 \u6a21\u578b\u7814\u7a76\u901a\u5e38\u9700\u8981\u6570\u5206\u949f\uff0c\u8bf7\u52ff\u5173\u95ed\u5e94\u7528\u3002": "\u5f8c\u53f0\u4ecd\u5728\u5de5\u4f5c \u00b7 \u7576\u524d\u6b65\u9aa4\u5df2\u7b49\u5f85 {elapsed} \u00b7 \u6a21\u578b\u7814\u7a76\u901a\u5e38\u9700\u8981\u6578\u5206\u949f\uff0c\u8acb\u52ff\u95dc\u9589\u61c9\u7528\u3002",
    "\u5df2\u505c\u6b62\u542f\u52a8\u65b0\u6b65\u9aa4\uff1b\u5f53\u524d\u7f51\u7edc\u8bf7\u6c42\u7ed3\u675f\u540e\u4f1a\u5b89\u5168\u4fdd\u5b58\u4e2d\u95f4\u7ed3\u679c\u3002": "\u5df2\u505c\u6b62\u555f\u52d5\u65b0\u6b65\u9aa4\uff1b\u7576\u524d\u7db2\u7d61\u8acb\u6c42\u7d50\u675f\u5f8c\u6703\u5b89\u5168\u4fdd\u5b58\u4e2d\u9593\u7d50\u679c\u3002",
    "{error}\n\n\u771f\u5b9e\u516c\u53f8\u7814\u7a76\u9700\u8981\u8bbf\u95ee SEC\uff0c\u8bf7\u5148\u5b8c\u6210 SEC \u8bbe\u7f6e\u3002": "{error}\n\n\u771f\u5be6\u516c\u53f8\u7814\u7a76\u9700\u8981\u8bbf\u554f SEC\uff0c\u8acb\u5148\u5b8c\u6210 SEC \u8a2d\u7f6e\u3002",
    "\u53cd\u5411 DCF \u8f93\u5165\u5fc5\u987b\u662f\u6570\u5b57": "\u53cd\u5411 DCF \u8f93\u5165\u5fc5\u9808\u662f\u6578\u5b57",
    "\u5e02\u503c\u5fc5\u987b\u4e3a\u6b63\u6570\uff0c\u4e14\u6298\u73b0\u7387\u5fc5\u987b\u9ad8\u4e8e\u6c38\u7eed\u589e\u957f\u7387": "\u5e02\u503c\u5fc5\u9808\u70ba\u6b63\u6578\uff0c\u4e14\u6298\u73fe\u7387\u5fc5\u9808\u9ad8\u65bc\u6c38\u7e8c\u589e\u9577\u7387",
    "\u542f\u7528\u6a21\u578b\u6bd4\u8f83\u65f6\uff0c\u4e3b\u6a21\u578b\u548c\u7b2c\u4e8c\u6a21\u578b\u90fd\u5fc5\u987b\u914d\u7f6e\u63d0\u4f9b\u65b9\u3001\u6a21\u578b\u540d\u79f0\u548c\u63a5\u53e3\u5730\u5740\u3002": "\u555f\u7528\u6a21\u578b\u6bd4\u8f83\u6642\uff0c\u4e3b\u6a21\u578b\u548c\u7b2c\u4e8c\u6a21\u578b\u90fd\u5fc5\u9808\u914d\u7f6e\u63d0\u4f9b\u65b9\u3001\u6a21\u578b\u540d\u7a31\u548c\u63a5\u53e3\u5730\u5740\u3002",
    "\u6b63\u5728\u89e3\u6790\u8d22\u62a5\u8bc1\u636e\u4e0e\u8868\u683c": "\u6b63\u5728\u89e3\u6790\u8ca1\u5831\u8b49\u64da\u8207\u8868\u683c",
    "\u4e3b\u6a21\u578b\u7814\u7a76\u5b8c\u6210\uff0c\u6b63\u5728\u542f\u52a8\u5bf9\u6bd4\u6a21\u578b": "\u4e3b\u6a21\u578b\u7814\u7a76\u5b8c\u6210\uff0c\u6b63\u5728\u555f\u52d5\u5c0d\u6bd4\u6a21\u578b",
    "\u5bf9\u6bd4\u6a21\u578b\uff1a": "\u5c0d\u6bd4\u6a21\u578b\uff1a",
    "\u57fa\u7840\u8d22\u52a1\u5206\u6790\u5b8c\u6210\uff1b\u914d\u7f6e\u6a21\u578b\u540e\u53ef\u8fd0\u884c\u5b8c\u6574\u7814\u7a76": "\u57fa\u790e\u8ca1\u52d9\u5206\u6790\u5b8c\u6210\uff1b\u914d\u7f6e\u6a21\u578b\u5f8c\u53ef\u904b\u884c\u5b8c\u6574\u7814\u7a76",
    "\u6b63\u5728\u5e76\u884c\u8fd0\u884c\u8d22\u52a1\u3001\u5546\u4e1a\u4e0e\u4f1a\u8ba1\u98ce\u9669 Agent\uff080/3\uff09": "\u6b63\u5728\u4e26\u884c\u904b\u884c\u8ca1\u52d9\u3001\u5546\u696d\u8207\u6703\u8a08\u98ce\u9669 Agent\uff080/3\uff09",
    "\u57fa\u7840\u5206\u6790 Agent \u5df2\u5b8c\u6210 {completed}/3\uff1a{agent_id}": "\u57fa\u790e\u5206\u6790 Agent \u5df2\u5b8c\u6210 {completed}/3\uff1a{agent_id}",
    "\u57fa\u7840\u7814\u7a76\u6863\u6848\u5b8c\u6210": "\u57fa\u790e\u7814\u7a76\u6863\u6848\u5b8c\u6210",
    "\u6b63\u5728\u7814\u7a76\u516c\u53f8\u4e0e\u884c\u4e1a\u589e\u957f\u673a\u4f1a": "\u6b63\u5728\u7814\u7a76\u516c\u53f8\u8207\u884c\u696d\u589e\u9577\u6a5f\u6703",
    "\u6b63\u5728\u8fdb\u884c\u53cd\u65b9\u5ba1\u67e5\u4e0e\u538b\u529b\u6d4b\u8bd5": "\u6b63\u5728\u9032\u884c\u53cd\u65b9\u5be9\u67e5\u8207\u538b\u529b\u6e2c\u8a66",
    "\u5f53\u524d\u9009\u62e9 none\uff0c\u4e0d\u4f1a\u8c03\u7528\u8bed\u8a00\u6a21\u578b\u3002": "\u7576\u524d\u9078\u64c7 none\uff0c\u4e0d\u6703\u8abf\u7528\u8a9e\u8a00\u6a21\u578b\u3002",
    "\u5df2\u4fdd\u5b58\u4e3a v{version}": "\u5df2\u4fdd\u5b58\u70ba v{version}",
    "\u5f53\u524d\u6ca1\u6709\u53ef\u5bfc\u51fa\u7684\u5185\u5bb9\u3002": "\u7576\u524d\u6ca1\u6709\u53ef\u5c0e\u51fa\u7684\u5167\u5bb9\u3002",
    "\u5168\u90e8\u7814\u7a76\u9636\u6bb5\u5df2\u5b8c\u6210\uff0c\u5b8c\u6574\u62a5\u544a\u548c\u4e2d\u95f4\u4ea7\u7269\u5df2\u7ecf\u4fdd\u5b58\u3002": "\u5168\u90e8\u7814\u7a76\u968e\u6bb5\u5df2\u5b8c\u6210\uff0c\u5b8c\u6574\u5831\u544a\u548c\u4e2d\u9593\u7522\u7269\u5df2\u7ecf\u4fdd\u5b58\u3002",
    "\u4efb\u52a1\u5df2\u505c\u6b62\uff1b\u5f53\u524d\u6b65\u9aa4\u4e4b\u524d\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u5df2\u7ecf\u5b89\u5168\u4fdd\u5b58\u3002": "\u4efb\u52d9\u5df2\u505c\u6b62\uff1b\u7576\u524d\u6b65\u9aa4\u4e4b\u524d\u5b8c\u6210\u7684\u4e2d\u9593\u7d50\u679c\u5df2\u7ecf\u5b89\u5168\u4fdd\u5b58\u3002",
    "\u53ef\u4ee5\u91cd\u65b0\u8fd0\u884c\u7814\u7a76\uff0c\u6216\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u67e5\u770b\u4e2d\u95f4\u7ed3\u679c\u3002": "\u53ef\u4ee5\u91cd\u65b0\u904b\u884c\u7814\u7a76\uff0c\u6216\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u67e5\u770b\u4e2d\u9593\u7d50\u679c\u3002",
    "\u5df2\u5408\u5e76 {count} \u4e2a\u5728\u7ebf\u6a21\u578b\uff1b\u5185\u7f6e\u63a8\u8350\u9879\u4fdd\u6301\u7f6e\u9876\u3002": "\u5df2\u5408\u4e26 {count} \u500b\u5728\u7dda\u6a21\u578b\uff1b\u5167\u7f6e\u63a8\u85a6\u9879\u4fdd\u6301\u7f6e\u9876\u3002",
    "\u7f51\u7edc\u6216\u6a21\u578b\u54cd\u5e94\u65f6\u95f4\u8d85\u8fc7\u9650\u5236\u3002\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u3002": "\u7db2\u7d61\u6216\u6a21\u578b\u97ff\u61c9\u6642\u9593\u8d85\u8fc7\u9650\u5236\u3002\u5df2\u5b8c\u6210\u7684\u4e2d\u9593\u7d50\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u3002",
    "\u8bf7\u6838\u5bf9\u6a21\u578b ID \u4e0e\u63a5\u53e3\u5730\u5740\uff0c\u5fc5\u8981\u65f6\u5728\u6a21\u578b\u8bbe\u7f6e\u4e2d\u5237\u65b0\u5728\u7ebf\u76ee\u5f55\u3002": "\u8acb\u6838\u5c0d\u6a21\u578b ID \u8207\u63a5\u53e3\u5730\u5740\uff0c\u5fc5\u8981\u6642\u5728\u6a21\u578b\u8a2d\u7f6e\u4e2d\u5237\u65b0\u5728\u7dda\u76ee\u9304\u3002",
    "\u65e0\u6cd5\u8fde\u63a5\u6a21\u578b\u6216\u6570\u636e\u670d\u52a1\u3002\u8bf7\u68c0\u67e5\u7f51\u7edc\u3001\u4ee3\u7406\u3001\u63a5\u53e3\u5730\u5740\u548c\u672c\u5730\u670d\u52a1\u72b6\u6001\u3002": "\u7121\u6cd5\u9023\u63a5\u6a21\u578b\u6216\u6578\u64da\u670d\u52d9\u3002\u8acb\u6aa2\u67e5\u7db2\u7d61\u3001\u4ee3\u7406\u3001\u63a5\u53e3\u5730\u5740\u548c\u672c\u5730\u670d\u52d9\u72b6\u614b\u3002",
    "\u4efb\u52a1\u672a\u80fd\u5b8c\u6210\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u95f4\u7ed3\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u5386\u53f2\u4e2d\u3002\u53ef\u68c0\u67e5\u8bbe\u7f6e\u540e\u91cd\u65b0\u8fd0\u884c\u3002": "\u4efb\u52d9\u672a\u80fd\u5b8c\u6210\uff1b\u5df2\u5b8c\u6210\u7684\u4e2d\u9593\u7d50\u679c\u4ecd\u4fdd\u5b58\u5728\u7814\u7a76\u6b77\u53f2\u4e2d\u3002\u53ef\u6aa2\u67e5\u8a2d\u7f6e\u5f8c\u91cd\u65b0\u904b\u884c\u3002",
})

UI_HANT.update({
    "v0.1 \u6a21\u5757\u4ec5\u5141\u8bb8 Markdown\u3001JSON \u517c\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u672c\uff1b\u4e0d\u5141\u8bb8\u8fd0\u884c\u4ee3\u7801\u3001\u8bbf\u95ee\u6587\u4ef6\u7cfb\u7edf\u3001\u7f51\u7edc\u6216\u5bc6\u94a5\u3002": "v0.1 \u6a21\u7d44\u50c5\u5141\u8a31 Markdown\u3001JSON \u76f8\u5bb9 YAML\u3001JSON Schema \u548c\u6587\u5b57\uff1b\u4e0d\u5141\u8a31\u57f7\u884c\u7a0b\u5f0f\u78bc\u3001\u5b58\u53d6\u6a94\u6848\u7cfb\u7d71\u3001\u7db2\u8def\u6216\u91d1\u9470\u3002",
})

MODEL_PRESET_LABELS: Final[dict[str, dict[str, str]]] = {
    ZH_CN: {
        "none": "不调用 AI（本地确定性分析）",
        "deepseek": "国内 · DeepSeek",
        "qwen": "国内 · Qwen（通义千问）",
        "kimi": "国内 · Kimi（中国大陆）",
        "kimi-global": "国外 · Kimi International",
        "glm": "国内 · GLM（智谱）",
        "openai": "国外 · OpenAI",
        "gemini": "国外 · Gemini",
        "openrouter": "国外 · OpenRouter",
        "ollama": "本地 · Ollama",
        "custom": "自定义 · OpenAI-compatible",
    },
    EN: {
        "none": "No AI (Local Deterministic Analysis)",
        "deepseek": "China · DeepSeek",
        "qwen": "China · Qwen",
        "kimi": "China · Kimi",
        "kimi-global": "International · Kimi",
        "glm": "China · GLM",
        "openai": "International · OpenAI",
        "gemini": "International · Gemini",
        "openrouter": "International · OpenRouter",
        "ollama": "Local · Ollama",
        "custom": "Custom · OpenAI-compatible",
    },
    ZH_HANT: {
        "none": "不呼叫 AI（本地確定性分析）",
        "deepseek": "中國 · DeepSeek", "qwen": "中國 · Qwen（通義千問）",
        "kimi": "中國 · Kimi", "kimi-global": "國際 · Kimi International",
        "glm": "中國 · GLM（智譜）", "openai": "國際 · OpenAI",
        "gemini": "國際 · Gemini", "openrouter": "國際 · OpenRouter",
        "ollama": "本地 · Ollama", "custom": "自訂 · OpenAI-compatible",
    },
}


SEC_PROFILE_IDS: Final = ("personal", "independent", "organization")
SEC_PROFILE_LABELS: Final[dict[str, dict[str, str]]] = {
    ZH_CN: {
        "personal": "个人投资者（推荐）",
        "independent": "独立研究者",
        "organization": "公司或研究团队",
    },
    EN: {
        "personal": "Personal Investor (Recommended)",
        "independent": "Independent Researcher",
        "organization": "Organization or Research Team",
    },
    ZH_HANT: {
        "personal": "個人投資者（推薦）",
        "independent": "獨立研究者",
        "organization": "公司或研究團隊",
    },
}


def translate(source: str, language: str, **params: object) -> str:
    locale = normalize_language(language)
    template = UI_EN.get(source, source) if locale == EN else UI_HANT.get(source, source) if locale == ZH_HANT else source
    return template.format(**params) if params else template


def model_preset_label(preset_id: str, language: str) -> str:
    locale = normalize_language(language)
    return MODEL_PRESET_LABELS[locale].get(preset_id, preset_id)


def model_preset_id_from_label(label: str) -> str | None:
    for labels in MODEL_PRESET_LABELS.values():
        for preset_id, candidate in labels.items():
            if label == candidate:
                return preset_id
    return None


def sec_profile_label(profile_id: str, language: str) -> str:
    locale = normalize_language(language)
    return SEC_PROFILE_LABELS[locale].get(profile_id, profile_id)


def sec_profile_id_from_label(label: str) -> str:
    if label in SEC_PROFILE_IDS:
        return label
    for labels in SEC_PROFILE_LABELS.values():
        for profile_id, candidate in labels.items():
            if label == candidate:
                return profile_id
    return "personal"


def placeholder_names(template: str) -> set[str]:
    return {
        name
        for _, name, _, _ in Formatter().parse(template)
        if name is not None and name
    }


RUN_STATUS_LABELS: Final[dict[str, dict[str, str]]] = {
    ZH_CN: {
        "created": "已创建",
        "running": "运行中",
        "completed": "已完成",
        "partial": "部分完成",
        "failed": "失败",
        "cancelled": "已取消",
    },
    EN: {
        "created": "Created",
        "running": "Running",
        "completed": "Completed",
        "partial": "Partially Complete",
        "failed": "Failed",
        "cancelled": "Cancelled",
    },
    ZH_HANT: {
        "created": "已建立", "running": "執行中", "completed": "已完成",
        "partial": "部分完成", "failed": "失敗", "cancelled": "已取消",
    },
}


def run_status_label(status: str, language: str) -> str:
    locale = normalize_language(language)
    return RUN_STATUS_LABELS[locale].get(status, status)


ERROR_REPLACEMENTS_EN: Final[tuple[tuple[str, str], ...]] = (
    ("此提供方暂不提供在线模型列表，请使用内置列表或手动填写。", "This provider does not expose an online model list. Use the built-in list or enter a model ID manually."),
    ("请先填写本次会话的 API Key，再刷新在线模型。", "Enter the API key for this session before refreshing online models."),
    ("认证失败，请检查 API Key 和账号权限。", "Authentication failed. Check the API key and account permissions."),
    ("认证失败（HTTP ", "Authentication failed (HTTP "),
    ("在线模型目录不存在（HTTP ", "The online model catalog was not found (HTTP "),
    ("），请检查 API Key、区域预设和账号权限。", "); check the API key, region preset, and account permissions."),
    ("，地址 ", ", endpoint "),
    ("（地址 ", " (endpoint "),
    ("），请检查区域预设或使用内置列表。", "); check the region preset or use the built-in list."),
    ("），已保留内置列表。", "); the built-in list was preserved."),
    ("该接口没有可用的在线模型目录，请使用内置列表或手动填写。", "This endpoint has no available online model catalog. Use the built-in list or enter a model ID manually."),
    ("在线模型目录返回 HTTP ", "The online model catalog returned HTTP "),
    ("，已保留内置列表。", "; the built-in list was preserved."),
    ("在线模型目录连接超时或不可达", "The online model catalog timed out or was unreachable"),
    ("在线模型目录返回了无法识别的数据", "The online model catalog returned unrecognized data"),
    ("在线模型目录为空", "The online model catalog was empty"),
    ("研究包必须包含 manifest.yaml 和 workflow.yaml", "The research pack must contain manifest.yaml and workflow.yaml"),
    ("研究包扩展名必须是 .othesis", "The research pack extension must be .othesis"),
    ("研究包超过 10 MB 限制", "The research pack exceeds the 10 MB limit"),
    ("研究包为空", "The research pack is empty"),
    ("研究包包含不安全路径：", "The research pack contains an unsafe path: "),
    ("研究包包含禁止文件：", "The research pack contains a prohibited file: "),
    ("研究包单个文件过大：", "A file in the research pack is too large: "),
    ("工作流引用的 Prompt 不存在：", "The workflow references a missing prompt: "),
    ("研究包 v0.1 不允许权限：", "Research pack v0.1 does not allow permission: "),
    ("相同 ID 和版本的研究包已经存在，但内容哈希不同；请修改版本号", "A research pack with the same ID and version already exists with a different content hash; change the version."),
    ("Prompt 路径越过研究包目录", "The prompt path escapes the research-pack directory"),
    ("SEC User-Agent 必须包含联系邮箱，例如 OpenThesis name@example.com", "The SEC User-Agent must include a contact email, for example OpenThesis name@example.com"),
    ("SEC 请求失败：", "SEC request failed: "),
    ("模型接口返回 HTTP ", "The model endpoint returned HTTP "),
    ("模型接口请求失败：", "Model endpoint request failed: "),
    ("接口响应为空", "The endpoint response was empty"),
    ("连接成功", "Connection successful"),
    ("无法解析模型响应：", "Could not parse the model response: "),
    ("无法解析 Ollama 响应：", "Could not parse the Ollama response: "),
    ("暂不支持模型提供方：", "Unsupported model provider: "),
    ("请输入你本人或所在研究团队可正常收信的邮箱地址。", "Enter a working email address belonging to you or your research team."),
    ("请选择一个 SEC 请求身份模板。", "Select an SEC requester profile."),
    ("请选择一个内置的常用公司。", "Select one of the built-in common companies."),
)


def translate_error(message: str, language: str) -> str:
    locale = normalize_language(language)
    if locale == ZH_HANT:
        replacements = {
            "\u7814\u7a76\u5931\u8d25": "\u7814\u7a76\u5931\u6557",
            "\u8fde\u63a5\u6210\u529f": "\u9023\u7dda\u6210\u529f",
            "\u8ba4\u8bc1\u5931\u8d25": "\u9a57\u8b49\u5931\u6557",
            "\u65e0\u6cd5\u52a0\u8f7d": "\u7121\u6cd5\u8f09\u5165",
        }
        for simplified, traditional in replacements.items():
            message = message.replace(simplified, traditional)
        return message
    if locale != EN:
        return message
    translated = message
    for chinese, english in ERROR_REPLACEMENTS_EN:
        translated = translated.replace(chinese, english)
    return translated
