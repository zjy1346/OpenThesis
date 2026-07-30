# OpenThesis

> 面向长期个人投资者的开源、模型无关 AI 公司研究投资系统。  
> An open-source, model-agnostic AI company research system for long-term individual investors.

## 中文简介

OpenThesis 帮助个人投资者研究公司的长期价值，而不是预测短期股价。它读取
SEC 财报与 XBRL 数据，建立可追溯的证据链，协调财务、商业模式、风险、增长、
质疑、预测和验证等专门 Agent，并保存可以持续修订的投资论点。

用户可以选择 Ollama 本地模型或任意 OpenAI-compatible 云端模型，也可以导入
声明式 `.othesis` 研究模块。财务指标与反向 DCF 由确定性程序计算，AI 生成的
事实结论必须引用证据。项目不会连接券商、执行交易或提供短线信号。

## English introduction

OpenThesis helps investors study companies instead of predicting short-term price movements. It ingests public filings, builds traceable evidence, coordinates specialized research agents, creates long-term operating scenarios, and preserves an investment thesis that can be reviewed as new information arrives.

The user chooses the model. OpenThesis supplies the research protocol, financial tools, prompts, verification, and reproducibility layer.

## Product principles

- Bring your own model.
- Every factual claim needs evidence.
- Financial calculations are deterministic.
- Facts, inferences, assumptions, and unknowns remain distinct.
- Forecasts use scenarios, ranges, probabilities, and invalidation conditions.
- Research is reproducible from a model, prompt pack, and point-in-time data snapshot.
- AI supports decisions; it does not execute trades.

## Initial scope

- Target user: serious individual long-term investors.
- Initial market: US-listed companies.
- Initial source: SEC 10-K filings and Inline XBRL facts.
- Runtime: local-first Windows desktop application.
- Model support: local and cloud providers selected by the user.
- Output: cited company research, growth opportunities, long-term scenarios, valuation assumptions, risks, and a versioned investment thesis.

OpenThesis will not integrate brokerage accounts, place orders, generate short-term trading signals, or promise investment returns.

## Current release

Version 0.2.0 includes:

- a local Windows desktop interface;
- a visible three-step research flow with a persistent primary start button;
- common-company shortcuts and guided SEC requester identity templates;
- in-app SEC help that explains EDGAR access and safe contact-email usage;
- SQLite persistence and research history;
- SEC company lookup, five-year 10-K download, text/table evidence, and Company Facts ingestion;
- deterministic financial metrics, reverse DCF implied expectations, and an offline synthetic-company demo;
- Ollama and OpenAI-compatible model adapters;
- an evidence-aware multi-agent workflow for financials, business, accounting risk, growth, skepticism, scenarios, synthesis, and verification;
- clickable SEC evidence sources in exported and on-screen reports;
- editable, append-only investment-thesis versions;
- optional side-by-side model comparison on the same inputs;
- safe declarative `.othesis` research-pack import;
- the built-in `official.long-term-fundamentals` pack.

The consolidated product and architecture specification is in [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md).

## Run the portable Windows release

1. Extract `OpenThesis-0.2.0-windows-x64-portable.zip`.
2. Keep `_internal` beside `OpenThesis.exe`.
3. Start `OpenThesis.exe`.
4. Choose the synthetic demo company for a fully offline first run.

The application never connects to a brokerage or executes a trade. API keys are
kept in memory for the current session and are not written to the local database.

## Run from source on the development machine

Install Python 3.11 or newer. If `python` is not on `PATH`, set
`OPENTHESIS_PYTHON` to the full path of `python.exe`. No machine-specific path is
stored in the repository.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

Select the synthetic demo company to exercise the complete local data, report, and history path without network access or an API key. Configure a SEC contact email before querying real companies. The packaged Windows application is built at `dist/OpenThesis/OpenThesis.exe`; keep the accompanying `_internal` directory beside the executable.

## Test and package

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
python -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
```

The packaging command runs the test suite, builds the Windows application, executes deterministic and GUI smoke tests against the frozen binary, and creates a portable ZIP plus SHA-256 checksum under `installer-output/`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
