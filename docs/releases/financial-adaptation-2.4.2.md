# OpenThesis 2.4.2 财报派生负债修复验收记录

- workflow name: `financial-adaptation`
- target version: `2.4.2`
- Acceptance: PASS（独立 Antigravity QA 最终验收通过）
- User Test: PASS（用户已完成实机验证并批准上传，2026-09-08）
- Upload Ready: YES

## 请求与根因

2.4.1 独立 QA 报告 `DEFECT-241-SEC-01` 指出：可口可乐等发行人的
`LiabilitiesAndStockholdersEquity - StockholdersEquity` 实际表示“负债加非控制性权益”，
却与真实负债路径按同一容差强制比较，因而误杀 2017–2025 年负债事实。

## 已批准的修复方案

- 官方 `Liabilities` 始终优先，存在时不生成派生负债。
- `LiabilitiesAndStockholdersEquity - StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`
  表示真实负债路径。
- `LiabilitiesCurrent + LiabilitiesNoncurrent` 表示真实负债路径。
- 归母权益路径只有在同一 accession、期间、币种、合并范围存在明确 NCI 事实时才可使用，
  公式为 `LiabilitiesAndStockholdersEquity - StockholdersEquity - NCI`。
- 不从总权益减归母权益猜测 NCI；缺乏明确 NCI 时不生成该路径。
- 多条同语义路径必须在会计容差内一致；跨期、跨币种、跨范围或冲突路径拒绝。
- 每条派生事实保留公式、输入标签、accession、期间、币种和来源 URL。

## 代码与测试范围

- `src/openthesis/sec_client.py`
- `tests/test_sec_client.py`
- 版本位置：`pyproject.toml`、`src/openthesis/__init__.py`、`desktop/package.json`、
  `desktop/package-lock.json`、`desktop/src-tauri/Cargo.toml`、`desktop/src-tauri/Cargo.lock`、
  `desktop/src-tauri/tauri.conf.json`、`scripts/package-desktop.ps1`

## 验收清单

- [x] 官方 Liabilities 优先。
- [x] 含 NCI 总权益路径与流动/非流动负债路径可交叉验证。
- [x] 显式 NCI 归母权益反向路径可审计。
- [x] 仅归母权益且无 NCI 时不猜测负债。
- [x] KO 型 2017–2025 九年正向回归测试。
- [x] 少数股东权益反向测试。
- [x] 跨期、跨币种、冲突路径拒绝测试保持覆盖。
- [x] Antigravity QA 独立复验并生成最终报告（结论 PASS，参见 docs/qa/financial-ingestion-2.4.2-final-qa-report.md）。
- [x] 候选包通过 QA，已具备用户测试条件。

## 开发侧结果

- 新增语义回归测试：4/4 通过。
- SEC 定向回归：`tests.test_sec_client` 16/16 通过（退出码 0）。
- 前端 production build：通过；Tauri release 构建：通过。
- 发布隐私门：`ForbiddenDataEntries=0`、`CredentialOrPersonalDataMatches=0`。
- 便携包结构、Windows GUI 子系统、隔离数据目录与 sidecar 运行检查：通过。
- 上述结果已结合 Antigravity QA 与用户实机验证完成最终交接。

## 发布候选

- artifact: `D:\githubmax\installer-output\OpenThesis-2.4.2-windows-x64-portable.zip`
- SHA-256: `E7A1C9F9E17F5A267036149DB97BBECC8EA2A2D86564E6D748AA861686BE1EB0`（开发交接值与 Antigravity QA 独立实测值一致：E7A1C9F9E17F5A267036149DB97BBECC8EA2A2D86564E6D748AA861686BE1EB0）
- signature: `unsigned-test`
- GitHub upload: ready

## Acceptance results

### Antigravity QA 最终验收结论（2026-09-08）

- **验收判定**：**`PASS`**
- **报告文档**：`docs/qa/financial-ingestion-2.4.2-final-qa-report.md`
- **主要复验结果**：
  - 可口可乐 (KO) 2016–2025 FY 真实数据重放：10 年负债全量成功派生（2025 年为 70,541,000,000 USD），`DEFECT-241-SEC-01` 彻底消除，期末验证组 11 概念完备。
  - 5 条派生优先级与会计语义规则实测全部通过。
  - 风险抽查项（比亚迪双锚点、伊利/立讯、谷歌 statement 映射、小米 2024 年营收 3659.06 亿/净利润 236.58 亿与 `31,000` 消除）无任何回归。
  - 全量自动化测试通过：Python 480/480（耗时 359.3s）、Vitest 76/76、前端 production build 成功。
  - 安装包 SHA-256 独立复算完全吻合，便携包在隔离环境完整运行且数据目录生效。
  - 准予进入用户验收测试阶段。

## 未解决事项

- 无。用户已完成实机验证并批准上传。
