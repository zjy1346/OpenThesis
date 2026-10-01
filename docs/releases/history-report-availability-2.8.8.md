# OpenThesis 2.8.8：恢复历史报告阅读，隔离恢复功能故障

| 项目 | 状态 |
|---|---|
| 工作流名称 | `history-report-availability` |
| 目标版本 | `2.8.8`，承接 `2.8.7` |
| 决策批准 | `APPROVED`；用户于 2026-10-01 明确要求按本交接文档开发 |
| Development | `COMPLETE`；2.8.8 实施、全量回归、包构建及包内持久化读取验收均已完成 |
| Acceptance / User Test | `PASS` / `PASS`（用户已完成验证并批准上传） |
| Upload Ready / Publication | `YES` / `PENDING` |

## 本版必须交付的结果

用户更新到测试包后，可以直接打开原有历史研究，包括已完成和中断的研究；阅读不要求重新研究、重新配置模型或再次付费。报告正文、研究完整性与可执行恢复动作各自有明确状态。恢复规划器出错可以暂时禁用继续动作，但不能使已有报告消失。

保留研究质量：只把通过相应事实/证据校验的内容呈现为可信研究；部分通过的成果按条目或章节保留，说明缺口。标点、样式、可选字段与恢复提示故障不能阻断阅读。没有可信章节时不得用标题、免责声明、artifact 清单或大量占位段落冒充报告。历史原文及其验证记录保留，不能因升级覆盖、删除或自动升格。

本版优先完成报告读取链路的局部重构，同时闭环 2.8.7 已确认的展示契约与相关测试失败。财务解析器全面重写、重新运行所有真实公司研究、界面风格重做、GitHub 发布不在范围内。2.8.7 尚未完成的真实研究验收不会因本版而自动通过。

## 已确认的原因与证据边界

已阅读 [2.8.7 QA](../qa/2.8.7qa.md)、[另一份独立 QA](../qa/independent/2.8.7QA.md)、[2.8.7 实施记录](report-integrity-and-resume-2.8.7.md)，并检查当前源码、用户截图与 QA 数据库快照。当前 checkout 的 HEAD 为 `74f5af09a2cbd9a1380d4679b81cfc7b39255743`，工作区有大量已有未提交改动；该 HEAD 不能单独代表测试包的全部源码。

| 发现 | 本轮独立验证 | 决策 |
|---|---|---|
| 所有历史报告无法打开 | 对 `D:\test_qa_287\openthesis.db` 使用 SQLite `mode=ro&immutable=1` / `query_only=ON`，绕过初始化与迁移，调用真实 `AppService.get_report()`。12/12 失败，包含 8 个 `partial` 和 4 个 `completed`；均为 `ResearchRecoveryPlanner.BASE_AGENTS` 不存在的 `AttributeError`。 | `P0`。修正错误依赖，同时让恢复信息退出报告阅读的必经成功条件。不能只修中断任务。 |
| 报告尚存，而恢复信息装配使整个请求失败 | `_recovery_failure_code()` 无条件构造 aliases 字典，即使 target 为 `auto`、没有任何 artifact，也先访问不存在的属性。它在正文渲染的异常保护之外，由 `get_report()` 返回字典时执行。 | 修复职责耦合；恢复 Module 自己解释角色、别名和失败原因，读取方不读取其内部常量。 |
| 阶段正文仍被界面隐藏 | 仅在诊断进程内临时补上该属性后，12/12 返回非空正文；NVDA 返回 5,693 字，readiness=`action_required`，`substantive_sections=[]`，无 `visible_sections`。`ReportWorkspace.tsx` 仍据此隐藏全部正文。 | 从同一 `ReportDocument` 产生可阅读章节，完整性不能作为正文显示开关。临时属性补丁没有落入源码，也不算修复验收。 |
| 加载失败被误报为没有历史报告 | `useWorkbenchSession.ts` 把异常收敛成不带 run 身份的 `report-unavailable`；`App.tsx` 在 report 为空时显示“还没有研究报告”。截图与该路径一致。选择历史项时也没有请求代次校验，可能保留上一个公司的正文或接受晚到的旧响应。 | 建立带目标 run 的读取状态与请求身份；分别显示历史为空、加载中、读取失败和已读取但缺少研究内容。 |
| QA 的财务根因结论不成立 | NVDA 快照的六个年度 `metrics[*].free_cash_flow` 均为 `null`，没有 FCF 的 `money_metadata`；其市场快照持久化标为 `VERIFIED`。不能从一句单位错误提示认定“FCF 数值已算对，只差标签”。 | 先分清缺数、缺单位和快照失配；不得通过填 `unit_provenance` 造出可估值金额。旧 run 未重算，不能据此证明 2.8.7 新生成链路仍有相同缺陷。 |
| QA 对阶段质量有过度表述 | 三个 `agent-analysis` 均为 `completed_partial`、整体 `verification.passed=false`；增长 artifact 的 `_validation` / `_audit.verification` 为通过。财务快照持久化标为 `VERIFIED`，实际渲染 FY 2021–2026，非 QA 所称 2020–2025。 | 从已筛选的 `verified_analyses` 和逐条证据状态取可信内容；不能把“三个备忘录存在”解释成三个备忘录全部可信。字符数只说明非空，不说明研究充分。 |
| 全量测试与定向验收不一致 | 两份 QA 的测试工具、范围和数量不同：一份报告 850 项中 10 Errors + 1 Failure，另一份报告 861 项中 12 失败。共同确认读取异常；另一份还记录 Markdown 部分就绪度断言失败。 | 分别保留统计，不拼成虚假的统一结果。测试包须增加持久化历史读取验证，不能只检查 hello/bootstrap。 |

快照主文件 SHA-256：`04635503004186A39614DC81E9A7B8D59303D6E95909282629EFB072E7D2855B`。两次只读重放前后哈希均一致，没有调用 provider 或运行研究。`immutable=1` 仅用于已保存的静态 QA 快照，不能用于活动中的用户数据库；开发取活动库样本必须用一致的 SQLite backup，不能仅复制忽略 WAL 的主文件。本轮没有在用户安装的 GUI/sidecar 上完成修复后验证，因此不能声称软件已恢复可用。

最小确定性复现，在仓库根目录执行；已实跑，退出码为 1，抛出上述 `AttributeError`：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -c "import sys; sys.path.insert(0, 'src'); from openthesis.service import _recovery_failure_code; print(_recovery_failure_code([], 'auto'))"
```

开发应把红灯锁在公开 `research.get_report` 的真实路径上；上面的最小命令用来定位原因，不能替代历史报告回归。

## 实施方向一：让报告读取成为稳定的 Interface

沿用现有 `ReportService`、`ReportDocument` 与格式 renderer，不再引入第二套报告引擎。扩展 `application_services.py` 中的 `ReportService.read(snapshot) -> ReportReadResult`，把同一输入的文档装配、格式输出、可见章节和局部诊断集中起来。`AppService.get_report()` 负责读取持久化快照并组装返回值，不再独立推断正文状态。

```text
持久化 run + 选定 report revision + 对应 artifacts
  -> ReportReadSnapshot（run/revision/input generation 身份固定）
  -> ReportService.read(snapshot)
  -> ReportDocument + markdown/html + visible_sections + read_state
  -> research.get_report -> 桌面 / 导出

同一快照 -> RecoveryPlanner.describe(...) -> recovery_plan 或 unavailable 诊断
同一快照 -> financial status / continuity -> 独立状态或 unavailable 诊断
```

**故障分界必须具体：** run 不存在、数据库无法读取、主体身份不匹配属于真正读取失败，返回分类错误，不伪造成功报告。单个可选章节装配失败隔离到该章节；恢复规划、恢复错误提示或非正文诊断发生程序异常时，正文返回，相关功能置为 `unavailable` 并记录诊断。完整性评估异常时采用保守的 `complete=false` / `quality_status=unknown`，保留能验证的章节；不得沿用未经确认的“完整”标签。不得在整个 `get_report()` 外面增加一个吞掉全部异常的 catch，再返回空白 Markdown。

`ResearchRecoveryPlanner.describe()` 在 planner 内部完成别名解析、节点失败分类和计划构建，返回完整的恢复视图。`RecoveryPlan` 可扩展 failure / reason 信息，避免 `service.py` 再维护一份角色字典。错误常量引用在第一步修正；本版最终应删除服务层 `_recovery_failure_code()` 的重复映射，通过 planner Interface 取得结果，不能让这个临时修正成为第二套长期判断路径。

有界异常隔离只放在可选信息装配的明确位置，保留错误类型、阶段、run/revision 身份和诊断 ID。日志不暴露模型凭据、完整 prompt 或用户本地路径。恢复规划不可用时禁止执行按钮，提供重试读取/诊断入口；不能返回一个可执行但缺少输入身份的空 plan。阅读本身不触发研究、重算、下载、模型调用、attempt 写入或 report revision 更新。

正文渲染失败时，优先按已通过校验的独立章节生成阶段性内容。现有 `SafeReportAssembler` 只列 artifact 名称，属于 `diagnostic_only`，不能称为实质研究、不能计入可见研究章节或让完整导出通过。旧格式缺少新字段时由已知格式 Adapter 做只读适配；不写回历史 payload，也不猜测币种、期间或验证结果。

## 实施方向二：可阅读章节与完整性分开计算

在 `report_document.py` 的 `SectionBlock` 中记录章节来源 artifact/revision、验证等级和是否包含实质节点。当前 `_section()` 只要 heading 有文字就赋 `state="available"`，不能用于判定实质内容。表格须有有效事实单元格，论点须有通过相应校验的文本/证据，机会须经过对应结构与证据检查；标题、状态提示、免责声明、占位段落和技术清单不计入实质章节。

返回值使用以下合同，可按现有 dataclass / TS type 落地；新增字段由新后端统一生成，不要求历史库补写：

```text
ReportReadResult {
  report_contract_version,
  run_id, revision_id, input_snapshot_hash,
  read_state: ready | partial | diagnostic_only,
  visible_sections: [{section_id, title, verification_state,
                      source_artifact_ids, is_substantive}],
  report_readiness, markdown, html,
  read_diagnostics,
  recovery_plan: {state: available | none | unavailable,
                  available, target?, stages?, plan_hash?, reason, error_code?}
}
```

`visible_sections` 从**实际装配完成且获准显示**的节点投影，不是把 `readiness.substantive_sections` 换个名字，也不是 `len(markdown)>0`。优先消费 dossier 中的 `verified_analyses`；兼容旧 `analyses` 只有对应版本合同能证明已筛选时才进入可信正文。部分通过的 artifact 保留其已验证条目，未验证推论必须明确分级；证据失效、金额不明的条目不能因“恢复显示”升格。旧格式无法重建验证状态的原始历史文本可以在“原始历史记录（验证状态无法确认）”中按已知格式读取，不进入可信摘要或 `complete=true`，不做任意 dict 全量展开。

桌面以可见实质章节决定正文显示，以 readiness 决定“完整/阶段性/质量待确认”标签。删除 `noSubstantiveReport` 对 `readiness.substantive_sections` 空数组的整页阻断。缺失章节汇总到一张缺口卡片，避免为每个缺口重复输出长占位段落。正文、Markdown 与 HTML 导出均来自同一份文档与章节分级；有可信成果时允许显著标记“阶段性”的导出，只有诊断内容时按钮明确为诊断导出。

保留当前已验证事实、单位、期间、币种、引用门禁。放宽的只是不影响事实的展示问题，以及把不完整研究整体清空的门禁。UI 接到缺少新合同字段的响应时应识别核心版本/合同不匹配，给明确提示；不能退回“只要正文长就算可用”。既有 IPC 2.0 无须因兼容新增字段整体改版，但本版包内前后端必须匹配并通过真实响应验证。

恢复判断与历史阅读使用各自正确的身份：正文可阅读选定的持久化 revision；继续动作只能基于当前 canonical 输入与有效 plan hash。历史原文没有新 audit 字段不等于新发生模型失败。只因字段格式不同而无法证实可复用性时，显示“历史恢复兼容性待确认”，不能生成默认付费重跑建议。本轮发现一条 readiness=`complete` 的旧记录仍得到 counter-analysis plan；开发须核对该记录的 stage/revision lineage，不能单凭 run.status=completed 屏蔽真正后续失败，也不能凭旧字段缺失制造新失败。

## 实施方向三：界面准确表达所选历史报告的状态

`useWorkbenchSession.ts` 引入统一 `ReportLoadState`，至少有 `idle`、`loading(run_id)`、`loaded(run_id, report)`、`failed(run_id, code, diagnostic_id)`。历史库成功返回零条时才出现“还没有研究报告”；读取失败则显示目标公司/研究时间、失败原因、重试读取和返回历史列表。加载失败页不能诱导用户通过运行合成演示或重新研究来恢复历史报告。

每次启动默认读取、选择历史项、切换报告语言、财务刷新与研究完成后读取都携带目标 run ID 和请求代次。只接受仍属于当前选择且 `response.run_id` 一致的最新响应；快速选择 A→B 时，A 的晚到响应和错误不得覆盖 B。切到 B 后 B 读取失败不能显示 A 的正文；同一 run 的刷新失败可保留该 run 最近已读取的正文，并明确“刷新失败，当前显示此前版本”。关闭重启后从持久化历史列表选择，不能依赖 `lastRequest`。

保留 sidecar 返回的分类错误。至少区分 `REPORT_NOT_FOUND`、`REPORT_STORAGE_UNAVAILABLE`、`REPORT_IDENTITY_MISMATCH`、`REPORT_PROJECTION_FAILED` 与核心合同不匹配；UI 不展示堆栈/敏感参数。恢复元信息失败不应再走 RPC `internal error` 导致整份报告不可读。真实存储故障仍明确失败，不能伪装为没有历史。

## 对 QA 剩余意见的具体处理

| 项目 | 2.8.8 的处理 |
|---|---|
| FCF / 反向 DCF | 本版修正缺口分类：数值缺失显示所缺 FCF/CapEx 输入，金额有值但无单位显示单位缺口，期间/范围/币种不匹配分别说明。当前代次由持久层提供；入库前的 PDF 解析仅在申报编号、解析器版本、原始文档标识三者完全相同时视为同一来源批次。官方美团 2022 年报证明此前空缺源于两条同报表已验证事实缺少持久层代次，而非缺值；恢复 OCF−CapEx typed 派生。不同代次/来源仍拒绝计算。打开历史不自动重算、不补标签；旧 NVDA run 的 canonical facts 另按本记录范围检查，不能以其他公司的样本代替。 |
| 并发测试 `operation()` 不接参数 | 产品调用合同已是 `Callable[[RecoveryPlan], ...]`。让测试替身显式接收并断言同一个 `RecoveryPlan`，不用 `Any=None` 掩盖合同变化。补验真实重试入口的相同模型去重、不同模型竞争拒绝、失败后锁释放与报告读取；不能仅修签名就声称并发行为已通过。 |
| Markdown 缺少部分就绪度文案 | 对 Markdown、HTML、桌面核对同一状态和实质正文；保留清楚的本地化“阶段性”提示。测试若绑定了旧模板中的一段措辞，改为验证状态与缺口含义，但不能通过删除断言、隐藏状态或只检测字数转绿。 |
| 重复资产负债表、全空权益列等旧问题 | 已存 NVDA 原始展示仍有这些问题，说明读取成功不等于呈现合格。沿用已批准的类型化财务投影和非空列规则，检查新阅读路径是否实际调用它；从源节点修正本版读取中出现的重复/全空列，禁止修改 QA 整理稿冒充程序修复。不在本版另起 renderer。 |

## 开发顺序、文件责任与退出旧路径

| 顺序 | 改动范围与可交付证据 |
|---|---|
| 1. 锁定原始红灯 | `tests/test_service.py` / `tests/test_sidecar.py`：在公开读取入口保存完成、partial、无最终 artifact 的持久化样本。用一致且脱敏的 QA 快照验证 12 条历史；保留报告内容与数据库摘要。禁止修改用户原库做复现。 |
| 2. 恢复可用性 | `research_recovery.py`、`service.py`、`application_services.py`：修错误依赖，将恢复描述集中到 planner，建立 `ReportService.read` Interface，隔离可选元信息异常；公共 RPC 读报告应从红变绿。删除服务层别名复制路径。 |
| 3. 章节合同落地 | `report_document.py`、`safe_report.py`、`reporting.py` / `report_html.py`：同一文档输出真实 visibility、分级与局部缺口；旧 wrappers 委托同一装配路径，不留下另外一套完整性/可见性判断。对源节点修重复财务表/空列。 |
| 4. 桌面与协议接通 | `types.ts`、`protocol.ts`、`backend.ts`、`useWorkbenchSession.ts`、`App.tsx`、`ReportWorkspace.tsx`、`i18n.ts`：带身份的读取状态、异步响应防串、明确错误/重试、阶段阅读与导出。删除“report 为 null 就历史为空”和空 completeness 清正文的旧判断。 |
| 5. 定向债务闭环 | `financials.py` / 财务投影、并发恢复测试、readiness 测试：按上表证据修分类与合同，禁止为了绿色测试更改事实验证结论。 |
| 6. 测试包验证 | `scripts/verify-desktop-runtime.ps1` / `scripts/verify-desktop-portable.ps1`：用隔离数据目录，执行实际 demo 研究→`research.list → research.get_report`→关闭 sidecar→重启再读同一报告；检查包内真实 sidecar，而不是只测源码或 hello。记录源码输入清单、包/可执行文件哈希、前后端版本和 GUI 结果。 |

开发发现必须覆盖原始历史 artifact、放宽事实/金额门禁或增加默认模型调用时，必须回本记录解决决策缺口。不得清库、删除历史、重跑所有研究或通过模型“重写旧报告”解除当前故障。

## 可观察验收与发布阻断

| 验收 | 必须观察到的结果 |
|---|---|
| 12 条历史读取 | 原始 4 completed + 8 partial 均由公开读取入口成功返回相符的 run/company；可信实质内容可以阅读，缺失/历史不明内容有准确状态；不能用 12 个诊断清单算通过。NVDA 财务和已验证增长材料可读，未通过的条目不升格。 |
| 数据与费用保留 | 同一份历史重复打开、关窗重启、切语言与读取重试，原 artifact/run/revision/attempt 的内容与数量不变，provider/下载/财务重算调用为零。活动库仅允许已批准的常规 schema 迁移，不能改变旧研究内容。 |
| 故障隔离 | 在 recovery 描述、可选财务状态与单个章节投影分别注入异常：已验证正文仍可读，相关状态不可用且有诊断；数据库不可读、不存在 run、主体身份错误仍明确失败。分类错误不会变成空历史。 |
| GUI 身份 | 启动读取失败、A→B 快速选择/乱序响应、B 加载失败、同 run 刷新失败、重启进入历史均有正确状态；不串公司，不用旧响应覆盖新选择，不出现截图中的“加载失败 + 没有报告”组合。 |
| 研究质量与导出 | 缺口卡片与 verified 内容分级一致；Markdown/HTML/桌面呈现同一事实、状态和来源。标题/占位不能算 visibility；无 FCF 不写可估值结论，无估值不肯定安全边际；阶段报告显著标缺口，完整状态仍须满足既有实质研究要求。 |
| 恢复仍可使用 | 真实历史 partial run 从可见正文旁选已配置新模型继续；校验输入和 plan hash，复用已完成可信节点。重启后有效，不依赖 `lastRequest`；planner unavailable 时不能发送调用。用受控失败覆盖已有各模型阶段恢复入口，检查 provider 请求与 attempt，不靠按钮单测证明续跑成功。 |
| 已知失败与最终包 | 2.8.7 QA 的所有读取失败、readiness 断言及并发合同失败分别有闭环；跑完整 Python 套件与改动相关桌面/协议测试，不 skip 已知红灯。2.8.8 实际便携包的持久化历史读取和 GUI 关键路径通过；hello、构建、源码回归单独通过不构成此项通过。 |

本版不要求为了证明历史阅读而重新付费生成 12 份研究。新建研究做既有确定性 demo 的完成→持久化→重启读取回归；真实旧 NVDA 读取/恢复与 2.8.7 未完成的真实 AAPL/NVDA 全研究验收分别记录。任一历史读取仍整体异常、已保存可信正文被清空、串公司、读取触发模型费用、历史数据被覆盖、无效内容冒充可信报告、测试包未验证读取，均阻断本版交付为可验收版本。真实研究质量未验证的项目继续保持 `PENDING`，不能以本次可用性修复代替。

## 审批与后续证据

**决策批准：`APPROVED`。** 用户于 2026-10-01 明确确认目标版本为 2.8.8，并要求严格按照本决策记录开发；批准范围与验收标准未变。开发 Agent 按 `workflows/DEVELOPMENT.md` 实施，并在本文件追加改动、测试/包证据、用户验收与发布状态，不另建交接文档。

### 开发实施与当前证据

| 要求 | 实施 / 证据 | 状态 |
|---|---|---|
| 稳定报告读取及可选信息故障隔离 | `application_services.py` / `service.py` 由同一快照生成正文、章节与诊断；可选恢复、财务状态与 continuity 分段隔离。报告状态统一为 `ready`、`partial`、`diagnostic_only`；全量回归与便携包验收均已通过。 | PASS |
| 单章节故障不影响其他已验证内容 | `report_document.py` 将投影、typed 节点和 growth 归一化放入章节边界；失败章节带 `section_projection_failed` 且不计实质章节。注入 executive summary 失败后 business model 仍可见的回归通过。 | PASS |
| FCF 匹配规则保留财务质量 | 仅相同持久化代次或同一官方来源批次（申报编号、解析器版本、文档标识全部相同）允许派生；跨解析代次拒绝。官方美团 2022 年报 OCF/CAPEX 回归恢复 `5,680,144,000 CNY` FCF。 | PASS |
| 章节 / readiness / 表格 / FCF 缺口契约 | 更新过时测试以检查真实阶段状态、精确 FCF 缺口、不渲染空白财务列；read_state 三态映射均有单测。 | 定向验证 PASS |
| 全部原历史报告可读且无数据改写 | `D:\test_qa_287\openthesis.db` 以只读不可变连接做快照，复制到隔离临时目录再调用公开 `AppService.get_report()`：12/12 身份匹配且含实质内容（8 partial、4 completed）；副本 rows：research_runs 12、artifacts 117、report_revisions 2、attempts 24，读取前后计数一致；源库 SHA-256 前后均为 `04635503004186A39614DC81E9A7B8D59303D6E95909282629EFB072E7D2855B`。11 条返回阶段性、1 条完整。读取未调用模型或财务重算。 | PASS（源码读路径，不等同包验收） |
| 桌面请求身份 / 失败状态 | A→B 晚到响应、失败重试与空报告 UI 测试通过；全部桌面单测 108/108、TypeScript `tsc -b` 及 Vite production build 成功（2026-10-01，日志有 Vite chunk >500KB 提示，不影响构建）。 | PASS |
| 针对根因定向回归 | `python -B -m pytest -p no:cacheprovider tests/test_domain_and_financials.py tests/test_financial_ingestion_engine.py::FinancialIngestionEngineTests::test_public_engine_parses_real_meituan_2022_operating_metrics tests/test_report_document.py tests/test_report_readiness.py tests/test_reporting.py tests/test_service.py::AppServiceTests::test_persisted_report_read_is_not_blocked_by_optional_recovery_metadata -q`：85 passed，32.72s。 | PASS |
| 全 Python 回归 | `python -B -m pytest -p no:cacheprovider tests -q`：870 passed，753.03s，退出码 0；Windows 11 / Python 3.12.8。 | PASS |
| 便携包、版本资源、持久化重启读取 | [OpenThesis-2.8.8-windows-x64-portable.zip](../../installer-output/OpenThesis-2.8.8-windows-x64-portable.zip) unsigned-test 便携包构建成功；主程序与 sidecar 均报告 2.8.8。实际包内 GUI 进程冷启动 3/3 通过；sidecar 无网络 demo 研究完成，生成 14 个实质章节；停止并重新启动 sidecar 后，`research.list → research.get_report` 返回相同 run、`input_generation` 与 Markdown。报告状态为 `partial`，不伪称完整。 | PASS |
| 包完整性与隐私扫描 | 默认隐私扫描通过：敏感数据命中 0、禁止数据条目 0、保留已审阅公开许可证声明 5 项。压缩包 SHA-256：`BBCE24DECBAAD222297DC48C028BD709FAE02E1043E1DE4DD44BC77921B20843`；大小 55,346,168 字节；签名模式 `unsigned-test`，未签名。 | PASS |
| 源码输入快照 | [history-report-availability-2.8.8-inputs.jsonl.gz](history-report-availability-2.8.8-inputs.jsonl.gz)：45,405 项，基准提交 `74f5af09a2cbd9a1380d4679b81cfc7b39255743`，SHA-256 `26C6F7400DEF672E123189ED1FB66B3DBD6A6E0204005A6F6DA8B1DB7B36C849`。最终包生成于 2026-10-01 07:18:10 UTC；快照生成于 07:14:21 UTC，打包后未再修改构建输入。 | PASS |

**本轮工作边界：** 未调用 Antigravity；未运行付费/真实公司研究；历史 QA 原件未打开写入。用户已完成 2.8.8 验证并批准上传；发布前源码输入清单与便携包哈希已再次精确核对。尚未执行 Git commit、push 或 Release 发布；因此 `Upload Ready=YES`、`Publication=PENDING`。
