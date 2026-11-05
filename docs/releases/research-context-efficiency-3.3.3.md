# 3.3.3：恢复调用收敛与大材料规划性能

- 日期：2026-11-03；工作流：`research-context-efficiency`。
- 状态：用户已于2026-11-05 明确确认3.3.3验收完成并批准上传发布；QA报告仅作辅助。真实模型调用和额外外发材料仍未获授权。

## 1. 发布判断：3.3.2 暂不正式发布

本轮重算 ZIP SHA-256 为 `F4D4C4B5653DD1FC3A793C2256187D10C98D02A1D939629809734539A232F7F2`。直接读取包内 build-info：build ID `5ade14d87e0449c9a9a332622600cacb20970e955cd1b022a9bf4619b180c28b`，base commit `530f7a0138dcfccf518dc9bc213be20da1278ce1`。

已阅读两份 3.3.2 QA 及开发记录/QA 分类账。判断如下：

| 证据 | 独立判断与处理 |
| --- | --- |
| 主 QA 宣称 PASS，独立 QA 宣称 FAIL | 不按票数决定。引用解析和前端 mock 修复可以保留；两份均使用模拟模型，不能证明真实模型完整研报、费用和缓存收益。 |
| 主 QA 的同 SHA ZIP 写另一组 build ID/commit | 与本轮包内读取不符；发布说明必须纠正身份，不能沿用其“全项验收通过”。不要改写独立 QA 原文，增加复核说明。 |
| 17 次 synthesis 补全调用、恢复 9→12 次等 | QA 为包内模拟调用，未造成实际账单；代码 `_recover_synthesis_output_sections` 确实逐 missing section 调用。属于必须优化的调用放大路径；其他计数差异仍需按用途和输入变化逐项判断。 |
| 大型 CMB context 约 122MB、repair observations 64.7MB，规划约五分钟 CPU | 来自主 QA 的观测，未在本轮复测；`production.py` 多次 compact_json/loads 和按阶段重复 digest 支持重复规划工作风险。不能只列成无关紧要的建议。 |
| pending claim/无来源数字仍出现在报告 | 保留待核验内容符合已批准产品原则，不能以“存在于 safe/claims”直接断言污染计算。但 pending 是否影响 proof/readiness/计算必须核实；角色标签不能授予事实可信度。 |
| 非干净 commit 构建 | 开发记录明确 base commit + 冻结输入清单，不能说完全没有来源。现有包内 commit 字段单独不足以复现；发布需可恢复源码快照及唯一内容身份。 |
| 并行最大值为 1、缺财务标签、取消返回 partial child | 有具体 QA 证据；需修复或给契约证明，不能全归为旧测试。并行根因尚未定位，不直接假定 scheduler 设置错误。 |

因此本版不是仅缺一次发布操作；需要代码修正及针对性验证。不能用新的次数/token 硬上限代替治理，也不因换版本号宣称问题已解决。

## 2. P0：补全按语义工作组执行，停止逐章节机械放大

**入口**：`research.py::_recover_synthesis_output_sections`、既有 `repair_context.py`、request journal、`analysis_references.py`。

1. 首先复用原响应中完整章节及上游 `analysis_refs`；本地装配能够完成的内容不发模型请求。完成判定同时检查内容、依赖和义务，不仅检查字段存在。
2. 缺失章节先形成持久化工作清单：章节 ID、输入/来源版本、现有候选、缺失原因、依赖。将共享证据和上游论证的章节合成一次定向请求，复用现有 `sectional_delivery.sections` 数组协议；不能每章必发一次，也不能全篇重新生成。
3. 依据实际输入闭包、路由上下文和输出能力安排分组；容量不明时使用现有估算规划并明确估算身份，不增加固定章节数量/总调用次数上限。确实收到 length 才将未完成组进一步分解，已完成项立即持久化。
4. 路由拒绝、鉴权或提交未知继续沿用 route hold，停止自动发其他组。连续空结果/同类格式故障且没有新材料或策略改变时标为可恢复；“下一个章节”不能自动成为继续同类无效尝试的理由。
5. 去重键包含任务、目标章节集合、材料/候选版本、模型及策略；每次重启从已完成工作项重建缺口，不重发收到且保存的结果。不能靠内存标志去重。
6. 诊断逐请求记录用途、目标、新增成果、复用成果和未完成原因。验收比较有效信息增量，不把测试要求的 2 次、6 次、17 次写成运行时硬限制。

**效果**：能合并的章节一次补全，原有成果不重写；拆分仅用于真实容量或内容边界，重启不重复收费。

## 3. P0：生成身份与核验身份分开，修复无变化恢复的重复调用

**入口**：`research_execution/executor.py::stage_input_digest`、`production.py` 的 stage inputs/invalidated 判定、request journal 和 checkpoint 存储。

- 当前 digest 包含依赖 validation state、issue codes 与 trusted_output；核验状态变化可能传播为下游输入变化。先导出失败恢复的字段级 digest 差异，明确到底是正文、证据、核验状态、路由还是运行元数据变化。
- 生成身份覆盖模型实际看见的材料、提示词、输出契约和模型配置；核验身份覆盖候选及校验策略/来源版本。只有核验发生变化而模型可见内容未变时，重验并更新投影，不重调 producer。
- 模型确实看见的来源状态改变不得从 key 隐藏。依据依赖图只失效受影响阶段；显式换模型只影响用户选择的恢复范围及必要下游，不重跑独立已完成阶段。
- 原始响应已持久化、publication 或本地核验失败时先重用响应。拒绝将缺日志当成可安全重发的依据，保留 remote unknown 的明确状态。

**效果**：同材料同模型恢复零新增 generation；必要重算有具体依赖变化说明，保持此前已保存的报告可读。

## 4. P1：原件纠错正文按需装载，冻结上下文只编译一次

**入口**：`ResearchContext.compact_json`、`research_execution/production.py`、`request_planning.py`、`source_compiler.py`、现有 repair material 路由及存储。

- 一次运行冻结一份规范上下文/材料身份。各阶段共享只读视图；不要在同一规划中反复 compact_json→loads→dumps，更不能为九阶段重复 hash 相同的大对象。memo/cache 绑定材料版本，不跨变更误复用。
- 将纠错观察的重复原文存入现有本地材料存储，以原件 hash、页/窗口、观察 ID 定位。研究主上下文携带索引和问题元数据，角色需要纠错时装入必要原行、表头、单位、前后文及冲突候选。
- 优先复用既有 observation/source registry，不新增一个通用缓存系统。存储引用在本地解析后才可加入请求；不能把数据库 ID 发给模型并假设它能读取。
- 不能用摘要替代唯一证据，不能按条数丢 observations；每项仍可追溯、可恢复，拥有明确负责阶段。其他定性阶段不重复携带整份纠错快照。
- 来源版本不变时固定 wire 前缀保持稳定；新诊断、运行时间、修复状态放尾部。缓存命中和本地编译复用分开统计。
- 对相同 12 份 CMB 材料记录 read/parse/route/encode/hash/plan 耗时与峰值内存。将规划 CPU 相对已记录约五分钟减少至少 80% 作为待验证工程目标，未达到需解释瓶颈；这是验收目标，不是运行超时门禁。

## 5. P1：待核验仍可阅读，但可信度不能由字段名决定

**入口**：`research_section_validation.py`、proof/readiness 消费端、报告渲染、财务计算输入。

- 保留原叙述和局部警告，不采纳“未核验就从报告清空”的处理。章节级汇总提示与可展开证据状态即可，不恢复每句话下重复提示。
- 分开 presentation candidate、verified assertion、declared assumption 的用途。`opinion/question/methodology/summary` 等标签不能将无来源历史数字升级为 verified 或支撑确定性估值。
- 核查 `accept()` 生成的 proof 节点、pending claim 列表是否被下游当成已验证；如果只是可读待核验且不进入计算，则更新旧删除式断言。如果出现状态提升，修状态传播和消费者，不整章删正文。
- 同句含假设和历史数值时分别标记来源状态；不因出现阿拉伯数字就整句拒绝。无效 supporting/contradicting reference 记录局部异常、保留原始候选；不得伪造合法引用或将坏引用作为支持。

**验收**：用 QA 的无来源数字例子检查最终 UI 状态、proof/readiness 和计算输入，而非只断言字符串有没有出现在 safe 中。可读不等于核验完成。

## 6. P1：财报恢复状态与概念映射闭合

- `financial_checkpoint.py` 的 `results.extend(partial_children)` 与“完整父窗口”合同存在冲突。采用明确的完整结果/部分进度分流：子窗口仍持久化并供下次恢复复用，父窗口只有覆盖齐全且上下文连续时标 complete。所有调用方必须按状态与覆盖范围消费。
- 已完成子窗口的独立、来源完整事实可走现有验证链；依赖缺失表头/单位/跨页续表的事实保持待补，不得按完整父窗口计算。取消必须尽快返回取消状态，不能把 partial 静默当成功，也不能清空已存结果。
- 在 `financial_ingestion.py` 的 prepass 标签映射补齐 `reported_roe_excluding_nonrecurring`，对齐 `financial_contracts.py`，区分普通 ROE、扣非 ROE、加权平均及期间，不得猜值或用普通 ROE 顶替。

## 7. 并发差异：先定位排队层，再改调度

QA 单测观察最大并发 1；当前 `ResearchWorkflow` 已用 shared scheduler limit=2，production 也按 parallel_agents 设置 max_workers。因此不能直接认定“忘了开线程”。

开发记录 stage ready/start、executor 等待、scheduler active/limit/cooldown、provider dispatch 时间。检查共享 scheduler 先前实例、限流状态、图依赖与缓存组调度。仅修错误继承的串行配置或不必要依赖；真实 429 cooldown 与服务商限制仍有效。无容量限制且两独立基础阶段就绪时应重叠执行；跨公司仍保留后台 FIFO，不扩大为多公司同时收费。

## 8. 发布来源和验收合同

- 保留 3.3.2 原 ZIP、QA 与输入清单。补充更正主 QA 的包身份，不改其原始证据；报告由模拟器产生就明确标模拟产物，不称真实模型语义通过。
- 3.3.3 打包前冻结可恢复源码快照或干净隔离 checkout，记录 base commit、dirty 状态、源内容 hash、依赖锁定、构建输入清单、build ID、ZIP SHA 的映射。原用户脏工作区不 reset、不批量提交。
- 快照 hash 不能自引用：区分预构建源码输入与构建生成的 build-info，最终 manifest 单独绑定包。发布源码应与受测包对应；现有冻结清单可作为证据，不能只改 commit 字符串假装干净构建。
- 先在源代码和精确候选包验证补全、恢复、状态隔离、取消与编排的具体场景；复用既有冻结材料，不为验收盲目重新联网抓取。原生 GUI 的后台任务、报告阅读/恢复操作必须有候选包证据。
- 调用数应按语义工作项和真实新增信息验收；上下文容量按配置/计数/服务商拒绝分类，拒绝恢复旧任意硬预算。全量红项按当前合同分类，不能仅因旧版同红关闭。
- 缓存目标仍为真实供应商输入命中 90% 以上，缺 usage 写 UNKNOWN，同时报告总输入、未命中输入、输出和耗时；免费模拟不得替代费用/缓存验收。真实调用需用户授权。

## 9. 开发交付与批准

顺序：恢复调用与身份 → 大材料规划 → 证据状态/财务取消边界 → 并发定位 → 精确包验证与发布来源绑定。优先修现有函数与合同，不另造通用兜底框架。

初始判断为3.3.2发布 HOLD；3.3.3完成实质修复后，按用户2026-11-05的明确验收与发布决定执行。

- Decision approval: APPROVED — 2026-11-04，用户“进行3.3.3开发”。
- Acceptance: PASS — 用户于2026-11-05确认验收完成。
- User Test: PASS — 按用户既有约定，“上传并发布”表示已完成测试；QA报告为辅助证据。
- Upload Ready: YES
- Publication: PENDING
- Artifact / SHA-256: `installer-output/OpenThesis-3.3.3-windows-x64-portable.zip` / `54B99152322E831CA18A13EC963E37388CD8A735BF5C8DAF5E74659F3DC5BEFF`

## 10. 开发证据（2026-11-04，北京时间）

本轮按标准流程：后端生产行为、报告呈现和打包版本受影响；没有新增运行时依赖、没有启用新服务商、没有改变财务事实准入、跨公司 FIFO 或显式费用上限。所有研究和报告回归仅使用本地数据与模拟提供商。

| 项目 | 根因与完成内容 | 当前证据 |
| --- | --- | --- |
| R1 合并补全 | 旧代码无条件按章节循环；改为从现有章节及义务路径建立缺口、合并共享来源请求、保存工作清单和结果收据。容量或实际输出截断只细分剩余目标；拒绝、未知结果和无增量停止自动扩散。 | `tests/test_capacity_completion_311.py`：同一截断样例旧 17 次、新 2 次；多段截断只补剩余组；重新打开 journal 不增加物理请求。 |
| R2 恢复身份 | 外层核验问题不等于生成变化。SQLite 对对象键排序曾改变证据节点遍历及可选缺口顺序；使用稳定的节点/缺口顺序，同时保留模型可见的证据级状态。现行生成身份排除纯展示/核验策略，旧检查点须精确匹配才能复用。 | `test_base_parallel_311.py` 的模拟 429 后同材料恢复；`test_research_generation_identity_333.py` 区分本地策略变化与实际证据升级。诊断记录变更类别、不导出正文。 |
| R3 规划性能 | 每图冻结一次上下文与观测索引；原始纠错记录保存在既有确定性财务快照，按命中目标交给对应角色，保留原件和反证。按字段形状建索引消除观测×目标的逐对匹配；复用来源编译和计量。 | `scripts/profile-research-planning.py`，只读打开 `D:/test_qa_332/live_run/openthesis.db`；286 事实、3,804 条含派生记录的证据、3,498 个观测。相同输入下旧包 300.00 秒→新代码 40.27 秒 CPU（−86.58%），峰值工作集 1,108,381,696→461,127,680 字节。九阶段规划字节数和计划完全相同；全树／前缀差异复核另见下文。 |
| R4 证据用途 | 将展示、核验、计算资格绑定到独立 proof；未证实数字仍可读但不履行义务、不参加计算，坏引用不充当支持。历史数值和前瞻假设同句时分别标注，并共用章节级提示。 | `tests/test_local_coverage_proof.py`、`tests/test_report_verification_notes_311.py`、`tests/test_trusted_emergency_content.py` 覆盖历史证据兼容、三语 HTML/Markdown 和计算资格。 |
| R5 财报 | 子窗口只写断点和进度，完整父窗口须等待所有依赖；明确区分取消和时间片。补齐扣非 ROE 预解析标签，不用普通 ROE 推值。 | `tests/test_gap_work_orders_325.py`、`tests/test_financial_prepass_concept_labels.py`。 |
| R6 并发 | 官方已分配原件的独立阅读 lane 曾被旧共享前缀家族锁串行；按实际 lane 放行并记录 ready/queue/provider dispatch。共享 provider 限流不变。 | `tests/test_base_parallel_311.py`：关闭工作流先创建共享 scheduler，再开启时两调用重叠；429 后仍降并发并冷却。 |
| R7 交付 | 版本同步、冻结可恢复源码、包前后清单和实际 ZIP/SHA 绑定；用户已于2026-11-05明确确认验收完成并批准发布。 | 用户验收 PASS；QA报告仅作辅助。 |

开发检查：Windows／Python 3.12；`PYTHONPATH=src`、TEMP/TMP 与 pytest basetemp 隔离在工作区。2026-11-04 执行 `pytest -q --tb=short --basetemp tmp/333-final-regression-r1-20261104` 加本版生成身份、并发、容量、证据、引用、财务断点、阶段恢复和 OT 等 18 个定向文件，164 通过；最终证据展现专项 `pytest ... tests/test_report_verification_notes_311.py tests/test_trusted_emergency_content.py tests/test_local_coverage_proof.py tests/test_research_generation_identity_333.py`，35 通过（重叠不累加）。受管沙箱首次 pytest 在临时目录报 WinError 5，使用受审查的隔离执行后复测；未修改目录权限或安全设置。

同一 SHA 的 3.3.2 包身份复核：2026-11-04 重算 SHA-256 仍为 `F4D4C4B5653DD1FC3A793C2256187D10C98D02A1D939629809734539A232F7F2`，实际包内 build ID/commit 见 §1，**不是**主 QA 页首的 `e74e4db7…`／`94c489cf…`。原 ZIP、两份 QA 和旧清单均保留；其中研报由模拟器产生，不作为真实模型语义或费用证据。

真实供应商：未授权、未调用。输入命中率、实际总输入、未命中输入、输出、账单和线上耗时均为 **UNKNOWN**；本地编译 A/B 不替代供应商指标。

### 10.1 同一材料的完整请求比对

性能记录：`tmp/333-cmb-332-official-plan.json` 与 `tmp/333-cmb-333-official-plan.json`。源材料 SHA-256 分别为 `94a0f0b975c9087ec64b86acf4adb7a5281ab3c052482f0429f177135eb66b31`（同一持久化摘要）与 `cc0dac12e443bf7f9d87051fbbbdf7539d269856cbf921325e32a2c024442a85`（同一 286 事实文件）。旧包代码来自已绑定 3.3.2 ZIP 的 PYZ 提取；两侧同 Python/同规划入口，没有发模型请求。

9/9 system 哈希相同，6/9 完整 user 字节相同。增长、反方、情景三处的补充尾部 JSON 键顺序不同；三者完整 JSON 树（仅按键规范化、不去字段）、固定前缀哈希均完全相同，见 `tmp/333-cmb-{332,333}-tail-equivalence.json`。保留全部原始材料与依赖，未靠剪裁提速；不外推真实供应商缓存。总规划含前置的 CPU 300.00→40.27 秒，plan 调用壁钟 266.88→38.77 秒，读取 0.38→0.20 秒，解析 0.60→0.60 秒。

最终源码另做一次 `python -m cProfile -o tmp/333-planner-final-cmb.prof scripts/profile-research-planning.py ... --output tmp/333-cmb-333-phase-profile.json`：路由累计 25.03 秒（匹配器 0.31 秒），JSON 编码 15.72 秒，引用编码 11.98 秒、物理引用校验 11.66 秒，SHA-256/hex 原语自身约 1.92 秒。采样带逐调用 instrumentation，整个 plan 为 120.24 秒；子项会重叠，**不用于**上面无 profiler 的 A/B 比率。

### 10.2 历史红项复核

不改两份 QA 原文。旧 `candidate-first-pass-failed-nodeids.txt` 中研究编排节点及当前证据/报告测试复验，53 通过、13 失败（截至增加无进展长度保护前）；旧八个坏引用测试已改名，按整个现行文件复验，不能把旧名未收集算通过。分类如下：

| 范围 | 现行契约判断 |
| --- | --- |
| 失败 base 的 verified 分组、无证据增长机会 | 旧断言要求不完整对象与已验证分组等同、或完全不显示。现行要求部分可读、计算和已验证渠道隔离，按 R4 实测；不把红色断言计通过。 |
| 单一来源变化后 18 次总调用、合成空结果 12 次、完整工作流语义验证 3 次 | 旧绝对调用数不能代替实际变化的证据闭包；以 R1/R2 的具名请求、增量和模型可见状态验收。保留这些旧断言的失败。 |
| 阶段 “validating” 时模拟崩溃前应只有 1 次生成 | 当前在同一 ready wave 先存多项候选，再批量核验；该回调不等于首个候选写入时刻。单阶段候选的重用和真正收据重开由现行定向测试覆盖，旧断言仍红。 |
| 上下文字节估算 4,000／23,424、容量拒绝前 0 次合成 | 估算系数、tokenizer、显式物理字节和实际服务商拒绝必须分别看；不将旧估值改为硬门槛。显式物理上限与有效请求校验见 `test_capacity_completion_311.py`，相邻大容量旧测试仍红。 |
| 长度截断应仅一次、增长显式重试应再发相同请求 | 截断允许按具名缺口补全，重复长度且无目标增量应停；同材料同模型优先重用收到的候选，不能为达到固定数字重发。合法配置／材料变更和授权补救需独立证明。 |
| 反方 registry 应重复 41 条事实 | 引用 registry 与请求中完整 common core 不是同一集合；以物理引用闭包及完整树验证，不按重复数量判定丢失。 |

其他旧 QA 的环境/夹具、视觉恢复、个别 PDF、本地结构与历史数据问题仍按 3.3.2 台账保留，未将未运行节点或任何未复验原生用户流程计作通过。

## 11. 构建前来源冻结

- 基准提交 `530f7a0138dcfccf518dc9bc213be20da1278ce1`；在保留脏工作区的情况下冻结，不将它描述为干净提交构建。
- `python scripts/generate-release-snapshot.py 3.3.3 research-context-efficiency`：27,953 项（普通文件与目录链接），prebuild 清单 SHA-256 `4c34825f2434198ce56b3a4ed6e160bd52fea39032e3f08897aedb2c217c38a3`。生成器选择树与根文件后缀、模式／链接目标／删除记录、显式生成项排除均记录在 `prebuild-metadata.json`。
- 可恢复项目源码归档 `tmp/research-context-efficiency-3.3.3/package-20261104-182446-9b6ff5aa/source-snapshot-scoped.zip`：331 项逐文件对照清单并回读验证；SHA-256 `49dfe2a2925ce6a5dee4126339637eba69ddc3bd16faf3cc056c073f80dfa05d`。包目录下 `snapshot-record.json` 含归档文件和排除规则；`external-dependency-files.jsonl.gz` 记录 21,662 项 Node／Python 工具依赖，配合锁文件和固定 D 盘工具链恢复。打包前后27,953项非生成输入逐项完全相同；权威清单共27,955项，SHA-256 `d98313e53d89f5cc5ed2e4a9b8654a0743e2659440b0e25dfbef646b730f8e33`，严格比较 PASS。
- 旧的 `desktop/target/**`、`desktop/tmp/**` 及本地诊断文件没有进入源码归档；本次使用独立的 `CARGO_TARGET_DIR`，不会读取旧 target。这些条目在宽范围清单及 `excluded-nonsource-manifest-rows.jsonl.gz` 中保留作区分。一次过宽尝试的未完整 ZIP 保留在同一临时目录，不是交付产物。

### 构建尝试

2026-11-04 首次正常打包：sidecar 构建完成，Tauri 前置 `npm run build` 在受管沙箱内被 Vite 的子进程 `spawn EPERM` 拒绝；退出非零，未生成正式 ZIP、未运行隐私扫描。见构建目录 `package-build.log`。重试前抽查两份 TS 增量缓存、Cargo.lock 与 package-lock 均与冻结输入一致。仅对同一冻结源码执行一次受审查的离线构建重试，日志为 `package-build-elevated-retry.log`，无权限配置、杀软或生产环境变更。

## 12. 用户验收与发布

- 用户于2026-11-05明确确认3.3.3已完成验收，并批准上传和发布；该决定优先，QA报告仅作辅助，不再要求重复确认。
- 用户验收与用户测试：PASS。记录此状态不扩写未提供的测试步骤或真实服务商指标。
- 便携包：`installer-output/OpenThesis-3.3.3-windows-x64-portable.zip`，61,569,744字节，SHA-256 `54B99152322E831CA18A13EC963E37388CD8A735BF5C8DAF5E74659F3DC5BEFF`；build ID `7dea9c20bfe7d3d11ef168e98b0231042e47dd66491522e5859d110fab17d131`，基准提交 `530f7a0138dcfccf518dc9bc213be20da1278ce1`。
- 源码依据：331项冻结源码归档，SHA-256 `49dfe2a2925ce6a5dee4126339637eba69ddc3bd16faf3cc056c073f80dfa05d`；上传前逐项确认归档文件与工作区内容一致。只提交该快照对应的版本差异、源码清单和发布材料。
- 构建记录：构建器退出码0；便携包检查、内置PDF版本、冷启动3/3、演示报告重启后读取及隐私扫描均为PASS。上述构建记录不替代用户验收；服务商真实用量和缓存指标未测量。
- 双语发布说明：`docs/releases/research-context-efficiency-3.3.3-release-notes.md`，汇总自3.3.1、3.3.2、3.3.3未发布版本记录。
- Publication: PENDING（完成PR合并及GitHub Release后更新本地交接状态）。
