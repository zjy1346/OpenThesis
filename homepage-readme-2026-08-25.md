# OpenThesis 主页与 README 变更记录（2026-08-25）

## 记录范围

本文件是本窗口专用的变更记录，不采用版本号命名，不替代或修改其他窗口创建的版本记录。

## 用户要求

- [ ] GitHub 项目主页 About 区域改为英文在上、中文在下。
- [ ] README 语言入口按 `English`、`简体中文`、`繁體中文` 的顺序展示。
- [ ] 三种语言分别进入三个独立页面，不再把完整内容连续挤在同一个 README 页面中。
- [ ] 评估截图中 `.codex`、`build_support`、`AGENTS.md` 是否可以从网络仓库删除。

## 当前诊断

- 根目录 `README.md` 当前连续包含简体中文、英文、繁体中文三个区块，顶部语言导航顺序也是简体中文、英文、繁体中文。
- 仓库已有 `README.zh-CN.md` 与 `README.zh-Hant.md`，但没有独立英文 README。建议根目录 `README.md` 作为英文独立页面，并让三个文件互相链接。
- GitHub About 简介属于远程仓库元数据，不由 README 控制。后续需要在用户明确授权发布后更新。
- `build_support/version_info.txt` 被 `OpenThesis.spec` 直接读取，是打包版本输入。
- `.codex` 包含项目级 Codex 配置和 `lunahigh` 配置；`AGENTS.md` 包含本仓库开发、验收、打包和发布约束。

## 方案

1. 将根目录 `README.md` 整理为英文独立页面。
2. 将三个 README 的导航统一为 `English` → `简体中文` → `繁體中文`，使用文件链接而不是页内锚点。
3. 保留 `.codex`、`build_support`、`AGENTS.md`，本项不删除。
4. 远程 About 简介在后续获得明确发布授权后更新为英文段落在上、中文段落在下。

## 验收清单

### README

- [ ] 默认 README 只显示英文正文。
- [ ] 三个 README 的语言导航顺序均为 English、简体中文、繁體中文。
- [ ] 三个入口分别打开三个独立页面。
- [ ] 图片、徽章、相对链接、下载链接和许可证链接正常。

### About 与删除评估

- [ ] About 简介英文位于中文之前，且仅在用户授权发布后更新。
- [ ] `.codex`、`build_support`、`AGENTS.md` 保留。
- [ ] `OpenThesis.spec` 仍能读取 `build_support/version_info.txt`。

## 当前状态

- 已完成主页、README 和删除候选的只读诊断。
- 尚未修改 README、应用代码或远程仓库元数据。
- 未删除任何项目源码或配置；仅将上一轮本窗口创建的 `2.0.2.md` 替换为本文件。
- 等待用户批准后再实施主页与 README 调整。
