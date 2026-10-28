# OpenThesis 3.2.6：收缩识别链路，修复真实耗时与误报

日期：2026-10-26；开发验收更新：2026-10-27；用户测试及发布授权更新：2026-10-28。状态：**批准范围开发验收完成，用户已按约定确认测试完成并授权发布**。用户于 2026-10-26 明确提出“开发3.2.6”，据此批准本记录下述完整范围、回退/收缩决策和验收标准；GitHub 上传/发布授权于 2026-10-28 明确给出。

版本按用户明确要求定为 3.2.6，承接仓库 3.2.5；消息中的“3.3.5”按笔误处理，不据此修改版本历史。

## 1. 产品目标与判断

用户点一次研究，应自动获得可用于研究的财务数据。普通文本型年报不能持续几十分钟拆窗口；安装识别组件不能要求普通用户寻找 Python 路径；年度已识别不能因为同年季报失败而被显示为拒绝。

**本版先减去无效执行，再改造已有解析热路径。暂停增加识别引擎、通用恢复框架和公司特例。** 99% 仍为公司级识别目标，沿用 3.2.5 定义；本版不能用取消校验、部分事实或仿真报告证明该目标。已有正确事实、原件、检查点、研究正文必须保留。

## 2. 决策依据与边界

- 用户贵州茅台截图：已运行 23:12，进度 24%，显示窗口 10/10，同时剩余 44–110 分钟；这是不可接受的产品表现。缺少该 run 的阶段明细，不能认定所有时间均来自某一函数。
- `docs/qa/3.2.5qa.md`：精确包 SHA-256 为 `D3493C344A830C4DF83E94954AD726492FABEC231FF2DEE31EEE13B03B6B8A5C`，使用确定性提供方仿真 seam，报告写 PASS / PRODUCTION READY。该结果不证明真实模型质量、默认用户识别性能或新增组件带来的提升。
- `docs/qa/independent/3.2.5QA.md`：相同包冰轮冷恢复超过 30 分钟仍为 8/10，续作 131 秒完成；最终记录 73 个 PDF worker、1,173 页，无 worker 原始错误。70 个金额对页通过，但年度状态被季度污染。即便 QA 未单列性能缺陷，本版仍把耗时列为 P0。
- `financial_checkpoint.py::run_checkpointed_windows/produce`：整窗口超时后递归二分，每个子窗口又可用完整 worker timeout；父节点必须两个子节点都完成才返回。它能保留子检查点，但不能等同于及时发布可用子结果。
- `financial_ingestion.py::_CheckpointPdfWindowWorker`：每窗口新建引擎，`_parse_pdf_ast` 每次 `pdfplumber.open`；外层切片还会重新执行文件 hash、文档上下文装配、选页和目录生成。磁盘文本缓存存在，因此不能声称每次都重做全文 OCR；应测量打开、启动、装配和解析分别占多少。
- `document_catalog` 当前在入口生成并保存，实际 `selected_pages` 仍从旧文本分类函数计算；新目录尚未成为执行计划的共同输入。
- `local_structure.py::LocalStructureExtractor` 已接入 pipeline，但需安装并启用。截图显示未安装，不能把此次等待归因于 Paddle 推理。既有开发证据只证明公开单页 CPU 推理可运行，约 79 秒且有表头/合并行错误；不证明它能改善完整公司识别。
- 本地组件当前每个未缓存页独立启动 `structure_worker.py`，找不到标题就选择整份文件。开启后存在重复加载模型和整本重识别的明确风险，尚未测得此次用户机器的耗时贡献。

## 3. 保留、回退与修改清单

| 对象 | 决策 | 开发必须实现的效果 |
| --- | --- | --- |
| 3.2.4 包内 PDF 启动修复、pypdf 回退 | 保留 | 不回到旧版包无法读 PDF 的状态 |
| 原件 hash、单位/期间/scope 证据、财务 compiler | 保留 | 减少执行成本不减少财务准确性 |
| 页文本缓存、已完成窗口、原始结构结果、局部能力准入 | 保留并迁移 | 升级后可继续；规则变化只重验受影响候选 |
| 超时后整窗→半窗→单页的默认递归策略 | **退出默认热路径** | 改为开始前页级计划、页级保存；无须先耗尽大窗口才能得到小任务 |
| 未被消费的目录重复生成 | **删除重复执行，接入唯一页计划** | 一个源版本生成一次，可用于 native、结构修复、诊断；不再平行维护两套选页结果 |
| 默认设置页中手填 Python 路径、现场 pip 安装流程 | **撤出普通用户流程** | 放入明确实验性的高级入口；未安装不提示用户必须安装才可研究 |
| PP-StructureV3 扩展投入 | **暂停扩张，保留已装用户能力** | 不自动卸载、删除模型或关闭用户已启用项；不得作为普通研究的必要前置条件 |
| 整本文档逐页独立启动重模型 | **废除默认行为** | 按缺口批量处理、复用一个受控推理进程；无标题先定位，不直接整本重推理 |
| 按日历年份聚合拒绝状态、笼统年度隔离提示 | **修正** | FY、Q1/H1、AS_OF 和 scope 各自汇总，与实际参与计算的数据一致 |
| 仅凭窗口事件推总进度/长时剩余估计 | **替换** | 显示唯一页/表的完成量、当前工作与真实耗时，不把“恢复缓存”当解析完成 |

这是按功能撤销和替换，不执行 `git reset`、整版回退或数据库清空。当前工作区含大量历史未提交内容，开发只能提交本版明确文件差异。

## 4. P0：把 native 解析变成有用工作持续落盘

### 4.1 一次计划、按页执行

修改 `financial_ingestion.py`、`financial_checkpoint.py`、`financial_sources/document_catalog.py`：

1. 以原件 hash 绑定文本/页几何和目录，以解析规则指纹绑定分类结果。源字节未变可复用提取结果；规则变化重做分类，不重复读 PDF。消费缓存前验证源身份，不能依赖路径相同。
2. 目录输出明确的主报表页、续表关联、单位/期间声明页及尚未定位缺口。主表优先，相关附注随后；未知页仍保留并可渐进扩大定位，不靠页数截断材料。
3. 将最小可保存工作定为单页的几何提取和候选解析。页面间的单位、表头、期间上下文保存为显式依赖；单页成功立即落盘。已定位的大表无需先提交一个可能超时的整窗。
4. 文档 worker 保持 PDF 打开状态，流式返回已完成页的结果。跨文档并发有统一 CPU/内存调度，避免外层多文档与内层多 worker 叠加；同一连续表按依赖顺序执行。
5. supervisor 保留单项卡死隔离与取消。页卡死只终止该 worker，重建时继续尚未完成工作；不同页不因父窗口失败重跑。没有新输入/策略的失败页不能自动循环原算法。
6. 已完成子页的事实，只要自身证据链完整，通过同一 compiler 即可增量提交；依赖缺失表头的候选保留待补。禁止将一页失败扩散为父窗口、整份原件零结果。
7. 兼容旧窗口检查点：先读取可验证的已完成记录，并保留页来源/context；无需把所有旧窗口重新解析才能迁移。新旧执行者不能同时处理同一源/页/策略。

### 4.2 用现有执行链切换路线

`financial_pipeline.py` 继续拥有策略排序，checkpoint 只拥有原件提取续作，compiler 只拥有候选验证，应用层只负责发布与用户操作。不得新增第四个调度器。

- 文本清晰但数值未入库：区分列映射、期间、单位、科目及校验失败，先重用已有表格/文字进行局部解释。无须重复 OCR 同一清晰页。
- 空白/乱码或几何列不可恢复：才进入已启用的结构引擎或已授权云端路线。
- 未定位到目标表：先用文本目录/书签与轻量布局定位，按需扩大搜索；必须允许扫描文档的定位路径，不能“无文本标题→无可修复页”。
- 外部授权和用户显式费用上限沿用现有约定；授权内自动续作，不能新增反复确认，也不能静默转收费模型。保留 3.2.5 第13节用户明确要求生效的显式上限。
- 每次修复必须记录缺口是否减少。保存新日志、更新心跳、增加尝试次数不算识别进展。自动新增全量研究模型调用不得用于弥补本地执行设计问题。

## 5. P1：本地结构组件的处置

现有组件有运行价值的证据，但没有默认用户收益的证据。3.2.6 不再以“大模型已安装”作为交付成果。

修改 `local_structure.py`、`resources/structure_worker.py`、`SettingsView.tsx` 及相关文案：

- 普通设置只显示当前有效路径和是否可用；未安装组件不妨碍默认识别。现有安装/启用用户仍可管理、关闭和查看日志，未完成安装不标记可用。
- 手动 Python/pip 入口仅留在高级实验设置，明确依赖、大小、用途和状态。**本版不另造在线运行时安装器**；普通用户一键离线组件包列为后续独立交付，未就绪不显示虚假“一键可用”。
- 已启用时，按明确缺口/页计划进入批处理 worker，同批加载一次必要模型、逐页保存；缓存命中直接复用结构结果。独立页失败不抹除其他页，取消后无孤儿进程。
- 不加载与目标财报表格无关的可选推理模块；具体模块裁剪必须以所锁定模型的输出对照为依据，不擅自改变模型文件契约。
- 解析后的结构仍走现有 cell/period/scope/compiler；不得用 OCR 成功标记直接生成 verified 事实。
- 记录实际新增的正确字段数、冷启动/推理时间和峰值内存。若在固定原页上未提供有效增量，则不扩展默认适用范围。不得把该组件的单页可执行证明再写成公司识别成功。

## 6. P0：修复状态与用户看到的进展

### 年度状态

`service.py::_financial_status` 不再仅按 `period_end[:4]` 分组。按披露 accession、目标 fiscal period、期间起止、合并/母公司 scope、revision 归属汇总；年度节点使用相应 FY 的有效主版本，不能被同年 Q1/H1/AS_OF 或已被替代版本覆盖。`report_document.py` 的隔离提示必须对应实际排除的年度事实，不依据任意 continuity 非 accepted 就称年度数据全部被隔离。季报问题单独显示。

### 进度与恢复

- 顶部区分“财报 8/10”和“当前文件已完成 7/9 页”；窗口/子任务拆分不改变已完成唯一页面计数。
- 缓存命中显示“已复用”；还需校验的文件显示“正在校验”，不能仅因窗口已恢复而暗示整份财报完成。
- 展示当前页/阶段、最近实质产物时间、等待类型；资料/模型两个区域均可查看。错误和重试细节折叠，不挤占主要进度。
- ETA 按同类阶段实测吞吐与剩余页估计，样本不足/进入新后端时显示“正在估算”；不得用固定总百分比反推 44–110 分钟。对截图的 ETA 公式先定位实际实现再替换，不假定其当前算法。
- 可取消、保留进度、继续已就绪研究。只有证据依赖已满足的阶段可开始；不能为了缩短界面耗时让依赖缺失的财务阶段空转付费。

## 7. 开发顺序、验收与禁止范围

1. **先补现有诊断的阶段计时**：源/页/策略标识、进程启动、PDF 打开、文本/几何提取、候选映射、compiler、落盘、缓存命中、失败原因。导出脱敏汇总；不新增并行日志系统，不记录密钥。
2. **替换 native 默认执行与接通页计划**，然后修复年度状态和进度，最后收缩/批处理可选组件。按这一顺序提交可回退的功能改动，禁止先扩建插件市场或引擎数量。
3. 开发使用精确交付 ZIP、固定机器与原件 hash，对贵州茅台、冰轮环境、宁德时代进行默认关闭可选组件的冷/热完整识别。单列冷下载和本地识别；比较 3.2.5 同条件基线，不用缓存过的单页或仿真研究替代。
4. **性能目标**：上述普通文本型 10–12 份披露的本地识别，固定参考 Windows CPU 环境冷运行目标中位数不超过 5 分钟、每个场景不超过 10 分钟；热缓存无规则/源变化时避免再次启动页面解析/OCR。另以足够次数测 P95，不用三个单次样本伪造分位数。该目标用于验收，绝不变成达到时间就清空或终止研究的运行门禁。
5. 超过目标时必须给出阶段计时、主要成本及仍可交付范围，不靠延长 ETA 宣称性能修复完成。参考机器信息、重复次数、内存峰值与取消响应必须随证据记录。
6. 页面卡死场景必须证明已完成独立页可发布、继续不重复处理、受影响页可换路线；主表跨页/单位证据缺失必须证明不会错误继承或伪通过。年度正常而季度失败时，FY 状态与实际采用事实一致。
7. 对原件核对金额、币种、倍率、期间与口径，至少保持已确认的70项正确字段；茅台比较列缺口必须专项核查。覆盖扫描/无标题/跨页表的额外固定样本，不因解析失败更换样本。
8. QA 仿真供应商只证明编排与展示；组件启动只证明运行；完整公司识别与模型研报质量分别记载。不能再将前三者汇总成“生产全量可用”。99% 未经既定公司级样本验收仍标未验证。

不在本版范围：全量重构研究模型链、改变研究质量门槛、提高自动收费额度、清空缓存或用户报告、静默安装大型依赖。保留模型稳定前缀、证据快照与已有调用缓存策略；页面解析优化不得引发无关模型重新计算。

**批准状态：已批准。依据：用户于 2026-10-26 明确指令“开发3.2.6”；实施严格限于本记录列出的决策与验收范围。**

## 8. 开发记录（2026-10-27，Asia/Shanghai）

| 字段 | 状态 |
| --- | --- |
| Workflow name | `research-input-stability` |
| Target version | `3.2.6` |
| Acceptance | PASS |
| User Test | PASS |
| Upload Ready | YES |
| Publication | PENDING |

用户于2026-10-28提出“上传并发布3.2.6”。按既有约定，该指令同时表示用户已完成测试并批准上传/发布；不再另行等待测试确认。

### 已实施的源头改动

- Native 默认从页级计划开始；文档内保留一个可终止的 PDF/解析会话，页成功即落盘；兼容旧窗口及已成功子页。独立页失败不阻断其他页，但断点后的页不得沿用失败前的表头。
- 页目录成为 native 与结构修复的共同入口；同源全文文本、原始词坐标与页文本分别缓存，财务规则变化只重解释，不要求重读已缓存页。主表及其连续依赖优先，未知页不删除。
- 茅台实际剖析发现标准化热点：同一页曾发生约 419 万次规范化调用。增加有界纯函数缓存及完整规则快照键；不是减少字段、材料、校验或解释步骤。单页剖析从 52.297 秒降至 1.656 秒，4 个事实及10条证据保持不变。详见 `tmp/research-input-stability-3.2.6/moutai-page-profile-{before,after-cached-labels}`。
- FY 与季度/中期、母公司/合并、有效 revision 分开汇总；只有实际年度拒绝才显示年度隔离提示。
- 财报数与唯一页数分开；缓存复用、当前页、最近实质产物、等待类型可在两种活动视图查看。ETA 只用当前文件的新解析样本或同类校验样本，不以百分比反推；不足时显示正在估算。
- Python/pip 安装入口收进高级实验设置；默认识别不依赖可选组件。已安装启用项未被关闭、卸载或删除。
- 可选结构引擎复用一个批处理进程，逐页原子保存；扫描无标题先做轻量布局排序，保留全部搜索前沿，只有同一 compiler 确认目标完整才停止扩展。坏页、取消不抹掉已完成页。
- 诊断只导出源身份绑定的数值白名单，支持零事实失败的阶段计时；包含实际结构新增准入字段数，而非把 OCR 成功当财务成功。

### Scope check

| 范围 | 影响 | 说明 |
| --- | --- | --- |
| Production behavior | YES | native 页会话/缓存/计划、诊断、状态、可选结构批处理 |
| Dependencies | NO | 未新增依赖或下载权重；沿用锁定模型与运行时 |
| Packaging/build configuration | YES | 九处版本字段统一为3.2.6；使用现有打包脚本 |
| Packaged output | YES | 后端与前端均需重建；仅在源码变化后重建，新旧候选及失效理由分别保留 |
| User-facing workflow | YES | 实质进度、阶段 ETA、年度状态、实验设置入口 |
| Acceptance metadata only | NO | 不适用 |
| Selected path | STANDARD | 功能改动及正常测试版打包；不自动调用QA |

### 本次明确实施文件

后端：`financial_checkpoint.py`、`financial_ingestion.py`、`financial_taxonomy.py`、`financial_sources/document_catalog.py`、`financial_pipeline.py`、`financial_admission.py`、`application/financials.py`、`application/financial_repair.py`、`storage.py`、`service.py`、`report_document.py`、`local_structure.py`、`resources/structure_worker.py`（均位于 `src/openthesis/`）。

前端：`desktop/src/components/States.tsx`、`desktop/src/features/research/researchProgress.ts`、`desktop/src/features/settings/SettingsView.tsx`、`desktop/src/types.ts`、`desktop/src/i18n.ts` 及对应测试。

回归：`test_financial_checkpoint.py`、`test_pdf_singleflight_310.py`、`test_financial_candidate_observations.py`、`test_parser_observations_326.py`、`test_positioned_page_cache_326.py`、`test_pdf_session_geometry_326.py`、`test_financial_label_cache.py`、`test_financial_label_performance_326.py`、`test_local_structure_325.py`、`test_reporting.py`、`test_service.py`、`test_financial_retry_progress_326.py`、`test_incremental_page_publication_326.py`、`test_preparation_recovery_session_326.py` 等相关定向套件（均位于`tests/`）。开发工具：`scripts/benchmark-recognition.py`、`scripts/profile-financial-page.py`。

版本输入：`pyproject.toml`、`src/openthesis/__init__.py`、`desktop/package.json`、`desktop/package-lock.json`、`desktop/src-tauri/Cargo.toml`、`desktop/src-tauri/Cargo.lock`、`desktop/src-tauri/tauri.conf.json`、`build_support/version_info.txt`、`OpenThesisSidecar.version.txt`。

## 9. 开发证据与逐项交付检查

参考环境：Windows、Python3.12、AMD Ryzen 9 6900HS、16个逻辑处理器、物理内存16,370,769,920 bytes、电源方案“平衡”；可选组件关闭的原件重放不调用研究模型。所有临时结果位于 `tmp/research-input-stability-3.2.6/`；原库只读，隔离输出。时间采用日志/JUnit的UTC或Asia/Shanghai时间戳。冷运行指每次全新应用数据/文本/页/检查点缓存，不清空操作系统文件缓存；固定本地原件不等同冷下载。

下表E1指上述固定Windows/CPU环境、工作区TEMP/TMP；E2为同机`tmp/research-input-stability-3.2.5/installed-component-proof/runtime/Scripts/python.exe`的可选PP CPU环境，沿用锁定的PaddleOCR3.7.0/PaddleX3.7.2/PaddlePaddle3.2.0与已安装官方模型，完整性核对通过，无新增下载。日期均为2026-10-27，时间为+08:00；路径相对本版TMP目录，测试路径相对`tests/`。操作命令见本记录“可复现的最终开发命令”及末节，各`.log`/JUnit/JSON保留退出结果和时间。本表是当前包结论，后文历史候选的PENDING/撤销记录不覆盖本表。

| ID | 批准要求 | 操作与日志/输出 | 实际结果 | 环境 | 时间 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| R01 | 同源身份、规则重解释不重复提取 | pytest缓存/会话套件；`page-geometry-cache.log`、`geometry-integration.log`；三个warm进程proof | 缓存16项、集成30项通过；暖重验无新增解析进程，原始页字节不变 | E1 | 08:06 / 20:51–20:53 | PASS |
| R02 | 唯一目录、完整计划、主表/依赖顺序 | pytest目录/checkpoint；`final-checks-primary.log`；最终20轮RPC/源清单 | 105项组合通过；完整10/10/12原件，405/453/464候选页全部完成 | E1 | 07:55 / 20:21 | PASS |
| R03 | 页级保存、单会话、故障隔离、取消/迁移/无循环 | checkpoint/增量发布套件；`recovery-case-regression.log`；`package-cancel-resume-final-v3/proof.json` | 214项组合通过；取消后worker退出，4个已存页hash不变，续作完成 | E1 | 17:19 / 20:55 | PASS |
| R04 | 同一compiler及依赖门槛、旧值保护、片段发布不假关闭问题 | `financial-semantics-final.log`；214项组合；83项原页核对 | 语义84项、最终组合214项、原页83项通过；独立43项发布/repair回归包含于组合中 | E1 | 08:07 / 17:19 / 17:35 | PASS |
| R05 | 局部解释优先，既有结构/云授权及显式预算边界 | pytest结构/恢复/表格；`final-checks-agent.log`；`structure-batch-proof/proof.json` | 授权/预算回归通过；未调用云/收费模型，未扩大默认路线 | E1/E2 | 07:55–08:08 | PASS |
| R06 | 普通设置移出Python/pip，保留已装用户能力 | `npm test`两文件；`frontend-final.log` | 40项组合通过，安装入口为折叠高级实验区，默认不要求安装 | E1 | 07:56 | PASS |
| R07 | 结构单批进程、原始缓存、无标题定位、取消/坏页隔离 | pytest本地结构/恢复；`structure-batch-proof/proof.json` | 回归通过；真实两页一worker，98.937秒；缓存和兄弟页保留 | E2 | 07:55–08:08 | PASS |
| R08 | 锁定模型契约、不加载无关模块、不扩张默认投入 | pytest worker lock；锁文件未改；真实两页proof | 契约通过；PP峰值4,276,797,440 bytes，新增正确字段0，因此继续实验性 | E2 | 07:55–08:08 | PASS |
| R09 | FY/中期/AS_OF、scope/revision状态隔离 | pytest service/report；最终宁德`result.json.financial_status` | 年度2020–2025 verified；Q3/AS_OF真实缺口单列，不污染年度结论 | E1 | 08:06 / 17:38 / 20:21 | PASS |
| R10 | 文件/唯一页、缓存/校验、当前页/等待/实质产物 | 前端两视图测试；20轮正式retry/status RPC `progress.jsonl` | 40项组合通过；20轮均有实际页计数及实质产物事件 | E1 | 07:56 / 20:21 | PASS |
| R11 | 实测ETA，不以百分比反推，样本不足估算 | States缓存/长等待/新样本测试；前端build及精确EXE启动 | 测试通过；`tsc -b && vite build`通过，精确包启动3.437秒 | E1 | 07:56 / 17:23–17:25 | PASS |
| R12 | 取消/保存/依赖就绪，零无效模型调用 | 最终214项组合；取消v3；20冷+3暖及3独立暖证明 | 取消0.125秒终态、worker零残留；续作96.187秒/324事实，4原页不变，模型调用0 | E1 | 17:19 / 20:55 | PASS |
| R13 | 白名单诊断、hash/期间及零事实失败计时 | observations套件；`final-checks-agent.log`；214项组合；最终JSON导出 | 边界通过，阶段成本可见；历史117通过1跳过不算全PASS，最终214项零跳过 | E1 | 07:55 / 17:19 / 20:57 | PASS |
| R14 | 三公司完整冷/暖、固定来源、无对照字段损失 | 精确包矩阵20+3、3个warm proof；`final-evidence-summary.json` | 20/20冷完成，旧字段损失0；三场景中位数198.094/191.297/238.336秒 | E1 | 17:27–20:21 / 20:57 | PASS |
| R15 | 旧版同条件对照、阶段成本、受控重复P95 | 新20轮矩阵；旧3轮矩阵；汇总器源集合/旧ZIP校验 | 新混合P95 241.453秒；旧茅台>600删失，冰轮342.969/宁德410.359秒；不伪造旧P95 | E1 | 20:21 / 20:45 / 20:57 | PASS |
| R16 | 保留QA70及茅台13项，固定扫描/跨页/无标题回归 | `check-frozen-financial-values.py`；`recovery-final-package-financial-values.json`；结构/目录/跨页套件 | 金额、币种、倍率、期间、口径、原页83/83；固定组件夹具与真实公司验收分开 | E1/E2 | 08:08 / 17:35 | PASS |
| R17 | 正常包/输入快照/启动/版本/关键流程 | 打包日志；精确EXE/PDF；取消v3；`final-source-freeze-check.log` | 3B43A10C包、3.2.6、启动/PDF/正式重试/取消续作通过；46,450项快照Hash未变 | E1 | 17:23–17:25 / 20:47 / 20:55 | PASS |
| R18 | 分开编排、组件、真实识别、模型质量与99% | 本记录与最终汇总JSON边界字段核查 | 真实研究模型质量、总体99%及用户独立QA仍未验证；未写成通过 | E1 | 20:57 | PASS |

开发检查确切命令、退出码、耗时和时间戳见上述`.log`及`.junit.xml`。全局 `git diff --check` 另发现无关历史文件 `docs/releases/website-1.2.1.md:3` 的尾空格，本版未修改；本版源文件范围检查无尾空格问题。

## 10. 源快照、产物与后续状态

- Base commit：`3573ea7f0133fc632253ec38d92889093d0477e8`；当前工作区有大量历史改动，本版不恢复、不清空、不提交它们。
- 生成规则：`scripts/generate-release-snapshot.py 3.2.6 research-input-stability`；确定性排序记录src/desktop/build_support/scripts/.build-tools/.cargo及所列根输入，含未跟踪构建输入、文件mode/type/hash和删除项；最终生成build-info须绑定打包物，不以开发占位值替代。
- 排除仅限生成输出/缓存及不参与构建的验收元数据；不包括模型权重、用户库、财报原件、诊断导出、下载器或工作区无关二进制。
- Intended publication files：只限本版明确源/测试/版本/工具及本记录和最终输入manifest；具体上传另走`GITHUB_UPLOAD.md`，本次不授权上传。
- Artifact path：`installer-output/OpenThesis-3.2.6-windows-x64-portable.zip`。
- Artifact SHA-256：`3B43A10C3D1B61D8E5F13D8107B8E3314D1233274F38378ABB699AE4F79A4552`，61,212,144 bytes，`unsigned-test`。
- 最终输入manifest：`docs/releases/research-input-stability-3.2.6-inputs.jsonl.gz`，46,450项，SHA-256 `62A57B34E98285AEA43EEEF064807FF9DBB765414B2C4C6DC2DA9D398D8311F3`；以当前3B43A10C包内真实build-info生成，见`final-snapshot-recovery.log`。旧e4580快照仅保留于历史目录，不得与新包混用。
- Acceptance: PASS
- User Test: PASS（2026-10-28，按用户既有上传授权约定）
- Upload Ready: YES
- Publication: PENDING

当前包结论以第9节及末节的精确包最终证据为准；后续历史候选的失效记录完整保留，不把开发期间测量自动提升为最终包验收。

### 2026-10-27：精确包发现回调桥缺陷，撤销候选绑定

- 首个候选 ZIP：SHA-256 `0C84D6AA43BFAE4EC2BA27AD3DF0B126B5D99692D87FB9B5BAA000803754E077`，59,807,291 bytes。启动、包内PDF、版本/结构及一次隐私检查通过；最终输入快照46,450项，SHA-256 `041e06bc610ff14d1dcfe21838de15abde582f95a9a3827ae12740f8631dbe27`。副本和旧快照保存在 `tmp/research-input-stability-3.2.6/pre-progress-fix/`。
- 精确包茅台/冰轮首轮分别148.641/149.891秒，均无对照事实损失。但实时`research.status`中的`filing_states`始终为空，不能将进度交付项判为通过。
- 根因：`_capture_financial_repair_request.on_progress`仍只转发三个参数，丢弃上游detail；重试job对parse/validation阶段又只更新粗进度。此问题从正式RPC发现，不以组件mock通过掩盖。
- 修复仅补齐实际入口的detail通路并兼容既有三参数callback；需新增正式重试job的回归，确保回调体异常不被误吞或重试。
- 原20轮测量在第四次启动前通过不覆盖保护目录主动停止；前三次证据保留，不计为最终包P95，控制停止不归类为财报识别失败。新源码、新快照、新ZIP需重建并重测。
- Acceptance: PENDING；User Test: PENDING；Upload Ready: NO；Publication: PENDING。旧包未交付为通过版本。

### 正式重试进度修复与第二候选（2026-10-27，后因增量缺口撤销）

- 修复实际重试入口而不只修组件：`_capture_financial_repair_request`按回调签名转发第四个detail参数；旧三参数回调仍兼容，回调体`TypeError`不捕获重试。`_run_financial_retry_job`的parse/validation事件进入统一页进度发布；后续未带页计数的事件不清空已完成计数。
- 新增`tests/test_financial_retry_progress_326.py`，覆盖正式重试job到状态读取的链路。修复前复现证据为`progress-bridge-red.log`，修复后`progress-bridge-green.log`为31项通过；冻结后含新增终态计数断言的`final-progress-frozen.log`为151项通过、1项跳过，21.57秒。跳过项是当前Windows环境不能创建目录符号链接，不记为通过。
- 既有`test_latest_verified_partial_financial_data_continues_to_provider`的“两次provider实例”断言与当前冻结准备后共用工作流的实际架构不符；调整为严格验证一次正确配置的实例，并保留原有输入披露、终态、财务warning和准入断言，新增`revenue-only`输入断言。未修改生产模型阶段、调用策略或质量门槛。
- 最终候选ZIP：`installer-output/OpenThesis-3.2.6-windows-x64-portable.zip`；SHA-256 `3728504415F2F05BB69723AFA425DE6EFBEF96D267A4EAF56946FE5F6F57C0EA`，59,807,720 bytes，`unsigned-test`。正常构建日志`package-desktop-final.log`：内置启动3/3、包内PDF、portable结构与版本校验通过；精确解压EXE另一次启动通过，6.432秒，见`verify-desktop-runtime-final.log`。
- 最小调用隐私核对：旧候选已做全量零发现检查；最终候选逐项SHA-256比对550个未变条目、4个变化条目、0个删除条目，仅对变化条目运行原核对规则，退出码0。证据`final-package-privacy-delta/proof.json`与`delta-scan.log`。没有对未修改内容重复全量扫描。
- 可选PP真实固定两页批处理：茅台原件hash `474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288`，第59/61页，共98.937秒、一个worker；模型加载17.828秒，逐页推理36.844/36.047秒，进程峰值约4.277GB。4个候选、1个准入、相对native新增正确字段为0。因此维持实验性、不扩展默认范围；不把单页OCR可执行写成公司识别通过。详见`structure-batch-proof/proof.json`。
- 最终ZIP完整原件重复性能、金额对照与实时RPC页进度继续以本候选hash重新测量；旧候选的耗时不混入最终分位数。99%公司识别与真实研究模型质量仍未验证。

### 已撤销372850候选的首轮核查（历史证据，不作为新包验收）

`package-performance-final-20/`前三次冷运行分别为茅台146.156秒（10份/324个事实）、冰轮151.688秒（10份/222个事实）、宁德193.937秒（12份/234个事实）。三组均正式重试job完成、基线事实损失0、开放恢复问题0；正式RPC页进度和实质产物时间均可见。每个原件仍使用完整源hash清单，不因错误更换样本。

`final-package-financial-values.json`：既有60项年度与10项中期原页检查70/70通过，茅台12项金额加1项2024比较列权益共13/13通过，总计83/83；逐项核对金额、币种CNY、倍率1、期间、合并口径及规定原页。此结果绑定最终候选，不引用作废候选的金额结果代替。

首轮诊断计数和阶段成本（均来自最终包导出）：

| 公司 | 原件总页数 | 唯一候选页/完成页 | Native文档会话启动 | 源收集墙钟秒 | Compiler秒 | 准入落盘秒 |
| --- | --- | --- | --- | --- | --- | --- |
| 茅台 | 1,036 | 405/405 | 10 | 135.546 | 1.203 | 0.266 |
| 冰轮 | 1,173 | 453/453 | 10 | 135.891 | 4.298 | 0.187 |
| 宁德 | 1,750 | 464/464 | 12 | 183.328 | 1.000 | 0.172 |

全文上下文覆盖全部原件页；候选页由共同目录和依赖计划决定，不以性能时限截断。文档并发时间存在重叠，以上源收集/compiler等批次计时在各源观察中重复绑定，使用批次最大值而非逐行求和。原始诊断保留逐文件PDF打开、词坐标提取、候选映射及缓存等成本。这里的10/10/12是native文档会话数，不把另有的全文文本提取进程隐藏为“所有进程仅10个”。

### 增量发布审查发现与补齐（仍在原批准4.1(6)范围）

- 再次通过正式`OfficialFinancialPreparer → FinancialPipeline → compiler → Storage`本地夹具发现：一份原件已有6个字段通过compiler，仅附带另一页`pdf_window_incomplete`，`publishable_accessions`仍为空，最终研究事实6→0。证据`independent-publication-probe/proof.json`。逐页候选检查点可恢复，不能冒充独立已验证事实已经可发布。
- 因此撤销SHA `3728504415F2F05BB69723AFA425DE6EFBEF96D267A4EAF56946FE5F6F57C0EA`的最终候选资格，仍保留全部测试和金额证据。其16轮完整冷重放均无基线损失，但主动在第17轮前停止（`operator_stopped=true`），不能据此写最终包P95；旧版对照与取消探针尚未开始。
- 最小实施：在既有view/publication seam标明安全增量源；只允许原件hash/期间匹配的页级检查点确有独立compiler准入事实，且故障局限于已知页解析未完成的情况。故障和缺口继续保存，不把该原件宣称完整。
- 对已有同源正确事实，应用层将旧的已验证事实和新增候选再走同一compiler；若旧正确语义身份或值丢失/冲突，保留旧代、不覆盖。无合法原件/依赖证据、错误hash或期间仍不得准入；不新增存储表、调度器、云调用、费用或schema捷径。
- 不要求凑齐全部画像字段才发布其他独立正确事实；缺失字段不自动变成其余所有字段的依赖。页计划未完成的原件不进入healthy跳过集合，后续恢复仍保留。
- 普通完整识别路径不额外重复compiler；页候选先原子落盘，研究事实只在既有compiler与原子generation发布后可见。开发验收仍PENDING，需新源码快照与新包重新绑定。

### 安全增量发布实施复核

- `FinancialResearchView.incremental_accessions`只表达可合并的原件片段，不表达整份原件完成；入口必须同时具备源hash/披露期间绑定、`positioned-page-checkpoint`的真实部分完成计数和compiler明确准入记录。不要求其他独立字段凑齐才能发布；比较列与AS_OF仍由compiler判定，不强行改成主期间。
- 仅已登记的局部页/解析运行时故障可走此路；文件不存在、不可读、损坏、取消、hash冲突、无来源的required失败继续隔离。保留原typed failure、缺口和`source_page_plan_incomplete`；仅允许FULL降为PARTIAL，不提升BLOCKED或ACTION_REQUIRED。
- 应用层合并同原件已有正确事实后重走同一compiler。按币种、期间、口径及维度比较规范化金额，单位倍率不能掩盖金额冲突；旧正确身份/金额丢失、冲突或原件变更时保持旧generation。完整原件不增加该合并工作。
- 页计划未完成即使核心字段已齐，也不能进入healthy跳过集合。正式研究入口的`recovery_session_id`继续绑定实际run ID；额外回归验证不接受调用者内部会话覆盖。未修改生产会话绑定逻辑。
- 新增`tests/test_incremental_page_publication_326.py`（40项通过），涵盖真实prepare/compiler/storage链路、独立5/6字段、旧值保护、hash/期间及运行时故障边界；`tests/test_preparation_recovery_session_326.py`两项通过。补齐后的最终组合回归与新ZIP证据另行追加。
- 最终组合回归：2026-10-27 16:37:46 +08:00，Python3.12、`PYTHONPATH=src`、工作区TEMP/TMP，对上述两文件及pipeline、official preparation、candidate observations、publication CAS、source correction retention、lineage、dimensions、正式retry progress、parser observations、原始词坐标缓存、PDF session、checkpoint、label cache共15个测试文件运行`python -m pytest ... --junitxml=tmp/research-input-stability-3.2.6/final-incremental-regression.junit.xml`，退出码0，**204通过、0失败、0跳过，54.891秒**。精确目标列表、解释器路径和结果见`final-incremental-regression.log`；JUnit保留逐项用例与时间。未将独立用户QA记为已完成。

### 增量修复后候选包绑定（旧候选不得混用）

- 正常unsigned-test构建退出码0，包内启动3/3、PDF与portable结构校验通过：`package-desktop-incremental-final.log`。精确解压到`tmp/research-input-stability-3.2.6/exact-package-incremental-final/`后另做一次启动，3.213秒、版本3.2.6、PersistentReportRead通过：`exact-package-incremental-final.log`。该读取检查证明持久化/展示通路，不证明仿真研究质量。
- 新ZIP SHA-256 `D7A531EEABE27B7A270B6A51D047BAF71311EE5628446980448988769C89E1A2`；包内sidecar SHA-256 `1B15DDABF78329EDFEB52A2CA14FF7525D5A4869F3AC4A3C361D661D543244AA`。旧372850候选及旧manifest保存在`pre-incremental-fix/`。
- `incremental-final-package-privacy-delta/proof.json`逐条SHA对照初次全量零发现候选：550条未变、4条变化、0删除；只对变化条目按未修改规则扫描，退出码0，见`delta-scan.log`。不重复扫描未变字节，不据此声明有发布签名。
- 后续完整原件性能、83项金额和取消/恢复只绑定此新ZIP；`package-performance-incremental-final-20/`为全新输出，不沿用旧候选16轮作为最终P95样本。
- 新包首轮完整本地冷运行：茅台199.407秒（10份/324事实）、冰轮186.797秒（10份/222事实）、宁德234.828秒（12份/234事实）；三组旧事实损失0、开放恢复问题0、研究模型调用0，正式RPC逐页进度与实质产物可见。原件完整且源Hash一致；并非任意科目均无缺口或真实研究模型已完成。
- 2026-10-27 16:58 +08:00，新包首轮facts经`check-frozen-financial-values.py`对照冻结原页检查，83/83通过（原70项及茅台13项），退出码0，输出`incremental-final-package-financial-values.json`。宁德年度节点2020—2025均verified，Q3/AS_OF局部缺口仍单列于interim，不转写成年度拒绝；没有用降低财务门槛抹掉真实缺口。

### 正式repair后置条件的补齐（D7A531候选暂停最终资格）

- 最终审查继续追踪所有`publishable_accessions`/`resolve_financial_recovery_cases`使用方，发现`FinancialRepairer`把“片段事实已promoted”与“整源恢复已完成”混为一谈。真实repairer、compiler、storage和report refresh夹具复现：6项事实和报告更新成功，未完成页的parse case却被误置resolved。
- 红色证据：`incremental-repair-case-red.log/.junit.xml`，新增`test_formal_repair_keeps_incomplete_page_case_open_after_incremental_publication`一项失败，其他40项未选；不将前述prepare级204项通过当作完整repair后置条件已覆盖。
- 限定修复：保留processed、已准入事实audit与报告刷新；仅“关闭恢复案例”的集合扣除`incremental_accessions`。真正完整恢复后仍按原有路径关闭，未完成页保留open与下一操作。所有使用方已定向检查，不新增状态框架。
- D7A531包重复矩阵在当前第6轮完成后受控停止，当前job不强杀；既有结果只作历史测量，未混入下个hash的P95。待补测试证明“partial仍open→后续完整恢复才关闭，原正确事实不丢失”后再冻结、构建并绑定新包。Acceptance继续PENDING，尚未交付为通过版本。
- 后置条件修复已通过：保持`processed`用于准入audit/accepted/报告刷新，仅关闭集合扣除`incremental_accessions`。三项真实repair回归证明部分成功保持open、后续完整恢复关闭且6项旧正确值保留、正常完整恢复关闭既有runtime case；2026-10-27 17:18:25 +08:00最终16文件组合回归**214通过、54.08秒**（JUnit 54.027秒），退出码0，证据`recovery-case-regression.log/.junit.xml`。D7A531及其manifest已验同Hash备份到`pre-repair-case-fix/`；新候选另建，不覆盖历史结果。

### 当前3B43A10C候选包（最终测量对象）

- `package-desktop-recovery-final.log`：正常unsigned-test构建退出码0，内置启动3/3、包内PDF及portable结构通过。ZIP SHA-256 `3B43A10C3D1B61D8E5F13D8107B8E3314D1233274F38378ABB699AE4F79A4552`；61,212,144 bytes。
- 精确解压目录`exact-package-recovery-final/`；其中sidecar SHA-256 `377182D7E98A78C22443812425A537570760E70DE62B56C3D06BA4DC5E225DB9`。首次附加启动检查被本机沙箱CIM读取权限阻塞，日志`exact-package-recovery-final.log`保留；检查无本次残留进程后，以提升权限对同一EXE执行一次，通过（3.437秒），版本3.2.6与持久化报告读取通过，见`exact-package-recovery-final-elevated.log`。不重打包，不把权限环境失败改写为生产失败或抹掉记录。
- `recovery-final-package-privacy-delta/proof.json`：550个未变条目继承初次已检查字节；仅扫描4个变化条目，0删除，原核对规则退出码0。未重复全量扫描。
- 当前完整原件重复性能目录为`package-performance-recovery-final-20/`，绑定此ZIP/sidecar；20次冷运行与3组热运行串行执行，不与构建/测试抢CPU，不调用研究模型。旧候选结果不参与本包分位数。
- 2026-10-27 17:35 +08:00，当前新包首轮茅台205.453秒、冰轮197.578秒；83项冻结金额/币种/倍率/期间/口径/原页检查再次全部通过，证据`recovery-final-package-financial-values.json`，不是引用旧候选结果。

### 可复现的最终开发命令

工作目录`D:\githubmax`，解释器`C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe`，`PYTHONPATH=src`、`TEMP=TMP=D:\githubmax\tmp`；下列214项回归的目标顺序与实际执行一致：

```powershell
python -m pytest -q --junitxml=tmp/research-input-stability-3.2.6/recovery-case-regression.junit.xml `
  tests/test_incremental_page_publication_326.py tests/test_preparation_recovery_session_326.py `
  tests/test_financial_pipeline.py tests/test_official_financial_preparation.py `
  tests/test_financial_candidate_observations.py tests/test_financial_publication_cas.py `
  tests/test_source_correction_retention.py tests/test_financial_lineage_storage.py `
  tests/test_financial_dimension_contract.py tests/test_financial_retry_progress_326.py `
  tests/test_parser_observations_326.py tests/test_positioned_page_cache_326.py `
  tests/test_pdf_session_geometry_326.py tests/test_financial_checkpoint.py `
  tests/test_financial_label_cache.py tests/test_financial_repair_use_case.py
```

- 前端：在`desktop`执行`npm test -- src/components/States.test.tsx src/features/settings/SettingsView.test.tsx`，40项通过，`frontend-final.log`。最新打包内`npm run build`执行`tsc -b && vite build`成功，不额外重复同源构建。
- 精确包完整原件：`python tmp/research-input-stability-3.2.6/run-frozen-performance.py --sidecar tmp/research-input-stability-3.2.6/exact-package-recovery-final/OpenThesis-3.2.6/bin/openthesis-sidecar/openthesis-sidecar.exe --archive installer-output/OpenThesis-3.2.6-windows-x64-portable.zip --version 3.2.6 --trials 20 --name package-performance-recovery-final-20 --observer-timeout 3600`；各子命令使用既有`scripts/benchmark-recognition.py`，原件源库只读，新建应用缓存，日志和source sets保留在该目录。
- 包内PDF：`recovery-final-package-pdf-proof.json`保存与当前ZIP SHA一致的一页原件、page_count/text_window两操作，均exit0、backend pdfium、非空页1、无原始错误码。此小样本运行证明与完整公司回放分开，不相互替代。

### 测量工具会话中断与同包续测（2026-10-27 Asia/Shanghai）

- 工具重连后原执行session `85905`返回Unknown process id，原子agent不再存在；矩阵停在17次完成，第18次仅记录至53.625秒、最后页事件为18:27:57，未生成完成结果。没有将该不完整尝试认定为产品成功、产品失败或完整耗时。
- 20:08后复核：ZIP仍为`3B43A10C...79A4552`，生产源码digest与首轮一致，Windows/CPU/Python环境与首轮一致。仅按本次精确TMP二进制路径检查进程，不泛杀用户进程。
- 仅TMP测量器增加`--resume`：先逐条验证原17次的版本、sidecar Hash、原件集合、主体、冷运行标记、来源digest及旧事实无损，再复制原matrix并记录其Hash；未完成18目录和日志保留。18–20用新的continuation输出和全新应用缓存补齐，不把一次中断后的缓存续作当冷样本。
- 原17次有效结果不重复运行；原件、源、二进制不变，无需重打包或重做隐私扫描。完成20次后再统一计算P95，并如实记录工具重连导致的测量时间间隔。

### 当前精确包的20次完整冷运行及三组热运行（2026-10-27 20:21 +08:00）

- `package-performance-recovery-final-20/matrix.json`：20/20完成，无右删失；每次完整使用固定10/10/12份原件，原件集合和源库digest前后不变，旧对照事实损失0、开放恢复问题0、研究模型调用0。原第18次工具中断记录另存，未计入20个完成样本。
- 冷运行混合中位数196.758秒、最大248.094秒；nearest-rank混合P95为241.453秒。仅适用于本机受控的茅台/冰轮/宁德混合样本，不是各公司单独P95、总体P95或99%公司识别结论。

| 公司 | 完成冷运行数 | 冷运行中位数（秒） | 第一轮事实数 | 热运行（秒） | 热运行事实数 |
| --- | --- | --- | --- | --- | --- |
| 茅台 | 7 | 198.094 | 324 | 7.547 | 342 |
| 冰轮 | 7 | 191.297 | 222 | 8.984 | 223 |
| 宁德 | 6 | 238.336 | 234 | 6.813 | 234 |

- 三个场景中位数均低于300秒，所有20次完整观察均低于600秒；该阈值只用于开发测量，不是达到时限就中断用户研究的产品门禁。20次冷运行测得的进程树采样RSS峰值338,919,424 bytes；这不是系统峰值或可选PP模型内存。
- 热运行的新增事实来自已存候选重解释/准入，不能要求其总数与冷运行完全相同，也不能把原诊断中“上次实际解析”的历史worker计数当成此次新启动次数。热运行是否真正未新开页面/OCR进程另以Windows Job Object累计进程核查，不把历史诊断清零伪造缓存命中。
- 当前包冻结金额核对`recovery-final-package-financial-values.json`：原QA70项加茅台当前/比较列13项，共83/83通过；逐格金额、币种、倍率、期间、合并口径和规定原页均检查，未降低财务门槛。
- 后续旧版同条件观察、取消恢复、热运行进程证明与最终输入冻结核对继续执行；这些项目完成前不将总体Acceptance改为PASS。

当前3B43A10C包第一轮源诊断（不沿用被撤销包的计时）：

| 公司 | 原件全文页数 | 唯一候选/完成页 | Native会话 | 新提取几何页/命中页 | 源收集/Compiler/准入墙钟秒 |
| --- | --- | --- | --- | --- | --- |
| 茅台 | 1,036 | 405/405 | 10 | 405/0 | 190.375 / 2.094 / 0.515 |
| 冰轮 | 1,173 | 453/453 | 10 | 453/0 | 177.219 / 6.625 / 0.297 |
| 宁德 | 1,750 | 464/464 | 12 | 464/0 | 224.250 / 1.390 / 0.250 |

计时取同批次绑定观察的最大值，不对重复附在各源上的批次时间求和；Native会话不包含另行全文文本提取的进程。全文上下文仍覆盖原件全部页，候选页来自共同目录及显式依赖；没有为达成耗时目标删原件、截断未知页或跳过同一compiler。

### 3.2.5同原件对照（2026-10-27 20:45 +08:00）

旧ZIP SHA-256 `D3493C344A830C4DF83E94954AD726492FABEC231FF2DEE31EEE13B03B6B8A5C`，以其精确解压sidecar执行相同正式retry RPC、同机器/电源方案、相同主体和完整10/10/12原件、全新应用缓存；不配置研究模型或可选结构组件。命令为上文`run-frozen-performance.py`，改用`--version 3.2.5 --trials 3 --name package-baseline-325-final --observer-timeout 600 --baseline-observation`，同时传入该旧ZIP和sidecar。原日志与结果见`package-baseline-325-final.log`及`package-baseline-325-final/matrix.json`。

| 场景 | 3.2.5冷运行观察（秒） | 当前3.2.6首轮冷运行（秒） | 比较约束 |
| --- | --- | --- | --- |
| 茅台10份 | 大于600，未完成 | 205.453 | 旧样本右删失，不能写成完整600秒或产品超时 |
| 冰轮10份 | 342.969 | 197.578 | 两版均完成，既有对照事实损失0 |
| 宁德12份 | 410.359 | 235.641 | 两版均完成，既有对照事实损失0 |

旧版仅两次完成加一次删失，不报告三个场景的完整中位数或P95，不拿两个完成样本的中位数冒充旧版总体。旧版暖缓存冰轮9.828秒、宁德7.766秒只作历史对照；最终新包的缓存无重复解析另有进程证明，不能由两版暖耗时相近推定。

### 热缓存的实际进程与持久页证明

独立执行TMP工具`verify-warm-session.py`，参数`--sidecar`为当前精确包sidecar、`--expected-sha256 377182D7E98A78C22443812425A537570760E70DE62B56C3D06BA4DC5E225DB9`。下表的seed均在`package-performance-recovery-final-20/`；输出目录均在本版TMP目录，含`proof.json`及独立`.log`，三项均退出0。

| 公司/seed | 有效proof目录 | 秒 | 未变持久receipt数 | 重试期间累计新增进程 | 财务/证据字段及模型调用 |
| --- | --- | --- | --- | --- | --- |
| 茅台/01-moutai-cold | `warm-process-moutai-proof-v2` | 5.640 | 1,302 | 0 | 字段不变，模型调用0 |
| 冰轮/02-binglun-cold | `warm-process-binglun-proof-v3` | 8.266 | 1,456 | 0 | 字段不变，模型调用0 |
| 宁德/03-catl-cold | `warm-process-catl-proof-v2` | 5.969 | 1,531 | 0 | 字段不变，模型调用0 |

Windows Job Object在任何业务RPC前绑定本次进程，使用累计TotalProcesses而非间歇采样，避免漏掉短命PDF/OCR worker。启动树与Job成员须完全相同；核对精确app及可选`SystemRoot/System32/conhost.exe`（本机Hash `E449BCE01F275CD08F3D4E64BB73B3B43AE845A0DBDB3E6131426E66537705E5`），重试后PID/image集合不变、累计新增0，退出后active0。此系统文件的嵌入式签名查询为NotSigned，本记录不提供其签名/系统安全认证；仅以路径、字节和启动成员区分控制台宿主与新增解析进程。

所有检查点/原始页JSON逐文件Hash前后相同；所有事实和补充事实比较时只排除可因候选重验推进的`generation_id`，不排除金额、单位、期间、证据或其他财务字段。历史parse observation计数保留原值，明确不是本次暖运行计数。

测量器历史失败均保留：初版错误假设启动仅1进程，随后严格同app路径断言定位到系统控制台宿主；取消v2的实际取消/续作完成后，末尾导出把dict当对象调用`to_dict`而退出1。另有冰轮v2误传不存在的seed路径，未启动sidecar。以上没有被改写成生产失败或悄悄覆盖；只修TMP测量器并用新输出重核，不修改生产源/包、不重复隐私扫描。

最终输入冻结复核：先备份manifest到`final-freeze-manifest-before.jsonl.gz`，再执行`python scripts/generate-release-snapshot.py 3.2.6 research-input-stability --generated-build-info tmp/research-input-stability-3.2.6/build-info-recovery-final.json`。`final-source-freeze-check.log`退出0，46,450项及SHA-256 `62A57B34E98285AEA43EEEF064807FF9DBB765414B2C4C6DC2DA9D398D8311F3`与备份完全相同。后续修改仅TMP测量器和本验收文档，均非构建输入；不使当前3B43A10C产物失效。

## 11. 最终取消续作、证据汇总及交付状态（2026-10-27，Asia/Shanghai）

取消检查命令：`python tmp/research-input-stability-3.2.6/verify-cancel-resume.py --seed tmp/research-input-stability-3.2.6/package-performance-recovery-final-20/01-moutai-cold --sidecar tmp/research-input-stability-3.2.6/exact-package-recovery-final/OpenThesis-3.2.6/bin/openthesis-sidecar/openthesis-sidecar.exe --output tmp/research-input-stability-3.2.6/package-cancel-resume-final-v3`，20:55:04完成、退出0，日志`package-cancel-resume-final-v3.log`及`proof.json`。

- 10份固定茅台原件、全新隔离应用缓存；积累4个持久页后取消，ACK在本次计时精度下为0.000秒，0.125秒达到cancelled。原有3个worker全部退出，进程集合只保留启动app/console成员。
- 续作96.187秒完成，324个事实，研究模型调用0；取消前、取消后、续作后4个页的路径/Hash映射逐项相同，已在proof保存原始映射，不仅比较计数。最终Job累计94个启动成员、active0、无强制终止成员；累计成员包含PDF全文/页worker和系统宿主，不能误写为94个native文档会话。
- 最终事实按来源、科目、起止期间、币种、合并口径、金额、倍率与第一轮冷运行逐项对照，无旧正确事实损失。20:56:10 `package-baseline-325-worker-cleanup-check.log`核对旧/新精确sidecar路径及三组旧样本路径相关进程均为0，没有泛杀用户进程。

最终只读汇总器校验命令（20:56:57退出0）：

```powershell
python tmp/research-input-stability-3.2.6/summarize-final-evidence.py `
  --matrix tmp/research-input-stability-3.2.6/package-performance-recovery-final-20/matrix.json `
  --baseline tmp/research-input-stability-3.2.6/package-baseline-325-final/matrix.json `
  --cancel tmp/research-input-stability-3.2.6/package-cancel-resume-final-v3/proof.json `
  --amounts tmp/research-input-stability-3.2.6/recovery-final-package-financial-values.json `
  --archive installer-output/OpenThesis-3.2.6-windows-x64-portable.zip `
  --baseline-archive installer-output/OpenThesis-3.2.5-windows-x64-portable.zip `
  --warm-proof tmp/research-input-stability-3.2.6/warm-process-moutai-proof-v2/proof.json `
  --warm-proof tmp/research-input-stability-3.2.6/warm-process-binglun-proof-v3/proof.json `
  --warm-proof tmp/research-input-stability-3.2.6/warm-process-catl-proof-v2/proof.json `
  --output tmp/research-input-stability-3.2.6/final-evidence-summary.json
```

汇总器实际校验：当前ZIP/sidecar、旧ZIP内sidecar、三组旧/新主体原件hash及期间集合、20冷/3暖结果、全部旧事实无损、83项金额、取消后旧事实及逐页Hash、三组暖进程累计计数/事实/持久文件和退出清理。`final-evidence-summary.json`的`developer_scope_pass=true`；不是仅凭子agent口头汇总写PASS。

文档与交付引用检查见`final-delivery-check.log`：18个要求行、关键文件引用、记录尾空格、产物字节/Hash、manifest Hash和本版范围`git diff --check`核对。一次额外命令误加`core.autocrlf=false`，把原有Windows CRLF视为尾空格；错误测量输出保存在`final-delivery-check-crlf-assumption.log`。按仓库实际`core.autocrlf=true`执行范围检查通过；没有改Git配置、重写源文件行尾或因此重打包。全局无关历史文档尾空格仍未擅自处理。

最终交付：

- 批准要求R01—R18均有当前有效证据。旧候选、红色用例、环境权限及TMP测量器失败记录保留；被撤销包的结果未计入最终分位数或当前包验收。
- 正常unsigned-test portable ZIP为`installer-output/OpenThesis-3.2.6-windows-x64-portable.zip`，SHA-256 `3B43A10C3D1B61D8E5F13D8107B8E3314D1233274F38378ABB699AE4F79A4552`。未对未变源码重打包、未对未变字节重复隐私检查，未Git提交/上传/发布。
- 20次普通文本型固定原件的本地财报识别达成批准耗时目标；**不等于包括下载、云排队和模型写作的整场研究均在5分钟内，也不等于所有财报字段永不缺失、真实研报质量或99%公司识别率已验证**。真实季度/时点缺口保持披露；无新增云端或研究模型调用。
- 本地PP新增正确字段为0，因此按批准方案保留实验入口，不把安装模型或能输出OCR冒充产品收益。独立用户QA、真实模型质量/费用和总体99%评价尚未进行，不影响本次已明确的正常测试包交付，但不得宣传为这些项目通过。
- Acceptance: PASS
- User Test: PENDING
- Upload Ready: NO
- Publication: PENDING
