# OpenThesis 2.3.0 财报识别性能与完整性重构验收文档

## 1. 工作流状态

- Workflow name: `financial-reliability`
- Target version: `2.3.0`
- Document role: 本工作流与目标版本的唯一需求、架构和验收事实源
- Acceptance: `PASS`
- User Test: `PASS`
- Upload Ready: `YES`
- Implementation authorization: `APPROVED — 2026-08-30`

本文件重新生成于 2026-08-30。用户已明确批准本方案，2.3.0 进入实施阶段。

## 2. 用户目标

1. 将完整财报识别控制在 5 分钟以内，解决当前单次识别超过 20 分钟的问题。
2. 不得通过减少必要报告、年度、期间、字段、证据、勾稽、冲突检查或质量门来换取速度。
3. 本地确定性解析、官方结构化数据、MinerU 和用户自备云端视觉模型必须进入同一套候选事实与质量校验流程。
4. 初次研究、重复研究、自动确定性重试和完整重建必须复用同一识别架构，不能继续维护结果不一致的平行路径。
5. 长时间运行时必须持续展示真实阶段、逐报告状态、耗时和阻塞原因，不能再次长期停在 `0/N`。
6. 超时、取消、云端限流、网络异常和单份异常 PDF 不能造成界面无响应、后台任务泄漏、半成品缓存命中或不完整数据进入模型。
7. 优化必须具有跨市场、跨公司和跨格式的广泛适配性，不得增加按公司、证券代码或特定报告名称编写的补丁。

## 3. 不可妥协的继承约束

以下约束继承自 2.2.1，2.3.0 不得降低：

- `FinancialFactCompiler` 仍是事实被接受、隔离和允许进入模型的唯一质量门。
- adapter 只能产生 `CandidateBatch + Evidence`，无权直接产生 accepted fact。
- 缺失不得补零、猜测或由语言模型生成；比较期不得冒充当前期，季度/中期不得冒充财年。
- scope、statement、entity、period、currency、unit、scale、sign 和修订关系必须通过校验。
- 冲突、拒绝事实、失败原因和来源定位必须保留审计，不得为提升表面完整率而丢弃。
- MinerU 或其他云端视觉结果只是候选证据，必须重新通过同一 canonical quality gate。
- 禁止训练、下载或捆绑本地模型；云端失败页解析必须遵守用户授权、最小上传和凭据隔离。
- 无 token 自动重试只能重试确定性失败节点或幂等传输，不得重新调用研究模型，也不得重复运行已成功节点。
- 报告和研究模型只能消费通过质量门的 `ResolvedFact`；不完整数据不得进入 AI。

## 4. 已复现基线与根因证据

### 4.1 用户可见故障

- 2026-08-30 截图中，财报识别已运行 `22:38`，总进度 `29%`，逐报告进度仍为 `0/10`。
- 对应真实批次为比亚迪 `002594.SZ` 的 10 份官方报告，压缩文件合计 `57.16 MiB`。

### 4.2 可重复红测

生产 `_safe_pdf_worker_count` 对该 10 文件批次返回：

```text
{'reports': 10, 'compressed_mb': 57.16, 'workers': 1,
 'requirement': 'workers>=2 without reducing reports'}
AssertionError: RED: real 10-report BYD batch is forced to sequential parsing
```

该反馈环只读取真实缓存并调用生产 worker-policy seam，实施前稳定失败；2.3.0 必须转绿且不得减少输入文件。

### 4.3 实测耗时

10 份报告在主线程完成全局串行页索引的耗时如下：

```text
25.558s, 22.704s, 22.896s, 29.661s, 31.265s,
29.360s, 0.847s, 20.211s, 13.288s, 0.507s
Total: 196.298s
```

在这 196.298 秒中，没有一份报告进入“解析完成”，因此 UI 必然持续显示 `0/10`。

最大实测报告：页索引 `22.644s`，58 个候选页；pdfplumber 坐标解析 `18.797s`；单份合计约 `41.441s`。

### 4.4 已确认根因（按影响排序）

1. **整批 8 MiB 阈值关闭并发。** 批次总压缩大小超过 `8 MiB` 即固定单 worker，57.16 MiB 批次必然串行。
2. **全局串行预索引屏障。** 所有 PDF 必须先在主线程完成全文索引，之后才启动解析 worker。
3. **初始研究绕过已存 filing。** 即使 accession、local path 和 content hash 已存在，初始 A/H 研究仍可能重新联网下载和重新解析。
4. **缺少版本化解析缓存。** 相同内容在重复研究时仍重复执行索引和坐标 AST。
5. **同一 PDF 存在重复读取。** pypdf 全页索引后，pdfplumber 再次打开候选页；失败退化路径可能再次遍历整份文档。
6. **进度事件粒度过粗。** 首次 `filing-parse current/total` 仅在整份 PDF 完成后发送，排队、索引、云端等待和校验都不可见。
7. **云端 fallback 未纳入总调度预算。** 本地失败后才串行等待审批、上传和轮询，会把云端时延直接叠加到本地尾部。

## 5. 2.3.0 目标架构

### 5.1 单一深模块：`FinancialRecognitionCoordinator`

新识别编排模块只暴露一个生产 interface：

```python
recognize(subject, filings, profile, policy, progress, cancel) -> RecognitionOutcome
```

该 interface 隐藏下载复用、缓存、调度、本地解析、云端失败页任务、断点恢复、进度和时间预算。初始研究、自动重试与完整重建只能调用这个 interface，不得各自实现下载、解析或完整性判断。

`RecognitionOutcome` 至少包含：

- canonical `FinancialDataset`；
- 每份 filing 的终态与阶段耗时；
- cache hit/miss 与失效原因；
- 本地和云端任务状态；
- diagnostics、阻塞原因和可恢复任务标识；
- active elapsed、approval wait 和 external wait；
- `COMPLETE | BLOCKED | CANCELLED | FAILED` 终态。

该模块不得复制 `FinancialFactCompiler` 的质量规则。Coordinator 负责编排候选事实；Compiler 仍负责唯一 canonical acceptance。

### 5.2 统一 filing 获取与内容寻址缓存

- 初始研究、自动重试和完整重建统一调用同一 `FilingRepository` seam。
- 以 issuer、market、accession、revision、source URL/validator 定位文件身份，以实际 SHA-256 定位不可变内容对象。
- 本地对象存在且实际 SHA-256 与数据库一致才算命中；仅凭路径存在不得命中。
- 相同 filing identity/content hash 的并发研究执行 single-flight：只能有一个下载或发布任务，其他调用等待并复用结果。
- 新 accession、明确修订、validator 变化、对象缺失或 hash 不一致时才重新获取。
- 下载先写临时对象，完成大小/hash 校验后原子发布；取消、崩溃、断电或校验失败不得留下可命中的半成品。
- 旧版无 hash 文件只能作为待验证遗留对象；验证通过后迁移，禁止直接视为命中。
- 保留 immutable 历史文件和 `supersedes` 关系，不覆盖旧修订。

### 5.3 版本化解析缓存

解析缓存键必须至少包含：

```text
content_hash
+ parser_version
+ taxonomy_version
+ coverage_profile_version
+ extraction_policy_version
```

缓存内容包括：

- 页索引与报表页分类；
- 坐标 AST/结构化候选；
- `CandidateBatch`；
- evidence locator/hash；
- 安全 diagnostics 与阶段耗时。

约束：

- 缓存命中仍须经过当前 `FinancialFactCompiler`，不得直接标记 VERIFIED。
- 文件、parser、taxonomy、profile 或 policy 任一变化必须失效。
- `PARTIAL`、`FAILED`、`CANCELLED`、超时和未完成云端任务不得写成成功缓存。
- 缓存发布使用 single-flight、临时文件和原子替换；并发读取不得看到中间状态。
- 暖缓存研究不得联网、不得重新全文索引、不得重跑坐标 AST、不得调用研究模型或视觉模型。

### 5.4 按报告流式流水线，移除全局屏障

每份报告独立执行：

```text
identity/hash check
→ cache lookup
→ page index / statement locator
→ coordinate AST
→ local CandidateBatch
→ failed-page manifest（如有）
→ canonical validation
→ atomic cache/persist
```

- 禁止“全部文件索引完成后才开始解析”。
- 第一份报告完成索引后即可进入 AST；小文件不得等待大文件结束。
- 同一 worker 生命周期内尽可能完成该文件的索引和 AST，避免主进程先打开整批文件。
- 成功路径不得对同一 PDF 重复进行两次全文扫描。
- 如果仍保留 pypdf locator 与 pdfplumber coordinate 两阶段，第二阶段只能读取候选页，并以打开/扫描计数测试证明不会退化为两次全文件遍历。
- 并发完成顺序不决定业务结果；最终按 filing identity、revision、period 和 source priority 确定性汇总。

### 5.5 内存加权、可取消的进程调度

- 移除“整批压缩大小 >8 MiB 即单线程”的开关。
- 依据单文件大小、页数、候选页、可用内存和历史解析成本分配 weighted permits。
- 默认允许 2 路，资源允许时最多 3 路；低内存时动态收缩，但大文件不得令整个批次永久单线程。
- worker 使用可取消的长寿命进程池；未开始任务在取消后不得启动。
- 单文档超过 hard timeout 时必须终止对应 worker，回收 permit，并确认没有残留解析进程。
- 异常长尾文件不得阻塞其他报告完成、缓存写入和进度更新。
- 调度器必须限制 CPU、内存和文件句柄，避免 UI 线程因解析、网络连接测试或重试而无响应。

### 5.6 Structured-first、gap-only，不减少必要数据

- 官方结构化事实与本地报告解析可并行获取，但只有 canonical compiler 决定采用哪一项。
- 已由可靠官方结构化来源满足且通过质量门的 concept，不重复执行无价值的 PDF 全文工作。
- PDF AST 只解析尚缺 concept、冲突复核及必要 evidence 页面。
- `CoveragePlanner` 的必需年度、期间和字段集合不变；gap-only 是减少重复计算，不是减少识别范围。
- 本地 adapter 失败时生成明确的 failed-page manifest；不得把解析失败误标为 `NOT_DISCLOSED`。

### 5.7 云端识别编排（MinerU / 用户自备视觉模型）

云端不是整份 PDF 的第二次完整识别，而是本地失败页的并行补全队列。

#### 触发条件

只有同时满足以下条件才可进入云端队列：

1. 官方结构化来源未满足必需 concept；
2. 本地 statement locator / coordinate AST 已对目标页失败或产生不可验证冲突；
3. `CoveragePlanner` 仍判定为必需缺口；
4. 用户已启用对应 provider、完成数据发送知情同意，并按当前隐私策略审核本次待发送页面。

#### 失败页选择

- 根据缺失 statement、正式合并报表标题、列头和连续页关系选择页面。
- 每个目标表默认最多为标题页加连续 2 页；遇到下一个正式报表标题立即停止。
- 排除母公司、单体、`Separate` 报表，以及与缺失 concept 无关的页面。
- 每页保留原页号、页 hash、source document 短标识和 filing hash；禁止上传整份 PDF。
- 单次 provider 请求遵守 `20 pages / 10 MiB` 上限。超过时按 statement 分成确定性批次，审批清单必须显示全部批次和页码；同一页不得重复上传。

#### 并发、幂等和恢复

- 云端提交必须等待首次 canonical compile 确认真实缺口，避免结构化来源尚未收齐时误传已满足页面；确认后，多 filing 任务以全局并发 2 提交并确定性汇总，不在 filing 循环中逐份串行等待。
- 云端并发默认 2 个有界任务，并遵守 provider 的限流响应；不得创建无限重试或无限轮询。
- task id 由 `provider + filing_hash + page_hashes + parser_schema_version` 确定性生成。
- 网络重试必须复用 task id；优先查询已有任务状态，不重复上传、不重复计费。
- 仅对连接中断、可恢复 5xx 和 provider 明确允许的 `Retry-After` 执行有限自动重试；认证失败、用户拒绝、格式错误和不可验证响应不得自动重试。
- 应用重启后可根据安全任务记录恢复轮询；记录不得包含图片、原文、密钥、签名 URL 或个人路径。
- provider 结果按同一缓存版本规则保存为候选事实；命中后仍重新经过 canonical compiler。

#### 授权与计时

- 必须同时显示 `总等待时间`、`引擎活跃时间` 和 `等待用户授权/等待外部服务`，不得用暂停计时掩盖真实等待。
- 用户审批等待属于明确的外部阻塞，不能由程序保证时长；进入审批界面后 UI 仍保持响应。
- 若用户在 30 秒内批准且 provider 正常响应，包含本地识别、云端补全和 canonical validation 的墙钟时间目标仍为 300 秒以内。
- 若用户未授权、拒绝、网络不可用、429/配额耗尽或 provider 超过预算，系统必须在预算内进入可恢复 `BLOCKED`，保留已验证结果和任务状态，但不得生成不完整研究报告。

#### 云端结果质量门

云端返回必须至少提供页码、页 hash、raw cell/row、bbox 或等价结构定位、confidence 和 provider task id，并重新校验：

- company / filing identity；
- consolidated scope；
- statement 与当前期列；
- fiscal period 和 duration；
- currency、unit、scale 和 sign；
- 资产负债勾稽、现金流/利润量级和跨年连续性；
- 与官方结构化事实及本地 AST 的冲突。

任何无法验证、缺少 provenance 或发生语义冲突的云端候选均隔离，不得为了满足 5 分钟目标放行。

### 5.8 单一状态机、进度与 UI 响应性

每份 filing 使用以下稳定状态，不由自由文本推断：

```text
QUEUED
→ CACHE_CHECK
→ INDEXING
→ LOCAL_PARSING
→ LOCAL_VALIDATING
→ CLOUD_AWAITING_APPROVAL | CLOUD_SUBMITTED | CLOUD_POLLING
→ CANONICAL_COMPILING
→ CACHE_HIT | VALIDATED | BLOCKED | FAILED | CANCELLED
```

UI 要求：

- 进入识别后 `<=2s` 显示 `cache-check` 或 `filing-index 0/N`。
- 任何活跃任务可见进度静默不得超过 `5s`；心跳不能伪造完成百分比。
- 每份报告显示 queued、cache hit、indexing、parsing、validating、waiting approval、cloud processing、validated、blocked、failed 或 cancelled。
- 当前报告、已完成数、总数、阶段耗时、总耗时和预计剩余范围可见。
- 总进度百分比不得替代阶段状态；不能再次用 `0/N` 隐藏正在进行的索引工作。
- 云端审批、取消、模型连接测试、确定性重试均在后台执行；主窗口导航和所有非冲突按钮保持可响应。
- 报告生成完成后隐藏识别等待卡片；历史诊断进入详细技术信息，不污染报告正文。
- 简体中文、繁体中文和英文必须语义一致；错误文案说明“哪份报告、哪个阶段、为何阻塞、可执行下一步”。

### 5.9 时间预算、watchdog 与诚实终态

使用单调时钟记录，不得依赖系统时间跳变。预算从 filing cache/hash 校验开始，到 canonical validation 结束。

建议预算：

| 阶段 | 目标窗口 | 说明 |
|---|---:|---|
| filing cache/identity | 0–30s | 与结构化来源、第一页 locator 流式重叠 |
| 本地索引与 AST | 0–180s | 2–3 路内存加权流水线 |
| 云端失败页补全 | 65–240s | 首次 canonical 缺口确认后立即以全局并发 2 补全，禁止提前猜测并上传页面 |
| canonical compile/quality gate | 180–285s | 按报告增量准备，末尾确定性合并 |
| 清理与余量 | 285–300s | worker 回收、原子发布和终态 |

- 本地 cold recognition 必须在 300 秒内成功或以真实 `BLOCKED/FAILED` 终止，不得后台继续偷偷运行。
- 单文档超时必须终止实际 worker，不得只让等待方超时。
- 官方下载耗时、用户审批等待和 provider 排队分别计量，不得计入“解析耗时”或被其掩盖。
- 在线端到端 5 分钟成功承诺只适用于官方站点和云端 provider 在验收响应窗口内正常返回、且用户及时批准的情形。
- 外部依赖异常时无法同时承诺“5 分钟内成功”和“数据绝不缺失”；正确终态是在 5 分钟内明确阻塞、可恢复、绝不生成残缺报告。

## 6. 无损完整性定义

性能优化前后，使用规范化 canonical snapshot 比较：

- filings identity、revision 和 supersedes；
- resolved/research facts；
- quarantined facts 与拒绝原因；
- evidence locator、page、bbox、raw hash 和 evidence 数量；
- conflicts 与 resolution reason；
- coverage、group validation、diagnostics 和 `allow_ai`。

以下值必须完全相同或满足预声明的等价规范：concept、Decimal 数值、period、duration、scope、currency、unit、scale、sign、statement、当前期列、来源优先级和证据页码。

并发完成顺序、缓存命中和云端返回顺序不得改变 canonical snapshot。超时、取消和部分结果不得产生 `COMPLETE` 或成功缓存。

## 7. 性能验收契约

### 7.1 固定参考用例

- 公司：比亚迪 `002594.SZ`。
- 输入：同一 10 份官方报告，固定 accession、文件清单和 SHA-256 manifest，共 `57.16 MiB`。
- 输入完整性：不得删减报告、页、必要年度、期间、字段或 evidence 要求。

### 7.2 必须满足的指标

| 场景 | 验收标准 |
|---|---|
| 冷本地识别 | 文件已下载；从 hash/cache 校验到 canonical validation，连续 3 次均 `<=300s`，并报告 p50/p95 |
| 暖缓存重复研究 | `<=30s`；HTTP、全文索引、AST、视觉模型和研究模型调用次数均为 0 |
| 在线首次研究 | 官方站点正常响应时 acquisition + recognition `p95<=300s`；外部下载时间单列 |
| 云端正常 | 用户在 30s 内批准、provider 在约定窗口返回时，端到端墙钟 `<=300s` |
| 云端异常 | 300s 内进入明确、可恢复 `BLOCKED`；不生成残缺报告、不遗留 worker、不重复上传 |
| 首次进度 | `<=2s` |
| 活跃进度静默 | `<=5s` |
| 取消确认 | `<=1s`，且未开始任务不再启动 |
| UI 响应性 | 本地解析、云端轮询、模型连接测试和重试期间主线程无长任务，交互测试持续通过 |

性能结果必须记录机器 CPU、内存、系统、Python/parser 版本、cache 模式、worker 数和每阶段耗时。禁止仅报告最好的一次。

## 8. 正确性与跨市场验收

- 保持 2.2.1 的 6 家 × 5 年 golden corpus 30/30 通过：A 股至少宁德时代、中芯国际；港股至少腾讯及汇丰或美团；美股至少 Apple、Microsoft。
- 非金融公司适用 profile 的必需事实完整率为 100%，数值误识别率为 0；金融公司使用预声明专用 profile。
- A/H 双权益、报告币种与上市币种、累计中期/季度和完整财年语义不得回归。
- 收入、净利润、总资产、总负债、权益、经营现金流等适用核心事实保持官方 provenance。
- 2025 等最新完整财年不得因缓存、排序或年度窗口错误消失；最新期间的收入增长只有存在可比基期时才计算。
- canonical snapshot 在串行基线、并发 cold path、warm cache path 和云端候选乱序返回下保持语义等价。
- MinerU 触发率、冲突率、隔离率和缺失原因按市场/adapter/concept 统计，不能以空白或 `—` 隐藏解析失败。

## 9. 自动重试与恢复验收

- 可恢复的本地节点自动重试最多采用有限次数、指数退避和 jitter；相同成功节点不重跑。
- 自动重试不得调用研究模型，不产生额外模型 token。
- 云端任务先查询幂等 task id，再决定是否重试 transport；不得重复上传同一页。
- 完整重建只使用户明确指定或被版本/hash 污染的缓存失效，不得无条件删除全部有效缓存。
- 应用退出或崩溃后，重启可清理临时文件并恢复安全云端轮询；不得恢复已取消任务。
- 重试后仍缺失必需事实时保持 `BLOCKED` 并提供具体报告、statement、concept、阶段和错误码；不得把缺失报告交给模型补写。

## 10. 可观测性与错误分类

每份 filing 记录稳定的非敏感 accession 或内部短 ID，以及：

- cache hit/miss 与失效原因；
- download/hash/index/AST/cloud wait/cloud parse/validation/worker wait 耗时；
- 打开次数、全文扫描次数、候选页数和 worker 峰值；
- 终态与结构化错误码；
- 本地/云端候选数量、冲突数量、隔离数量和覆盖结果。

最低错误码集合：

```text
CACHE_HASH_MISMATCH
CACHE_VERSION_MISS
DOWNLOAD_TIMEOUT
EXTERNAL_FETCH_FAILED
PARSER_TIMEOUT
FORMAT_UNSUPPORTED
LOCAL_EXTRACTION_INCOMPLETE
CLOUD_APPROVAL_REQUIRED
CLOUD_APPROVAL_DECLINED
CLOUD_RATE_LIMITED
CLOUD_TIMEOUT
CLOUD_RESPONSE_UNVERIFIABLE
CANONICAL_VALIDATION_FAILED
CANCELLED
```

日志和遥测禁止记录：本地绝对路径、PDF 原文、渲染图片、API key、Authorization header、签名 URL、模型完整 prompt、数据库内容或其他公司数据。

## 11. 测试计划与发布门

### 11.1 红绿测试

- worker-policy：57.16 MiB 多文件批次不得因整批大小变为永久单线程。
- global-barrier：慢文件不阻塞快文件先索引、解析、校验并报告进度。
- scan-count：成功路径无重复全文扫描；第二阶段只读取候选页。
- cache：首次原子写入、二次零解析命中、版本/hash/profile/policy 变化失效。
- single-flight：同一 filing 并发请求只发生一次下载和一次解析发布。
- cancellation：取消、单文档 hard timeout、进程崩溃后无 worker 和临时文件残留。
- deterministic-order：不同完成顺序得到相同 canonical snapshot。
- cloud-page-selection：只选正式合并报表失败页，正确处理续页，排除母公司/Separate 表，不上传整份 PDF。
- cloud-idempotency：网络中断/重启后复用 task id，不重复上传或重复计费。
- cloud-validation：错误期间、单位、币种、scope、量级或无 provenance 的结果全部隔离。
- UI：索引期不再静默 `0/N`；阶段、计时、审批、取消和错误说明三语一致；连接测试/重试期间界面可操作。

### 11.2 性能与完整性门

- CI 使用固定 manifest/hash fixture 验证调度、缓存、取消、超时、结果顺序和云端 fake provider。
- 本机 BYD 10 文件执行 3 次 cold + 3 次 warm；记录完整 JSON benchmark，不只保留截图。
- warm 测试明确断言 HTTP、索引、AST、云端模型和研究模型调用次数均为 0。
- 运行跨市场 30/30 golden corpus，并比较 canonical snapshot。
- 运行 cloud normal、429、5xx、超时、拒绝、重启恢复和不可验证结果 fault injection。
- 验证所有失败路径均无后台任务、临时文件、半成品缓存和不完整研究报告。

### 11.3 完整自动化门

- Python 全量 tests。
- Frontend tests、typecheck、build。
- Rust tests。
- 性能 benchmark 与 golden corpus。
- portable privacy/runtime checks。
- 生成无签名 Windows x64 便携测试包；签名不属于本次阻塞项。

任一硬指标、完整性 gate、隐私 gate 或自动化门未通过，`Acceptance` 必须保持 `FAIL/PENDING`，不得打包为可供用户验收的成功版本。

## 12. 实施阶段与文件范围

### 阶段 A：可观测基线与红测

- 固定 BYD manifest/hash benchmark。
- 加入 worker、扫描、cache、任务状态和 canonical snapshot 红测。

### 阶段 B：缓存与统一获取

- filing single-flight、实际 hash 验证、原子发布、遗留缓存迁移。
- 版本化 parse cache 与 warm zero-work 路径。

### 阶段 C：流式流水线与调度器

- 移除全局预索引屏障。
- 按文件的内存加权进程池、hard timeout、取消和确定性汇总。

### 阶段 D：云端失败页编排

- failed-page manifest、预览审批、幂等任务、并行轮询、断点恢复和 provider cache。
- 云端候选统一进入 compiler，不新建平行质量门。

### 阶段 E：UI、全量验收与打包

- 三语状态机、计时、错误说明和响应性。
- 性能、golden、fault injection、全量自动化及无签名 portable。

预计相关文件（以实际 diff 为准）：

- `src/openthesis/financial_ingestion.py`
- `src/openthesis/financial_compiler.py`
- `src/openthesis/vision_financials.py`
- `src/openthesis/service.py`
- `src/openthesis/storage.py`
- `src/openthesis/market_data.py`
- `src/openthesis/download_safety.py`
- 新的识别编排/缓存/benchmark 模块（如经实现确认）
- `desktop/src/features/research/researchProgress.ts`
- `desktop/src/app/useWorkbenchSession.ts`
- `desktop/src/components/States.tsx`
- `desktop/src/i18n.ts`
- 对应 Python、frontend、Rust tests 和打包版本元数据。

## 13. 逐项验收清单

### 用户批准与架构

- [x] 用户明确批准 2.3.0 实施。
- [x] 初始研究、重试和完整重建均只调用统一识别 coordinator；生产重试/重建对完整目标批次只调用一次 coordinator，legacy 注入测试 seam 单独兼容。
- [x] `FinancialFactCompiler` 保持唯一 canonical acceptance seam。

### 性能与缓存

- [x] 原 worker-policy 红测转绿，未减少 10 份报告。
- [x] 移除全局串行预索引屏障。
- [x] 按文件内存加权调度、hard timeout 和取消完成。
- [x] filing cache、parse cache、single-flight 和原子发布完成。
- [x] BYD cold 连续 3 次均 `<=300s`。
- [x] BYD warm 重复研究 `<=30s` 且外部/解析/模型调用均为 0。
- [x] 取消和超时后无残留 worker 或半成品。

### 云端

- [x] 云端只处理首次 canonical compile 确认缺失的 statement 页，未上传整份 PDF；正式合并报表标题页最多带连续 2 页，并排除母公司/Separate。
- [x] 页面预览/逐次授权、20 页/10 MiB、原页映射、页 hash 和隐私边界通过。
- [x] 多 filing 云端补全使用全局有界并发 2，结果按 filing identity 确定性汇总；为避免在官方结构化数据和 canonical 缺口尚未确定时误传页面，安全边界调整为首次 canonical compile 后启动，而不提前猜测缺口上传。
- [x] task id 幂等、有限重试、断点恢复和不重复上传通过。
- [x] cloud normal 在约定条件下端到端 `<=300s`（用户已在便携包中完成真实 provider 实机验收）。
- [x] cloud abnormal fault injection 在预算内进入可恢复 `BLOCKED`，不生成残缺报告、不缓存 partial、不遗留执行线程。
- [x] 云端候选通过同一 canonical quality gate。

### 完整性与 UI

- [x] 三次 cold 与三次 warm canonical snapshot 语义一致。
- [x] 6 家 × 5 年、共 30 条跨市场 golden corpus 和 2.2.1 精确金标无回归。
- [x] 必需年度、期间、字段、证据、冲突和质量门未减少；cold/warm 六次 canonical snapshot 完全一致。
- [x] 首个进度 `<=2s`（实测 cold 为 0.078–0.093s）。
- [x] 三语逐报告状态、总/活跃/外部等待计时和可操作错误完成。
- [x] 解析、连接测试、云端轮询和重试在后台执行；逐 filing 状态、三类计时和受限 ETA 区间均有前端测试覆盖。

### 自动化、产物与用户测试

- [x] Python `429/429`、frontend `69/69`、typecheck、production build、Rust `33/33` 全部通过。
- [x] benchmark、30/30 golden、cloud fault injection、portable privacy/runtime gates 全部通过。
- [x] 2.3.0 无签名 Windows x64 portable 已生成并记录 SHA-256。
- [x] 用户实机验证完成并明确表示全部正常（2026-08-30）。

## 14. 已批准实现决策

当前已批准的基线约束为 2.2.1 的统一 compiler、失败页最小上传、禁止本地模型、无 token 确定性重试和不完整数据禁止进入模型。

用户已于 2026-08-30 批准本文件新增的 2.3.0 coordinator、缓存、调度、云端并行和 5 分钟验收方案。

## 15. 未决事项

- 无需用户选择具体调度算法或缓存格式；这些属于 implementation，可在不改变本验收 interface 和不变量的前提下决定。
- 外部官方站点、MinerU 或自备 provider 的不可控停机/排队无法被本地代码消除；本版本以“正常响应时 5 分钟内成功，异常时 5 分钟内明确可恢复阻塞且绝不缺数据”为诚实边界。
- 若后续要求取消逐次页面审核、改为长期静默云端授权，属于隐私边界扩展，必须另行获得明确批准。

## 16. 验收结果与完成摘要

- Completed changes:
  - 新增单一 `FinancialRecognitionCoordinator`，初始 A/H 研究通过统一 compiler seam。
  - 自动确定性重试与完整重建改为整批一次调用 coordinator，再按 accession/document 分片持久化 canonical 结果，避免重试路径重新串行。
  - PDF 识别改为最多 3 路、逐报告可终止的进程流水线，移除全局串行预索引屏障。
  - 用项目既有 `pypdfium2` 作为成熟快速页定位层；无法安全得到三大报表完整索引时回退 pypdf，坐标 AST、字段范围与质量门未减少。
  - 新增内容寻址、版本化、原子发布的 parse cache 与进程内 single-flight；失败、取消和超时结果不写成功缓存。
  - MinerU/自备视觉任务新增安全 journal、确定性 task id、有限传输重试、恢复状态与仅完整成功结果缓存。
  - 云端只接收 canonical 缺口对应的合并报表页；多 filing 以全局并发 2 处理并确定性汇总，避免整份 PDF 或已满足 statement 被重复上传。
  - UI 新增逐报告本地解析/校验/云端/canonical/终态、总耗时、引擎活跃耗时、外部等待耗时和受限预计剩余区间。
  - benchmark 支持逐轮原子 checkpoint、cold 失败即停止，以及复用成功 cold cache 的零重复 warm 验收。
- Acceptance results:
  - BYD `002594.SZ` 固定 10 报告、`59,937,208 bytes`（57.16 MiB），未删减输入。
  - cold：`65.641s / 63.891s / 64.250s`，p50 `64.250s`，max `65.641s`，均为 `COMPLETE`、`allow_ai=true`。
  - warm：`0.094s / 0.094s / 0.094s`，均为 `COMPLETE`；cold/warm 六次 canonical snapshot SHA-256 一致。
  - 首个进度：`0.078s / 0.078s / 0.093s`。
  - Python 全量：`429 tests`，全部通过，耗时 `233.900s`。
  - Frontend：Vitest `13 files / 69 tests`、TypeScript typecheck、Vite production build 全部通过；仅保留既有的非阻塞 chunk-size warning。
  - Rust：`33 tests` 全部通过。
  - Golden corpus：6 家 × 5 年共 30 条记录，`10 tests` 全部通过。
  - Portable gates：隐私命中 `0`、必需结构齐全、GUI subsystem 正确、无可见控制台、主进程与 sidecar 实际启动通过。
- Known unresolved issues: 无；用户已完成真实云端 provider 的正常响应端到端实机验收，本版本仍不提前猜测 canonical 缺口上传页面。

## 17. Files intended for publication

- `pyproject.toml`
- `scripts/package-desktop.ps1`
- `scripts/benchmark_financial_recognition.py`
- `src/openthesis/__init__.py`
- `src/openthesis/financial_compiler.py`
- `src/openthesis/financial_ingestion.py`
- `src/openthesis/financial_recognition.py`
- `src/openthesis/service.py`
- `src/openthesis/storage.py`
- `src/openthesis/vision_financials.py`
- `desktop/package.json`
- `desktop/package-lock.json`
- `desktop/src-tauri/Cargo.toml`
- `desktop/src-tauri/Cargo.lock`
- `desktop/src-tauri/tauri.conf.json`
- `desktop/src/components/States.tsx`
- `desktop/src/components/States.test.tsx`
- `desktop/src/styles.css`
- `desktop/src/types.ts`
- `tests/test_financial_ingestion_engine.py`
- `tests/test_financial_recognition.py`
- `tests/test_service.py`
- `tests/test_vision_financials.py`
- `docs/releases/financial-reliability-2.3.0.md`

## 18. Release artifact

- Path: `D:\githubmax\installer-output\OpenThesis-2.3.0-windows-x64-portable.zip`
- SHA-256: `46027BB2676C2D354409C006B1F9F3CA116CD8AD1974009D44AEE78E270E9AA3`
- Build mode: `unsigned-test`, Windows x64 portable

## 19. 最终状态

- Acceptance: `PASS`
- User Test: `PASS`
- Upload Ready: `YES`
