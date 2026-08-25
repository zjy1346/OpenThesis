<div align="center">

# OpenThesis

**Evidence-first, model-agnostic company research**

[English](README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-Hant.md)

[![Release](https://img.shields.io/github/v/release/zjy1346/OpenThesis?display_name=tag&sort=semver)](https://github.com/zjy1346/OpenThesis/releases/latest)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows)](https://github.com/zjy1346/OpenThesis/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Let deterministic software handle deterministic work, and let models handle analysis and reasoning.

</div>

OpenThesis is an open-source Windows desktop application for long-term company research. It separates evidence collection and deterministic finance from model reasoning, so an LLM does not have to be the fact database, calculator, citation source, and analyst at the same time.

```text
official filings
  → evidence extraction and normalization
  → validated structured financial facts
  → deterministic financial analysis
  → specialist research agents
  → synthesis
  → verification
  → traceable research report
```

## Interface previews

The research workspace presents deterministic financial summaries, source evidence, and live research progress in one traceable workflow.

![Deterministic financial report](https://github.com/user-attachments/assets/30f83fa3-b441-4ba5-9ad4-79b4038456ba)

_Deterministic financial overview and year-over-year comparison._

![Source evidence](https://github.com/user-attachments/assets/3032effc-434d-4a4c-987f-4c711edcc65c)

_Source pages and evidence remain available for audit._

![Research progress](https://github.com/user-attachments/assets/de923fb9-d3a2-447b-b957-b455a58b8d68)

_Specialist agents show the current stage instead of hiding research progress._

## Model Center

Model configuration is separate from the research page. In Model Center, users create one or more connections, discover or add models manually, and test connectivity before research, retries, visual fallback, or OT Studio use them.

Built-in providers include:

- DeepSeek
- Qwen / DashScope
- Kimi China and global endpoints
- GLM
- OpenAI
- Gemini
- OpenRouter
- Ollama
- Custom OpenAI-compatible endpoints

One provider can have multiple connections, and one connection can expose multiple models. A `Free` label describes a verified free-tier or no-provider-fee path; quotas and availability are controlled by the provider. OpenThesis does not promise that third-party services remain free and never silently switches to a paid model after a free model fails.

Ollama connects only to a service that is already installed and running on the user's computer. OpenThesis does not bundle Ollama, download models, or package model weights.

## API key security

On Windows 2.0, API keys are stored in the current user's Windows Credential Manager. SQLite, ordinary settings, research records, reports, logs, and `.ot` files contain only non-secret model references and configuration versions; the frontend can show configured status but cannot read or echo old key plaintext.

When a key is replaced, the new key is tested as a staged version before an atomic switch. If the test fails, the old key remains active. Removing a connection also removes its related system credentials and model metadata.

This boundary protects against ordinary file leakage and accidental packaging, but cannot protect a Windows user session already controlled by malware. After moving to another computer, reinstalling the operating system, or losing system credentials, the user may need to configure keys again. See [the model configuration and key security research](docs/research/model-configuration-security-2.0.md) for the detailed design.

## OT ecosystem and OT Studio

OpenThesis 2.0 uses `.ot` as a declarative, verifiable research package format. OT Studio supports:

- guided research goals, sliders, options, and workflow forms;
- raw JSON editing, undo, redo, and diagnostics for advanced users;
- optional natural-language field assistance;
- proposed diffs with explicit accept or reject decisions;
- deterministic compilation and export.

Small models can suggest only one allowlisted field at a time, and a draft is not changed until the user accepts the suggestion. Manual editing, validation, and export remain available without a configured model.

An `.ot` package is a ZIP-compatible container containing a normalized `manifest.json`, `ot.lock.json`, workflow, output settings, UI schema, and prompt resources. The compiler fixes entry order, timestamps, and canonical JSON, and records resource hashes and content identity so identical inputs produce identical bytes. The loader limits file count, per-file size, total size, and compression ratio, and rejects path traversal, absolute paths, case-colliding names, symlinks, executable content, undeclared resources, hash mismatches, secrets, and unknown execution capabilities.

Version 2.0 does not open, import, run, or generically convert user or third-party legacy `.othesis` files. The project's own legacy official package is converted once during development by a controlled tool, with source hashes and workflow semantic-equivalence checks.

## Supported markets and data boundaries

| Market | Official filing sources | Current boundary |
| --- | --- | --- |
| United States | SEC EDGAR | Real requests require the user's own SEC contact email |
| Mainland China A-shares | CNINFO and SSE, SZSE, and BSE disclosures | Shanghai, Shenzhen, and Beijing exchanges |
| Hong Kong | HKEXnews and issuer disclosures | Main Board and GEM; listing and reporting currencies remain separate |

Reports preserve reporting period, consolidation scope, currency, unit, source page, and evidence identifiers. Different periods or scopes are never silently mixed, and missing values are not treated as zero. Financial-institution research remains Beta: disclosures, financials, and risk analysis are retained, but standard free-cash-flow reverse DCF is not applied where it is unsuitable for banks or insurers.

OpenThesis does not fetch live prices automatically. When a price, market cap, or valuation snapshot is needed, the user enters the value, currency, and date.

## Research and reproducibility

A study can select one primary model and zero or more comparison models. Financial, business-model, accounting-risk, growth, counterargument, scenario, synthesis, and verification roles share the same curated evidence instead of guessing facts from model memory.

Research records include model references and configuration versions, parameters, report language, package content identity, data snapshots and hashes, evidence counts, market snapshots, and run identifiers. They do not include API keys.

Visual financial-report fallback also uses a configured model reference. Only after local structured extraction fails, the user enables the feature, selects a tested vision-capable model, and confirms data transmission can candidate pages enter the review workflow. Each request is limited to at most 20 pages or 10 MB.

## Download and run

The official binary target is currently Windows x64. There are no official macOS or Linux installers yet.

1. Open [GitHub Releases](https://github.com/zjy1346/OpenThesis/releases/latest).
2. Download the Windows x64 portable ZIP and its corresponding SHA-256 file.
3. Verify the hash, extract the archive completely, and run `OpenThesis\OpenThesis.exe`.
4. Keep the adjacent `bin` directory; it contains the research sidecar, not models or Ollama.
5. You can start with the synthetic demo to understand deterministic finance and report structure at low cost.

## Run from source

You need Python 3.11+, Node.js, Rust, and Visual C++ Build Tools. After installing Python and the desktop dependencies, run:

```powershell
$env:OPENTHESIS_PYTHON = "C:\path\to\python.exe"
powershell -ExecutionPolicy Bypass -File .\scripts\desktop.ps1 dev
```

Tests and local packaging:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\package.ps1
```

`package.ps1` builds the Tauri desktop application and isolated Python sidecar, then creates a portable ZIP and SHA-256 file in `installer-output/`. Release validation also runs a privacy scan and portable-runtime verification.

## Privacy and usage limits

- API keys are stored in Windows Credential Manager and are not written to SQLite, reports, logs, backups, or `.ot` files.
- SEC contact email and research history remain in the current user's local data directory and are not included in release packages.
- A custom remote endpoint displays its complete origin and requires confirmation of where research content will be sent.
- Local Ollama and remote Ollama are shown distinctly; a remote address is never labeled local.
- OpenThesis is research software, not a trading signal, automated trading system, or guarantee of investment returns.

When reporting a security issue, do not paste keys, personal information, research databases, or complete financial-report files into a public issue.

## Contributing

Issues, test cases, documentation improvements, and pull requests are welcome. New `.ot` research packages should remain declarative, least-privilege, and secret-free; do not add executable code, arbitrary filesystem access, or hidden network requests.

OpenThesis is released under the [Apache License 2.0](LICENSE).
