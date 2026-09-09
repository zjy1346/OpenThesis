# OpenThesis 2.5.0 自动反向 DCF 与 QA 遗留项验收文档

## 1. 工作流状态

- Workflow name: `reverse-dcf`
- Target version: `2.5.0`
- Document role: 本开发会话、2.5.0 需求与验收的唯一事实源
- Implementation authorization: `APPROVED — 2026-09-08`
- Acceptance: `PASS`
- User Test: `PASS（用户已完成实机验证并批准发布，2026-09-09）`
- Upload Ready: `YES`

本版本遵循新约定：已发布版本之后由 QA 发现的问题写入 `docs/qa` 的下一版本待解决项，下一开发版本同时完成这些问题与新功能。2.5.0 必须同步关闭 `docs/qa/2.5.0bug待解决项.md` 中的全部问题，并研发自动反向 DCF。

用户已批准免 API key 双来源行情、可选自备 API、零填写默认策略、权益反向 DCF 口径及陈旧行情保护方案，2.5.0 进入实施阶段。

## 2. 用户请求

1. 研发反向 DCF，使软件自动获取价格、价格日期、币种、市值及计算所需的可验证市场快照，不再要求用户逐项手工录入。
2. 默认行情链路必须免 API key、免注册、免模型 token；普通用户选择公司后，不填写任何 DCF 字段即可直接开始研究。
3. 保留自行配置市场数据 API 的能力，但只能作为增强来源或免 Key 来源失败后的回退，不能成为普通用户使用反向 DCF 的前置条件。
4. 市场数据必须保留来源、时间和币种，能够复现当次研究，不能把实时变化的数据无来源地写进报告。
5. 2.5.0 同步解决 QA 待解决项：
   - 新任务成功后历史财报重试状态残留、顶部横条和进度状态错误；
   - “反方观点”有上游内容却被投影层丢弃，最终出现空白；
   - 完成解析后规范化财务度量与报告投影未可靠刷新；
   - 完整研究在慢速/免费模型端点下耗时过长，需减少无效串行等待和超长合成负荷。
6. 变更必须保持现有财报质量门、证据链、三语界面、取消/超时和 UI 响应能力，不得以减少必要研究内容换取速度。

## 3. 已确认的当前行为与根因

### 3.1 现有反向 DCF 不是自动估值模块

当前调用链为：

```text
NewResearchView 手工输入
→ service._market_snapshot / _valuation_inputs
→ ResearchWorkflow.run
→ financials.reverse_dcf_analysis
→ reporting.py / report_html.py
```

已确认的问题：

- 价格、日期、币种和市值均来自手工表单；项目不存在行情获取 interface。
- 价格本身不参与当前计算；真正使用的是手工市值。
- 当前实现把市值直接当作 DCF 终值目标，同时在说明中称其为“近似企业价值”，现金流口径与价值口径不够一致。
- 上市币种与报告币种不一致时直接跳过计算，没有汇率快照。
- 没有行情来源、新鲜度、交易日、汇率来源或手工覆盖的 provenance。
- 报告没有完整展示“价格日期—价格—币种—市值—现金流期间—假设”的可复现链路。

### 3.2 QA 遗留项根因

- `financial_retry_state` 以证券为中心长期残留，未与当前研究运行/重建操作建立生命周期，旧失败会污染新成功运行。
- 前端按历史 `last_stage` 推断后续阶段为“处理中”，缺少明确终态和当前 operation identity。
- `report_projection.py` 的 `counterarguments` 白名单与 Agent 真实输出结构不一致，静默丢弃 `strongest_counterarguments`、`unsupported_assumptions`、`missing_evidence` 等字段；HTML 层又允许空容器。
- 基础 Agent 已存在有限并行能力，但配置可能关闭；增长、反方、预测和最终合成仍形成长串行尾部，最终合成重复吞入大量上游内容，慢速免费端点下可超过 20 分钟。

## 4. 提议的 2.5.0 架构

### 4.1 深模块一：`MarketSnapshotModule`

在公司研究入口与外部行情源之间建立单一 seam，只向调用者暴露一个小 interface：

```python
capture(company, policy, manual_override=None) -> MarketSnapshotOutcome
```

模块内部隐藏证券代码映射、请求、限流、缓存、交易日判断、字段交叉校验、汇率和失败回退。生产与测试分别使用 HTTP adapter 和内存 adapter；调用者不得直接理解某家行情供应商的响应字段。

`MarketSnapshotOutcome` 至少包含：

- `status`: `VERIFIED | STALE | MANUAL | UNAVAILABLE | CONFLICT`；
- 证券/发行人 identity、报价证券代码与交易所；
- price、market cap、quote currency、quote as-of/date、market state；
- reporting currency；跨币种时的 FX rate、FX as-of 和 FX source；
- 归一到报告币种后的 equity market value；
- provider、source URL/标识、retrieved-at、cache 状态；
- warnings、结构化错误码和是否允许进入估值。

### 4.2 免 Key 市场数据 adapter 与优先级

默认路径不要求用户申请或填写任何 API key。首版采用有界、只读、带缓存和来源标识的双 adapter 链：

- `EastmoneyPublicQuoteAdapter`：取得 A 股、港股、美股的报价、更新时间和供应商总市值字段；适配层负责证券市场编号、价格精度和字段版本，调用者不接触 `f43/f86/f116` 等供应商字段。
- `TencentPublicQuoteAdapter`：作为 A 股与港股的独立 HTTPS 备用源，在东财被防火墙、代理或服务端断连阻断时提供价格、交易日期、币种与证券口径市值；只接受经过严格变量名、证券代码、字段布局、日期和币种校验的 GB18030 响应。
- `YahooChartQuoteAdapter`：取得最近价格、交易时间、报价币种和交易所元数据，作为独立交叉核对及主源不可用时的价格回退。
- 两个来源都不可用时，使用仍在新鲜度窗口内的最近成功快照；不存在安全缓存时，自动 DCF 标记 `UNAVAILABLE`，但不阻止正常公司研究。

免 Key 来源属于外部公共数据接口，不能假设永久稳定。因此必须具备：

- adapter 级响应 schema 校验、超时、有限重试、熔断和 provider health；
- A 股、港股、美股的集中 symbol mapper，不在调用者中散落后缀判断；
- 主源/副源价格差异检查，差异超限时标记 `CONFLICT` 而不是任意选值；
- 原始响应不得直接进入模型，只保存归一化市场快照与最小审计字段；
- provider 字段变化只需修复 adapter，不影响研究、报告和 DCF interface。

可选增强来源保留 Alpha Vantage 等用户自行配置的 adapter：

- 用户配置后可作为更稳定的优先源或公共免 Key 来源失败后的回退；
- API key 只存本地安全配置，不进入研究报告、日志、数据库快照或错误信息；
- 未配置任何 key 时，研究入口和默认反向 DCF仍可直接使用。

最终优先级由策略决定，默认顺序为：本次请求内 single-flight → 新鲜缓存 → 免 Key 主源 → 免 Key副源 → 用户配置源 → 可选手工覆盖。手工值永远必须由用户显式选择，不能静默覆盖自动值。

### 4.3 汇率与跨币种

- 上市币种等于报告币种时不转换。
- 不同币种时通过独立 `FxSnapshotAdapter` 获取与报价日相同或最近前一工作日的参考汇率；首个生产 adapter 使用 ECB 官方日度参考汇率。
- 每次转换保存原金额、原币种、目标币种、汇率、汇率日期和来源。
- 行情币种与已验证的交易所/证券币种冲突时标记 `CONFLICT`，不得静默猜测。
- 无可接受 FX 快照时不执行跨币种 DCF；报告说明缺少的是行情、汇率还是证券映射。

### 4.4 正确的反向 DCF 口径

2.5.0 将现有“市值近似企业价值”替换为明确的权益反向 DCF：

- 以规范化 `经营现金流 - 资本开支` 作为有清晰标注的 FCFE proxy；
- 目标值使用同币种、同证券/发行人范围的 equity market value，而不再称为 enterprise value；
- 自动选择截至行情日当时已公开的最近完整财年或 TTM 基础现金流，禁止未来信息穿越；
- 报告明确基础现金流期间、口径和限制；
- 折现率、永续增长率与预测年限保留可编辑默认值和手工覆盖 provenance；
- 求解器返回隐含增长率、可解区间、敏感性矩阵和结构化失败原因；
- 自由现金流非正、数据范围不匹配、双重上市市值不完整或币种不可统一时，不给出伪精确结果。

如果未来引入 FCFF/WACC 企业价值模型，应作为另一估值策略 adapter；不得在同一公式中混用 FCFE 与企业价值。

### 4.4.1 零填写默认策略

普通用户不需要填写折现率、永续增长率、预测年限或任何行情字段。新增版本化 `ReverseDcfPolicy`：

- 默认采用 5 年显性期、10% 权益折现率、3% 永续增长率；这些是清楚标注的模型政策假设，不伪装成实时市场事实。
- 自动值始终配套显示敏感性矩阵，避免单一假设造成伪精确。
- 高级设置仍允许用户覆盖假设；覆盖值保存 `source=manual_override`。
- 后续若引入按币种的无风险利率和权益风险溢价，只替换 policy 的内部 adapter，不扩大研究入口 interface。
- 默认假设、policy version 和任何覆盖值写入研究快照，保证报告可复现。

### 4.5 双重上市、股本与市值安全规则

- 优先采用供应商直接提供且可验证的发行人/证券市值。
- 只有在股份数的证券类别、日期、复权与 listing identity 明确一致时，才允许 `price × shares` 派生市值。
- A/H、ADR、多类别股票不得用单一挂牌价格乘公司全部股份数；无法覆盖全部权益类别时返回明确的 `MARKET_CAP_SCOPE_INCOMPLETE`。
- 手工市值覆盖必须显示“手工数据”、日期和币种，不得伪装为自动行情。

### 4.6 前端交互与可观察性

- 选择公司后在后台预取行情，不阻塞主线程；发起研究时再次确认快照新鲜度。
- 发起研究区不再要求填写 DCF 表单；公司已选择即可使用自动市场快照与版本化默认假设开始研究。
- “高级估值”改为可选行情快照卡：自动/缓存/手工状态、价格、市值、日期、币种、来源和刷新按钮清晰可见。
- 用户可以采用自动值，也可以显式切换为手工覆盖；两者差异和来源可见。
- 行情连接测试、刷新、研究和重试均不得冻结窗口或禁用无关按钮。
- 简体中文、繁体中文、英文文案与错误码语义一致。
- 报告的反向 DCF 区域显示输入来源、日期、币种转换、基础现金流期间、隐含增长与敏感性，不显示无来源数字。

### 4.7 QA 遗留项的根治方案

#### 重试状态与报告刷新

- 将重试状态绑定到 operation/run identity；历史记录只用于诊断，不能成为当前 UI 状态。
- 新研究开始时创建新的 operation；完成/取消/失败均写明确终态。
- 当前运行成功或没有活跃重试时隐藏警示横条；前端读取后端终态，不再从旧 `last_stage` 猜测。
- canonical 财务数据更新后，以数据集 digest/version 触发度量缓存和报告投影失效与重建。

#### 反方观点投影

- 在投影 seam 对 Agent 的合法别名做一次 canonical normalization，不在每个渲染器分别堆补丁。
- canonical 结构完整保留 strongest arguments、unsupported assumptions、missing evidence 和 claims。
- Markdown/HTML/前端均从同一 canonical 结构渲染；遇到未知但安全的文本结构时保留可读 fallback。
- 有上游正文时禁止生成空容器；确实无内容时显示明确的“未生成/证据不足”状态和诊断。

#### 研究耗时

- 默认启用经过能力检查的基础 Agent 有界并行；供应商明确不支持并发时才退化为顺序执行并展示原因。
- 把调度表示为显式依赖图，只有真实依赖才串行。
- 最终合成只消费 canonical、去重、长度受控的 section projection，不重复传入完整证据和上游原文。
- 结构化输出修复按缺失 section 定向执行，禁止整份报告无差别重跑。
- 保存各 Agent 排队、首 token、生成、验证和修复耗时；UI 区分模型排队与程序计算。
- 免费限速端点给出真实性能提示，不以减少章节、结论、证据要求或验证门换取速度。

## 5. 外部数据选择依据

- 东方财富公共报价响应包含最新价、更新时间和总市值字段，可覆盖本项目主要的 A/H/美股入口；因其并非稳定契约，必须封装并由其他来源、缓存和 schema 测试保护，不能让供应商字段泄漏到业务调用者。
- Yahoo Chart 公共响应提供价格、交易时间和币种元数据，可作为免 Key 的第二报价 adapter 与交叉核对来源；它同样不是长期稳定契约，不能成为唯一来源。
- Alpha Vantage 官方文档提供 `GLOBAL_QUOTE`，并给出中国证券代码示例；免费 key 可使用日终更新行情，保留为用户自选的文档化增强 adapter。
- ECB Data Portal 提供正式 SDMX 日度汇率接口，可按日期和币种查询，适合作为可审计 FX adapter。
- 外部供应商的可用性、限额和市场覆盖会变化，因此 provider 细节必须封装在 adapter 内，缓存和手工覆盖不能绕过验证。

## 6. 预计代码范围

以实施后的实际 diff 为准，预计包括：

- 新增 `src/openthesis/market_snapshot.py`（深模块、数据模型、缓存策略和 adapter seam）；
- `src/openthesis/market_data.py` 或独立 provider adapter 文件；
- `src/openthesis/financials.py`；
- `src/openthesis/research.py`；
- `src/openthesis/service.py`；
- `src/openthesis/storage.py`；
- `src/openthesis/report_projection.py`；
- `src/openthesis/reporting.py`；
- `src/openthesis/report_html.py`；
- `desktop/src/features/research/NewResearchView.tsx`；
- `desktop/src/features/report/ReportWorkspace.tsx`；
- `desktop/src/types.ts`、`desktop/src/protocol.ts`、`desktop/src/i18n.ts`、`desktop/src/styles.css`；
- 对应 Python、frontend、Rust 测试与 2.5.0 版本元数据。

## 7. 验收清单

### 自动市场快照

- [ ] 未配置任何市场数据 API key 时，选定公司后自动获取价格、最近交易日期、报价币种和市值，无需手工填写。
- [ ] 未填写价格、市值、日期、币种、折现率、永续增长率或预测年限时，研究可正常发起，自动 DCF 使用可审计默认策略。
- [ ] A 股、港股、美股各至少一个 fixture 覆盖 symbol mapping、价格、日期、币种和市值。
- [ ] 免 Key 主源和副源分别具备成功、字段变化、超时、限流、空响应与冲突测试；任一来源失效不会令应用崩溃或界面冻结。
- [ ] 免 Key 来源均失败时自动使用合格缓存；无缓存时只停用 DCF，不阻止其余研究，也不要求用户必须补录数据。
- [ ] 行情响应保存 provider、retrieved-at、as-of 和来源，不保存 API key。
- [ ] 缓存命中、过期、429、认证失败、网络失败、字段缺失和供应商冲突均有稳定错误码与测试。
- [ ] 手工覆盖可用且 provenance 明确；不会被自动刷新静默覆盖。
- [ ] 行情刷新和连接测试不阻塞 UI。

### 汇率与估值正确性

- [ ] 上市币种与报告币种相同不转换，不同币种通过有日期和来源的 FX 快照转换。
- [ ] 周末/休市日使用最近前一有效报价/汇率日期并清楚显示，不伪装成当天实时值。
- [ ] 反向 DCF 的现金流口径与 equity market value 一致，不再把市值称为企业价值。
- [ ] DCF 输入仅使用截至行情日已公开的数据，无 look-ahead。
- [ ] 隐含增长求解、不可解范围、非正 FCF、陈旧行情、币种冲突和不完整多类别市值有测试。
- [ ] A/H 或多类别股票不会错误地以单一价格乘全部公司股份。
- [ ] 报告三语展示价格、日期、币种、市值、汇率（如有）、基础现金流期间、假设、结果、敏感性与限制。

### QA 待解决项

- [ ] 新研究不会继承旧 `financial_retry_state` 警告或进度。
- [ ] 当前 operation 结束后进度有明确终态，报告完成后不再显示“处理中”。
- [ ] canonical 数据更新会使旧度量/旧报告投影失效并重建。
- [ ] 上游存在反方分析时，投影、Markdown、HTML 和前端均完整显示，未知安全结构不会静默清空。
- [ ] 无反方内容时显示明确状态，不输出空白章节。
- [ ] 基础 Agent 的有界并行默认生效；依赖图、供应商退化与耗时记录可验证。
- [ ] 最终合成输入已去重和限长，定向修复不重跑已通过章节。
- [ ] 不减少现有研究章节、证据、财报数据或验证规则。

### 回归与发布门

- [ ] Python 全量测试通过。
- [ ] Frontend tests、typecheck、production build 通过。
- [ ] Rust tests 通过。
- [ ] A 股、港股、美股反向 DCF 端到端 fixture 通过。
- [ ] 2.4.2 财报识别 golden/regression corpus 无回归。
- [x] Developer acceptance completed from targeted regression and real public smoke checks; Antigravity is optional and is not a release prerequisite.
- [x] 生成无签名 Windows x64 测试包并记录 SHA-256。
- [x] 用户已完成实机测试并批准发布（2026-09-09）。

## 8. 未决决定

1. 是否接受 2.5.0 明确采用“权益反向 DCF（FCFE proxy 对 equity market value）”，而不是继续以市值近似企业价值。
2. 对超过 7 个自然日的行情快照，建议仅展示并尝试后台刷新；刷新失败时研究继续，但旧快照不自动进入 DCF。该阈值可在实施中固化为策略。
3. 免 Key公共接口不提供稳定性 SLA；本方案以双来源、缓存、校验、熔断和可选自备 API 降低风险，但不能承诺第三方接口永不变化。

## 9. 当前结果

- Completed changes:
  - 新增深模块 `MarketSnapshotModule`，提供免 Key 双行情源、规范化快照、持久缓存、single-flight、熔断、有界并行、币种/日期/市值范围校验和 ECB 双腿汇率。
  - 普通用户选择公司后可零填写发起研究；行情预取不阻塞界面，自动估值失败只停用 DCF，不阻断研究。
  - 反向 DCF 改为 FCFE proxy 对权益市值，并只选择行情日前已披露的最近完整财年，排除 Q1/H1/Q3 累计数据。
  - 最终综合采用 32 KB 硬上限的确定性 projection；结构修复使用专用 section-patch 提示词并在本地合并，不重写已通过章节。
  - 修复旧运行财报恢复状态污染、反方分析投影/HTML 丢失、默认并行配置和三语行情状态。
  - 根据 `docs/qa/2.5.0qa.md` Round 3 的真实环境结果，新增免 Key 的 Nasdaq 美股价格/市值适配器，Yahoo Chart 继续作为价格交叉校验源；默认链路在东财美股端点失效时仍可生成发行人口径的可审计权益市值。
  - HTTPS 运输改用 PyPA `truststore` 专用 `SSLContext`，在 Windows 上使用 CryptoAPI 系统信任链；未关闭证书或 hostname 校验，也未全局注入 SSL 行为。
  - 行情熔断键改为 `provider + market`，美股端点失败不再连带熔断 A 股/港股；不支持的市场不计入 provider 失败。
  - Markdown 与 HTML 共用反向 DCF 状态和免责声明的简体/繁体/英文映射，英文和繁体不再透传上游简体中文 `reason`。
  - 自备行情 API 目前仅提供后端安全 adapter factory seam；2.5.0 没有用户可操作的配置 UI，因此不计为已完成的用户功能。
  - 关闭 QA Round 4 的 `BUG-250-05`：默认行情链现为 Eastmoney → Tencent → Nasdaq → Yahoo；腾讯适配器只使用经过证书与主机名校验的 HTTPS，A 股采用 `sh/sz/bj`、港股采用 `hk` 映射，响应最多读取 2 MB，原始内容不持久化也不进入模型。
  - 关闭 QA Round 4 的 `BUG-250-06`：3 个 TLS 回归测试已改为零参数 `unittest.mock.patch`，不再要求未安装的 pytest `monkeypatch` fixture。
- Files intended for publication: `src/openthesis/market_snapshot.py`、`financials.py`、`research.py`、`service.py`、`storage.py`、报告投影/渲染文件、官方 OT 包与专用 repair prompt、桌面端研究/报告/i18n/protocol/type 文件、相应测试及 2.5.0 版本元数据。实际发布范围仍须在 QA 通过后以 Git diff 复核。
- Release artifact path: `D:\githubmax\installer-output\OpenThesis-2.5.0-windows-x64-portable.zip`
- Release artifact hash: `EF081A576C300FE57E6AFA3FB51E0255F55AC4376171FBB13DC35DBB7DB4C455` (SHA-256)
- Developer verification (not final QA): 行情/DCF/synthesis/service/report 定向 Python 测试通过；frontend Vitest 18 项通过；TypeScript `tsc -b`、Python compileall 和 diff check 通过。Round 3 修复后，新增的美股副源、TLS context、按市场熔断和三语 HTML 回归全部通过；真实公网 smoke test 于 2026-09-09 成功取得 AAPL 价格、交易日、发行人市值，并成功取得 ECB HKD/CNY 带日期参考汇率。Round 4 修复后，Tencent 与 TLS 针对性回归 7/7、market snapshot FunctionTestCase 37/37、HTML/reporting 32/32、`py_compile` 与 `git diff --check` 通过；真实 HTTPS smoke 成功取得比亚迪 `002594.SZ` 与腾讯 `00700.HK` 的当日价格、证券市值、币种和日期。
- Acceptance results: BUG-250-01..06 已完成修复，并通过开发侧精确回归与真实公网 smoke test（AAPL、ECB HKD/CNY、比亚迪、腾讯）。无签名 Windows x64 测试包已生成；产物完整性检查确认 GUI 子系统、主程序与 Sidecar 必需条目、无可见控制台进程及 `truststore 0.10.4` PyInstaller 收集清单。按用户要求未调用 Antigravity、未重复执行隐私扫描；用户已完成独立实机测试并批准发布。

### 9.1 待 QA 执行的最小测试清单

1. `python -m pytest tests/test_market_snapshot.py tests/test_research_workflow.py tests/test_service.py tests/test_report_html.py tests/test_reporting.py`
2. `python -m pytest`（全量 Python 回归与 2.4.2 财报 golden corpus）。
3. 在 `desktop` 目录执行 `npm test` 与 `npm run build`。
4. 在 `desktop/src-tauri` 目录执行 `cargo test`。
5. 从真实服务/桌面入口验证 A 股、港股、美股零填写研究；跨币种 ECB；断网、陈旧、冲突、无市值和 A/H scope 场景；确认界面可操作且非 DCF 研究继续。
6. 验证同一证券“旧运行失败后新运行成功”不显示旧重试状态；最终 HTML/UI 显示三类反方内容；三语行情状态无中英混用。
7. 记录真实行情 endpoint 结果和从选择公司到市场快照返回的耗时；不把 fixture 通过当成公网可用性证明。
