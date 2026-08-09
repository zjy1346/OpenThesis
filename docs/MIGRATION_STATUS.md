# OpenThesis Windows architecture migration status

This document is the release gate for replacing the legacy Tkinter desktop UI.
It distinguishes implemented behavior from work that still blocks removal of the
legacy application. A checked item means the behavior is covered by an automated
contract, unit, or smoke test unless explicitly marked as manual.

## Completed in `1.0.0-alpha.3`

- [x] React and TypeScript own presentation and interaction.
- [x] Tauri 2 owns the native window, sidecar lifecycle, and Windows packaging.
- [x] The Python research core is exposed through a versioned JSON-RPC 2.0 seam.
- [x] Core modules do not import Tkinter or Windows-only APIs.
- [x] API keys remain session-only and protocol errors do not echo secrets.
- [x] Company search, SEC identity, model catalog/discovery/connection testing,
  model comparison, research packs, reverse DCF inputs, progress, cancellation,
  recovery of interrupted runs, report history, and thesis versioning are wired
  through the new workbench.
- [x] Reports support a large reading workspace, focus mode, 90%-130% zoom, and
  opt-in technical evidence identifiers.
- [x] Report export uses a platform-owned native save dialog. React never sees the
  selected filesystem path, and standalone HTML, Markdown, and text remain
  available on both Windows and future macOS builds.
- [x] Provider help links are opened only after HTTPS validation in the Tauri
  adapter; the About view exposes versioned architecture, privacy, and scope
  diagnostics without exposing local paths.
- [x] Imported `.othesis` packs cross the protocol as validated content; the web
  workbench never receives a native filesystem path.
- [x] The Windows portable archive runs after extraction without installation and
  launches both the GUI and sidecar without a visible console window.
- [x] Windows-specific GUI subsystem configuration is target-gated.
- [x] A macOS Tauri overlay defines `.app` and `.dmg` bundle targets without
  changing the shared frontend or Python core.
- [x] Mobile remains outside the 1.0 scope.
- [x] The React shell delegates research sessions, report reading, research
  setup, thesis editing, settings, diagnostics, and localization to focused
  modules; only the desktop adapter imports Tauri.
- [x] The platform-neutral TypeScript JSON-RPC contract centralizes method,
  parameter, and result types; Tauri remains an adapter rather than a type
  source for shared React features.
- [x] Legacy behavior parity and intentional retirements are documented in
  `docs/LEGACY_PARITY.md`.

## Completed in `1.2.0`

- [x] One platform-neutral market Interface routes US, China A-share, and Hong
  Kong company search without placing exchange logic in the React workbench.
- [x] Issuers and listed securities have separate persisted identities, with
  SSE, SZSE, BSE, and HKEX adapters behind the same research seam.
- [x] CNInfo and HKEXnews official reports normalize conservative PDF facts with
  page evidence, currency, accounting standard, and explicit missing-data behavior.
- [x] Manual market snapshots are dated and source-labelled; Financials Beta and
  cross-currency valuation guards are enforced by the research core.

## Remaining for the Windows release line

- [ ] Verify accessibility and high-DPI behavior manually on supported Windows
  display scales in addition to automated `980 x 680` smoke coverage.

## Deferred future work (not Windows release gates)

- [ ] Build, sign, notarize, and runtime-test the macOS sidecar and application on
  real macOS hardware or a macOS CI runner. This is explicitly deferred and is
  not part of the Windows release work; the shared React, protocol, Python core,
  and Tauri resource seam remain platform-neutral so it can be resumed later.
- [ ] When the migration is resumed, switch the Python console-script entry point
  away from `openthesis.app`, remove Tkinter/WebView compatibility dependencies
  from the default installation, and delete the legacy UI in a separate reviewable
  change.

## Exit criteria

The Windows migration is complete when all Windows criteria below are true. The
macOS criteria are a future project, not a requirement for this Windows stop point:

1. The JSON-RPC contract covers every supported user workflow and rejects malformed
   or secret-bearing persistence requests.
2. The React workbench passes unit, keyboard, reduced-motion, minimum-window, and
   high-DPI smoke tests.
3. Windows portable packaging passes structure, checksum, privacy, PE-subsystem,
   launch, sidecar-count, and no-visible-console checks.

---

# OpenThesis 1.0 架构迁移状态

`1.0.0-alpha.3` 已完成 React/Tauri 工作台、Python 研究核心协议化、免安装
Windows 便携包、无命令行窗口启动，以及未来 macOS 所需的平台隔离。旧 Tkinter
界面目前仍保留在源码中作为迁移期间的兼容入口，并不代表迁移已经结束。

Windows 版本目前只剩多种高 DPI 场景的人工验证。macOS 构建、签名、公证和运行
验证明确延期，不在本次 Windows 工作范围内；共享 React、协议、Python 核心和
Tauri 资源 seam 保持跨平台，以便未来继续。旧界面及其默认依赖也保留为迁移期间
兼容入口，不用未经验证的 macOS 声明阻塞 Windows 发布。
