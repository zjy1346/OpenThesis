# ADR-0001: Tauri desktop shell with a Python research core

- Status: Accepted
- Date: 2026-08-02
- Target release: OpenThesis 1.0.0

## Context

The Tkinter interface has become a shallow module: layout, animation, research
orchestration, persistence, and platform behavior meet in one large interface.
That limits motion quality and makes every visual change risky. OpenThesis needs
a Windows-first desktop experience without closing the path to a future macOS
application.

## Decision

OpenThesis 1.0 uses three deep modules separated by stable seams:

1. A React and TypeScript workbench owns presentation and interaction.
2. A Tauri 2 platform adapter owns desktop windows, process lifecycle, packaging,
   and platform-specific paths.
3. The existing Python research core owns SEC access, evidence, model providers,
   workflows, reports, and SQLite persistence.

The desktop adapter communicates with the Python core through a versioned JSON-RPC
2.0 line protocol. API keys remain session-only and are never persisted or echoed
in protocol errors. The legacy Tkinter interface remains available during migration
until the new workbench reaches feature parity.

## Portability constraints

- Core Python modules and protocol contracts contain no Windows-only behavior.
- Platform data directories live behind one adapter and follow Windows, macOS, and
  XDG conventions.
- Sidecar binaries are built per target triple; Windows builds never stand in for
  macOS verification.
- React code never launches Python or reads native paths directly.
- CSS must work in both WebView2 and WebKit and honor `prefers-reduced-motion`.
- macOS signing, notarization, and final runtime verification will run on macOS.
- Mobile is explicitly outside the 1.0 scope.

## Consequences

The new seams increase leverage: the same Python implementation serves both Windows
and future macOS adapters, while the workbench can be tested without native process
startup. Packaging becomes target-specific, but domain behavior and protocol tests
remain shared.
