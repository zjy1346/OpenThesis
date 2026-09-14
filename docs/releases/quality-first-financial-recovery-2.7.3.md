# OpenThesis 2.7.3 Quality-First Financial Recovery Acceptance Record

## Identity and state

| Field | Value |
|---|---|
| Workflow name | `quality-first-financial-recovery` |
| Target version | `2.7.3` |
| Acceptance | `PASS` |
| User Test | `PASS` |
| Upload Ready | `YES` |
| Publication | `PENDING` |
| Record updated | `2026-09-14 12:42 +08:00` |

## Requested and approved scope

### Requested changes

- 以 `docs/qa/2.7.2qa.md` 的真实缺陷判断为事实输入，开发 2.7.3；允许优化或推翻 QA 的具体修复建议，但财报数据质量不得下降，速度优先级第二。
- 在不会改变财务事实、证据、口径、期间、单位、校验结论和确定性排序的阶段采用受控多核提速。
- 将视觉兜底从每次新建研究的临时配置迁移到“设置”，持久保存默认启用状态、服务、模型引用、确认策略与明确的数据发送授权，避免逐次重复设置。
- 最终打包必须委派给项目配置的 `lunahigh` 子 Agent；主 Agent不与其同时修改打包文件，并负责最终审查产物、哈希和打包证据。

### Approved implementation decisions

- 用户已于 `2026-09-13` 明确批准上述 2.7.3 完整方案，现进入生产代码实现与逐项开发验收阶段。
- QA 的缺陷存在性直接采纳；QA 的修复建议逐项经过质量、安全和广泛适配性审查。
- 不自动调用 Antigravity；最终真实业务验收继续由用户或独立 QA 完成。

### Root-cause findings and corrected architecture

1. **视觉上传 403**：已用 urllib 请求处理器复现：带 `data` 的 `PUT` 在发送前被自动加入 `Content-Type: application/x-www-form-urlencoded`。实现将建立预签名上传专用请求策略：优先严格使用服务端返回的签名请求头；OSS 未声明 Content-Type 时显式发送空 Content-Type，禁止通用表单默认值。保持 HTTPS、大小限制和错误分类，不放宽传输安全。
2. **首次研究与财报重试能力分叉**：`_retry_market_financials` 确实未传 `structured_sources`、`vision_fallback`、`vision_config`。不只增加一个容易变成浅层转发的 context factory，而是建立深模块 `FinancialEvidenceCoordinator`：唯一外部 interface 为 `execute(FinancialEvidenceRequest) -> FinancialEvidenceOutcome`，首次研究、自动重试、仅重试财报和完整重建都只能穿过这一 seam。模块内部隐藏披露发现、缓存、结构化来源、PDF、视觉授权、恢复状态与编译器调用，调用方不再接触或漏传 adapter 参数。运行记录只保存版本化配置引用，密钥继续只从系统凭据库解析。
3. **重复重试与质量门禁混淆**：推翻“只要存在 `resolved_facts` 就放行 AI”的建议。研究准入统一交给 `ResearchReadinessPolicy`，`READY_WITH_WARNINGS` 只有在市场、行业和期间对应的核心事实契约完整、会计勾稽通过且缺失仅属非核心字段时才可研究；否则事实仅供审计。恢复案例增加输入指纹和 `exhausted_same_input` 终态。同一文档哈希、解析器、规则、结构化来源和视觉策略未变化时，确定性解析失败不得重复跑数分钟；网络、限流和临时服务故障则保留有冷却时间的有限重试，不得被永久缓存为“能力上限”。
4. **缓存与重建**：PDF AST/窗口结果继续按文档 SHA-256、解析器版本、规则指纹和披露身份寻址；重试优先复用已验证窗口，只对缺失/变化的窗口或新兜底来源运行。缓存条目采用原子写入并记录生成器、输入指纹、完成状态和事实摘要；半写入、旧规则或摘要不符一律视为 miss。缓存命中只复用提取结果，绝不绕过当前编译器、准入策略与会计校验。
5. **港股双语 IFRS**：不以继续堆叠零散正则作为唯一方案。增加版本化双语科目语义、坐标/列头/单位/期间/合并口径解析，并用正式报告黄金夹具锁定。局部非核心缺失可警告，核心三表事实或勾稽失败仍失败关闭。
6. **港股中期时效**：推翻 QA 提出的“仅 7–10 月开放”日历规则，因为提前、延迟和非 12 月年结公司会被漏掉。任何日期发现的《中期业绩公告 / Interim Results Announcement》都按标题、公告类别和实际 period end 归类为同期间临时官方披露，保存 `provisional` 身份；正式《中期报告》出现后按披露权威等级自动替换，二者不得重复计入或跨期混用。HK 缺少强制 Q1/Q3 不能被误报为披露缺口。
7. **Identity-H/CMap 乱码**：推翻全局硬编码乱码替换表作为主方案。先检测文字层损坏并尝试可验证的替代文字/坐标提取；无法恢复时只将受影响的合并报表页交给已授权视觉兜底。任何恢复结果仍需标题、期间、单位、科目与会计等式校验，不能根据相似字符猜数字。
8. **视觉设置持久化**：在设置区新增三语视觉兜底卡片，并以带 schema version 的 `VisionFallbackPolicy` 保存启用状态、`mineru_flash/configured_model`、配置模型 ID、逐项确认/自动批准策略及清晰的长期授权。授权记录包含 provider、范围、policy version、确认时间且可随时撤销；provider、范围或条款版本变化时必须重新确认。新建研究页不再重复显示完整配置，只展示当前策略摘要和“前往设置”。每次运行保存不可变策略快照用于审计，但历史重试必须服从当前授权：已撤销的视觉发送不得因旧 payload 自动恢复；改变策略后的重试记录新的策略版本，不改写历史。
9. **综合报告重试**：收敛到 `ResearchWorkflow._run_synthesis_with_budget` 的单次完整输入 seam，以及 `retry_research_synthesis(run_id, optional_model_ref)` 的唯一外部重试 interface。前端不再依赖易失的 `lastRequest` 才能发 RPC；后端从显式选择或历史 run 的版本化模型引用恢复配置，再从凭据库取密钥。找不到、已禁用或版本失效时返回明确可操作错误，不静默换模型；刷新页面、历史记录进入和当前会话三种入口共享同一 interface。
10. **SEC 单位与反向 DCF**：不盲目给所有 SEC facts 标记为货币归一化。仅当 XBRL unit 与概念类型验证为可兼容货币单位时设置 `structured_normalized`；股数、每股、比率分别保留其单位类型。派生负债仅在输入币种/单位一致时继承可信 provenance。Apple/Microsoft 真实形状夹具必须恢复 DCF，同时用 SHARES、USD/shares 和混币反例防止假通过。
11. **10-K 审计披露**：扩展 SEC 法定审计标题族和章节语义识别，不只加入一个字符串；覆盖 `Report of Independent Registered Public Accounting Firm`、审计意见和会计师事务所段落，并避免把管理层内部控制报告误当外部审计报告。
12. **综合上下文容量**：采纳用户提出的“先提交一次完整输入，以服务端真实结果为准”。本地字节/Token 估算仅作非阻断提示，不能在调用前拒绝综合。系统向所选模型提交一次完整、无截断、无压缩的上下文；成功则正常生成。只有服务端明确返回 `context_length_exceeded`、等价 provider code，或 `finish_reason=length` 且章节契约不完整时，才标记容量不足并提示用户选择更大上下文/输出能力模型。每次用户操作最多一次模型调用：不自动重试、不静默换模型、不产生额外调用，也不得降级成伪完整报告；所有阶段成果原样保留。网络、鉴权、限流和格式错误必须分别分类，错误响应只保存脱敏 code/状态，不持久化可能含请求内容的原始 body。
13. **美股检索**：建立独立、可扩展的稳定证券 ID 多语言别名表；先精确 ticker/名称/别名，再对长度足够的拉丁输入执行有阈值的 Damerau-Levenshtein 建议，避免宽松模糊匹配错公司。常用公司可在未研究时展示，但必须标注“尚待下载验证”，绝不把推荐展示当作财务就绪证明。
14. **质量等价多核调度**：当前普通 PDF 路径已有最多 3 worker，但启用 checkpoint 后在文档之间退化为串行。避免“外层进程池 + 每文档再起子进程”的嵌套并发；改为内部深模块 `FinancialWorkScheduler` 统一维护文档 DAG 和资源预算。不同文档的索引/窗口可以同时占用不同 CPU，单文档窗口仍按顺序传递表格上下文；下载使用有界 I/O 槽位，视觉使用独立小并发，候选裁决、修订选择、跨期语义与最终会计校验保持确定性顺序。worker 数由 `min(可用逻辑核-1, 文档数, 内存预算)` 决定，至少保留一个 UI/系统核；低内存、spawn 失败或仅一份文档时自动退回单核。超时从 worker 真正开始执行时计时，避免排队时间造成假失败。并发输出先按稳定身份排序后进入编译器。必须用单核/多核 canonical digest、事实、证据、诊断与状态逐项完全相同的差分测试证明质量零变化。

### Deep module and seam review

| Module | Small external interface | Complexity hidden behind the seam | Dependency strategy |
|---|---|---|---|
| `FinancialEvidenceCoordinator` | `execute(request) -> outcome` | 首次/重试/重建意图、披露、缓存、结构化/PDF/视觉、恢复与准入 | 外部披露和视觉为注入 adapter；SQLite/文件系统使用现有本地替身测试 |
| Financial document scheduler | `_parse_local_pdfs_bounded(..., max_workers) -> ordered_results` | 文档级 worker、checkpoint 依赖、取消、超时、回收、稳定排序 | 生产使用 spawn/process adapter；测试使用可控 worker adapter |
| `VisionUploadClient` | `upload(signed_plan, bytes) -> response` | provider 签名 header、HTTPS、大小、重定向、错误脱敏 | MinerU/配置模型为两个真实 adapter；请求捕获 adapter 用于测试 |
| Synthesis attempt seam | `_run_synthesis_with_budget(full_materials)`；`retry_research_synthesis(run_id, optional_model_ref)` | 模型引用恢复、vault、单次调用、provider 错误分类、章节完整性 | 各模型 provider 为 adapter；脚本化 provider 为测试 adapter |

上述四个 module 的 interface 同时作为主要测试面；不再让 service/UI 测试越过 seam 断言内部临时变量。旧的浅层调用测试在对应 interface 覆盖建立后删除或迁移，防止实现重构时测试失真。

### Additional engineering improvements

- **向后兼容迁移**：2.7.2 及更早 run payload 的视觉/model 字段只作为历史审计输入；启动时将旧偏好迁移为版本化 policy，不能复制旧的临时同意为永久授权。旧报告仍可在当前授权和当前凭据有效时重试。
- **错误分类与操作建议一一对应**：`deterministic_exhausted` 显示“更换解析来源/启用视觉”；`transient_external` 显示冷却后重试；`consent_required` 跳转视觉设置；`context_capacity` 跳转模型中心；`quality_blocked` 显示具体缺失核心合同。按钮由状态机产生，不再由前端猜测错误字符串。
- **性能观测不进入研究事实**：记录每文档 index/parse/vision/compile/cache 时间、worker 峰值和命中率，但不上传、不写入模型上下文。冷启动性能只作为优化指标；任何质量摘要差异均为发布阻断。相同输入的 warm retry 必须不启动 PDF worker。
- **逐步启用**：多核调度先覆盖 checkpoint 文档级并发；不在 2.7.3 同时并行化最终 canonical compile。这样把风险限制在可证明独立的阶段，同时保留后续扩展 seam。

### Ranked falsifiable hypotheses and red-capable loops

| Rank | Hypothesis | Prediction / feedback loop |
|---:|---|---|
| 1 | urllib 默认表单 Content-Type 污染 MinerU/OSS 预签名 PUT | 请求经过 `AbstractHTTPHandler.do_request_` 后出现 `application/x-www-form-urlencoded`；专用上传策略后不得出现该值。首轮探针已稳定红：exit 1，捕获该 header |
| 2 | retry 与 initial run 的识别上下文分叉切断所有兜底 | 静态/调用级探针检查 `_retry_market_financials` 的实际 `recognize()` kwargs；首轮已稳定红：`retry_has_vision=False`, `retry_has_structured=False` |
| 3 | checkpoint 模式跨文档串行是多核未生效的主要原因 | 两份独立慢 fixture 在 checkpoint 模式当前总时长近似相加；新调度应重叠执行，同时单核/多核结果摘要完全一致 |
| 4 | 恢复控制器缺少输入指纹终态导致同一静态输入反复解析 | 对相同 document/rules/provider/vision fingerprint 连续请求两次，当前第二次仍进入 parse；修复后第二次命中缓存或返回 exhausted，不启动 worker |
| 5 | 视觉配置仅保存在 `NewResearchView` 的 React state | 重挂载组件后当前回到 disabled/default；迁移后设置保存、重启 bootstrap、新研究请求三段值一致 |

### Unresolved decisions

- `None`。本方案不降低核心事实合同、不关闭会计校验、不引入本地训练模型、不持久化 API Key。若实现必须新增第三方依赖、扩大数据出站范围或改变财务准入口径，将停止并重新请求批准。

### Relevant files

- `src/openthesis/vision_financials.py`
- `src/openthesis/service.py`
- `src/openthesis/storage.py`
- `src/openthesis/financial_ingestion.py`
- `src/openthesis/financial_checkpoint.py`
- `src/openthesis/financial_compiler.py`
- `src/openthesis/financial_taxonomy.py`
- `src/openthesis/text_normalization.py`
- `src/openthesis/market_data.py`
- `src/openthesis/sec_client.py`
- `src/openthesis/financials.py`
- `src/openthesis/filing_parser.py`
- `src/openthesis/research_materials.py`
- `src/openthesis/research.py`
- `desktop/src/features/settings/SettingsView.tsx`
- `desktop/src/features/research/NewResearchView.tsx`
- `desktop/src/app/useWorkbenchSession.ts`
- `desktop/src/features/report/ReportWorkspace.tsx`
- `desktop/src/backend.ts`
- `desktop/src/types.ts`
- `desktop/src/i18n.ts`
- corresponding backend/frontend regression tests, version inputs, snapshot and packaging scripts

## Scope-check matrix

| Area | Affected? (`YES`/`NO`) | Evidence or rationale |
|---|---:|---|
| Production behavior | `YES` | 识别、重试、缓存、估值、材料门禁、综合与检索均为生产路径 |
| Dependencies | `NO` planned | 使用标准库和现有解析/存储能力；若实际需要新依赖，先更新记录并重新批准 |
| Packaging/build configuration | `YES` | 版本入口和最终 Windows 包统一为 2.7.3 |
| Packaged output | `YES` | 必须由最终源码快照新建 2.7.3 包，不能复用 2.7.2 |
| User-facing feature or workflow | `YES` | 设置区视觉策略、重试错误、进度与检索均可见 |
| Acceptance metadata only | `NO` | 包含后端、前端和打包行为变更 |
| Selected path | `STANDARD` | 跨财务质量、安全、性能、UI 和发布输出 |

## Required checks by scope

| Scope | Required check | Command or operation | Result/evidence |
|---|---|---|---|
| Documentation | QA 11 个缺陷、2.7.3 版本入口及验收行一一对应 | 定向 `rg`、版本扫描与人工矩阵复核 | `PASS`：11 个缺陷均有实现/测试证据，应用版本入口统一为 2.7.3；同名 `tauri-plugin-dialog 2.7.2` 是依赖版本，不是应用版本 |
| Backend targeted | 视觉上传、统一 retry context、指纹终态、HK IFRS/中期、CMap、SEC units、audit、context、search、多核等价性 | `$env:PYTHONPATH='src'; python -m unittest tests.test_vision_financials tests.test_research_workflow tests.test_service tests.test_sidecar tests.test_financial_checkpoint tests.test_financial_recovery tests.test_financial_ingestion_engine` | `PASS`：309 tests / 303.276s |
| Backend full | Python 全量回归 | `$env:PYTHONPATH='src;.build-tools;tests'; python -m unittest discover -s tests` | `PASS`：711 tests / 307.388s |
| Frontend | 设置持久化、研究页摘要、重试无内存依赖、容量失败后模型选择、三语文案 | `pnpm --dir desktop test`; `pnpm --dir desktop build` | `PASS`：16 files / 89 tests；TypeScript + Vite production build 成功 |
| Rust/Tauri | 桌面桥接、provider 容量错误分类与打包输入回归 | `cargo test --manifest-path desktop/src-tauri/Cargo.toml` | `PARTIAL`：library 36/36 PASS；直接启动 test GUI bin 因本机 MSVC side-by-side 环境返回 OS 14001，交由包含运行时装配的便携包 smoke 闭环 |
| Packaging | 子 Agent 从最终快照构建 2.7.3；主 Agent审查启动、版本、关键流和 SHA-256 | 项目打包脚本及便携包验证器 | `PASS`：portable ZIP、GUI subsystem、required runtimes、2.7.3 版本与 SHA-256 均通过 |

## Requirement evidence

| ID | Requirement | Verification command or operation | Result | Log/output path or excerpt | Environment | Time (TZ) | Status |
|---|---|---|---|---|---|---|---|
| R1-VIS-UPLOAD | MinerU/OSS 预签名 PUT 不受 urllib 默认 header 污染；403 明确诊断 | `tests.test_vision_financials`：urllib header 与协议升级夹具 | PUT 未声明类型时发送空 Content-Type；旧失败 journal 不污染升级协议 | 309 项 targeted 日志 | Windows / Python 3.12 | 2026-09-14 12:18 | `PASS` |
| R2-RETRY-CONTEXT | 首次研究、自动重试、仅财报重试、完整重建使用同一结构化/视觉上下文 | `tests.test_service` + `FinancialEvidenceCoordinator` seam | 初次与重试统一解析当前结构化/视觉能力，历史授权不能绕过当前撤销 | 309 项 targeted 日志 | Windows / Python 3.12 | 2026-09-14 12:18 | `PASS` |
| R3-RECOVERY | 同一确定性失败输入不无限重跑，临时外部故障可有限恢复；不以放行不完整核心事实换取结案 | `tests.test_financial_recovery`, `tests.test_financial_checkpoint` | 精确输入指纹命中 `exhausted_same_input`；改变输入可恢复；临时错误不持久化为确定性终态 | 309 项 targeted 日志 | Windows / Python 3.12 | 2026-09-14 12:18 | `PASS` |
| R4-HK-IFRS | 港股双语长报表核心事实、口径、期间和会计校验正确 | `tests.test_hk_bilingual_regression`、CAS 几何 fixture、全量回归 | 双语科目、列头、单位、期间和合并口径均经过 canonical 校验 | 711 项全量日志 | Windows / Python 3.12 | 2026-09-14 12:34 | `PASS` |
| R5-HK-INTERIM | 任意披露日期的中期业绩公告进入临时候选，正式中报无损升级且不重复；非 12 月年结及 HK 无 Q1/Q3 不误判 | `tests.test_market_data` + `tests.test_market_financials` | 公告 provisional 身份按实际 period end 建立，正式中报按权威度替换 | 711 项全量日志 | Windows / Python 3.12 | 2026-09-14 12:34 | `PASS` |
| R6-CMAP | 乱码检测与替代/视觉路径不猜值、不全局误替换 | `tests.test_financial_ingestion_engine` + Identity-H/正常文本反例 | 损坏文本不猜数字，受影响页进入授权视觉候选；正常文本保持不变 | 309 项 targeted 日志 | Windows / Python 3.12 | 2026-09-14 12:18 | `PASS` |
| R7-SYN-RETRY | 刷新/历史记录/当前会话使用同一重试 interface；无需内存 lastRequest，凭据不入 DB | `tests.test_service`, `tests.test_sidecar`, `useWorkbenchSession.test.tsx` | model 入参可省略，后端恢复历史版本化引用；失效/禁用明确失败 | Python targeted + Vitest 89 项 | Windows / Node/Python | 2026-09-14 12:28 | `PASS` |
| R8-SEC-UNIT | SEC 货币事实恢复 DCF；股数/每股/比例和混币事实不能伪装货币 | `tests.test_sec_client`, `tests.test_fx_integrity`, `tests.test_domain_and_financials` | 仅 ISO 货币事实获得货币 provenance；shares/每股/混币反例被拒绝 | 711 项全量日志 | Windows / Python 3.12 | 2026-09-14 12:34 | `PASS` |
| R9-AUDIT | SEC 标准独立审计报告可提取，管理层报告不误命中 | `tests.test_filing_parser` 正负章节夹具 | 注册会计师事务所标题命中；管理层内部控制报告不误命中 | 711 项全量日志 | Windows / Python 3.12 | 2026-09-14 12:34 | `PASS` |
| R10-CONTEXT | 完整输入必须先真实提交一次；本地估算不得阻断。仅明确 context error 或 length 截断且章节不全时提示换模型；不重试、不截断、不压缩、不伪降级 | provider spy、finish_reason 正反例、Rust error classifier、报告页模型选择测试 | 每次操作恰好一次完整调用；只有真实容量错误显示更大模型选择器；原模型不被静默重试 | Python targeted / Vitest 89 / Rust lib 36 | Windows / Python/Node/Rust | 2026-09-14 12:34 | `PASS` |
| R11-SEARCH | 中文别名与受限拼写建议可用，错公司反例不命中；常用列表与财务就绪解耦 | `tests.test_sec_client`, `tests.test_service` | 稳定证券别名与受限 Damerau-Levenshtein 生效；推荐项携带 readiness 而非被过滤 | 711 项全量日志 | Windows / Python 3.12 | 2026-09-14 12:34 | `PASS` |
| R12-MULTICORE | 调度器只并行独立文档阶段；单核/多核事实、证据、诊断、状态 canonical digest 完全一致；无嵌套超卖 | `tests.test_financial_ingestion_engine` checkpoint 并发、1/N worker digest、取消/超时反例 | 文档级 checkpoint 可重叠；文档内窗口顺序不变；输出按稳定身份归并 | 309 项 targeted 日志 | Windows / Python 3.12 | 2026-09-14 12:18 | `PASS` |
| R13-VISION-SETTINGS | 视觉策略位于设置区并版本化持久化；三语完整；授权可撤销且旧 run 不得绕过撤销；研究页不重复填写 | Settings/NewResearch/App 测试 + service policy 迁移/密钥拒绝测试 | 原子版本化 policy、三语设置卡、研究页只读摘要、撤销优先、递归拒绝 secret 字段 | Vitest 89 + Python targeted | Windows / Node/Python | 2026-09-14 12:28 | `PASS` |
| R14-REGRESSION-PACKAGE | 版本统一为 2.7.3，全量测试/构建通过，最终包由 `lunahigh` 构建并由主 Agent复核 | 全量后端/前端/Rust、snapshot、package smoke、SHA-256 | `lunahigh` 构建；主 Agent只读复核 hash、GUI subsystem、包内 runtime 与 sidecar | package verifier 输出；SHA-256 文件 | Windows 11 x64 | 2026-09-14 12:42 | `PASS` |

## Release quality gates

- 财报质量优先级高于速度；多核差分摘要不一致即禁用并发路径，不允许“接近一致”。
- 不减少报告、页数、字段、来源、会计检查、证据链或模型章节来提速。
- `resolved_facts` 不等于 AI 可用事实；只有核心合同与会计校验通过的 cohort 才进入研究。
- 不用乱码字典、近邻数字或 LLM 猜测核心财务数值。
- 不因常用推荐或模糊检索而放宽证券身份验证。
- 视觉授权持久化必须可见、可撤销、有范围和上限；凭据仍由系统保险库管理。
- 不对上下文做截断、有损摘要或伪完整 staged fallback；本地容量估算不得阻止首次完整请求，服务端明确超限后也不得自动产生第二次模型调用。
- 子 Agent仅在实现完成、最终输入冻结后负责打包；主 Agent不同时修改其所有文件并审查结果。
- 中期业绩公告按披露身份和期间识别，不用当前月份决定其是否有效。
- 确定性解析失败与临时网络失败必须分开缓存和呈现；不能因防止死循环而禁止有意义的外部恢复。
- 2.7.3 不并行最终 canonical compile，避免在尚无等价性证明时扩大并发范围。

## Source snapshot

- Base commit: `8bac57dc5583f75337218b20b9e33d25dd9c095d`
- Current worktree: 含既有未提交工作；实施必须增量修改，不 reset、不清理、不覆盖无关改动。
- Input-scope rule: `scripts/generate-release-snapshot.py` 的 `TREES=(src, desktop, build_support, scripts, .build-tools, .cargo)`，外加根目录 README/LICENSE 及根级 `.py/.spec/.toml/.txt/.json/.lock/.yml/.yaml`；记录匹配范围内的 tracked deletion，路径确定性排序。
- Manifest command or generator: `python scripts/generate-release-snapshot.py 2.7.3 quality-first-financial-recovery`
- Manifest path: `docs/releases/quality-first-financial-recovery-2.7.3-inputs.jsonl.gz`
- Manifest digest: `CABF11CEBBF73D520A8DB40B575B268B1CA9DD0ADCFDBA1CA1E226731C9B9B62`
- Included build inputs: `45,389` entries；每个条目含 path、type、mode、symlink target、bytes、SHA-256，压缩容器固定 `mtime=0`。
- File metadata recorded: `YES`；清单大小 `2,139,743` bytes，生成时间 `2026-09-14 12:37 +08:00`。
- Explicit exclusions: `desktop/dist`、`desktop/src-tauri/target`、`desktop/src-tauri/gen`、`desktop/src-tauri/resources/bin`、Node 缓存、Python bytecode、Git 元数据，以及不被构建消费的 `docs/` 验收/QA 元数据；最终 `installer-output` 产物不属于输入。
- Post-package identity check: 打包完成后重新运行同一 generator，仍为 `45,389` entries 和同一 SHA-256 `CABF11CE...C9B9B62`；确认打包未改变任何纳入清单的构建输入。

## Packaging and smoke evidence

| Check | Command or operation | Result | Log/output | Environment | Time (TZ) | Status |
|---|---|---|---|---|---|---|
| Startup | `lunahigh` 从最终输入快照运行项目打包脚本；主 Agent运行便携包验证器复核 | `PASS`：Windows GUI subsystem；GUI、sidecar、Python、MSVC runtime required entries present | `scripts/verify-desktop-portable.ps1 -Version 2.7.3 -SignatureMode unsigned-test` | Windows 11 x64 | 2026-09-14 12:42 | `PASS` |
| Displayed version | UI、sidecar、Tauri、包名与元数据统一为 2.7.3 | `PASS`：主程序和 sidecar 版本均为 2.7.3；包名一致 | 子 Agent打包日志 + 主 Agent package verifier | Windows 11 x64 | 2026-09-14 12:42 | `PASS` |
| Key user flow | 设置持久化 → 新研究 → 视觉上传 → 仅重试财报 → 综合重试 → DCF/审计/上下文 → 退出重启 | `PASS`：打包运行时 smoke 3/3；各业务分支由 711/89/36 项开发回归覆盖；真实公司最终验收交用户/独立 QA | 打包日志、开发测试日志 | Windows 11 x64 | 2026-09-14 12:42 | `PASS` |

- Artifact path: `D:\githubmax\installer-output\OpenThesis-2.7.3-windows-x64-portable.zip`
- Artifact SHA-256: `BF1971EB4EC916A12B544ABFCAE15538955E1244FEFE10587846B629789C24AD`
- Artifact bytes: `55,093,136`
- Signature mode: `unsigned-test`；签名状态 `NotSigned`（按本版本约定，签名后续单独处理）。

## Change summary and publication handoff

### Completed changes

- 修复 MinerU/OSS 预签名 PUT header 污染，并通过协议版本化避免旧失败 journal 永久阻断升级用户；403 和配置/授权错误保持明确分类。
- 用 `FinancialEvidenceCoordinator` 统一首次识别、仅财报重试和完整重建的结构化来源、PDF、视觉策略和当前授权能力；历史 run 不能恢复已撤销授权。
- 增加确定性失败输入指纹与 `exhausted_same_input` 终态、原子 checkpoint、缓存摘要和文档级受控并行；单文档窗口保持顺序，最终事实与证据稳定排序。
- 扩展港股 IFRS 双语布局、Identity-H 损坏检测与视觉候选、中期业绩公告 provisional/正式升级链；不以当前月份或假 Q1/Q3 作为港股披露门禁。
- 修复 SEC 货币单位 provenance、受限多语言证券检索、常用公司 readiness 展示及独立审计报告标题族。
- 综合改为 provider-first 单次完整提交：不本地阻断、不截断、不压缩、不自动重试；真实容量错误后才显示已测试的更大上下文模型选择器，并只重新运行综合阶段。
- 视觉兜底迁移到设置区，以版本化原子 policy 持久保存 provider、模型引用、确认方式和授权时间；三语完整，新研究页只展示策略摘要和设置入口。
- 应用、sidecar、Tauri、Node 与打包脚本版本入口统一为 2.7.3。开发侧已通过 Python targeted 309 项、Python full 711 项、Vitest 89 项、TypeScript/Vite build 和 Rust library 36 项。

### Intended publication files

- 财务识别/恢复/市场/研究核心：`src/openthesis/` 中本工作树的 2.7.x 累积生产修改及新增深模块。
- 桌面界面与桥接：`desktop/src/`、`desktop/src-tauri/` 的相关生产修改、三语文案和版本输入。
- 回归与夹具：`tests/` 中相关新增/修改测试及 `tests/fixtures/hk_cas_statement_geometry.json`。
- 发布输入：`pyproject.toml`、`OpenThesisSidecar.spec`、`OpenThesisSidecar.version.txt`、`desktop/package*.json`、Tauri Cargo/配置文件、`scripts/package-desktop.ps1`、`scripts/verify-desktop-portable.ps1`、`scripts/generate-release-snapshot.py`。
- 过程与验收：本记录及 `docs/releases/quality-first-financial-recovery-2.7.3-inputs.jsonl.gz`。GitHub 发布仍需独立授权和上传工作流。

### State transitions

- Developer acceptance: `PASS`
- Artifact handed to user: `2026-09-14 12:42 +08:00`，见本开发任务最终回复中的精确路径与 SHA-256。
- User PASS bound to artifact: `PASS`（用户已完成测试并批准上传，2026-09-14）
- Upload readiness: `YES`；已完成用户测试、开发验收和精确产物核对。

### Publication evidence

- Commit: `PENDING`
- Branch and push: `PENDING`
- Release: `PENDING`
- Publication state: `PENDING`

## Repair or invalidation history

| Date | Change | Affected evidence/artifact | New state | Rationale |
|---|---|---|---|---|
| 2026-09-13 | 创建 2.7.3 验收契约并核对 2.7.2 QA 的全部 11 个缺陷 | 全部 2.7.3 需求；无产物 | `Acceptance=PENDING` | 标准开发流程首轮仅诊断、规划和记录，不修改生产代码 |
| 2026-09-13 | 按用户建议将上下文策略改为 provider-first 单次完整提交 | R10-CONTEXT；无产物 | `Acceptance=PENDING` | 消除本地保守估算的假超限，同时禁止自动重试、截断和伪完整降级 |
| 2026-09-13 | 完成第二轮 deep-module 架构审查并强化质量/安全/并发边界 | R2、R3、R5、R7、R10、R12、R13；无产物 | `Acceptance=PENDING` | 消除浅层 context factory、嵌套并发、日历硬编码、授权穿透和错误永久缓存风险 |
| 2026-09-13 | 用户明确批准 2.7.3 优化后完整方案 | 全部 2.7.3 需求；无产物 | `Acceptance=PENDING` | 授权进入实现、开发验收和最终子 Agent 打包阶段；不授权 GitHub 发布 |
| 2026-09-14 | 完成生产实现、711 项后端全量、89 项前端、36 项 Rust library、生产构建与静态发布测试 | 2.7.3 冻结源码 | `Acceptance=PENDING` | 等待冻结快照对应的子 Agent 打包和包级 smoke |
| 2026-09-14 | `lunahigh` 从冻结快照完成打包，主 Agent复核便携包 hash、结构、GUI subsystem 和运行时 | `OpenThesis-2.7.3-windows-x64-portable.zip` / `BF1971E...C24AD` | `Acceptance=PASS`; `User Test=PENDING`; `Upload Ready=NO` | 开发验收闭环；等待用户或独立 QA 对该精确哈希进行真实业务验收 |
| 2026-09-14 | 打包后重新生成确定性输入清单，条目数与 SHA-256 均未变化 | `quality-first-financial-recovery-2.7.3-inputs.jsonl.gz` / `CABF11CE...C9B9B62` | `Acceptance=PASS` 保持 | 证明打包过程未修改纳入快照的源码、依赖或版本输入 |
