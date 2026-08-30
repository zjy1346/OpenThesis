# OpenThesis 2.2.1

- Workflow name: `financial-reliability`
- Target version: `2.2.1`
- Acceptance: `PASS`
- User Test: `PASS`
- Upload Ready: `YES`

## 请求、决策与范围

### Requested changes

- 用广泛适配、低误识别率的统一财务事实编译架构替代逐公司补丁。
- 必需字段必须实际识别；不得以容忍字段缺失、补零或模型补数伪装成功。
- 解决归母权益/合并总权益、期间、scope、币种、单位、比较列与修订冲突。
- 失败节点执行无 token、有限、可取消、幂等重试；可选 MinerU 只处理失败页。
- 报告、增长机会证据和三语 UI 只消费通过统一质量门的事实。
- 完整验证后生成无签名 Windows x64 便携测试包；签名以后处理。

### Approved implementation decisions

- `FinancialFactCompiler` 是生产事实、验证、覆盖率与 AI 准入的唯一决策边界。
- source adapters 只产生候选与证据；`GapResolver` 依固定优先级补缺，不覆盖更高优先级事实。
- `equity` 表示归母权益；`total_equity` 表示合并所有者权益合计，分别服务 ROE 与资产负债勾稽。
- Golden corpus 对 A/H 股双权益口径做精确勾稽；生产容差不得掩盖金标错误。
- 不降低最终综合报告 schema；测试 provider 必须包含资产负债表等 2.2.1 必需章节。
- 正式 Authenticode 签名不在本轮范围内，产物必须明确标记 `unsigned-test` / `NotSigned`。

### Unresolved decisions

- 正式代码签名证书及发布签名流程留待后续。
- 外部用户实机验收尚未完成；在用户明确通过前不得上传发布。

### Relevant scope / files

- 编译与提取：`src/openthesis/financial_compiler.py`、`financial_ast_adapter.py`、`financial_ingestion.py`。
- 生产接入：service、research、reporting/report projection、market/SEC adapters、storage/sidecar。
- 桌面端：研究进度、重试、模型中心、报告工作区、协议、类型、样式与三语文本。
- 验证：compiler/ingestion/service/research/reporting/growth/frontend/Rust tests、官方 golden corpus 与 evaluator。
- 发布元数据与脚本：Python、npm、Cargo、Tauri、sidecar version、2.2.1 打包与验证脚本。

## 广泛适配架构与完整字段发布门

本方案将现有规则补丁升级为统一深模块架构，并保留腾讯精确验收、隐私安全与打包约束。

### 统一财务事实编译器

生产唯一事实入口：

```python
compiler.compile(subject, period_range, policy) -> FinancialDataset
```

Dataset 必须包含 filings、resolved_facts、quarantined_facts、evidence、conflicts、分组 validation、coverage、diagnostics 和 `allow_ai`。service、report、retry 只能消费 dataset/profile，不得自行判断完整性、跨期拼接或绕过质量门。

### 内部 seam 与适配器

- `FilingSource`：SEC、SSE、SZSE、BSE、HKEX 及 fixture adapters，输出带 accession、期间、revision/supersedes、来源 URL/hash 的 `FilingIdentity`。
- `FactExtractor`：US-GAAP、IFRS-HKFRS、CASBE、issuer extensions 的 XBRL/iXBRL、PDF 坐标 AST、MinerU 云失败页和 fixture adapters；adapter 只输出 `CandidateBatch + Evidence`，无权直接产生 accepted fact。

生命周期固定为 `FilingIdentity → FactCandidate → EvidenceRef → ResolvedFact → FactGroupValidation`。候选、冲突和拒绝原因全部保留审计；报告和模型只能读取 `ResolvedFact`。

### Profile、缺口补全与 taxonomy

`CoveragePlanner` 按市场、准则、行业和公司类型生成 profile。非金融企业要求核心六项；银行、保险、券商使用预声明专用 profile，不套用工业企业规则或借减少字段掩盖失败。

`GapResolver` 只处理缺失 concept 或失败页，顺序为：官方结构化事实 → PDF 坐标 AST → 同年度官方披露 → 用户授权 MinerU。官方明确未披露时使用有证据的 `NOT_DISCLOSED`／`NOT_APPLICABLE`；不得补零、猜数或把解析错误当未披露。

PDF 坐标 AST 必须使用表格标题、scope、列头、期间和完整行标签的通用语法；legacy 整页正则只能诊断，不能决定事实。MinerU 仅处理失败页并产生候选，候选仍须通过同一质量门；禁止本地训练、下载或捆绑模型。

### Canonical taxonomy 与五层验证

canonical taxonomy 覆盖 US-GAAP、IFRS-HKFRS、CASBE 和 issuer extensions。映射同时依据 concept、statement role、行列结构、归属上下文、期间和 scope，禁止公司级字符串特例。腾讯相邻归属行作为通用语法 fixture。

五层验证为：身份/类型/entity/scope/币种/单位/期间；报表身份和当前期列；勾稽/量级/符号/年度连续性；官方来源冲突、修订和 supersedes；profile 覆盖、衍生指标可追踪性和 AI 准入。任一关键层失败即隔离事实或整组。

### Golden corpus、盲测与迁移

Golden corpus 和盲测按市场、adapter、concept 记录完整率、数值误识别率、冲突率、MinerU 触发率和耗时。固定样本已披露必需字段须 100% 完整且数值误识别率为 0 才能发布。五年矩阵按适用 profile 验收：非金融至少核心六项，金融使用专用 profile。

迁移顺序：canonical 类型和 adapter 包装 → 唯一质量门 → service 判定迁入 compiler → legacy 正则降为诊断 → A 股官方 XBRL/结构化 adapter → 报告和 AI 迁移到 `ResolvedFact` → 删除重复规则，避免大爆炸重构。

## 2.2.1 可执行验收条款

### 状态与授权

- 用户已授权直接实施 2.2.1，无需二次确认。
- 容忍字段缺失并继续研究不算完成；必需字段必须实际识别并通过质量门。
- 代码和矩阵验收后先生成无签名 Windows x64 便携测试包；正式签名及证书后续处理，不得伪造签名状态。

### 腾讯精确红测与绿测

- `2023040601848`：PDF p132 `Consolidated Income Statement`，`RMB’Million`，FY2022 当前列；`Attributable to:` 与 `Equity holders of the Company` 相邻，`net_income=188,243,000,000 CNY`。
- `2022040701694`：PDF p170 同类正式表，FY2021 当前列，`net_income=224,822,000,000 CNY`。
- 两组都必须具备收入、归母净利润、资产、负债、权益、经营活动现金流六项核心事实，consolidated、CNY、scale `1,000,000`，并保留 page/bbox/raw/单位/当前列证据。
- EPS/basic/diluted 不得误匹配，比较期不得替代当前期；READY_WITH_WARNINGS 不等于字段完整成功。

### 跨市场五年样本

A 股至少宁德时代、中芯国际；港股至少腾讯与汇丰或美团；美股至少 Apple、Microsoft。每家公司连续五个 FY 的适用 profile 必需字段必须 100% 有官方 provenance；95%、字段级保留或模型补数均不通过。

### MinerU、隐私与失败页边界

仅在结构化来源和 PDF AST 均失败且用户启用时处理失败页。上传前显示供应商、公司、报告、页码和 hash；不得发送 key、数据库、个人路径或其他公司内容。结果必须含页/bbox（或结构化定位）、raw、confidence、hash，并重新经过期间/scope/currency/unit/statement/量级/勾稽校验。未配置、超时、限流、取消或不可验证响应必须安全停止；禁止本地模型。

### 防错、UI 与重试

比较期不得当当前期，季度/中期不得当 FY，scope/currency/unit 不混，缺失不得补 0，括号负数不得丢失；修订保留旧文件，冲突不得静默选择，不完整事实不得进模型。

三语用户视图必须将缺失转换为本地化、可操作说明，不显示无解释的空白、`—`、`None` 或 `unknown`。下载、解析、质量门、报告投影和刷新显示独立阶段。无 token 重试只对失败节点执行有限、可取消、幂等的确定性重试，不重跑成功节点或调用模型。报告刷新失败必须提供真实的仅刷新操作，UI 与取消保持响应。

### 自动化与打包

运行完整 Python、前端 test/typecheck/build、Rust tests，并逐项记录。固定官方 fixture 必须有 URL、公告/accession、报告期、最小原文或结构化事实及可重算 SHA-256。全部通过后生成无签名 Windows x64 portable，记录路径、SHA-256、build mode、portable/privacy/runtime 结果；正式 Authenticode 属于后续外部步骤。

### 完成定义

只有 compiler 成为生产唯一入口、腾讯两组六项核心绿测、跨市场 profile 矩阵通过、候选冲突与隐私边界通过、零 token 重试和分阶段 UI 通过、完整自动化检查通过，并记录无签名测试包结果后，才可标记 2.2.1 完成。任一条款未验证必须标为未完成或外部阻塞。

## 当前实现记录（阶段一）

- 已实现：正式 PDF AST 增加有界归属上下文（同表、前两行、同标签列），并排除 EPS/basic/diluted；`FinancialIngestionEngine` 暴露 `extract_pdf_candidates` 与 `validate_group` 公共 seam。
- 已实现：`financial_ast_adapter.py` 是唯一 PDF adapter，仅包装正式 AST 输出为 `CandidateBatch`；`FinancialFactCompiler` 通过显式候选证据关联进入统一质量门，并使用 Decimal 上下文键隔离冲突。
- 已验证：腾讯本机官方 PDF `2023040601848`（FY2022，p132，188243000000）与 `2022040701694`（FY2021，p170，224822000000）均经正式 ingestion AST 得到六项核心事实并为 VERIFIED；另有 CI 可生成的最小坐标 PDF fixture。
- 当前状态：`Acceptance: PENDING`；`User Test: PENDING`；`Upload Ready: NO`。完整跨市场矩阵、service 迁移和打包仍未完成，不得据此标记发布通过。

## 当前实现记录（阶段二）

- 已实现：`CoveragePlanner` 按非金融企业与银行、保险、券商 profile 声明必需概念；金融 profile 额外要求净利息收入，非金融 profile 保留核心六项并声明可选扩展概念。
- 已实现：`GapResolver` 提供结构化事实、正式 PDF AST、同年度披露和用户授权视觉适配器的有序注入 seam；只返回候选和证据，支持有限取消与失败诊断，不补零、不调用模型、不绕过质量门。
- 已实现：`StructuredFactExtractor` 将既有官方结构化来源显式包装为 `CandidateBatch`，候选与 `EvidenceRef` 使用 fact_id 显式关联；compiler 使用 Decimal 上下文键并统一调用公开 `validate_group`。
- 已实现：service 的 SEC 兼容质量桥已不再直接调用私有 `_validate_group`，统一经公开验证 seam；现有正式 PDF AST、腾讯双报告及 CI 坐标 fixture 回归保持通过。
- 已实现：质量门接受 compiler profile 的必需概念；非金融、银行、保险、证券/券商四类 profile 分离，专用 profile 不继承工业核心六项。初始研究与财报 retry 均经 compiler 重新决策，canonical validation 再投影到旧 storage 审计结构，研究事实仅保留合并口径/报告币种。
- 已实现：不完整但无结构性错误的候选字段以 `INCOMPLETE` 保留审计和确定性修复所需事实，`allow_ai` 保持 false；结构性冲突、勾稽和 provenance 失败仍全组隔离。
- 当前状态：`Acceptance: PENDING`；`User Test: PENDING`；`Upload Ready: NO`。完整跨市场矩阵、所有 service 判定迁移和打包仍未完成，不得据此标记发布通过。

## 当前实现记录（阶段三：研究目标视图）

- 已实现：`CompilerPolicy` 明确声明 `fiscal_period`（默认 FY）、`scope`（默认 consolidated）、`reporting_currency` 与可解析的 `period_range`；`compile_facts` 从所选 filings 构造实际期间范围。
- 已实现：`FinancialDataset.research_facts` / `research_validations` 只暴露目标期间、目标口径、目标币种且通过 profile 完整校验的事实；`resolved_facts` 继续保留结构校验通过的审计事实，但 INCOMPLETE、母公司或外币组不会进入研究/模型输入。
- 已实现：`allow_ai` 只由目标组验证结果决定，并要求每个目标 filing 都有完整 VERIFIED 组；非目标审计组不会阻断目标组，目标组任一年缺失则关闭模型准入。`equity` 与 `total_equity` 别名由 compiler 统一处理。
- 已实现：初始市场研究、市场 retry、SEC latest 与 SEC 初始研究均消费 canonical research view；service 不再自行以固定工业核心集合重判完整性。新增目标期间/范围、母公司/外币隔离、金融 profile 与 SEC 错口径定向回归。
- 已验证：compiler `18/18`、service `52/52` 定向测试通过，py_compile 通过；此前完整 Python suite `364/364` 通过。`git diff --check` 仍仅报告既有无关 `docs/releases/website-1.2.1.md:3` 尾随空格。
- 当前状态：`Acceptance: PENDING`；`User Test: PENDING`；`Upload Ready: NO`。完整跨市场矩阵、前端/Rust/打包与外部用户验收仍未完成，不得据此标记发布通过。

## 当前实现记录（阶段四：证据与报告一致性）

- 已实现：增长机会的支持/相反证据计数由已注册的确定性证据 ID 去重计算；模型伪造的计数字段和未知 ID 不参与非技术视图。双零时显示本地化的“未引用已验证证据”提示，技术视图仍可审计。
- 已实现：报告投影将资产负债表、情景和投资逻辑纳入必需章节；缺失章节使用三语可操作说明，不以空白、`—`、`None` 或 `unknown` 代替。阶段性回退报告从确定性财务指标生成资产负债表摘要。
- 已实现：报告/HTML 渲染保留增长模型的安全失败元数据，仅用于显示“模型未返回有效内容/可单独重试”说明；内部协议字段和证据 ID 不进入非技术输出。
- 已实现：研究综合/增长重试在创建 provider 前经 canonical `FinancialFactCompiler` 重新校验目标报告币种、合并口径、适用 profile 和快照摘要；不完整或陈旧快照以稳定质量错误关闭，模型调用次数为零。
- 已验证：报告与 HTML 定向测试 `40/40` 通过；service 定向测试 `52/52` 通过（提升权限以允许临时目录创建）；此前 compiler `18/18` 通过。默认沙箱临时目录权限导致的一次失败不代表业务断言失败。
- 当前状态：`Acceptance: PENDING`；`User Test: PENDING`；`Upload Ready: NO`。跨市场字段完整性矩阵、前端/Rust/打包和外部用户验收仍未完成，不得据此标记发布通过。

## 当前实现记录（阶段五：官方 Golden Corpus / Live Evaluator）

- 已新增 `tests/fixtures/official_financial_sources.json` 的
  `2.2.1-golden-corpus.v1` 契约：六家指定公司各保留 FY2021--FY2025
  记录，每期包含官方 URL、公告/accession、期间、币种、口径、单位、所需六项
  概念、摘录及 SHA-256；30/30 期均经独立官方来源确认并标记 `CONFIRMED`。
- 已新增 `tests/test_financial_golden_corpus.py`：重复键、五年连续性、摘录哈希、
  CONFIRMED 六项/page、未解析期隔离、完整候选经 canonical compiler 及冲突负例。
- 已新增只读 `scripts/evaluate-financial-corpus.py`：按公司/期间（支持 `--ticker`
  与精确 `--year`）逐项定位本机官方 PDF；每份解析在独立子进程内执行，120 秒后
  terminate/join，不残留超时线程。结果输出必需字段识别率、精确匹配、误识别率、
  冲突率、MinerU 触发计数/率、耗时及安全 `actual_facts`（仅概念、字符串数值、
  币种、单位、页码、口径、期间和 raw 摘录 hash，不含路径或原文）。不下载、不写
  数据库、不调用模型。未独立确认的期别即使解析完整也标记
  `EXPECTATION_UNRESOLVED`；SEC 无本地缓存时明确报 `SOURCE_UNAVAILABLE`，不伪造数值。
- 五年矩阵已完成：宁德时代、中芯国际、腾讯、美团、Apple、Microsoft 六家公司
  FY2021--FY2025 共 30 期、每期六项核心事实均有官方 provenance。A/H 20 期另将
  归母权益和合并总权益分开保存，并以严格等式验证 `资产-负债-合并总权益=0`；
  金标测试不得使用生产舍入容差掩盖错误。
- 当前状态：自动化验证已通过；`Acceptance: PENDING`；`User Test: PENDING`；
  `Upload Ready: NO`，等待用户测试便携包。

## 最终自动化验收（2026-08-29）

### Acceptance checklist and results

- [x] Canonical compiler、profile、候选冲突、研究目标视图和 AI 准入回归通过。
- [x] 腾讯 FY2021/FY2022 正式 PDF 相邻归属行六项核心事实回归通过。
- [x] 官方 golden corpus：6 家公司 × 5 个 FY = 30/30 `CONFIRMED`；摘录 SHA-256 可重算。
- [x] A/H 20/20 期双权益语义正确且精确资产负债勾稽为 0。
- [x] Python compileall 通过；完整 Python suite `402/402` 通过，耗时 `510.996s`。
- [x] Frontend Vitest `13` files / `67` tests 通过。
- [x] TypeScript `tsc -b` 通过；Vite production build 通过。
- [x] Rust/Tauri `33/33` tests 通过。
- [x] 无签名 Windows x64 portable 构建通过。
- [x] Release privacy：`ForbiddenDataEntries=0`，`CredentialOrPersonalDataMatches=0`。
- [x] Portable verification：必需文件齐全，`Windows GUI`，`SignatureMode=unsigned-test`，`SignatureStatus=NotSigned`。
- [x] Runtime verification：主程序与 sidecar 成功启动，`VisibleConsoleHosts=0`。
- [x] 用户在真实研究流程中完成最终测试，并于 2026-08-30 批准上传。

### Completed-change summary

已完成统一财务事实编译器、PDF 坐标 AST、分层缺口补全、跨市场 profile、唯一质量门、
研究/重试/报告接入、双权益语义、跨市场五年金标、增长证据与必需报告章节校验，并将
全部应用与包元数据统一为 2.2.1。字段不完整或结构冲突时保持可审计并阻止 AI；成功
来源不会因重试重复运行，MinerU 仍受用户授权与失败页边界约束。

### Files intended for publication

- 上述 `Relevant scope / files` 中的生产代码、桌面端代码、测试、官方 fixture、evaluator、
  版本元数据、打包/验证脚本与本验收记录。
- 不发布工作区临时目录、测试缓存、Cargo/Python 构建目录或本机数据目录。

### Release artifact

- Path: `D:\githubmax\installer-output\OpenThesis-2.2.1-windows-x64-portable.zip`
- SHA-256: `A425A8EACFBACA08C2403929622BE499171FA78E63BFF8453DB6B657D5D3E847`
- Build mode: `unsigned-test`
- Signature: `NotSigned`（符合本轮批准范围）

### Current state

- Acceptance: `PASS`
- User Test: `PASS`
- Upload Ready: `YES`
