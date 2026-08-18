# OpenThesis Website 1.2.1 GitHub Pages 部署方案

状态：**用户已批准正式部署实施；部署配置已实现、本地验收进行中、待提交推送与线上验证**  
建立日期：2026-08-18  
目标站点：`https://openthesis.cc.cd/`  
目标仓库：`zjy1346/OpenThesis`  
目标分支：`main`

## 1. 用户要求

- 将当前 OpenThesis 宣传网站完整部署到 GitHub Pages。
- `main` 分支更新网站或部署工作流后，GitHub Actions 自动构建并部署。
- 使用自定义域名 `openthesis.cc.cd`，最终通过 HTTPS 访问。
- 修复 Vite/React 的 `base`、静态资源、路由和 Pages 404 风险。
- 不读取、不登录、不修改 DNSHE 账号；DNS 记录由用户手动配置。
- 完成验收后提交并直接推送必要文件到 GitHub。
- 明确列出 GitHub Pages 与 DNS 仍需用户手动完成的设置。

## 2. 当前状态与诊断

### 2.1 技术栈

- 网站位于 `website/`，使用 React 19、TypeScript 7、Vite 8、GSAP 3 与 pnpm lockfile。
- 生产构建命令为 `pnpm --dir website build`，构建输出为 `website/dist/`。
- 网站没有 React Router、BrowserRouter 或客户端路径路由；所有站内跳转均为 `#anchor`，刷新时不会产生 SPA 子路径 404。
- `website/index.html` 是构建入口，Pages artifact 必须让构建后的 `index.html` 位于 artifact 根目录。

### 2.2 Vite base 与静态资源

- `website/vite.config.ts` 已显式设置 `base: '/'`。
- 网站源代码中的产品图片使用 `/product/...` 根路径。
- 因目标是自定义域名根路径 `https://openthesis.cc.cd/`，正确的 Vite `base` 是 `/`；不能使用 `/OpenThesis/`，否则自定义域名下会生成错误的仓库子目录资源路径。
- 本地生产构建已验证该根路径策略；线上脚本、CSS 与图片 URL 仍待 Pages 部署后验证。

### 2.3 GitHub Pages 与 Actions

- 仓库是公开仓库，默认分支为 `main`，连接的 GitHub App 对仓库具有管理员和推送权限。
- 独立 `.github/workflows/deploy-pages.yml` 已实现，不改动 Windows Release 流程。
- Pages 工作流使用官方 `configure-pages`、`upload-pages-artifact@v4`、`deploy-pages@v4` 动作，设置 `pages: write` 与 `id-token: write` 权限，并部署 `website/dist`。
- 工作流的静态触发、权限、build/deploy jobs、artifact 连接和 `github-pages` environment 已完成配置；Actions 实际运行结果待提交推送后验证。

### 2.4 当前 Git 状态与提交边界

- 当前本地分支 `codex/financial-ingestion-v2` 与 `origin/main` 指向同一提交 `09e4af1`，尚无分支分叉。
- 网站源码和网站变更记录目前尚未被 Git 跟踪，因此首次 Pages 部署提交必须包含网站源码本身。
- 工作区另有 `.pnpm-store/`、`tmp/` 和 `.github/release-notes/v1.4.0-rc2.md`；这些不属于官网部署，不会被暂存或提交。
- `website/node_modules/`、`website/dist/`、`*.tsbuildinfo` 已由 `.gitignore` 排除；Actions 将从 lockfile 干净安装并重新构建。

### 2.5 发布工具与执行环境诊断

- GitHub CLI 已安装在 `C:\Program Files\GitHub CLI\gh.exe`，版本为 `2.97.0`。
- 宿主 Windows PowerShell 下，`gh 2.97.0` 已通过 Keyring 登录 `zjy1346`，`active=true`；`gh api user` 返回 `zjy1346`。
- 宿主环境中 `GH_TOKEN` 与 `GITHUB_TOKEN` 均不存在，认证来自已登录的 Keyring。
- 当前执行环境为 `Windows CodexSandboxOffline`，不能读取宿主 Keyring且网络受限；GitHub 网络操作（提交后的推送、Actions/Pages 检查）必须在已批准的宿主环境执行。
- 因此部署配置已经实现，本地验收进行中，待提交推送与线上验证；认证不再是待修复阻塞项。

### 2.6 同仓库隔离结论

- 用户确认采用同一仓库中的独立 `website/` 目录，不另建网站仓库。
- Python 打包只从 `src/` 查找包，并只收集 `src/openthesis/resources/**`；`website/` 不会进入 Python 包。
- Tauri/桌面端和 Windows 打包配置没有引用 `website/`；网站不会进入桌面应用资源或安装包。
- 现有 Windows Release 工作流只监听自身工作流和 Release Note；普通 `website/**` 更新不会触发 Windows 发布。
- GitHub Pages 使用 Actions artifact 部署 `website/dist`；生成的 `dist/` 不提交到 `main`，也不建立承载构建产物的 `gh-pages` 分支。
- 暂存时使用明确文件路径，不使用 `git add -A`；网站依赖、缓存、临时截图、重复 PNG 和测试版发布说明保持在部署提交之外。

## 3. 计划修改

### 3.1 Pages 工作流（已实现）

`.github/workflows/deploy-pages.yml` 已实现：

- 触发：`main` push（限制到 `website/**` 和工作流本身）及 `workflow_dispatch`。
- 并发组：`pages`，避免多个部署互相覆盖。
- 构建任务：checkout、pnpm、Node、依赖缓存、`pnpm install --frozen-lockfile`、typecheck、build。
- artifact：仅上传 `website/dist`，确保 `index.html` 位于 artifact 根目录。
- 部署任务：使用 `github-pages` environment 与 `actions/deploy-pages`。
- 输出：GitHub Actions 记录实际部署 URL。

### 3.2 Vite 配置

- 已在 `website/vite.config.ts` 显式设置 `base: '/'`。
- 保持 `/product/...` 静态资源根路径，因为自定义域名从域名根目录提供网站。
- 不增加 SPA fallback 或 `404.html`；当前没有路径路由，额外 fallback 只会掩盖真正的错误 URL。

### 3.3 自定义域名

- 使用 GitHub Pages Settings 中的 Custom domain 配置 `openthesis.cc.cd`。
- 对 GitHub Actions 发布方式，仓库内 `CNAME` 文件会被忽略且不是必需项，因此不新增无效的 `CNAME` 文件。
- 域名和 HTTPS 状态必须在 GitHub Settings → Pages 中保存；工作流文件本身不能替代这个设置。

### 3.4 拟提交范围（待执行）

实施提交只包含：

- `.github/workflows/deploy-pages.yml`
- `website/` 下的源码、配置、lockfile、第三方声明与页面实际引用的 WebP 产品图片；不包含重复 PNG 源图、构建缓存和依赖目录
- `docs/releases/website-1.0.0.md`
- `docs/releases/website-1.1.0.md`
- `docs/releases/website-1.2.0.md`
- `docs/releases/website-1.2.1.md`

明确排除：

- `.pnpm-store/`
- `tmp/`
- `.github/release-notes/v1.4.0-rc2.md`
- `website/public/product/*.png`（与已优化 WebP 重复，页面没有引用）
- `website/node_modules/`
- `website/dist/`
- `website/*.tsbuildinfo`

### 3.5 仓库隔离约束

- 不修改 `src/`、`desktop/`、Python/Tauri 打包入口或 Windows Release 工作流。
- Pages 工作流只读取 `website/`，其缓存键和 artifact 路径也只指向网站目录。
- 官网依赖使用 `website/pnpm-lock.yaml` 独立锁定，不复用或改写桌面端 Node 依赖。
- 未来 `main` 上只有 `website/**` 或 Pages 工作流变化时才自动部署官网；应用源码的普通变化不会无意义重建网站。

## 4. GitHub Pages 手动设置

代码推送到 `main` 后，用户需要在 GitHub 完成：

1. 打开 `zjy1346/OpenThesis` → **Settings** → **Pages**。
2. 在 **Build and deployment** → **Source** 选择 **GitHub Actions**。
3. 等待 `Deploy website to GitHub Pages` 工作流首次成功。
4. 在 **Custom domain** 输入 `openthesis.cc.cd` 并点击 **Save**。
5. DNS 生效、GitHub 签发证书后勾选 **Enforce HTTPS**。证书选项可能需要数分钟到 24 小时才可用。
6. 可选：在 **Settings** → **Environments** → `github-pages` 中限制只允许 `main` 部署。

## 5. DNSHE 手动 DNS 配置

`openthesis.cc.cd` 是自定义子域名，应使用一条 CNAME：

| 项目 | 值 |
| --- | --- |
| 类型 | `CNAME` |
| 记录名 / Host | 如果 DNSHE 管理 `cc.cd` 区域则填 `openthesis`；如果它把 `openthesis.cc.cd` 作为独立区域交给你管理则填 `@` |
| 目标 / Value | `zjy1346.github.io` |
| TTL | `600` 秒或 `Auto` |
| 代理 | 若有代理开关，首次签发证书前选择仅 DNS / 不代理 |

注意：

- 目标必须是 `zjy1346.github.io`，不能填写 `zjy1346.github.io/OpenThesis`，CNAME 值不允许带路径。
- 不需要为这个子域名添加 GitHub Pages 的四条 `A` 或 `AAAA` 记录。
- 删除或停用同一主机名上冲突的 `A`、`AAAA` 或其他 `CNAME`。
- 不要创建通配符 `*.cc.cd` 或 `*.openthesis.cc.cd` 指向 GitHub Pages。
- 推荐顺序：先在 GitHub Pages 保存 Custom domain，再添加 DNS CNAME，以降低域名被他人抢占配置的风险。

DNS 生效后可验证：

```powershell
Resolve-DnsName openthesis.cc.cd -Type CNAME
```

预期结果应最终指向 `zjy1346.github.io`。

## 6. 验收清单

### 6.1 本地与构建

- [ ] 从 lockfile 干净安装依赖成功。
- [ ] TypeScript 检查通过。
- [ ] Vite 生产构建通过。
- [ ] `website/dist/index.html` 位于部署 artifact 根目录。
- [ ] 生产 HTML 的 JS/CSS 路径以 `/assets/` 开头，产品图片路径以 `/product/` 开头。
- [ ] 本地 production preview 无控制台错误或资源 404。

### 6.2 GitHub Actions

- [x] Pages 工作流仅在 `main` 的网站相关变更或手动触发时运行。
- [x] 工作流拥有最小的 `contents: read`、`pages: write`、`id-token: write` 权限。
- [x] build 与 deploy jobs 正确通过 artifact 和 `needs` 连接。
- [x] `github-pages` environment 记录部署 URL。

### 6.3 路径与域名

- [x] Vite `base` 为 `/`。
- [ ] 首页、锚点导航、语言切换和产品图片在根域路径工作。
- [ ] `https://openthesis.cc.cd/` 返回网站首页而不是 404。
- [ ] 静态资源全部通过 HTTPS 加载，无 mixed content。
- [ ] GitHub Pages Custom domain 显示 `openthesis.cc.cd`。
- [ ] DNS CNAME 指向 `zjy1346.github.io`。
- [ ] Enforce HTTPS 可用并启用。

### 6.4 提交与发布

- [ ] 只暂存计划内文件，没有提交缓存、临时截图或测试版 Release Note。
- [ ] 没有提交未被网页引用的重复 PNG 源图。
- [ ] 提交前检查完整 staged diff。
- [ ] 必要检查全部通过后创建部署提交。
- [ ] 直接快进推送到 `origin/main`。
- [ ] 推送后检查 Pages workflow 状态和部署日志。
- [ ] 不访问或修改 DNSHE 账号。

### 6.5 最终线上效果验收

最终验收不是“工作流显示绿色”即可，而是确认线上网站与已经验收的本地预览保持一致：

- [ ] `https://openthesis.cc.cd/` 可直接正常打开并返回首页，不出现 GitHub Pages 404、重定向循环或证书警告。
- [ ] 线上首屏、六段滚动故事、能力矩阵、最终 CTA 和页脚内容与本地预览一致。
- [ ] 桌面端 GSAP/ScrollTrigger pin、scrub、遮罩、SVG 路径、证据放大、Agent 汇聚和深度堆叠动画正常工作，滚动无黑屏或场景重叠。
- [ ] `980px` 以下使用与本地一致的非 pin 紧凑内容流；`prefers-reduced-motion` 降级保持有效。
- [ ] 中文、繁体中文系统环境默认打开中文；其他语言环境默认打开英文；手动切换和跟随系统语言正常。
- [ ] 所有 `/assets/*` 与 `/product/*` 资源返回成功，真实公司 WebP 截图完整显示，无资源 404、跨域错误或 mixed content。
- [ ] 在 `1440×900` 和 `390×844` 对本地 production preview 与线上页面进行截图比对，不存在可见的排版、断行、图片比例或场景位置差异。
- [ ] 浏览器控制台无 error/warning；网络面板无失败资源。
- [ ] 官网可见内容仍不出现应用版本号，下载链接继续指向 GitHub `releases/latest`。
- [ ] 只有上述线上效果全部通过，才将本次 GitHub Pages 部署标记为完成。

## 7. 用户批准项

- [x] 确认采用同一仓库中的独立 `website/` 目录，不另建网站仓库。
- [x] 确认最终验收标准为线上网站正常打开，且视觉、内容、语言、截图和滚动动画效果与本地预览一致。
- [x] 批准新增 GitHub Pages Actions 工作流并设置 Vite `base: '/'`。
- [x] 批准提交本文件第 3.4 节列出的官网源码和四份网站变更记录。
- [x] 批准明确排除 `.pnpm-store/`、`tmp/` 和测试版 Release Note。
- [x] 批准完成检查后将部署提交直接快进推送到 `origin/main`。
- [x] GitHub CLI 的 `gh auth status` 实际返回登录成功。
