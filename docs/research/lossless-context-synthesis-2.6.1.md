# OpenThesis 2.6.1：无损上下文综合调研

## 摘要结论

没有现成的模型、提示压缩器或 RAG 框架，能够对任意长财务研究报告证明“上下文受限时跨领域推理质量严格零下降”。现有成果分别解决了不同问题：

- 内容寻址、JSON Pointer/Schema、结构化事实图可以做到字节级可重建和审计引用不丢，但不等于模型已经同时看见并正确推理了全部证据。
- 长上下文模型减少分片需求，但官方文档仍提示多信息点检索准确率会下降，不能作为零损失证明。
- LLMLingua/LongLLMLingua 等是有实验依据的有损语义压缩，论文报告的是若干基准上的少量性能损失或提升，不是对 OpenThesis 财务、跨语言、跨年度任务的普遍保证。
- RAG、GraphRAG、MCP/外部记忆和 MapReduce 能把全部证据保留在外部，并按需读取；它们改善可扩展性，却引入召回、图抽取、工具选择和多轮状态风险。
- Prompt/context caching 主要降低重复前缀的成本和延迟，不减少首次输入 token，也不提升推理正确性。
- 严格“零下降”只能作为工程验收边界：对权威事实采用不可删的结构化主档和确定性校验；模型上下文不足时保留原证据、分段调用或安全停止，而不是声称语义压缩无损。

对 2.6.1 最成熟的组合是：**长上下文优先 + provider tokenizer 精确预算 + 可逆结构化证据引用 + 内容寻址缓存 + 有范围的检索/工具读取 + 确定性合并与回归评估**。LLMLingua 类语义压缩只能作为明确可降级的非权威叙述优化，不能作用于财务事实、冲突、反方证据或唯一 claim。

## 术语和判定标准

本调研区分四个容易混淆的目标：

| 目标 | 可验证含义 | 是否等于零质量下降 |
| --- | --- | --- |
| 字节可逆 | 通过 hash、引用或外部存储可恢复同一原始内容 | 否；模型可能没有读取该内容 |
| 语义信息不丢 | 压缩后保留所有任务相关事实、限定条件和关系 | 一般无法对开放式语言推理证明 |
| 任务指标近似不降 | 在固定 benchmark、模型和压缩比上统计相近或更好 | 否；不能外推到财务研究和新模型 |
| 推理质量可证明不降 | 对任意输入、模型和跨域关系都有形式化单调保证 | 当前没有通用方案 |

“无损”在 OpenThesis 中应默认只描述第一项；第二至第四项必须分别给出数据集、模型、任务和置信区间，不能用“保留所有 ID”代替。

## 方案比较

| 方案 | 成熟度 | 模型无关性 | 是否减少 token | 字节/事实可逆 | 是否保证零质量损失 | OpenThesis 适配与主要风险 |
| --- | --- | --- | --- | --- | --- | --- |
| LLMLingua / LongLLMLingua | 研究成果，有开源实现 | 压缩器可在黑盒模型前使用，但效果依赖压缩模型和目标模型 | 是 | 否，删除 token | 否；论文是基准上的“minimal performance loss”或局部提升 | 仅用于非权威背景、重复叙述；不得删 financial fact/evidence/冲突。小模型评分错误会删除关键限定语、负号或期间 |
| RAG | 生产中广泛使用，原始 RAG 论文提供了知识密集任务证据 | 检索层相对模型无关，生成质量仍依赖模型 | 发送给模型的 token 通常减少 | 外部库可逆，检索结果不是全量 | 否；召回漏项和排序偏差会影响结论 | 用于按 claim、年份、报表和风险主题召回；保留全量 registry，输出缺口诊断，不把未召回当不存在 |
| GraphRAG | 有官方开源实现和文档，适合全局/局部检索 | 索引/查询接口可复用，图抽取仍依赖模型和配置 | 通常减少单次上下文 | 原文/Parquet/图可保留 | 否；官方文档明确不同索引策略有相关性、噪声和成本权衡 | 适合建立公司—事实—来源—风险—主张图；核心事实仍由 FinancialFactCompiler 决策，图不能覆盖冲突 |
| 长上下文模型 | 已产品化；不同 provider 能力差异很大 | 不同 provider 的窗口、计费、位置偏差不同 | 不必压缩，但总 token 仍受上限 | 是，只要完整输入 | 否；Google 官方文档指出多 needle 场景准确率会显著变化 | 先按模型能力选择全量调用；用 exact tokenizer、输出/修复预算和回退门禁。不能因为窗口大就取消 evidence coverage 验收 |
| Prompt/context caching | OpenAI、Anthropic、Google 均有官方能力或文档 | 否，协议/TTL/数据保留由 provider 决定 | 不减少请求的逻辑 token；主要减少重复前缀处理成本 | 缓存内容可由 provider 保存，非本地可逆档案 | 否 | 把稳定的 schema、规则、canonical evidence manifest 放在可缓存前缀，动态问题放末尾；必须记录隐私/TTL/失效条件 |
| 结构化去重、JSON Reference/Pointer | RFC/标准成熟 | 是 | 只在传输中去掉重复文本；展开后 token 不变 | 是，引用解析可恢复 | 不能保证模型使用了引用目标 | 采用 content hash + stable evidence ID + JSON Pointer/Schema；每个分片带实际引用闭包，模型读不到的引用必须触发补读或显式缺失 |
| 内容寻址缓存 | 内容寻址协议成熟 | 是 | 不直接减少模型 token | 是；同 hash 内容可复用，修订生成新地址 | 否 | filing/PDF/AST/fact/evidence 分层 hash，版本键含 parser/taxonomy/policy；不能仅凭 accession 或文件大小命中 |
| 外部记忆/迭代工具读取 | MCP 等协议已标准化，工具生态成熟 | 协议层相对模型无关 | 可把大内容留在外部，按需读取 | 是，资源仍可读取 | 否；工具调用是额外决策回合，模型可能不调用或漏读 | 以只读 evidence resource + `read_claim_bundle` 工具提供按 claim 的完整证据；强制读取清单、授权、超时、取消、审计，禁止依赖模型自由选择 |
| MapReduce/层级综合 | 分布式执行模式成熟 | 执行框架模型无关，语义 map/reduce 不是无损 | 是，分批处理 | 中间原始输出可保留 | 否；语义摘要会产生级联“传话”损失 | 仅在 typed claim/evidence 边界上 map；reduce 优先确定性合并，模型摘要必须保留 source IDs、冲突和未决项 |
| Provider tokenizer exact counting | 工具/SDK 成熟但各家不同 | 只对相应 tokenizer/model 精确 | 不减少 token | 不涉及 | 不能保证质量，但避免运行时超限 | 每个 provider/model 由 adapter 提供 tokenizer/count seam；计算 system + role + user + JSON envelope，并预留输出/修复 token；禁止 bytes×4 作为最终判定 |

## 一手来源与能支持的结论

### 提示压缩

[Microsoft Research 的 LLMLingua 项目页](https://www.microsoft.com/en-us/research/project/llmlingua/llmlingua/)说明 LLMLingua 使用对齐后的小模型检测不重要 token，报告最高约 20 倍压缩和“minimal performance loss”。[原始 EMNLP 论文](https://aclanthology.org/2023.emnlp-main.825.pdf)的实验覆盖 GSM8K、BBH、ShareGPT 和 Arxiv-March23，并非财务事实完整性或跨域综合的形式化证明。[LongLLMLingua 论文](https://arxiv.org/abs/2310.06839)报告长上下文任务上的成本、延迟和特定 benchmark 表现；这仍是任务/模型条件下的经验结果。

官方实现的 [Microsoft/LLMLingua 源码](https://github.com/microsoft/LLMLingua/blob/main/llmlingua/prompt_compressor.py)明确以压缩率/目标 token 控制删除内容，压缩后的表达可能不适合人类直接理解。因此它应被 OpenThesis 视为有损优化器，而不是可逆编码器。

### RAG 与 GraphRAG

[RAG 原始论文](https://doi.org/10.48550/arXiv.2005.11401)把参数记忆与外部非参数记忆结合，证明知识密集任务可获得更具体、多样和事实性更强的输出；论文没有给出全量召回或零质量下降保证。

[Microsoft GraphRAG 官方仓库](https://github.com/microsoft/graphrag)和[官方概览](https://microsoft.github.io/graphrag/index/overview/)描述了实体、关系、claims、社区摘要和局部/全局搜索。官方文档同时提醒索引成本较高，并建议针对任务调 prompt；[方法文档](https://microsoft.github.io/graphrag/index/methods/)明确不同图检索策略存在相关性和噪声权衡。这支持“图是组织/检索层，不是事实质量门”的边界。

### 长上下文与缓存

[Google Gemini 长上下文官方文档](https://ai.google.dev/gemini-api/docs/long-context)说明长窗口可以减少过去依赖的滑窗、摘要和 RAG，但也明确指出多条 needle 的准确率会明显低于单条 needle，且位置、成本和延迟仍影响设计。它还建议对重复大输入使用[Context caching](https://ai.google.dev/gemini-api/docs/caching)。

[Anthropic Prompt Caching 文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)说明缓存的是固定前缀，默认有时效、命中依赖精确前缀；它优化处理时间和成本，不改变模型能否正确使用所有动态证据。[OpenAI Chat API 参考](https://platform.openai.com/docs/api-reference/chat/create)也把 `prompt_cache_key` 定义为缓存优化提示前缀的机制，而非质量或上下文扩展机制。

### 可逆引用、内容寻址和协议

[RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901)定义了从 JSON 文档按稳定路径解析值的语法和失败条件；[JSON Schema 2020-12 规范](https://json-schema.org/specification)提供结构化验证基础。OpenThesis 可以用它们表达“证据 manifest + claim 引用闭包”，但必须在模型调用前解析必要引用，不能只发送一个无法访问的 pointer。

[IPFS 内容寻址文档](https://docs.ipfs.tech/concepts/content-addressing/)说明 CID 基于内容 hash，内容任何变化都会产生不同标识；同时指出 CID 不一定等于文件的直接 SHA-256，取决于分块、DAG 和 codec。项目应保存自己的直接 SHA-256，并把内容地址作为版本/复用键，不要将 CID 语义误当作文件校验值。

[MCP 官方规范](https://modelcontextprotocol.io/specification/2024-11-05/index)提供 resources、tools、进度、取消、错误报告和用户同意等协议能力；规范也明确资源数据不得在未经同意时被转发。它适合实现受控的外部 evidence reader，但不能保证模型一定发起正确工具调用。

### 分层执行与 tokenizer

[MapReduce 原始 USENIX 论文](https://www.usenix.org/conference/osdi-04/mapreduce-simplified-data-processing-large-clusters)把 map 产生中间 key/value、reduce 合并相同 key 的执行模式形式化，并由运行时处理分区、调度和失败恢复。它证明了工程分治模式的成熟，不证明语义摘要的无损；OpenThesis 应把 reduce 输入设计为 typed facts/claims，而非任意自然语言摘要。

[OpenAI tiktoken 官方仓库](https://github.com/openai/tiktoken)提供按模型选择 encoding 的实现；源码还明确 `decode` 默认可能以 replacement 方式处理不能合法 UTF-8 往返的字节。因此预算必须以目标 provider/model 的 token 编码和 API envelope 为准，不能用字符数或固定 bytes/token 替代。

## 对 OpenThesis 的推荐架构

### 1. 先保住“数据无损”，再决定“模型看什么”

建立不可变的 `EvidenceManifest`，每条记录至少包括：

- `evidence_id`、`content_hash`、source document/accession、page/bbox/JSON Pointer；
- canonical fact/claim identity、period、scope、currency、unit、parser/taxonomy version；
- `supports`、`contradicts`、`derived_from` 的稳定边；
- raw/normalized 值和验证状态，但不在普通报告暴露技术 ID。

所有分片只传递 manifest 中该分片引用的闭包；缺失引用可确定性补读，无法补读则报告 `insufficient_evidence`。原始 PDF、AST、facts、冲突和未决项继续由 content-addressed cache 保存，任何新修订生成新 hash，不覆盖旧对象。

### 2. 全量可容纳时使用单次综合

按实际 provider tokenizer 计算完整 system prompt、role prompt、用户 JSON、工具/协议 envelope，并扣除 reserved output/repair tokens。只有完整输入连同保留预算均能容纳时，才使用单次全局综合，让模型同时看到财务、增长、反方和情景。

稳定的 schema、研究规则和只读 evidence manifest 可放到 provider 的 prompt cache 前缀；动态公司数据和问题放末尾。缓存只优化成本/延迟，不能替代本地完整证据存储、版本失效或质量校验。

### 3. 超限时优先“分区读取”，不要语义压缩权威材料

推荐顺序：

1. 切换到已验证可容纳的长上下文模型；
2. 使用结构化去重和 JSON Pointer，让重复来源/相同事实只传一次；
3. 通过 claim/evidence graph 做确定性检索，保证每个 required claim 的证据闭包；
4. 用 MCP/内部只读工具按 claim 读取未随首包发送的完整证据，并强制记录工具读取结果；
5. 对各领域做 typed analysis，保留跨领域索引（债务、现金、增长、反方、情景之间的关系），最后用确定性 reducer 合并；
6. 若单个不可拆 claim/evidence bundle 仍超限，安全停止并显示证据不足，而不是截断、摘要或猜测。

LLMLingua 只有在用户明确允许降低叙述冗余、且该段不承载唯一事实/证据/冲突时才可作为可选优化。压缩前后必须保留 hash 和引用闭包，并用固定 golden corpus 比较 claim recall、数值识别、冲突识别和章节完整率。

### 4. 将“跨域推理质量”变成可测试的约束

不能测量模型内部 attention 是否“没有损失”，但可以测量可观察的下界：

- 每个 required fact、claim、counterargument、scenario 都有稳定输入引用；
- 跨域关系 fixture（例如增长机会与债务风险）在至少一个综合调用中同时可见；
- 任何冲突、负号、单位、期间和否定条件在分片后仍被保留；
- provider 调用输入均通过 exact tokenizer budget，绝不在运行时才超限；
- 分片/工具/缓存失败不会静默删除内容，最终状态为 partial/failed 或本地化证据不足；
- 与 full-context 基线比较 claim recall、numeric exact match、conflict recall、evidence coverage、章节非空率和用户可见错误率，按 provider/model/语言分别记录。

## 2.6.1 具体修订建议

1. 将“lossless context synthesis”改名为“lossless evidence transport with bounded synthesis”；不要承诺语义零损失。
2. 抽出 provider-aware `TokenBudget`：每个模型给出 tokenizer、system/role/envelope 计数、输出/修复预留、最小可用预算和拒绝原因。
3. 修复 merge 预算盲区：所有 section、merge、final 调用共用同一个完整 `_agent_input_size`，而不是只比较 JSON payload 字节数。
4. 取消全局固定的 `evidence_ids[:N]`、字符截断和 `limit //= 2`；每个分片按实际引用闭包生成输入，无法容纳则分段读取或 fail-closed。
5. 将 `FinancialFactCompiler` 的 resolved facts、quarantined facts、conflicts、provenance 和 report claims 建成可寻址 typed graph；模型只读 resolved/明确标注的 inference channel。
6. 将“跨域桥接”作为显式输入，而不是希望四个孤立 section 自己发现：至少生成确定性关系索引（财务质量↔增长↔反方↔情景），并在综合调用前检查每个 required section 的桥接证据。
7. staged fallback 只能由确定性事实生成，context limit 时要保留已验证财务和本地化证据不足提示；不能把未经验证的 raw narrative 伪装为完整报告。
8. Prompt caching 仅缓存不敏感、版本化、稳定前缀；动态 facts、公司信息、密钥、数据库路径、signed URL 不进入 provider cache。记录 TTL、命中和失效原因。
9. 外部 memory/MCP 采用只读 `read_evidence_bundle`，必须有用户同意、可取消、超时和审计；工具没有返回完整 bundle 时不能升级 claim 状态。
10. 评估门禁必须包含 full-context 基线、结构化分区、RAG/graph 检索、长上下文和可选 LLMLingua 五组；按 A/H/US、三语和 provider 分层。固定样本“已披露必需字段 100% 完整、数值误识别率 0、冲突不静默消失”是发布门，而不是平均分。

## 结论与未解决问题

- **问题 1：是否有现成方案可直接满足严格 0 下降？** 没有。能直接复用的是可逆数据组织、精确预算、缓存、检索/工具协议和长上下文能力；语义压缩与层级模型综合都只能提供经验性近似。
- **问题 2：最成熟可组合架构？** 长上下文优先，结构化内容寻址证据图作为真相层，exact tokenizer budget 控制调用，provider cache 降低重复成本，RAG/GraphRAG/MCP 做受控读取，确定性 reducer/quality gate 收口。
- **问题 3：应复用哪些库/协议？** RFC 6901 JSON Pointer、JSON Schema 2020-12、provider 官方 tokenizer/cache API、MCP resources/tools/progress/cancellation、成熟向量/图检索实现；不要自研 token 估算、通用远端记忆协议或把 LLMLingua 当事实压缩器。
- **问题 4：2.6.1 应如何定义完成？** 不是“所有报告都单次装入模型”，而是所有权威证据可重建、每个 claim 的引用闭包可审计、预算失败不丢数据、跨域关系有显式覆盖、模型输出经过确定性验证；无法证明时显示 partial/insufficient，而不是宣称零质量损失。

未解决的是 provider 间 tokenizer、缓存 TTL/隐私政策、长上下文位置偏差、工具调用可靠性和开放式洞见的形式化质量证明。这些只能通过按 provider/model/语言的 golden corpus、盲测、回归指标和用户验收持续评估，不能由单一压缩库解决。
