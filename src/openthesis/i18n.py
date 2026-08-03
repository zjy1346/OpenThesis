from __future__ import annotations

from string import Formatter
from typing import Final


ZH_CN: Final = "zh-CN"
EN: Final = "en"
SUPPORTED_LANGUAGES: Final = (ZH_CN, EN)
LANGUAGE_NAMES: Final = {
    ZH_CN: "简体中文",
    EN: "English",
}


def normalize_language(value: str | None) -> str:
    normalized = (value or "").strip().lower().replace("_", "-")
    if normalized in {"en", "en-us", "en-gb"}:
        return EN
    return ZH_CN


def language_name(language: str) -> str:
    return LANGUAGE_NAMES[normalize_language(language)]


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


MODEL_PRESET_LABELS: Final[dict[str, dict[str, str]]] = {
    ZH_CN: {
        "none": "不调用 AI（本地确定性分析）",
        "deepseek": "国内 · DeepSeek",
        "qwen": "国内 · Qwen（通义千问）",
        "kimi": "国内 · Kimi",
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
        "glm": "China · GLM",
        "openai": "International · OpenAI",
        "gemini": "International · Gemini",
        "openrouter": "International · OpenRouter",
        "ollama": "Local · Ollama",
        "custom": "Custom · OpenAI-compatible",
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
}


def translate(source: str, language: str, **params: object) -> str:
    template = UI_EN.get(source, source) if normalize_language(language) == EN else source
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
}


def run_status_label(status: str, language: str) -> str:
    locale = normalize_language(language)
    return RUN_STATUS_LABELS[locale].get(status, status)


ERROR_REPLACEMENTS_EN: Final[tuple[tuple[str, str], ...]] = (
    ("此提供方暂不提供在线模型列表，请使用内置列表或手动填写。", "This provider does not expose an online model list. Use the built-in list or enter a model ID manually."),
    ("请先填写本次会话的 API Key，再刷新在线模型。", "Enter the API key for this session before refreshing online models."),
    ("认证失败，请检查 API Key 和账号权限。", "Authentication failed. Check the API key and account permissions."),
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
    if normalize_language(language) != EN:
        return message
    translated = message
    for chinese, english in ERROR_REPLACEMENTS_EN:
        translated = translated.replace(chinese, english)
    return translated
