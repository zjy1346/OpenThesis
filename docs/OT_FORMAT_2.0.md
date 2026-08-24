# OpenThesis OT 1.0 容器规范（OpenThesis 2.0）

> 文档语言：中文。英文术语首次出现时附中文说明。  
> 应用版本：OpenThesis 2.0.0。  
> 容器协议版本：OT 1.0。应用大版本与容器协议版本独立演进。

## 1. 目标

`.ot` 是 OpenThesis 的声明式研究包容器。它用于表达研究目标、工作流、提示资源、输出约束、UI Schema（界面结构描述）、能力要求、依赖和来源信息，不用于携带可执行代码、模型、API Key 或任意网络逻辑。

设计目标：

1. **确定性**：同一规范化草稿应生成完全相同的文件字节。
2. **可验证**：每个运行时资源都有长度、媒体类型和 SHA-256。
3. **最小权限**：OT 1.0 不授予网络、文件系统、秘密或代码执行能力。
4. **渐进式复杂度**：新手可以通过 OT Studio 表单编写，专业用户可以编辑 JSON。
5. **可扩展**：未知可选扩展可以保存在 `optional_extensions`；未知必需能力在检查模式下产生诊断，在执行模式下拒绝。
6. **可复现**：Manifest（清单）、Lockfile（锁定文件）、资源哈希和 Content Identity（内容身份）共同描述一次构建。

## 2. 容器与文件名

- 公共扩展名：`.ot`。
- 容器格式：ZIP-compatible（兼容 ZIP）。
- 文本编码：UTF-8。
- 路径分隔符：`/`。
- 所有路径必须是 Unicode NFC、相对路径，且不得包含 `..`、`.`、反斜线、NUL 或冒号。
- 文件名比较同时检查原文和大小写折叠，避免 Windows/macOS 上的大小写冲突。
- `.othesis` 不是 2.0 的兼容输入格式。

保留路径：

| 路径 | 必需 | 说明 |
| --- | --- | --- |
| `manifest.json` | 是 | 包、权限、能力、资源和完整性清单 |
| `ot.lock.json` | 是 | 包 ID、版本、依赖和资源哈希锁定 |
| `README.md` | 否 | 人类可读说明，不参与资源声明 |
| `resources/**` | 按清单 | 工作流、提示、输出和 UI 资源 |

编译器当前生成：

- `resources/workflow.json`
- `resources/output.json`
- `resources/ui-form.json`
- `resources/prompts/{step-id}.md`

## 3. 确定性编译

OT Studio 编译器执行以下规范化：

- JSON 对象键按字典序排序；
- JSON 使用紧凑分隔符，不输出 NaN/Infinity；
- ZIP 条目按路径排序；
- ZIP 时间戳固定为 `1980-01-01 00:00:00`；
- 文件模式固定为普通只读数据文件 `0644`；
- 当前使用 `ZIP_STORED`，避免压缩器版本造成字节差异；
- 提示文本去除两端空白并以一个换行结束。

因此，同一有效草稿在同一 OT 协议实现中必须得到相同字节。测试会同时比较完整字节和内容身份。

## 4. Manifest

最小结构示意：

```json
{
  "ot_version": "1.0",
  "schema_version": "1",
  "package": {
    "id": "my.company-research",
    "name": "Company Research",
    "version": "1.0.0",
    "kind": "openthesis.research-pack",
    "description": "Evidence-first company research.",
    "license": "Apache-2.0",
    "compatibility": { "openthesis": ">=2.0.0,<3.0.0" }
  },
  "permissions": {
    "network": [],
    "filesystem": "none",
    "execute_code": false,
    "secrets": "prohibited"
  },
  "required_capabilities": [
    "openthesis.workflow.v1",
    "openthesis.output-schema.v1"
  ],
  "resources": [],
  "optional_extensions": {},
  "relationships": [],
  "integrity": {
    "algorithm": "sha256",
    "content_identity": "..."
  }
}
```

`package.id` 必须是 3–128 位稳定小写标识符，可使用小写字母、数字、点、下划线和横线；`package.version` 使用 SemVer（语义化版本）形式。

OT 1.0 已知必需能力：

- `openthesis.workflow.v1`
- `openthesis.deterministic-transform.v1`
- `openthesis.output-schema.v1`

未知必需能力的处理：

- 检查/展示：保留诊断，允许工具解释包元数据；
- 安装或执行：作为错误拒绝，不能降级执行或猜测含义。

## 5. 资源记录

每个运行时资源必须在 `manifest.resources` 中声明：

```json
{
  "id": "prompt.financial-analysis",
  "type": "openthesis.prompt",
  "schema": "ot://openthesis/prompt/1",
  "media_type": "text/markdown",
  "path": "resources/prompts/financial-analysis.md",
  "bytes": 128,
  "sha256": "...",
  "purpose": "runtime",
  "privacy": "public"
}
```

允许的媒体类型：

- `application/json`
- `application/jsonl`
- `text/markdown`
- `text/plain`
- `text/csv`

未声明文件、资源缺失、长度不符、哈希不符、重复 ID、保留路径占用或不支持的媒体类型都会拒绝。

## 6. Lockfile

`ot.lock.json` 结构：

```json
{
  "ot_version": "1.0",
  "package_id": "my.company-research",
  "package_version": "1.0.0",
  "dependencies": [],
  "resources": [
    { "id": "workflow.main", "sha256": "..." }
  ]
}
```

加载器会检查 OT 版本、包 ID、包版本、依赖数组，以及 Lockfile 的资源 ID/哈希集合是否与 Manifest 完全一致。Lockfile 缺失、不是 UTF-8 JSON 或资源集合不同都会拒绝。

2.0 已定义依赖字段和锁定语义，但不会从网络自动解析或下载依赖。未来依赖解析器必须是显式用户操作，并受独立的来源、签名、能力和网络策略约束。

## 7. Content Identity

内容身份使用 SHA-256：

1. 从 Manifest 中移除 `integrity`；
2. 对剩余 Manifest 生成规范 JSON；
3. 按资源路径排序；
4. 依次加入资源路径、资源 SHA-256 和资源字节数；
5. 计算最终 SHA-256。

内容身份标识语义内容和资源集合，不代替发布者签名。未来增加签名时，签名必须建立在内容身份之上，并拥有独立的信任根、撤销和时间策略。

## 8. 安全预算

| 限制 | 当前值 |
| --- | ---: |
| `.ot` 原始大小 | 10 MB |
| 解压后总大小 | 20 MB |
| 单个资源 | 2 MB |
| ZIP 条目数 | 256 |
| 单资源压缩比 | 100:1 |

拒绝的内容包括：

- 路径穿越、绝对路径、反斜线和非 NFC 路径；
- 重复或仅大小写不同的路径；
- 符号链接；
- `.py`、`.js`、`.exe`、`.dll`、Shell/PowerShell 脚本等可执行内容；
- ZIP、`.othesis` 和嵌套 `.ot`；
- Manifest 未声明文件；
- API Key、Access Token、Refresh Token、密码、`sk-` 形式秘密和 Bearer Token 特征；
- 网络、文件系统、秘密或任意代码执行权限；
- 未知必需执行能力。

秘密扫描覆盖 Manifest、Lockfile、README 和资源，而不只覆盖 Prompt。扫描用于阻止常见误提交，不应被宣传为通用数据防泄漏系统；作者仍有责任不把秘密写入草稿。

## 9. OT Studio 草稿约束

当前引导模式限制：

- `horizon_years`：1–20；
- `depth`：1–5；
- `risk_emphasis`：1–5；
- `report_language`：`zh-CN`、`zh-Hant` 或 `en`；
- 输出格式：`markdown`、`json` 或 `html`；
- 工作流至少一个步骤；
- 步骤 ID 必须唯一；
- 依赖只能引用已有步骤，且不能形成循环；
- Role（角色）、Output Schema（输出结构）和 Prompt 均不能为空。

自然语言助手只能返回一个已选择路径的建议。应用显示修改前/修改后内容；用户明确接受后才应用。建议不能绕过同一草稿验证器和编译器。

## 10. 版本与扩展策略

- OpenThesis 2.x 首发只执行 OT 1.0。
- 新增可选元数据优先进入带命名空间的 `optional_extensions`。
- 会改变执行语义的功能必须分配新的 `required_capabilities`。
- 不认识的必需能力不得静默忽略。
- OT 容器协议升级不要求应用大版本同步升级，但兼容范围必须写入 `package.compatibility`。
- Provider 凭据、模型账户和机器绝对路径永远不是可移植 OT 内容。

## 11. 旧官方包迁移

2.0 不提供面向用户的 `.othesis` 转换器。项目自有的旧官方包位于开发工具隔离目录，由 `scripts/convert_official_pack_to_ot.py` 受控转换。转换器固定源目录，记录源内容 SHA-256，并比较步骤 ID、角色、依赖、Prompt 和顺序，验证语义等价后才写入官方 `.ot`。

当前受控迁移源 SHA-256：

`3cc341a8e21be69b8775598aa7a43f11375187609f29366b991965ccc5abac9e`

当前官方 OT Content Identity：

`0753806ce7c284f8f00175e70dd514612567cfbd17fe827aa7cfe5d3bb3f9271`
