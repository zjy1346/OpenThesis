# OpenThesis 3.0.4：原件身份、跨页证据与正式研究链修复

## 1. 身份与决策状态

| 字段 | 值 |
|---|---|
| Workflow name | `adaptive-research-gates`，承接既有工作流 |
| Target version | `3.0.4` |
| Decision approval | **APPROVED — 2026-10-15**：用户明确要求“进行3.0.4开发”，按既有约定即批准本文件现有A–F方案；不扩大云端、收费、发布或安全策略权限 |
| Development | IMPLEMENTED AND PACKAGED CANDIDATE；保留工作区既有改动，完整A–F验收未关闭 |
| Acceptance / User Test | PENDING / PASS（用户明确要求上传并发布；依照既有约定，该请求视为已完成本包用户测试） |
| Upload Ready / Publication | NO / PENDING |

本次由决策 Agent 编写，不修改生产代码、不调用用户模型、不启动研究任务。开发 Agent 获批准后依照 `workflows/DEVELOPMENT.md` 执行，在本文件追加实现与验证证据，不另建交接文档。

## 2. 依据与独立判断

已阅读 `docs/qa/independent/3.0.3QA.md` 全文、3.0.3 发布记录最新交付段，并核对相关源码。3.0.3 包 SHA-256 为 `B861B1ED7EAF71740A5D51B098A7A9F211979D9163D5A03643826E9DA59DD8BD`。包检查通过不等于完整研究通过，旧验收结论原样保留。

| 问题 | 证据及判断 | 本版本处理 |
|---|---|---|
| 五粮液金额被隔离 | QA 用正确原件 hash 复现；当前 `financial_statutory_currency.py` 仍限制名称至少4字，且要求信用代码和 CAS 声明同页。五粮液简称3字，相关声明分处第6/71页 | 确认整改；改为同原件、同主体的跨页证据组合 |
| 小米已有原件被拒用 | QA 身份复现 JSON 显示 PDF hash 正确；当前 `downloads.py:filing_identity_matches` 严格比较 provisional revision 和空/非空期间 | 确认整改；文件身份与元数据解析状态分离 |
| 小米正式下载后8/8解析失败 | QA 独立校验8份原件 hash，正式入口仍零事实；单份引擎探针曾产出23事实。超时、期间未确认等混合出现 | 必须解决的端到端故障，但准确根因待定位；不能直接认定 OCR 能力不足或单纯增加超时 |
| 报告投影异常 | QA 正式读取出现 `REPORT_PROJECTION_FAILED`；当前 `service.py` 外层异常降级未记录原异常 | 补齐本地诊断并重放失败快照，修实际异常；不能以安全报告返回成功判定修复 |
| 冰轮未完成研究 | QA 10/18义务 fulfilled、零财务事实；开发50格离线通过与正式入口结果不一致 | 追踪同原件在打包正式链的首个分歧，不把开发样本通过等同用户链通过 |

上述是 QA 证据与当前源码复核，不声称本轮重新运行了真实研究或复现所有异常。QA 的28项契约/fixture漂移仍须开发逐条对照已批准合同；环境回环阻断、无原生窗口、SEC配置缺失不作为产品缺陷。provider circuit 的归因保留待证。

## 3. 本版目标和边界

**用户结果：已有官方原件能稳定进入研究；小格式差异不会取消有效材料；局部失败可恢复且不清空其他章节；最终交付实质完整的研究。**

保留3.0.3已批准的模型中立、来源追溯、局部恢复、18项适用研究义务和已有分析保存策略。本版收敛为下述六项；不新建通用兜底框架，不整体重写 service/research，不同时接入新OCR供应商，不通过扩大默认上传、收费或无界重试解决问题。

“只能降级，不能清零”指故障不能抹去独立有效成果。被证实错误的具体事实必须撤销，并重新评估其依赖结论；保留旧版本及失效原因，不能为满足保留率继续展示错误结论为有效。

## 4. 开发方案

### A. 文件身份与知识补全分开

责任位置：`financial_sources/downloads.py`、`filing_selection.py`、`application/financials.py`，以及调用它们的存储/manifest适配。

1. 文件身份包含市场/发行主体、官方 document/accession 标识、来源地址、实际修订信息和内容hash；期间是否已解析、解析器版本、元数据置信状态分别表达。
2. `period_end_provisional` 是元数据状态，不能代表一版新PDF。兼容读取旧记录时转换到内部独立状态，不批量改写旧研究。
3. 官方发现缺期间、已验证原件含期间：允许以该原件的来源证明补全，不能清空 `local_path/content_hash`。双方明确且冲突：保留原件，标记待裁决，禁止直接复用该期间的金融事实。
4. 缓存可用于离线原件解析，不证明线上没有更新。同URL的新hash必须形成新版本；有官方更正标记不得被“元数据补全”覆盖。URL只规范化合法等价部分，不把大小写敏感路径或有语义的query随意删除/小写化。
5. 校验失败返回结构化原因（文件不存在、hash不符、主体冲突、实际修订冲突、期间待确认等）；调用方按原因恢复，不能统一抹掉原件再报“已获取”。

验收：小米原缓存与 provisional 发现可进入正式解析；错误主体、改hash、真实修订、明确期间冲突均不能被混用。读取旧缓存无破坏性迁移；在线更新与离线复用分别测试。

### B. 跨页证据组合替代同页字符串门禁

责任位置：`financial_statutory_currency.py`、`financial_sources/pdf_document_units.py`、`financial_validation.py`。

1. 在同一个已校验内容hash内分别采集报告身份、发行主体法定身份、CAS编制声明，组成带角色的证据集合；每项保留页码、原文、主体关联及hash。证据可跨页，不做全书拼接后无出处正则匹配。
2. 删除名称长度作为真实性条件。短简称必须通过原件中的证券代码/正式名称/明确简称关系消歧；不能只凭短词出现便匹配。子公司营业执照或编制声明不得替代发行主体。
3. 采集与compiler复核共用同一证据合同和纯验证逻辑。旧证明按版本读，新证明版本加入缓存身份；不能采集放宽后仍被旧“两条、同页”验证器拒绝。
4. 继续限定已批准的境内CAS年度法定报告范围；显式呈报币种冲突优先处理，单位倍率来自原件，不推倍率，不扩大到HK/US。不改变现有法规依据，本版不追加新法律推定。

验收：五粮液真实PDF金额恢复，同时原冰轮50格不退化；跨页/短简称/排版换行正例，错主体/子公司/不同PDF拼证/外币声明/错误单位负例。不得仅加“五粮液”特例或将最小字数从4改3。

### C. 让正式批量解析与单份解析使用同一可恢复执行合同

责任位置：`financial_sources/pdf.py`、`financial_sources/pdf_document_units.py`、`financial_ingestion.py`、`application/research_preparation.py`、`financial_recovery.py`。

**先复现，再选择内部修复。** 使用隔离副本与冻结原件，比较单份引擎、正式collector、正式sidecar冷启动/暖缓存四条路径；必须比较原件hash、元数据、解析器版本、worker启动结果、预算剩余、候选数、准入数，而非只比较最终报告状态。

当前collector在主解析前逐文档读取期间上下文，共用batch deadline。这是需排查的预算消耗点，尚不能据此宣称8/8失败根因。至少区分：预读耗尽预算、worker启动/打包依赖失败、期间解析失败、真正表格抽取失败。每类用原失败样本或故障注入建立可红可绿的正式调用链回归。

确定的执行约束：

- 同原件上下文与页面索引可复用，cache key含内容hash、解析配置和版本；不得预读一遍、主解析再无条件重复全书工作。
- 区分未调度、已启动超时、进程启动失败、无可识别目标表和已识别但待裁决；禁止全部编码成“识别不到”。
- 到期保存已完成的文档/表格检查点，失败文档独立终止；其他文档的有效候选仍可编译、写入。
- 重试从未完成工作继续，不能每次从列表首部重做成功项；取消/代次过期禁止写入当前头，保存的独立候选按既有身份合同恢复。
- 预算含排队、启动、hash、预读和解析，子步骤仅使用剩余预算。不以无限超时修复，也不把所有任务排队等待归因为解析器慢。
- 在原件正文已有时，来源型研究独立推进；财务事实更新只重新计算依赖它的阶段。没有来源材料不能以空章节冒充已启动有效研究。

验收：正式入口冰轮与小米原件不再出现“单份能识别、批量全零”的未解释差异；注入一份超时仍保留其他文档事实，重试完成缺失项且不重复成功解析/已完成模型请求。打包worker与开发进程分别验证。

### D. 投影失败须可定位、可本地修复

责任位置：`service.py`、`application/reports.py`、`report_projection.py`、`safe_report.py`及现有报告存储接口。

先在QA数据库副本重放小米失败的report snapshot，捕获首个异常类型、阶段和结构路径，最小化输入后修实际分支。当前未知异常根因不得预设为某个缺键或一律填空字典。

外层继续提供可读的有效章节，同时保存有界、脱敏的本地诊断（diagnostic_id、run/revision/generation、schema版本、异常类型和受控栈）。不记录密钥或完整prompt到通用日志。用户只看到清楚的失败范围与恢复操作。

采用既有报告节点合同；坏节点隔离，独立正常节点保留；解析/升级/投影失败不能改写或删除原始模型body。修复投影后本地重读即可恢复，不新增模型调用。旧扁平schema兼容应集中在现有入口转换层，不能在每个renderer补一套兼容判断。

验收：同一失败快照修复后正常read/export/restart一致；注入一个坏节点，其余节点及来源仍显示；零模型wire。安全降级仍发生时不能标记投影修复完成。

### E. 小错误与财务真值分层，避免又加一套门禁

沿用现有compiler为财务采用裁决入口，来源正文/修复观察可供模型分析。标点、空白、简称及非标准标签不应阻断整份报告；模型返回解释/原格定位，由宿主回原件重读。金额、单位、币种、主体、期间的真实歧义只暂停受影响数字的计算及依赖断言。

逐一检查A–D改动经过的下游：不得再用旧group失败标记清空已接受格，不得把财报不完整转成全部业务/风险章节不可见，不得把待核实提示计为完成义务。未知标签原文和来源应保留，而不是为了允许名单而丢弃。无需再增加第二套全局“可研究”布尔条件。

### F. 用真实链路差异收敛，保留质量目标

1. 冻结现有QA运行、失败body与原件hash；在隔离副本重放，不改用户数据库，不覆盖历史证据。
2. 冰轮50格逐格记录 `original→extracted→normalized→resolved→admitted→metric→rendered`，正式入口复用同一对照表；报告不使用的格标明用途，不能伪造rendered成功。
3. 五粮液为跨页证明回归；小米为HK发现/缓存/正式下载/批处理回归；再选未用于修复调试的公司/年度，检查是否只对上述样本有效。
4. 原件可读且已披露目标格为召回分母；同时统计错误采用，按金额、期间、主体、单位、币种联合判正确。区分未披露、不适用、不可辨认，不能从分母删掉解析失败格。
5. 继承3.0.3 R13的冰轮、华北制药、跨市场完整研究要求。18项适用义务须有实质分析、来源、推理及必要量化支持；不得改少义务或用免责声明填满。不适用须有具体理由，不能随失败改口。
6. 模型替代relay可验证编排与合同，但不能代表任意用户模型质量或实际供应商性能；记录输入量、wire数、耗时和重试原因。新增付费/云端调用仍受原授权边界约束。

## 5. 执行顺序与交付门槛

| 顺序 | 开发交付 | 必须提交的证据 |
|---|---|---|
| 1 | 固定失败基线及正式链诊断 | 原件/包/输入身份；A–D可复现命令；不能复现项明确缺失信息，不猜归因 |
| 2 | A、B语义修复 | 缓存身份与跨页证明红绿回归、负例、旧数据兼容 |
| 3 | C正式批处理修复 | 首个分歧定位、冷暖路径及包内worker对照、超时/取消/恢复保存结果 |
| 4 | D投影修复、E下游一致性 | 失败快照重放、坏节点隔离、重启及导出、零重复模型请求 |
| 5 | F质量对照与正常测试包 | 原格流向、完整研究结果、剩余缺项、构建输入绑定及SHA |

每项至少记录“原失败→源头原因→修改边界→同样本复测”。测试漂移要逐项核对合同并更新正确seam；保留保护意图，禁止仅改预期为当前输出。环境阻断单列，不能删断言制造全绿。

开发验证完成后按用户既有流程生成正常测试包供用户独立QA；不自动调用Antigravity、不上传发布。完整质量证据不足时如实交付测试候选并保留验收PENDING，不能把打包成功写成3.0.4目标达成。

## 6. 待决事项

本方案的范围与方向已由2026-10-15用户“进行3.0.4开发”批准。内部修复路径由开发依据复现选择，无需用户决定代码细节。若必须引入新OCR依赖、扩大云端上传或改变研究质量标准，回到本记录说明实证理由并请求单独批准。

本轮仅形成决策；没有运行新的产品测试，因此不新增PASS声明。

## 7. 开发启动与证据（持续追加）

### D01 — 批准与冻结诊断范围（2026-10-15）

- 本节开始承接开发；上方“本轮仅形成决策”为决策阶段历史，不改写旧QA/3.0.3失败结论。已完整读取AGENTS、DEVELOPMENT、CONTEXT、本方案、3.0.3QA及验收模板。
- 先固定A–D红灯：QA原件和数据库只读，复现/写入均使用工作区隔离副本；不调用用户模型或新云端、不自动运行Antigravity，不操作旧3.0.3ZIP。测试合同漂移逐项核对，不把环境回环/窗口不可用列为产品缺陷。
- 使用`diagnosing-bugs`先建立真实调用者红绿反馈；`codebase-design`用于复用既有身份/证据/worker/报告模块，避免新增并行门禁。原件视觉复核按需要使用PDF技能。
- 主Agent负责A–D复杂归因、改动方案和最终diff；项目`lunahigh`（GPT-6 Luna/xhigh）仅承接明确的机械整理、局部实现/测试或打包，不派生其他Agent，不与主Agent修改相同文件。

| 范围 | 影响 | 本版依据 |
|---|---|---|
| Production behavior | YES | 原件身份、证明组合、正式解析、报告读取 |
| Dependencies | NO（当前批准范围） | 复用既有AST/worker/已授权adapter，不安装新模型/OCR |
| Packaging/build configuration | YES | 3.0.4版本及实际构建身份 |
| Packaged output | YES | 正常未签名测试包，之后用户独立QA |
| User-facing workflow | YES | 正确来源复用、局部继续与投影诊断 |
| Acceptance metadata only | NO | 标准生产变更 |
| Selected path | STANDARD | 必要开发检查→正常测试包→用户独立QA；不发布 |

| ID | 对应已批准目标 | 当前证据/状态 |
|---|---|---|
| A | 原件身份与元数据状态分离 | 开发定向检查通过；真实缓存复用及冲突隔离已复现修复，发布包绑定待补 |
| B | 同原件跨页、短简称证据组合 | 开发定向检查通过；五粮液正式链17事实、含正确总净利润，发布包绑定待补 |
| C | 正式批处理与单份执行一致、可恢复 | PENDING；QA原HK身份冷/暖均131事实，冰轮正式入库50/50，包内worker对照见后续记录 |
| D | 实际投影异常局部修复、零新wire | 实际ZIP三次启动读取/导出旧失败快照，25章节、MD/HTML哈希稳定、投影诊断空，wire=0；研究本身仍partial |
| E | 下游局部准入/正文保留合同一致 | 比较期编译/入库角色、附注金额末位换行已修；完整模型研究质量仍PENDING |
| F | 冻结样本质量及完整研究对照 | PENDING；冰轮50/50不缩分母，不以局部回放或包成功冒充18项完整研究 |

源码manifest/base/SHA、3.0.4 ZIP及SHA、包启动/版本/关键流检查：PENDING，完成时补实际字节绑定。Intended publication files仅限本轮实际源码、测试、版本和记录，未授权Git提交、上传或发布。

### D02 — 已复现的红灯与首批修复（2026-10-15）

- `identity-red.xml`：真实小米哈希正确的原缓存丢失、URL路径大小写被错误等同、五粮液跨页证明未捕获，3 failed / 1 passed。前两项分离字节身份/元数据后修复；期间真实冲突保留原件但暂停相关金额采用，不重下同一份PDF。
- 五粮液原件SHA `09133e1f44b3bb4b2cebe211529ad68f5b04b6be10b43870f2a78c947d5910a4`，实际渲染复核p6（股票代码、正式名称、简称、信用代码）和p71（发行人CAS声明）。新增v2三个角色证据合同；v1验证分支保留，不修改旧proof正文。`basis-green-1.xml`：19 passed，尚需新增负例及正式原件采用复测。
- D真实异常已在QA原库只读备份重放：`report_document.py:1888 UnboundLocalError(all_financial_ids)`，不是猜测缺字段。修复分支初始化后，隔离小米快照`REPORT_PROJECTION_FAILED`消失，25章节可见，wire=0；仍为partial且`MODEL_RESPONSE_INVALID`，不得当完整研究通过。定向2 passed；相关43 passed。
- C先固定两条可重复红灯（`collector-red.xml`，2 failed）：整队列串行预读后才启动worker；同路径同大小同mtime的替换字节误用旧文本。实际8份小米冷collector（不启用checkpoint）98.187秒/131 accepted；与正式checkpoint配置仍在对照中，不能提前把旧QA零格归因于OCR或预算。
- 证据根目录：`tmp/adaptive-research-gates-3.0.4/`。QA原件/原库及3.0.3包保持不动；相关既有测试的旧元数据/诊断断言按新合同审查更新，保留原失败XML，不删保护意图。

### D03 — 冷暖路径首次实质分歧（2026-10-15）

- 同8份小米原件、默认120秒/文档与280秒/cohort不变：`cold-checkpoint-before.json`150.828秒/131条；取消串行预读并复用完整文本后`cold-checkpoint-after.json`117.235秒/131条。后续发现这组早期探针漏联`security_listings`而误用默认US身份，因此仅保留为开发差异探针，不列入正式HK验收或性能承诺；没有删页/缩小历史。
- **真实暖启动红灯**：`warm-checkpoint-after.json`0.703秒、cache-hit 8/8但accepted=0。定位磁盘JSON将`source_bbox`/`bbox`元组变列表，正式statement证据ID包含原坐标tuple表示，且准入要求同一物理坐标；并非财务字段真的不存在。`cache-hydration-red.xml`复现容器/证据不等价。
- 在领域对象恢复JSON容器为tuple，保留原坐标数值、原证据ID和全部金额/主体/期间校验。`cache-hydration-green.xml`5 passed；同一原暖缓存不删除、不重新解析，`warm-checkpoint-fixed.json`1.406秒/131 accepted恢复，与冷态原件/事实集合继续核对。
- 额外红灯`collector-identity-red.xml`：只按hash合并不同公告/修订候选。现在解释候选singleflight/dedup同时绑定既有parse-cache完整输入身份，原始文本仍可共享；不同doc/期间/edition不能串用候选。文本缓存增加实际内容hash和reader版本，覆盖保持size/mtime替换字节的负例。
- D补充只在技术视图输出受限异常类型/最后3个内部代码位置，不输出exception正文、局部变量、prompt或绝对用户路径。该本地读取仍无wire。
- 旧QA当次8/8解析失败的所有原因并未因此全部证明归属；本节只确认已实测的冷暖缓存源头缺陷。包内冷/暖、正式原格cohort、其余质量目标及独立QA继续PENDING。

### D04 — 正确市场身份与真实入库源头修复（2026-10-15）

- 更正回放器的公司加载：只读QA原库，联接`companies`与`security_listings`取得真实HK字段，不再以默认US代替。原库存储`accounting_standard=UNKNOWN`、初始呈报币种HKD，原样保留，不擅自标作IFRS；正式preparer仍须从披露裁决实际呈报币种。`hk-profile-cold-r2.json`127.500秒/131事实；同缓存`hk-profile-warm-r2.json`2.860秒/131事实。该时点为v3附注版本，后续v4需按新cache key重新验证，不能将旧计时绑定新包。
- 冰轮首次正确CN_A/CAS正式链仅匹配20/50格。源头一是发现接口的`limit`计公告、不计年度，中报挤出旧年报；现在仅在返回满页且年度不足时有界扩展元数据窗口，保留已有候选，最多3轮/128条，不猜年度、不额外调用模型。`discovery-window-red.xml`→`discovery-window-green.xml`。
- 源头二是同一比较期混入独立附注后，编译器把整组误作后来年度原报；源头三是Storage把已准入比较期统一写成`canonical_research`，遮蔽旧年度原报。现在编译角色绑定同原件的较早同型期间，写入保留比较角色；旧扁平记录仅在同事务、当前derived ledger的可用角色唯一为comparator时恢复读取语义，不改历史原行，不改金额/证据，随后仍走同一compiler。`comparator-role-red-2.xml`、`storage-role-red.xml`保留；修复后49/50。
- `SCHEMA_VERSION=19`：新增期间元数据列前沿用现有备份迁移机制，旧库不直接覆盖。

### D05 — 最后一格不是四舍五入，保留原文坐标恢复（2026-10-15）

- 冰轮2024原件SHA `522041655ccb76252413124516796c6f54256e82e0dcb0a8d749aac70783ac81`，正式合并利润表物理p59净利润`667,448,757.23`；p133现金流补充资料将该值打印为`667,448,757.2`及下一物理行的`3`，比较栏也同样拆行。旧附注解析把缺末位前缀当独立金额，制造真实冲突。已实际渲染核对`icewheel-2024-page133.png`，不修改容差/金额/gold答案。
- 新`numeric_continuations.py`只拼接相邻、左右两列均为一位小数加单个末位数字、垂直间距及列右缘明确对应的原始token。见证含两行原文/各格坐标；纯验证重算，拒绝伪造token与原文不一致、越界、错列、额外标签、非有限坐标及无法证明的前缀。不推断倍率、币种或缺失数字。
- 附注证据升级v4，保留v1/v2/v3读取；新语义进入原有parse/checkpoint cache key，不删除旧缓存。`note-wrap-red.xml`旧前缀误识别红灯；`note-v4-r1.xml`79 passed，覆盖正例、旧证明、缺见证与篡改负例。
- 真实p133新AST提取`667448757.23`；`formal-icewheel-v4-warm-r8.log`正式preparer→compiler→独立Storage→active view→报告恢复196事实、冻结50/50格，7.125秒（已生成缓存，不是冷态）。首次v4冷态输出在128.156秒完成解析/校验事件后无完整结果，保留`formal-icewheel-v4-cold-r7.log`，不计为冷态通过；后续缓存回放才具有完整退出0证据。
- 五粮液v4正式回放`formal-wuliangye-v4-r3.log`54.015秒/17事实，物理p57总净利`9,317,113,184.68`、归母`8,954,257,202.51`、少数股东`362,855,982.17`各自保留，禁止用归母代总净利润。
- 最新核心定向回归`final-core-r1.xml`166 passed/16.77秒；二级`final-secondary-r2.xml`144 passed/5.21秒。不同集合有交集，不相加宣称独立覆盖数；`final-secondary-r1`为一次测试文件路径错误、未执行测试，保留该记录。执行环境Windows/Python3.12，隔离tmp，未访问模型/网络。
- 旧structured-priority测试将1000元合成行绑定真实宁德时代原件，与PDF原格冲突；按批准的compiler唯一裁决合同，此冲突必须隔离，不新增“结构化永远胜出”优先级。测试修订保留冲突保护与structured失败后真实PDF可用两种场景。
- 完整18项来源型/财务型研究义务、供应商真实综合质量和独立QA仍PENDING；本轮未以新模型调用或替代relay补足，不把确定性财务报告称作完整公司研究。正常测试候选打包不等于上述目标全部通过。

### D06 — 逐层轨迹、测试合同及构建冻结（2026-10-15）

- `formal-icewheel-stage-trace-r9.json`（在`formal-icewheel-v4-cold-r7/`隔离目录）：collector真实采集50/50、normalized/resolved50/50、正式准入50/50、metric50/50、rendered40/50。未作为独立汇总栏显示的为5年`operating_income`及5年`capital_expenditure`，保留其指标用途与来源，不伪称50格都进入可见独立栏目。冻结矩阵SHA仍为`63d76d82ad707d77aaf8213a585fda67f897f9a3cc129eeba543a23cc1ee1713`。
- 茅台holdout最新v4正式链：`formal-moutai-v4-r2.log`23.343秒、34事实。这里只证明样本非回归，不声称总体覆盖率。
- `storage-final-r1.xml`56 passed / 3 failed：三例仍要求坏lineage导致整份snapshot抛错，属于与既有“坏行隔离、健康兄弟保留”合同冲突的旧断言。修订后同时断言坏行不准入、精确诊断保留、健康行保留、原坏字节不被改写及单行decoder仍严格抛错；并非删除fail-closed保护。
- `contract-final-r2.xml`13 passed / 51.62秒：上述lineage/row-isolation集合及真实PDF/合成structured冲突、structured失败后PDF可用两条保护全通过。原`three-failures-r8.xml`、`storage-final-r1.xml`不删。先前五粮液及service两例已通过，两个provider实例经角色核对为不同研究lane，不是重复stage wire。
- Luna后续执行停于`pending_init`，未运行打包；主Agent明确interrupt并通知停止，接管测试维护及构建以避免重复执行。没有创建第二个子Agent或改主模型。
- 生产冻结base `67c264c0eed55b96fbd02112bcfd4370611fb359`。打包前manifest45499项，SHA `5b9c51e2c9977d45187a990fe15711555a30070a9f5212bd7f2941efedfea5cd`，副本`tmp/adaptive-research-gates-3.0.4/inputs-before-package.jsonl.gz`；生成命令`python scripts/generate-release-snapshot.py 3.0.4 adaptive-research-gates`。枚举规则见脚本：src/desktop/build_support/scripts/.build-tools/.cargo全树及声明根文件/后缀，含未跟踪构建输入和显式删除项；仅排除脚本列明的生成产物、cache及开发build-info占位。测试/证据tmp/版本记录不为构建输入。构建完成后须补物化build-info并与冻结普通输入对照，不能拿开发占位充当实际编译身份。
- 正常构建命令`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package-desktop.ps1 -SignatureMode unsigned-test`，完整日志`tmp/adaptive-research-gates-3.0.4/package-r1.log`；开始前核对构建/portable/resource-bin等绝对目标均位于工作区且无reparse，旧3.0.3 ZIP不动。只执行既有脚本一次隐私/runtime/portable检查，不额外重复扫描；无Git提交、上传或发布。

### D07 — 可复执行的开发检查操作清单

环境：2026-10-15，Windows，Python3.12；`PYTHONPATH=src`，`TMP/TEMP=D:/githubmax/tmp`。下列pytest均加`-q`、独立`--basetemp=tmp/adaptive-research-gates-3.0.4/pytest-<对应组>`及`--junitxml=tmp/adaptive-research-gates-3.0.4/<对应证据名>.xml`，stdout/stderr同名`.log`。调用普通用户权限执行器绕过开发沙箱临时目录DACL问题，未更改ACL/安全软件。

| 证据名 | `python -m pytest`后的明确文件/节点集 | 结果 |
|---|---|---|
| final-core-r1 | `tests/test_filing_original_identity_304.py tests/test_filing_identity_assessment_304.py tests/test_filing_metadata_persistence_304.py tests/test_filing_acquisition.py tests/test_filing_selection.py tests/test_statutory_cross_page_304.py tests/test_statutory_presentation_currency_303.py tests/test_collector_context_schedule_304.py tests/test_parse_cache_roundtrip_304.py tests/test_financial_checkpoint.py tests/test_checkpoint_isolation_failure.py tests/test_filing_discovery_window_304.py tests/test_comparator_role_isolation_304.py tests/test_financial_compiler.py tests/test_financial_publication_cas.py tests/test_report_financial_context_304.py tests/test_report_diagnostic_safety_304.py tests/test_wrapped_note_proof_304.py tests/test_numeric_continuations_304.py` | 166 passed |
| final-secondary-r2 | `tests/test_filing_identity_assessment_304.py tests/test_market_data.py tests/test_financial_recovery.py tests/test_scoped_note_disclosures.py tests/test_scoped_note_revenue.py tests/test_note_table_continuation.py tests/test_recovery_candidate_payload.py` | 144 passed，含最后supersedes标识大小写保护 |
| contract-final-r2 | `tests/test_financial_lineage_storage.py tests/test_storage_row_isolation.py tests/test_financial_ingestion_engine.py::FinancialIngestionEngineTests::test_conflicting_structured_amount_cannot_override_real_pdf_source tests/test_financial_ingestion_engine.py::FinancialIngestionEngineTests::test_failed_structured_source_keeps_independently_proven_pdf_facts` | 13 passed |
| note-v4-r1 | `tests/test_wrapped_note_proof_304.py tests/test_numeric_continuations_304.py tests/test_scoped_note_disclosures.py tests/test_scoped_note_revenue.py tests/test_note_table_continuation.py tests/test_comparator_role_isolation_304.py` | 79 passed |

- AST检查：`src/openthesis`全部139个Python模块使用`ast.parse`，`AST_OK 139`；不替代语义回归。相关变更`git diff --check`通过，CRLF提示不算失败。
- 正式冰轮轨迹命令：`python tmp/adaptive-research-gates-3.0.4/replay_formal_sources.py --cohort icewheel --label formal-icewheel-stage-trace-r9 --reuse-label formal-icewheel-v4-cold-r7`，9.157秒、50格一致、退出0；`extracted_sources`来自collector实际输出，不再把compiler后已过滤集合称为最初提取。
- 五粮液：同脚本`--cohort wuliangye --label formal-wuliangye-v4-r3 --reuse-label formal-wuliangye-r2`；茅台：`--cohort moutai --label formal-moutai-v4-r2 --reuse-label formal-moutai-holdout-r1`。只读冻结原件/QA源库，无模型/网络。计时仅为本机该调用，非产品总研究时间保证。

### D08 — 一次正常测试包及实际ZIP关键流（2026-10-15）

| 项目 | 实际绑定 |
|---|---|
| Artifact | `D:\githubmax\installer-output\OpenThesis-3.0.4-windows-x64-portable.zip` |
| SHA-256 | `3F38D52E0FA408203841628332EC960E60E72941B45767EC53EC73A08B15B5F9` |
| 大小 | 54,818,036 bytes |
| 签名模式/状态 | `unsigned-test` / `NotSigned`；不宣称已解决信誉/签名提示 |
| Build base | `67c264c0eed55b96fbd02112bcfd4370611fb359` |
| Build time UTC | `2026-10-15T14:26:44Z` |
| Build ID | `df644d63ed0e12330f534809c18950a0c8ba6ba24d20aa422c9927ecbdf414a3` |
| 最终输入manifest | `docs/releases/adaptive-research-gates-3.0.4-inputs.jsonl.gz`，45501项 |
| Manifest SHA-256 | `081452fcbb2332fdd8ee79174f19506f4b5a8388746db56db409e08a7d1e60d8` |
| Source-package binding | 45499普通输入逐项不变，仅新增两份实际物化build-info；两份SHA均与ZIP内字节一致；`source-package-binding.json` PASS |

- `package-r1.log`退出0：sidecar、前端构建及Rust release完成；现有桌面启动3/3、持久报告重启检查通过；一次隐私检查`ForbiddenDataEntries=0`、`CredentialOrPersonalDataMatches=0`；ZIP Windows GUI/必要运行时/校验和通过。Vite体积及Rust unused/linker提示保留，未抹去日志。不重复构建。
- 实际ZIP解压后运行`python tmp/adaptive-research-gates-3.0.4/packaged_report_smoke.py --archive installer-output/OpenThesis-3.0.4-windows-x64-portable.zip`：QA原库只读backup后由包内正式JSON-RPC读取小米原失败run `2c4a9729cd6048ee967d714f2332c0b0`。三次进程启动均退出0、版本3.0.4、25章节，`report_read_diagnostics=[]`；MD SHA `44c13b3bb6201c5149a423feae97af1eb24b5cfc01e9a5b6a4314f350dc18b74`、HTML SHA `cdd828448ae2a3148ad617964b1560f3d7c9c5d1d342d857e9210e48df9894ac`三次一致。原QA DB字节未变；wire=0。原研究partial不改为completed。
- C包内worker：`replay_cohort.py --label packaged-xiaomi-v4-cold-r1 --checkpoint --worker-executable <上项ZIP解压后的openthesis-sidecar.exe>`，原8份PDF、无删页/换原件，131事实、93.110秒；`--label packaged-xiaomi-v4-warm-r2 --checkpoint --cache-label packaged-xiaomi-v4-cold-r1 --worker-executable <同一exe>`，131事实、1.110秒，原件hash与完整事实数组严格相同。
- 该worker对照的驱动器为开发Python，明确模拟冻结spawn envelope并隔离child的sys.path；probe证实`sys.frozen=True`、`financial_ingestion.__file__`来自ZIP `_internal`、Build ID与上表相同，非偷用src。它验证的是实际包内native/window worker，不冒称完整侧边栏发起研究或真实供应商质量验收。
- 同缓存继续运行正式`OfficialFinancialPreparer→Storage→active view→report`（`formal-xiaomi-v4-r3.log`）：2.140秒、132个研究视图事实、model_ready=True，上市币种HKD保留、呈报币种依据披露归为CNY。兼容`engine.ingest.accepted_facts`的131与最终`view.facts`的132是不同投影，不能只凭总数声称逐格同义；正式结果/各组角色和原件来源保留供独立核对。年度2021–2025指标可见，但2026 H1仍仅有部分现金流，收入/同比等不完整，未用猜值补足。
- 旧3.0.3 ZIP SHA复核仍为`B861B1ED7EAF71740A5D51B098A7A9F211979D9163D5A03643826E9DA59DD8BD`，未替换或删除。开发build-info占位已由正常脚本恢复，编译身份来自ZIP，不取占位。

### D09 — 交付范围、未完成项与状态

本轮生产修复涉及：`domain.py`、`storage.py`、`financial_contracts.py`、`financial_compiler.py`、`financial_ingestion.py`、`financial_recovery.py`、`financial_statutory_currency.py`、`disclosure_index.py`、`filing_selection.py`、`market_data.py`、`application/financials.py`、`financial_sources/{downloads,acquisition,pdf,note_disclosures,numeric_continuations}.py`、`report_document.py`、`service.py`（均在`src/openthesis/`）。另外为3.0.4版本元数据、针对上述接缝的测试、此记录及最终输入manifest；未来上传必须先按独立上传流程审查，不能把当前全部dirty历史改动归为本Agent独有。

**仍未满足最终验收的内容：**

1. 完整18项适用义务的真实模型公司研究尚无本轮完整结果，旧partial/invalid body未重写成“已完成”；未使用新模型/云端上传替代验证。
2. 小米中报财务仍有缺项；上述正式视图与兼容API输出之间尚未完成所有源格逐项对照。C/F不能仅凭“131/132、非零事实”标PASS。
3. 旧QA全部28项合同/fixture漂移、provider circuit未决归因及供应商质量仍需独立收敛。本轮只对有明确证据的条目修复或更新合同，未对其余问题宣称已通过。

因此本次交付**正常未签名测试候选**，不是最终质量验收或发布：

- Development: IMPLEMENTED AND PACKAGED CANDIDATE；完整A–F验收未关闭。
- Acceptance: PENDING
- User Test: PENDING
- Upload Ready: NO
- Publication: PENDING

精确候选SHA见D08。用户独立QA应使用该ZIP；未收到绑定该SHA的用户PASS及独立发布授权，不提交Git、不上传或发布。A–F未完成项原样保留，不用package成功覆盖失败历史。

### D10 — 用户上传授权（2026-10-15）

- 用户明确要求上传并正式发布3.0.4，并重申其既有约定：提出上传即表示已完成本包测试并授权上传。因此本记录将 `User Test` 记为 `PASS`。
- `Acceptance` 仍为 `PENDING`、`Upload Ready` 仍为 `NO`：D09列出的完整研究质量、财务逐格对照与QA遗留仍未关闭；不把用户上传授权改写为开发验收通过。
- 用户要求按当前授权继续进行GitHub代码提交、合并与正式Release；发布说明须如实概括3.0.0–3.0.4累计变更，并注明记录中保留的未完成验收项。
