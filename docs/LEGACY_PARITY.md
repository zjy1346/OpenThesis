# OpenThesis 1.0 legacy parity audit

This audit maps the legacy Tkinter desktop workflows to the React/Tauri
workbench. It is a product-behavior audit, not a claim that the old and new
interfaces share the same layout or implementation.

| Legacy behavior | React/Tauri destination | Decision |
| --- | --- | --- |
| Company selection and synthetic demo | New research and report-first empty state | Preserved |
| SEC identity, contact email, and help | New research step 01 with inline help and official SEC link | Preserved |
| Provider preset, online models, connection test, session API Key | New research step 02 | Preserved |
| Optional comparison model | New research step 02 | Preserved |
| Research pack selection and `.othesis` import | New research step 03 | Preserved |
| Reverse DCF inputs | Collapsed advanced settings in step 03 | Preserved |
| Progress, cancellation, failure feedback, retry | Fixed workbench status and error regions | Preserved |
| Research history and manual refresh | Collapsible global history drawer | Preserved |
| Report export, zoom, technical details, focus reading | Report workspace toolbar | Preserved |
| Append-only thesis versions and JSON editing | Investment theses view | Preserved |
| Interface/report language settings | Settings view | Preserved |
| Separate model and research-pack tabs | Guided new-research flow | Intentionally consolidated |
| Clear the currently displayed report | Select another report or start a new run | Intentionally retired; clearing added no durable state change |
| Animated collapse of research controls | Dedicated report-first workspace | Intentionally retired; the layout no longer needs this control |
| Expandable low-level progress details | Concise progress message and percentage | Intentionally retired; durable intermediate results remain in history |

## Architectural result

The shell composes feature modules and owns navigation only. Research session
orchestration lives behind `useWorkbenchSession`; report reading, research setup,
thesis editing, settings, and diagnostics each live behind their own interface.
The method/parameter/result contract lives in the Tauri-free
`desktop/src/protocol.ts`; only `desktop/src/backend.ts` imports the Tauri
JavaScript interface. Native
filesystem dialogs, external URL validation, sidecar lifecycle, and platform
packaging remain in Rust, so the shared React features and Python research core
do not branch on Windows or macOS. A future macOS adapter can implement the
same protocol seam without changing the workbench modules.
