# OpenThesis 2.0 模型配置与密钥安全研究

> 研究日期：2026-08-23  
> 适用范围：Windows 桌面版优先，同时为 macOS/Linux 保留可扩展接口。  
> 研究原则：只采用官方文档、官方源码仓库或维护者发布的官方资料；免费额度、模型列表和服务状态均视为会变化的运行时信息。

## 结论摘要

OpenThesis 2.0 应采用“操作系统凭据库保存秘密，Rust/Tauri 后端持有秘密，前端和 Python sidecar 只接触非秘密标识或一次性请求”的架构。

具体建议如下：

1. Windows 默认使用 Windows Credential Manager 的 Generic Credential（通用凭据）。Rust 侧通过 `keyring` 的 Windows 原生后端访问；凭据目标名按 `provider/account/profile` 分层命名。
2. 不把 API key 放入 SQLite、`.ot`、研究报告、日志、命令行参数、环境变量、前端状态、Tauri 事件或可导出的配置文件。
3. Rust/Tauri 提供最小权限的 Secret Broker（密钥代理），负责保存、读取、测试、轮换和删除；React/TypeScript 页面只能看到已配置状态、掩码、模型名、能力和错误信息。
4. Python sidecar 不应持有长期密钥。优先让 Rust 代理完成需要认证的 HTTP 请求；若迁移阶段必须由 Python 发请求，则通过受限的本地 IPC/标准输入传递短生命周期的内存秘密，并在请求结束后清零，绝不通过命令行或持久化环境变量传递。
5. SQLite 只保存非秘密元数据、凭据引用、版本和状态。SQLite 公共版本本身没有透明加密；官方 SQLite Encryption Extension（SEE）是商业扩展，因此不应作为 OpenThesis 2.0 的默认免费方案。
6. `tauri-plugin-stronghold` 是官方 Tauri 2 插件，可用于加密数据库，但它需要应用管理 vault 密钥；其 JS API 还存在“前端可以接触秘密”的边界问题。因此它不作为 Windows API key 的第一选择，可作为未来跨平台可移植保险库的候选。
7. Ollama 只检测并连接用户已经安装的本地服务，不内置 Ollama 或模型；它的 OpenAI 兼容端点默认在 `http://localhost:11434/v1/`，示例中的 API key 参数是必填但会被忽略。
8. GitHub Models 在当前研究日期已经由官方标记为全面退役（2026-07-30），不能列为 2.0 的免费模型适配器。OpenRouter Free 可以作为可选云端免费适配器，但官方明确给出低额度、可用性和模型列表变化风险。

## 1. Windows Credential Manager 与 DPAPI

### 1.1 Windows Credential Manager

Microsoft 将 Generic Credentials 定义为由应用自行完成授权的凭据类型，并说明应用可以使用 Credentials Management API 对其进行长期存储。`CredWrite` 可创建或修改当前用户的凭据，`CredRead` 读取当前登录会话关联的凭据；Generic Credential 的 `TargetName` 应标识使用该凭据的服务。官方资料：

- [Microsoft：Kinds of Credentials](https://learn.microsoft.com/en-us/windows/win32/secauthn/kinds-of-credentials)
- [Microsoft：wincred.h API 总览](https://learn.microsoft.com/en-us/windows/win32/api/wincred/)
- [Microsoft：CredWriteW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)
- [Microsoft：CredReadW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw)

适合 OpenThesis 的用法：

```text
服务名/TargetName：OpenThesis/2/provider/{provider_id}/account/{account_id}
用户名字段：账户显示名或 provider account id（不放 API key）
CredentialBlob：API key 或 provider 需要的密钥材料
类型：CRED_TYPE_GENERIC
持久性：用户级持久化，不使用机器全局凭据
```

`CredWrite` 在同一目标和类型下会替换现有凭据，因而轮换实现必须先写入新版本并完成连通性测试，再更新 SQLite 中的“活动版本”，最后删除旧目标或旧版本。删除操作使用 `CredDelete`，不应只删除 SQLite 行而留下系统凭据。

威胁边界：Credential Manager 解决的是“磁盘上和跨普通应用读取”的保护，不是对当前用户下恶意进程、调试器、管理员或已经被攻陷的应用进程的绝对保护。应用在请求 provider 时仍需短暂解密/读取到内存，必须限制生命周期、日志和转储暴露。

### 1.2 DPAPI

`CryptProtectData` 会为数据创建会话密钥和完整性校验；默认情况下通常只有相同用户凭据、同一台计算机才能解密。Microsoft 同时明确说明：如果使用 `CRYPTPROTECT_LOCAL_MACHINE`，本机任何用户都可能解密，因此 OpenThesis 不应为 API key 使用该标志。官方资料：

- [Microsoft：CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
- [Microsoft：CryptProtectData 示例与范围限制](https://learn.microsoft.com/en-us/windows/win32/seccrypto/example-c-program-using-cryptprotectdata)
- [Microsoft：Windows 威胁缓解技术（DPAPI 与 Credential Manager）](https://learn.microsoft.com/en-us/windows/win32/secbp/threat-mitigation-techniques)

DPAPI 适合：

- 加密 OpenThesis 自己的本地非导出配置或 envelope；
- 保护一个只在同一 Windows 用户和机器上使用的本地密钥材料；
- 作为 Windows 专用的直接 Rust FFI 方案。

DPAPI 不适合直接解决：

- 跨电脑迁移同一密钥；
- 多 Windows 用户共享秘密；
- 服务器端或云端同步；
- 已经被当前用户进程控制时的秘密保护。

Microsoft 还指出，管理员重置用户密码可能导致原有保护数据无法恢复，除非有恢复代理或 DPAPI 备份密钥。因此 OpenThesis 必须提供“凭据不可恢复时重新配置”的可理解错误，而不是承诺永久可恢复。

### 1.3 选择

Windows 2.0 的第一实现应使用 Credential Manager，而不是自行在 SQLite 中做一层 DPAPI 加密。原因是：Credential Manager 已经提供了用户级目标、读写、删除和系统凭据管理；OpenThesis 只需管理稳定命名、版本和生命周期。直接 DPAPI 作为后备适配器保留，以便在特殊部署环境中使用，但不与 `CRYPTPROTECT_LOCAL_MACHINE` 混用。

## 2. Tauri 2 与 Rust 生态方案

### 2.1 `keyring` crate

官方 docs.rs 文档显示，`keyring` 的 v1 API 会自动选择平台原生凭据库：Windows Credential Manager、macOS Keychain Services、类 Unix 的 Secret Service。文档还建议需要更多控制时直接依赖 `keyring-core` 与具体 store，而不是把所有可选后端都带入产品。

- [docs.rs：keyring](https://docs.rs/keyring/latest/keyring/)
- [docs.rs：keyring::v1](https://docs.rs/keyring/latest/keyring/v1/index.html)
- [docs.rs：keyring CLI 与平台 store](https://docs.rs/keyring/latest/keyring/cli/index.html)

适用性：

- Windows 优先、跨平台可扩展；
- API 小，适合被 Rust Secret Broker 封装；
- 不增加模型或 Ollama 体积；
- 可对 provider 账户进行独立条目管理。

限制：

- Linux 的 Secret Service 依赖用户桌面密钥服务，最小发行版可能不可用；
- 原生凭据库的行为和错误码跨平台不同；
- 应在首次保存、读取、删除和升级后各平台测试；
- `keyring` 本身不是威胁模型，也不会阻止应用把读取到的秘密写日志或返回前端。

### 2.2 `tauri-plugin-stronghold`

Tauri 官方插件工作区将 Stronghold 描述为“Encrypted, secure database（加密安全数据库）”；插件使用 IOTA Stronghold，并支持 Tauri 2。当前官方 changelog 显示 v2 系列仍有维护，检索到的最新发布为 2.3.1。

- [Tauri 官方插件工作区](https://github.com/tauri-apps/plugins-workspace)
- [Tauri Stronghold API 文档](https://v2.tauri.app/reference/javascript/stronghold/)
- [Tauri Stronghold 官方源码镜像](https://github.com/tauri-apps/tauri-plugin-stronghold)
- [Stronghold v2 changelog](https://github.com/tauri-apps/plugins-workspace/blob/v2/plugins/stronghold/CHANGELOG.md)
- [Stronghold 官方文档改进问题（前端秘密暴露边界）](https://github.com/tauri-apps/plugins-workspace/issues/1959)

适用性：

- 未来需要跨平台加密容器、非 OS 原生凭据库或可携带 vault 时可评估；
- 可以为非导出的本地应用数据提供加密存储。

不作为 API key 首选的原因：

- 应用必须管理 vault 解锁密钥或密码；
- 官方示例主要通过 JS guest API 操作 vault，若直接在前端处理，前端依赖或 XSS/供应链问题可能读取秘密；官方 issue 也明确提出了这一疑问；
- Stronghold 的加密数据库不等于“前端永远看不到明文”；
- 对 Windows 单用户 API key，系统 Credential Manager 的用户绑定和生命周期更直接。

如果未来使用 Stronghold，必须由 Rust 层创建和解锁 vault，前端只能调用不返回明文的业务命令。vault 解锁材料不能硬编码，不能由前端传入后长期缓存。

### 2.3 Tauri 权限与窗口隔离

Tauri 2 的 capability（能力）系统为窗口/WebView 分配命令权限；官方说明权限边界可以降低前端被攻陷后的影响，但不能保护恶意或不安全的 Rust 代码，也不能弥补过宽 scope（范围）。Tauri 还建议只暴露必要能力，并可以为不同窗口配置不同 capability。

- [Tauri：Capabilities](https://v2.tauri.app/security/capabilities/)
- [Tauri：Runtime Authority](https://v2.tauri.app/security/runtime-authority/)
- [Tauri：Security 总览](https://v2.tauri.app/security/)
- [Tauri：使用插件权限](https://v2.tauri.app/learn/security/using-plugin-permissions/)

模型配置页需要的命令应拆成最小接口，例如：

```text
list_provider_metadata() -> 非秘密元数据
list_configured_accounts() -> provider、账户显示名、已配置状态、掩码
save_secret(provider, account, secret) -> 成功/错误，不返回 secret
test_connection(provider, account) -> 状态、公开错误分类
delete_secret(provider, account) -> 状态
rotate_secret(provider, account, secret) -> 状态
```

不要提供 `get_secret()` 给前端。研究窗口只应提交 `credential_ref`，不能提交 API key。

## 3. Python sidecar 与密钥持有者

### 3.1 Tauri sidecar 的官方边界

Tauri 官方将 Python CLI/API server 等二进制视为 sidecar，可由 `externalBin` 打包并通过 shell plugin 执行。Tauri 2 的 capability/permission 仍是调用边界，不能把 sidecar 当成天然可信的密钥保险箱。

- [Tauri：Embedding External Binaries](https://v2.tauri.app/develop/sidecar/)
- [Tauri：Node.js sidecar 示例](https://v2.tauri.app/learn/sidecar-nodejs/)
- [Tauri：IPC 概念](https://tauri.app/concept/inter-process-communication/)

### 3.2 推荐持有关系

```text
React/TypeScript 前端
    │ 仅发送 provider_id / credential_ref / request_id
    ▼
Rust/Tauri Secret Broker
    │ 读取 Credential Manager / OS keyring
    │ 记录最小审计元数据，不记录秘密
    ├── 首选：Rust 代理直接完成带认证的 provider HTTP 请求
    └── 迁移期：短生命周期本地 IPC/标准输入传给 Python sidecar
```

首选是 Rust 代理完成请求，Python 只接收已清理的模型响应、结构化结果或不含密钥的研究上下文。这样前端和 Python 都没有长期密钥读取接口。

如果现有 Python provider 代码暂时不能迁移：

- 不把 key 放在命令行参数、sidecar 参数、持久化环境变量或临时文件；
- 使用 Rust 创建的私有进程通道（Windows named pipe 或受限 loopback/标准输入），只允许当前 OpenThesis sidecar 连接；
- 每次请求使用 request-scoped secret lease（请求范围租约），传递后立即清零；
- sidecar stdout/stderr、异常、traceback、调试日志全部进行 secret redaction（秘密脱敏）；
- Rust 验证 provider_id、模型、URL 和能力，拒绝 Python 自由指定任意地址，防止 key 被外带；
- sidecar 结束或取消时主动撤销租约并清理内存；
- 不把 key 放在 `.ot`、研究运行记录、cache、Crash dump 或报告中。

进程内内存无法提供绝对防护；具备当前用户调试权限的恶意程序仍可能观察内存。该方案的目标是缩短暴露时间、减少可访问组件并阻止正常前端调用链泄露。

## 4. SQLite 与密文

SQLite 官方安全文档建议把来自其他安全域、可能被篡改的数据库视为不可信输入，并在高敏感场景启用完整性检查、限制输入、关闭不必要的触发器/视图等防护。

- [SQLite：Defense Against The Dark Arts](https://www.sqlite.org/security.html)

SQLite 官方提供 SEE（SQLite Encryption Extension），可加密整个数据库、回滚日志等，但官方资料明确它是需要购买许可证的商业扩展；SQLite 支持页面列出的 SEE 一次性源代码许可证价格为 2000 美元。

- [SQLite：SEE](https://sqlite.org/com/see.html)
- [SQLite：SQLite Support Options](https://sqlite.org/support.html)

因此 2.0 推荐：

```text
SQLite：provider metadata、account id、display name、credential_ref、active_version、状态、更新时间
OS keyring：API key、OAuth refresh token、其他长期秘密
.ot / report / cache：绝不保存 API key
```

如果未来确实要加密 SQLite：

- 不把加密密钥硬编码在应用或前端；
- 用 OS keyring 保存数据库密钥，形成“keyring 保护数据库密钥、数据库保存非秘密业务数据”的两层结构；
- 评估商业 SEE 的许可、发行和构建影响；
- 不把“加密数据库”宣传为能抵御当前用户已控制的进程；SEE 官方说明数据在进程内存中会以明文存在。

2.0 不建议为 API key 自行实现 AES + 固定文件密钥。自制 envelope 很容易把密钥放在程序目录、备份文件或日志中，且没有比系统凭据库更好的用户绑定。

## 5. 多 Provider、多账户与生命周期

### 5.1 数据模型

SQLite 中保存如下非秘密字段：

```text
provider_id       规范化 provider 标识
account_id        用户自定义或 provider 返回的非秘密账户标识
display_name      页面显示名
credential_ref    OS keyring target name，不是 API key
secret_version    单调递增版本
status            configured / test_failed / disabled / revoked
capabilities      chat / json / tools / vision 等能力快照
last_tested_at    最近测试时间
created_at
updated_at
```

一个 provider 可有多个账户；研究运行保存 `provider_id + account_id + secret_version`，而不是复制秘密。这样同一 provider 的个人和团队 key 可以并存，研究历史仍可复现使用了哪个配置版本。

### 5.2 添加与测试

用户在模型配置页点击 provider 卡片后：

1. 选择或创建账户；
2. 输入 key；
3. 前端只把 key 发送给 Rust 的 `save_secret`；
4. Rust 写入 OS keyring；
5. Rust 进行最小权限、最低成本的连接测试；
6. 页面只显示成功/失败、账户状态和掩码；
7. 研究下拉栏只列出状态为 configured 且通过策略检查的配置。

### 5.3 轮换、删除、锁定

- 轮换：写入新 Credential target → 测试 → 原子切换 active version → 旧版本延迟删除；
- 删除：先停止新请求，再删除 OS keyring 条目，最后删除 SQLite 元数据；
- 禁用：保留元数据和历史引用，但拒绝新请求；
- 失效：provider 返回 401/403 时标为 `revoked`，不自动重试或反复发送；
- 应用锁定：隐藏配置和研究入口、撤销正在发放的 lease；OS keyring 仍负责持久保护；
- 自动锁定不是内存擦除保证，恢复时必须重新从 OS keyring 读取；
- 退出应用时撤销 lease、关闭 sidecar、清理临时缓存和可清零缓冲区。

### 5.4 导入导出

默认禁止导出 API key。允许导出：provider metadata、模型选择、能力、非秘密配置和脱敏 `.ot` 模板。

如果未来增加“加密备份”：

- 必须是明确的用户操作和二次确认；
- 使用用户输入的备份密码派生密钥，并使用现代认证加密格式；
- 备份文件不能在无密码时恢复；
- 导入先显示内容、目标 provider 和风险，再写入 keyring；
- 导入时不覆盖现有账户，除非用户明确选择轮换；
- 记录格式版本、KDF 参数和失败原因，但不记录密码或 key。

## 6. 免费模型与零成本路径

### 6.1 Ollama：本机服务，不内置

Ollama 官方 Quickstart 说明支持 Windows、macOS 和 Linux；用户自行下载安装并通过 `ollama` 管理模型。OpenAI compatibility 文档给出 `http://localhost:11434/v1/` 兼容端点，并明确示例中的 `api_key='ollama'` 是“required but ignored（必须提供但会被忽略）”。

- [Ollama：Quickstart](https://docs.ollama.com/quickstart)
- [Ollama：OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)

OpenThesis 2.0 行为：

1. 不下载、不内置 Ollama，不把模型权重打包进安装程序；
2. 用户点击“连接已有 Ollama”，应用探测 `localhost`，也允许用户修改为明确的本机地址；
3. 通过 `/v1/models` 发现已安装模型；
4. “免费”标记只表示本机推理不产生 OpenThesis 费用，不代表硬件、耗电或模型许可证没有成本；
5. 不为 Ollama 保存虚假 API key；若兼容客户端要求字段，使用运行时常量 `ollama`，不写入 keyring；
6. 如果用户配置了远程 Ollama URL，必须显示“请求会离开本机”的隐私提示，并要求显式确认。

### 6.2 GitHub Models：当前不可用

GitHub 官方文档当前写明：GitHub Models 已于 2026-07-30 完全退役，playground、模型目录、inference API 和 BYOK 均不再可用。

- [GitHub Docs：GitHub Models](https://docs.github.com/en/github-models)

因此：

- 2.0 不应提供 GitHub Models 的注册教程、API key 表单或“免费”卡片；
- 旧规划中关于 GitHub Models 的内容应标记为历史信息并删除运行时适配器；
- 如果未来 GitHub 发布替代服务，必须重新核对官方文档和权限模型后再增加 provider。

### 6.3 OpenRouter Free：可选云端免费适配器

OpenRouter 官方 Quickstart 提供统一的 OpenAI 兼容接口。其 FAQ 说明免费模型通常有较低限制；未购买 credits 时免费模型通常是每天 50 次，总体可用性和模型列表会变化；FAQ 还说明购买至少 10 credits 后免费模型限额可到每天 1000 次，但这不是“永久免费无限”。OpenRouter 官方 Free Models Router 使用 `openrouter/free` 自动选择可用免费模型，特定免费变体可使用 `:free` 后缀。

- [OpenRouter：Quickstart](https://openrouter.ai/docs/quickstart)
- [OpenRouter：FAQ](https://openrouter.ai/docs/faq)
- [OpenRouter：Free Models Router](https://openrouter.ai/docs/guides/routing/routers/free-router)
- [OpenRouter：Free Variant](https://openrouter.ai/docs/guides/routing/model-variants/free)
- [OpenRouter：Create API key](https://openrouter.ai/docs/api/api-reference/api-keys/create-keys)
- [OpenRouter：Pricing](https://openrouter.ai/pricing)

用户步骤：

1. 打开 OpenRouter 官方账户/keys 页面并注册或登录；
2. 创建 API key，设置清晰的名称、过期时间和可接受的消费上限；
3. 复制只显示一次的 key，立即粘贴到 OpenThesis 模型配置页；
4. OpenThesis 保存到 OS keyring，不保存到 `.ot` 或日志；
5. 选择 `openrouter/free` 或带 `:free` 的具体模型；
6. 研究前显示当前免费额度和可用性可能变化；出现 429、模型下线或额度耗尽时明确报告，不自动切换到收费模型；
7. 如果用户只想辅助编写 `.ot`，可在辅助模型选择中只显示已配置的 OpenRouter Free/Ollama 条目。

注意：OpenRouter API key 可能访问多个模型，不能把“free”当作最小权限。应支持 key 的过期时间和金额上限，并把账户余额/限额状态作为可选的非秘密健康检查。

## 7. OpenThesis 2.0 明确推荐架构

### 7.1 分层

```text
模型配置页（React/TypeScript）
  - provider 卡片、图标、Free 标签、状态、模型列表
  - 账户别名、模型能力、注册说明、隐私提示
  - 只显示掩码和配置状态
          │ 最小 Tauri commands / capability
          ▼
Rust/Tauri Model & Secret Broker
  - provider registry 与在线刷新后的签名/校验元数据
  - OS keyring 读写（Windows Credential Manager 优先）
  - 连接测试、轮换、删除、租约、审计脱敏
  - 请求路由和 allowlist，拒绝任意外发地址
          │
          ├── Rust HTTP provider adapters（目标架构）
          └── Python sidecar bridge（迁移期，短租约、无持久 key）
          │
          ▼
SQLite
  - 非秘密 provider/account metadata
  - credential_ref、版本、状态、能力和时间戳

研究运行 / OT 创作工作室
  - 只列出已配置且通过测试的模型
  - 任务保存 provider/account/version，不保存 key
```

### 7.2 Provider Registry

provider 元数据可以在线刷新，但必须：

- 内置一个经过签名或校验的安全基线，离线时仍可打开配置页；
- 在线文档只更新展示元数据、注册链接、endpoint 和能力声明，不直接执行代码；
- 远程配置不能任意改变允许域名、传输安全或密钥发送策略；
- 每次刷新保留版本、来源、时间和校验结果；
- provider 适配器代码仍随应用发布，在线数据不能注入可执行逻辑。

### 7.3 用户体验

模型配置页采用卡片网格：图标、名称、Provider、免费标签、已配置状态和可用能力；点击后展示账户列表和最小字段。研究页、`.ot` 辅助页都复用同一个“已配置模型选择器”，只显示可用账户，不再出现每次输入 API key 的字段。

Emil 风格只用于交互品质，不改变安全边界：短而明确的状态反馈、可中断的测试、键盘可访问、错误靠近输入位置、减少无意义动画；任何复制、显示或导出秘密的操作都必须是显式、短时和可撤销的。

## 8. 验收标准

- Windows 上可在 Credential Manager 中看到 OpenThesis Generic Credential，而 SQLite 和 `.ot` 中没有 key。
- 前端网络、React state、Tauri events、日志和错误消息不出现 key。
- 研究下拉栏只显示已配置并通过测试的模型；Ollama 未安装时不显示为可用。
- Ollama 连接使用用户已有安装，不增加安装包体积。
- GitHub Models 不出现在当前免费模型列表中，并显示已退役的迁移说明（可放在文档，不放运行时 provider）。
- OpenRouter Free 显示免费、额度和可用性风险；429/下线不会静默切换到收费模型。
- 多账户可独立测试、轮换、禁用和删除；历史运行只保留非秘密引用。
- 导出 `.ot`、研究报告和设置备份默认不含 API key。
- sidecar 崩溃、取消、异常和 debug 模式都经过秘密脱敏测试。
- 迁移期 Python provider 与未来 Rust provider 的请求结果和错误分类有一致测试。
- 使用普通用户、管理员、未登录/锁定、凭据删除、密码重置和机器迁移场景进行验证，并明确哪些秘密会因系统恢复边界而要求重新配置。

## 9. 未决风险

1. Windows 当前用户进程被调试或恶意软件控制时，任何方案都无法保证 API key 不在使用瞬间暴露；需要在威胁模型和产品文档中明确。
2. Linux Secret Service、macOS Keychain 和 Windows Credential Manager 的错误、锁定和迁移行为不同，必须做平台测试矩阵。
3. Python sidecar 迁移到 Rust provider 是大型工程；在迁移完成前，短租约 IPC 仍会让 Python 进程短暂接触明文。
4. OpenRouter 免费模型、额度、排队和可用模型列表会变化，不能把固定模型名和固定额度硬编码为协议保证。
5. GitHub Models 已退役，旧文档或用户已有配置需要显示为不可用并提供删除/迁移路径。
6. 在线 provider registry 若没有签名、域名 allowlist 和版本回滚保护，可能成为配置投毒入口。
7. “永久保留”只能表示尽量持久地保存在用户 OS 凭据库，不代表跨设备、密码重置、系统重装或管理员策略变化后永远可恢复。
8. 备份 API key 会显著扩大攻击面；2.0 应先交付非秘密配置备份，延后加密秘密备份，直到格式、KDF、恢复和撤销流程经过独立安全评审。

## 参考资料索引

所有链接均为本报告直接使用的官方资料：

- Microsoft Win32：Credential Manager、DPAPI
- Tauri 官方文档与 `tauri-apps/plugins-workspace` 官方源码
- docs.rs 的 `keyring` 官方 crate 文档
- Tauri 官方 sidecar、IPC 与 capability 安全文档
- SQLite 官方安全文档与 SEE 文档
- Ollama 官方 Quickstart/API 文档
- GitHub 官方 GitHub Models 文档
- OpenRouter 官方 API、FAQ、免费路由和定价文档
