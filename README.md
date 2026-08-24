<div align="center">

# OpenThesis

**Evidence-first, model-agnostic company research**

[简体中文](#简体中文) · [English](#english) · [繁體中文](#繁體中文)

[![Release](https://img.shields.io/github/v/release/zjy1346/OpenThesis?display_name=tag&sort=semver)](https://github.com/zjy1346/OpenThesis/releases/latest)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows)](https://github.com/zjy1346/OpenThesis/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

让确定性软件负责确定性工作，让模型负责分析与推理。

</div>

## 简体中文

OpenThesis 是一个面向长期公司研究的开源 Windows 桌面应用。它不连接券商、不自动交易，也不承诺投资收益。它解决的问题是：语言模型可以帮助分析业务和风险，但不应该同时充当事实数据库、计算器、引用来源和分析师。

```text
官方披露
  → 证据提取与标准化
  → 经过验证的结构化财务事实
  → 程序化确定性财务分析
  → 专业研究 Agent
  → 综合
  → 验证
  → 可追溯研究报告
```

![确定性财务报告](https://github.com/user-attachments/assets/30f83fa3-b441-4ba5-9ad4-79b4038456ba)

![来源证据](https://github.com/user-attachments/assets/3032effc-434d-4a4c-987f-4c711edcc65c)

![研究进度](https://github.com/user-attachments/assets/de923fb9-d3a2-447b-b957-b455a58b8d68)

### 2.0 的核心变化

#### 模型中心

模型配置从研究页面中完全分离。用户先在独立的“模型中心”建立一个或多个连接、发现或手动添加模型并完成连接测试；研究页、重试流程、视觉识别和 OT 创作工作室只引用已经配置且可用的模型。

内置 Provider（模型服务商）目录包括：

- DeepSeek
- Qwen / DashScope
- Kimi 中国区与国际区
- GLM
- OpenAI
- Gemini
- OpenRouter
- Ollama
- 自定义 OpenAI-compatible Endpoint（兼容 OpenAI 接口的服务地址）

同一 Provider 可以创建多个连接，一个连接可以添加多个模型。免费路径会显示 `Free`，但免费额度和模型可用性由服务商决定，OpenThesis 不承诺第三方服务永久免费，也不会在免费模型失败后静默切换到收费模型。

Ollama 只连接用户电脑上已经安装并启动的服务。OpenThesis 不内置 Ollama、不下载模型、不打包模型权重，也不会为了“本地 AI”增加无意义的安装体积。

#### API Key 安全保存

Windows 2.0 使用当前系统用户的 Windows Credential Manager（Windows 凭据管理器）保存 API Key。SQLite、普通设置、研究记录、报告、日志和 `.ot` 文件只保存非秘密的模型引用与配置版本，不保存密钥；前端只能看到“已配置”状态，不能读取或回显旧密钥明文。

替换密钥时，新密钥先以暂存版本完成连接测试，测试成功后才原子切换；失败时旧密钥保持不变。删除连接会同时清理相关系统凭据和模型元数据。

这个边界防止普通文件泄漏和误打包，但不能抵御已经控制当前 Windows 用户进程的恶意软件。换电脑、重装系统或系统凭据损坏后，用户可能需要重新配置密钥。

详细设计见 [模型配置与密钥安全研究](docs/research/model-configuration-security-2.0.md)。

#### OT 生态与 OT Studio

OpenThesis 2.0 使用 `.ot` 作为声明式、可验证的研究包格式。OT Studio（OT 创作工作室）提供：

- 面向新手的研究目标、滑块、选项和工作流表单；
- 面向专业用户的原始 JSON 编辑、撤销、重做和诊断；
- 可选的自然语言字段助手；
- 建议前后差异、接受或拒绝；
- 确定性编译与导出。

小模型一次只能建议一个白名单字段；用户接受前不会修改草稿。没有配置模型时，所有手动编辑、校验和导出能力仍然可用。

`.ot` 是 ZIP-compatible（兼容 ZIP 的容器），包含规范化的 `manifest.json`、`ot.lock.json`、工作流、输出设置、UI Schema 和提示资源。编译器固定条目顺序、时间戳和规范 JSON，记录每个资源哈希与内容身份，使同一输入得到相同字节。加载器限制文件数、单文件大小、总大小和压缩比，并拒绝路径穿越、绝对路径、大小写重复、符号链接、可执行内容、未声明资源、哈希不一致、秘密和未知执行能力。

2.0 不打开、导入、运行或通用转换用户及第三方旧 `.othesis`。项目自己维护的旧官方包只在开发期通过受控工具一次性转换为官方 `.ot`，同时记录源哈希并校验工作流语义等价性。

### 支持的市场与数据边界

| 市场 | 官方披露入口 | 当前边界 |
| --- | --- | --- |
| 美国 | SEC EDGAR | 真实请求需要用户自己的 SEC 联系邮箱 |
| 中国 A 股 | 巨潮资讯及沪深北交易所披露 | 包含上交所、深交所、北交所 |
| 中国香港 | HKEX 披露易及发行人披露 | 主板与 GEM；上市币种和报告币种分开记录 |

程序会保留报告期、合并口径、币种、单位、来源页和证据标识。不同期间或口径不会被静默混合，缺失值不会被当成零。金融机构研究仍是 Beta：保留披露、财务与风险分析，但不会套用不适合银行或保险公司的标准自由现金流反向 DCF。

OpenThesis 不自动抓取实时价格。需要价格、市值或估值快照时，由用户输入数值、币种与日期。

### 研究与可复现性

一次研究可以选择一个主模型和零个或多个比较模型。财务、商业模式、会计风险、增长、反方审查、情景、综合与验证角色共享同一组经过整理的证据，而不是各自从模型记忆中猜测事实。

研究记录包含模型引用与配置版本、参数、报告语言、研究包内容身份、数据快照和哈希、证据数量、市场快照及运行标识；不包含 API Key。

视觉财报兜底也是已配置模型引用。只有本地结构化识别失败后，用户启用该功能、选择具备视觉能力且已测试的模型并确认数据发送，候选页面才会进入待审核流程。每次限制为最多 20 页或 10 MB。

### 下载与运行

当前正式二进制目标是 Windows x64。macOS 和 Linux 暂无正式安装包。

1. 打开 [Releases](https://github.com/zjy1346/OpenThesis/releases/latest)。
2. 下载 Windows x64 portable ZIP 和对应 SHA-256 文件。
3. 校验哈希，完整解压，然后运行 `OpenThesis\OpenThesis.exe`。
4. 保留相邻的 `bin` 目录；它包含研究 sidecar，不包含模型或 Ollama。
5. 可以先运行合成演示，低成本理解确定性财务和报告结构。

### 从源码运行

需要 Python 3.11+、Node.js、Rust 和 Visual C++ Build Tools。先安装 Python 与桌面依赖，然后执行：

```powershell
$env:OPENTHESIS_PYTHON = "C:\path\to\python.exe"
powershell -ExecutionPolicy Bypass -File .\scripts\desktop.ps1 dev
```

测试与本地打包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
```

`package.ps1` 只构建 Tauri 桌面应用和隔离的 Python sidecar，并在 `installer-output/` 生成便携 ZIP 与 SHA-256 文件。发布前还会执行隐私扫描和便携运行验证。

### 隐私与使用限制

- API Key 由 Windows 凭据管理器保存，不进入 SQLite、报告、日志、备份或 `.ot`。
- SEC 联系邮箱和研究历史只保存在当前用户的本机数据目录，不进入发布包。
- 自定义远程 Endpoint 会显示完整 Origin，并要求用户确认研究内容的发送位置。
- 本地 Ollama 与远程 Ollama 会明确区分；远程地址不会标记为“本地”。
- OpenThesis 是研究软件，不是交易信号、自动交易系统或投资收益保证。

报告安全问题时，请勿在公开 Issue 中粘贴密钥、个人信息、研究数据库或完整财报文件。

### 参与贡献

欢迎提交 Issue、测试案例、文档改进和 Pull Request。新增 `.ot` 研究包时，请保持声明式、最小权限和无秘密；不要加入可执行代码、任意文件系统访问或隐藏网络请求。

OpenThesis 使用 [Apache License 2.0](LICENSE)。

---

## English

OpenThesis is an open-source Windows desktop application for long-term company research. It separates evidence collection and deterministic finance from model reasoning, so an LLM does not have to be the database, calculator, citation source, and analyst at the same time.

```text
official filings
  → evidence extraction
  → validated financial facts
  → deterministic finance
  → specialist agents
  → synthesis
  → verification
  → traceable report
```

Version 2.0 adds a separate Model Center and OT Studio. Model Center stores reusable API credentials in Windows Credential Manager and exposes only non-secret configured-model references to research, retries, vision fallback, and OT assistance. Built-in providers are DeepSeek, Qwen/DashScope, Kimi (China and global), GLM, OpenAI, Gemini, OpenRouter, Ollama, and custom OpenAI-compatible endpoints.

OpenThesis can connect to an Ollama service that you already installed and started. It never bundles Ollama, model weights, or automatic model downloads. “Free” labels describe verified no-provider-fee or free-tier paths; third-party quotas can change, and there is no silent paid fallback.

OT Studio creates deterministic `.ot` research packages through guided controls or professional JSON editing. Optional model assistance is limited to one approved field and requires an explicit accept/reject decision. Manual authoring, validation, and export remain available without a model. User or third-party `.othesis` files are not supported in 2.0; only the project's own legacy official pack is converted during development.

Supported filing markets are US/SEC, mainland China (SSE, SZSE, and BSE), and Hong Kong/HKEX. OpenThesis keeps listing and reporting currencies separate, preserves filing periods and evidence references, and does not silently turn missing values into zero. Financial institutions remain Beta, and live market prices are not fetched automatically.

The official binary target is Windows x64; there are no official macOS or Linux binaries yet. Download releases from [GitHub Releases](https://github.com/zjy1346/OpenThesis/releases/latest), or run the Tauri desktop application from source with `scripts/desktop.ps1 dev`.

OpenThesis does not connect to brokerages, execute trades, provide short-term signals, or promise investment returns. It is licensed under [Apache-2.0](LICENSE).

---

## 繁體中文

OpenThesis 是面向長期公司研究的開源 Windows 桌面應用。它把官方披露、證據整理與確定性財務計算放在模型推理之前，避免讓語言模型同時充當資料庫、計算器、引用來源與分析師。

2.0 新增獨立的模型中心和 OT 創作工作室。模型中心把 API Key 儲存在目前 Windows 使用者的系統憑據庫中；研究、重試、視覺兜底和 OT 助手只能使用不含秘密的已設定模型引用。支援 DeepSeek、Qwen / DashScope、Kimi 中國區與國際區、GLM、OpenAI、Gemini、OpenRouter、Ollama 和自訂 OpenAI-compatible Endpoint。

OpenThesis 只連接使用者已經安裝並啟動的 Ollama，不內建 Ollama、不下載模型，也不打包模型權重。免費標籤只表示已核對的本機無服務商費用或第三方 Free Tier；額度可能改變，且不會在失敗後靜默切換到付費模型。

OT Studio 可用表單、滑桿和選項建立 `.ot`，也可直接編輯 JSON。模型一次只建議一個允許欄位，使用者接受前不會修改草稿；沒有模型時仍可手動編寫、驗證和匯出。2.0 不支援使用者或第三方舊 `.othesis`，只在開發期受控轉換專案自己的舊官方包。

目前支援 US/SEC、中國 A 股（滬深北）和香港/HKEX 官方披露。上市幣種與財報幣種分開記錄，缺失值不會被當作零。金融機構研究仍為 Beta，實時價格不會自動抓取。

正式二進位目標目前只有 Windows x64；macOS 和 Linux 暫無正式安裝包。OpenThesis 不連接券商、不執行交易、不提供短線訊號，也不承諾投資回報。專案使用 [Apache-2.0](LICENSE)。
