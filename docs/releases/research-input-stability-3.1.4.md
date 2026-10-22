# 3.1.4：财务局部准入与中期比较修正

- Workflow：`research-input-stability`
- 日期：2026-10-21；目标版本：3.1.4。
- 状态：本地开发及正常测试包交付完成，等待独立用户测试；真实供应商指标仍暂缓、未验收。2026-10-21 用户明确要求“进行3.1.4开发”，批准本文件第 1–7 节既有方案；开发仅执行该范围，不自动调用收费模型或发布。

## 1. 本版目标

消除“可选指标有冲突，其他可靠财务数据也无法继续使用”的残留路径；让中期同比使用正确的流量比较期间。已验证成果持续可用，缺口限定到对应指标和计算。保留 3.1.3 启动、进度、请求预算和缓存改进，不全面重构、不新增付费兜底渠道。

研究质量不得下降。真实供应商全 run 输入加权缓存率目标仍为 ≥90%，且不低于同条件既定基线；不得靠填充公共前缀、重复请求或少做必要研究达标。此前用户暂缓的真实供应商验证仍为待验证，不自动启动。

## 2. 两份 QA 的独立裁决

已完整读取 `docs/qa/3.1.3qa.md` 与 `docs/qa/independent/3.1.3QA.md`，绑定相同 3.1.3 包 SHA-256：`9B17B0019B9FB0A04AB764D70F06C65F3610D24803095B42EF064A84A3EBA9D2`，Build ID：`c0b506e2cbe86dd5f8fdc8d7ea661bde42bdaa02cfd7067e0135977987a848b8`。

| 记录 | 可采纳证据 | 决策边界 |
|---|---|---|
| 常规 QA：冰轮环境、5 份年报、确定性本地提供方、PASS | 指定回归、解析及报告呈现结果 | 固定模型输出、顶层键数量和报告字数不能证明真实研究完成；其 PRODUCTION READY 不作为本版前提 |
| independent：中芯国际、10 份原件、209 接纳/72 隔离/116 审计、FAIL | 最新 H1 的 ROE 冲突、中期比较组完整性问题、财报重建 partial | 未调用研究模型。`FILING_DATA_QUALITY_PARTIAL` 是财报重建操作结果，不能单凭它认定正式研究入口被同一门禁阻断 |
| independent 附加的 MC-311-01 | 3.1.1 冻结包可选 usage=null 收存失败 | 属旧包。当前 `research_execution/responses.py:_normalize_usage` 已保留 nullable 计数，不重复修复；保持既有兼容回归 |

两份报告都记录前端 1 项失败，但原因归类不同；以实际断言差异核定，不预设为生产故障。Python 全量测试中途停止，不能推算最终失败数，也不能把测试进程内存增长直接当作应用内存泄漏。PDF worker 沙箱限制、缺 Cargo、测试桥缓存和原生隔离环境问题不混入已确认产品缺陷。

## 3. P0：统一“可用事实”与“完整财报”的边界

### 代码证据

- `financial_compiler.py` 的 latest interim、target 和 comparator 选择仍以组级 `VERIFIED` 配合完整 required profile 判断；最终 `allow_ai` 又要求所有目标组完整。
- `financial_admission.py:FinancialAdmission.admit` 已从安全事实视图派生 `model_financial_analysis`，允许局部降级；说明只修改 compiler 的布尔值不是完整解决方案。
- `service.py:_canonical_retry_snapshot` 仍因 `canonical.allow_ai == false` 返回财务质量失败；另有币种恢复等路径继续使用该旧标志。相同事实在不同入口可能得到不同可用性。
- `service.py` 财报重试任务把 partial 映射为 failed；这是操作状态，不应成为后续所有研究能力的替代判据。

### 实施决定

1. **复用现有 `FinancialResearchView` 作为研究消费入口。** compiler 保留事实编译和完整性诊断；`allow_ai` 若需兼容则明确仅为旧完整性摘要，不能再作为研究、重放、恢复、币种证明的统一开关。逐个审查现有读取点，不全局改为 true。
2. capability 由该计算/阶段实际需要的已接纳事实决定。可选 ROE 冲突只撤销该 ROE 及依赖它的结论；收入、现金流、资产等可靠事实仍可研究。缺核心值只限制依赖该值的计算，不能连带隐藏其他内容。
3. 事实状态和组完整性分别保存。`cohort_facts` 等装配不得因为组为 partial 将组内已验证事实降成未经验证，也不能把隔离事实抬成 verified。沿用原件、期间、币种、口径、单位和 provenance 的真实校验结果。
4. 历史恢复继续核对原研究快照和事实摘要；改用同一准入能力后，不能借机把当前新财报注入旧报告、绕过 snapshot digest 或复用已失效 proof。对不匹配快照给明确原因，保留旧正文。
5. 财报重建的任务终止、资料完整度、可继续研究能力分别呈现：例如“财报更新完成，ROE 待核验，可继续研究”。不得将其标成完整财报，不因局部 warning 自动循环重建，也不在用户只点“重试财报”时自动发模型请求。
6. 实体错配、原件损坏等按受影响来源隔离；只有当前研究身份或所有必要证据均无法可靠确定时才停止相应研究。诊断要带 fact/source/period/capability 作用范围，避免仅按错误字符串使全 run 失败。

**修改入口：** `financial_compiler.py`、`financial_admission.py`、`financial_contracts.py`、`financial_pipeline.py`、`service.py`；必要的前端财报恢复状态映射。按现有类型扩展，不建立第二套准入框架。

## 4. P1：ROE 识别从行列归属修复

QA 样本 accession `1225520297` 第 8 页出现 4 个 `reported_roe` 冲突候选。当前 `_merge_visual_rows` 和 ROE 专用提取分支已有多处跨行保护；尚不能仅凭候选冲突断定具体是哪条合并规则导致，禁止直接追加“中芯国际例外”。

**修改入口：** `financial_ingestion.py:_merge_visual_rows`、摘要页 ROE 提取及比较列分支、现有 ratio/期间绑定契约。

1. 开发先用已存该页 AST/候选回链确认每个值的行标签、bbox、列标题、数值类型及期间，区分错行、当期/同期、增减百分点和不同 ROE 定义。只对证据确认的路径修复。
2. 表格数字必须绑定自己的逻辑行与语义列。跨行修复保留真正换行的“加权平均净资产收益…率”，但不能吸收相邻 EPS、扣非指标或比较增减列。列的变化率/百分点不能当作 ROE 原值；不同定义不得压成同一事实身份。
3. 每个候选保留原始单元格定位和规范化依据。不能可靠区分时只保留为候选并提示该指标待核验；不挑第一个值、不平均冲突值、不让模型猜正确值。
4. 真正同口径同期间的重述冲突仍需隔离。`same_filing_restatement_conflict` 不得因可选指标政策而被整体删掉；记录实际冲突事实及受影响计算。

**最终效果：** 减少错误候选产生，同时保证即使 ROE 仍无法识别，其他可信研究能够继续。

## 5. P0：比较关系按指标和期间建立

**修改入口：** `financial_compiler.py` comparator 选择及完整性判断、`financial_profile.py`（按实际 profile 模块定位）、现有财务指标计算与期间契约。

1. 收入/利润/现金流属于 duration：H1 同比匹配上一年同起止月份的 H1，额外核对主体、合并范围、币种、单位和定义。Q1、Q3 累计和单季区间不能互换；不得仅凭年份与 fiscal_period 字符串认定可比。
2. 资产/负债/权益属于 instant：保留官方真实日期。2026 H1 资产负债表中的 2025-12-31 是上年末比较，不能改标为 2025 H1；若要算同比余额则必须有真正的 2025-06-30。
3. 比较组不再必须提供整套资产负债和流量字段。按指标匹配候选、形成 comparison binding；收入可比就生成收入同比，现金流没有可比值就仅该项缺失。不得以一个 `interim_comparator_complete` 布尔值替代所有指标状态。
4. 优先遵守当前披露的明确重述关系和权威来源选择；同文件比较列或独立同期原件均可提供依据。跨文件组合逐项留 lineage，不能混成看似来自一份完整报表的记录。真实口径冲突不自动择优掩盖。
5. 确定性计算、覆盖视图、模型阅读材料和报告展示消费同一比较绑定，避免修了 compiler 后 profile 仍套完整 profile。标清“同比”“较上年末”及所用期间，分母为零/负数沿用适当数学与解释约束。

**最终效果：** 不再要求 H1 流量比较组补出原件不存在的 H1 资产负债表；可计算的同比被保留，不制造跨期比较。

## 6. 历史修复、成本与缓存

- 有可用 AST 时仅重做相关行映射；compiler/准入问题仅重编译相关数据，不重下载、不全库清缓存。解析或事实契约变化须按受影响文档/转换版本失效，保留原件及审计历史。
- 新事实改变 proof 依赖时仅让相应证明失效，报告局部重投影不调用模型。新财务事实引发的研究更新需遵守已有授权与预算，不在打开历史报告时自动再生成。
- 不改不相关角色的公共前缀、模型路由和并发。真实证据变化必须形成新快照，不能为保持命中而发送旧数据；只失效受影响材料组，稳定材料编译结果继续复用。
- 沿用 3.1.3 全 run 缓存、输入、未命中、输出和费用联合指标。缓存率 ≥90% 且不回退仍为硬目标；本版未做真实供应商验证，不能称已实现。数据修复导致的新冷成本单列但不从总分母剔除。

## 7. 开发交付及验收要求

以下交给开发 Agent 实施后验证，本决策轮不执行额外测试。

| 场景 | 应交付结果 |
|---|---|
| 最新 H1 核心事实可用、仅 ROE 冲突 | 新研究、恢复、历史只读重放对同快照采用一致能力；可用正文/事实保留，ROE 及直接依赖待核验 |
| ROE 相邻 EPS、换行标签、增减百分点、扣非口径 | 数字归属正确；不确定者局部隔离；不新增公司特例 |
| H1 损益及现金流比较、上年末余额 | 分别按 duration/instant 绑定；可比流量不要求同期资产负债齐全；无跨期错配 |
| 核心事实真实错误或来源身份冲突 | 错误事实及依赖被隔离，不能被放宽可选字段规则放行 |
| 财报重建 partial | 可见具体缺口和剩余能力；无自动重建风暴、无隐式收费模型调用 |
| 历史报告修复 | 原件/旧候选不被覆盖；未变化事实 proof 保留，变化部分失效；本地修复零模型调用 |
| 性能/缓存 | 保留 3.1.3 最小启动、轻量进度与稳定前缀；不重复全库迁移，不全量缓存失效；真实供应商指标仍按授权边界待验收 |
| 现有失败断言 | 核对 `ModelCenterView.test.tsx` 的 nullable 字段差异和旧逐句警告断言；更新契约时同时保留交互行为、来源定位、partial/非伪完成断言，不仅删除失败检查 |

先交付统一准入和比较绑定，再修已定位的 ROE 提取路径及局部历史恢复。开发记录只追加具体修改、必要回归与未覆盖边界；不以 mock 的字段齐全或本地字节减少宣称真实质量、费用或缓存通过。

## 8. 批准状态

具体方案：**APPROVED — 2026-10-21，用户“进行3.1.4开发”**。开发 Agent 在本文件继续记录实施；不创建另一份交接文档。QA 原文保持只读，不自动发布、不自动发起收费研究。

## 9. 开发状态及范围核对

| 字段 | 状态 |
|---|---|
| Workflow | `research-input-stability` |
| Target version | `3.1.4` |
| Acceptance | PASS |
| User Test | PASS |
| Upload Ready | YES |
| Publication | PENDING |
| Record updated | 2026-10-22 12:44 Asia/Shanghai |

`Acceptance: PASS` 指本地开发回归及本节绑定正常包的开发者检查通过；真实供应商费用/缓存指标和真实模型研究质量不在本轮宣称范围。R9 延续用户暂缓真实调用的指示，保持 PENDING，不以本地或离线模拟替代。用户依照既有约定明确要求上传并表示全部同意，该上传请求即作为本包用户检验完成和发布授权；据此记录 `User Test: PASS`、`Upload Ready: YES`。

| 范围 | 影响 | 依据 |
|---|---|---|
| 生产行为 | YES | 财务准入、比较关系、ROE 解析及恢复状态 |
| 依赖 | NO | 复用既有财务契约及测试工具，不新增依赖 |
| 打包/版本 | YES | 版本升级及正常测试包 |
| 用户流程 | YES | 局部缺口下研究继续、财报重建状态 |
| 仅元数据 | NO | 涉及生产实现；使用 STANDARD 路径 |

执行顺序：先建立真实缺陷回归，再统一准入与逐指标比较，定位并修复 ROE，复核历史快照边界，执行必要开发检查并正常打包。真实供应商费用/缓存对照延续此前暂缓；不将其记录为通过。独立 QA 原文及隔离原库保持只读。

## 10. 实施与开发检查（2026-10-22）

本节 `T` 指 `tmp/research-input-stability-3.1.4/`。主 Agent 负责准入/比较/历史快照和最终审查；唯一委派上下文 `lunahigh_314_roe` 为项目配置的 GPT-6 Luna xhigh，仅执行已明确的 ROE 局部实现、前端检查及普通打包，不承担产品决策，不再派生子 Agent。未修改独立 QA 原文，不调用 Antigravity，不做真实供应商研究。

### 10.1 修改说明与源头定位

1. `financial_compiler.py` 将已通过 validator 的单元格状态与 cohort 完整度分离：缺项或隔离的兄弟字段不再将其降为 PARTIAL。隔离单元格不会被提升；`allow_ai` 保留旧完整性含义。`FinancialAdmission`、service 历史恢复和币种恢复使用同一研究能力，不再拿完整性总开关当研究准入；Recognition 增加 PARTIAL，区分可用与完整。
2. `financial_comparatives.py:ComparisonBinding` 统一逐指标比较。流量核对真实起止日、期间形状、主体/口径/币种/定义；instant 只按真实日期绑定同比或上年末。H1 流量比较组可只含已验证流量；独立同期原件和当前文件比较列都保留 lineage，真正重述冲突仍禁用对应计算。profile、确定性指标、覆盖/模型材料与报告共用绑定，不新增第二套事实准入。
3. 真实 accession `1225520297` 第 8 页证明 EPS→单位碎片→ROE 被错误并行：`_merge_visual_rows` 原先仅检查最后碎片是否有数值。现在检查累计片段并保留行边界；摘要列按明确当前/同期/增减表头绑定。扣非 ROE 独立为 `reported_roe_excluding_nonrecurring`，不混入普通 ROE；validator 同时检查比例单位、标签语义，EPS/扣非不能冒充普通 ROE。原件截图与候选溯源见 `T/roe/trace.md`。
4. 保持 `PDF_PARSER_VERSION=financial-ingestion-ast-v33`。共享 `ROE_ROW_MAPPING_VERSION=roe-row-v2` 仅使含旧 ROE 候选的解析缓存和相关 checkpoint window 失效；无 ROE 的旧缓存/窗口继续命中。确有受影响旧 window 时，旧 exhausted fence 不封锁修正后的本地重试；当前版本仍保留同输入穷尽限制。原件/旧 checkpoint 不删除，不重新下载或清全库缓存；旧缓存没有持久 AST，不伪称重用了不存在的 AST。
5. 财报重建结果增加 `research_can_continue`、capabilities、scoped_issues；可用 partial 且投影成功的任务为 completed，资料状态仍 partial。报告刷新失败、代次冲突、无可用资料仍明确失败。UI 三语解释缺口与剩余能力，显示局部指标及阶段状态，不因 warning 自动循环、不隐式调用研究模型。
6. 历史恢复继续校验原 fact-ID 摘要；新版快照再绑定金额/日期/口径/单位/原件证据的语义摘要。只排除准入展示状态，改金额或换原件不能因 ID 不变复用。显式财务刷新原子更新快照；旧报告/证明保留，未修改 proof 重放契约。更新已有失败断言时保留来源位置、partial、不可计算与非伪完成检查：不删除这些安全断言。
7. 打包前最后复核确认 `_healthy_accessions` 可以在“核心字段完整”时提前跳过旧 ROE 映射。已暂停构建，先通过 `test_healthy_core_cannot_short_circuit_pending_local_roe_mapping` 跑出准确 red，再将该短路接到 pipeline/engine 的只读 `pending_local_mappings` 查询；只看本文件哈希/配置对应的旧缓存，不扫描全库。待重映射标记更新后恢复健康缓存短路。修复后相关 9 模块 **104 passed**（`T/cache-bypass-green.log`）；此前 pre-snapshot 因源码变更失效并保留为 pre-closure 记录，没有用旧源码开始构建。

### 10.2 逐项证据

环境：Windows x64，Python 3.12.8；Node v24.15.0；React/Vitest 使用项目锁定版本。时间：2026-10-21 至 2026-10-22（Asia/Shanghai）。`PYTHONPATH=src`，pytest `--basetemp` 均指定本工作流隔离目录。命令范围及完整结果见日志；重叠套件不相加。

| ID | 批准要求 | 操作与结果 | 证据 | 状态 |
|---|---|---|---|---|
| R1 | 可选冲突仅局部影响，新研究/恢复统一能力 | `pytest tests/test_financial_partial_admission_314.py`：最初 4 个真实症状红；修复后覆盖可选 ROE 冲突、部分核心字段缺失、同快照恢复与摘要拒绝 | 初次命令 `4 failed in 8.38s`；`T/final-backend-r3.log` 261 passed；`T/fixtures-final.log` 56 passed | PASS（本地） |
| R2 | ROE 错行、换行、同比原值/百分点、扣非口径 | `pytest tests/test_roe_row_binding_314.py`：最小错行先红后绿，实际原页四个值分离；再经实际 compiler/admission 接纳 | `T/roe/trace.md`；`T/frozen-replay-remapped-r2.log`：普通 0.029/0.015、扣非 0.020/0.013；均非 EPS 或 pp；子任务最终 3 passed | PASS |
| R3 | duration/instant 精确绑定，比较不要求完整同期资产负债 | 独立 H1 流量、同文件比较、错误币种/口径/起止日、上年末日期、报告三语比较标签 | `tests/test_financial_partial_admission_314.py`、`test_financial_comparison_display_314.py`；上述 261 项及 `T/display-final.log` 30 passed | PASS |
| R4 | 核心错误/身份冲突仍隔离，诊断有作用范围 | 真实冲突不平均、不取首值；损坏来源不封锁有效兄弟来源；研究身份冲突仍 blocked，比较单位冲突只隔离相应比较事实 | `test_financial_compiler.py`、`test_financial_partial_admission_314.py`；`T/final-backend-r3.log` | PASS（指定范围） |
| R5 | partial 重建可见具体缺口与能力，无伪完成/隐式模型 | job 状态、前端 promise、三语状态、旧 result 可选字段回归；刷新/CAS 失败仍失败 | 后端 261 项；`T/frontend-final-r2.log` 66 passed + build；旧结果 optional-chain 单项 red/green 日志 | PASS |
| R6 | 历史快照/proof 边界、本地修复零模型 | 同 ID 改语义摘要仍拒绝；历史原文/原 proof 保留；只读重编译 QA 原候选和单页本地重映射 | `test_currency_snapshot_recovery.py`、`test_historical_proof_replay_312.py`、`test_report_revision_contexts.py`；`T/frozen-replay-remapped-r2.log`，model_calls=0，原库 mode=ro | PASS（本地） |
| R7 | 保留启动/轻量进度/稳定材料，不全库失效 | 3.1.3 startup/progress/source_compiler/request_plan 回归包含在 261 项；局部 cache/window/exhausted 失效用例 | `T/final-backend-r3.log`；`T/display-final.log`；ROE 3 项回归；未改 storage 迁移、并发、模型路由、公共 prompt | PASS（本地保留检查） |
| R8 | 修正旧失败断言但保留行为/安全边界 | nullable context_window_hint、章节级待核提示、UTF-8 估算非安全上界；分析正文可见但不准入计算/不伪验证；夹具改为真实年度/半年起止日 | `ModelCenterView.test.tsx`、`test_analysis_presentation_policy.py`、`test_reporting.py`、`test_financial_compiler.py`；前端 66 项、后端 261 项 | PASS |
| R9 | 真实供应商输入/费用/全 run 缓存目标 | 延续用户“暂时跳过真实模型验证”；没有供应商 token/usage，不能宣称输入降幅或 ≥90% 命中达标 | USER DEFERRED / REAL PENDING；不得替换为 mock 或字节数证明 | PENDING（授权暂缓，非通过） |

实际原件回放：读取相同 10 份原件的 404 个已保存候选，只重映射上述 ROE 页后，295 条研究事实接纳、68 条隔离，`model_ready=true / research_ready=true`，H1 收入/利润/经营现金流比较可用；`legacy_complete=false / readiness=partial`，其他真实重述冲突保留。它证明本版本地修复与能力，不代表最终 AI 研报或独立 QA PASS。

### 10.3 验证中的环境与契约差异

- 初始 Python 缺 PYTHONPATH 导致 collection 错误，设为 src 后跑出 4 个准确红信号；没有把导入错误当产品故障。
- 默认沙箱 `%TEMP%` 的 Vitest 缓存 rename EPERM 阻止测试启动，正式 `require_escalated` 宿主运行后通过；未调整 ACL、防护或生产约束。两次未完成的默认沙箱多进程检查由本轮停止，不能算 PASS；后续宿主有界 23 模块 261 项完成。子任务额外旧临时目录 PermissionError 记录为环境阻断，不计入通过数。
- 第一轮扩大回归发现旧断言仍要求“保守 UTF-8 安全上界”和删除未核验正文；按当前已批准的精确估算与可见但不可计算契约更新，并保留具体安全检查。另有旧财务夹具把两年区间标成 FY/H1，修正真实起止日，而不是放宽期间验证。
- 最终 23 个后端模块命令逐项列在 `T/final-backend-r3.log` 对应执行记录（261 passed，27.23s）；其后报告零/负基数说明与共享 marker 收口检查 `T/display-final.log`（30 passed，1.49s），重叠结果不相加。前端 4 个指定文件 66 passed，build 通过，保留已有 >500KB chunk 提示。没有执行全量独立 QA 或真实供应商测试。
- 健康缓存短路的后补修复使相关旧证据失效，已用 `T/cache-bypass-green.log` 对受影响 9 模块重跑（104 passed，11.99s）；其他未变路径的 261 项记录保留。旧结果载荷确实可重现 `updated_artifacts.length` 空值错误，optional-chain 修复及独立回归后通过，未只删除断言。

可复现命令（均使用上文环境，以下 stdout/stderr 分别记录到对应日志）：

```powershell
$env:PYTHONPATH='D:/githubmax/src'
& 'C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe' -m pytest tests/test_financial_partial_admission_314.py tests/test_financial_comparison_display_314.py tests/test_roe_checkpoint_mapping_314.py tests/test_roe_row_binding_314.py tests/test_financial_compiler.py tests/test_financial_checkpoint.py tests/test_domain_and_financials.py tests/test_financial_admission.py tests/test_financial_pipeline.py tests/test_financial_recognition.py tests/test_financial_repair_use_case.py tests/test_official_financial_preparation.py tests/test_official_financial_service_repair.py tests/test_currency_snapshot_recovery.py tests/test_reporting.py tests/test_report_html.py tests/test_historical_proof_replay_312.py tests/test_report_revision_contexts.py tests/test_optional_usage_receipt_312.py tests/test_startup_read_model_313.py tests/test_progress_read_model_313.py tests/test_source_compiler_313.py tests/test_request_plan_313.py -q --basetemp=tmp/research-input-stability-3.1.4/pytest-final-backend-r3 --tb=short
& 'C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe' -m pytest tests/test_financial_partial_admission_314.py tests/test_roe_row_binding_314.py tests/test_roe_checkpoint_mapping_314.py tests/test_financial_repair_use_case.py tests/test_official_financial_preparation.py tests/test_official_financial_service_repair.py tests/test_financial_pipeline.py tests/test_financial_comparison_display_314.py tests/test_reporting.py -q --basetemp=tmp/research-input-stability-3.1.4/pytest-cache-bypass-green --tb=short
& 'C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe' tmp/research-input-stability-3.1.4/replay_frozen_financials.py --remap-roe
# cwd=desktop
npm run test -- --run src/features/report/ReportWorkspace.test.tsx src/features/models/ModelCenterView.test.tsx src/app/useWorkbenchSession.test.tsx src/i18n.test.ts
npm run build
```

### 10.4 文件责任与拟交付范围

- 后端：`financial_{compiler,comparatives,admission,contracts,pipeline,profile,validation,ingestion,checkpoint,recognition}.py`、`financials.py`、`service.py`、`reporting.py`、`application/{financials,financial_repair,financial_refresh,research_snapshot}.py`。
- 前端：`app/useWorkbenchSession.ts`、`features/report/ReportWorkspace.tsx`、`types.ts`、`i18n.ts` 及对应测试；`ModelCenterView.test.tsx` 仅校正 nullable 契约。
- 新回归：`tests/test_financial_partial_admission_314.py`、`test_roe_row_binding_314.py`、`test_roe_checkpoint_mapping_314.py`、`test_financial_comparison_display_314.py`。已有相关测试按本节说明校正；11 份版本元数据统一 3.1.4，第三方依赖不变。
- 本权威记录与实际输入清单。`tmp/` 为开发证据，不自动提交；QA 原文、根目录既有候选/提示文件和其他既有脏工作树修改不自动纳入提交。GitHub 发布需独立授权。

## 11. 正常测试包与交付（2026-10-22）

- Base commit：`7b46cccff1cd27bb1e82899b141f5c480c1430e5`；保留现有脏工作树，不 reset/commit/push。
- 源码输入规则沿用 `scripts/generate-release-snapshot.py` 的 TREES/ROOT_FILES/ROOT_SUFFIXES/included；确定排序，包含 changed/untracked 构建输入、类型/模式/链接目标/内容 hash 与删除项；仅沿用生成输出、缓存和非构建验收元数据的排除。
- 正常包命令：`scripts/package-desktop.ps1 -SignatureMode unsigned-test -SkipPrivacyVerification`。按既有用户要求不重复隐私扫描，不省略脚本内建 runtime/版本/便携包检查；成功产物不重复构建。
- 正式命令由 `lunahigh_314_roe` 通过宿主 PowerShell、`require_escalated` 执行；一次构建成功，退出码 0，没有成功后重建。环境沿用 Python 3.12、Node v24.15.0、D 盘 Rust/Cargo/VS 工具链；日志 `T/package-final.log`。
- 正式 ZIP：`installer-output/OpenThesis-3.1.4-windows-x64-portable.zip`，**56,418,794 bytes**。
- ZIP SHA-256：`E8CD4F77D96D72A8EEB278E0BB5CE23F733ADCF54812E035AE44C0EB9D9D9469`；同名 `.zip.sha256` 一致，主 Agent 于 12:44 独立 `Get-FileHash -Algorithm SHA256` 复核。
- 包内实际身份（主 Agent 直接读取 ZIP 的 `OpenThesis-3.1.4/bin/openthesis-sidecar/_internal/openthesis/resources/build-info.json`）：version `3.1.4`，commit 同上，contract `2.0`，build time `2026-10-22T04:38:59Z`，build ID `acd1be5a7f6dd1f0f1e8c2b9d76c89ce9d26976f86787f666f57112b99f22134`。

### 11.1 输入冻结与修复历史

- 正式清单：`docs/releases/research-input-stability-3.1.4-inputs.jsonl.gz`，45,532 条，SHA-256 `10B2707C712D6FB354B7C63FA7336C4E02ADA0AA21DEB209781FBE06F6CADD72`。
- 最终构建前清单：`T/research-input-stability-3.1.4-inputs-pre-package.jsonl.gz`，45,530 条，SHA-256 `67EAE874D06F577F4236180A751E5860E87DA81F6AA2DF9EF68503A3D2D6F719`。
- 被健康缓存短路修复作废的旧清单保留为 `T/research-input-stability-3.1.4-inputs-pre-closure.jsonl.gz`，SHA-256 `5AB28D329E743F9168123DE077DC11C16F29E0279281AFA783E2E1B71EFF8068`；未从该旧状态启动构建，没有对应旧产物冒充本包。
- 构建前运行 `python scripts/generate-release-snapshot.py 3.1.4 research-input-stability`；构建后从本包取得实际 build-info，再运行相同命令附加 `--generated-build-info tmp/research-input-stability-3.1.4/package-build-info.json`。生成结果见 `T/post-manifest-final.log`。
- 主 Agent 逐 path 对比压缩清单：**仅新增两条本次实际生成的 build-info，没有删除、其他内容/模式变化或 tsbuildinfo 变化**。两条身份输入各 241 bytes，内容 SHA-256 `BE6CF3B3FAA7D3EC6A36616AAC399CD1415C75C8A0AD18C4C7AAB22BDFBD557E`。原开发占位符不是实际生成输入；正式清单绑定本包身份而非占位文件。
- 精确范围：`TREES=(src, desktop, build_support, scripts, .build-tools, .cargo)`；根 `README.md`、`LICENSE` 及根扩展名 `.py/.spec/.toml/.txt/.json/.lock/.yml/.yaml`。排除 `__pycache__/.git/*.pyc/*.pyo`、`desktop/dist`、`desktop/src-tauri/{target,gen,resources/bin}`、`desktop/node_modules/{.vite,.cache}`，两个 build-info 开发占位符由实际生成字节单列。此规则未 blanket 排除构建依赖或脏/未跟踪源码。

### 11.2 包内必要检查

下列检查由正常打包脚本调用既有验证脚本完成，不是 Antigravity 或真实研究验收。环境为 Windows x64，时间为 2026-10-22 12:38–12:44（Asia/Shanghai），输出统一在 `T/package-final.log`，主 Agent 已复核结果。

| 检查 | 命令/操作 | 实际结果 | 状态 |
|---|---|---|---|
| 启动及 sidecar | `scripts/verify-desktop-runtime.ps1 -Executable <本次 staging 的 OpenThesis.exe>`，脚本内 3 次隔离目录启动 | 3/3 PASS，2.840 / 2.480 / 2.626 秒 | PASS |
| 运行版本/契约 | 上述启动探针检查 `system.hello.app_version`、`build_info.version` 和 `contract_version`；主 Agent 另读 ZIP 身份 | `3.1.4 / 3.1.4 / 2.0`；非仅检查源码版本。没有以此声称完成 GUI 全量视觉审查 | PASS（探针范围） |
| 持久报告主流程 | 同脚本 `Invoke-PersistentReportReadSmoke`，写入/读取/重启再读隔离离线报告 | run `d4e5b5b4ee3c4a86bd8c01ffbea26095`，research state `completed`，read state `partial`，可见 substantive 章节 3，restart stable `true` | PASS（离线持久化，不代表真实研究完整） |
| 便携包结构 | `scripts/verify-desktop-portable.ps1 -Version 3.1.4 -CargoTarget D:/OpenThesisToolchain/cargo-target/openthesis -SignatureMode unsigned-test` | Windows GUI subsystem、required entries、checksum 一致 | PASS |
| 签名 | 同一便携包验证 | `NotSigned`，符合已选择的 `unsigned-test`；不宣称签名/信誉已建立 | PASS（预期测试模式） |

构建保留已有前端 >500KB chunk 和 Rust unused-function/linker 提示，无构建错误。本次按用户约定跳过重复隐私扫描，不把它记录为新扫描通过。开发者检查及本包交付完成：`Acceptance: PASS / User Test: PENDING / Upload Ready: NO / Publication: PENDING`。

真实模型及费用/缓存对照仍暂缓；正常测试包交付不能当作这些目标或独立用户测试通过。仅交付上述精确 SHA 的包供用户独立测试，未 commit/push、未创建 GitHub Release，也未自动调用 QA。

### 11.3 用户发布授权（2026-10-22）

- 用户明确要求按照既有约定上传3.1.4并表示“我已经全部同意”。依照用户已反复确认的流程，该上传请求表示用户已完成本包检验并批准源码上传、合并及Release发布。
- R9真实供应商费用/缓存对照仍是用户暂缓项，不触发收费调用，也不改写为通过；不影响用户对当前3.1.4测试包的检验确认及发布授权。
- 本阶段操作前状态：`Acceptance: PASS / User Test: PASS / Upload Ready: YES / Publication: PENDING`。
